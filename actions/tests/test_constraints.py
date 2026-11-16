"""deploy/constraints.txt must match uv.lock, so the image runs the versions the tests ran."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_constraints_match_the_lock() -> None:
    exported = subprocess.run(
        ["uv", "export", "--frozen", "--no-hashes", "--all-groups", "--no-emit-workspace",
         "--no-emit-local", "--no-header", "--no-annotate"],
        cwd=REPO, check=True, capture_output=True, text=True,
    ).stdout
    committed = (REPO / "deploy" / "constraints.txt").read_text(encoding="utf-8")
    pins = [line for line in committed.splitlines() if line and not line.startswith("#")]
    assert pins == exported.splitlines(), "run deploy/export-constraints.sh"


def test_containerfile_installs_with_the_constraints() -> None:
    text = (REPO / "Containerfile").read_text(encoding="utf-8")
    assert "--constraint /src/constraints.txt" in text
    assert "COPY xfm_ssrl_tegel/deploy/constraints.txt" in text
