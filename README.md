# xfm_ssrl_tegel

The SSRL X-ray fluorescence microscopy (XFM) incarnation of [Tegel](https://github.com/tegelwerk/tegel), on spec 0.4.0. It aligns with the [aims-leaf](https://github.com/lambda-ber/aims-leaf) schema.

```
beamline files ──ETL──► scan crates ──► data server ──┬─► dashboard
                       (OME-Zarr +                     ├─► curl / Python
                        aims-leaf)    action server ◄──┘ (processing jobs; results record what they used)
```

This repository holds four independent components. They share no code, configuration or files. Between the servers and their clients, the only contract is the Tegel read API plus a bearer token. The ETL hands crates to the data server.

| | **ETL** (Part I) | **Data server** (Part II) | **XFM actions** (Part III) | **Dashboard** (Part IV) |
| --- | --- | --- | --- | --- |
| Purpose | Beamline files → OME-Zarr scan crates with an aims-leaf record | Catalogs raw scans and serves metadata and sliceable arrays | Processing jobs on a Tegel action server | Search page and image preview |
| Command | `xfm-etl` | `tegel-xfm` | `tegel-actionserver` (+ this package) | `tegel-xfm-dash` |
| Package | `etl/` (`xfm-ssrl-etl`), no Tegel import; also Tegel's `xfm` converter plugin | `dataserver/` (plugin), `actions/` (launcher), `schemas/` | `compute/` (`tegel-xfm-compute`) | `dash/` (`tegel-xfm-dash`) |
| Runs on | where the beamline files are | where the crates are | anywhere | anywhere |
| Default port | — | 8000 | 8010 | 8001 |

Raw data lives on the data server, and every processed product is an action-server result. **Full operator walkthrough: [docs/MANUAL.md](docs/MANUAL.md).**

All components assume the two checkouts sit side by side:

```bash
mkdir -p ~/tegelwerk && cd ~/tegelwerk
git clone https://github.com/tegelwerk/tegel
git clone https://github.com/tegelwerk/xfm_ssrl_tegel
cd xfm_ssrl_tegel
```

---

# Part I — ETL: beamline files to OME-Zarr scan crates

```bash
uv sync --package xfm-ssrl-etl
uv run xfm-etl readers
uv run xfm-etl convert --reader synthetic AAPL_10serp_P1_leaf1 --out /data/xfm/crates --study aapl
```

The output directory has a fixed outline, one crate per raster scan:

```
/data/xfm/crates/
├── README.md, index.csv                one row per scan: id, study, sample, scan, path, raw sources, …
└── <study>/<sample_code>/xfm_<typeid>/ the TypeID (UUIDv7) is the unique name
    ├── ro-crate-metadata.json          RO-Crate 1.1, conformsTo https://w3id.org/tegel/xfm/raw-scan/0.1
    ├── aims-leaf.json                  aims-leaf Dataset: ExperimentRun, Sample/PlantSample, Instrument, Study, DataFiles
    └── scan.ome.zarr/                  OME-Zarr 0.5: MCA cube (c, y, x) + 2x pyramid, scalers/, quicklook/total_counts
```

**Automatic, monitored conversion (Prefect).**

```bash
uv run tegel-xfm --auth dev --records /data/xfm/crates --output run \
    --watch /data/beamline --reader <reader>
```

The conversion is a pre-ingest step of Tegel's own ingest pipeline. `xfm-ssrl-etl` registers a **converter plugin** (`tegel.converters` entry point `xfm`). The command above starts:
- a local Prefect server, with its UI at <http://127.0.0.1:4200>, unless `PREFECT_API_URL` points at one;
- Tegel's catalog updater;
- Tegel's `ingest-raw` flow, scheduled every `--interval` seconds.

Each pass:
1. finds raw files that have stopped changing;
2. converts them with the XFM converter, one task run per unit, retried once;
3. ingests exactly the new crates through the normal pipeline: staging, the single catalog writer, and a data-server refresh.

No action server is involved. The first folder under the raw directory names the study, and a ledger makes sure nothing is converted twice. `xfm-etl watch` applies the same rules without Prefect or Tegel and writes crates only (manual §4.4).

- The beamline files are only read.
- `--sample-sheet samples.yaml` adds aims-leaf `PlantSample` fields per sample code.
- The output is checked against the official OME-Zarr 0.5 models and the aims-leaf pydantic models. Apart from `file_format: zarr`, which aims-leaf's enum lacks, the record validates.

**Status:** only the `synthetic` reader exists. The SSRL beamline-format reader needs a real raw file first (manual §4.2). Details are in [etl/README.md](etl/README.md).

---

# Part II — Data server (raw scans)

```bash
uv sync --package tegel-xfm-actions
uv run --package tegel-xfm-actions tegel-xfm --auth dev --records /data/xfm/crates --output run
```

At startup the server claims every scan crate under `--records`, builds `run/catalog.db` from scratch, and listens on `http://127.0.0.1:8000`. It writes:
- `run/token`: the bearer token;
- `run/errors.jsonl`: skipped records;
- in dev mode, `run/dev-issuer.env`, which lets another Tegel service on this machine trust the same token.

| Option | Default | Meaning |
| --- | --- | --- |
| `--records DIR` | the test fixture | directory searched for scan crates |
| `--output DIR` | `.` (must be empty) | catalog, token, error log |
| `--auth dev\|keycloak` | `keycloak` | `dev`: in-process issuer, local only. `keycloak`: Keycloak in podman |
| `--host`, `--port`, `--origin` | `127.0.0.1`, `8000`, `http://<host>:<port>` | where to listen; the public address written into every `content_url` |
| `--readable DIR` | | extra directories Tiled may read from |
| `--on-error skip\|fail` | `skip` | skip and log bad records, or stop |
| `--watch DIR --reader R` | | run Tegel's `ingest-raw` on that raw directory with the XFM converter (reader `R`), every `--interval` s (30); files must be unchanged for `--stable` s (60); `--study`, `--sample-sheet` |
| `--prefect-port` | 4200 | port of the local Prefect server `--watch` starts when `PREFECT_API_URL` is unset |

**Record.** The flat class `XfmRawScan` (`schemas/xfm/raw.yaml`) is a projection of the crate's aims-leaf record. Each slot names its aims-leaf source path, or is marked as a gap. Search fields: `sample_code`, `study`, `beamline`, `energy_kev`, `facility`.

**Parts:**
- `mca-0`, `mca-1`, …: the OME-Zarr levels, sliceable through Tiled;
- `I0` and the other scalers;
- `total_counts`: the designated part, used for previews;
- `aims-leaf`: the record.

```bash
T=$(cat run/token); H="Authorization: Bearer $T"; S=http://127.0.0.1:8000
curl -H "$H" "$S/search?study=aapl"
curl -H "$H" $S/resources/<id>/data
curl -H "$H" "$S/api/v1/array/full/<id>/mca-0?slice=640,:,:&format=application/json"    # one channel
curl -H "$H" "$S/api/v1/array/full/<id>/mca-0?slice=:,48,60&format=application/json"    # one spectrum
```

**Production.** `Containerfile` builds the image for the edge deployment in `tegel/tegel-base/deploy`. The image contains the data server, the pipeline workers and `xfm-ssrl-etl` (so the `xfm` converter is installed), and is pinned to `uv.lock` through `deploy/constraints.txt`. To convert raw data there, run the `etl` worker as `python -m tegel.actions.ingest raw --interval 60` with `TEGEL_RAW` and `XFM_ETL_READER` set.

---

# Part III — XFM actions (processing, on a separate action server)

Processing lives on its own Tegel action server, which can run anywhere and needs no access to the data server's disk. Install the action server together with this package:

```bash
uv sync --package tegel-xfm-compute
set -a; . run/dev-issuer.env; set +a                  # trust the data server's dev token
export TEGEL_PUBLIC_ORIGIN=http://127.0.0.1:8010 TEGEL_RESULTS_DIR=$PWD/actions-run \
       TEGEL_FORWARD_ORIGINS=http://127.0.0.1:8000 \
       TEGEL_SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
uv run --package tegel-xfm-compute tegel-actionserver serve
```

| Action | Input | Outputs |
| --- | --- | --- |
| `xfm.roi_maps` | `mca`: a scan's `mca-0` URL on the data server | `maps` `(element, y, x)`, `rois` |
| `xfm.import_processed` | a legacy `ProcessedCache/<measurement>/ro-crate-metadata.json` | `clr`, `mask`, `elements`, `record` |

```bash
curl -H "$H" -H 'Content-Type: application/json' \
  -d '{"action": "xfm.roi_maps", "inputs": {"mca": "http://127.0.0.1:8000/api/v1/array/full/<id>/mca-0"},
       "parameters": {"elements": "K,Ca,Fe,Zn"}}' http://127.0.0.1:8010/runs
```

The action server reads the input from the data server with the caller's token. It stores the run as a result whose relations record `used` → the input URL, and serves it with the same read API.

---

# Part IV — Dashboard

The dashboard needs only a data server URL and a token. To install it without a checkout:

```bash
uv tool install --no-sources \
  --with "tegel-core @ git+https://github.com/tegelwerk/tegel#subdirectory=tegel-core" \
  "tegel-xfm-dash @ git+https://github.com/tegelwerk/xfm_ssrl_tegel#subdirectory=dash"
tegel-xfm-dash --server http://127.0.0.1:8000 --token-file ~/xfm-dash/token
```

From a checkout, use `uv sync --package tegel-xfm-dash`. Then open <http://127.0.0.1:8001>.

- Search by sample code, study, beamline, energy or facility.
- A scan opens on its total-counts map.
- **Array** switches parts (`mca-0`, `I0`, …).
- **Channel** picks the MCA channel.

| Option | Default | Meaning |
| --- | --- | --- |
| `--server URL` | `http://127.0.0.1:8000` | must equal the data server's public origin |
| `--token-file PATH` | required | read once at startup |
| `--issuer URL` | `http://127.0.0.1:8080/realms/tegel` | Keycloak, used only when `credentials` sits next to the token |
| `--host`, `--port` | `127.0.0.1`, `8001` | where the dashboard listens |

On another machine, copy the token (`scp`), and either tunnel (`ssh -N -L 8000:127.0.0.1:8000 user@data-host`) or point `--server` at the server's `--origin`. Dev tokens change at every data-server restart. The dashboard has no login of its own, so keep it on `127.0.0.1` or behind a tunnel (manual §8–9).

---

# Part V — Developing in this repository

The repository is a uv workspace with five members: `etl`, `dataserver`, `actions`, `compute` and `dash`.

```bash
uv sync --group dev
uv run pytest            # ETL (OME-Zarr + aims-leaf validation), plugin, conformance kit,
                         # end-to-end serve, Tegel ingest-raw with the XFM converter,
                         # processing actions direct and through an action server, dashboard
uv run ruff check .
```

`dataserver/tests/fixtures/xfm_…/` is a small ETL-written crate, committed as the positive fixture. `xfm_etl.readers.synthetic.scan()` makes realistic test scans.

## Known gaps

- **SSRL beamline reader:** not written yet; it needs one real raw scan.
- **aims-leaf change requests:**
  - `zarr` in `FileFormatEnum`;
  - slots for scan step size, MCA calibration and channel count;
  - an XRF mapping technique;
  - a raw-MCA data type.
- **Keycloak:**
  - Keycloak mode and the full edge stack (Keycloak, Caddy, Prefect) have not been run end to end;
  - Keycloak does not yet issue the `actions:*` scopes.
- **Tiled's Python client** (`tiled.client.from_uri`) cannot use the bearer token yet. Use `DataClient` or HTTP.
- **Local catalog rebuild:** the local launcher rebuilds the catalog at every start. The edge deployment ingests incrementally.
