"""The dashboard searches the data server and opens one measurement."""

from collections.abc import Callable

import httpx
import numpy
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tegel.core.client.data import DataClient
from tegel.xfm.dash.app import RefreshingClient, create_app

LEAF = {
    "document": "ro-crate",
    "count": 1,
    "hits": [
        {
            "uri": "http://data/resources/leaf-1",
            "name": "aapl-P1-leaf1-10serp",
            "identifier": "aapl-P1-leaf1-10serp",
            "date_created": "2024-05-01",
            "properties": {
                "measurement_id": "aapl-P1-leaf1-10serp",
                "sample_code": "AAPL-P1-leaf1",
                "campaign": "aapl",
                "kind": "mca_fit",
                "beam_energy_kev": "10",
                "designated": "masked-clr.zarr/",
            },
        }
    ],
}


def test_search_returns_hits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.url.params["sample_code"] == "AAPL-P1-leaf1"
        assert "campaign" not in request.url.params
        return httpx.Response(200, json=LEAF)

    with TestClient(_app(handler)) as client:
        response = client.get(
            "/api/search",
            params={"sample_code": "AAPL-P1-leaf1", "campaign": ""},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["hits"][0]["id"] == "leaf-1"
    assert body["hits"][0]["properties"]["sample_code"] == "AAPL-P1-leaf1"


def test_empty_search_returns_the_catalog() -> None:
    second = {
        "uri": "http://data/resources/leaf-2",
        "name": "aapl-P1-leaf2-10serp",
        "identifier": "aapl-P1-leaf2-10serp",
        "date_created": "",
        "properties": {"sample_code": "AAPL-P1-leaf2"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert not request.url.params
        return httpx.Response(
            200,
            json={"document": "ro-crate", "count": 2, "hits": [LEAF["hits"][0], second]},
        )

    with TestClient(_app(handler)) as client:
        response = client.get("/api/search")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [hit["id"] for hit in body["hits"]] == ["leaf-1", "leaf-2"]


def test_page_loads_the_catalog() -> None:
    with TestClient(_app(lambda request: httpx.Response(404))) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "runSearch();" in response.text


def test_detail_returns_metadata_and_descriptor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/resources/leaf-1":
            return httpx.Response(
                200,
                json={
                    "id": "leaf-1",
                    "uri": "http://data/resources/leaf-1",
                    "name": "aapl-P1-leaf1-10serp",
                    "identifier": "aapl-P1-leaf1-10serp",
                    "metadata": {"sample_code": "AAPL-P1-leaf1", "key": "leaf-1"},
                },
            )
        if request.url.path == "/resources/leaf-1/data":
            return httpx.Response(
                200,
                json={
                    "family": "array",
                    "media_type": "application/zarr",
                    "shape": [374, 573, 7],
                    "chunks": [[64, 64, 1]],
                    "data_type": "float64",
                },
            )
        return httpx.Response(404, json={"detail": "missing"})

    with TestClient(_app(handler)) as client:
        response = client.get("/api/resources/leaf-1")

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["sample_code"] == "AAPL-P1-leaf1"
    assert body["data"]["family"] == "array"
    assert body["data"]["shape"] == [374, 573, 7]
    assert body["data"]["data_type"] == "float64"


def test_image_is_a_png_of_the_tiled_slice() -> None:
    plane = numpy.arange(12, dtype="float64").reshape(3, 4)
    plane[0, 0] = numpy.nan

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/resources/leaf-1/data":
            return httpx.Response(
                200,
                json={
                    "family": "array",
                    "media_type": "application/zarr",
                    "shape": [3, 4, 2],
                    "chunks": [[3, 4, 1]],
                    "data_type": "float64",
                },
            )
        if request.url.path == "/api/v1/array/full/leaf-1":
            assert request.url.params["slice"] == ":,:,1"
            assert request.url.params["format"] == "application/octet-stream"
            return httpx.Response(200, content=plane.tobytes())
        return httpx.Response(404, json={"detail": "missing"})

    with TestClient(_app(handler)) as client:
        response = client.get("/api/resources/leaf-1/image", params={"channel": 1})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(b"\x89PNG")


def test_expired_token_is_refreshed(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "fresh-token"})
        if request.headers.get("authorization") != "Bearer fresh-token":
            return httpx.Response(401, json={"detail": "Authentication required"})
        return httpx.Response(200, json=LEAF)

    credentials = tmp_path / "credentials"
    credentials.write_text("client_secret=secret\nuser_password=pw\n", encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("stale\n", encoding="utf-8")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = RefreshingClient(
        "http://data",
        token_path=token,
        credentials_path=credentials,
        issuer="http://keycloak/realms/tegel",
        client=http,
    )
    with TestClient(create_app(client)) as api:
        response = api.get("/api/search")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert token.read_text(encoding="utf-8").strip() == "fresh-token"


def test_unknown_search_field_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["nope"] == "x"
        return httpx.Response(400, json={"detail": "Unknown search field nope"})

    with TestClient(_app(handler)) as client:
        response = client.get("/api/search", params={"nope": "x"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown search field nope"


def _app(handler: Callable[[httpx.Request], httpx.Response]) -> FastAPI:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://data")
    return create_app(DataClient("http://data", token="test-token", client=http))
