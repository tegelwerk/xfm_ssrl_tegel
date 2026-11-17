"""Stage new scan crates, merge them into the data server's catalog, refresh it.

Shared by the Prefect flows and the ``xfm.etl`` action. One writer at a time:
the merge holds an exclusive lock on ``<records>/.xfm-etl/catalog.lock``.

In the production stack set ``XFM_CATALOG_UPDATER=1``: the merge then goes through
Tegel's ``update-catalog`` Prefect deployment, the stack's single catalog writer,
instead of the local lock.

Environment (as for the data server): ``TEGEL_RECORDS``, ``TEGEL_CATALOG_URI``
(``sqlite:///...``), ``TEGEL_PROFILE``, ``TEGEL_SCHEMA_ROOT``, and for the refresh
``TEGEL_REFRESH_URL`` with ``TEGEL_REFRESH_TOKEN`` or the OIDC client settings.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx


def records_dir() -> Path:
    value = os.environ.get("TEGEL_RECORDS", "").strip()
    if not value:
        raise RuntimeError("TEGEL_RECORDS (the data server's records directory) is not set")
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def catalog_path() -> Path:
    uri = os.environ.get("TEGEL_CATALOG_URI", "").strip()
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise RuntimeError("TEGEL_CATALOG_URI must name the data server's sqlite catalog")
    return Path(uri[len(prefix):])


@contextmanager
def catalog_lock(records: Path) -> Iterator[None]:
    path = records / ".xfm-etl" / "catalog.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ingest_crates(crates: list[Path], log: logging.Logger | Any) -> dict[str, Any]:
    """Stage ``crates``, merge them into the catalog, and refresh the data server."""
    from tegel.actions.merge import merge_staging
    from tegel.actions.staging import write_staging

    records = records_dir()
    catalog = catalog_path()
    if not crates:
        return {"staged": 0, "skipped": 0, "errors": [], "refreshed": False,
                "catalog": str(catalog)}
    with tempfile.TemporaryDirectory(prefix="xfm-ingest-") as work:
        staging = Path(work) / "staging.db"
        result = write_staging(
            [str(Path(crate) / "ro-crate-metadata.json") for crate in crates],
            staging,
            roots=[records],
            error_log=records / ".xfm-etl" / "errors.jsonl",
        )
        errors = [{"path": e.path, "stage": e.stage, "message": e.message}
                  for e in result.errors]
        for error in errors:
            log.error("not staged: %s (%s): %s", error["path"], error["stage"], error["message"])
        refreshed = False
        if result.staged:
            if os.environ.get("XFM_CATALOG_UPDATER", "").strip() in ("1", "true", "yes"):
                _merge_with_updater(staging, catalog)
            else:
                with catalog_lock(records):
                    merge_staging(str(staging), str(catalog))
            refreshed = refresh(log)
    log.info("staged %d, skipped %d into %s", result.staged, result.skipped, catalog)
    return {"staged": result.staged, "skipped": result.skipped, "errors": errors,
            "refreshed": refreshed, "catalog": str(catalog)}


def _merge_with_updater(staging: Path, catalog: Path) -> None:
    """Hand the staging catalog to the stack's ``update-catalog`` deployment and wait."""
    from prefect.deployments import run_deployment

    from tegel.actions.ingest import UPDATER

    kept = catalog.parent / "staging" / f"xfm-{os.getpid()}-{staging.stat().st_mtime_ns}.db"
    kept.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), kept)
    run = run_deployment(
        name=UPDATER,
        parameters={"staging_path": str(kept), "production_path": str(catalog)},
        as_subflow=False,
    )
    if run.state is None or not run.state.is_completed():
        raise RuntimeError(f"catalog update did not finish: {run.state}")


def refresh(log: logging.Logger | Any) -> bool:
    url = os.environ.get("TEGEL_REFRESH_URL", "").strip()
    if not url:
        log.warning("TEGEL_REFRESH_URL unset: the data server sees new crates after restart")
        return False
    token = os.environ.get("TEGEL_REFRESH_TOKEN", "").strip()
    if token:
        response = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        response.raise_for_status()
        return True
    from tegel.actions.refresh import refresh_catalog

    refresh_catalog()
    return True
