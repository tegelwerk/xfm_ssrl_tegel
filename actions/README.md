# tegel-xfm-actions

The data-server side of the SSRL XFM incarnation:

- the local stage-and-serve command `tegel-xfm`;
- `tegel-xfm --watch DIR --reader R` also runs Tegel's own `ingest-raw` pipeline (with a local Prefect server and the catalog updater) on a raw directory. The `xfm` converter plugin, registered by `xfm-ssrl-etl`, does the conversion.

Stage and serve:

```bash
uv run tegel-xfm --auth dev --records /path/to/crates --output run
```

It claims every scan crate (written by `xfm-etl`) under `--records` with the installed extractors, stages them with `tegel.actions.staging.write_staging` (skip-and-log; `--on-error fail` to stop), merges into a fresh `run/catalog.db`, and serves it with the reference data server. Bad records go to `run/errors.jsonl`.

- `--auth dev` signs a bearer token with an in-process issuer (no Keycloak, no podman) and writes it to `run/token`. Local use only.
- `--auth keycloak` (default) starts Keycloak in podman, configures realm `tegel`, and fetches a reader token.
- `--readable DIR` adds directories Tiled may read assets from (records are always readable). `--host`, `--port`, `--origin` set where it listens and the public origin.
- `--profile` and `--schema-root` default to `dataserver/schema/profile.yaml` and `schemas/` in this repository.
