"""Tegel's ingest-raw flow with the XFM converter, found by entry point."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tegel.actions import ingest
from tegel.actions.merge import merge_staging
from tegel.xfm.actions.serve import stage

DATASERVER = Path(__file__).resolve().parents[2] / "dataserver"


def _keys(catalog: Path) -> set[str]:
    with sqlite3.connect(catalog) as connection:
        return {row[0] for row in connection.execute("SELECT key FROM nodes")}


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_raw_files_become_served_scans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw, records, output = tmp_path / "raw", tmp_path / "records", tmp_path / "out"
    records.mkdir()
    output.mkdir()
    monkeypatch.setenv("TEGEL_PROFILE", str(DATASERVER / "schema" / "profile.yaml"))
    monkeypatch.setenv("TEGEL_SCHEMA_ROOT", str(DATASERVER.parent / "schemas"))
    stage(records, output, [records])  # the data server's starting catalog
    for name, value in {"TEGEL_OUTPUT": output, "TEGEL_RECORDS": records, "TEGEL_RAW": raw,
                        "XFM_ETL_READER": "synthetic"}.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("TEGEL_CONVERTERS_CONFIG", raising=False)
    monkeypatch.delenv("TEGEL_READABLE_STORAGE", raising=False)

    class Done:
        class state:
            @staticmethod
            def is_completed() -> bool:
                return True

    def updater(**kwargs):  # what update-catalog/catalog-updater does
        merge_staging(kwargs["parameters"]["staging_path"],
                      kwargs["parameters"]["production_path"])
        return Done()

    monkeypatch.setattr(ingest, "run_deployment", updater)
    monkeypatch.setattr(ingest, "refresh_catalog", lambda: None)
    monkeypatch.setattr(ingest, "_find_ready", ingest._find_ready.fn)
    monkeypatch.setattr(ingest, "_convert", ingest._convert.fn)
    monkeypatch.setattr(ingest, "_run_id", lambda: "test")
    monkeypatch.setattr(ingest, "_report_raw", lambda *args: None)

    _touch(raw / "aapl" / "AAPL_10serp_P1_leaf1.h5")
    _touch(raw / "radish" / "RAD_leaf2.h5")
    assert ingest.ingest_raw.fn(stable=0) == 2
    crates = sorted(p.parent for p in records.rglob("ro-crate-metadata.json"))
    assert [c.parent.parent.name for c in crates] == ["aapl", "radish"]
    assert {c.name for c in crates} <= _keys(output / "catalog.db")
    assert ingest.ingest_raw.fn(stable=0) == 0
    _touch(raw / "aapl" / "AAPL_10serp_P2_leaf1.h5")
    assert ingest.ingest_raw.fn(stable=0) == 1
    assert (records / "index.csv").read_text().count("\n") == 4
