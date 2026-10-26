"""Publish aggregate-only calibration summaries and reliability diagrams."""

import json
import math
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import matplotlib
import polars as pl

from study.bootstrap import cluster_bootstrap
from study.breakdowns import breakdowns
from study.metrics import ReliabilityBin, brier_score, log_loss, reliability_bins

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402

_HOUR = 3_600_000_000_000
_MINUTE = 60_000_000_000
_SECOND = 1_000_000_000


def _label(horizon_ns: int) -> str:
    for unit, multiplier in (("h", _HOUR), ("m", _MINUTE), ("s", _SECOND)):
        if horizon_ns % multiplier == 0:
            return f"{horizon_ns // multiplier}{unit}"
    return f"{horizon_ns}ns"


def _format(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _chart(path: Path, label: str, item: dict[str, object], bins: Sequence[ReliabilityBin]) -> None:
    figure, axis = plt.subplots(figsize=(6, 4.5), dpi=100)
    axis.plot([0, 1], [0, 1], linestyle="--", color="#738497", label="Perfect calibration")
    axis.set(
        xlim=(0, 1),
        ylim=(0, 1),
        xlabel="Mean forecast probability",
        ylabel="Observed outcome rate",
        title=f"Reliability — {label}",
    )
    if item["status"] == "result":
        points = [entry for entry in bins if entry.n]
        axis.scatter(
            [entry.mean_p for entry in points if entry.mean_p is not None],
            [entry.observed for entry in points if entry.observed is not None],
            s=[35 + 8 * math.sqrt(entry.n) for entry in points],
            color="#175c83",
            label="Aggregate bins",
            zorder=3,
        )
        axis.legend(loc="upper left")
    else:
        axis.text(
            0.5,
            0.5,
            str(item["status"]),
            ha="center",
            va="center",
            fontsize=13,
            color="#526274",
            transform=axis.transAxes,
        )
    axis.grid(alpha=0.2)
    figure.tight_layout()
    figure.savefig(path, format="png")
    plt.close(figure)


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
    if any(not isinstance(h, int) or isinstance(h, bool) or h < 0 for h in horizons_ns) or len(
        set(horizons_ns)
    ) != len(horizons_ns):
        raise ValueError("horizons must be unique nonnegative integers")
    if not isinstance(min_n, int) or isinstance(min_n, bool) or min_n <= 0:
        raise ValueError("min_n must be positive")
    if not isinstance(n_resamples, int) or isinstance(n_resamples, bool) or n_resamples < 2:
        raise ValueError("n_resamples must be at least two")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be nonnegative")
    required = {"ticker", "event_ticker", "category", "horizon_ns", "p", "y"}
    if not required.issubset(rows.columns):
        raise ValueError("report rows lack required columns")
    if not {"reason", "count"}.issubset(exclusions.columns):
        raise ValueError("exclusion counts lack required columns")

    selected = rows.filter(pl.col("horizon_ns").is_in(list(horizons_ns)))
    sample = {
        "markets": selected["ticker"].n_unique(),
        "events": selected["event_ticker"].n_unique(),
    }
    exclusion_counts = {reason: int(count) for reason, count in exclusions.iter_rows()}
    breakdown_entries = [
        asdict(item)
        for item in breakdowns(selected, min_n=min_n, n_resamples=n_resamples, seed=seed)
    ]
    payload: dict[str, object] = {
        "sample": sample,
        "exclusions": exclusion_counts,
        "horizons": {},
        "breakdowns": breakdown_entries,
        "inference": {
            "event_sign_flip": "exact for up to 16 events; otherwise 9,999 seeded draws",
            "monte_carlo_min_p": 0.0001,
            "multiple_testing": "Holm across all eligible breakdown groups",
            "confidence": 0.95,
        },
    }
    horizon_summary: dict[str, dict[str, object]] = {}
    charts: dict[str, list[ReliabilityBin]] = {}
    for horizon in horizons_ns:
        label = _label(horizon)
        subset = selected.filter(pl.col("horizon_ns") == horizon)
        n = len(subset)
        item: dict[str, object] = {
            "n": n,
            "status": "result" if n >= min_n else ("no data" if n == 0 else "insufficient n"),
            "brier": None,
            "log_loss": None,
            "clipped": 0,
            "brier_ci_low": None,
            "brier_ci_high": None,
        }
        bins: list[ReliabilityBin] = []
        if n >= min_n:
            p = subset["p"].to_list()
            y = subset["y"].to_list()
            events = subset["event_ticker"].to_list()
            loss = log_loss(p, y)
            item.update(brier=brier_score(p, y), log_loss=loss.value, clipped=loss.clipped)
            bins = list(reliability_bins(p, y))
            if len(set(events)) >= 2:
                ci = cluster_bootstrap(
                    p, y, events, statistic="brier", n_resamples=n_resamples, seed=seed
                )
                item.update(brier_ci_low=ci.lower, brier_ci_high=ci.upper)
        item["bins"] = [asdict(entry) for entry in bins]
        horizon_summary[label] = item
        charts[label] = bins
    payload["horizons"] = horizon_summary

    lines = [
        "# Calibration study",
        "",
        f"Markets: {sample['markets']} · Events: {sample['events']}",
        "",
        "| Horizon | n | Status | Brier | Log loss | Clipped |",
        "| --- | ---: | --- | ---: | ---: | ---: |",
    ]
    for label, item in horizon_summary.items():
        lines.append(
            f"| {label} | {item['n']} | {item['status']} | "
            f"{_format(item['brier'])} | {_format(item['log_loss'])} | "
            f"{item['clipped']} |"
        )
    lines.extend(
        [
            "",
            "## Breakdowns",
            "",
            "A breakdown is labeled only when its Holm-adjusted p-value is at most 0.05.",
            "Intervals resample whole events. Sign-flip p-values assume event-level sign symmetry.",
            "",
            "| Dimension | Group | n | Status | Brier | Gap | 95% CI | p adjusted | Finding |",
            "| --- | --- | ---: | --- | ---: | ---: | --- | ---: | --- |",
        ]
    )
    for entry in breakdown_entries:
        interval = f"{_format(entry['ci_low'])} to {_format(entry['ci_high'])}"
        finding = "breaks down" if entry["breaks_down"] else ""
        lines.append(
            f"| {entry['dimension']} | {entry['group']} | {entry['n']} | "
            f"{entry['status']} | {_format(entry['brier'])} | {_format(entry['gap'])} | "
            f"{interval} | {_format(entry['p_adjusted'])} | {finding} |"
        )
    lines.extend(["", "## Exclusions", ""])
    for reason, count in exclusion_counts.items():
        lines.append(f"- {reason}: {count}")
    lines.extend(
        [
            "",
            "These are descriptive calibration diagnostics, not trading returns or "
            "profitability estimates.",
            "",
        ]
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temp:
        staging = Path(temp)
        (staging / "summary.json").write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n")
        (staging / "summary.md").write_text("\n".join(lines))
        for label, item in horizon_summary.items():
            _chart(staging / f"reliability_{label}.png", label, item, charts[label])
        output.mkdir(parents=True, exist_ok=True)
        owned = {"summary.json", "summary.md"} | {
            f"reliability_{label}.png" for label in horizon_summary
        }
        for path in staging.iterdir():
            os.replace(path, output / path.name)
        for stale in output.glob("reliability_*.png"):
            if stale.name not in owned:
                stale.unlink()
    return payload
