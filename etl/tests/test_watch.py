"""The watcher: ready files only, study from folders, ledger, tree layout and index."""

import csv
import logging
import os
import time
from pathlib import Path

import pytest

from xfm_etl import readers
from xfm_etl.cli import main
from xfm_etl.watch import LEDGER, Batch, Ledger, local_converter, pending, watch


def _touch(path: Path, age: float = 0.0, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def _index(out: Path) -> list[dict[str, str]]:
    with (out / "index.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_one_pass_builds_the_outline(tmp_path: Path) -> None:
    raw, out = tmp_path / "raw", tmp_path / "out"
    _touch(raw / "aapl" / "AAPL_10serp_P1_leaf1.h5")
    _touch(raw / "radish" / "deep" / "RAD_leaf2.h5")
    _touch(raw / "loose.h5")
    _touch(raw / "notes.txt")
    _touch(raw / ".hidden" / "skip.h5")
    total = watch(raw, out, "synthetic", local_converter("synthetic", out), stable=0, once=True)
    assert total == 3
    rows = _index(out)
    assert sorted(row["study"] for row in rows) == ["", "aapl", "radish"]
    for row in rows:
        crate = out / row["path"]
        assert (crate / "ro-crate-metadata.json").is_file()
        assert crate.name == row["id"] and row["id"].startswith("xfm_")
        assert row["sources"].endswith(".h5")
    assert {row["path"].split("/")[0] for row in rows} == {"aapl", "radish", "_unassigned"}
    assert (out / "README.md").is_file()
    assert (out / LEDGER).is_file()
    again = watch(raw, out, "synthetic", local_converter("synthetic", out), stable=0, once=True)
    assert again == 0
    _touch(raw / "aapl" / "AAPL_10serp_P2_leaf1.h5")
    assert watch(raw, out, "synthetic", local_converter("synthetic", out), stable=0, once=True) == 1
    assert len(_index(out)) == 4


def test_files_still_being_written_wait(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    fresh = _touch(raw / "a.h5")
    ledger = Ledger(tmp_path / "ledger.json")
    spec = readers.get("synthetic")
    assert pending(raw, spec, ledger, stable=60) == []
    later = pending(raw, spec, ledger, stable=60, now=fresh.stat().st_mtime + 61)
    assert [batch.files for batch in later] == [[fresh]]


def test_explicit_study_overrides_folders(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _touch(raw / "x" / "a.h5")
    _touch(raw / "b.h5")
    batches = pending(raw, readers.get("synthetic"), Ledger(tmp_path / "l.json"), stable=0,
                      study="aapl")
    assert {batch.study for batch in batches} == {"aapl"}


def test_changed_and_failed_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    raw, out = tmp_path / "raw", tmp_path / "out"
    done = _touch(raw / "done.h5", age=5)
    watch(raw, out, "synthetic", local_converter("synthetic", out), stable=0, once=True)
    _touch(done, content=b"rewritten")
    with caplog.at_level(logging.WARNING):
        assert watch(raw, out, "synthetic", local_converter("synthetic", out), stable=0,
                     once=True) == 0
    assert "changed after conversion" in caplog.text

    bad = _touch(raw / "bad.h5", age=5)

    def broken(batch: Batch) -> list[str]:
        raise RuntimeError("reader exploded")

    assert watch(raw, out, "synthetic", broken, stable=0, once=True) == 0
    entry = Ledger(out / LEDGER).get(bad)
    assert entry["status"] == "failed" and "exploded" in entry["error"]
    assert watch(raw, out, "synthetic", local_converter("synthetic", out), stable=0,
                 once=True) == 0  # unchanged failed file is not retried
    _touch(bad, content=b"fixed")
    assert watch(raw, out, "synthetic", local_converter("synthetic", out), stable=0,
                 once=True) == 1


def test_cli_watch_once(tmp_path: Path) -> None:
    raw, out = tmp_path / "raw", tmp_path / "out"
    _touch(raw / "aapl" / "AAPL_x.h5")
    main(["watch", str(raw), "--reader", "synthetic", "--out", str(out), "--stable", "0",
          "--once"])
    assert len(_index(out)) == 1
