"""Scan crates: layout, OME-Zarr validity, aims-leaf validity, ids, CLI."""

import json
import uuid
from pathlib import Path

import numpy as np
import pytest
import zarr

from xfm_etl import CRATE_PROFILE, typeid, write_scan
from xfm_etl.cli import main
from xfm_etl.readers.synthetic import scan

SHEET = {
    "genus": "Arabidopsis",
    "species": "thaliana",
    "collection_date_time": "2024-04-30T09:00:00",
    "tissue": "leaf",
    "growth_facility": "growth_chamber",
    "growth_medium": "soil",
    "developmental_stage": "rosette",
}


def _decode(text: str) -> uuid.UUID:
    number = 0
    for char in text.rpartition("_")[2]:
        number = (number << 5) | typeid.ALPHABET.index(char)
    return uuid.UUID(int=number)


def test_crate_layout_and_typeid(tmp_path: Path) -> None:
    crate = write_scan(scan(), tmp_path)
    assert crate.parent == tmp_path and crate.name.startswith("xfm_")
    assert _decode(crate.name).version == 7
    assert {p.name for p in crate.iterdir()} == {
        "ro-crate-metadata.json", "aims-leaf.json", "scan.ome.zarr"}
    metadata = json.loads((crate / "ro-crate-metadata.json").read_text())
    graph = {entity["@id"]: entity for entity in metadata["@graph"]}
    assert {"@id": CRATE_PROFILE} in graph["ro-crate-metadata.json"]["conformsTo"]
    assert graph["./"]["identifier"] == crate.name
    assert not list(tmp_path.glob(".*partial"))
    with pytest.raises(FileExistsError):
        write_scan(scan(), tmp_path, scan_id=crate.name)


def test_ids_are_time_ordered() -> None:
    first = typeid.encode("xfm", typeid.uuid7(1_000))
    second = typeid.encode("xfm", typeid.uuid7(2_000))
    assert first < second


def test_omezarr_is_valid_ngff_05_and_counts_are_kept(tmp_path: Path) -> None:
    raw = scan(shape=(80, 100), channels=256)
    crate = write_scan(raw, tmp_path)
    group = zarr.open_group(crate / "scan.ome.zarr", mode="r")
    image = pytest.importorskip("ome_zarr_models.v05.image").Image.from_zarr(group)
    multiscale = image.attributes.ome.multiscales[0]
    assert [axis.name for axis in multiscale.axes] == ["c", "y", "x"]
    assert group["0"].shape == (256, 80, 100)
    assert np.array_equal(group["0"][:], np.moveaxis(raw.mca, -1, 0))
    assert int(group["1"][:].sum()) == int(raw.mca.sum())
    assert np.allclose(group["quicklook/total_counts"][:], raw.mca.sum(axis=2))
    assert np.allclose(group["scalers/I0"][:], raw.scalers["I0"])
    scales = [d.coordinateTransformations[0].scale for d in multiscale.datasets]
    assert scales[0] == [1.0, 2.0, 2.0] and scales[1] == [1.0, 4.0, 4.0]
    assert group.attrs["xfm"]["mca"]["gain_ev"] == 10.0


def _validate(record: dict) -> None:
    models = pytest.importorskip("aims_leaf_schema.pydantic")
    models.Dataset.model_validate(record)


def test_aims_leaf_record_validates_except_the_zarr_format_gap(tmp_path: Path) -> None:
    crate = write_scan(scan(), tmp_path)
    record = json.loads((crate / "aims-leaf.json").read_text())
    image = record["data_files"][0]
    assert image["file_format"] == "zarr"
    with pytest.raises(Exception, match="file_format"):
        _validate(record)
    image["file_format"] = "hdf5"  # stand-in until aims-leaf adds zarr
    _validate(record)
    run = record["experiment_runs"][0]
    assert run["technique"] == "xray_microprobe"
    assert run["energy"] == {"numeric_value": 10.0, "unit": "keV"}
    assert record["samples"][0]["sample_code"] == "AAPL-P1-leaf1"
    assert record["studies"][0]["title"] == "aapl"


def test_sample_sheet_gives_a_valid_plant_sample(tmp_path: Path) -> None:
    raw = scan()
    raw.meta.sample = SHEET
    record = json.loads((write_scan(raw, tmp_path) / "aims-leaf.json").read_text())
    assert "samples" not in record
    assert record["plantsamples"][0]["genus"] == "Arabidopsis"
    record["data_files"][0]["file_format"] = "hdf5"
    _validate(record)


def test_cli_converts_with_a_reader(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sheet = tmp_path / "samples.yaml"
    sheet.write_text("AAPL-P1-leaf1:\n  genus: Arabidopsis\n", encoding="utf-8")
    main(["convert", "--reader", "synthetic", "a", "b", "--out", str(tmp_path / "crates"),
          "--study", "radish", "--sample-sheet", str(sheet)])
    crates = sorted((tmp_path / "crates").iterdir())
    assert len(crates) == 2
    record = json.loads((crates[0] / "aims-leaf.json").read_text())
    assert record["studies"][0]["title"] == "radish"
    assert record["plantsamples"][0]["genus"] == "Arabidopsis"
    main(["readers"])
    assert "synthetic" in capsys.readouterr().out
