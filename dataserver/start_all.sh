#!/usr/bin/env bash
# Start Keycloak, catalog processed records with the plugin, and serve them.
set -euo pipefail
cd "$(dirname "$0")"
exec uv run tegel-xfm "$@"
