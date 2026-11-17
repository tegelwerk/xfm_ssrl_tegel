"""Import an existing ProcessedCache measurement as an action-server run.

Processed products are not served by the XFM data server any more. This action
turns one legacy ``ProcessedCache/<measurement>/`` crate into a run whose outputs
are the CLR stack, its mask, the element table and a summary record, so the
products stay searchable next to new processing results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
import zarr

from tegel.actionserver import action, port


@action(
    "xfm.import_processed",
    version="0.1.0",
    inputs=[
        port("crate", "file",
             description="ro-crate-metadata.json of a ProcessedCache/<measurement>/ crate"),
    ],
    outputs=[
        port("clr", "array", description="CLR stack of the designated store"),
        port("mask", "array", description="foreground mask", optional=True),
        port("elements", "table", description="elements.csv of the crate", optional=True),
        port("record", "json", description="measurement summary"),
    ],
    software=["zarr", "pandas"],
    license="BSD-3-Clause",
)
def import_processed(ctx, crate):
    """Read a ProcessedCache crate: designated zarr store, element table, summary."""
    metadata = Path(crate)
    if metadata.name != "ro-crate-metadata.json":
        raise ValueError("crate must be a ro-crate-metadata.json file")
    crate_dir = metadata.parent
    if crate_dir.parent.name != "ProcessedCache":
        raise ValueError("crate is not under a ProcessedCache directory")
    record = summary(metadata)
    store = crate_dir / record["designated"]
    if not record["designated"] or not store.is_dir():
        raise ValueError(f"designated store not found: {record['designated']!r}")
    group = zarr.open_group(store, mode="r")
    names = set(group.array_keys())
    if "clr" not in names:
        raise ValueError(f"designated store has no clr array (has {sorted(names)})")
    outputs: dict[str, Any] = {"clr": np.asarray(group["clr"][...]), "record": record}
    if "mask" in names:
        outputs["mask"] = np.asarray(group["mask"][...])
    table = crate_dir / "elements.csv"
    if table.is_file():
        outputs["elements"] = pd.read_csv(table)
    ctx.log.info("imported %s: clr %s", record["measurement_id"], outputs["clr"].shape)
    return outputs


def summary(metadata: Path) -> dict[str, str]:
    """The measurement summary of a ProcessedCache crate."""
    body = json.loads(metadata.read_text(encoding="utf-8"))
    graph = body.get("@graph") if isinstance(body, dict) else None
    graph = graph if isinstance(graph, list) else []
    root = next((e for e in graph if isinstance(e, dict) and e.get("@id") == "./"), None)
    if root is None:
        raise ValueError("processed crate has no root dataset")
    sidecar = metadata.parent / "lambda-record.yaml"
    fuller: dict[str, Any] = {}
    if sidecar.is_file():
        loaded = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        fuller = loaded if isinstance(loaded, dict) else {}
    folder = metadata.parent.name
    name = root.get("name")
    measurement_id = name if isinstance(name, str) and name.strip() else folder
    sample_code = root.get("sample_code") if isinstance(root.get("sample_code"), str) else ""
    designated = next(
        (str(e.get("@id") or "") for e in graph
         if isinstance(e, dict) and e.get("isDesignated") is True),
        "",
    )
    technique = root.get("technique")
    return {
        "measurement_id": measurement_id,
        "sample_code": sample_code,
        "campaign": _campaign(sample_code, measurement_id),
        "kind": _kind(measurement_id),
        "beam_energy_kev": _beam_energy(fuller),
        "acquired": _acquired(root, fuller),
        "technique": technique if isinstance(technique, str) and technique else "xrf",
        "designated": designated.rstrip("/"),
    }


def _campaign(sample_code: str, measurement_id: str) -> str:
    if sample_code.startswith("AAPL") or measurement_id.startswith("aapl-"):
        return "aapl"
    if "Germ" in sample_code or measurement_id.startswith("radish-germ"):
        return "radish-germ"
    return "radish"


def _kind(measurement_id: str) -> str:
    if measurement_id.startswith("aapl-"):
        return "mca_fit"
    if "sulfur-edge" in measurement_id:
        return "sulfur_edge"
    return "roi_map"


def _numeric(quantity: Any) -> str:
    """``numeric_value`` (aims-leaf), or ``has_numeric_value`` (older lambda records)."""
    if not isinstance(quantity, dict):
        return ""
    for key in ("numeric_value", "has_numeric_value"):
        if quantity.get(key) not in (None, ""):
            return _text(quantity[key])
    return ""


def _beam_energy(record: dict[str, Any]) -> str:
    if record.get("beam_energy_keV") not in (None, ""):
        return _text(record["beam_energy_keV"])
    for run in record.get("experiment_runs") or []:
        if isinstance(run, dict):
            found = _numeric(run.get("energy")) or _numeric(run.get("beam_energy"))
            if found:
                return found
    images = record.get("data_files") or record.get("images") or []
    images = [*images, record.get("xrf_image") or {}]
    for image in images:
        if isinstance(image, dict) and _numeric(image.get("beam_energy")):
            return _numeric(image.get("beam_energy"))
    return ""


def _acquired(root: dict[str, Any], record: dict[str, Any]) -> str:
    created = root.get("dateCreated")
    if isinstance(created, str) and created:
        return created[:10]
    for run in record.get("experiment_runs") or []:
        if isinstance(run, dict) and run.get("experiment_date"):
            return str(run["experiment_date"])[:10]
    return ""


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else format(value, "g")
    return str(value)
