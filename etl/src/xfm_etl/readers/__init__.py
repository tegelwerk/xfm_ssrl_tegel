"""Readers turn beamline files into ``RawScan`` objects; the writer does the rest.

A reader has:

- ``read(paths, **options) -> list[RawScan]``
- ``patterns``: glob patterns of the files it reads (the watcher uses them)
- ``group(paths) -> list[list[Path]]``: which files make up one scan. The default
  is one file per scan; a format that splits a scan over several files (an MCA file
  plus a scaler file, say) groups them, and returns a group only once it is complete.

Register a reader in ``READERS``. The SSRL beamline format reader goes here once a
real file is available; until then ``synthetic`` produces realistic test scans.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from xfm_etl.model import RawScan
from xfm_etl.readers import synthetic


def one_per_file(paths: Sequence[Path]) -> list[list[Path]]:
    return [[Path(item)] for item in sorted(paths)]


@dataclass(frozen=True)
class Reader:
    name: str
    read: Callable[..., list[RawScan]]
    patterns: tuple[str, ...]
    group: Callable[[Sequence[Path]], list[list[Path]]] = field(default=one_per_file)


READERS: dict[str, Reader] = {
    "synthetic": Reader("synthetic", synthetic.read, synthetic.PATTERNS),
}


def get(name: str) -> Reader:
    try:
        return READERS[name]
    except KeyError:
        known = ", ".join(sorted(READERS))
        raise ValueError(f"unknown reader {name!r}; known: {known}") from None


def read(name: str, paths: list[Path], **options) -> list[RawScan]:
    return get(name).read(paths, **options)
