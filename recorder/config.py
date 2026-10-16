"""Market selector configuration (Stage 3 RED stubs)."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MarketConfig:
    tickers: tuple[str, ...]
    series: tuple[str, ...]
    events: tuple[str, ...]


def load_config(path: str | Path) -> MarketConfig:
    raise NotImplementedError("Stage 3 GREEN: load_config")
