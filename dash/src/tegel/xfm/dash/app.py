"""Search dashboard for the SSRL XFM data server."""

from __future__ import annotations

import argparse
import base64
import io
import json
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import unquote, urlparse

import httpx
import numpy
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from PIL import Image

from tegel.core.client.data import DataClient, DataClientError
from tegel.core.contracts.resources import SearchHit, StructureFamily

HOST = "127.0.0.1"
PORT = 8001
SERVER = "http://127.0.0.1:8000"
ISSUER = "http://127.0.0.1:8080/realms/tegel"
CLIENT_ID = "tegel"
READER = "reader"
T = TypeVar("T")

FIELDS = (
    ("sample_code", "Sample code"),
    ("campaign", "Campaign"),
    ("cache", "Cache"),
    ("kind", "Kind"),
    ("beam_energy_kev", "Beam energy (keV)"),
)
COLUMNS = (
    "measurement_id",
    "sample_code",
    "campaign",
    "kind",
    "beam_energy_kev",
    "acquired",
    "technique",
    "designated",
)


def create_app(client: DataClient) -> FastAPI:
    """Serve the search page and proxy read calls to the data server."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.client.close()

    app = FastAPI(title="XFM measurements", lifespan=lifespan)
    app.state.client = client

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return _page()

    @app.get("/api/search")
    def search(request: Request) -> dict[str, Any]:
        filters = _filters(request.query_params)
        result = _call(lambda: request.app.state.client.search(filters))
        return {"count": result.count, "hits": [_hit(hit) for hit in result.hits]}

    @app.get("/api/resources/{resource_id}")
    def resource(resource_id: str, request: Request) -> dict[str, Any]:
        client = request.app.state.client
        found = _call(lambda: client.get(resource_id))
        descriptor = _call(lambda: client.open_data(resource_id))
        return {
            "id": found.id,
            "uri": found.uri,
            "name": found.name,
            "identifier": found.identifier,
            "date_created": found.date_created,
            "metadata": found.metadata,
            "data": descriptor.model_dump(mode="json"),
        }

    @app.get("/api/resources/{resource_id}/image")
    def image(resource_id: str, request: Request, channel: int = 0) -> Response:
        client = request.app.state.client
        descriptor = _call(lambda: client.open_data(resource_id))
        shape = list(descriptor.shape or [])
        if descriptor.family is not StructureFamily.array or len(shape) < 2:
            raise HTTPException(status_code=404, detail="This measurement has no image")
        slice_text, image_shape = _image_slice(shape, channel)
        params = {"format": "application/octet-stream"}
        if slice_text:
            params["slice"] = slice_text
        response = _get(client, f"/api/v1/array/full/{resource_id}", params)
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=_http_detail(response))
        array = _array_from_bytes(response.content, descriptor.data_type or "float64", image_shape)
        return Response(content=_png(array), media_type="image/png")

    return app


class RefreshingClient(DataClient):
    """Data client that replaces an expired Keycloak reader token."""

    def __init__(
        self,
        base_url: str,
        *,
        token_path: Path,
        credentials_path: Path,
        issuer: str,
        client: httpx.Client | None = None,
    ) -> None:
        token = token_path.read_text(encoding="utf-8").strip()
        super().__init__(base_url, token=token, client=client)
        self._token_path = token_path
        self._credentials = _credentials(credentials_path)
        self._token_url = issuer.rstrip("/") + "/protocol/openid-connect/token"
        if _expired(token):
            self.refresh()

    def refresh(self) -> None:
        """Ask Keycloak for a new reader token and store it beside the credentials."""
        response = self._client.post(
            self._token_url,
            data={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "client_secret": self._credentials["client_secret"],
                "username": READER,
                "password": self._credentials["user_password"],
            },
        )
        if response.status_code >= 400:
            raise DataClientError("Token refresh failed", status_code=response.status_code)
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise DataClientError("Token refresh returned no access_token")
        self._token = token
        self._token_path.write_text(token + "\n", encoding="utf-8")
        self._token_path.chmod(0o600)

    def _bearer(self) -> str | None:
        if _expired(self._token):
            self.refresh()
        return self._token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return super()._request(method, path, params=params)
        except DataClientError as exc:
            if exc.status_code != 401:
                raise
            self.refresh()
            return super()._request(method, path, params=params)


def main() -> None:
    """Listen locally and read the data server bearer token from a file."""
    args = _arguments()
    token_path = Path(args.token_file)
    if not token_path.is_file():
        raise SystemExit(f"token file not found: {token_path}")
    credentials_path = token_path.parent / "credentials"
    if credentials_path.is_file():
        client: DataClient = RefreshingClient(
            args.server,
            token_path=token_path,
            credentials_path=credentials_path,
            issuer=args.issuer,
        )
    else:
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise SystemExit(f"token file is empty: {token_path}")
        client = DataClient(args.server, token=token)
    uvicorn.run(create_app(client), host=args.host, port=args.port)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tegel-xfm-dash")
    parser.add_argument("--server", default=SERVER)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--issuer", default=ISSUER)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    return parser.parse_args()


def _credentials(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key and value:
            found[key] = value
    missing = [name for name in ("client_secret", "user_password") if not found.get(name)]
    if missing:
        names = ", ".join(missing)
        raise SystemExit(f"{path} is missing {names}")
    return found


def _expired(token: str | None) -> bool:
    if not token:
        return True
    exp = _expiry(token)
    if exp is None:
        return True
    return exp < time.time() + 30


def _expiry(token: str) -> int | None:
    try:
        part = token.split(".")[1]
    except IndexError:
        return None
    part += "=" * (-len(part) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(part))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    return exp if isinstance(exp, int) else None


def _get(client: DataClient, path: str, params: dict[str, str]) -> httpx.Response:
    response = _once(client, path, params)
    if response.status_code == 401 and isinstance(client, RefreshingClient):
        client.refresh()
        response = _once(client, path, params)
    return response


def _once(client: DataClient, path: str, params: dict[str, str]) -> httpx.Response:
    return client._client.request(
        "GET",
        f"{client.base_url}{path}",
        params=params,
        headers=client._headers(),
    )


def _filters(params: Mapping[str, str]) -> dict[str, str]:
    return {key: value.strip() for key, value in params.items() if value.strip()}


def _hit(hit: SearchHit) -> dict[str, Any]:
    return {
        "id": _resource_id(hit.uri),
        "uri": hit.uri,
        "name": hit.name,
        "identifier": hit.identifier,
        "date_created": hit.date_created,
        "properties": hit.properties,
    }


def _image_slice(shape: list[int], channel: int) -> tuple[str | None, tuple[int, ...]]:
    """Pick a 2D plane. The last axis is the channel; axes in between are 0."""
    if len(shape) == 2:
        return None, (shape[0], shape[1])
    index = min(max(channel, 0), shape[-1] - 1)
    parts = [":", ":"]
    parts.extend("0" for _ in shape[2:-1])
    parts.append(str(index))
    return ",".join(parts), (shape[0], shape[1])


def _array_from_bytes(payload: bytes, data_type: str, shape: tuple[int, ...]) -> numpy.ndarray:
    dtype = numpy.dtype(data_type)
    count = 1
    for item in shape:
        count *= item
    if len(payload) != count * dtype.itemsize:
        raise HTTPException(status_code=502, detail="Array response does not match its shape")
    return numpy.frombuffer(payload, dtype=dtype).reshape(shape)


def _png(array: numpy.ndarray) -> bytes:
    finite = array[numpy.isfinite(array)]
    if finite.size == 0:
        scaled = numpy.zeros(array.shape, dtype=numpy.uint8)
    else:
        low, high = numpy.percentile(finite, [1, 99])
        if high <= low:
            scaled = numpy.zeros(array.shape, dtype=numpy.uint8)
        else:
            filled = numpy.nan_to_num(array, nan=float(low))
            unit = numpy.clip((filled - low) / (high - low), 0, 1)
            scaled = (unit * 255).astype(numpy.uint8)
    image = Image.fromarray(scaled, mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _http_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except httpx.DecodingError:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"]
    return f"HTTP {response.status_code}"


def _resource_id(uri: str) -> str:
    name = unquote(urlparse(uri).path.rstrip("/").rsplit("/", 1)[-1])
    if not name or "/" in name or "\\" in name:
        raise DataClientError(f"search hit has no resource id: {uri}")
    return name


def _call(func: Callable[[], T]) -> T:
    try:
        return func()
    except DataClientError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=exc.message) from exc


def _page() -> str:
    labels = "".join(
        f'<label>{label}<input name="{name}" autocomplete="off"></label>' for name, label in FIELDS
    )
    heads = "".join(f"<th>{name}</th>" for name in COLUMNS)
    fields = ",".join(f'"{name}"' for name, _ in FIELDS)
    columns = ",".join(f'"{name}"' for name in COLUMNS)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>XFM measurements</title>
<style>
body {{ font: 15px/1.4 system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
form {{ display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: end; }}
label {{ display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.85rem; }}
input, button {{ font: inherit; padding: 0.3rem 0.45rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1.5rem; }}
th, td {{ text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #ddd; }}
tr.result {{ cursor: pointer; }}
tr.result:hover, tr.selected {{ background: #f3f3f3; }}
#error {{ color: #8a1f1f; min-height: 1.4em; }}
#detail {{ margin-top: 1.5rem; }}
#preview {{ max-width: 100%; background: #111; }}
#channel {{ width: 5rem; }}
pre {{ background: #f6f6f6; padding: 0.75rem; overflow: auto; }}
</style>
</head>
<body>
<h1>XFM measurements</h1>
<p>
Leave the fields blank to list the whole catalog.
A selected row shows its image, metadata, and array descriptor.
</p>
<form id="search">{labels}<button type="submit">Search</button></form>
<p id="error"></p>
<table>
<thead><tr>{heads}</tr></thead>
<tbody id="hits"></tbody>
</table>
<section id="detail" hidden>
<h2 id="detail-title"></h2>
<label id="channel-label" hidden>Channel
<input id="channel" type="number" min="0" value="0">
</label>
<img id="preview" alt="" hidden>
<p id="descriptor"></p>
<pre id="metadata"></pre>
</section>
<script>
const fields = [{fields}];
const columns = [{columns}];
const form = document.querySelector("#search");
const error = document.querySelector("#error");
const hits = document.querySelector("#hits");
const detail = document.querySelector("#detail");

form.addEventListener("submit", (event) => {{
  event.preventDefault();
  runSearch();
}});

async function runSearch() {{
  const params = new URLSearchParams();
  for (const field of fields) {{
    const value = form.elements[field].value.trim();
    if (value) params.set(field, value);
  }}
  const response = await fetch("/api/search?" + params);
  const body = await response.json();
  if (!response.ok) {{
    error.textContent = body.detail || "Search failed";
    hits.replaceChildren();
    detail.hidden = true;
    return;
  }}
  error.textContent = body.count + " measurement(s)";
  hits.replaceChildren();
  for (const hit of body.hits) {{
    const row = document.createElement("tr");
    row.className = "result";
    for (const column of columns) {{
      const cell = document.createElement("td");
      const value = hit.properties[column];
      cell.textContent = value == null ? "" : String(value);
      row.append(cell);
    }}
    row.addEventListener("click", () => openHit(hit, row));
    hits.append(row);
  }}
}}

runSearch();

async function openHit(hit, row) {{
  for (const item of hits.querySelectorAll("tr")) item.classList.remove("selected");
  row.classList.add("selected");
  const response = await fetch("/api/resources/" + encodeURIComponent(hit.id));
  const body = await response.json();
  if (!response.ok) {{
    error.textContent = body.detail || "Could not open the measurement";
    detail.hidden = true;
    return;
  }}
  error.textContent = "";
  detail.hidden = false;
  document.querySelector("#detail-title").textContent = body.name || body.id;
  const data = body.data;
  const shape = data.shape || [];
  document.querySelector("#descriptor").textContent =
    data.family + " " + (data.data_type || "") + " " + shape.join(" × ");
  document.querySelector("#metadata").textContent =
    JSON.stringify(body.metadata, null, 2);
  const preview = document.querySelector("#preview");
  const channelLabel = document.querySelector("#channel-label");
  const channel = document.querySelector("#channel");
  if (data.family === "array" && shape.length >= 2) {{
    const last = shape[shape.length - 1];
    channel.max = String(Math.max(last - 1, 0));
    channelLabel.hidden = shape.length < 3;
    const show = () => {{
      const index = channel.value || "0";
      preview.hidden = false;
      preview.alt = body.name || body.id;
      preview.src = "/api/resources/" + encodeURIComponent(hit.id)
        + "/image?channel=" + encodeURIComponent(index);
    }};
    channel.onchange = show;
    show();
  }} else {{
    preview.hidden = true;
    preview.removeAttribute("src");
    channelLabel.hidden = true;
  }}
}}
</script>
</body>
</html>
"""
