"""Where crates go, and the index and README kept at the top of an output directory.

Layout ("tree", the default)::

    <out>/
    ├── README.md                         how this directory is organized
    ├── index.csv                         one row per scan crate
    └── <study>/<sample_code>/<typeid>/   one scan crate (TypeID is the unique name)

The TypeID folder is the only name that must be unique; study and sample folders
are for people browsing the tree. ``flat`` puts crates directly under ``<out>``.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xfm_etl.model import ScanMeta

UNASSIGNED = "_unassigned"
INDEX = "index.csv"
README = "README.md"
COLUMNS = (
    "id", "study", "sample_code", "scan_name", "beamline", "experiment_date", "energy_kev",
    "map_shape", "mca_channels", "path", "sources", "created",
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(text: str | None) -> str:
    """A folder-safe name: letters, digits, ``.``, ``_`` and ``-``; other runs become ``_``."""
    cleaned = _UNSAFE.sub("_", (text or "").strip()).strip("._-")
    return cleaned[:96] or UNASSIGNED


def crate_dir(out: Path, meta: ScanMeta, scan_id: str, layout: str = "tree") -> Path:
    if layout == "flat":
        return out / scan_id
    if layout != "tree":
        raise ValueError(f"unknown layout {layout!r}; use 'tree' or 'flat'")
    return out / slug(meta.study) / slug(meta.sample_code) / scan_id


def index_row(out: Path, crate: Path, meta: ScanMeta, shape: tuple[int, ...]) -> dict[str, str]:
    ny, nx, channels = shape
    return {
        "id": crate.name,
        "study": meta.study or "",
        "sample_code": meta.sample_code,
        "scan_name": meta.scan_name,
        "beamline": meta.beamline,
        "experiment_date": meta.experiment_date.isoformat() if meta.experiment_date else "",
        "energy_kev": "" if meta.energy_kev is None else f"{meta.energy_kev:g}",
        "map_shape": f"{ny}x{nx}",
        "mca_channels": str(channels),
        "path": crate.relative_to(out).as_posix(),
        "sources": ";".join(str(Path(item).resolve()) for item in meta.raw_files),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def update_index(out: Path, row: dict[str, Any]) -> Path:
    """Insert or replace ``row`` (by id) in ``<out>/index.csv``, written atomically."""
    path = out / INDEX
    rows: dict[str, dict[str, str]] = {}
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as handle:
            for existing in csv.DictReader(handle):
                rows[existing["id"]] = existing
    rows[row["id"]] = {column: str(row.get(column, "")) for column in COLUMNS}
    temporary = out / f".{INDEX}.tmp"
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for item in sorted(rows.values(), key=lambda r: (r["study"], r["sample_code"], r["id"])):
            writer.writerow(item)
    os.replace(temporary, path)
    return path


def ensure_readme(out: Path) -> None:
    path = out / README
    if path.exists():
        return
    path.write_text(
        "# XFM scan crates\n\n"
        "Written by `xfm-etl` (xfm-ssrl-etl). Do not edit by hand; re-run the ETL instead.\n\n"
        "```\n"
        "index.csv                            one row per scan: id, study, sample, scan name,\n"
        "                                     beamline, date, energy, map shape, MCA channels,\n"
        "                                     path, raw source files, conversion time\n"
        "<study>/<sample_code>/<typeid>/      one scan crate; the TypeID (xfm_...) is unique\n"
        "├── ro-crate-metadata.json           RO-Crate 1.1 (profile https://w3id.org/tegel/xfm/raw-scan/0.1)\n"
        "├── aims-leaf.json                   aims-leaf Dataset record\n"
        "└── scan.ome.zarr/                   OME-Zarr 0.5: MCA cube (c, y, x) + pyramid,\n"
        "                                     scalers/, quicklook/total_counts\n"
        "```\n\n"
        f"Scans without a study are under `{UNASSIGNED}/`. The raw files are referenced, never "
        "copied or changed.\n",
        encoding="utf-8",
    )
