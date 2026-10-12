# xfm_ssrl_tegel

SSRL XFM incarnation of Tegel, split out of the reference repository.

`dataserver` is the processed-measurement plugin. The reference server loads it through entry points. `dash` and `actions` are the dashboard and action packages for this incarnation.

`tegel-core` is the path source `../tegel/tegel-core`. The reference data server is `../tegel/tegel-base/packages/dataserver`.

```bash
uv sync
uv run pytest
./dataserver/start_all.sh --output run
```

`./dataserver/start_all.sh` changes to the dataserver package, starts Keycloak, catalogs the JSON records in `dataserver/tests/fixtures`, and serves them at `http://127.0.0.1:8000`.
