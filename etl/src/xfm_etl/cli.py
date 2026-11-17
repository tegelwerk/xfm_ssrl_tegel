"""``xfm-etl``: convert beamline files into OME-Zarr scan crates."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from xfm_etl import readers
from xfm_etl.write import write_scan


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="xfm-etl")
    sub = parser.add_subparsers(dest="command", required=True)
    convert = sub.add_parser("convert", help="convert beamline files into scan crates")
    convert.add_argument("inputs", nargs="*", type=Path, help="beamline files")
    convert.add_argument("--reader", required=True, help="reader name; see `xfm-etl readers`")
    convert.add_argument("--out", required=True, type=Path, help="directory that receives crates")
    convert.add_argument("--study", help="study / campaign name")
    convert.add_argument("--sample-sheet", type=Path,
                         help="YAML: sample_code -> aims-leaf PlantSample fields")
    sub.add_parser("readers", help="list the available readers")
    args = parser.parse_args(argv)
    if args.command == "readers":
        for name in sorted(readers.READERS):
            print(name)
        return
    sheet = yaml.safe_load(args.sample_sheet.read_text("utf-8")) if args.sample_sheet else {}
    for scan in readers.read(args.reader, list(args.inputs)):
        if args.study:
            scan.meta.study = args.study
        if scan.meta.sample_code in (sheet or {}):
            scan.meta.sample = sheet[scan.meta.sample_code]
        crate = write_scan(scan, args.out)
        print(f"{crate}  {scan.meta.scan_name}  {scan.mca.shape}")


if __name__ == "__main__":
    main()
