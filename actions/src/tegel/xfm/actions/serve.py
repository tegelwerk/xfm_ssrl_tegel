"""Stage processed XFM records with the plugin and serve them with the reference server.

``--auth keycloak`` (default) starts Keycloak in podman and configures a realm.
``--auth dev`` signs tokens with an in-process issuer instead; use it for local work only.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from tegel.dataserver.app import create_app
from tegel.dataserver.config import ServerConfig

KEYCLOAK_PORT = 8080
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.4.0"
CONTAINER = "tegel-keycloak"
REALM = "tegel"
CLIENT_ID = "tegel"
READER = "reader"
HOST = "127.0.0.1"
PORT = 8000
# Scopes on the dev token, so an action server trusting the same issuer accepts it too.
DEV_SCOPES = "read:metadata read:data actions:read actions:run catalog:write"
BASE = f"http://{HOST}:{KEYCLOAK_PORT}"
REPO = Path(__file__).resolve().parents[5]
DATASERVER = REPO / "dataserver"


def main() -> None:
    """Stage a directory of records, merge it into a fresh catalog, and serve it."""
    args = _arguments()
    records = Path(args.records).resolve()
    output = _output_directory(Path(args.output))
    profile = Path(args.profile).resolve()
    schema_root = Path(args.schema_root).resolve()
    if not records.is_dir():
        raise SystemExit(f"records directory not found: {records}")
    if not profile.is_file():
        raise SystemExit(f"profile not found: {profile}")
    if not schema_root.is_dir():
        raise SystemExit(f"schema root not found: {schema_root}")
    readable = [records, *[Path(item).resolve() for item in args.readable]]
    if args.watch:
        if not args.reader:
            raise SystemExit("--watch needs --reader (see `xfm-etl readers`)")
        raw = Path(args.watch).resolve()
        if not raw.is_dir():
            raise SystemExit(f"raw directory not found: {raw}")
        args.action_server = True
        if str(raw) not in args.beamline:
            args.beamline.append(str(raw))
    os.environ["TEGEL_PROFILE"] = str(profile)
    os.environ["TEGEL_SCHEMA_ROOT"] = str(schema_root)
    catalog = output / "catalog.db"
    count = stage(records, output, readable, on_error=args.on_error)
    origin = args.origin or f"http://{args.host}:{args.port}"
    if args.auth == "dev":
        from tegel.dataserver.testing import OIDCIssuer

        issuer = OIDCIssuer(lifetime=args.token_hours * 3600)
        with issuer as issued:
            token = issuer.sign(scope=DEV_SCOPES, audience=issued.audience)
            env = output / "dev-issuer.env"
            env.write_text(
                f"TEGEL_AUTH=keycloak\nTEGEL_OIDC_ISSUER={issued.issuer}\n"
                f"TEGEL_OIDC_AUDIENCE={issued.audience}\nTEGEL_OIDC_CLIENT_ID={issued.client_id}\n",
                encoding="utf-8",
            )
            print(f"Dev issuer for other Tegel services (e.g. an action server): {env}",
                  flush=True)
            actions = None
            if args.action_server:
                oidc = {"TEGEL_AUTH": "keycloak", "TEGEL_OIDC_ISSUER": issued.issuer,
                        "TEGEL_OIDC_AUDIENCE": issued.audience,
                        "TEGEL_OIDC_CLIENT_ID": issued.client_id}
                service = issuer.sign(subject="xfm-etl", scope="read:metadata",
                                      audience=issued.audience)
                actions = _start_action_server(args, output, records, profile, schema_root,
                                               catalog, origin, oidc, refresh_token=service)
            watcher = _start_watcher(args, records, token) if args.watch else None
            try:
                _serve(args, origin, output, profile, schema_root, catalog, readable, count,
                       issuer=issued.issuer, audience=issued.audience,
                       client_id=issued.client_id, client_secret=issued.client_secret,
                       token=token)
            finally:
                if watcher is not None:
                    watcher.set()
                _stop(actions)
        return
    creds = _credentials(output)
    print(f"Keycloak admin: {BASE}/admin  user admin", flush=True)
    print(f"Keycloak admin password: {creds['admin_password']}", flush=True)
    print(f"Reader user {READER} password: {creds['user_password']}", flush=True)
    _start_keycloak(creds["admin_password"])
    issuer_url = _realm(creds)
    actions = None
    if args.action_server:
        oidc = {"TEGEL_AUTH": "keycloak", "TEGEL_OIDC_ISSUER": issuer_url,
                "TEGEL_OIDC_AUDIENCE": CLIENT_ID, "TEGEL_OIDC_CLIENT_ID": CLIENT_ID,
                "TEGEL_OIDC_CLIENT_SECRET": creds["client_secret"]}
        actions = _start_action_server(args, output, records, profile, schema_root, catalog,
                                       origin, oidc, refresh_token="")
    watcher = None
    if args.watch:
        watcher = _start_watcher(args, records, _reader_token(issuer_url, creds))
    try:
        _serve(args, origin, output, profile, schema_root, catalog, readable, count,
               issuer=issuer_url, audience=CLIENT_ID, client_id=CLIENT_ID,
               client_secret=creds["client_secret"], token=_reader_token(issuer_url, creds))
    finally:
        if watcher is not None:
            watcher.set()
        _stop(actions)


def stage(records: Path, output: Path, readable: list[Path], *, on_error: str = "skip") -> int:
    """Claim every record under ``records``, stage it, and merge into ``output/catalog.db``."""
    from tegel.actions.merge import merge_staging
    from tegel.actions.staging import claim_paths, write_staging

    paths = claim_paths(records)
    print(f"Claimed {len(paths)} record(s) under {records}", flush=True)
    staging = output / "staging.db"
    catalog = output / "catalog.db"
    _reset_catalog(catalog)
    error_log = output / "errors.jsonl"
    result = write_staging(
        paths, staging, on_error=on_error, error_log=error_log, roots=readable
    )
    for error in result.errors:
        print(f"skipped {error}", flush=True)
    merge_staging(str(staging), str(catalog))
    _reset_catalog(staging)
    print(f"Staged {result.staged}, skipped {result.skipped}; errors in {error_log}", flush=True)
    return result.staged


def _serve(args, origin, output, profile, schema_root, catalog, readable, count, *,
           issuer, audience, client_id, client_secret, token) -> None:
    app = create_app(
        ServerConfig(
            public_origin=origin,
            catalog_uri=f"sqlite:///{catalog.resolve()}",
            profile_path=profile,
            schema_root=schema_root,
            issuer_url=issuer,
            audience=audience,
            client_id=client_id,
            client_secret=client_secret,
            session_secret=secrets.token_hex(32),
            host=args.host,
            port=args.port,
            readable_storage=readable,
        )
    )
    token_path = output / "token"
    token_path.write_text(token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    print(f"Catalogued {count} record(s); output directory {output}", flush=True)
    print(f"Listening on {origin}", flush=True)
    print(f"Bearer token written to {token_path}", flush=True)
    print(
        f'curl -H "Authorization: Bearer $(cat {token_path})" "{origin}/search?study=aapl"',
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port)



def _start_action_server(args, output: Path, records: Path, profile: Path, schema_root: Path,
                         catalog: Path, origin: str, oidc: dict[str, str], *,
                         refresh_token: str) -> subprocess.Popen:
    """Start the co-located action server, wired to this data server.

    It serves ``--actions`` (default ``xfm.etl``), writes crates into the records
    directory, merges into this server's catalog, and refreshes it.
    """
    beamline = [str(Path(item).resolve()) for item in args.beamline]
    action_origin = args.action_origin or f"http://{args.host}:{args.action_port}"
    env = {
        **os.environ,
        **oidc,
        "TEGEL_PUBLIC_ORIGIN": action_origin,
        "TEGEL_HOST": args.host,
        "TEGEL_PORT": str(args.action_port),
        "TEGEL_RESULTS_DIR": str((output / "actions").resolve()),
        "TEGEL_READABLE_STORAGE": os.pathsep.join([*beamline, str(records)]),
        "TEGEL_FORWARD_ORIGINS": origin,
        "TEGEL_ACTIONS": args.actions,
        "TEGEL_SESSION_SECRET": secrets.token_hex(32),
        "TEGEL_RECORDS": str(records),
        "TEGEL_CATALOG_URI": f"sqlite:///{catalog.resolve()}",
        "TEGEL_PROFILE": str(profile),
        "TEGEL_SCHEMA_ROOT": str(schema_root),
        "TEGEL_REFRESH_URL": f"http://{args.host}:{args.port}/refresh-catalog",
        "TEGEL_REFRESH_TOKEN": refresh_token,
    }
    log = (output / "actions.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "tegel.actionserver", "serve"],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    print(f"Action server ({args.actions}) on {action_origin}; log {output / 'actions.log'}",
          flush=True)
    if beamline:
        print(f"Beamline files readable by xfm.etl under: {', '.join(beamline)}", flush=True)
    return process



def _start_watcher(args, records: Path, token: str) -> threading.Event:
    """Watch ``--watch`` and send each ready batch to the co-located ``xfm.etl``.

    Conversion and ingest happen in the action server, one run per batch, so each
    batch has a run record. The watcher's ledger is ``<records>/.xfm-etl/ledger.json``.
    """
    import logging

    import yaml

    from xfm_etl.watch import watch

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    action_origin = args.action_origin or f"http://{args.host}:{args.action_port}"
    samples = (
        yaml.safe_load(Path(args.sample_sheet).read_text("utf-8")) if args.sample_sheet else None
    )
    stop = threading.Event()
    headers = {"Authorization": f"Bearer {token}"}

    def convert(batch) -> list[str]:
        parameters: dict = {"reader": args.reader, "study": batch.study or ""}
        if samples:
            parameters["samples"] = samples
        body = {
            "action": "xfm.etl",
            "inputs": {"files": [path.resolve().as_uri() for path in batch.files]},
            "parameters": parameters,
        }
        with httpx.Client(base_url=action_origin, headers=headers, timeout=60) as client:
            response = client.post("/runs", json=body)
            response.raise_for_status()
            run_id = response.json()["id"]
            run = client.post(f"/runs/{run_id}/wait", params={"timeout": 3600},
                              timeout=3700).json()
            if run.get("state") != "succeeded":
                raise RuntimeError(f"run {run_id} {run.get('state')}: {run.get('error')}")
            report = client.get(f"/resources/{run_id}/data/ingested/content").json()
        return [crate["id"] for crate in report.get("crates", [])]

    def loop() -> None:
        deadline = time.monotonic() + 120
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                if httpx.get(f"{action_origin}/actions", headers=headers, timeout=5).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        watch(Path(args.watch), records, args.reader, convert, interval=args.interval,
              stable=args.stable, study=args.study, stop=stop.is_set)

    threading.Thread(target=loop, name="xfm-watch", daemon=True).start()
    print(f"Watching {Path(args.watch).resolve()} (reader {args.reader}); crates into {records}",
          flush=True)
    return stop

def _stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()

def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tegel-xfm")
    parser.add_argument("--records", default=str(DATASERVER / "tests" / "fixtures"))
    parser.add_argument("--output", default=".")
    parser.add_argument("--profile", default=str(DATASERVER / "schema" / "profile.yaml"))
    parser.add_argument(
        "--schema-root",
        default=os.environ.get("TEGEL_SCHEMA_ROOT") or str(REPO / "schemas"),
    )
    parser.add_argument("--readable", action="append", default=[],
                        help="extra directory Tiled may read assets from (repeatable)")
    parser.add_argument("--auth", choices=["keycloak", "dev"], default="keycloak")
    parser.add_argument("--token-hours", type=int, default=12, help="dev auth token lifetime")
    parser.add_argument("--on-error", choices=["skip", "fail"], default="skip")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--origin", default="", help="public origin, if behind a proxy")
    parser.add_argument("--action-server", action="store_true",
                        help="also run the co-located action server (the xfm.etl pipeline)")
    parser.add_argument("--action-port", type=int, default=8010)
    parser.add_argument("--action-origin", default="", help="action server public origin")
    parser.add_argument("--actions", default="xfm.etl",
                        help="glob of action ids the co-located server offers")
    parser.add_argument("--beamline", action="append", default=[],
                        help="directory of beamline files xfm.etl may read (repeatable)")
    parser.add_argument("--watch", default="",
                        help="raw directory to convert automatically (implies --action-server)")
    parser.add_argument("--reader", default="", help="xfm-etl reader for --watch")
    parser.add_argument("--study", default=None,
                        help="study for every watched file (default: first folder under --watch)")
    parser.add_argument("--sample-sheet", default="",
                        help="YAML: sample_code -> aims-leaf PlantSample fields")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    parser.add_argument("--stable", type=float, default=60.0,
                        help="seconds a raw file must be unchanged before conversion")
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
