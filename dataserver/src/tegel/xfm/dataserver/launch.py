"""Start Keycloak, catalog records with the plugin, and serve them."""

from __future__ import annotations

import argparse
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from tegel.core.plugins import PluginContractError, check_package
from tegel.dataserver.app import create_app
from tegel.dataserver.catalog import insert_record
from tegel.dataserver.config import ServerConfig
from tegel.xfm.dataserver import _array_metadata, _local_path

HOST = "127.0.0.1"
PORT = 8000
KEYCLOAK_PORT = 8080
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.4.0"
CONTAINER = "tegel-keycloak"
REALM = "tegel"
CLIENT_ID = "tegel"
READER = "reader"
BASE = f"http://{HOST}:{KEYCLOAK_PORT}"


def main() -> None:
    """Run the plugin over a directory of records and serve the catalog."""
    args = _arguments()
    records = Path(args.records)
    output = _output_directory(Path(args.output))
    profile = Path("schema/profile.yaml")
    catalog = output / "catalog.db"
    if not records.is_dir():
        raise SystemExit(f"records directory not found: {records}")
    if not profile.is_file():
        raise SystemExit(f"profile not found: {profile}")
    origin = f"http://{HOST}:{PORT}"
    creds = _credentials(output)
    print(f"Keycloak admin: {BASE}/admin  user admin", flush=True)
    print(f"Keycloak admin password: {creds['admin_password']}", flush=True)
    print(f"Reader user {READER} password: {creds['user_password']}", flush=True)
    _start_keycloak(creds["admin_password"])
    issuer = _realm(creds)
    _reset_catalog(catalog)
    app = create_app(
        ServerConfig(
            public_origin=origin,
            catalog_uri=f"sqlite:///{catalog.resolve()}",
            profile_path=profile.resolve(),
            issuer_url=issuer,
            audience=CLIENT_ID,
            client_id=CLIENT_ID,
            client_secret=creds["client_secret"],
            session_secret=secrets.token_hex(32),
            host=HOST,
            port=PORT,
        )
    )
    count = _catalog_records(app, records)
    token = _reader_token(issuer, creds)
    token_path = output / "token"
    token_path.write_text(token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    print(f"Catalogued {count} record(s) from {records}", flush=True)
    print(f"Output directory {output}", flush=True)
    print(f"Listening on {origin}", flush=True)
    print(f"Bearer token written to {token_path}", flush=True)
    print(
        "curl -H "
        f'"Authorization: Bearer $(cat {token_path})" '
        f'"{origin}/search?sample_code=AAPL-P1-leaf1"',
        flush=True,
    )
    uvicorn.run(app, host=HOST, port=PORT)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tegel-xfm")
    parser.add_argument("--records", default="tests/fixtures")
    parser.add_argument("--output", default=".")
    return parser.parse_args()


def _output_directory(path: Path) -> Path:
    """Return the directory that receives the catalog, credentials, and token."""
    if _is_current_directory(path):
        if any(Path.cwd().iterdir()):
            raise SystemExit(
                "The output path . is allowed only when the current directory is empty"
            )
        return Path(".")
    if path.exists() and not path.is_dir():
        raise SystemExit(f"output path is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_current_directory(path: Path) -> bool:
    if path == Path("."):
        return True
    return path.exists() and path.resolve() == Path.cwd().resolve()


def _credentials(output: Path) -> dict[str, str]:
    credentials = output / "credentials"
    if credentials.is_file():
        found: dict[str, str] = {}
        for line in credentials.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key and value:
                found[key] = value
        needed = ("admin_password", "client_secret", "user_password")
        if all(found.get(name) for name in needed):
            return found
    credentials.parent.mkdir(parents=True, exist_ok=True)
    generated = {
        "admin_password": secrets.token_urlsafe(18),
        "client_secret": secrets.token_urlsafe(18),
        "user_password": secrets.token_urlsafe(18),
    }
    credentials.write_text(
        "\n".join(f"{key}={value}" for key, value in generated.items()) + "\n",
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    print(f"Wrote {credentials}", flush=True)
    return generated


def _start_keycloak(admin_password: str) -> None:
    state = _container_running()
    if state is True:
        print(f"Keycloak container {CONTAINER} is already running at {BASE}", flush=True)
        _show_logs(follow=False)
        _wait_http(f"{BASE}/realms/master")
        return
    if state is False:
        print(f"Starting container {CONTAINER}", flush=True)
        _run(["podman", "start", CONTAINER])
        _show_logs(follow=True)
        return
    if _port_open(KEYCLOAK_PORT):
        raise SystemExit(f"port {KEYCLOAK_PORT} is already in use")
    print(f"podman run {KEYCLOAK_IMAGE} start-dev as {CONTAINER} on {BASE}", flush=True)
    _run(
        [
            "podman",
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-p",
            f"{HOST}:{KEYCLOAK_PORT}:8080",
            "-e",
            "KC_BOOTSTRAP_ADMIN_USERNAME=admin",
            "-e",
            f"KC_BOOTSTRAP_ADMIN_PASSWORD={admin_password}",
            "-e",
            "KC_HTTP_ENABLED=true",
            "-e",
            "KC_HOSTNAME_STRICT=false",
            "-e",
            f"KC_HOSTNAME=http://{HOST}:{KEYCLOAK_PORT}",
            KEYCLOAK_IMAGE,
            "start-dev",
        ]
    )
    _show_logs(follow=True)


def _realm(creds: dict[str, str]) -> str:
    print(f"Configuring Keycloak realm {REALM}", flush=True)
    admin = _admin_token(creds["admin_password"])
    headers = {"Authorization": f"Bearer {admin}"}
    with httpx.Client(base_url=BASE, timeout=30.0, headers=headers) as client:
        existing = client.get(f"/admin/realms/{REALM}")
        if existing.status_code == 404:
            created = client.post(
                "/admin/realms",
                json={"realm": REALM, "enabled": True, "sslRequired": "none"},
            )
            if created.status_code >= 400:
                detail = f"{created.status_code} {created.text}"
                raise SystemExit(f"Keycloak realm create failed: {detail}")
        elif existing.status_code >= 400:
            raise SystemExit(f"Keycloak realm check failed: {existing.status_code}")
        _ensure_client(client, creds["client_secret"])
        _ensure_reader(client, creds["user_password"])
    discovery = httpx.get(f"{BASE}/realms/{REALM}/.well-known/openid-configuration", timeout=30.0)
    discovery.raise_for_status()
    issuer = discovery.json().get("issuer")
    if not isinstance(issuer, str) or not issuer:
        raise SystemExit("Keycloak discovery document has no issuer")
    return issuer


def _ensure_client(client: httpx.Client, client_secret: str) -> None:
    found = client.get(f"/admin/realms/{REALM}/clients", params={"clientId": CLIENT_ID})
    found.raise_for_status()
    clients = found.json()
    if clients:
        return
    made = client.post(
        f"/admin/realms/{REALM}/clients",
        json={
            "clientId": CLIENT_ID,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "secret": client_secret,
            "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": True,
            "redirectUris": [f"http://{HOST}:{PORT}/*"],
            "webOrigins": [f"http://{HOST}:{PORT}"],
        },
    )
    if made.status_code >= 400:
        raise SystemExit(f"Keycloak client create failed: {made.status_code} {made.text}")
    found = client.get(f"/admin/realms/{REALM}/clients", params={"clientId": CLIENT_ID})
    found.raise_for_status()
    clients = found.json()
    if not clients:
        raise SystemExit("Keycloak did not return the new client")
    mapped = client.post(
        f"/admin/realms/{REALM}/clients/{clients[0]['id']}/protocol-mappers/models",
        json={
            "name": "audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.client.audience": CLIENT_ID,
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        },
    )
    if mapped.status_code >= 400:
        raise SystemExit(f"Keycloak audience mapper failed: {mapped.status_code} {mapped.text}")


def _ensure_reader(client: httpx.Client, user_password: str) -> None:
    listed = client.get(
        f"/admin/realms/{REALM}/users",
        params={"username": READER, "exact": "true"},
    )
    listed.raise_for_status()
    users = listed.json()
    profile = {
        "username": READER,
        "enabled": True,
        "emailVerified": True,
        "email": "reader@localhost",
        "firstName": "Reader",
        "lastName": "Local",
        "requiredActions": [],
    }
    if users:
        user_id = str(users[0]["id"])
        updated = client.put(f"/admin/realms/{REALM}/users/{user_id}", json=profile)
    else:
        updated = client.post(f"/admin/realms/{REALM}/users", json=profile)
        listed = client.get(
            f"/admin/realms/{REALM}/users",
            params={"username": READER, "exact": "true"},
        )
        listed.raise_for_status()
        users = listed.json()
        user_id = str(users[0]["id"]) if users else ""
    if updated.status_code >= 400 or not user_id:
        raise SystemExit(f"Keycloak user setup failed: {updated.status_code} {updated.text}")
    password = client.put(
        f"/admin/realms/{REALM}/users/{user_id}/reset-password",
        json={"type": "password", "value": user_password, "temporary": False},
    )
    if password.status_code >= 400:
        raise SystemExit(f"Keycloak password set failed: {password.status_code} {password.text}")
    cleared = client.put(f"/admin/realms/{REALM}/users/{user_id}", json=profile)
    if cleared.status_code >= 400:
        raise SystemExit(f"Keycloak user setup failed: {cleared.status_code} {cleared.text}")


def _catalog_records(app: Any, records: Path) -> int:
    registry = app.state.tegel_registry
    count = 0
    print(f"Reading {records}", flush=True)
    for path in sorted(records.rglob("*.json")):
        if not path.is_file():
            continue
        try:
            plugin = registry.extractor_for(str(path))
        except PluginContractError:
            print(f"skipped {path}", flush=True)
            continue
        package = check_package(plugin, plugin.process(str(path), {}))
        key = str(package.metadata.get("key", ""))
        if not key or "/" in key or "\\" in key:
            raise SystemExit(f"{path} has no single-segment catalog key")
        insert_record(
            app.state.tegel_catalog,
            key=key,
            metadata=package.metadata,
            structure_family=_family(package.metadata),
        )
        count += 1
        print(f"catalogued {key}", flush=True)
    if count == 0:
        raise SystemExit(f"no processed record in {records}")
    return count


def _family(metadata: dict[str, Any]) -> str:
    uri = metadata.get("payload_uri")
    if isinstance(uri, str) and _array_metadata(_local_path(uri)) is not None:
        return "array"
    return "bytes"


def _reader_token(issuer: str, creds: dict[str, str]) -> str:
    response = httpx.post(
        issuer.rstrip("/") + "/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": creds["client_secret"],
            "username": READER,
            "password": creds["user_password"],
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise SystemExit(f"Keycloak token request failed: {response.status_code} {response.text}")
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise SystemExit("Keycloak token response has no access_token")
    return token


def _admin_token(password: str) -> str:
    print(f"Requesting a Keycloak admin token from {BASE}", flush=True)
    deadline = time.time() + 180
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.post(
                f"{BASE}/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": "admin",
                    "password": password,
                },
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            last = str(exc)
            print(f"Keycloak admin login not ready: {last}", flush=True)
            time.sleep(1)
            continue
        if response.status_code < 400:
            token = response.json().get("access_token")
            if isinstance(token, str) and token:
                return token
        if response.status_code in {400, 401}:
            raise SystemExit(
                "Keycloak rejected the admin password in this output directory. "
                f"Remove the running container with: podman rm -f {CONTAINER}"
            )
        last = f"{response.status_code} {response.text}"
        print(f"Keycloak admin login not ready: {last}", flush=True)
        time.sleep(1)
    raise SystemExit(f"Keycloak admin login failed: {last}")


def _wait_http(url: str) -> None:
    deadline = time.time() + 180
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
        except httpx.HTTPError as exc:
            last = str(exc)
            time.sleep(1)
            continue
        if response.status_code < 500:
            return
        last = str(response.status_code)
        time.sleep(1)
    logs = subprocess.run(
        ["podman", "logs", "--tail", "40", CONTAINER],
        check=False,
        capture_output=True,
        text=True,
    )
    raise SystemExit(f"Keycloak did not become ready ({last})\n{logs.stdout}\n{logs.stderr}")


def _container_running() -> bool | None:
    result = subprocess.run(
        ["podman", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() == "true"


def _port_open(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((HOST, port)) == 0


def _show_logs(*, follow: bool) -> None:
    if not follow:
        subprocess.run(["podman", "logs", "--tail", "20", CONTAINER], check=False)
        return
    print(f"Waiting for {BASE}/realms/master", flush=True)
    logs = subprocess.Popen(["podman", "logs", "-f", "--tail", "20", CONTAINER])
    try:
        _wait_http(f"{BASE}/realms/master")
    finally:
        logs.terminate()
        logs.wait(timeout=5)


def _run(command: list[str]) -> None:
    visible = [arg for arg in command if not arg.startswith("KC_BOOTSTRAP_ADMIN_PASSWORD=")]
    print(" ".join(visible), flush=True)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "podman failed")


def _reset_catalog(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)
