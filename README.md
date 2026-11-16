# xfm_ssrl_tegel

The SSRL X-ray fluorescence microscopy (XFM) incarnation of [Tegel](https://github.com/tegelwerk/tegel), on spec 0.4.0.

It serves processed XFM measurements (RO-Crates under `ProcessedCache/`) as a searchable, read-only catalog. You can slice each measurement's zarr arrays, CSV tables and TIFFs over HTTP through Tiled. A small dashboard lets you search the catalog and preview images.

**→ Full walkthrough: [docs/MANUAL.md](docs/MANUAL.md)**, covering setup, loading your data, the dashboard, curl and Python access, authentication, running the server and dashboard on different machines, and troubleshooting.

## Quick start

```bash
# 1. both repositories side by side
mkdir -p ~/tegelwerk && cd ~/tegelwerk
git clone https://github.com/tegelwerk/tegel
git clone https://github.com/tegelwerk/xfm_ssrl_tegel
cd xfm_ssrl_tegel

# 2. install and verify
uv sync --group dev
uv run pytest -q

# 3. terminal 1: data server (stages your crates, serves on :8000, writes run/token)
uv run tegel-xfm --auth dev --records /path/to/caches --output run

# 4. terminal 2: dashboard on :8001
uv run tegel-xfm-dash --token-file run/token
```

Open <http://127.0.0.1:8001>. From the command line:

```bash
T=$(cat run/token)
curl -H "Authorization: Bearer $T" "http://127.0.0.1:8000/search?campaign=aapl"
curl -H "Authorization: Bearer $T" "http://127.0.0.1:8000/resources/<measurement_id>/data"
curl -H "Authorization: Bearer $T" \
  "http://127.0.0.1:8000/api/v1/array/full/<measurement_id>/masked-clr.zarr-clr?slice=:,:,0&format=application/json"
```

The data server and the dashboard are separate processes and can run on **different machines**. The dashboard needs network access to the server and a copy of `run/token`, and its `--server` must equal the server's public origin (`--origin`). An SSH tunnel is the simplest setup (manual §7).

`--auth dev` needs no Keycloak and is for local use only. Leave it off to run Keycloak in podman (see manual §8).

## Expected data layout

```
/path/to/caches/
└── ProcessedCache/
    └── <measurement_id>/
        ├── ro-crate-metadata.json     root "@id": "./" with sample_code, technique, dateCreated;
        │                              one entity with "isDesignated": true (e.g. masked-clr.zarr/)
        ├── lambda-record.yaml         optional: beam_energy_keV, experiment_runs
        ├── *.zarr/                    every array becomes a sliceable part (v2 or v3)
        └── *.csv, *.tif, *.png, ...   side files become parts too
```

Search fields: `sample_code`, `campaign`, `cache`, `kind`, `beam_energy_kev` (exact match).

## Repository layout

| Path | What |
| --- | --- |
| `dataserver/` | the plugin, package `tegel-xfm`: extractor, reader and resource type, loaded by entry point. Ships no schema, by the Tegel contract. |
| `dataserver/schema/profile.yaml` | search profile: search fields, displayed properties, id field |
| `schemas/xfm/processed.yaml` | LinkML schema `ProcessedMeasurement` v2; point `TEGEL_SCHEMA_ROOT` here |
| `actions/` | `tegel-xfm`: stage records and serve them with the reference data server |
| `dash/` | `tegel-xfm-dash`: search page and image preview |
| `Containerfile` | image used by the edge deployment in `../tegel/tegel-base/deploy` |
| `docs/MANUAL.md` | operator manual |

`tegel-core`, `tegel-register`, the reference data server and the base actions come from `../tegel` as editable path sources.

## Development

```bash
uv run pytest            # unit tests, conformance kit, end-to-end serve test
uv run ruff check .
```

`actions/tests/test_serve.py::write_crate` builds a realistic synthetic crate. Use it as a template for new test data.

## Known gaps

- Keycloak mode and the container build have not been run end to end on this branch.
- Tiled's Python client (`tiled.client.from_uri`) cannot use the bearer token yet. Use `tegel.core.client.data.DataClient` or HTTP (manual §6.3).
- The catalog is rebuilt at every start; there is no incremental ingest locally. The edge deployment has it.
