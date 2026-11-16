# xfm_ssrl_tegel

The SSRL X-ray fluorescence microscopy (XFM) incarnation of [Tegel](https://github.com/tegelwerk/tegel), on spec 0.4.0.

This repository holds two independent components:

| | **Data server** (Part I) | **Dashboard** (Part II) |
| --- | --- | --- |
| Purpose | Catalogs processed XFM measurements and serves metadata and sliceable data over HTTP | Web page to search a data server and preview images |
| Command | `tegel-xfm` | `tegel-xfm-dash` |
| Packages | `dataserver/` (plugin), `actions/` (launcher), `schemas/` | `dash/` |
| Runs on | the machine that can read the data | anywhere |
| Needs | the data, both Tegel checkouts, ~1 GB environment | a data server URL and a bearer token, ~100 MB environment |
| Default port | 8000 | 8001 |

The two components share no code, configuration or files. Their only contract is the data server's HTTP read API plus a bearer token. You can install, run, upgrade or replace either one without the other, on the same machine or on different ones. Any client of the read API (curl, Python, another dashboard) is as much a peer of the data server as this dashboard is.

**Full operator walkthrough: [docs/MANUAL.md](docs/MANUAL.md).**

---

# Part I — Data server

The data server reads processed XFM RO-Crates, catalogs them, and serves them read-only. Each measurement's zarr arrays, CSV tables and TIFFs can be sliced over HTTP through Tiled. It never writes into your data.

## I.1 Install

The data server builds on the Tegel packages and must sit next to a `tegel` checkout:

```bash
mkdir -p ~/tegelwerk && cd ~/tegelwerk
git clone https://github.com/tegelwerk/tegel
git clone https://github.com/tegelwerk/xfm_ssrl_tegel
cd xfm_ssrl_tegel
uv sync --package tegel-xfm-actions      # data server only; the dashboard is not installed
```

## I.2 Run

```bash
uv run --package tegel-xfm-actions tegel-xfm --auth dev --records /path/to/caches --output run
```

At startup the server claims every crate under `--records`, stages it, builds `run/catalog.db` from scratch, and listens on `http://127.0.0.1:8000`. It writes:

- `run/token`: a bearer token for clients;
- `run/errors.jsonl`: records that were skipped, and why.

Restart the server to pick up new crates.

| Option | Default | Meaning |
| --- | --- | --- |
| `--records DIR` | the test fixture | directory searched for crates |
| `--output DIR` | `.` (must be empty) | catalog, token, error log, credentials |
| `--auth dev\|keycloak` | `keycloak` | `dev`: in-process token issuer, local use only. `keycloak`: Keycloak in podman |
| `--token-hours N` | 12 | lifetime of the dev token |
| `--host`, `--port` | `127.0.0.1`, `8000` | where to listen |
| `--origin URL` | `http://<host>:<port>` | public address written into every `content_url`; set it when clients reach the server by another name |
| `--readable DIR` | | extra directory Tiled may read assets from (repeatable) |
| `--on-error skip\|fail` | `skip` | skip and log bad records, or stop at the first |

## I.3 Data layout

```
/path/to/caches/
└── ProcessedCache/
    └── <name or TypeID>/              folder name; see I.4
        ├── ro-crate-metadata.json     root "@id": "./" with sample_code, technique, dateCreated (and name);
        │                              one entity with "isDesignated": true (e.g. masked-clr.zarr/)
        ├── lambda-record.yaml         optional: beam_energy_keV, experiment_runs
        ├── *.zarr/                    every array becomes a sliceable part (zarr v2 or v3)
        └── *.csv, *.tif, *.png, ...   side files become parts too
```

Stand-alone JSON records with `"cache": "ProcessedCache"` are also accepted (manual §4.1).

## I.4 Measurement ids

Every measurement's id is a [TypeID](https://github.com/jetify-com/typeid) with prefix `xfm`, e.g. `xfm_3cwwyaqehzby5v83bnypw4mac6`.

- If the crate folder is named with an `xfm_` TypeID, that is the id, and the readable name comes from the crate root's `name`.
- Otherwise the id is derived deterministically from the folder name (UUIDv5), so it is stable across restarts and nothing is written into the data. The folder name is kept as the readable `measurement_id`.

To get the id for a folder name:

```bash
uv run python -c "from tegel.xfm.dataserver.typeid import derive; print(derive('aapl-P1-leaf1-10serp'))"
```

## I.5 Read API

Every request carries `Authorization: Bearer <token>`.

```bash
T=$(cat run/token); H="Authorization: Bearer $T"; S=http://127.0.0.1:8000

curl -H "$H" $S/capabilities                          # spec version, plugins, search fields
curl -H "$H" $S/resources                             # all ids
curl -H "$H" "$S/search?campaign=aapl&kind=mca_fit"   # exact-match search
curl -H "$H" $S/resources/<id>/metadata
curl -H "$H" $S/resources/<id>/data                   # parts, each with content_url
curl -H "$H" "$S/api/v1/array/full/<id>/masked-clr.zarr-clr?slice=:,:,0&format=application/json"
curl -H "$H" "$S/api/v1/table/full/<id>/elements.csv?format=text/csv"
```

Search fields: `sample_code`, `campaign`, `cache`, `kind`, `beam_energy_kev`. For Python, use `tegel.core.client.data.DataClient` (manual §6.3).

## I.6 Serving clients on other machines

Clients only need network access to the server and the token. Either keep the server on `127.0.0.1` and let clients use an SSH tunnel (recommended), or listen on the network with a matching `--origin`:

```bash
uv run --package tegel-xfm-actions tegel-xfm --auth dev --records /data/caches --output run \
    --host 0.0.0.0 --origin http://data-host.lbl.gov:8000
```

In dev mode every restart issues a new token. Hand clients the new `run/token`. Do not expose a dev-mode server on an untrusted network (manual §7).

## I.7 Production

`Containerfile` builds the image used by the edge deployment in `tegel/tegel-base/deploy` (Caddy, Keycloak, Prefect ingest). The image is pinned to `uv.lock` through `deploy/constraints.txt`; regenerate that file with `deploy/export-constraints.sh` after changing the lock.

```bash
cd ~/tegelwerk && docker build -f xfm_ssrl_tegel/Containerfile -t tegel-dataserver:local .
```

## I.8 Data server files

| Path | What |
| --- | --- |
| `dataserver/` | plugin package `tegel-xfm`: extractor, reader and resource type, loaded by entry point. Ships no schema, by the Tegel contract. |
| `dataserver/src/tegel/xfm/dataserver/typeid.py` | TypeID encode/decode and `derive(name)` |
| `dataserver/schema/profile.yaml` | search profile: search fields, displayed properties, id field |
| `schemas/xfm/processed.yaml` | LinkML schema `ProcessedMeasurement` v3; `TEGEL_SCHEMA_ROOT` points here |
| `actions/` | launcher package `tegel-xfm-actions` (`tegel-xfm`): stage and serve |
| `Containerfile`, `deploy/` | production image and its version pins |

---

# Part II — Dashboard

The dashboard is a small web app for searching a data server and previewing the designated array of each measurement. It holds no data and reads nothing from disk except its token file. It needs only:

1. the URL of a running data server, and
2. a bearer token for that server.

## II.1 Install

No checkout of either repository is needed:

```bash
uv tool install --no-sources \
  --with "tegel-core @ git+https://github.com/tegelwerk/tegel#subdirectory=tegel-core" \
  "tegel-xfm-dash @ git+https://github.com/tegelwerk/xfm_ssrl_tegel#subdirectory=dash"
```

This puts `tegel-xfm-dash` on your PATH, in its own environment of about 100 MB, without Tiled or any server code. Run the same command with `--reinstall` to upgrade. `--no-sources` makes uv ignore the repository's local path settings, so `tegel-core` is taken from the git URL.

If you already have a checkout from Part I, `uv sync --package tegel-xfm-dash` in it installs the dashboard alone.

## II.2 Run

```bash
tegel-xfm-dash --server http://127.0.0.1:8000 --token-file ~/xfm-dash/token
```

Open <http://127.0.0.1:8001>.

| Option | Default | Meaning |
| --- | --- | --- |
| `--server URL` | `http://127.0.0.1:8000` | data server address; must equal the server's public origin |
| `--token-file PATH` | required | bearer token, read once at startup |
| `--issuer URL` | `http://127.0.0.1:8080/realms/tegel` | Keycloak realm, used only when a `credentials` file sits next to the token |
| `--host`, `--port` | `127.0.0.1`, `8001` | where the dashboard listens |

## II.3 Getting a token and reaching the server

Copy the token from the data-server host:

```bash
mkdir -p ~/xfm-dash
scp user@data-host:path/to/run/token ~/xfm-dash/token && chmod 600 ~/xfm-dash/token
```

If the data server listens only on its own `127.0.0.1` (the default), forward its port:

```bash
ssh -N -L 8000:127.0.0.1:8000 user@data-host
tegel-xfm-dash --token-file ~/xfm-dash/token
```

If the server listens on the network, `--server` must be exactly the server's `--origin`. Otherwise search works but previews fail with `401`, because the token is only sent to that origin.

- **Dev tokens:** these change on every data-server restart and expire after `--token-hours`. Re-copy the token and restart the dashboard.
- **Keycloak:** copy `credentials` next to `token` and point `--issuer` at a reachable Keycloak, and the dashboard renews its token itself.

## II.4 Using it

Search by any of the fields, or leave them blank to list everything. Click a row for the preview, the metadata and the list of data parts. **Channel** selects the plane along the array's last axis.

The dashboard has no login of its own: whoever can open it reads through its token. Keep it on `127.0.0.1`, or behind a tunnel or proxy you control.

It also serves a small JSON API: `/api/search`, `/api/resources/<id>`, `/api/resources/<id>/image?channel=N&part=NAME` (manual §5).

## II.5 Dashboard files

| Path | What |
| --- | --- |
| `dash/` | package `tegel-xfm-dash`; depends only on `tegel-core` (the read-API client and contracts) |

---

# Part III — Developing in this repository

The repository is a uv workspace with three members (`dataserver`, `actions`, `dash`). For development, install everything:

```bash
uv sync --group dev
uv run pytest            # plugin, conformance kit, end-to-end serve test, dashboard
uv run ruff check .
```

The dashboard tests use a mocked data server, and the data server tests do not touch the dashboard. `actions/tests/test_serve.py::write_crate` builds a realistic synthetic crate to use as a template for test data.

## Known gaps

- Keycloak mode and the full edge stack (Keycloak, Caddy, Prefect) have not been run end to end. The container image itself is built and its data server tested.
- Tiled's Python client (`tiled.client.from_uri`) cannot use the bearer token yet. Use `DataClient` or HTTP.
- The catalog is rebuilt at every start; there is no incremental ingest locally. The edge deployment has it.
