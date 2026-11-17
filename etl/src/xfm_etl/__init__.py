"""ETL for SSRL XFM beamline files.

Each scan becomes one crate directory named by a TypeID (``xfm_<uuidv7>``):

- ``scan.ome.zarr/``: an OME-Zarr (NGFF 0.5) image holding the MCA spectral cube
  ``(c, y, x)`` with a 2x pyramid, plus per-pixel scalers and a total-counts map;
- ``aims-leaf.json``: an aims-leaf ``Dataset`` with the experiment run, sample,
  instrument, study, data files and their associations;
- ``ro-crate-metadata.json``: the RO-Crate that packages both.

The beamline files are only read. This package has no Tegel dependency: Tegel's
XFM data server claims the crates it writes.
"""

from xfm_etl.model import RawScan, ScanMeta
from xfm_etl.write import CRATE_PROFILE, write_scan

__all__ = ["CRATE_PROFILE", "RawScan", "ScanMeta", "write_scan"]
__version__ = "0.1.0"
