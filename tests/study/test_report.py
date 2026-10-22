"""Aggregate-only report artifacts and the offline calibration command."""

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from study.calibration import main
from study.dataset import Dataset, write_dataset
from study.report import write_report
from tests.study.stage12_helpers import ready

DAY = 86_400_000_000_000


def sample() -> Dataset:
    rows = pl.DataFrame(
        {
            "ticker": [f"LEAK_TICKER_{i}" for i in range(4)],
            "event_ticker": ["LEAK_EVENT_0", "LEAK_EVENT_0", "LEAK_EVENT_1", "LEAK_EVENT_1"],
            "series_ticker": ["LEAK_SERIES"] * 4,
            "category": ["Economics", "Economics", "Politics", "Politics"],
            "scheduled_close_ns": [10 * DAY, 10 * DAY, 11 * DAY, 11 * DAY],
            "horizon_ns": [DAY] * 4,
            "price_timestamp_ns": [9 * DAY - 1] * 2 + [10 * DAY - 1] * 2,
            "price_ticks": [3000.0, 8000.0, 2000.0, 7000.0],
            "p": [0.3, 0.8, 0.2, 0.7],
            "y": [0, 1, 0, 1],
        }
    )
    exclusions = pl.DataFrame(
        {"reason": ["void", "no_trade_in_window"], "count": [1, 2]},
        schema={"reason": pl.String, "count": pl.UInt64},
    )
    return Dataset(rows, exclusions)


def _assert_aggregate_only(payload: Any, forbidden: set[str]) -> None:
    if isinstance(payload, dict):
        assert not (set(payload) & forbidden)
        for value in payload.values():
            _assert_aggregate_only(value, forbidden)
    elif isinstance(payload, list):
        for value in payload:
            _assert_aggregate_only(value, forbidden)


def _png(path: Path) -> tuple[int, int]:
    content = path.read_bytes()
    assert content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) > 500
    assert content[12:16] == b"IHDR"
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    assert width >= 320 and height >= 240
    return width, height


def test_report_has_hand_computed_global_metrics_and_no_row_data(tmp_path: Path) -> None:
    data = sample()
    output = tmp_path / "reports"
    returned = ready(
        write_report,
        data.rows,
        data.exclusions,
        output,
        horizons_ns=[DAY],
        min_n=2,
        n_resamples=31,
        seed=7,
    )
    payload = json.loads((output / "summary.json").read_text())
    assert returned == payload
    assert set(payload["horizons"]) == {"24h"}
    item = payload["horizons"]["24h"]
    # (.3^2+.2^2+.2^2+.3^2)/4=.065.
    assert item["n"] == 4 and item["brier"] == pytest.approx(0.065)
    assert item["clipped"] == 0
    assert payload["exclusions"] == {"no_trade_in_window": 2, "void": 1}
    assert payload["sample"]["markets"] == 4
    assert payload["sample"]["events"] == 2
    forbidden = {
        "ticker",
        "market_ticker",
        "event_ticker",
        "series_ticker",
        "trade_id",
        "market_id",
        "price_ticks",
        "price_timestamp_ns",
        "scheduled_close_ns",
        "p",
        "y",
        "rows",
    }
    _assert_aggregate_only(payload, forbidden)
    json_text = (output / "summary.json").read_text()
    markdown = (output / "summary.md").read_text()
    for secret in ("LEAK_TICKER_0", "LEAK_EVENT_0", "LEAK_SERIES"):
        assert secret not in json_text and secret not in markdown
    assert "0.065" in markdown and "24h" in markdown
    _png(output / "reliability_24h.png")


def test_empty_horizons_are_null_and_rerun_removes_stale_chart(tmp_path: Path) -> None:
    data = sample()
    output = tmp_path / "reports"
    ready(
        write_report,
        data.rows,
        data.exclusions,
        output,
        horizons_ns=[DAY, 6 * 3_600_000_000_000, 3_600_000_000_000],
        min_n=2,
        n_resamples=31,
        seed=7,
    )
    assert all((output / f"reliability_{h}.png").exists() for h in ("24h", "6h", "1h"))
    summary = json.loads((output / "summary.json").read_text())
    for horizon in ("6h", "1h"):
        assert summary["horizons"][horizon]["n"] == 0
        assert summary["horizons"][horizon]["brier"] is None
        _png(output / f"reliability_{horizon}.png")
    ready(
        write_report,
        data.rows,
        data.exclusions,
        output,
        horizons_ns=[DAY],
        min_n=2,
        n_resamples=31,
        seed=7,
    )
    assert not (output / "reliability_6h.png").exists()
    assert not (output / "reliability_1h.png").exists()
    assert set(json.loads((output / "summary.json").read_text())["horizons"]) == {"24h"}


def test_small_horizon_reports_insufficient_n_instead_of_score(tmp_path: Path) -> None:
    data = sample()
    output = tmp_path / "reports"
    ready(
        write_report,
        data.rows,
        data.exclusions,
        output,
        horizons_ns=[DAY],
        min_n=5,
        n_resamples=31,
        seed=7,
    )
    item = json.loads((output / "summary.json").read_text())["horizons"]["24h"]
    assert item["n"] == 4 and item["status"] == "insufficient n"
    assert item["brier"] is None and item["log_loss"] is None
    assert "insufficient n" in (output / "summary.md").read_text().lower()


@pytest.mark.parametrize("horizons,min_n", [([DAY, DAY], 2), ([-DAY], 2), ([DAY], 0)])
def test_report_rejects_invalid_options(tmp_path: Path, horizons: list[int], min_n: int) -> None:
    data = sample()
    with pytest.raises(ValueError):
        ready(
            write_report,
            data.rows,
            data.exclusions,
            tmp_path / "reports",
            horizons_ns=horizons,
            min_n=min_n,
        )


def test_command_reads_partitioned_dataset_and_writes_requested_horizons(tmp_path: Path) -> None:
    source = tmp_path / "study"
    write_dataset(sample(), source)
    output = tmp_path / "reports"
    code = ready(
        main,
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--horizons",
            "24h",
            "6h",
            "1h",
            "--min-n",
            "2",
            "--bootstrap",
            "31",
            "--seed",
            "7",
        ],
    )
    assert code == 0
    summary = json.loads((output / "summary.json").read_text())
    assert set(summary["horizons"]) == {"24h", "6h", "1h"}
    for horizon in ("24h", "6h", "1h"):
        _png(output / f"reliability_{horizon}.png")
    assert (output / "summary.md").is_file()
    assert summary["sample"]["markets"] == 4
