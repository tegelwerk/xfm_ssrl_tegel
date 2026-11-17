"""What a reader hands to the writer: the scan's arrays and its metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class ScanMeta:
    """Metadata of one raster scan, in the units named by each field.

    Everything a reader cannot find stays ``None`` and is left out of the record.
    """

    scan_name: str
    sample_code: str
    beamline: str
    experiment_date: date | None = None
    study: str | None = None
    facility: str = "SSRL"
    energy_kev: float | None = None
    dwell_time_ms: float | None = None
    step_size_um: tuple[float, float] | None = None  # (y, x)
    beam_size_um: tuple[float, float] | None = None  # (y, x)
    detector_model: str | None = None
    mca_offset_ev: float | None = None
    mca_gain_ev: float | None = None  # energy per MCA channel
    operator: str | None = None
    raw_files: list[Path] = field(default_factory=list)
    sample: dict[str, Any] | None = None  # aims-leaf PlantSample fields, from a sample sheet
    reader: str = "unknown"


@dataclass
class RawScan:
    """One scan: the MCA cube ``(y, x, channel)`` as acquired, and per-pixel scalers."""

    meta: ScanMeta
    mca: np.ndarray
    scalers: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mca.ndim != 3:
            raise ValueError(f"MCA cube must be (y, x, channel); got shape {self.mca.shape}")
        for name, scaler in self.scalers.items():
            if scaler.shape != self.mca.shape[:2]:
                raise ValueError(
                    f"scaler {name} has shape {scaler.shape}; the map is {self.mca.shape[:2]}"
                )
