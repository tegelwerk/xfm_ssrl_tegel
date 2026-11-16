#!/usr/bin/env bash
# Stage processed records with the plugin and serve them. Pass --auth dev to skip Keycloak.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run tegel-xfm "$@"
