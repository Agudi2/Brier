"""Validated selectors for read-only market-data recording."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MarketConfig:
    tickers: tuple[str, ...]
    series: tuple[str, ...]
    events: tuple[str, ...]


def load_config(path: str | Path) -> MarketConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError:
        raise ValueError("Invalid YAML in market configuration") from None
    if raw is None:
        raise ValueError("At least one market selector is required")
    if not isinstance(raw, dict):
        raise ValueError("Market configuration must be a mapping")
    names = ("tickers", "series", "events")
    unknown = [str(name) for name in raw if name not in names]
    if unknown:
        raise ValueError("Unknown market selector: " + ", ".join(unknown))
    selectors: dict[str, tuple[str, ...]] = {}
    for name in names:
        values = raw.get(name, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"{name} must be a list of nonempty strings")
        selectors[name] = tuple(values)
    if not any(selectors.values()):
        raise ValueError("At least one market selector is required")
    return MarketConfig(**selectors)
