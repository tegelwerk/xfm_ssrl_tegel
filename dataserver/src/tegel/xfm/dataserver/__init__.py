"""Processed-measurement plugin. The server loads this module by entry point."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml

from tegel.core.contracts.resources import DataDescriptor, DataPart, Resource, StructureFamily
from tegel.core.contracts.types import ResourceType
from tegel.core.plugins import Extractor, ProcessedDataPackage
from tegel.xfm.dataserver.typeid import derive, is_typeid

SCHEMA_ID = "https://w3id.org/tegel/xfm/processed"

RECORD = ResourceType(
    name="xfm.processed",
    version="3",
    metadata_schema=SCHEMA_ID,
)

# Slots of ProcessedMeasurement that the extractor fills. The schema itself lives
# outside this package, under TEGEL_SCHEMA_ROOT.
FIELDS = (
    "id",
    "measurement_id",
    "sample_code",
    "campaign",
    "cache",
    "kind",
    "beam_energy_kev",
    "acquired",
    "technique",
    "crate_path",
    "designated",
)

ZARR_MEDIA_TYPE = "application/x-zarr"
SIDE_FILES = {
    ".tif": (StructureFamily.array, "image/tiff"),
    ".tiff": (StructureFamily.array, "image/tiff"),
    ".csv": (StructureFamily.table, "text/csv;header=present"),
    ".yaml": (StructureFamily.bytes, "application/yaml"),
    ".yml": (StructureFamily.bytes, "application/yaml"),
    ".png": (StructureFamily.bytes, "image/png"),
    ".txt": (StructureFamily.bytes, "text/plain"),
}


class ProcessedExtractor(Extractor):
    """Claim a processed measurement record. Other files are left for another plugin."""

    @property
    def plugin_id(self) -> str:
        return "xfm-processed-v3"

    def validate(self, input_path: str) -> bool:
        path = Path(input_path)
        if path.name == "ro-crate-metadata.json":
            return _cache_name(path) == "ProcessedCache" and _crate_root(path) is not None
        body = _object(input_path)
        return (
            body is not None
            and isinstance(body.get("measurement_id"), str)
            and isinstance(body.get("sample_code"), str)
            and body.get("cache") == "ProcessedCache"
        )

    def process(self, input_path: str, options: dict[str, Any]) -> ProcessedDataPackage:
        path = Path(input_path)
        if path.name == "ro-crate-metadata.json":
            body = _record_from_crate(path)
        else:
            body = _object(input_path)
        if body is None:
            raise ValueError("processed measurement record is not a JSON object")
        record = {field: _text(body.get(field)) for field in FIELDS}
        if not record["measurement_id"]:
            raise ValueError("measurement_id is required")
        if not is_typeid(record["id"]):
            record["id"] = derive(record["measurement_id"])
        record["crate_path"] = record["crate_path"] or "."
        record["designated"] = record["designated"].strip().rstrip("/")
        return ProcessedDataPackage(
            payload_path=str(path),
            record=record,
            resource_type=RECORD.name,
            payload_uri=path.resolve().as_uri(),
            metrics={},
        )


class ProcessedReader:
    """List a measurement's parts: every zarr array in the crate, plus side files.

    Shape hints come from ``zarr.json`` / ``.zarray``; no chunk is opened.
    The designated array is the record's ``designated`` store, preferring ``clr``.
    """

    @property
    def plugin_id(self) -> str:
        return "xfm-processed-v3"

    def validate(self, resource: Resource) -> bool:
        return resource.type == RECORD.name

    def describe(self, resource: Resource) -> DataDescriptor:
        uri = resource.payload_uri
        if not uri:
            raise ValueError("payload_uri is required")
        record_file = _local_path(uri)
        crate_path = str(resource.metadata.get("crate_path") or "")
        crate = _crate_dir({"crate_path": crate_path}, record_file)
        designated = str(resource.metadata.get("designated") or "")
        crate_mode = record_file.name == "ro-crate-metadata.json"
        parts: list[DataPart] = []
        stores = _zarr_stores(crate, designated, crate_mode)
        chosen = _designated_array(stores, designated)
        for store in stores:
            for array_dir, header in _arrays(store):
                name = _part_name(array_dir.relative_to(crate))
                parts.append(
                    DataPart(
                        name=name,
                        family=StructureFamily.array,
                        media_type=ZARR_MEDIA_TYPE,
                        asset_uri=array_dir.resolve().as_uri(),
                        designated=array_dir == chosen,
                        title=str(array_dir.relative_to(crate)),
                        shape=[int(size) for size in header["shape"]],
                    )
                )
        if crate_mode:
            for side in sorted(crate.iterdir()):
                kind = SIDE_FILES.get(side.suffix.lower())
                if not side.is_file() or kind is None or side.name.startswith("."):
                    continue
                family, media_type = kind
                parts.append(
                    DataPart(
                        name=_part_name(Path(side.name)),
                        family=family,
                        media_type=media_type,
                        asset_uri=side.resolve().as_uri(),
                        title=side.name,
                    )
                )
        _unique(parts)
        payload = DataPart(
            name="payload",
            family=StructureFamily.bytes,
            media_type="application/json",
            asset_uri=uri,
        )
        return DataDescriptor(parts=parts, payload=payload)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return _number_string(value) if not isinstance(value, str) else value


def _crate_dir(record: dict[str, str], record_file: Path) -> Path:
    base = record_file.parent.resolve()
    crate = record.get("crate_path") or ""
    if crate not in {"", "."}:
        candidate = Path(crate)
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.is_dir():
            return candidate.resolve()
    return base


def _zarr_stores(crate: Path, designated: str, crate_mode: bool) -> list[Path]:
    """The designated store, plus every top-level ``*.zarr`` in a crate directory."""
    stores: list[Path] = []
    if designated:
        candidate = crate / designated
        if candidate.is_dir():
            stores.append(candidate)
    if crate_mode and crate.is_dir():
        for found in sorted(crate.glob("*.zarr")):
            if found.is_dir() and found not in stores:
                stores.append(found)
    return stores


def _arrays(store: Path, depth: int = 3) -> list[tuple[Path, dict[str, Any]]]:
    """Arrays in a zarr store (v3 ``zarr.json`` or v2 ``.zarray``), in path order.

    Uses consolidated metadata when present, else looks ``depth`` levels down.
    No chunk file is opened.
    """
    header = _zarr_header(store)
    if header is not None and _is_array(header):
        return [(store, header)]
    consolidated = _consolidated(store, header)
    if consolidated:
        return consolidated
    found: list[tuple[Path, dict[str, Any]]] = []
    if depth <= 0 or not store.is_dir():
        return found
    for child in sorted(store.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            found.extend(_arrays(child, depth - 1))
    return found


def _zarr_header(directory: Path) -> dict[str, Any] | None:
    for name in ("zarr.json", ".zarray", ".zgroup"):
        path = directory / name
        if path.is_file():
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if isinstance(body, dict):
                if name == ".zarray":
                    body.setdefault("node_type", "array")
                return body
    return None


def _is_array(header: dict[str, Any]) -> bool:
    return header.get("node_type") == "array" and "shape" in header


def _consolidated(store: Path, header: dict[str, Any] | None) -> list[tuple[Path, dict[str, Any]]]:
    metadata: dict[str, Any] = {}
    v3 = (header or {}).get("consolidated_metadata")
    if isinstance(v3, dict) and isinstance(v3.get("metadata"), dict):
        metadata = v3["metadata"]
    elif (store / ".zmetadata").is_file():
        try:
            v2 = json.loads((store / ".zmetadata").read_text(encoding="utf-8")).get("metadata", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            v2 = {}
        metadata = {
            key.removesuffix("/.zarray"): {**value, "node_type": "array"}
            for key, value in v2.items()
            if key.endswith(".zarray") and isinstance(value, dict)
        }
    found = [
        (store / key, value)
        for key, value in sorted(metadata.items())
        if isinstance(value, dict) and _is_array(value) and (store / key).is_dir()
    ]
    return found


def _designated_array(stores: list[Path], designated: str) -> Path | None:
    if not designated or not stores or stores[0].name != Path(designated).name:
        return None
    arrays = [directory for directory, _ in _arrays(stores[0])]
    if not arrays:
        return None
    for directory in arrays:
        if directory.name == "clr":
            return directory
    return arrays[0]


def _part_name(relative: Path) -> str:
    text = "-".join(relative.parts)
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)
    cleaned = cleaned.lstrip("._-") or "part"
    return cleaned[:128]


def _unique(parts: list[DataPart]) -> None:
    seen: dict[str, int] = {}
    for index, part in enumerate(parts):
        count = seen.get(part.name, 0)
        seen[part.name] = count + 1
        if count:
            parts[index] = part.model_copy(update={"name": f"{part.name[:120]}-{count}"})


def _cache_name(metadata_path: Path) -> str:
    return metadata_path.parent.parent.name


def _crate_root(metadata_path: Path) -> dict[str, Any] | None:
    body = _object(str(metadata_path))
    graph = body.get("@graph") if isinstance(body, dict) else None
    if not isinstance(graph, list):
        return None
    for entity in graph:
        if isinstance(entity, dict) and entity.get("@id") == "./":
            return entity
    return None


def _record_from_crate(metadata_path: Path) -> dict[str, Any]:
    crate_dir = metadata_path.parent
    root = _crate_root(metadata_path)
    if root is None:
        raise ValueError("processed crate has no root dataset")
    fuller_path = crate_dir / "lambda-record.yaml"
    fuller: dict[str, Any] = {}
    if fuller_path.is_file():
        loaded = yaml.safe_load(fuller_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            fuller = loaded
    folder = crate_dir.name
    if is_typeid(folder):
        name = root.get("name")
        measurement_id = name if isinstance(name, str) and name.strip() else folder
        typeid = folder
    else:
        measurement_id = folder
        typeid = derive(folder)
    sample_code = root.get("sample_code")
    sample_code = sample_code if isinstance(sample_code, str) else ""
    technique = root.get("technique")
    graph = _object(str(metadata_path))
    entities = graph.get("@graph") if isinstance(graph, dict) else []
    return {
        "id": typeid,
        "measurement_id": measurement_id,
        "sample_code": sample_code,
        "campaign": _campaign(sample_code, measurement_id),
        "cache": "ProcessedCache",
        "kind": _kind(measurement_id),
        "beam_energy_kev": _beam_energy(fuller),
        "acquired": _acquired(root, fuller),
        "technique": technique if isinstance(technique, str) and technique else "xrf",
        "crate_path": ".",
        "designated": _designated(entities if isinstance(entities, list) else []),
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


def _beam_energy(record: dict[str, Any]) -> str:
    direct = record.get("beam_energy_keV")
    if direct not in (None, ""):
        return _number_string(direct)
    for run in record.get("experiment_runs") or []:
        if not isinstance(run, dict):
            continue
        energy = run.get("beam_energy")
        if isinstance(energy, dict) and energy.get("has_numeric_value") not in (None, ""):
            return _number_string(energy["has_numeric_value"])
    image = record.get("xrf_image") or {}
    energy = image.get("beam_energy") if isinstance(image, dict) else None
    if isinstance(energy, dict) and energy.get("has_numeric_value") not in (None, ""):
        return _number_string(energy["has_numeric_value"])
    return ""


def _acquired(root: dict[str, Any], record: dict[str, Any]) -> str:
    created = root.get("dateCreated")
    if isinstance(created, str) and created:
        return created[:10]
    for run in record.get("experiment_runs") or []:
        if isinstance(run, dict) and run.get("experiment_date"):
            return str(run["experiment_date"])[:10]
    return ""


def _designated(graph: list[Any]) -> str:
    for entity in graph:
        if isinstance(entity, dict) and entity.get("isDesignated") is True:
            return str(entity.get("@id") or "")
    return ""


def _number_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, "g")
    return str(value)


def _object(input_path: str) -> dict[str, Any] | None:
    path = Path(input_path)
    if not path.is_file() or path.suffix.lower() != ".json":
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    return body


def _local_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    return Path(uri)
