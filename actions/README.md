# tegel-xfm-actions

Actions for the SSRL XFM incarnation. For now this is the local stage-and-serve command `tegel-xfm`:

```bash
uv run tegel-xfm --auth dev --records /path/to/caches --output run
```

It claims every record under `--records` with the installed extractors, stages them with `tegel.actions.staging.write_staging` (skip-and-log; `--on-error fail` to stop), merges into a fresh `run/catalog.db`, and serves it with the reference data server. Bad records go to `run/errors.jsonl`.

- `--auth dev` signs a bearer token with an in-process issuer (no Keycloak, no podman) and writes it to `run/token`. Local use only.
- `--auth keycloak` (default) starts Keycloak in podman, configures realm `tegel`, and fetches a reader token.
- `--readable DIR` adds directories Tiled may read assets from (records are always readable). `--host`, `--port`, `--origin` set where it listens and the public origin.
- `--profile` and `--schema-root` default to `dataserver/schema/profile.yaml` and `schemas/` in this repository.
