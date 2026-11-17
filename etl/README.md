# xfm-ssrl-etl

ETL for SSRL X-ray fluorescence microscopy beamline files. It reads the files and never changes them. Each raster scan becomes one **scan crate**:

```
<out>/xfm_01k…/                        TypeID (prefix xfm, UUIDv7), minted at conversion
├── ro-crate-metadata.json             RO-Crate 1.1; conformsTo https://w3id.org/tegel/xfm/raw-scan/0.1
├── aims-leaf.json                     aims-leaf Dataset: ExperimentRun, Sample, Instrument, Study, DataFiles
└── scan.ome.zarr/                     OME-Zarr 0.5 (zarr v3)
    ├── 0, 1, 2, …                     MCA cube (c, y, x), 2x pyramid in y and x (counts summed)
    ├── scalers/<name>                 per-pixel beamline scalers, e.g. I0
    └── quicklook/total_counts         sum over all MCA channels
```

The OME-Zarr image is a standard multiscale image. The channel axis `c` is the MCA channel. Its energy calibration (`offset_ev`, `gain_ev`), the step size and the scaler names are stored in the group attributes under `xfm`. The scale transforms carry the step size in micrometres.

This package does not depend on Tegel. Tegel's XFM data server claims these crates.

## Output outline

```
<out>/
├── README.md, index.csv              index: id, study, sample, scan, beamline, date, energy, shape, path, sources
├── .xfm-etl/ledger.json              watcher ledger
└── <study>/<sample_code>/xfm_<id>/   one crate (above); `--layout flat` puts crates directly under <out>
```

## Use

```bash
xfm-etl readers                                   # available readers
xfm-etl convert --reader synthetic a b --out /data/xfm/crates --study aapl
xfm-etl convert --reader <name> /beamline/files/*.h5 --out /data/xfm/crates \
    --study aapl --sample-sheet samples.yaml
xfm-etl watch /beamline/files --reader <name> --out /data/xfm/crates   # automatic; --once for cron
```

`watch` converts each file once its size and mtime have been stable for `--stable` seconds (default 60) and polls every `--interval` seconds (default 30). The study comes from `--study`, else from the first folder under the raw directory. A ledger in `<out>/.xfm-etl/` prevents double conversion; a file that changes after conversion is logged, not reconverted.

`--sample-sheet` maps a sample code to aims-leaf `PlantSample` fields (genus, species, tissue, growth facility, …). Without a sheet the record carries a generic aims-leaf `Sample`.

From Python:

```python
from xfm_etl import RawScan, ScanMeta, write_scan
crate = write_scan(RawScan(meta=ScanMeta(...), mca=cube_yxc, scalers={"I0": i0}), "/data/xfm/crates")
```

## Readers

A reader turns beamline files into `RawScan` objects: the MCA cube `(y, x, channel)`, the scalers and the metadata. It also declares the file `patterns` it reads and how files `group` into scans (one file per scan by default). Register it in `xfm_etl.readers.READERS`.

So far only `synthetic` exists, which makes leaf-shaped test scans with K-line peaks. **The SSRL beamline reader still needs a real raw file to be written.**

## aims-leaf alignment

The record validates against the aims-leaf pydantic models, with one exception: `file_format: zarr` is not yet in aims-leaf's `FileFormatEnum`. aims-leaf also has no slots for the scan step size or the MCA calibration, so those live in the OME-Zarr attributes. These are change requests for aims-leaf.
