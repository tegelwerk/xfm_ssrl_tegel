# tegel-xfm-compute

XFM processing actions for the Tegel action server (`tegel-actionserver`). Raw scans live on the XFM data server. Every product derived from them is an action-server run, and that run records the inputs it `used`.

| Action | Inputs | Outputs |
| --- | --- | --- |
| `xfm.roi_maps` | `mca`: an MCA cube `(c, y, x)`, e.g. a raw scan's `mca-0` part URL on the data server | `maps` `(element, y, x)`, `rois` table |
| `xfm.import_processed` | `crate`: `ProcessedCache/<measurement>/ro-crate-metadata.json` | `clr`, `mask`, `elements`, `record` |

Install this package next to the action server, which then loads the actions from the `tegel.actions` entry-point group.
