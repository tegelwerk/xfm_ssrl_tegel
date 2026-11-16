# XFM Tegel server — operator manual

This manual takes you from a fresh machine to a running XFM data server and dashboard, and then covers day-to-day use: loading your own data, querying it, reading arrays, and fixing common problems.

Everything here was run against `main` of both `xfm_ssrl_tegel` and `tegel`. Two parts are marked as untested: Keycloak mode, and the full edge stack (Keycloak, Caddy, Prefect) around the container image.

---

## 1. What you are running

Two processes, started separately:

| Process | Command | Default address | What it does |
| --- | --- | --- | --- |
| Data server | `tegel-xfm` | `http://127.0.0.1:8000` | Reads your records, builds a catalog, and serves the Tegel read API and Tiled underneath it. Requires a bearer token. |
| Dashboard | `tegel-xfm-dash` | `http://127.0.0.1:8001` | Search page with an image preview. Talks to the data server with the token. Has no login of its own. |

In Keycloak mode a third process runs: Keycloak in podman on port 8080.

```
 browser ──► dashboard :8001 ──(bearer token)──► data server :8000 ──► your files (read-only)
                                                      │
 curl / Python ──────────(bearer token)──────────────┘
```

The two processes are independent. The dashboard holds no data and needs only an HTTP connection to the data server plus a copy of the token, so it can run on another machine: on your laptop, on a web host, or next to the data. Section 7 covers that setup.

The data server never modifies your data. It reads records and array headers, writes its own catalog database to the output directory, and serves bytes from your files on request.

---

## 2. One-time setup

### 2.1 Requirements

- Linux or macOS with Python 3.11 or later.
- [`uv`](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `pip install uv`).
- `git`.
- Only for Keycloak mode: `podman`.

### 2.2 Check out both repositories side by side

The XFM repository finds the Tegel packages at `../tegel`, so both checkouts must sit in the same parent directory:

```bash
mkdir -p ~/tegelwerk && cd ~/tegelwerk
git clone https://github.com/tegelwerk/tegel
git clone https://github.com/tegelwerk/xfm_ssrl_tegel
cd xfm_ssrl_tegel
```

Result:

```
~/tegelwerk/
├── tegel/              core, reference data server, base actions
└── xfm_ssrl_tegel/     XFM plugin, schema, launcher, dashboard
```

### 2.3 Install

```bash
cd ~/tegelwerk/xfm_ssrl_tegel
uv sync --group dev
```

This creates `.venv/` and installs everything editable, including the sibling `tegel` packages. If you change code in either repository, the change takes effect without reinstalling.

### 2.4 Verify the install

```bash
uv run pytest -q
```

All tests should pass. They stage a synthetic measurement, serve it, run the Tegel conformance kit, and slice arrays and tables through Tiled. If they pass, your installation works.

---

## 3. First run on the built-in fixture (2 minutes)

Terminal 1, the data server:

```bash
cd ~/tegelwerk/xfm_ssrl_tegel
uv run tegel-xfm --auth dev --output run
```

Without `--records`, it serves the small fixture in `dataserver/tests/fixtures`. Startup takes 10–30 s the first time. Wait for:

```
Claimed 1 record(s) under .../dataserver/tests/fixtures
Staged 1, skipped 0; errors in run/errors.jsonl
Catalogued 1 record(s); output directory run
Listening on http://127.0.0.1:8000
Bearer token written to run/token
...
INFO:     Application startup complete.
```

Terminal 2, the dashboard:

```bash
cd ~/tegelwerk/xfm_ssrl_tegel
uv run tegel-xfm-dash --token-file run/token
```

Open <http://127.0.0.1:8001>, leave the fields blank, and click **Search**. The fixture's arrays have no chunk files, so its preview is blank. Use your own data (section 4) to see real images.

Stop either process with Ctrl-C.

---

## 4. Serving your own data

### 4.1 What the server picks up

`--records DIR` is searched recursively. The XFM extractor claims two kinds of file and ignores everything else:

**A. Processed RO-Crates** (the normal case). The layout must be:

```
DIR/
└── ProcessedCache/                       ← the parent folder must be named exactly this
    ├── aapl-P1-leaf1-10serp/             ← folder name: a readable name, or a TypeID (4.2)
    │   ├── ro-crate-metadata.json        ← required; must contain a root entity "@id": "./"
    │   ├── lambda-record.yaml            ← optional; beam energy, experiment date
    │   ├── masked-clr.zarr/              ← zarr store(s)
    │   │   ├── clr/                      ← array → part "masked-clr.zarr-clr" (designated)
    │   │   └── mask/                     ← array → part "masked-clr.zarr-mask"
    │   ├── elements.csv                  ← table part
    │   └── overview.png                  ← bytes part
    └── radish-leaf2-sulfur-edge/
        └── ...
```

Other folders under `DIR` (for example `DataCache/`) are ignored.

**B. Stand-alone JSON records**: any `*.json` with `"cache": "ProcessedCache"` and string fields `measurement_id` and `sample_code`. An `id` field is used if it is a valid `xfm_` TypeID; otherwise the id is derived from `measurement_id`. Its zarr store is found from `designated`, relative to the JSON file or to `crate_path`. The fixture `dataserver/tests/fixtures/processed-leaf.json` is an example.

### 4.2 Measurement ids (TypeID)

Every measurement's unique id is a [TypeID](https://github.com/jetify-com/typeid) with prefix `xfm`, for example `xfm_3cwwyaqehzby5v83bnypw4mac6`. The id is used in every URL (`/resources/<id>`, `/api/v1/array/full/<id>/…`). The readable name is kept separately in `measurement_id`.

- **Folder named with a TypeID** (`ProcessedCache/xfm_01h…/`, the preferred layout for new data): the folder name is the id. The readable `measurement_id` is the crate root's `name` in `ro-crate-metadata.json`, or the folder name if there is no `name`.
- **Any other folder name** (`ProcessedCache/aapl-P1-leaf1-10serp/`): the id is derived from the folder name, as the TypeID of a version-5 UUID of that name in a fixed XFM namespace. The folder name becomes `measurement_id`.

Derived ids are deterministic: the same folder name gives the same id at every restart and on every machine, and nothing is written into the data. Renaming a folder changes its id. Two crates with the same folder name in different trees get the same id, and staging logs the second as `duplicate`.

To get the id for a folder name:

```bash
uv run python -c "from tegel.xfm.dataserver.typeid import derive; print(derive('aapl-P1-leaf1-10serp'))"
```

The derived suffix encodes a version-5 UUID rather than the version 7 the TypeID spec prefers, so derived ids are not sortable by time. Any TypeID tool decodes them.

### 4.3 How record fields are filled (crate mode)

| Field | Source |
| --- | --- |
| `id` | the folder name if it is an `xfm_` TypeID, else derived from the folder name (4.2) |
| `measurement_id` | the crate folder name; for a TypeID folder, the root `name` |
| `sample_code` | root entity `sample_code` in `ro-crate-metadata.json` (empty if missing) |
| `campaign` | `aapl` if the sample code starts with `AAPL` or the id starts with `aapl-`; `radish-germ` if the sample code contains `Germ` or the id starts with `radish-germ`; otherwise `radish` |
| `kind` | `mca_fit` for `aapl-*`; `sulfur_edge` if the id contains `sulfur-edge`; otherwise `roi_map` |
| `beam_energy_kev` | `lambda-record.yaml`: `beam_energy_keV`, else `experiment_runs[].beam_energy.has_numeric_value`, else `xrf_image.beam_energy.has_numeric_value` |
| `acquired` | root `dateCreated` (date part), else `experiment_runs[].experiment_date` |
| `technique` | root `technique`, default `xrf` |
| `designated` | the `@id` of the graph entity with `"isDesignated": true` (e.g. `masked-clr.zarr`) |
| `cache`, `crate_path` | `ProcessedCache`, `.` |

The `campaign` and `kind` rules are specific to the current AAPL/radish datasets. For a new campaign, edit `_campaign` and `_kind` in `dataserver/src/tegel/xfm/dataserver/__init__.py`.

### 4.4 What becomes a data part

For each measurement, the server lists named **parts**:

- **Arrays**: every array inside the designated zarr store and inside any other top-level `*.zarr` in the crate. Both zarr v3 (`zarr.json`) and v2 (`.zarray`) work. Consolidated metadata is used when present; otherwise the server looks up to 3 levels into the store. Part name = path inside the crate with `/` replaced by `-`. The `clr` array of the designated store is the *designated* part, which the dashboard shows by default.
- **Side files** at the crate's top level: `.tif/.tiff` (array), `.csv` (table, first row is the header), `.yaml/.yml`, `.png`, `.txt` (bytes).
- **Payload**: the record file itself (`ro-crate-metadata.json` or the JSON record).

Arrays, TIFFs and CSVs are registered with Tiled, so you can slice them. Byte parts are served by Tegel with HTTP Range. Chunks are never read during startup, only headers.

### 4.5 Run it

```bash
uv run tegel-xfm --auth dev --records /data/xfm/caches --output ~/xfm-run
```

- `--output` holds `catalog.db`, `token`, `errors.jsonl` and, in Keycloak mode, `credentials`. Keep it outside the git checkout. `.` is allowed only for an empty current directory.
- Every start **rebuilds the catalog from scratch**. To pick up new crates, restart the server.
- If arrays live outside `--records` (for example `crate_path` points elsewhere), add `--readable /other/dir`. Tiled refuses to read files outside readable directories.

### 4.6 Bad records

By default a record that fails is skipped and logged (`--on-error skip`). Use `--on-error fail` to stop at the first bad record. Inspect the log with:

```bash
uv run python -m tegel.actions.errors --file ~/xfm-run/errors.jsonl --last
uv run python -m tegel.actions.errors --file ~/xfm-run/errors.jsonl --stage process
uv run python -m tegel.actions.errors --file ~/xfm-run/errors.jsonl --json      # raw lines
```

Each line gives the stage where the record failed. The viewer's `--stage` filter does not accept `parts` yet (a bug in `tegel`), so use `grep '"parts"'` on the file for those:

| Stage | Meaning |
| --- | --- |
| `process` | the extractor could not read the record |
| `check` | the record does not match the schema |
| `parts` | a part is missing, unreadable by Tiled, or outside readable storage |
| `id` | no id could be built (empty `measurement_id`) |
| `duplicate` | two records with the same id, e.g. the same folder name in two trees |
| `write` | the catalog write failed |

The extractor is lenient: a crate with no `sample_code` still loads, with empty fields. Check the dashboard for blank sample codes after your first real run.

---

## 5. Using the dashboard

```bash
uv run tegel-xfm-dash --token-file ~/xfm-run/token
```

Options: `--server` (default `http://127.0.0.1:8000`), `--host` (default `127.0.0.1`), `--port` (default `8001`), `--issuer` (Keycloak mode only). The dashboard reads the token file once, at startup. Restart it whenever the data server restarts or the token is replaced. To run it on a different machine from the data server, see section 7.

On the page:

1. Fill any of **Sample code, Campaign, Cache, Kind, Beam energy (keV)**, or none, to list everything. Values must match exactly.
2. Click a row. It shows a PNG preview of the designated array, plus the measurement's metadata and its list of data parts.
3. **Channel** chooses the plane along the last axis of the array (for a `(y, x, element)` CLR stack, the element). The image is scaled between the 1st and 99th percentile, and NaNs are dropped.

The dashboard also has a small JSON API, which is handy for scripts:

| Route | Returns |
| --- | --- |
| `GET /api/search?campaign=aapl` | `{"count": n, "hits": [...]}` |
| `GET /api/resources/{id}` | metadata plus `data.parts` |
| `GET /api/resources/{id}/image?channel=2` | PNG of the designated array |
| `GET /api/resources/{id}/image?part=masked-clr.zarr-mask` | PNG of another 2-D or 3-D array part |

---

## 6. Querying the data server directly

Every request needs the token:

```bash
T=$(cat ~/xfm-run/token)
H="Authorization: Bearer $T"
S=http://127.0.0.1:8000
```

### 6.1 Discover

```bash
curl -H "$H" $S/capabilities                       # spec version, plugins, search fields
curl -H "$H" $S/resources                          # every measurement id (TypeID)
curl -H "$H" "$S/search?campaign=aapl&kind=mca_fit" # RO-Crate JSON-LD of hits
ID=$(uv run python -c "from tegel.xfm.dataserver.typeid import derive; print(derive('aapl-P1-leaf1-10serp'))")
curl -H "$H" $S/resources/$ID/metadata
curl -H "$H" $S/resources/$ID/data               # parts, each with content_url
```

Search works only on `sample_code`, `campaign`, `cache`, `kind` and `beam_energy_kev`, by exact match. Any other field returns `400 Unknown search field`.

### 6.2 Read data

Use the `content_url` from `/data` as the base URL for each part:

```bash
# $ID from 6.1 (or copy an id from /resources or the dashboard)

# array slice as JSON (numpy slice syntax, no spaces)
curl -H "$H" "$S/api/v1/array/full/$ID/masked-clr.zarr-clr?slice=0:10,:,2&format=application/json"

# array slice as raw little-endian bytes (fastest)
curl -H "$H" -o plane.bin "$S/api/v1/array/full/$ID/masked-clr.zarr-clr?slice=:,:,2&format=application/octet-stream"

# table, whole or selected columns
curl -H "$H" "$S/api/v1/table/full/$ID/elements.csv?format=text/csv"
curl -H "$H" "$S/api/v1/table/full/$ID/elements.csv?format=text/csv&column=element"

# byte part, with an optional Range
curl -H "$H" -H "Range: bytes=0-199" $S/resources/$ID/data/lambda-record.yaml/content

# the record file itself
curl -H "$H" $S/resources/$ID/data/content
```

### 6.3 From Python

The `tegel-core` client handles the token, finds each part's `content_url`, and fetches the data:

```python
from pathlib import Path

import numpy as np

from tegel.core.client.data import DataClient

token = Path("~/xfm-run/token").expanduser().read_text().strip()
with DataClient("http://127.0.0.1:8000", token=token) as client:
    hits = client.search({"campaign": "aapl"})
    ids = [hit.identifier for hit in hits.hits]

    rid = ids[0]
    record = client.metadata(rid)
    for part in client.parts(rid):
        print(part.name, part.family.value, part.shape, part.served_by)

    raw = client.read_array(rid, "masked-clr.zarr-clr", slice=":,:,0")
    # raw.shape is the full array shape; reshape to the slice you asked for
    plane = np.frombuffer(raw.data, dtype=raw.data_type).reshape(raw.shape[0], raw.shape[1])

    csv_bytes = client.read_table(rid, "elements.csv")
    yaml_bytes = client.read_bytes(rid, "lambda-record.yaml")
```

Run it with `uv run python script.py` inside the repository, or install `tegel-core` in your own environment.

> Known gap: Tiled's own Python client (`tiled.client.from_uri`) does not work against this server yet. It tries to start the Keycloak browser login instead of using the bearer token. Use `DataClient` or plain HTTP.

---

## 7. Running on different machines

The data server and the dashboard can run on different machines. Common setups:

- the data server on the machine that holds the data, and the dashboard on your laptop;
- the data server on a beamline or analysis node, and the dashboard on a shared web host;
- both on one machine, viewed from your laptop.

The dashboard needs two things from the data-server host: **network access to port 8000** and **a copy of the token file**.

One rule applies to every setup: **the dashboard's `--server` URL must be exactly the data server's public origin.** The server writes that origin into every `content_url`, and the client sends the token only to that origin. If the two differ, search and metadata still work but image previews fail with `401 … Requires scopes ['read:data']`. Without `--origin`, the public origin is `http://<--host>:<--port>`, i.e. `http://127.0.0.1:8000`.

### 7.1 Option A: SSH tunnel (recommended)

The data server stays on `127.0.0.1` and nothing new is exposed on the network. Because the dashboard reaches it at `127.0.0.1:8000` too, the origins match without any flags.

On the **data host**:

```bash
uv run tegel-xfm --auth dev --records /data/xfm/caches --output ~/xfm-run
```

On the **dashboard machine**, which also needs the two repositories and `uv sync` (section 2):

```bash
# copy the token (re-copy after every data-server restart)
scp user@data-host:xfm-run/token ~/xfm-dash/token && chmod 600 ~/xfm-dash/token

# terminal 1: forward local port 8000 to the data server
ssh -N -L 8000:127.0.0.1:8000 user@data-host

# terminal 2: the dashboard, defaults unchanged
uv run tegel-xfm-dash --token-file ~/xfm-dash/token
```

Open <http://127.0.0.1:8001> on the dashboard machine.

In Keycloak mode, also forward the Keycloak port (`-L 8080:127.0.0.1:8080`) and copy `credentials` into the same folder as `token`, so the dashboard can refresh its own token.

### 7.2 Option B: direct network access

The data server listens on the network and announces its public address with `--origin`.

On the **data host**:

```bash
uv run tegel-xfm --auth dev --records /data/xfm/caches --output ~/xfm-run \
    --host 0.0.0.0 --port 8000 --origin http://data-host.lbl.gov:8000
```

On the **dashboard machine**:

```bash
scp user@data-host.lbl.gov:xfm-run/token ~/xfm-dash/token && chmod 600 ~/xfm-dash/token
uv run tegel-xfm-dash --server http://data-host.lbl.gov:8000 --token-file ~/xfm-dash/token
```

Use exactly the same string for `--origin` and `--server`. A hostname in one and an IP address in the other will fail. Port 8000 on the data host must be reachable through the firewall.

Do not use Option B with `--auth dev` on a network you don't control: anyone holding the token can read the whole catalog until it expires. In Keycloak mode, Keycloak listens only on the data host's `127.0.0.1`, so the dashboard cannot refresh its token over the network. Either tunnel port 8080 as in Option A, or re-copy the token when it expires. For a properly shared service, use the edge deployment (section 9), which puts the data server, Keycloak and HTTPS behind one origin.

### 7.3 Making the dashboard reachable from other machines

The dashboard has no login of its own: anyone who can open it reads through its token. Keep it on `127.0.0.1` and reach it with a tunnel (`ssh -L 8001:127.0.0.1:8001 user@dash-host`). Use `--host 0.0.0.0` only on a trusted network.

### 7.4 Token lifecycle across machines

- `--auth dev` creates a **new signing key and token on every data-server start**. The old token stops working immediately. After each restart, re-copy `token` and restart the dashboard.
- Dev tokens expire after `--token-hours` (default 12). For a long session, start the server with a larger value.
- In Keycloak mode, the dashboard renews the token itself when `credentials` sits next to `token` and `--issuer` reaches Keycloak.

## 8. Authentication modes

### 8.1 `--auth dev` (local work)

- An issuer inside the server process signs one reader token and writes it to `<output>/token` (permissions 600).
- The token lasts `--token-hours` (default 12). When it expires, restart the data server, then the dashboard.
- Nothing else is needed: no container and no passwords.

### 8.2 `--auth keycloak` (default; untested end to end on this branch)

```bash
uv run tegel-xfm --records /data/xfm/caches --output ~/xfm-run
```

- On the first run it writes `<output>/credentials` with generated passwords for the Keycloak admin, the client secret and the user `reader`. Later runs reuse them.
- It starts the container `tegel-keycloak` (`quay.io/keycloak/keycloak:26.4.0`) on `127.0.0.1:8080`, or reuses it if it is already running.
- It creates the realm `tegel`, the client `tegel` and the user `reader`. It then fetches a reader token into `<output>/token`.
- The admin console is at <http://127.0.0.1:8080/admin> (user `admin`). The password is printed at startup and stored in `credentials`.
- The dashboard sees `credentials` next to the token file and refreshes the token itself.
- To reset everything: `podman rm -f tegel-keycloak`, then delete `<output>/credentials`.

---

## 9. Production: the edge deployment

For a shared server with HTTPS, Keycloak, Prefect-driven ingest and incremental catalog updates, use the reference stack in `../tegel/tegel-base/deploy` (see its README). It builds its image from this repository's `Containerfile` and needs these settings:

- `TEGEL_PROFILE=~/tegelwerk/xfm_ssrl_tegel/dataserver/schema/profile.yaml`
- `TEGEL_SCHEMA_ROOT=~/tegelwerk/xfm_ssrl_tegel/schemas`
- `TEGEL_RECORDS=` your caches directory

The image installs the exact versions in `uv.lock`, via `deploy/constraints.txt`. After changing `uv.lock`, run `deploy/export-constraints.sh`; a test fails if the two disagree. The image has been built, and its data server and staging tested, with the compose environment and mounts. The full stack with Keycloak, Caddy and Prefect has not been run yet.

---

## 10. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Claimed 0 record(s)` | Crates are not under a folder named exactly `ProcessedCache`, or `ro-crate-metadata.json` has no `"@id": "./"` entity. See 4.1. |
| `token file not found` when starting the dashboard | The data server has not finished starting. Wait for `Bearer token written`. |
| `401` on every request | Missing or expired token. Check the `Authorization: Bearer …` header; in dev mode, restart after `--token-hours`. |
| `400 Unknown search field` | Only the five search fields in 6.1 can be searched. |
| Measurement shows no parts | `designated` names no existing store, and the crate has no top-level `*.zarr`. Check the `isDesignated` entity in the crate. |
| `parts` errors: `outside readable storage` | Data lives outside `--records`. Add `--readable DIR`. |
| `parts` errors: `Tiled adapter … failed` | A zarr header is corrupt or uses a codec Tiled cannot open. Try `zarr.open` on it in Python. |
| Blank or black preview | Chunk files are missing (zarr returns fill values), or the plane is constant. Try another channel. |
| Remote dashboard: search works but image preview fails with `401 … read:data` | The dashboard's `--server` differs from the data server's `--origin`, so the token is not sent with array requests. Make them identical, or use the SSH tunnel (7.1). |
| Remote dashboard: everything returns 401 after a server restart | Dev mode issued a new token. Re-copy `token` and restart the dashboard (7.4). |
| `port 8000 already in use` | Another server is still running. Use `--port 8010` and `tegel-xfm-dash --server http://127.0.0.1:8010`. |
| New crates don't appear | The catalog is built only at startup. Restart `tegel-xfm`. |
| Arrays are deeper than 3 levels inside a store | Write consolidated metadata (`zarr.consolidate_metadata(store)`), or move the arrays up. |

---

## 11. Where things live

| Path | Contents |
| --- | --- |
| `dataserver/src/tegel/xfm/dataserver/__init__.py` | extractor (record fields), reader (parts), resource type |
| `dataserver/src/tegel/xfm/dataserver/typeid.py` | TypeID encode/decode and the deterministic `derive(name)` |
| `dataserver/schema/profile.yaml` | search fields, displayed properties, id field; names the schema |
| `schemas/xfm/processed.yaml` | LinkML schema `ProcessedMeasurement` (v2) |
| `actions/src/tegel/xfm/actions/serve.py` | the `tegel-xfm` launcher |
| `dash/src/tegel/xfm/dash/app.py` | the dashboard |
| `actions/tests/test_serve.py` | end-to-end test; `write_crate()` there builds a realistic crate you can copy |

### Adding a search field

1. Add the slot to `schemas/xfm/processed.yaml` with `range: string`. Search fields must be categorical, so no numbers or dates.
2. Fill it in `ProcessedExtractor.process`, and add its name to `FIELDS` in the plugin.
3. Add it under `search:` (and `properties:`) in `profile.yaml`. Bump `schema_version` in both files and `RECORD.version` in the plugin if the change is incompatible.
4. Add it to `FIELDS`/`COLUMNS` in the dashboard if you want it on the page.
5. Run `uv run pytest`, then restart the server.
