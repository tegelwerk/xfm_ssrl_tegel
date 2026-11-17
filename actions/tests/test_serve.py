"""Stage ETL-written scan crates, serve them, and read them back through the read API."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from tegel.core.client.data import DataClient
from tegel.dataserver.app import create_app
from tegel.dataserver.config import ServerConfig
from tegel.dataserver.testing import OIDCIssuer, assert_incarnation
from tegel.xfm.actions.serve import _output_directory, stage
from tegel.xfm.dataserver.typeid import decode, derive, encode, is_typeid
from xfm_etl import write_scan
from xfm_etl.readers.synthetic import scan

PACKAGE = Path(__file__).resolve().parents[2] / "dataserver"
PROFILE = PACKAGE / "schema" / "profile.yaml"
SCHEMA_ROOT = PACKAGE.parent / "schemas"
ORIGIN = "http://testserver"
LEAF = "xfm_01m7nc0000aaaaaaaaaaaaaaaa"
RADISH = "xfm_01m7nc0000bbbbbbbbbbbbbbbb"


def test_dot_output_is_allowed_only_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _output_directory(Path(".")) == Path(".")
    (tmp_path / "note").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="only when the current directory is empty"):
        _output_directory(Path("."))


def test_typeid_matches_the_spec_vector() -> None:
    import uuid

    known = uuid.UUID("0188bac7-4afa-78aa-bc3b-bd1eef28d881")
    assert encode("prefix", known) == "prefix_01h2xcejqtf2nbrexx3vqjhp41"
    assert decode("prefix_01h2xcejqtf2nbrexx3vqjhp41") == ("prefix", known)
    assert is_typeid(derive("some-folder"))
    assert not is_typeid("xfm_8zzzzzzzzzzzzzzzzzzzzzzzzz")
    assert is_typeid(LEAF) and is_typeid(RADISH)


@dataclass
class Served:
    http: httpx.Client
    token: str
    records: Path
    cube: np.ndarray
    leaf_crate: Path


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    tmp = tmp_path_factory.mktemp("xfm")
    records = tmp / "records"
    leaf = scan("AAPL_10serp_P1_leaf1", shape=(80, 100), channels=128)
    leaf_crate = write_scan(leaf, records, scan_id=LEAF)
    radish = scan("radish_leaf2", sample_code="RAD-leaf2", shape=(40, 50), channels=128,
                  study="radish", seed=3)
    write_scan(radish, records, scan_id=RADISH)
    (records / "README.txt").write_text("not a record\n", encoding="utf-8")
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
            yield Served(http_client, issued.token, records, np.moveaxis(leaf.mca, -1, 0),
                         leaf_crate)


def test_xfm_incarnation_conforms(served: Served) -> None:
    assert_incarnation(
        PACKAGE,
        base_url=ORIGIN,
        token=served.token,
        http_client=served.http,
        resource_id=LEAF,
        search_field="study",
        search_value="aapl",
        positive_fixture=served.leaf_crate / "ro-crate-metadata.json",
        rejected_input=served.records / "README.txt",
        schema_root=SCHEMA_ROOT,
    )


def test_search_parts_and_slices(served: Served) -> None:
    with DataClient(ORIGIN, token=served.token, client=served.http) as client:
        assert client.search({"study": "radish"}).count == 1
        assert client.search({"beamline": "BL2-3"}).count == 2
        record = client.metadata(LEAF)
        assert record["map_shape"] == "80x100" and record["mca_channels"] == "128"
        parts = {part.name: part for part in client.parts(LEAF)}
        assert set(parts) == {"mca-0", "mca-1", "I0", "total_counts", "aims-leaf"}
        assert all(parts[name].served_by == "tiled" for name in ("mca-0", "mca-1", "I0"))
        assert parts["mca-1"].shape == [128, 40, 50]
        plane = client.read_array(LEAF, "mca-0", "40,10:20,30")
        got = np.frombuffer(plane.data, dtype=plane.data_type)
        assert got.tolist() == served.cube[40, 10:20, 30].tolist()
        total = client.read_array(LEAF, "total_counts", "5,0:4")
        expected = served.cube.sum(axis=0)[5, 0:4]
        assert np.allclose(np.frombuffer(total.data, dtype=total.data_type), expected)
        record_bytes = client.read_bytes(LEAF, "aims-leaf")
        assert b"xray_microprobe" in record_bytes
