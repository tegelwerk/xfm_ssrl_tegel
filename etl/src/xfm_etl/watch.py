"""Watch a directory of raw beamline files and convert new ones automatically.

Every ``interval`` seconds the watcher lists the files under the raw directory
that the reader's patterns match. A file is ready once it has not changed for
``stable`` seconds, so files still being written are left alone. Ready files are
grouped into scans by the reader and handed to ``convert`` one batch per study.

The study of a file is ``--study`` if given, else the first folder under the raw
directory (``raw/aapl/...`` -> ``aapl``); files at the top level have none.

A ledger (``<out>/.xfm-etl/ledger.json``) records each file's size, mtime,
outcome and crates, so nothing is converted twice. A file that changes after
conversion is reported, not reconverted. A failed file is retried once it changes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xfm_etl import readers
from xfm_etl.write import write_scan

log = logging.getLogger("xfm_etl.watch")
LEDGER = Path(".xfm-etl") / "ledger.json"


@dataclass
class Batch:
    """Files that make up whole scans of one study."""

    study: str | None
    files: list[Path]


Convert = Callable[[Batch], list[str]]


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if path.is_file():
            self.entries = json.loads(path.read_text(encoding="utf-8"))

    def get(self, file: Path) -> dict[str, Any] | None:
        return self.entries.get(str(file))

    def record(self, file: Path, status: str, **fields: Any) -> None:
        stat = file.stat()
        self.entries[str(file)] = {
            "status": status,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            **fields,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.entries, indent=1, sort_keys=True), "utf-8")
        os.replace(temporary, self.path)


def _unchanged(entry: dict[str, Any], file: Path) -> bool:
    stat = file.stat()
    return entry.get("size") == stat.st_size and entry.get("mtime") == stat.st_mtime


def pending(raw: Path, reader: readers.Reader, ledger: Ledger, *, stable: float,
            study: str | None = None, now: float | None = None) -> list[Batch]:
    """Batches of ready files not yet converted, grouped by study and then by scan."""
    now = time.time() if now is None else now
    found: set[Path] = set()
    for pattern in reader.patterns:
        found.update(p for p in raw.rglob(pattern) if p.is_file())
    by_study: dict[str | None, list[Path]] = {}
    for file in sorted(found):
        relative = file.relative_to(raw)
        if any(part.startswith(".") for part in relative.parts):
            continue
        entry = ledger.get(file)
        if entry is not None:
            if _unchanged(entry, file):
                continue
            if entry.get("status") == "done":
                if not entry.get("changed_reported"):
                    log.warning("%s changed after conversion; not reconverting", file)
                    entry["changed_reported"] = True
                continue
        if now - file.stat().st_mtime < stable:
            continue
        key = study if study else (relative.parts[0] if len(relative.parts) > 1 else None)
        by_study.setdefault(key, []).append(file)
    batches = []
    for key, files in sorted(by_study.items(), key=lambda item: item[0] or ""):
        for group in reader.group(files):
            batches.append(Batch(study=key, files=list(group)))
    return batches


def local_converter(reader: str, out: Path, *, samples: dict[str, Any] | None = None,
                    layout: str = "tree") -> Convert:
    """Convert a batch in this process: read with ``reader`` and write crates to ``out``."""

    def convert(batch: Batch) -> list[str]:
        ids = []
        for scan in readers.read(reader, batch.files):
            scan.meta.study = batch.study  # the folder (or --study) decides, not the reader
            if samples and scan.meta.sample_code in samples:
                scan.meta.sample = samples[scan.meta.sample_code]
            ids.append(write_scan(scan, out, layout=layout).name)
        return ids

    return convert


def watch(raw: Path, out: Path, reader: str, convert: Convert, *, interval: float = 30.0,
          stable: float = 60.0, study: str | None = None, once: bool = False,
          stop: Callable[[], bool] = lambda: False) -> int:
    """Poll ``raw`` and convert ready files until ``stop()``; return the number of crates.

    With ``once``, a single pass is made.
    """
    raw = raw.resolve()
    spec = readers.get(reader)
    ledger = Ledger(out / LEDGER)
    total = 0
    log.info("watching %s for %s (reader %s); crates in %s", raw, spec.patterns, reader, out)
    while True:
        for batch in pending(raw, spec, ledger, stable=stable, study=study):
            names = ", ".join(file.name for file in batch.files)
            try:
                ids = convert(batch)
            except Exception as exc:  # one bad batch must not stop the watcher
                log.error("failed: %s: %s", names, exc)
                for file in batch.files:
                    ledger.record(file, "failed", error=str(exc)[:500])
            else:
                total += len(ids)
                log.info("converted %s -> %s", names, ", ".join(ids))
                for file in batch.files:
                    ledger.record(file, "done", crates=ids, study=batch.study)
            ledger.save()
        if once or stop():
            return total
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline:
            if stop():
                return total
            time.sleep(min(1.0, interval))
