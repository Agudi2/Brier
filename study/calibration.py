"""Offline command for aggregate calibration reports."""

import argparse
from collections.abc import Sequence
from pathlib import Path

import polars as pl

from study.breakdowns import parse_horizon
from study.dataset import _EXCLUSION_SCHEMA, _ROW_SCHEMA
from study.report import write_report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an aggregate calibration report")
    parser.add_argument("--input", type=Path, default=Path("data/study"))
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--horizons", nargs="+", default=["24h", "6h", "1h"])
    parser.add_argument("--min-n", type=int, default=30)
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    horizons = [parse_horizon(value) for value in args.horizons]
    files = sorted((args.input / "rows").glob("horizon_ns=*/*.parquet"))
    rows = (
        pl.concat([pl.read_parquet(path) for path in files])
        if files
        else pl.DataFrame(schema=_ROW_SCHEMA)
    )
    exclusions_path = args.input / "exclusions.parquet"
    exclusions = (
        pl.read_parquet(exclusions_path)
        if exclusions_path.exists()
        else pl.DataFrame(schema=_EXCLUSION_SCHEMA)
    )
    write_report(
        rows,
        exclusions,
        args.output,
        horizons_ns=horizons,
        min_n=args.min_n,
        n_resamples=args.bootstrap,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
