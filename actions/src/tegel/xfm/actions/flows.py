"""The XFM ETL as Prefect flows, so every conversion can be monitored.

- ``xfm-etl-raw-directory``: one pass over a raw directory. It finds files that
  are ready (stable for ``stable`` seconds, not yet in the ledger), converts them
  batch by batch (one task run per batch, retried once), ingests the new crates
  into the data server's catalog, and publishes a table artifact of the scans.
  Served on an interval, it is the automatic watcher.
- ``xfm-etl-files``: the same for an explicit list of files. The ``xfm.etl``
  action calls it.

Both write crates under ``TEGEL_RECORDS`` (tree layout, ``index.csv``, ledger in
``.xfm-etl/``) and ingest through ``tegel.xfm.actions.ingest``.

Command line (``tegel-xfm-etl``)::

    tegel-xfm-etl serve --raw DIR --reader R [--interval 60] [--stable 60] [--study S]
    tegel-xfm-etl run   --raw DIR --reader R          # one pass, then exit
    tegel-xfm-etl files --reader R FILE...            # convert these files once

``serve`` needs a Prefect API (``PREFECT_API_URL``); ``run`` and ``files`` work with
or without one (without, Prefect keeps the run in a temporary local API).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from prefect import flow, get_run_logger, serve, task
from prefect.artifacts import create_markdown_artifact, create_table_artifact

from tegel.xfm.actions.ingest import ingest_crates, records_dir
from xfm_etl import readers
from xfm_etl.watch import LEDGER, Ledger, pending
from xfm_etl.write import write_scan

RAW_FLOW = "xfm-etl-raw-directory"
FILES_FLOW = "xfm-etl-files"


@task(name="find-ready-files")
def find_ready(raw: str, reader: str, stable: float, study: str | None) -> list[dict[str, Any]]:
    ledger = Ledger(records_dir() / LEDGER)
    batches = pending(Path(raw).resolve(), readers.get(reader), ledger, stable=stable,
                      study=study)
    ledger.save()  # keeps "changed after conversion" notes
    get_run_logger().info("%d batch(es) ready under %s", len(batches), raw)
    return [{"study": b.study, "files": [str(f) for f in b.files]} for b in batches]


@task(name="convert-batch", retries=1, retry_delay_seconds=10,
      task_run_name="convert {batch[files][0]}")
def convert_batch(batch: dict[str, Any], reader: str, samples: dict[str, Any] | None,
                  layout: str) -> list[dict[str, str]]:
    """Read one batch with ``reader`` and write its scan crates; return ids and paths."""
    records = records_dir()
    written = []
    for scan in readers.read(reader, [Path(f) for f in batch["files"]]):
        scan.meta.study = batch["study"]
        if samples and scan.meta.sample_code in samples:
            scan.meta.sample = samples[scan.meta.sample_code]
        crate = write_scan(scan, records, layout=layout)
        written.append({"id": crate.name, "path": str(crate)})
    get_run_logger().info("%s -> %s", ", ".join(Path(f).name for f in batch["files"]),
                          ", ".join(item["id"] for item in written))
    return written


@task(name="ingest-crates")
def ingest(crates: list[dict[str, str]]) -> dict[str, Any]:
    return ingest_crates([Path(item["path"]) for item in crates], get_run_logger())


def _convert_all(batches: list[dict[str, Any]], reader: str, samples: dict[str, Any] | None,
                 layout: str, *, ledger: bool,
                 ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Convert each batch; a failing batch is recorded and does not stop the others."""
    book = Ledger(records_dir() / LEDGER) if ledger else None
    crates: list[dict[str, str]] = []
    failed: list[dict[str, Any]] = []
    for batch in batches:
        files = [Path(f) for f in batch["files"]]
        try:
            written = convert_batch(batch, reader, samples, layout)
        except Exception as exc:
            get_run_logger().error("failed: %s: %s", [f.name for f in files], exc)
            failed.append({"files": batch["files"], "error": str(exc)[:500]})
            if book is not None:
                for file in files:
                    book.record(file, "failed", error=str(exc)[:500])
        else:
            crates.extend(written)
            if book is not None:
                for file in files:
                    book.record(file, "done", crates=[item["id"] for item in written],
                                study=batch["study"])
        if book is not None:
            book.save()
    return crates, failed


def _report(crates: list[dict[str, str]], failed: list[dict[str, Any]],
            ingested: dict[str, Any]) -> None:
    import csv

    if not crates and not failed and not ingested.get("errors"):
        return  # an idle pass leaves no artifact
    ids = [item["id"] for item in crates]
    index = records_dir() / "index.csv"
    rows = []
    if ids and index.is_file():
        with index.open(newline="", encoding="utf-8") as handle:
            wanted = set(ids)
            rows = [
                {key: row[key] for key in ("id", "study", "sample_code", "scan_name",
                                           "map_shape", "mca_channels", "path")}
                for row in csv.DictReader(handle) if row["id"] in wanted
            ]
    if rows:
        create_table_artifact(key="xfm-etl-scans", table=rows,
                              description="Scans converted and ingested by this run")
    lines = [
        f"**Converted:** {len(ids)} scan(s)  ",
        f"**Staged:** {ingested.get('staged', 0)}, skipped {ingested.get('skipped', 0)}  ",
        f"**Catalog refreshed:** {ingested.get('refreshed', False)}  ",
        f"**Failed batches:** {len(failed)}",
    ]
    for item in failed:
        lines.append(f"- {', '.join(Path(f).name for f in item['files'])}: {item['error']}")
    for error in ingested.get("errors", []):
        lines.append(f"- not staged: {error['path']} ({error['stage']}): {error['message']}")
    create_markdown_artifact(key="xfm-etl-summary", markdown="\n".join(lines),
                             description="XFM ETL run summary")


@flow(name=RAW_FLOW, log_prints=True)
def etl_raw_directory(raw: str, reader: str, stable: float = 60.0, study: str | None = None,
                      samples: dict[str, Any] | None = None,
                      layout: str = "tree") -> dict[str, Any]:
    """Convert and ingest every ready, not yet converted file under ``raw``."""
    readers.get(reader)  # fail fast on an unknown reader
    batches = find_ready(raw, reader, stable, study)
    crates, failed = _convert_all(batches, reader, samples, layout, ledger=True)
    ingested = ingest(crates) if crates else {"staged": 0, "skipped": 0, "errors": [],
                                              "refreshed": False}
    _report(crates, failed, ingested)
    return {"crates": crates, "failed": failed, **ingested}


@flow(name=FILES_FLOW, log_prints=True)
def etl_files(files: list[str], reader: str, study: str | None = None,
              samples: dict[str, Any] | None = None, layout: str = "tree") -> dict[str, Any]:
    """Convert and ingest exactly these files (all in one study)."""
    readers.get(reader)
    batches = [{"study": study, "files": [str(Path(f)) for f in group]}
               for group in readers.get(reader).group([Path(f) for f in files])]
    crates, failed = _convert_all(batches, reader, samples, layout, ledger=False)
    if failed and not crates:
        raise RuntimeError(f"no batch could be converted: {failed}")
    ingested = ingest(crates) if crates else {"staged": 0, "skipped": 0, "errors": [],
                                              "refreshed": False}
    _report(crates, failed, ingested)
    return {"crates": crates, "failed": failed, **ingested}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tegel-xfm-etl")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "run"):
        command = sub.add_parser(name)
        command.add_argument("--raw", required=True)
        command.add_argument("--reader", required=True)
        command.add_argument("--stable", type=float, default=60.0)
        command.add_argument("--study", default=None)
        command.add_argument("--sample-sheet", default="")
        command.add_argument("--layout", choices=["tree", "flat"], default="tree")
        if name == "serve":
            command.add_argument("--interval", type=float, default=60.0)
    files = sub.add_parser("files")
    files.add_argument("files", nargs="+")
    files.add_argument("--reader", required=True)
    files.add_argument("--study", default=None)
    files.add_argument("--sample-sheet", default="")
    args = parser.parse_args(argv)
    samples = yaml.safe_load(Path(args.sample_sheet).read_text("utf-8")) if args.sample_sheet \
        else None
    records_dir()  # fail fast without TEGEL_RECORDS
    if args.command == "files":
        print(etl_files([str(Path(f).resolve()) for f in args.files], args.reader,
                        study=args.study, samples=samples))
        return
    parameters = {"raw": str(Path(args.raw).resolve()), "reader": args.reader,
                  "stable": args.stable, "study": args.study, "samples": samples,
                  "layout": args.layout}
    if args.command == "run":
        print(etl_raw_directory(**parameters))
        return
    raw_deployment = etl_raw_directory.to_deployment(
        name="watch", interval=args.interval, parameters=parameters,
        description=f"Convert new raw XFM files under {parameters['raw']}",
        tags=["xfm", "etl"],
    )
    files_deployment = etl_files.to_deployment(name="files", tags=["xfm", "etl"])
    serve(raw_deployment, files_deployment, limit=1)


if __name__ == "__main__":
    main()
