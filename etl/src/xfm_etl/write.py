"""Write one scan crate: OME-Zarr image, aims-leaf record, RO-Crate."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import zarr

from xfm_etl import typeid
from xfm_etl.model import RawScan, ScanMeta

CRATE_PROFILE = "https://w3id.org/tegel/xfm/raw-scan/0.1"
NGFF = "https://ngff.openmicroscopy.org/0.5/"
AIMS_LEAF = "https://w3id.org/aims-leaf/"
IMAGE = "scan.ome.zarr"
RECORD = "aims-leaf.json"
METADATA = "ro-crate-metadata.json"
MIN_LEVEL_SIZE = 32
MAX_LEVELS = 5
CHUNK = (128, 64, 64)

_FORMATS = {".h5": "h5", ".hdf5": "hdf5", ".hdf": "hdf5", ".nxs": "hdf5", ".dat": "ascii",
            ".txt": "ascii", ".csv": "csv", ".tif": "tiff", ".tiff": "tiff", ".json": "json"}


def write_scan(
    scan: RawScan, out_root: str | Path, *, scan_id: str | None = None, overwrite: bool = False
) -> Path:
    """Write ``scan`` as ``out_root/<scan_id>/`` and return that directory.

    The crate is assembled in a temporary sibling directory and renamed into place,
    so a crash never leaves a half-written crate under its final name.
    """
    scan_id = scan_id or typeid.mint()
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    final = root / scan_id
    if final.exists():
        if not overwrite:
            raise FileExistsError(f"crate already exists: {final}")
        shutil.rmtree(final)
    work = root / f".{scan_id}.partial"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    try:
        levels = _write_image(scan, work / IMAGE)
        record = aims_leaf_record(scan.meta, scan_id, scan.mca.shape, levels)
        (work / RECORD).write_text(json.dumps(record, indent=2, default=str) + "\n", "utf-8")
        crate = ro_crate(scan.meta, scan_id)
        (work / METADATA).write_text(json.dumps(crate, indent=2) + "\n", "utf-8")
        work.rename(final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return final


def _write_image(scan: RawScan, path: Path) -> int:
    """NGFF 0.5 multiscale image ``(c, y, x)`` plus ``scalers/`` and ``quicklook/``."""
    meta = scan.meta
    cube = np.moveaxis(np.asarray(scan.mca), -1, 0)  # (c, y, x)
    group = zarr.open_group(path, mode="w", zarr_format=3)
    step_y, step_x = meta.step_size_um or (1.0, 1.0)
    datasets = []
    level = cube
    for index in range(MAX_LEVELS):
        chunks = tuple(min(size, chunk) for size, chunk in zip(level.shape, CHUNK, strict=True))
        array = group.create_array(
            str(index), shape=level.shape, dtype=level.dtype, chunks=chunks,
            dimension_names=("c", "y", "x"),
        )
        array[...] = level
        factor = 2**index
        datasets.append(
            {
                "path": str(index),
                "coordinateTransformations": [
                    {"type": "scale", "scale": [1.0, step_y * factor, step_x * factor]}
                ],
            }
        )
        if min(level.shape[1:]) // 2 < MIN_LEVEL_SIZE:
            break
        level = _downsample(level)
    channel_axis: dict[str, Any] = {"name": "c", "type": "channel"}
    group.attrs["ome"] = {
        "version": "0.5",
        "multiscales": [
            {
                "name": "mca",
                "axes": [
                    channel_axis,
                    {"name": "y", "type": "space", "unit": "micrometer"},
                    {"name": "x", "type": "space", "unit": "micrometer"},
                ],
                "datasets": datasets,
            }
        ],
    }
    group.attrs["xfm"] = {
        "scan_name": meta.scan_name,
        "mca": {"channels": int(cube.shape[0]), "offset_ev": meta.mca_offset_ev,
                "gain_ev": meta.mca_gain_ev},
        "step_size_um": list(meta.step_size_um) if meta.step_size_um else None,
        "scalers": sorted(scan.scalers),
        "quicklook": ["total_counts"],
    }
    scalers = group.create_group("scalers")
    for name, values in sorted(scan.scalers.items()):
        scalers.create_array(name, shape=values.shape, dtype=values.dtype,
                             chunks=_chunks2d(values.shape),
                             dimension_names=("y", "x"))[...] = values
    quick = group.create_group("quicklook")
    total = cube.sum(axis=0, dtype=np.float64)
    quick.create_array("total_counts", shape=total.shape, dtype="float64",
                       chunks=_chunks2d(total.shape),
                       dimension_names=("y", "x"))[...] = total
    return len(datasets)


def _downsample(cube: np.ndarray) -> np.ndarray:
    c, y, x = cube.shape
    y2, x2 = y // 2 * 2, x // 2 * 2
    block = cube[:, :y2, :x2].reshape(c, y2 // 2, 2, x2 // 2, 2)
    summed = block.sum(axis=(2, 4), dtype=np.float64)
    if np.issubdtype(cube.dtype, np.integer):
        return summed.astype(cube.dtype if cube.dtype.itemsize >= 4 else np.uint32)
    return (summed / 4).astype(cube.dtype)


def _chunks2d(shape: tuple[int, ...]) -> tuple[int, int]:
    return (min(shape[0], 256), min(shape[1], 256))


def _quantity(value: float | None, unit: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"numeric_value": float(value), "unit": unit}


def _drop_none(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def aims_leaf_record(
    meta: ScanMeta, scan_id: str, mca_shape: tuple[int, ...], levels: int
) -> dict[str, Any]:
    """The scan as an aims-leaf ``Dataset`` (one run, sample, instrument, study, files)."""
    base = f"urn:typeid:{scan_id}"
    run_id = f"{base}#run"
    sample_id = f"urn:xfm:sample:{meta.sample_code}"
    instrument_code = f"{meta.facility}-{meta.beamline}"
    instrument_id = f"urn:xfm:instrument:{instrument_code}"
    image_id = f"{base}#{IMAGE}"
    beam_y, beam_x = meta.beam_size_um or (None, None)
    run = _drop_none(
        {
            "id": run_id,
            "title": f"XRF raster scan {meta.scan_name}",
            "experiment_code": meta.scan_name,
            "technique": "xray_microprobe",
            "experiment_date": meta.experiment_date.isoformat() if meta.experiment_date else None,
            "operator_id": meta.operator,
            "raw_data_location": _common_parent(meta.raw_files),
            "processing_status": "raw",
            "beamline": meta.beamline,
            "energy": _quantity(meta.energy_kev, "keV"),
            "dwell_time": _quantity(meta.dwell_time_ms, "ms"),
            "beam_size_x": _quantity(beam_x, "um"),
            "beam_size_y": _quantity(beam_y, "um"),
        }
    )
    instrument = _drop_none(
        {
            "id": instrument_id,
            "instrument_code": instrument_code,
            "instrument_category": "SYNCHROTRON_BEAMLINE",
            "facility_name": meta.facility,
            "beamline_id": meta.beamline,
        }
    )
    files = [
        _drop_none(
            {
                "id": image_id,
                "file_name": IMAGE,
                "file_format": "zarr",
                "data_type": "raw_data",
                "file_role": "converted",
                "related_entity": run_id,
                "description": f"OME-Zarr 0.5 MCA cube (c, y, x) = "
                f"{(mca_shape[2], mca_shape[0], mca_shape[1])}, {levels} level(s)",
            }
        )
    ]
    for index, raw in enumerate(meta.raw_files):
        raw = Path(raw)
        files.append(
            _drop_none(
                {
                    "id": f"{base}#raw-{index}",
                    "file_name": raw.name,
                    "file_path": str(raw.resolve()),
                    "file_format": _FORMATS.get(raw.suffix.lower(), "ascii"),
                    "data_type": "raw_data",
                    "file_role": "source",
                    "related_entity": run_id,
                    "storage_uri": raw.resolve().as_uri(),
                }
            )
        )
    record: dict[str, Any] = {
        "id": base,
        "title": f"{meta.sample_code} {meta.scan_name}",
        "keywords": ["XRF", "x-ray fluorescence microscopy", meta.facility],
        "instruments": [instrument],
        "experiment_runs": [run],
        "data_files": files,
        "experiment_sample_associations": [
            {"experiment_id": run_id, "sample_id": sample_id, "role": "target"}
        ],
        "experiment_instrument_associations": [
            {"experiment_id": run_id, "instrument_id": instrument_id, "role": "primary"}
        ],
        "sample_datafile_associations": [
            {"sample_id": sample_id, "data_id": image_id, "role": "target"}
        ],
    }
    if meta.sample:
        plant = {"id": sample_id, "sample_code": meta.sample_code, "sample_type": "tissue"}
        plant.update(meta.sample)
        record["plantsamples"] = [plant]
    else:
        record["samples"] = [
            {"id": sample_id, "sample_code": meta.sample_code, "sample_type": "tissue"}
        ]
    if meta.study:
        study_id = f"urn:xfm:study:{meta.study}"
        record["studies"] = [{"id": study_id, "title": meta.study}]
        record["study_experiment_associations"] = [
            {"study_id": study_id, "experiment_id": run_id}
        ]
    return record


def ro_crate(meta: ScanMeta, scan_id: str) -> dict[str, Any]:
    """RO-Crate 1.1 metadata packaging the image and the aims-leaf record."""
    from xfm_etl import __version__

    raw = [{"@id": Path(item).resolve().as_uri()} for item in meta.raw_files]
    graph: list[dict[str, Any]] = [
        {
            "@id": METADATA,
            "@type": "CreativeWork",
            "conformsTo": [{"@id": "https://w3id.org/ro/crate/1.1"}, {"@id": CRATE_PROFILE}],
            "about": {"@id": "./"},
        },
        {
            "@id": "./",
            "@type": "Dataset",
            "identifier": scan_id,
            "name": meta.scan_name,
            "description": f"XRF raster scan {meta.scan_name} of {meta.sample_code}",
            "datePublished": datetime.now(UTC).isoformat(timespec="seconds"),
            "hasPart": [{"@id": RECORD}, {"@id": f"{IMAGE}/"}],
            "mainEntity": {"@id": f"{IMAGE}/"},
            "isBasedOn": raw,
        },
        {
            "@id": RECORD,
            "@type": "File",
            "name": "aims-leaf record",
            "encodingFormat": "application/json",
            "conformsTo": {"@id": AIMS_LEAF},
        },
        {
            "@id": f"{IMAGE}/",
            "@type": "Dataset",
            "name": "MCA cube",
            "encodingFormat": "application/x-zarr",
            "conformsTo": {"@id": NGFF},
        },
        {
            "@id": "#etl",
            "@type": "CreateAction",
            "name": "Convert beamline files to OME-Zarr",
            "instrument": {"@id": "#xfm-ssrl-etl"},
            "object": raw,
            "result": [{"@id": "./"}],
            "endTime": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        {
            "@id": "#xfm-ssrl-etl",
            "@type": "SoftwareApplication",
            "name": "xfm-ssrl-etl",
            "version": __version__,
            "description": f"reader: {meta.reader}",
        },
    ]
    graph.extend({"@id": item["@id"], "@type": "File"} for item in raw)
    return {"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": graph}


def _common_parent(paths: list[Path]) -> str | None:
    if not paths:
        return None
    resolved = [Path(item).resolve() for item in paths]
    parent = resolved[0].parent
    while not all(item.is_relative_to(parent) for item in resolved):
        parent = parent.parent
    return str(parent)
