"""XFM actions, called directly and through a running action server."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pytest
import zarr
from fastapi.testclient import TestClient

from tegel.actionserver import RunContext
from tegel.actionserver.app import create_app
from tegel.actionserver.auth import ApiKeyAuth
from tegel.actionserver.config import ActionServerConfig
from tegel.xfm.compute.processed import import_processed, summary
from tegel.xfm.compute.roi import K_ALPHA, roi_maps
from xfm_etl.readers.synthetic import GAIN_EV, scan

H = {"Authorization": "Bearer xfm-key"}
CLR = np.arange(4 * 5 * 3, dtype="float64").reshape(4, 5, 3)


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(run_id="t", action="t", principal="t", work_dir=tmp_path,
                      log=logging.getLogger("t"))


def processed_crate(root: Path, measurement_id: str = "aapl-P1-leaf1-10serp",
                    sidecar: str = "beam_energy_keV: 10\n") -> Path:
    """A legacy ProcessedCache crate: clr + mask store, element table, sidecar."""
    crate = root / "ProcessedCache" / measurement_id
    crate.mkdir(parents=True)
    group = zarr.open_group(crate / "masked-clr.zarr", mode="w")
    group.create_array("clr", shape=CLR.shape, dtype="float64", chunks=(2, 2, 1))[:] = CLR
    group.create_array("mask", shape=(4, 5), dtype="bool", chunks=(4, 5))[:] = True
    (crate / "elements.csv").write_text("channel,element\n0,Fe\n1,Zn\n2,Mn\n", "utf-8")
    (crate / "lambda-record.yaml").write_text(sidecar, "utf-8")
    metadata = crate / "ro-crate-metadata.json"
    metadata.write_text(json.dumps({"@graph": [
        {"@id": "./", "sample_code": "AAPL-P1-leaf1", "technique": "xrf",
         "dateCreated": "2024-05-01T00:00:00Z"},
        {"@id": "masked-clr.zarr/", "isDesignated": True},
    ]}), "utf-8")
    return metadata


def test_roi_maps_sum_the_k_alpha_windows(tmp_path: Path) -> None:
    raw = scan(shape=(40, 50), channels=1024)
    cube = np.moveaxis(raw.mca, -1, 0)
    out = roi_maps(_ctx(tmp_path), cube, elements="Fe,Zn", gain_ev=GAIN_EV)
    rois = out["rois"].set_index("element")
    low, high = rois.loc["Fe", ["low_channel", "high_channel"]]
    assert np.array_equal(out["maps"][0], cube[low : high + 1].sum(axis=0))
    assert out["maps"].shape == (2, 40, 50)
    leaf, corner = out["maps"][0][20, 25], out["maps"][0][0, 0]
    assert leaf > 10 * max(corner, 1)


def test_roi_maps_refuse_bad_requests(tmp_path: Path) -> None:
    cube = np.zeros((100, 4, 4))
    with pytest.raises(ValueError, match="no K-alpha energy"):
        roi_maps(_ctx(tmp_path), cube, elements="Xx")
    with pytest.raises(ValueError, match="outside the MCA range"):
        roi_maps(_ctx(tmp_path), cube, elements="Zn", gain_ev=10.0)
    with pytest.raises(ValueError, match="MCA cube"):
        roi_maps(_ctx(tmp_path), np.zeros((4, 4)))
    assert "Fe" in K_ALPHA


def test_summary_reads_aims_leaf_and_older_energy_fields(tmp_path: Path) -> None:
    aims = processed_crate(tmp_path / "a", sidecar=(
        "experiment_runs:\n  - energy: {numeric_value: 12.5, unit: keV}\n"))
    older = processed_crate(tmp_path / "b", sidecar=(
        "experiment_runs:\n  - beam_energy: {has_numeric_value: 9}\n"))
    assert summary(aims)["beam_energy_kev"] == "12.5"
    assert summary(older)["beam_energy_kev"] == "9"
    record = summary(aims)
    assert record["campaign"] == "aapl" and record["designated"] == "masked-clr.zarr"


def test_import_processed_reads_the_crate(tmp_path: Path) -> None:
    out = import_processed(_ctx(tmp_path), processed_crate(tmp_path))
    assert np.array_equal(out["clr"], CLR)
    assert out["mask"].shape == (4, 5)
    assert list(out["elements"]["element"]) == ["Fe", "Zn", "Mn"]
    assert out["record"]["kind"] == "mca_fit"
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "ro-crate-metadata.json").write_text("{}", "utf-8")
    with pytest.raises(ValueError, match="ProcessedCache"):
        import_processed(_ctx(tmp_path), loose / "ro-crate-metadata.json")


@pytest.fixture
def server(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    config = ActionServerConfig(public_origin="http://testserver", results_dir=tmp_path / "srv",
                                readable_storage=[data])
    auth = ApiKeyAuth(keys=[{"key": "xfm-key", "subject": "ana",
                             "scopes": ["actions:read", "actions:run"]}])
    app = create_app(config, auth=auth)
    with TestClient(app) as client:
        client.data = data
        yield client
    app.state.tegel_actions.close()


def _run(client, body) -> dict:
    response = client.post("/runs", json=body, headers=H)
    assert response.status_code == 202, response.text
    run = client.post(f"/runs/{response.json()['id']}/wait", params={"timeout": 120},
                      headers=H).json()
    assert run["state"] == "succeeded", run["error"]
    return run


def _resource(client, run) -> dict:
    resource = run["result"].rsplit("/", 1)[1]
    return client.get(f"/resources/{resource}", headers=H).json(), resource


def test_actions_are_listed(server) -> None:
    ids = {card["id"] for card in server.get("/actions", headers=H).json()}
    assert {"xfm.roi_maps", "xfm.import_processed"} <= ids


def test_roi_maps_through_the_server(server) -> None:
    cube = np.moveaxis(scan(shape=(20, 24), channels=1024).mca, -1, 0)
    np.save(server.data / "mca.npy", cube)
    run = _run(server, {"action": "xfm.roi_maps",
                        "inputs": {"mca": (server.data / "mca.npy").as_uri()},
                        "parameters": {"elements": "K,Ca,Fe", "gain_ev": GAIN_EV}})
    resource, rid = _resource(server, run)
    assert resource["metadata"]["state"] == "succeeded"
    assert {"rel": "used", "target": (server.data / "mca.npy").as_uri()} in resource["relations"]
    parts = {p["name"]: p for p in server.get(f"/resources/{rid}/data", headers=H).json()["parts"]}
    assert parts["maps"]["shape"] == [3, 20, 24]


def test_import_processed_through_the_server(server) -> None:
    metadata = processed_crate(server.data)
    run = _run(server, {"action": "xfm.import_processed",
                        "inputs": {"crate": metadata.as_uri()}})
    resource, rid = _resource(server, run)
    parts = {p["name"]: p for p in server.get(f"/resources/{rid}/data", headers=H).json()["parts"]}
    assert parts["clr"]["shape"] == [4, 5, 3]
    assert {"clr", "mask", "elements", "record"} <= set(parts)
    url = parts["clr"]["content_url"].replace("http://testserver", "")
    got = server.get(url, params={"format": "application/json", "slice": "1:3,2,0"},
                     headers=H).json()
    assert got == CLR[1:3, 2, 0].tolist()
