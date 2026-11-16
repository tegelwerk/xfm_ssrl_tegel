# Processed measurement plugin

The dataserver package of the SSRL XFM incarnation (Tegel spec 0.4.0). The reference server loads it through entry points. It ships no schema: the profile in `schema/profile.yaml` names `xfm/processed.yaml` under `TEGEL_SCHEMA_ROOT` (in this repository, `../schemas`).

The extractor claims two inputs: a JSON record whose `cache` is `ProcessedCache`, and `ro-crate-metadata.json` inside a `ProcessedCache/<measurement>/` directory. For a crate it also reads `lambda-record.yaml` beside that file. The record holds the slots of `ProcessedMeasurement`. The resource id is `id`, a TypeID with prefix `xfm`: the crate folder name if it is one, else derived from the folder name as a UUIDv5 (`typeid.py`). The payload is the record file itself.

The reader lists the measurement's data parts without opening chunks:

- every array in the designated zarr store, and in a crate every top-level `*.zarr` store (v3 `zarr.json` or v2 `.zarray`, consolidated metadata when present). Part names are the path inside the crate with `/` replaced by `-`, e.g. `masked-clr.zarr-clr`. The `clr` array of the designated store is the designated part.
- in a crate, side files at the top level: TIFF (array), CSV (table), YAML, PNG and text (bytes).

Staging registers zarr, TIFF and CSV parts as Tiled nodes, so clients slice them on Tiled's routes; the rest are served by Tegel with HTTP Range.

Search fields: `sample_code`, `campaign`, `cache`, `kind`, `beam_energy_kev`.
