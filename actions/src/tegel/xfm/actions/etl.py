"""``xfm.etl``: the catalog action that turns beamline files into served raw scans.

It runs on an action server deployed next to the data server, because it writes
that data server's catalog:

1. converts the selected beamline files with an ``xfm-etl`` reader into scan
   crates under the data server's records directory;
2. stages the new crates with the plugin and merges them into the live catalog;
3. asks the data server to reopen its catalog (``POST /refresh-catalog``).

Configuration comes from the action server's environment:

- ``TEGEL_RECORDS``: the data server's records directory, where crates are written
- ``TEGEL_CATALOG_URI``: the data server's catalog (``sqlite:///...``)
- ``TEGEL_PROFILE``, ``TEGEL_SCHEMA_ROOT``: as for the data server, used by staging
- ``TEGEL_REFRESH_URL``: the data server's ``/refresh-catalog``; with
  ``TEGEL_REFRESH_TOKEN``, or else the OIDC client settings ``tegel.actions.refresh``
  uses. Unset, the catalog is merged but not reopened (a restart picks it up).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from tegel.actionserver import action, port


@action(
    "xfm.etl",
    version="0.1.0",
    inputs=[
        port("files", "selection",
             description="beamline files (file: URIs under the server's readable storage)"),
    ],
    outputs=[port("ingested", "json", description="crates written and staged, with errors")],
    scope="catalog:write",
    resources=["catalog-writer"],
    software=["xfm-ssrl-etl", "tegel-actions", "zarr"],
    license="BSD-3-Clause",
)
def etl(
    ctx,
    files,
    *,
    reader: str,
    study: str = "",
    samples: dict[str, Any] | None = None,
):
    """Convert beamline files to OME-Zarr scan crates and ingest them into the catalog.

    ``reader`` is an ``xfm-etl`` reader (``xfm-etl readers``). ``study`` names the
    aims-leaf Study. ``samples`` maps sample codes to aims-leaf PlantSample fields.
    """
    from tegel.actions.merge import merge_staging
    from tegel.actions.staging import write_staging
    from xfm_etl import readers, write_scan

    records = _records_dir()
    catalog = _catalog_path()
    paths = [Path(item) for item in files]
    scans = readers.read(reader, paths)
    if not scans:
        raise ValueError("the reader found no scans in the selected files")
    written = []
    for scan in scans:
        scan.meta.study = study or None
        if samples and scan.meta.sample_code in samples:
            scan.meta.sample = samples[scan.meta.sample_code]
        crate = write_scan(scan, records)
        written.append({"id": crate.name, "scan_name": scan.meta.scan_name, "path": str(crate)})
        ctx.log.info("wrote %s (%s)", crate.name, scan.meta.scan_name)
    staging = ctx.work_dir / "staging.db"
    result = write_staging(
        [str(Path(item["path"]) / "ro-crate-metadata.json") for item in written],
        staging,
        roots=[records],
        error_log=ctx.work_dir / "errors.jsonl",
    )
    errors = [{"path": e.path, "stage": e.stage, "message": e.message} for e in result.errors]
    if result.staged == 0:
        raise RuntimeError(f"no crate could be staged: {errors}")
    merge_staging(str(staging), str(catalog))
    refreshed = _refresh(ctx)
    ctx.log.info("ingested %d crate(s) into %s; refreshed=%s", result.staged, catalog, refreshed)
    return {
        "ingested": {
            "crates": written,
            "staged": result.staged,
            "skipped": result.skipped,
            "errors": errors,
            "catalog": str(catalog),
            "refreshed": refreshed,
        }
    }


def _records_dir() -> Path:
    value = os.environ.get("TEGEL_RECORDS", "").strip()
    if not value:
        raise RuntimeError("TEGEL_RECORDS (the data server's records directory) is not set")
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _catalog_path() -> Path:
    uri = os.environ.get("TEGEL_CATALOG_URI", "").strip()
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise RuntimeError("TEGEL_CATALOG_URI must name the data server's sqlite catalog")
    return Path(uri[len(prefix):])


def _refresh(ctx) -> bool:
    url = os.environ.get("TEGEL_REFRESH_URL", "").strip()
    if not url:
        ctx.log.warning("TEGEL_REFRESH_URL unset: the data server sees new crates after restart")
        return False
    token = os.environ.get("TEGEL_REFRESH_TOKEN", "").strip()
    if token:
        response = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        response.raise_for_status()
        return True
    from tegel.actions.refresh import refresh_catalog

    refresh_catalog()
    return True
