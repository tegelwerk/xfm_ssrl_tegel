# tegel-xfm-dash

Search the SSRL XFM data server, open a measurement, and show its image. The pixels come from Tiled's array route.

```bash
uv run tegel-xfm-dash --token-file /path/to/run/token
```

The dashboard listens on `http://127.0.0.1:8001`. The data server defaults to `http://127.0.0.1:8000`. `--server` selects another one. The token file is the bearer token written by `tegel-xfm`. When `credentials` sits beside that file, an expired reader token is replaced from Keycloak.
