"""Raw XRF scan plugin. The server loads this module by entry point.

It claims the scan crates the xfm-ssrl-etl producer writes
(``<root>/xfm_<typeid>/ro-crate-metadata.json``), projects the crate's aims-leaf
record onto the flat ``XfmRawScan`` class, and lists the OME-Zarr arrays as parts.
Processed products are not served here: they are action-server results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from tegel.core.contracts.resources import DataDescriptor, DataPart, Resource, StructureFamily
from tegel.core.contracts.types import ResourceType
from tegel.core.plugins import Extractor, ProcessedDataPackage
from tegel.xfm.dataserver.typeid import derive, is_typeid

SCHEMA_ID = "https://w3id.org/tegel/xfm/raw"
CRATE_PROFILE = "https://w3id.org/tegel/xfm/raw-scan/0.1"
METADATA = "ro-crate-metadata.json"
RECORD_FILE = "aims-leaf.json"
IMAGE = "scan.ome.zarr"

RECORD = ResourceType(name="xfm.raw_scan", version="1", metadata_schema=SCHEMA_ID)

# XfmRawScan slot -> where it comes from. Paths into the aims-leaf record are
# documented on each slot in schemas/xfm/raw.yaml (annotation `aims_leaf`), and a
# test checks this table against them.
FIELDS = (
    "id",
    "title",
    "scan_name",
    "sample_code",
    "study",
    "technique",
    "facility",
    "beamline",
    "experiment_date",
    "energy_kev",
    "dwell_time_ms",
    "beam_size_um",
    "processing_status",
    "raw_data_location",
    "map_shape",
    "mca_channels",
    "step_size_um",
)

ZARR_MEDIA_TYPE = "application/x-zarr"


class RawScanExtractor(Extractor):
    """Claim the RO-Crate metadata file of a scan crate. Everything else is left alone."""

    @property
    def plugin_id(self) -> str:
        return "xfm-raw-scan-v1"

    def validate(self, input_path: str) -> bool:
        path = Path(input_path)
        if path.name != METADATA or not (path.parent / RECORD_FILE).is_file():
            return False
        crate = _json(path)
        return crate is not None and CRATE_PROFILE in _conforms_to(crate)

    def process(self, input_path: str, options: dict[str, Any]) -> ProcessedDataPackage:
        path = Path(input_path)
        crate = _json(path)
        record = _json(path.parent / RECORD_FILE)
        if crate is None or record is None:
            raise ValueError("scan crate metadata or aims-leaf record is not a JSON object")
        projected = project(record, _root(crate), path.parent)
        return ProcessedDataPackage(
            payload_path=str(path),
            record=projected,
            resource_type=RECORD.name,
            payload_uri=path.resolve().as_uri(),
            metrics={},
        )


def project(record: dict[str, Any], root: dict[str, Any], crate_dir: Path) -> dict[str, str]:
    """The flat ``XfmRawScan`` record from an aims-leaf Dataset and the crate root."""
    run = _first(record.get("experiment_runs"))
    sample = _first(record.get("plantsamples")) or _first(record.get("samples"))
    study = _first(record.get("studies"))
    instrument = _first(record.get("instruments"))
    image = _image_attributes(crate_dir / IMAGE)
    folder = crate_dir.name
    identifier = _text(root.get("identifier"))
    if not is_typeid(identifier):
        identifier = folder if is_typeid(folder) else derive(folder)
    shape = image.get("shape") or []
    step = (image.get("xfm") or {}).get("step_size_um")
    mca = (image.get("xfm") or {}).get("mca") or {}
    projected = {
        "id": identifier,
        "title": _text(record.get("title")),
        "scan_name": _text(run.get("experiment_code")) or _text(root.get("name")) or folder,
        "sample_code": _text(sample.get("sample_code")),
        "study": _text(study.get("title")),
        "technique": _text(run.get("technique")),
        "facility": _text(instrument.get("facility_name")),
        "beamline": _text(run.get("beamline")),
        "experiment_date": _text(run.get("experiment_date"))[:10],
        "energy_kev": _quantity(run.get("energy"), "keV"),
        "dwell_time_ms": _quantity(run.get("dwell_time"), "ms"),
        "beam_size_um": _quantity(run.get("beam_size_x"), "um"),
        "processing_status": _text(run.get("processing_status")),
        "raw_data_location": _text(run.get("raw_data_location")),
        "map_shape": "x".join(str(size) for size in shape[1:]) if len(shape) == 3 else "",
        "mca_channels": _text(mca.get("channels") or (shape[0] if len(shape) == 3 else "")),
        "step_size_um": "x".join(_text(item) for item in step) if step else "",
    }
    return {field: projected[field] for field in FIELDS}


class RawScanReader:
    """Parts of a scan crate, from array headers only (no chunk is read).

    - ``mca-<level>``: the OME-Zarr multiscale levels of the MCA cube ``(c, y, x)``
    - ``<scaler>``: per-pixel beamline scalers, e.g. ``I0``
    - ``total_counts``: the quicklook sum map, the designated part
    - ``aims-leaf``: the aims-leaf record
    The payload is the RO-Crate metadata file.
    """

    @property
    def plugin_id(self) -> str:
        return "xfm-raw-scan-v1"

    def validate(self, resource: Resource) -> bool:
        return resource.type == RECORD.name

    def describe(self, resource: Resource) -> DataDescriptor:
        uri = resource.payload_uri
        if not uri:
            raise ValueError("payload_uri is required")
        metadata = _local_path(uri)
        crate = metadata.parent
        image = crate / IMAGE
        parts: list[DataPart] = []
        levels = _multiscale_paths(image)
        for path in levels:
            parts.append(_array_part(f"mca-{path}", image / path, title=f"MCA cube level {path}"))
        for group in ("scalers", "quicklook"):
            for array_dir, _header in _arrays(image / group, depth=1):
                name = array_dir.name
                parts.append(
                    _array_part(name, array_dir, designated=(name == "total_counts"),
                                title=f"{group}/{name}")
                )
        record = crate / RECORD_FILE
        if record.is_file():
            parts.append(
                DataPart(
                    name="aims-leaf",
                    family=StructureFamily.bytes,
                    media_type="application/json",
                    asset_uri=record.resolve().as_uri(),
                    title="aims-leaf record",
                )
            )
        _unique(parts)
        payload = DataPart(
            name="payload",
            family=StructureFamily.bytes,
            media_type="application/ld+json",
            asset_uri=uri,
        )
        return DataDescriptor(parts=parts, payload=payload)


def _array_part(name: str, directory: Path, *, designated: bool = False,
                title: str | None = None) -> DataPart:
    header = _zarr_header(directory) or {}
    return DataPart(
        name=_part_name(name),
        family=StructureFamily.array,
        media_type=ZARR_MEDIA_TYPE,
        asset_uri=directory.resolve().as_uri(),
        designated=designated,
        title=title,
        shape=[int(size) for size in header.get("shape", [])] or None,
    )


def _multiscale_paths(image: Path) -> list[str]:
    """Dataset paths of the first OME-Zarr multiscale (NGFF 0.5 or 0.4), finest first."""
    header = _zarr_header(image) or {}
    attributes = header.get("attributes") or {}
    ome = attributes.get("ome") or attributes  # 0.5 nests under "ome"; 0.4 is flat
    multiscales = ome.get("multiscales") or []
    if not multiscales:
        return []
    paths = [str(item.get("path")) for item in multiscales[0].get("datasets") or []]
    return [path for path in paths if (image / path).is_dir()]


def _image_attributes(image: Path) -> dict[str, Any]:
    header = _zarr_header(image) or {}
    attributes = header.get("attributes") or {}
    levels = _multiscale_paths(image)
    shape: list[int] = []
    if levels:
        shape = list((_zarr_header(image / levels[0]) or {}).get("shape") or [])
    return {"shape": shape, "xfm": attributes.get("xfm") or {}}


def _root(crate: dict[str, Any]) -> dict[str, Any]:
    for entity in crate.get("@graph") or []:
        if isinstance(entity, dict) and entity.get("@id") == "./":
            return entity
    return {}


def _conforms_to(crate: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for entity in crate.get("@graph") or []:
        if not isinstance(entity, dict) or entity.get("@id") != METADATA:
            continue
        value = entity.get("conformsTo")
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@id"), str):
                found.add(item["@id"])
            elif isinstance(item, str):
                found.add(item)
    return found


def _first(value: Any) -> dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _quantity(value: Any, unit: str) -> str:
    """``numeric_value`` of an aims-leaf QuantityValue, if it is in ``unit``."""
    if not isinstance(value, dict) or value.get("numeric_value") is None:
        return ""
    if value.get("unit") not in (None, unit):
        raise ValueError(f"expected a quantity in {unit}, got {value.get('unit')!r}")
    return _text(value["numeric_value"])


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else format(value, "g")
    return str(value)


def _arrays(store: Path, depth: int = 3) -> list[tuple[Path, dict[str, Any]]]:
    """Arrays in a zarr group (v3 ``zarr.json`` or v2 ``.zarray``), in path order.

    Looks at most ``depth`` levels down; no chunk file is opened.
    """
    header = _zarr_header(store)
    if header is not None and _is_array(header):
        return [(store, header)]
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
            body = _json(path)
            if body is None:
                return None
            if name == ".zarray":
                body.setdefault("node_type", "array")
            if name == ".zgroup" and (directory / ".zattrs").is_file():
                body["attributes"] = _json(directory / ".zattrs") or {}
            return body
    return None


def _is_array(header: dict[str, Any]) -> bool:
    return header.get("node_type") == "array" and "shape" in header


def _part_name(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)
    return (cleaned.lstrip("._-") or "part")[:128]


def _unique(parts: list[DataPart]) -> None:
    seen: dict[str, int] = {}
    for index, part in enumerate(parts):
        count = seen.get(part.name, 0)
        seen[part.name] = count + 1
        if count:
            parts[index] = part.model_copy(update={"name": f"{part.name[:120]}-{count}"})


def _json(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _local_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    return Path(uri)
