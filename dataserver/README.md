# Raw XRF scan plugin

The dataserver package of the SSRL XFM incarnation (Tegel spec 0.4.0). The reference server loads it through entry points. It ships no schema: the profile in `schema/profile.yaml` names `xfm/raw.yaml` (class `XfmRawScan`) under `TEGEL_SCHEMA_ROOT`, which in this repository is `../schemas`.

**Claims.** The extractor claims the scan crates the xfm-ssrl-etl producer writes. A crate is a `ro-crate-metadata.json` whose metadata descriptor `conformsTo` `https://w3id.org/tegel/xfm/raw-scan/0.1`, with an `aims-leaf.json` beside it.

**Record.** The extractor projects the aims-leaf `Dataset` onto the flat `XfmRawScan` record:
- run fields come from `experiment_runs[0]`;
- the sample from `samples`/`plantsamples`, the study from `studies`, the facility from `instruments`;
- map shape, MCA channel count and step size come from the OME-Zarr attributes.

Quantities are checked for their unit. The resource id is the crate's TypeID.

**Parts.** The reader lists, from array headers only:
- the OME-Zarr multiscale levels as `mca-<level>`;
- `scalers/*` under their own names;
- `quicklook/total_counts`, the designated part;
- `aims-leaf.json` as a bytes part.

The payload is `ro-crate-metadata.json`. Processed products are not served here: they are action-server results (`../compute`).

Search fields: `sample_code`, `study`, `beamline`, `energy_kev`, `facility`.
