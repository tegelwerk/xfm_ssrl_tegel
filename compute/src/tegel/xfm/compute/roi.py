"""Region-of-interest element maps from a raw MCA cube."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tegel.actionserver import action, port

# K-alpha1 energies in eV
K_ALPHA = {
    "P": 2014, "S": 2308, "Cl": 2622, "Ar": 2958, "K": 3314, "Ca": 3692, "Ti": 4511,
    "V": 4952, "Cr": 5415, "Mn": 5899, "Fe": 6404, "Co": 6930, "Ni": 7478, "Cu": 8048,
    "Zn": 8639, "As": 10544, "Se": 11222, "Br": 11924, "Rb": 13395, "Sr": 14165,
}


@action(
    "xfm.roi_maps",
    version="0.1.0",
    inputs=[port("mca", "array", description="MCA cube (c, y, x), e.g. a raw scan's mca-0 part")],
    outputs=[
        port("maps", "array", description="ROI counts (element, y, x), in the order of rois"),
        port("rois", "table", description="element, center_ev, low_channel, high_channel"),
    ],
    software=["numpy", "pandas"],
    license="BSD-3-Clause",
)
def roi_maps(
    ctx,
    mca,
    *,
    elements: str = "S,Cl,K,Ca,Mn,Fe,Ni,Cu,Zn",
    gain_ev: float = 10.0,
    offset_ev: float = 0.0,
    window_ev: float = 150.0,
):
    """Sum each element's K-alpha window over the MCA channels.

    ``gain_ev`` and ``offset_ev`` are the MCA energy calibration, found in the scan's
    OME-Zarr attributes (``xfm.mca``). The window is centre +/- ``window_ev``.
    """
    cube = np.asarray(mca)
    if cube.ndim != 3:
        raise ValueError(f"expected an MCA cube (c, y, x); got shape {cube.shape}")
    if gain_ev <= 0 or window_ev <= 0:
        raise ValueError("gain_ev and window_ev must be positive")
    names = [name.strip() for name in elements.split(",") if name.strip()]
    unknown = [name for name in names if name not in K_ALPHA]
    if unknown:
        raise ValueError(f"no K-alpha energy for {unknown}; known: {sorted(K_ALPHA)}")
    channels = cube.shape[0]
    rows, maps = [], []
    for name in names:
        center = K_ALPHA[name]
        low = max(0, int(np.floor((center - window_ev - offset_ev) / gain_ev)))
        high = min(channels - 1, int(np.ceil((center + window_ev - offset_ev) / gain_ev)))
        if low > high or low >= channels:
            raise ValueError(f"{name} K-alpha ({center} eV) is outside the MCA range")
        maps.append(cube[low : high + 1].sum(axis=0, dtype=np.float64))
        rows.append({"element": name, "center_ev": center, "low_channel": low,
                     "high_channel": high})
        ctx.log.info("%s: channels %d-%d", name, low, high)
    return {"maps": np.stack(maps), "rois": pd.DataFrame(rows)}
