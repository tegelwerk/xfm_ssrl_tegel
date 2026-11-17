"""The Prefect ETL flows against a live catalog (Prefect's temporary local API)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tegel.xfm.actions.flows import etl_files, etl_raw_directory
from tegel.xfm.actions.serve import stage
from xfm_etl.watch import LEDGER, Ledger

DATASERVER = Path(__file__).resolve().parents[2] / "dataserver"


def _keys(catalog: Path) -> set[str]:
    with sqlite3.connect(catalog) as connection:
        return {row[0] for row in connection.execute("SELECT key FROM nodes")}


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    records, output = tmp_path / "records", tmp_path / "out"
    records.mkdir()
    output.mkdir()
    monkeypatch.setenv("TEGEL_PROFILE", str(DATASERVER / "schema" / "profile.yaml"))
    monkeypatch.setenv("TEGEL_SCHEMA_ROOT", str(DATASERVER.parent / "schemas"))
    stage(records, output, [records])
    monkeypatch.setenv("TEGEL_RECORDS", str(records))
    monkeypatch.setenv("TEGEL_CATALOG_URI", f"sqlite:///{output / 'catalog.db'}")
    monkeypatch.delenv("TEGEL_REFRESH_URL", raising=False)
    monkeypatch.delenv("XFM_CATALOG_UPDATER", raising=False)
    return tmp_path


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_raw_directory_flow_converts_once_and_ingests(deployment: Path) -> None:
    raw = deployment / "raw"
    first = _touch(raw / "aapl" / "AAPL_10serp_P1_leaf1.h5")
    _touch(raw / "radish" / "RAD_leaf2.h5")
    report = etl_raw_directory(str(raw), "synthetic", stable=0)
    assert report["staged"] == 2 and report["failed"] == []
    ids = {crate["id"] for crate in report["crates"]}
    assert ids <= _keys(deployment / "out" / "catalog.db")
    assert Ledger(deployment / "records" / LEDGER).get(first)["status"] == "done"
    assert etl_raw_directory(str(raw), "synthetic", stable=0)["crates"] == []
    _touch(raw / "aapl" / "AAPL_10serp_P2_leaf1.h5")
    again = etl_raw_directory(str(raw), "synthetic", stable=0)
    assert len(again["crates"]) == 1
    assert "/aapl/" in again["crates"][0]["path"]


def test_files_flow_converts_the_given_files(deployment: Path) -> None:
    files = [str(_touch(deployment / "bl" / "x.h5")), str(_touch(deployment / "bl" / "y.h5"))]
    report = etl_files(files, "synthetic", study="aapl")
    assert report["staged"] == 2
    assert all("/aapl/" in crate["path"] for crate in report["crates"])
