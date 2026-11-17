"""Tegel pre-ingest converter (``tegel.converters`` entry point ``xfm``).

Tegel's ``ingest-raw`` flow calls this to turn ready raw beamline files into scan
crates under the records directory; it then ingests the crates like any other
record. Nothing here imports Tegel: the contract is checked by shape.

Configuration:

- ``XFM_ETL_READER`` (required): the ``xfm-etl`` reader to use. There is no
  default, so that installing this package never converts files with the wrong
  (or the synthetic) reader.
- the ``xfm`` entry of Tegel's converters configuration (``TEGEL_CONVERTERS_CONFIG``):
  ``study`` (overrides the collection folder), ``samples`` (sample code ->
  aims-leaf PlantSample fields) or ``sample_sheet`` (a YAML file of the same),
  ``layout`` (``tree`` or ``flat``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from xfm_etl import readers
from xfm_etl.write import METADATA, write_scan

READER_ENV = "XFM_ETL_READER"


class XfmConverter:
    """Beamline files -> OME-Zarr scan crates, with the reader named by ``XFM_ETL_READER``."""

    def __init__(self) -> None:
        name = os.environ.get(READER_ENV, "").strip()
        if not name:
            raise ValueError(
                f"set {READER_ENV} to an xfm-etl reader ({', '.join(sorted(readers.READERS))})"
            )
        self.reader = readers.get(name)

    @property
    def plugin_id(self) -> str:
        return f"xfm-ssrl-etl-{self.reader.name}"

    @property
    def patterns(self) -> tuple[str, ...]:
        return self.reader.patterns

    def group(self, paths: Sequence[Path]) -> list[list[Path]]:
        return self.reader.group(paths)

    def convert(self, paths: Sequence[Path], records: Path,
                options: Mapping[str, Any]) -> list[Path]:
        study = options.get("study") or options.get("collection")
        samples = dict(options.get("samples") or {})
        sheet = options.get("sample_sheet")
        if sheet:
            samples.update(yaml.safe_load(Path(sheet).read_text("utf-8")) or {})
        layout = options.get("layout") or "tree"
        written = []
        for scan in self.reader.read([Path(item) for item in paths]):
            scan.meta.study = study
            if scan.meta.sample_code in samples:
                scan.meta.sample = samples[scan.meta.sample_code]
            written.append(write_scan(scan, records, layout=layout) / METADATA)
        return written
