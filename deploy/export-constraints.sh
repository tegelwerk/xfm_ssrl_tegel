#!/usr/bin/env bash
# Write deploy/constraints.txt from uv.lock. The Containerfile installs with these pins.
set -euo pipefail
cd "$(dirname "$0")/.."
{
  echo "# Pinned versions for the container image, exported from uv.lock."
  echo "# Regenerate with deploy/export-constraints.sh after changing uv.lock; a test checks they agree."
  uv export --frozen --no-hashes --all-groups --no-emit-workspace --no-emit-local --no-header --no-annotate --prune aims-leaf
} > deploy/constraints.txt
