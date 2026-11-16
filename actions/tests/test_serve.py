"""The processed plugin: claiming, records, parts, and the served read API."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from tegel.core.contracts.resources import Resource, StructureFamily
from tegel.core.plugins import Registry
from tegel.dataserver.app import create_app
from tegel.dataserver.config import ServerConfig
from tegel.dataserver.testing import OIDCIssuer, assert_incarnation
from tegel.xfm.actions.serve import _output_directory, stage
from tegel.xfm.dataserver import ProcessedReader
from tegel.xfm.dataserver.typeid import derive, is_typeid

PACKAGE = Path(__file__).resolve().parents[2] / "dataserver"
FIXTURES = PACKAGE / "tests" / "fixtures"
FIXTURE = FIXTURES / "processed-leaf.json"
PROFILE = PACKAGE / "schema" / "profile.yaml"
SCHEMA_ROOT = PACKAGE.parent / "schemas"
ORIGIN = "http://testserver"
AAPL = derive("aapl-P1-leaf1-10serp")
CLR = np.arange(4 * 5 * 3, dtype="float64").reshape(4, 5, 3)


@pytest.fixture(autouse=True)
def _spec_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEGEL_PROFILE", str(PROFILE))
    monkeypatch.setenv("TEGEL_SCHEMA_ROOT", str(SCHEMA_ROOT))


def write_crate(
    root: Path, measurement_id: str = "aapl-P1-leaf1-10serp", name: str | None = None
) -> Path:
    """A ProcessedCache crate with a chunked clr array, a mask, a table and a record."""
    import zarr

    crate = root / "ProcessedCache" / measurement_id
    crate.mkdir(parents=True)
    store = crate / "masked-clr.zarr"
    group = zarr.open_group(store, mode="w")
    clr = group.create_array("clr", shape=CLR.shape, dtype="float64", chunks=(2, 2, 1))
    clr[:] = CLR
    mask = group.create_array("mask", shape=(4, 5), dtype="bool", chunks=(4, 5))
    mask[:] = True
    (crate / "elements.csv").write_text("channel,element\n0,Fe\n1,Zn\n2,Mn\n", encoding="utf-8")
    (crate / "lambda-record.yaml").write_text("beam_energy_keV: 10\n", encoding="utf-8")
    metadata = crate / "ro-crate-metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "@graph": [
                    {
                        "@id": "./",
                        **({"name": name} if name else {}),
                        "sample_code": "AAPL-P1-leaf1",
                        "technique": "xrf",
                        "dateCreated": "2024-05-01T00:00:00Z",
                    },
                    {"@id": "masked-clr.zarr/", "isDesignated": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    return metadata


def test_dot_output_is_allowed_only_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _output_directory(Path(".")) == Path(".")
    (tmp_path / "note").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="only when the current directory is empty"):
        _output_directory(Path("."))


def test_processed_cache_crate_becomes_a_record(tmp_path: Path) -> None:
    metadata = write_crate(tmp_path)
    raw = tmp_path / "DataCache" / "leaf" / "ro-crate-metadata.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(metadata.read_text(encoding="utf-8"), encoding="utf-8")
    plugin = Registry().extractor_for(str(metadata))
    assert plugin.validate(str(raw)) is False
    package = plugin.process(str(metadata), {})
    assert package.resource_type == "xfm.processed"
    assert package.payload_uri.endswith("ro-crate-metadata.json")
    record = package.record
    assert record["measurement_id"] == "aapl-P1-leaf1-10serp"
    assert record["id"] == AAPL
    assert is_typeid(record["id"])
    assert record["sample_code"] == "AAPL-P1-leaf1"
    assert record["campaign"] == "aapl"
    assert record["kind"] == "mca_fit"
    assert record["beam_energy_kev"] == "10"
    assert record["acquired"] == "2024-05-01"
    assert record["designated"] == "masked-clr.zarr"


def test_typeid_matches_the_spec_vector() -> None:
    import uuid

    from tegel.xfm.dataserver.typeid import decode, encode

    known = uuid.UUID("0188bac7-4afa-78aa-bc3b-bd1eef28d881")
    assert encode("prefix", known) == "prefix_01h2xcejqtf2nbrexx3vqjhp41"
    assert decode("prefix_01h2xcejqtf2nbrexx3vqjhp41") == ("prefix", known)
    assert derive("aapl-P1-leaf1-10serp") == AAPL
    assert AAPL.startswith("xfm_") and len(AAPL) == 30
    assert not is_typeid("xfm_8zzzzzzzzzzzzzzzzzzzzzzzzz")
    assert not is_typeid("other_01h2xcejqtf2nbrexx3vqjhp41")


def test_typeid_folder_is_the_id_and_the_crate_name_is_kept(tmp_path: Path) -> None:
    folder = derive("some-new-scan")
    metadata = write_crate(tmp_path, folder, name="radish-leaf3-roi")
    record = Registry().extractor_for(str(metadata)).process(str(metadata), {}).record
    assert record["id"] == folder
    assert record["measurement_id"] == "radish-leaf3-roi"
    assert record["campaign"] == "aapl"
    no_name = write_crate(tmp_path / "b", folder)
    record = Registry().extractor_for(str(no_name)).process(str(no_name), {}).record
    assert record["measurement_id"] == folder


def test_entry_point_claims_only_processed_records(tmp_path: Path) -> None:
    registry = Registry()
    plugin = registry.extractor_for(str(FIXTURE))
    assert plugin.plugin_id == "xfm-processed-v3"
    assert registry.resource_types()["xfm.processed"].metadata_schema.endswith("/processed")
    other = tmp_path / "note.json"
    other.write_text(json.dumps({"cache": "DataCache", "key": "x"}), encoding="utf-8")
    assert plugin.validate(str(other)) is False
    package = plugin.process(str(FIXTURE), {})
    assert set(package.record) <= {
        "id", "measurement_id", "sample_code", "campaign", "cache", "kind",
        "beam_energy_kev", "acquired", "technique", "crate_path", "designated",
    }


def test_reader_lists_arrays_and_side_files(tmp_path: Path) -> None:
    metadata = write_crate(tmp_path)
    package = Registry().extractor_for(str(metadata)).process(str(metadata), {})
    resource = Resource(
        id="x", uri="urn:x", type="xfm.processed", type_version="3",
        metadata=package.record, payload_uri=package.payload_uri,
    )
    descriptor = ProcessedReader().describe(resource)
    parts = {part.name: part for part in descriptor.parts}
    assert set(parts) == {
        "masked-clr.zarr-clr", "masked-clr.zarr-mask", "elements.csv", "lambda-record.yaml",
    }
    assert parts["masked-clr.zarr-clr"].designated is True
    assert parts["masked-clr.zarr-clr"].shape == [4, 5, 3]
    assert parts["elements.csv"].family is StructureFamily.table
    assert descriptor.payload is not None


def test_reader_on_legacy_record_fixture() -> None:
    package = Registry().extractor_for(str(FIXTURE)).process(str(FIXTURE), {})
    resource = Resource(
        id="x", uri="urn:x", type="xfm.processed", type_version="3",
        metadata=package.record, payload_uri=package.payload_uri,
    )
    descriptor = ProcessedReader().describe(resource)
    assert [part.name for part in descriptor.parts] == ["masked-clr.zarr-clr"]
    assert descriptor.parts[0].shape == [374, 573, 7]


@dataclass
class Served:
    http: httpx.Client
    token: str
    records: Path

    def get(self, path: str, **params: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"}
        return self.http.get(path, headers=headers, params=params)


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    tmp = tmp_path_factory.mktemp("xfm")
    records = tmp / "records"
    write_crate(records)
    write_crate(records, "radish-leaf2-sulfur-edge")
    (records / "ProcessedCache" / "README.txt").write_text("not a record\n", encoding="utf-8")
    output = tmp / "out"
    output.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TEGEL_PROFILE", str(PROFILE))
        patch.setenv("TEGEL_SCHEMA_ROOT", str(SCHEMA_ROOT))
        assert stage(records, output, [records]) == 2
    with OIDCIssuer() as issued:
        app = create_app(
            ServerConfig(
                public_origin=ORIGIN,
                catalog_uri=f"sqlite:///{output / 'catalog.db'}",
                profile_path=PROFILE,
                schema_root=SCHEMA_ROOT,
                issuer_url=issued.issuer,
                audience=issued.audience,
                client_id=issued.client_id,
                client_secret=issued.client_secret,
                session_secret="test-session-secret",
                readable_storage=[records],
            )
        )
        with TestClient(app) as http_client:
            yield Served(http_client, issued.token, records)


def test_xfm_incarnation_conforms(served: Served) -> None:
    assert_incarnation(
        PACKAGE,
        base_url=ORIGIN,
        token=served.token,
        http_client=served.http,
        resource_id=AAPL,
        search_field="campaign",
        search_value="aapl",
        positive_fixture=served.records
        / "ProcessedCache" / "aapl-P1-leaf1-10serp" / "ro-crate-metadata.json",
        rejected_input=served.records / "ProcessedCache" / "README.txt",
        schema_root=SCHEMA_ROOT,
    )


def test_search_and_slice_the_designated_array(served: Served) -> None:
    from tegel.core.client.data import DataClient

    with DataClient(ORIGIN, token=served.token, client=served.http) as client:
        assert client.search({"kind": "sulfur_edge"}).count == 1
        payload = client.read_array(AAPL, "masked-clr.zarr-clr", "1:3,2,0")
    got = np.frombuffer(payload.data, dtype="float64")
    assert got.tolist() == CLR[1:3, 2, 0].tolist()
    data = served.get(f"/resources/{AAPL}/data").json()
    clr = next(part for part in data["parts"] if part["designated"])
    assert clr["served_by"] == "tiled"
    assert clr["shape"] == [4, 5, 3]
    url = clr["content_url"].removeprefix(ORIGIN)
    url = url.replace("/api/v1/metadata/", "/api/v1/array/full/")
    url = url.replace("/api/v1/array/block/", "/api/v1/array/full/")
    response = served.http.get(
        url,
        params={"slice": "1:3,2,0"},
        headers={"Authorization": f"Bearer {served.token}", "Accept": "application/json"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == CLR[1:3, 2, 0].tolist()
    table = next(part for part in data["parts"] if part["name"] == "elements.csv")
    assert table["columns"] == ["channel", "element"]
