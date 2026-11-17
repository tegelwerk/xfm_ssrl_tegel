"""Synthetic XRF raster scans for tests and demos: a leaf-shaped map with K-line peaks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np

from xfm_etl.model import RawScan, ScanMeta

# K-alpha energies (eV) of elements in the AAPL leaf maps
LINES = {"S": 2308, "Cl": 2622, "K": 3314, "Ca": 3692, "Mn": 5899, "Fe": 6404,
         "Ni": 7478, "Cu": 8048, "Zn": 8639}
GAIN_EV = 10.0
PATTERNS = ("*.h5", "*.hdf5", "*.dat")  # any such file stands for one synthetic scan


def scan(
    name: str = "AAPL_10serp_P1_leaf1",
    *,
    sample_code: str = "AAPL-P1-leaf1",
    shape: tuple[int, int] = (96, 128),
    channels: int = 1024,
    seed: int = 0,
    study: str | None = "aapl",
) -> RawScan:
    rng = np.random.default_rng(seed)
    ny, nx = shape
    y, x = np.mgrid[0:ny, 0:nx]
    leaf = (((y - ny / 2) / (0.42 * ny)) ** 2 + ((x - nx / 2) / (0.46 * nx)) ** 2) < 1.0
    vein = np.abs(y - ny / 2) < max(1, ny // 30)
    energy = np.arange(channels) * GAIN_EV
    spectrum = np.zeros((ny, nx, channels))
    for index, line in enumerate(LINES.values()):
        weight = (leaf * (1.0 + 2.0 * vein * (index % 2))) * (40.0 / (1 + index))
        peak = np.exp(-0.5 * ((energy - line) / 70.0) ** 2)
        spectrum += weight[..., None] * peak[None, None, :]
    spectrum += 0.2  # background
    counts = rng.poisson(spectrum).astype(np.uint32)
    i0 = rng.normal(1e5, 500, size=shape).astype(np.float32)
    meta = ScanMeta(
        scan_name=name,
        sample_code=sample_code,
        beamline="BL2-3",
        experiment_date=date(2024, 5, 1),
        study=study,
        energy_kev=10.0,
        dwell_time_ms=50.0,
        step_size_um=(2.0, 2.0),
        beam_size_um=(2.0, 2.0),
        detector_model="Vortex-ME4",
        mca_offset_ev=0.0,
        mca_gain_ev=GAIN_EV,
        reader="synthetic",
    )
    return RawScan(meta=meta, mca=counts, scalers={"I0": i0})


def read(paths: list[Path], **options) -> list[RawScan]:
    """One synthetic scan per path name given (the paths need not exist)."""
    paths = [Path(item) for item in paths]
    if not paths:
        return [scan(seed=0, **options)]
    scans = []
    for index, path in enumerate(paths):
        options.setdefault("sample_code", None)
        sample = options["sample_code"] or path.stem.replace("_", "-")
        raw = scan(path.stem, seed=index, **{**options, "sample_code": sample})
        raw.meta.raw_files = [path] if path.exists() else []
        scans.append(raw)
    return scans
