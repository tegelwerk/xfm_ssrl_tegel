"""The plugin is found by entry point and describes the fixture record without the server."""

from pathlib import Path

import pytest

from tegel.core.contracts.resources import Resource
from tegel.core.plugins import Registry
from tegel.dataserver.testing import assert_incarnation_layout, assert_incarnation_ships_no_schema
from tegel.xfm.dataserver import ProcessedReader

PACKAGE = Path(__file__).resolve().parents[1]
FIXTURE = PACKAGE / "tests" / "fixtures" / "processed-leaf.json"


def test_layout_and_no_schema() -> None:
    assert_incarnation_layout(PACKAGE)
    assert_incarnation_ships_no_schema(PACKAGE)


def test_fixture_record_and_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = Registry().extractor_for(str(FIXTURE))
    package = plugin.process(str(FIXTURE), {})
    assert package.record["measurement_id"] == "aapl-P1-leaf1-10serp"
    resource = Resource(
        id="x", uri="urn:x", type="xfm.processed", type_version="2",
        metadata=package.record, payload_uri=package.payload_uri,
    )
    descriptor = ProcessedReader().describe(resource)
    assert [part.name for part in descriptor.parts] == ["masked-clr.zarr-clr"]
    assert descriptor.parts[0].designated is True
    assert descriptor.parts[0].shape == [374, 573, 7]
