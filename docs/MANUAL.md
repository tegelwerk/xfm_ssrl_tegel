# XFM Tegel — operator manual

This manual takes you from a fresh machine to a working XFM setup:
- raw beamline files converted to OME-Zarr scan crates;
- a data server that catalogs and serves them;
- a separate action server that processes them;
- a dashboard to look at them.

After setup it covers day-to-day use, querying, and fixing common problems.

Everything here was run against `main` of both `xfm_ssrl_tegel` and `tegel`. Three parts are not yet tested end to end, and are marked where they appear:
- the SSRL beamline-file reader, which still needs a real raw file;
- Keycloak mode;
- the full edge stack (Keycloak, Caddy, Prefect) around the container image.

---

## 1. What you are running

```
beamline files ──xfm-etl──► scan crates ──tegel-xfm──► data server :8000 ──┬─► dashboard :8001
 (read only)               xfm_<typeid>/                 (raw scans)       │
                           ├ ro-crate-metadata.json                        ├─► curl / Python
                           ├ aims-leaf.json                                │
                           └ scan.ome.zarr/          action server :8010 ◄─┘ inputs by URL
                                                     (xfm.* processing; results record what they used)
```

| Piece | Command | What it does |
| --- | --- | --- |
| ETL | `xfm-etl`, or Tegel's `ingest-raw` with the `xfm` converter plugin | Converts beamline files into one scan crate per raster scan: an OME-Zarr image of the MCA cube, an aims-leaf record, and an RO-Crate. Automatically, it runs as the pre-ingest stage of the data server's ingest pipeline (section 4.4). |
| Data server | `tegel-xfm` | Catalogs scan crates (raw data only) and serves metadata and sliceable arrays. Read-only. |
| Action server | `tegel-actionserver` with `tegel-xfm-compute` | Runs processing jobs (`xfm.roi_maps`, `xfm.import_processed`, …) on request. It reads inputs by URL, including straight from the data server, and keeps every run as a searchable result that records which inputs it `used`. |
| Dashboard | `tegel-xfm-dash` | Search page with image previews. Talks to the data server with a token. |

Each piece is independent and can run on its own machine. The automatic ETL runs inside the data server's ingest pipeline as a converter plugin, so it needs no action server (section 4.4). Processing lives on a separate action server, and processed products never live on the data server: they are action-server results.

---

## 2. One-time setup

### 2.1 Requirements

- Linux or macOS with Python 3.11 or later (3.12 or later to run the aims-leaf validation tests).
- [`uv`](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- `git`.
- Only for Keycloak mode: `podman`.

### 2.2 Check out both repositories side by side

```bash
mkdir -p ~/tegelwerk && cd ~/tegelwerk
git clone https://github.com/tegelwerk/tegel
git clone https://github.com/tegelwerk/xfm_ssrl_tegel
```

```
~/tegelwerk/
├── tegel/              core, reference data server, base actions, action server
└── xfm_ssrl_tegel/     ETL, XFM plugin + schema, launcher, actions, dashboard
```

### 2.3 Install

Install everything for development:

```bash
cd ~/tegelwerk/xfm_ssrl_tegel
uv sync --group dev
uv run pytest -q
```

Or install one piece only:

| Piece | Command |
| --- | --- |
| ETL | `uv sync --package xfm-ssrl-etl` |
| Data server | `uv sync --package tegel-xfm-actions` |
| Action server with XFM actions | `uv sync --package tegel-xfm-compute` |
| Dashboard | `uv sync --package tegel-xfm-dash`, or with no checkout, see the README, Part IV |

The tests cover the whole chain:
- the ETL writes crates, which are checked against the OME-Zarr 0.5 and aims-leaf models;
- the data server stages and serves them and passes the Tegel conformance kit;
- Tegel's `ingest-raw` runs with the XFM converter;
- the processing actions run directly and through an action server;
- the dashboard is tested against a mocked server.

---

## 3. First run (5 minutes, synthetic data)

```bash
cd ~/tegelwerk/xfm_ssrl_tegel

# 1. two synthetic scans -> scan crates
uv run xfm-etl convert --reader synthetic AAPL_10serp_P1_leaf1 RAD_leaf2 \
    --out ~/xfm-demo/crates --study aapl

# 2. terminal 1: data server
uv run tegel-xfm --auth dev --records ~/xfm-demo/crates --output ~/xfm-demo/run

# 3. terminal 2: dashboard
uv run tegel-xfm-dash --token-file ~/xfm-demo/run/token
```

Wait for `Application startup complete` in terminal 1. Then open <http://127.0.0.1:8001> and click **Search**. Click a scan to see its total-counts map. Pick `mca-0` under **Array** and set **Channel** to 640 to see the Fe K-alpha plane (6.4 keV at 10 eV per channel).

Without `--records`, `tegel-xfm` serves the tiny committed fixture in `dataserver/tests/fixtures`.

---

## 4. Converting raw data (ETL)

### 4.1 What the ETL writes

An output directory with a fixed outline, one crate per raster scan:

```
<out>/
├── README.md                              how the directory is organized
├── index.csv                              one row per scan: id, study, sample, scan name, beamline,
│                                          date, energy, map shape, MCA channels, path, raw sources, time
├── .xfm-etl/ledger.json                   `xfm-etl watch`'s record of converted raw files (Tegel's ledger is in its output directory)
└── <study>/<sample_code>/xfm_<typeid>/    one scan crate; the TypeID is the unique name
    ├── ro-crate-metadata.json             RO-Crate 1.1, conformsTo https://w3id.org/tegel/xfm/raw-scan/0.1
    ├── aims-leaf.json                     aims-leaf Dataset (see 4.3)
    └── scan.ome.zarr/                     OME-Zarr 0.5 (zarr v3)
        ├── 0, 1, 2, …                     MCA cube (c, y, x); 2x pyramid in y and x, counts summed
        ├── scalers/I0, …                  per-pixel beamline scalers
        └── quicklook/total_counts         sum over all MCA channels
```

- Scans without a study go under `_unassigned/`. Study and sample folder names keep letters, digits, `.`, `_` and `-`; any other run of characters becomes `_`.
- `--layout flat` puts crates directly under `<out>` instead.
- The channel axis `c` is the MCA channel. The energy of channel `n` is `offset_ev + n * gain_ev`. Both values are stored in the image attributes under `xfm.mca`, next to the step size, the channel count and the scaler names.
- The scale transforms give the pixel size in micrometres at each level.
- The beamline files are only read, and they are referenced from the crate by `file:` URI.
- A crate is written under a temporary name and renamed when it is complete, and `index.csv` is rewritten atomically.

### 4.2 Running it

```bash
uv run xfm-etl readers
uv run xfm-etl convert --reader <reader> /beamline/files/... --out /data/xfm/crates \
    --study aapl --sample-sheet samples.yaml
```

- `--study` sets the aims-leaf Study (the campaign).
- `--sample-sheet` is YAML mapping each sample code to aims-leaf `PlantSample` fields. The required ones are `genus`, `species`, `collection_date_time`, `tissue`, `growth_facility` (one of `growth_chamber`, `greenhouse`, `field`, …), `growth_medium` and `developmental_stage`. Without a sheet the record carries a generic aims-leaf `Sample` (`sample_type: tissue`).

> **Status:** the only reader so far is `synthetic`. The SSRL beamline-format reader needs one real raw scan, or a description of the file format, before it can be written. It goes in `etl/src/xfm_etl/readers/`. A reader returns `RawScan(meta=ScanMeta(...), mca=cube_yxc, scalers={...})` and the writer does the rest.

### 4.3 The aims-leaf record

`aims-leaf.json` is an aims-leaf `Dataset` containing:

| Collection | Content |
| --- | --- |
| `experiment_runs` | one `ExperimentRun`: `experiment_code` (scan name), `technique: xray_microprobe`, `experiment_date`, `beamline`, `energy` (keV), `dwell_time` (ms), `beam_size_x/y` (µm), `raw_data_location`, `processing_status: raw` |
| `samples` or `plantsamples` | the sample, by `sample_code` |
| `instruments` | the beamline (`instrument_code` `SSRL-BL2-3`, `facility_name`, `beamline_id`) |
| `studies` | the study, when `--study` is given |
| `data_files` | the OME-Zarr image (`data_type: raw_data`, `file_role: converted`) and each beamline file (`file_role: source`) |
| associations | experiment–sample, experiment–instrument, sample–data file, study–experiment |

It validates against the aims-leaf pydantic models with one exception: `file_format: zarr` is not yet in aims-leaf's `FileFormatEnum`. The tests check this. Change requests for aims-leaf:
- add `zarr` (or `ome_zarr`) to `FileFormatEnum`;
- add slots for the scan step size and the MCA calibration and channel count, which currently live in the OME-Zarr attributes;
- add an XRF mapping technique beyond `xray_microprobe`;
- add a data type for raw MCA spectra.

---

### 4.4 Automatic conversion: the pre-ingest stage of Tegel's pipeline (Prefect)

Conversion is part of the data server's ingest pipeline. Tegel has a plugin type for this: a **converter** (entry-point group `tegel.converters`) turns raw files into records an extractor claims. `xfm-ssrl-etl` registers one, named `xfm`. Tegel's flow `ingest-raw` runs the installed converters on a raw directory, then ingests exactly the records they wrote through the normal pipeline. Tegel does not learn the beamline format; the converter wraps `xfm-etl`.

One command, with no action server:

```bash
uv run tegel-xfm --auth dev --records /data/xfm/crates --output ~/xfm-run \
    --watch /data/beamline --reader <reader>
```

| Process | Address | Log |
| --- | --- | --- |
| data server | :8000 | console |
| Prefect server, with its UI (unless `PREFECT_API_URL` is already set, e.g. the stack's Prefect) | <http://127.0.0.1:4200> (`--prefect-port`) | `~/xfm-run/prefect.log` |
| Tegel catalog updater (`update-catalog/catalog-updater`, the single catalog writer) | — | `~/xfm-run/catalog-updater.log` |
| Tegel ingest worker: `ingest-raw/ingest-raw` every `--interval` seconds (default 30), plus `ingest-directories` | — | `~/xfm-run/ingest-raw.log` |

**One pass of `ingest-raw`:**
1. task `find-ready-raw-files`: files under the raw directory that the converter's patterns match, unchanged for `--stable` seconds (default 60), and not in the ledger `~/xfm-run/preingest/ledger.json`;
2. one task `convert <file>` per unit, retried once: the converter writes a scan crate into `--records` (study from `--study`, else the first folder under the raw directory);
3. the new crates are staged and handed to the catalog updater, then the data server is refreshed.

The new scans are searchable at once. A pass that finds nothing is a short completed run. Passes that did something publish two artifacts:
- `ingest-raw-converted`: the raw files and the crate each became;
- `ingest-raw-summary`: counts, failures, staging errors.

**Rules:**
- A file that changes after conversion is logged, not reconverted.
- A failed file is retried once it changes.
- Hidden folders are skipped.
- Files that no converter's patterns match (`xfm-etl readers` lists them) are ignored.

**Configuration.** The launcher sets these; set them yourself when running the workers directly:

| Variable | Meaning |
| --- | --- |
| `TEGEL_RAW` | raw directories (absolute, `os.pathsep`-separated) |
| `TEGEL_RECORDS`, `TEGEL_OUTPUT` | where crates go; where the catalog, staging and ledger live |
| `XFM_ETL_READER` | the reader the `xfm` converter uses. Required: without it the converter refuses to load, so installing the package never converts files with the wrong reader. |
| `TEGEL_CONVERTERS_CONFIG` | YAML with an `xfm:` entry: `study`, `sample_sheet` or `samples`, `layout` |
| `TEGEL_RAW_STABLE` | seconds a file must be unchanged |
| `TEGEL_REFRESH_URL` + `TEGEL_REFRESH_TOKEN` (or OIDC client settings), `PREFECT_API_URL`, `TEGEL_PROFILE`, `TEGEL_SCHEMA_ROOT` | as for the rest of the pipeline |

```bash
python -m tegel.actions.update                     # the catalog updater (one per catalog)
python -m tegel.actions.ingest raw --interval 30   # serves ingest-raw on a schedule, plus ingest-directories
```

**In the production stack** (`tegel-base/deploy`), run the `etl` worker with `raw --interval N` and set `TEGEL_RAW` and `XFM_ETL_READER`. The stack's catalog updater and Prefect are used as they are.

**Without Tegel**, `xfm-etl watch /data/beamline --reader <reader> --out /data/xfm/crates` applies the same rules. It only writes crates, and the data server picks them up at its next start.

## 5. Serving raw scans (data server)

### 5.1 Run it

```bash
uv run tegel-xfm --auth dev --records /data/xfm/crates --output ~/xfm-run
```

- The server claims every crate under `--records`: a `ro-crate-metadata.json` that conforms to the raw-scan profile and has an `aims-leaf.json` beside it. Everything else is ignored.
- `--output` holds `catalog.db`, `token`, `errors.jsonl`, `dev-issuer.env` (dev mode) and `credentials` (Keycloak mode). `.` is allowed only for an empty current directory.
- Every start rebuilds the catalog from scratch. Restart to pick up new crates.
- `--readable DIR` adds directories Tiled may read from, if crates link to data elsewhere.

### 5.2 Record fields

The record is the flat class `XfmRawScan` in `schemas/xfm/raw.yaml`. Every slot is a projection of the aims-leaf record. Its `aims_leaf` annotation names the source path, and slots with no aims-leaf home carry `aims_leaf_gap` instead. A test checks that every slot has exactly one of the two.

| Field | From |
| --- | --- |
| `id` | the crate's TypeID (`./` `identifier`, which equals the folder name) |
| `scan_name` | `experiment_runs[0].experiment_code` |
| `sample_code` | `samples[0]` or `plantsamples[0]` `.sample_code` |
| `study` | `studies[0].title` |
| `technique`, `beamline`, `experiment_date`, `processing_status`, `raw_data_location` | `experiment_runs[0]` |
| `facility` | `instruments[0].facility_name` |
| `energy_kev`, `dwell_time_ms`, `beam_size_um` | `experiment_runs[0]` `energy` / `dwell_time` / `beam_size_x`, `numeric_value`; the unit is checked |
| `map_shape`, `mca_channels`, `step_size_um` | OME-Zarr level 0 shape and `xfm` attributes (aims-leaf gaps) |

Search fields (exact match): `sample_code`, `study`, `beamline`, `energy_kev`, `facility`.

### 5.3 Parts

| Part | Family | Content |
| --- | --- | --- |
| `mca-0`, `mca-1`, … | array, served by Tiled | the OME-Zarr levels `(c, y, x)` |
| `I0`, other scalers | array, Tiled | `(y, x)` |
| `total_counts` | array, Tiled, **designated** | `(y, x)` quicklook |
| `aims-leaf` | bytes | the aims-leaf record |
| payload | bytes | `ro-crate-metadata.json` |

Only array headers are read at staging; chunks are read when a client asks for a slice.

### 5.4 Ids

Ids are [TypeIDs](https://github.com/jetify-com/typeid) with prefix `xfm`. The ETL mints them from a UUIDv7, so they sort by conversion time. The crate folder is named by the id, and the readable name stays in `scan_name`.

A crate without a valid TypeID still gets a stable id derived from its folder name (a UUIDv5 TypeID), but crates written by the ETL always carry one.

### 5.5 Bad records

A record that fails is skipped and logged. `--on-error fail` stops at the first one instead.

```bash
uv run python -m tegel.actions.errors --file ~/xfm-run/errors.jsonl --last
```

| Stage | Meaning |
| --- | --- |
| `process` | the crate or its aims-leaf record could not be read, or a quantity has the wrong unit |
| `check` | the projected record does not match the schema |
| `parts` | an array is missing or unreadable by Tiled, or lies outside readable storage |
| `duplicate` | two crates with the same id |

The viewer's `--stage` filter does not accept `parts` yet (a bug in `tegel`), so use `grep '"parts"'` on the log for those.

---

## 6. Querying the data server

```bash
T=$(cat ~/xfm-run/token); H="Authorization: Bearer $T"; S=http://127.0.0.1:8000

curl -H "$H" $S/capabilities
curl -H "$H" $S/resources                               # all ids
curl -H "$H" "$S/search?study=aapl&beamline=BL2-3"
ID=xfm_...                                              # an id from /resources
curl -H "$H" $S/resources/$ID/metadata
curl -H "$H" $S/resources/$ID/data                      # parts with content_url

# channel 640 of the full-resolution cube (one image plane), as JSON
curl -H "$H" "$S/api/v1/array/full/$ID/mca-0?slice=640,:,:&format=application/json"
# the spectrum of one pixel, as raw bytes
curl -H "$H" -o spectrum.bin "$S/api/v1/array/full/$ID/mca-0?slice=:,48,60&format=application/octet-stream"
# the quicklook map
curl -H "$H" "$S/api/v1/array/full/$ID/total_counts?format=application/json"
# the aims-leaf record
curl -H "$H" $S/resources/$ID/data/aims-leaf/content
```

From Python:

```python
from pathlib import Path

import numpy as np

from tegel.core.client.data import DataClient

token = Path("~/xfm-run/token").expanduser().read_text().strip()
with DataClient("http://127.0.0.1:8000", token=token) as client:
    rid = client.search({"study": "aapl"}).hits[0].identifier
    for part in client.parts(rid):
        print(part.name, part.shape)
    raw = client.read_array(rid, "mca-0", slice=":,48,60")      # one pixel's spectrum
    spectrum = np.frombuffer(raw.data, dtype=raw.data_type)
```

`DataClient` returns the bytes of the slice you asked for. `raw.shape` is the full array shape, not the shape of the slice.

> Known gap: Tiled's own Python client (`tiled.client.from_uri`) does not work against this server yet, because it starts the Keycloak login instead of using the bearer token. Use `DataClient` or HTTP.

---

## 7. Processing (action server)

Processing jobs are actions on a separate Tegel action server, which can run anywhere. The actions live in `compute/` (package `tegel-xfm-compute`):

| Action | Input | Parameters | Outputs |
| --- | --- | --- | --- |
| `xfm.roi_maps` | `mca`: an MCA cube `(c, y, x)`, e.g. a scan's `mca-0` URL on the data server | `elements` (`"S,Cl,K,Ca,Mn,Fe,Ni,Cu,Zn"`), `gain_ev` (10), `offset_ev` (0), `window_ev` (150) | `maps` `(element, y, x)`, `rois` table |
| `xfm.import_processed` | `crate`: a legacy `ProcessedCache/<measurement>/ro-crate-metadata.json` (`file:` URI) | — | `clr`, `mask`, `elements`, `record` |

`xfm.import_processed` brings the existing ProcessedCache products over to the action server, now that the data server serves only raw scans. The MCA fit and the CLR normalization become actions next, once their code is in hand.

### 7.1 Trying it locally next to a dev-mode data server

In dev mode `tegel-xfm` writes `run/dev-issuer.env`. An action server that loads it trusts the same token, which carries the scopes `actions:read` and `actions:run`. The action server then forwards that token to the data server when it reads inputs.

```bash
cd ~/tegelwerk/xfm_ssrl_tegel
set -a; . ~/xfm-run/dev-issuer.env; set +a
export TEGEL_PUBLIC_ORIGIN=http://127.0.0.1:8010
export TEGEL_RESULTS_DIR=~/xfm-actions
export TEGEL_FORWARD_ORIGINS=http://127.0.0.1:8000       # send the caller's token to the data server
export TEGEL_READABLE_STORAGE=/data/xfm                  # where file: inputs may come from
export TEGEL_SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
uv run --package tegel-xfm-compute tegel-actionserver serve
```

### 7.2 Running a job

```bash
T=$(cat ~/xfm-run/token); H="Authorization: Bearer $T"; A=http://127.0.0.1:8010
curl -H "$H" $A/actions                                   # cards: inputs, parameters, outputs

MCA=$(curl -s -H "$H" http://127.0.0.1:8000/resources/$ID/data \
      | python3 -c "import json,sys; print([p for p in json.load(sys.stdin)['parts'] if p['name']=='mca-0'][0]['content_url'])")
RUN=$(curl -s -H "$H" -H 'Content-Type: application/json' \
      -d "{\"action\": \"xfm.roi_maps\", \"inputs\": {\"mca\": \"$MCA\"}, \"parameters\": {\"elements\": \"K,Ca,Fe,Zn\"}}" \
      $A/runs | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
curl -X POST -H "$H" "$A/runs/$RUN/wait?timeout=120"     # state, result URI
curl -H "$H" $A/resources/$RUN                            # record; relations: used -> the mca-0 URL
curl -H "$H" "$A/api/v1/array/full/$RUN/maps?slice=2,:,:&format=application/json"   # the Fe map
```

Results are served with the same read API as the data server. `DataClient("http://127.0.0.1:8010", token=...)` reads them the same way.

> Keycloak mode: the realm `tegel-xfm` creates does not yet issue the `actions:*` scopes, so with Keycloak the action server needs those client scopes added to the realm. Untested.

---

## 8. Using the dashboard

```bash
uv run tegel-xfm-dash --token-file ~/xfm-run/token        # --server http://127.0.0.1:8000 by default
```

- Search by **Sample code, Study, Beamline, Energy (keV), Facility**, or list everything.
- Click a scan to see its designated array (`total_counts`).
- **Array** switches between the scan's array parts (`mca-0`, `mca-1`, `I0`, …).
- **Channel** picks the plane along the first axis (the MCA channel of an OME-Zarr cube).

Images are scaled between the 1st and 99th percentile, and NaNs are dropped.

JSON API: `GET /api/search?study=aapl`, `GET /api/resources/{id}`, `GET /api/resources/{id}/image?part=mca-0&channel=640`.

The dashboard reads its token once, at startup: restart it whenever the data server restarts. It has no login of its own, so keep it on `127.0.0.1` or behind a tunnel.

---

## 9. Running on different machines

Every piece can run on its own machine. A client needs network access to the server and a valid token. The client's server URL must be exactly the server's public origin (`--origin` on `tegel-xfm`, `TEGEL_PUBLIC_ORIGIN` on the action server). That origin is written into every `content_url`, and the token is only sent there. On a mismatch, search works but data reads fail with `401`.

The simplest setup leaves every server on `127.0.0.1` and reaches it through an SSH tunnel:

```bash
scp user@data-host:xfm-run/token ~/xfm-dash/token && chmod 600 ~/xfm-dash/token
ssh -N -L 8000:127.0.0.1:8000 user@data-host            # add -L 8010:127.0.0.1:8010 for the action server
uv run tegel-xfm-dash --token-file ~/xfm-dash/token
```

For direct network access, start the data server with `--host 0.0.0.0 --origin http://data-host.lbl.gov:8000` and use exactly that URL as the client's `--server`. Do not expose a dev-mode server on a network you don't control.

The action server reads from the data server over HTTP too. Put the data server's origin in `TEGEL_FORWARD_ORIGINS`, and the caller's token is forwarded.

In dev mode every data-server restart creates a new signing key. The old token, and any action server that loaded the old `dev-issuer.env`, stop working: re-copy the token and restart the clients and the action server.

---

## 10. Authentication modes

- **`--auth dev`** (local): an issuer inside `tegel-xfm` signs one token, written to `<output>/token`, with read and `actions:*` scopes. It lasts `--token-hours` (default 12). `dev-issuer.env` lets an action server trust it.
- **`--auth keycloak`** (default; untested end to end):
  - starts `quay.io/keycloak/keycloak:26.4.0` in podman on `127.0.0.1:8080`;
  - creates the realm `tegel`, the client `tegel` and the user `reader`;
  - writes `credentials` and a reader token into the output directory;
  - the dashboard refreshes its token from `credentials`;
  - to reset, run `podman rm -f tegel-keycloak` and delete `credentials`.

---

## 11. Production: the edge deployment

The reference stack in `tegel/tegel-base/deploy` (Caddy, Keycloak, Prefect ingest, incremental catalog updates) builds its image from this repository's `Containerfile`. Set:

| Variable | Value |
| --- | --- |
| `TEGEL_PROFILE` | `xfm_ssrl_tegel/dataserver/schema/profile.yaml` |
| `TEGEL_SCHEMA_ROOT` | `xfm_ssrl_tegel/schemas` |
| `TEGEL_RECORDS` | the crate directory |

The image also contains `xfm-ssrl-etl`, so the `xfm` converter is installed in the pipeline workers (section 4.4). It installs the exact versions in `uv.lock` via `deploy/constraints.txt`; run `deploy/export-constraints.sh` after changing the lock, and a test fails if they disagree.

The image is built and tested: in the container, the ETL writes a crate and the plugin stages it. The full stack has not been run yet.

---

## 12. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Claimed 0 record(s)` | No crate conforms to the raw-scan profile, or `aims-leaf.json` is missing next to `ro-crate-metadata.json`. Convert with `xfm-etl` (section 4). |
| `process` error mentioning a unit | An aims-leaf quantity is not in the unit the slot expects (keV, ms, um). |
| `401` on every request | Missing or expired token; in dev mode the server issues a new one at each start. |
| Remote client: search works, data reads fail with `401` | The client's server URL differs from the server's public origin (section 9). |
| Action run fails reading its input | The data server's origin is not in `TEGEL_FORWARD_ORIGINS`, the token has expired, or a `file:` input lies outside `TEGEL_READABLE_STORAGE`. |
| `400 Unknown search field` | Only the five search fields in 5.2 can be searched. |
| `port … already in use` | Pick another `--port` / `TEGEL_PORT` and point the clients at it. |
| New crates don't appear | The catalog is built only at startup; restart `tegel-xfm`. |

---

## 13. Where things live

| Path | Contents |
| --- | --- |
| `etl/src/xfm_etl/write.py` | OME-Zarr, aims-leaf and RO-Crate writer |
| `etl/src/xfm_etl/readers/` | beamline readers (`synthetic` so far): `read`, file `patterns`, scan `group`ing |
| `etl/src/xfm_etl/watch.py` | ready-file detection and the ledger; the standalone `xfm-etl watch` |
| `etl/src/xfm_etl/converter.py` | Tegel's `xfm` converter plugin (pre-ingest) |
| `etl/src/xfm_etl/layout.py` | the output outline, `index.csv`, `README.md` |
| `schemas/xfm/raw.yaml` | LinkML class `XfmRawScan`, with aims-leaf mappings and gaps |
| `dataserver/schema/profile.yaml` | search fields, displayed properties, id field |
| `dataserver/src/tegel/xfm/dataserver/__init__.py` | extractor (aims-leaf → record), reader (parts), resource type |
| `dataserver/tests/fixtures/xfm_…/` | a small ETL-written crate; the committed positive fixture |
| `actions/src/tegel/xfm/actions/serve.py` | the `tegel-xfm` launcher, including `--watch` |
| `compute/src/tegel/xfm/compute/` | processing actions (separate action server) |
| `dash/src/tegel/xfm/dash/app.py` | the dashboard |

### Adding a search field

1. Add the slot to `schemas/xfm/raw.yaml` with `range: string` and an `aims_leaf` (or `aims_leaf_gap`) annotation.
2. Fill it in `project()` in the plugin and add it to `FIELDS`.
3. Add it to `search:` and `properties:` in `profile.yaml`. Bump `schema_version` and `RECORD.version` if the change is incompatible.
4. Add it to the dashboard's `FIELDS`/`COLUMNS` if you want it on the page.
5. Run `uv run pytest`.
