"""Capture settled markets and exact trade history through read-only API endpoints."""

import calendar
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from brierlib.fixed_point import parse_price
from recorder.rest import RestClient
from study.dataset import Market, Trade

_ISO = re.compile(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$")


def _timestamp_ns(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO 8601 string")
    match = _ISO.fullmatch(value)
    if match is None:
        raise ValueError("timestamp must include an explicit UTC offset")
    date, time, fraction, zone = match.groups()
    try:
        parsed = datetime.strptime(f"{date}T{time}", "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    seconds = calendar.timegm(parsed.timetuple())
    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        hours, minutes = map(int, zone[1:].split(":"))
        if hours > 23 or minutes > 59:
            raise ValueError("invalid timestamp offset")
        seconds -= sign * (hours * 3600 + minutes * 60)
    return seconds * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


async def _request_object(client: RestClient, path: str, key: str) -> dict[str, Any]:
    response = await client.request("GET", path)
    body = _object(response.json(), path)
    return _object(body.get(key), f"{path}.{key}")


def _cache_path(client: RestClient, cache_dir: Path) -> Path:
    # The API origin belongs to the capture identity; headers and credentials do not.
    origin = str(client._http.base_url).rstrip("/")  # noqa: SLF001
    digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:20]
    return cache_dir / f"market-data-{digest}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    envelope = _object(json.loads(path.read_text()), "cache")
    if envelope.get("version") != 1 or envelope.get("complete") is not True:
        return None
    return _object(envelope.get("data"), "cache.data")


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".backfill-",
            suffix=".json",
            dir=path.parent,
            delete=False,
        ) as stream:
            temp = Path(stream.name)
            json.dump({"version": 1, "complete": True, "data": data}, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


async def _capture(client: RestClient) -> dict[str, Any]:
    cutoff = _object((await client.request("GET", "/historical/cutoff")).json(), "cutoff")
    # The two archive boundaries move separately; read both before capturing tiers.
    for field in ("market_settled_ts", "trades_created_ts"):
        if field not in cutoff:
            raise ValueError(f"historical cutoff missing {field}")
    archived = await client.paginate("/historical/markets", "markets")
    live = await client.paginate("/markets", "markets", params={"status": "settled"})
    by_ticker: dict[str, dict[str, Any]] = {}
    for raw in archived + live:
        ticker = _string(raw.get("ticker"), "market.ticker")
        old = by_ticker.setdefault(ticker, raw)
        if old != raw:
            raise ValueError(f"conflicting market data for {ticker}")
    events: dict[str, dict[str, Any]] = {}
    series: dict[str, dict[str, Any]] = {}
    trades: dict[str, list[dict[str, Any]]] = {}
    for ticker, raw in sorted(by_ticker.items()):
        event_ticker = _string(raw.get("event_ticker"), "market.event_ticker")
        if event_ticker not in events:
            events[event_ticker] = await _request_object(client, f"/events/{event_ticker}", "event")
        series_ticker = _string(events[event_ticker].get("series_ticker"), "event.series_ticker")
        if series_ticker not in series:
            series[series_ticker] = await _request_object(
                client, f"/series/{series_ticker}", "series"
            )
        # Trade age is independent of settlement age: fetch both for every market.
        old_trades = await client.paginate(
            "/historical/trades", "trades", params={"ticker": ticker}
        )
        new_trades = await client.paginate("/markets/trades", "trades", params={"ticker": ticker})
        trades[ticker] = old_trades + new_trades
    return {
        "cutoff": cutoff,
        "markets": by_ticker,
        "events": events,
        "series": series,
        "trades": trades,
    }


def _normalize(raw_data: dict[str, Any], scheduled_closes: Mapping[str, int]) -> list[Market]:
    raw_markets = _object(raw_data.get("markets"), "cache.markets")
    raw_events = _object(raw_data.get("events"), "cache.events")
    raw_series = _object(raw_data.get("series"), "cache.series")
    raw_trades = _object(raw_data.get("trades"), "cache.trades")
    result: list[Market] = []
    for ticker, raw_value in sorted(raw_markets.items()):
        raw = _object(raw_value, "market")
        event_ticker = _string(raw.get("event_ticker"), "market.event_ticker")
        event = _object(raw_events.get(event_ticker), "event")
        series_ticker = _string(event.get("series_ticker"), "event.series_ticker")
        series = _object(raw_series.get(series_ticker), "series")
        category = event.get("category") or series.get("category")
        if not isinstance(category, str):
            raise ValueError("event category is missing")
        raw_rows = raw_trades.get(ticker)
        if not isinstance(raw_rows, list):
            raise ValueError(f"trades missing for {ticker}")
        by_id: dict[str, Trade] = {}
        for raw_trade in raw_rows:
            entry = _object(raw_trade, "trade")
            if entry.get("ticker") != ticker:
                raise ValueError("trade belongs to a different market")
            trade_id = _string(entry.get("trade_id"), "trade.trade_id")
            price = entry.get("yes_price_dollars")
            if not isinstance(price, str):
                raise ValueError("trade YES price is missing")
            trade = Trade(_timestamp_ns(entry.get("created_time")), parse_price(price), trade_id)
            previous = by_id.setdefault(trade_id, trade)
            if previous != trade:
                raise ValueError(f"conflicting trade ID: {trade_id}")
        close = raw.get("close_time")
        anchor = scheduled_closes.get(ticker)
        if anchor is not None and (not isinstance(anchor, int) or isinstance(anchor, bool)):
            raise ValueError("scheduled close must be integer Unix nanoseconds")
        outcome = raw.get("result")
        if not isinstance(outcome, str):
            raise ValueError("market result is missing")
        result.append(
            Market(
                ticker=ticker,
                event_ticker=event_ticker,
                series_ticker=series_ticker,
                category=category,
                result=outcome,
                scheduled_close_ns=anchor,
                actual_close_ns=_timestamp_ns(close) if close is not None else None,
                trades=tuple(sorted(by_id.values(), key=lambda t: (t.timestamp_ns, t.trade_id))),
            )
        )
    return result


async def backfill(
    client: RestClient,
    cache_dir: Path,
    *,
    scheduled_closes: Mapping[str, int],
) -> list[Market]:
    """Capture settled market data once, then renormalize with caller-supplied anchors."""
    cache = _cache_path(client, cache_dir)
    raw = _read_cache(cache)
    if raw is None:
        raw = await _capture(client)
        # A failed normalization must not be published as a complete capture.
        normalized = _normalize(raw, scheduled_closes)
        _write_cache(cache, raw)
        return normalized
    return _normalize(raw, scheduled_closes)


def _duration_ns(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)([smhd])", value)
    if match is None:
        raise ValueError("duration must be a nonnegative integer followed by s, m, h, or d")
    amount, unit = match.groups()
    return int(amount) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * 1_000_000_000


def _scheduled_closes(path: Path | None) -> dict[str, int]:
    if path is None:
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("scheduled closes file must be a ticker-to-time JSON object")
    anchors: dict[str, int] = {}
    for ticker, raw in value.items():
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("scheduled close ticker must be nonempty")
        if isinstance(raw, str):
            anchors[ticker] = _timestamp_ns(raw)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            anchors[ticker] = raw
        else:
            raise ValueError("scheduled close must be an ISO timestamp or integer Unix ns")
    return anchors


async def _run_cli(args: Any, anchors: dict[str, int]) -> tuple[int, int, int | None]:
    import httpx

    from recorder.auth import Credentials
    from recorder.ratelimit import TokenBucket
    from study.dataset import build_book_dataset, build_dataset, read_tob, write_dataset

    credentials = Credentials.from_env()
    async with httpx.AsyncClient(base_url=args.rest_url.rstrip("/") + "/", timeout=30) as http:
        client = RestClient(
            http,
            credentials,
            limiter=TokenBucket(rate=args.token_rate, capacity=args.token_capacity),
        )
        markets = await backfill(client, args.cache_dir, scheduled_closes=anchors)
    missing = sum(m.scheduled_close_ns is None for m in markets)
    rows: int | None = None
    if args.output is not None:
        horizons = [_duration_ns(horizon) for horizon in args.horizons]
        lookback = _duration_ns(args.lookback)
        if args.tob_dir is None:
            dataset = build_dataset(markets, horizons, lookback)
        else:
            quotes = read_tob(args.tob_dir / "tob.bin", args.tob_dir / "markets.json")
            dataset = build_book_dataset(markets, quotes, horizons, lookback, args.max_spread_ticks)
        write_dataset(dataset, args.output)
        rows = dataset.rows.height
    return len(markets), missing, rows


def main(argv: list[str] | None = None) -> int:
    """Capture a reusable market-data snapshot and optionally materialize a study dataset."""
    import argparse
    import asyncio
    import sys

    import httpx

    parser = argparse.ArgumentParser(
        description="Backfill settled market data, then build study rows"
    )
    parser.add_argument("--cache-dir", type=Path, default=Path("data/backfill"))
    parser.add_argument(
        "--scheduled-closes", type=Path, help="JSON ticker-to-original-schedule map"
    )
    parser.add_argument("--output", type=Path, help="Dataset directory for partitioned Parquet")
    parser.add_argument("--horizons", nargs="+", default=["24h", "6h", "1h"])
    parser.add_argument("--lookback", default="1h")
    parser.add_argument("--tob-dir", type=Path, help="Replay output with tob.bin and markets.json")
    parser.add_argument("--max-spread-ticks", type=int, default=200)
    parser.add_argument("--token-rate", type=float, default=50.0)
    parser.add_argument("--token-capacity", type=float, default=10.0)
    parser.add_argument("--rest-url", default="https://external-api.kalshi.com/trade-api/v2")
    args = parser.parse_args(argv)
    if args.output is not None and args.scheduled_closes is None:
        parser.error("--output requires --scheduled-closes with original scheduled times")
    if args.tob_dir is not None and args.output is None:
        parser.error("--tob-dir requires --output")
    try:
        anchors = _scheduled_closes(args.scheduled_closes)
        count, missing, rows = asyncio.run(_run_cli(args, anchors))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        print(f"Backfill failed: {error}", file=sys.stderr)
        return 2
    print(f"Captured {count} settled markets; {missing} lack a scheduled-close anchor")
    if rows is not None:
        print(f"Wrote {rows} study rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
