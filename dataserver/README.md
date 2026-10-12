# Processed measurement plugin

This is the dataserver package of the SSRL XFM incarnation. Install it and the reference server loads it through entry points. It does not copy the Personalized Data Server stack, its secrets, or the measurement bytes.

The plugin claims two inputs. A JSON record whose `cache` is `ProcessedCache`, and `ro-crate-metadata.json` inside a `ProcessedCache` directory. For a crate it reads `lambda-record.yaml` beside that file and writes one catalog record per crate. `process` copies attributes from `schema/measurement.yaml`. The reader returns shape and chunk shape from the designated store's `zarr.json` and does not open chunk files.

Search fields are `sample_code`, `campaign`, `cache`, `kind`, and `beam_energy_kev`.

```bash
uv sync
uv run pytest
./start_all.sh --output run
```

`./start_all.sh` starts Keycloak, runs this plugin over the JSON records in `tests/fixtures`, writes the catalog database into the output directory, and serves it at `http://127.0.0.1:8000`. `--records` selects another directory of JSON records. `--output` selects where the catalog, credentials, and token are written. `.` is accepted only when the current directory is empty.

```bash
curl -H "Authorization: Bearer $(cat run/token)" \
  "http://127.0.0.1:8000/search?sample_code=AAPL-P1-leaf1"
```
