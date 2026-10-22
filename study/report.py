"""Aggregate report interface; deliberate RED stub."""

from collections.abc import Sequence
from pathlib import Path

import polars as pl


def write_report(
    rows: pl.DataFrame,
    exclusions: pl.DataFrame,
    output: Path,
    *,
    horizons_ns: Sequence[int],
    min_n: int = 30,
    n_resamples: int = 2000,
    seed: int = 0,
) -> dict[str, object]:
    raise NotImplementedError
