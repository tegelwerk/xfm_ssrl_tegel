"""xfm.etl: beamline files in, scan crates written and merged into a live catalog."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from tegel.actionserver import RunContext
from tegel.xfm.actions.etl import etl
from tegel.xfm.actions.serve import stage

DATASERVER = Path(__file__).resolve().parents[2] / "dataserver"


def _ctx(tmp_path: Path) -> RunContext:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return RunContext(run_id="t", action="t", principal="t", work_dir=work,
                      log=logging.getLogger("t"))


def _ids(catalog: Path) -> set[str]:
    with sqlite3.connect(catalog) as connection:
        return {row[0] for row in connection.execute("SELECT key FROM nodes")}


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    records = tmp_path / "records"
    records.mkdir()
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setenv("TEGEL_PROFILE", str(DATASERVER / "schema" / "profile.yaml"))
    monkeypatch.setenv("TEGEL_SCHEMA_ROOT", str(DATASERVER.parent / "schemas"))
    stage(records, output, [records])  # the data server's (empty) catalog
    monkeypatch.setenv("TEGEL_RECORDS", str(records))
    monkeypatch.setenv("TEGEL_CATALOG_URI", f"sqlite:///{output / 'catalog.db'}")
    monkeypatch.delenv("TEGEL_REFRESH_URL", raising=False)
    return tmp_path


def test_card_is_a_catalog_action() -> None:
    card = etl.card
    assert card.id == "xfm.etl"
    assert card.scope == "catalog:write"
    assert "catalog-writer" in card.resources


def test_files_become_crates_in_the_catalog(deployment: Path) -> None:
    beamline = deployment / "beamline"
    beamline.mkdir()
    files = [beamline / "AAPL_10serp_P1_leaf1.h5", beamline / "AAPL_10serp_P1_leaf2.h5"]
    for item in files:
        item.write_bytes(b"")
    out = etl(_ctx(deployment), files, reader="synthetic", study="radish",
              samples={"AAPL-10serp-P1-leaf1": {"genus": "Arabidopsis"}})["ingested"]
    assert out["staged"] == 2 and out["errors"] == [] and out["refreshed"] is False
    assert all("/radish/" in crate["path"] for crate in out["crates"])
    ids = {crate["id"] for crate in out["crates"]}
    assert ids <= _ids(deployment / "out" / "catalog.db")
    assert all(Path(crate["path"]).is_dir() for crate in out["crates"])
    assert (deployment / "records" / "index.csv").is_file()
    again = etl(_ctx(deployment), files[:1], reader="synthetic")["ingested"]
    assert again["staged"] == 1
    assert len(_ids(deployment / "out" / "catalog.db") & (ids | {again["crates"][0]["id"]})) == 3


def test_unknown_reader_fails(deployment: Path) -> None:
    with pytest.raises(ValueError, match="unknown reader"):
        etl(_ctx(deployment), [], reader="nope")
