"""``xfm-etl``: convert beamline files into OME-Zarr scan crates."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

from xfm_etl import readers
from xfm_etl.watch import local_converter
from xfm_etl.watch import watch as watch_directory
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
    watch = sub.add_parser("watch", help="convert new files in a raw directory automatically")
    watch.add_argument("raw", type=Path, help="directory of beamline files")
    watch.add_argument("--reader", required=True)
    watch.add_argument("--out", required=True, type=Path)
    watch.add_argument("--study", help="study for every file (default: first folder under raw)")
    watch.add_argument("--sample-sheet", type=Path)
    watch.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    watch.add_argument("--stable", type=float, default=60.0,
                       help="seconds a file must be unchanged before it is converted")
    watch.add_argument("--layout", choices=["tree", "flat"], default="tree")
    watch.add_argument("--once", action="store_true", help="one pass, then exit")
    convert.add_argument("--layout", choices=["tree", "flat"], default="tree")
    sub.add_parser("readers", help="list the available readers")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.command == "readers":
        for name, spec in sorted(readers.READERS.items()):
            print(f"{name}  {' '.join(spec.patterns)}")
        return
    if args.command == "watch":
        sheet = yaml.safe_load(args.sample_sheet.read_text("utf-8")) if args.sample_sheet else {}
        convert = local_converter(args.reader, args.out, samples=sheet or {},
                                  layout=args.layout)
        try:
            watch_directory(args.raw, args.out, args.reader, convert, interval=args.interval,
                            stable=args.stable, study=args.study, once=args.once)
        except KeyboardInterrupt:
            pass
        return
    sheet = yaml.safe_load(args.sample_sheet.read_text("utf-8")) if args.sample_sheet else {}
    for scan in readers.read(args.reader, list(args.inputs)):
        if args.study:
            scan.meta.study = args.study
        if scan.meta.sample_code in (sheet or {}):
            scan.meta.sample = sheet[scan.meta.sample_code]
        crate = write_scan(scan, args.out, layout=args.layout)
        print(f"{crate}  {scan.meta.scan_name}  {scan.mca.shape}")


if __name__ == "__main__":
    main()
