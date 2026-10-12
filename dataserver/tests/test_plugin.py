"""The processed plugin is discovered by entry point and serves one record."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tegel.core.client.data import DataClient
from tegel.core.contracts.resources import StructureFamily
from tegel.core.plugins import Registry, check_package
from tegel.dataserver.app import create_app
from tegel.dataserver.catalog import insert_record
from tegel.dataserver.config import ServerConfig
from tegel.dataserver.testing import OIDCIssuer
from tegel.xfm.dataserver.launch import _output_directory

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "processed-leaf.json"
PROFILE = Path(__file__).resolve().parents[1] / "schema" / "profile.yaml"


def test_dot_output_is_allowed_only_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _output_directory(Path(".")) == Path(".")
    (tmp_path / "note").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="only when the current directory is empty"):
        _output_directory(Path("."))


def test_named_output_directory_is_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    output = _output_directory(Path("run"))
    assert output == Path("run")
    assert output.is_dir()


def test_processed_cache_crate_becomes_a_record(tmp_path: Path) -> None:
    crate = tmp_path / "ProcessedCache" / "aapl-P1-leaf1-10serp"
    crate.mkdir(parents=True)
    metadata = crate / "ro-crate-metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "@graph": [
                    {
                        "@id": "./",
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
    (crate / "lambda-record.yaml").write_text("beam_energy_keV: 10\n", encoding="utf-8")
    raw = tmp_path / "DataCache" / "leaf" / "ro-crate-metadata.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(metadata.read_text(encoding="utf-8"), encoding="utf-8")
    registry = Registry()
    plugin = registry.extractor_for(str(metadata))
    assert plugin.validate(str(raw)) is False
    package = check_package(plugin, plugin.process(str(metadata), {}))
    assert package.metadata["key"] == "ProcessedCache--aapl-P1-leaf1-10serp"
    assert package.metadata["sample_code"] == "AAPL-P1-leaf1"
    assert package.metadata["kind"] == "mca_fit"
    assert package.metadata["beam_energy_kev"] == "10"
    assert package.metadata["acquired"] == "2024-05-01"
    assert package.metadata["payload_uri"].endswith("masked-clr.zarr")


def test_entry_point_claims_only_processed_records(tmp_path: Path) -> None:
    registry = Registry()
    plugin = registry.extractor_for(str(FIXTURE))
    assert plugin.plugin_id == "xfm-processed-v1"
    assert registry.resource_types()["xfm.processed"].metadata_schema.endswith("/processed")
    other = tmp_path / "note.json"
    other.write_text(json.dumps({"cache": "DataCache", "key": "x"}), encoding="utf-8")
    assert plugin.validate(str(other)) is False
    package = check_package(plugin, plugin.process(str(FIXTURE), {}))
    assert package.metadata["sample_code"] == "AAPL-P1-leaf1"
    assert package.metadata["plugin_id"] == "xfm-processed-v1"
    assert package.payload_path.endswith("processed-leaf.json")
    store = package.metadata["payload_uri"]
    assert store.endswith("masked-clr.zarr")
    assert Path(store.removeprefix("file://")).joinpath("clr", "zarr.json").is_file()


def test_server_loads_the_plugin_and_describes_chunks(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.db"
    with OIDCIssuer() as issued:
        app = create_app(
            ServerConfig(
                public_origin="http://testserver",
                catalog_uri=f"sqlite:///{catalog}",
                profile_path=PROFILE,
                issuer_url=issued.issuer,
                audience=issued.audience,
                client_id=issued.client_id,
                client_secret=issued.client_secret,
                session_secret="test-session-secret",
            )
        )
        names = [row.name for row in app.state.tegel_registry.installed()]
        assert "xfm-processed" in names
        plugin = app.state.tegel_registry.extractor_for(str(FIXTURE))
        package = check_package(plugin, plugin.process(str(FIXTURE), {"key": "leaf-1"}))
        insert_record(
            app.state.tegel_catalog,
            key="leaf-1",
            metadata=package.metadata,
            structure_family="array",
        )
        with TestClient(app) as http_client:
            with DataClient("http://testserver", token=issued.token, client=http_client) as client:
                found = client.search({"sample_code": "AAPL-P1-leaf1", "cache": "ProcessedCache"})
                assert found.count == 1
                descriptor = client.open_data("leaf-1")
                assert descriptor.family is StructureFamily.array
                assert descriptor.shape == [374, 573, 7]
                assert descriptor.chunks == [[64, 64, 1]]
                assert descriptor.data_type == "float64"
                content = http_client.get(
                    "/resources/leaf-1/data/content",
                    headers={"Authorization": f"Bearer {issued.token}"},
                )
                assert content.status_code == 404
