"""The Tegel converter entry point: reader from XFM_ETL_READER, crates under records."""

from importlib.metadata import entry_points
from pathlib import Path

import pytest

from xfm_etl.converter import XfmConverter


def test_registered_on_tegel_converters() -> None:
    found = {ep.name: ep.value for ep in entry_points(group="tegel.converters")}
    assert found["xfm"] == "xfm_etl.converter:XfmConverter"


def test_refuses_to_load_without_a_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XFM_ETL_READER", raising=False)
    with pytest.raises(ValueError, match="XFM_ETL_READER"):
        XfmConverter()


def test_converts_into_the_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XFM_ETL_READER", "synthetic")
    converter = XfmConverter()
    assert converter.plugin_id == "xfm-ssrl-etl-synthetic"
    assert "*.h5" in converter.patterns
    raw = tmp_path / "raw" / "x.h5"
    raw.parent.mkdir()
    raw.write_bytes(b"x")
    sheet = tmp_path / "samples.yaml"
    sheet.write_text("x:\n  genus: Arabidopsis\n", encoding="utf-8")
    written = converter.convert([raw], tmp_path / "records",
                                {"collection": "aapl", "sample_sheet": str(sheet)})
    (metadata,) = written
    assert metadata.name == "ro-crate-metadata.json"
    assert metadata.parent.parent.parent == tmp_path / "records" / "aapl"
    assert '"genus": "Arabidopsis"' in (metadata.parent / "aims-leaf.json").read_text()
    overridden = converter.convert([raw], tmp_path / "records",
                                   {"collection": "aapl", "study": "radish"})
    assert "/radish/" in str(overridden[0])
