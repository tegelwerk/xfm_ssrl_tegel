"""The raw-scan plugin is found by entry point and reads the committed ETL fixture."""

import json
from pathlib import Path

import yaml

from tegel.core.contracts.resources import Resource, StructureFamily
from tegel.core.plugins import Registry
from tegel.dataserver.testing import assert_incarnation_layout, assert_incarnation_ships_no_schema
from tegel.xfm.dataserver import FIELDS, RawScanReader
from tegel.xfm.dataserver.typeid import is_typeid

PACKAGE = Path(__file__).resolve().parents[1]
SCHEMA = PACKAGE.parent / "schemas" / "xfm" / "raw.yaml"
CRATE = PACKAGE / "tests" / "fixtures" / "xfm_01m7nb2a3ce5ty1cfq2zwhd9x7"
METADATA = CRATE / "ro-crate-metadata.json"


def test_layout_and_no_schema() -> None:
    assert_incarnation_layout(PACKAGE)
    assert_incarnation_ships_no_schema(PACKAGE)


def test_every_slot_is_mapped_to_aims_leaf_or_declared_a_gap() -> None:
    schema = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    slots = schema["classes"]["XfmRawScan"]["attributes"]
    assert list(slots) == list(FIELDS)
    for name, slot in slots.items():
        notes = slot.get("annotations") or {}
        assert ("aims_leaf" in notes) != ("aims_leaf_gap" in notes), name


def test_claims_scan_crates_only(tmp_path: Path) -> None:
    registry = Registry()
    plugin = registry.extractor_for(str(METADATA))
    assert plugin.plugin_id == "xfm-raw-scan-v1"
    assert plugin.validate(str(CRATE / "aims-leaf.json")) is False
    other = tmp_path / "ro-crate-metadata.json"
    other.write_text(json.dumps({"@graph": [{"@id": "./"}]}), encoding="utf-8")
    (tmp_path / "aims-leaf.json").write_text("{}", encoding="utf-8")
    assert plugin.validate(str(other)) is False


def test_record_is_projected_from_aims_leaf() -> None:
    package = Registry().extractor_for(str(METADATA)).process(str(METADATA), {})
    record = package.record
    assert record["id"] == CRATE.name and is_typeid(record["id"])
    assert record["scan_name"] == "AAPL_10serp_P1_leaf1_fixture"
    assert record["sample_code"] == "AAPL-P1-leaf1"
    assert record["study"] == "aapl"
    assert record["technique"] == "xray_microprobe"
    assert record["facility"] == "SSRL"
    assert record["beamline"] == "BL2-3"
    assert record["energy_kev"] == "10"
    assert record["dwell_time_ms"] == "50"
    assert record["map_shape"] == "12x16"
    assert record["mca_channels"] == "64"
    assert record["step_size_um"] == "2x2"
    assert record["experiment_date"] == "2024-05-01"


def test_parts_are_omezarr_levels_scalers_and_quicklook() -> None:
    package = Registry().extractor_for(str(METADATA)).process(str(METADATA), {})
    resource = Resource(
        id=package.record["id"], uri="urn:x", type="xfm.raw_scan", type_version="1",
        metadata=package.record, payload_uri=package.payload_uri,
    )
    descriptor = RawScanReader().describe(resource)
    parts = {part.name: part for part in descriptor.parts}
    assert set(parts) == {"mca-0", "I0", "total_counts", "aims-leaf"}
    assert parts["mca-0"].shape == [64, 12, 16]
    assert parts["total_counts"].designated is True
    assert parts["aims-leaf"].family is StructureFamily.bytes
    assert descriptor.payload.media_type == "application/ld+json"
