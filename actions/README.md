# tegel-xfm-actions

The data-server side of the SSRL XFM incarnation:

- the local stage-and-serve command `tegel-xfm`;
- the catalog action `xfm.etl` (`tegel.actions` entry point). It runs on an action server co-located with the data server, converts beamline files into scan crates, merges them into the live catalog, and refreshes the data server. `tegel-xfm --action-server --beamline DIR` starts that action server too.

Stage and serve:

```bash
uv run tegel-xfm --auth dev --records /path/to/crates --output run
```

It claims every scan crate (written by `xfm-etl`) under `--records` with the installed extractors, stages them with `tegel.actions.staging.write_staging` (skip-and-log; `--on-error fail` to stop), merges into a fresh `run/catalog.db`, and serves it with the reference data server. Bad records go to `run/errors.jsonl`.

- `--auth dev` signs a bearer token with an in-process issuer (no Keycloak, no podman) and writes it to `run/token`, with the read and `actions:*` scopes. `run/dev-issuer.env` lets an action server trust the same token. Local use only.
- `--auth keycloak` (default) starts Keycloak in podman, configures realm `tegel`, and fetches a reader token.
- `--readable DIR` adds directories Tiled may read assets from (records are always readable). `--host`, `--port`, `--origin` set where it listens and the public origin.
- `--profile` and `--schema-root` default to `dataserver/schema/profile.yaml` and `schemas/` in this repository.
