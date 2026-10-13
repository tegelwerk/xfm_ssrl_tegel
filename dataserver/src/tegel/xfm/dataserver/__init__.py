"""Processed-measurement plugin. The server loads this module by entry point."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml

from tegel.core.contracts.resources import DataDescriptor, Resource, StructureFamily
from tegel.core.contracts.types import ResourceType, linkml_attribute_names
from tegel.core.plugins import Extractor, ProcessedDataPackage

SCHEMA_ID = "https://w3id.org/tegel/xfm/processed"

RECORD = ResourceType(
    name="xfm.processed",
    version="1",
    metadata_schema=SCHEMA_ID,
)


class ProcessedExtractor(Extractor):
    """Claim a processed measurement record. Other files are left for another plugin."""

    @property
    def plugin_id(self) -> str:
        return "xfm-processed-v1"

    def validate(self, input_path: str) -> bool:
        path = Path(input_path)
        if path.name == "ro-crate-metadata.json":
            return _cache_name(path) == "ProcessedCache" and _crate_root(path) is not None
        body = _object(input_path)
        return (
            body is not None
            and isinstance(body.get("measurement_id"), str)
            and isinstance(body.get("sample_code"), str)
            and isinstance(body.get("key"), str)
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
        key = options.get("key")
        if isinstance(key, str) and key.strip():
            body["key"] = key
        body["payload_uri"] = _payload_uri(body, path)
        metadata: dict[str, Any] = {
            "plugin_id": self.plugin_id,
            "source_name": path.name,
            "resource_type": RECORD.name,
            "resource_type_version": RECORD.version,
        }
        for field in linkml_attribute_names(_schema()):
            metadata[field] = body[field]
        return ProcessedDataPackage(
            payload_path=str(path),
            metadata=metadata,
            metrics={},
        )


class ProcessedReader:
    """Describe the designated array from zarr metadata. Chunk files are not read."""

    @property
    def plugin_id(self) -> str:
        return "xfm-processed-v1"

    def validate(self, resource: Resource) -> bool:
        return resource.type == RECORD.name

    def describe(self, resource: Resource) -> DataDescriptor:
        uri = resource.metadata.get("payload_uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError("payload_uri is required")
        store = _local_path(uri)
        array = _array_metadata(store)
        if array is None:
            return DataDescriptor(
                family=StructureFamily.bytes,
                media_type="application/octet-stream",
                content_url=f"{resource.uri}/data/content",
            )
        shape = [int(item) for item in array["shape"]]
        chunk = array["chunk_grid"]["configuration"]["chunk_shape"]
        return DataDescriptor(
            family=StructureFamily.array,
            media_type="application/zarr",
            shape=shape,
            chunks=[[int(item) for item in chunk]],
            data_type=str(array["data_type"]),
        )


def _schema_path() -> Path:
    relative = Path("schema") / "measurement.yaml"
    if relative.is_file():
        return relative
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "schema" / "measurement.yaml"
        if candidate.is_file():
            return candidate
    raise ValueError("schema/measurement.yaml was not found")


def _schema() -> dict[str, Any]:
    loaded = yaml.safe_load(_schema_path().read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("measurement schema must be a YAML mapping")
    return loaded


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
    measurement_id = crate_dir.name
    sample_code = root.get("sample_code")
    sample_code = sample_code if isinstance(sample_code, str) else ""
    technique = root.get("technique")
    graph = _object(str(metadata_path))
    entities = graph.get("@graph") if isinstance(graph, dict) else []
    return {
        "key": f"ProcessedCache--{measurement_id}",
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


def _payload_uri(body: dict[str, Any], record: Path) -> str:
    explicit = body.get("payload_uri")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    designated = body.get("designated")
    if not isinstance(designated, str) or not designated.strip():
        return record.resolve().as_uri()
    base = record.parent
    crate = body.get("crate_path")
    if isinstance(crate, str) and crate not in {"", "."}:
        candidate = Path(crate)
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.is_dir():
            base = candidate
    return (base / designated.strip().rstrip("/")).resolve().as_uri()


def _local_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    return Path(uri)


def _array_directory(store: Path) -> Path | None:
    """Return the directory of the designated array inside a zarr store."""
    if store.is_file():
        return None
    preferred = store / "clr" / "zarr.json"
    paths = [preferred] if preferred.is_file() else []
    if store.is_dir():
        paths.extend(sorted(store.rglob("zarr.json")))
    seen: set[Path] = set()
    for path in paths:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        body = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(body, dict) and body.get("node_type") == "array" and "shape" in body:
            return path.parent
    return None


def _array_metadata(store: Path) -> dict[str, Any] | None:
    directory = _array_directory(store)
    if directory is None:
        return None
    body = json.loads((directory / "zarr.json").read_text(encoding="utf-8"))
    if isinstance(body, dict) and body.get("node_type") == "array" and "shape" in body:
        return body
    return None
