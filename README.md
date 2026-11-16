# xfm_ssrl_tegel

SSRL XFM incarnation of [Tegel](https://github.com/tegelwerk/tegel), on spec 0.4.0.

- `dataserver` — the processed-measurement plugin (extractor, reader, resource type). The reference server loads it by entry point.
- `schemas/xfm/processed.yaml` — the LinkML schema the profile names. It lives outside the plugin package, as the spec requires; point `TEGEL_SCHEMA_ROOT` at `schemas/`.
- `actions` — `tegel-xfm`, which stages a directory of records and serves the catalog.
- `dash` — a search page that previews the designated array of a measurement.

`tegel-core`, `tegel-register`, the reference data server and the base actions are path sources under `../tegel`.

```bash
uv sync --group dev
uv run pytest
uv run tegel-xfm --auth dev --records /path/to/ProcessedCache/.. --output run
uv run tegel-xfm-dash --server http://127.0.0.1:8000 --token-file run/token
```

Then:

```bash
T=$(cat run/token)
curl -H "Authorization: Bearer $T" "http://127.0.0.1:8000/search?campaign=aapl"
curl -H "Authorization: Bearer $T" "http://127.0.0.1:8000/resources/<measurement_id>/data"
curl -H "Authorization: Bearer $T" \
  "http://127.0.0.1:8000/api/v1/array/full/<measurement_id>/masked-clr.zarr-clr?slice=:,:,0&format=application/json"
```
