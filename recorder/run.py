"""Command-line recording and graceful signal shutdown."""

import argparse
import asyncio
import importlib
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import httpx

from recorder.auth import Credentials
from recorder.config import MarketConfig, load_config
from recorder.ratelimit import TokenBucket
from recorder.rest import RestClient
from recorder.writer import NdjsonWriter
from recorder.ws import Recorder


class RunnableRecorder(Protocol):
    async def run(self, stop: asyncio.Event) -> None: ...


async def resolve_markets(config: MarketConfig, rest: RestClient) -> list[str]:
    tickers = dict.fromkeys(config.tickers)
    for parameter, selectors in (("series_ticker", config.series), ("event_ticker", config.events)):
        for selector in selectors:
            rows = await rest.paginate("/markets", "markets", params={parameter: selector})
            for row in rows:
                ticker = row.get("ticker")
                if not isinstance(ticker, str) or not ticker:
                    raise ValueError("Market selector returned an invalid ticker")
                tickers[ticker] = None
    if not tickers:
        raise ValueError("Market selectors did not resolve to any markets")
    return list(tickers)


async def capture(
    recorder: RunnableRecorder,
    writer: NdjsonWriter,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Start writer and recorder, install SIGINT/SIGTERM when stop is absent, drain on exit."""
    loop = asyncio.get_running_loop()
    install_signals = stop is None
    event = stop if stop is not None else asyncio.Event()
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    installed: list[signal.Signals] = []
    try:
        if install_signals:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, event.set)
                installed.append(signum)
        await writer.start()
        try:
            await recorder.run(event)
        finally:
            await writer.close()
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
            signal.signal(signum, previous[signum])


async def _record(args: argparse.Namespace, config: MarketConfig) -> None:
    credentials = Credentials.from_env()
    async with httpx.AsyncClient(base_url=args.rest_url.rstrip("/") + "/", timeout=30) as http:
        rest = RestClient(
            http,
            credentials,
            limiter=TokenBucket(rate=args.token_rate, capacity=args.token_capacity),
        )
        tickers = await resolve_markets(config, rest)
        writer = NdjsonWriter(args.output)
        recorder = Recorder(
            args.ws_url,
            tickers,
            writer,
            rest,
            credentials,
            batch_size=args.batch_size,
            snapshot_interval=args.snapshot_interval,
        )
        await capture(recorder, writer)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record public Kalshi market data as raw NDJSON")
    parser.add_argument("--markets", type=Path, required=True, help="YAML market selectors")
    parser.add_argument("--output", type=Path, default=Path("data"), help="Raw NDJSON directory")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--snapshot-interval", type=float, default=60)
    parser.add_argument("--token-rate", type=float, default=50)
    parser.add_argument("--token-capacity", type=float, default=10)
    parser.add_argument("--rest-url", default="https://external-api.kalshi.com/trade-api/v2")
    parser.add_argument("--ws-url", default="wss://external-api-ws.kalshi.com/trade-api/ws/v2")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.markets)
        try:
            uvloop = importlib.import_module("uvloop")
        except ModuleNotFoundError:
            asyncio.run(_record(args, config))
        else:
            uvloop.run(_record(args, config))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        print(f"Recording failed: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
