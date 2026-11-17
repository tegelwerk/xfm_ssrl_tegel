"""``xfm.etl``: the catalog action that turns beamline files into served raw scans.

It runs on an action server deployed next to the data server, because it writes
that data server's catalog. It runs the Prefect flow ``xfm-etl-files``
(``tegel.xfm.actions.flows``): convert the files into scan crates under
``TEGEL_RECORDS``, stage and merge them into the catalog, refresh the data server.
With ``PREFECT_API_URL`` set, the run is visible in the Prefect UI next to the
scheduled ``xfm-etl-raw-directory`` runs.

Environment: see ``tegel.xfm.actions.ingest``.
"""

from __future__ import annotations

from typing import Any

from tegel.actionserver import action, port


@action(
    "xfm.etl",
    version="0.2.0",
    inputs=[
        port("files", "selection",
             description="beamline files (file: URIs under the server's readable storage)"),
    ],
    outputs=[port("ingested", "json", description="crates written and staged, with errors")],
    scope="catalog:write",
    resources=["catalog-writer"],
    software=["xfm-ssrl-etl", "tegel-actions", "prefect", "zarr"],
    license="BSD-3-Clause",
)
def etl(
    ctx,
    files,
    *,
    reader: str,
    study: str = "",
    samples: dict[str, Any] | None = None,
):
    """Convert beamline files to OME-Zarr scan crates and ingest them into the catalog.

    ``reader`` is an ``xfm-etl`` reader (``xfm-etl readers``). ``study`` names the
    aims-leaf Study. ``samples`` maps sample codes to aims-leaf PlantSample fields.
    """
    from tegel.xfm.actions.flows import etl_files
    from xfm_etl import readers

    readers.get(reader)  # an unknown reader fails before any work
    report = etl_files([str(path) for path in files], reader, study=study or None,
                       samples=samples)
    if not report["crates"]:
        raise ValueError("the reader found no scans in the selected files")
    if report["staged"] == 0:
        raise RuntimeError(f"no crate could be staged: {report['errors']}")
    for crate in report["crates"]:
        ctx.log.info("ingested %s", crate["id"])
    return {"ingested": report}
