"""Readers turn beamline files into ``RawScan`` objects; the writer does the rest.

A reader is a function ``read(paths, **options) -> list[RawScan]``. Register a new
one in ``READERS``. The SSRL beamline format reader goes here once a real file is
available; until then ``synthetic`` produces realistic test scans.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from xfm_etl.model import RawScan
from xfm_etl.readers import synthetic

Reader = Callable[..., list[RawScan]]

READERS: dict[str, Reader] = {"synthetic": synthetic.read}


def get(name: str) -> Reader:
    try:
        return READERS[name]
    except KeyError:
        known = ", ".join(sorted(READERS))
        raise ValueError(f"unknown reader {name!r}; known: {known}") from None


def read(name: str, paths: list[Path], **options) -> list[RawScan]:
    return get(name)(paths, **options)
