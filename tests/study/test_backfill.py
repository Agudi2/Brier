"""Offline API pages exercise archival coverage, caching, and exact normalization."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from recorder.ratelimit import TokenBucket
from recorder.rest import PaginationError, RestClient
from study.backfill import backfill
from tests.recorder.helpers import FakeClock, FakeSigner
from tests.study.helpers import ready_async


class Archive:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.offline = False
        self.fail_trade = False
        self.loop = False
        self.price = "0.1234"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert not self.offline, "A complete cache must make zero requests"
        assert request.method == "GET"
        self.calls.append(request)
        path = request.url.path.removeprefix("/trade-api/v2")
        cursor = request.url.params.get("cursor", "")
        if path == "/historical/cutoff":
            # Independent cutoffs: market settlement 600 seconds, trades 800 seconds.
            return httpx.Response(
                200,
                json={
                    "market_settled_ts": "1970-01-01T00:10:00Z",
                    "trades_created_ts": "1970-01-01T00:13:20Z",
                },
            )
        if path in ("/historical/markets", "/markets"):
            if path == "/markets":
                assert request.url.params.get("status") == "settled"
            ticker = "OLD" if path == "/historical/markets" else "NEW"
            if cursor:
                assert cursor == "next"
                return httpx.Response(
                    200, json={"markets": [], "cursor": "next" if self.loop else ""}
                )
            raw: dict[str, Any] = {
                "ticker": ticker,
                "event_ticker": "EVENT",
                "status": "finalized",
                "result": "yes" if ticker == "OLD" else "no",
                "market_type": "binary",
                "close_time": "1970-01-01T00:08:20Z",  # Actual early close = 500 seconds.
                "expiration_time": "1970-01-01T00:09:00Z",
                "expected_expiration_time": "1970-01-01T00:09:30Z",
                "latest_expiration_time": "1970-01-01T00:20:00Z",
                "can_close_early": True,
            }
            return httpx.Response(200, json={"markets": [raw], "cursor": "next"})
        if path == "/events/EVENT":
            return httpx.Response(
                200,
                json={
                    "event": {
                        "event_ticker": "EVENT",
                        "series_ticker": "SERIES",
                        "category": "Economics",
                    }
                },
            )
        if path == "/series/SERIES":
            return httpx.Response(
                200, json={"series": {"ticker": "SERIES", "category": "Economics"}}
            )
        if path in ("/historical/trades", "/markets/trades"):
            if self.fail_trade:
                return httpx.Response(500)
            ticker = request.url.params.get("ticker")
            assert ticker in ("OLD", "NEW"), "Fetch history for each selected market"
            if cursor:
                assert cursor == "next"
                return httpx.Response(200, json={"trades": [], "cursor": ""})
            historical = path == "/historical/trades"
            # Even a recently settled market has trades before the trade cutoff.
            row = {
                "ticker": ticker,
                "trade_id": f"{ticker}-{'old' if historical else 'new'}",
                "created_time": "1970-01-01T00:11:40.123456789Z"
                if historical
                else "1970-01-01T00:14:10Z",
                "yes_price_dollars": self.price if historical else "0.8000",
                "no_price_dollars": "0.8766" if historical else "0.2000",
                "count_fp": "2.00",
                "taker_side": "no",
            }
            return httpx.Response(200, json={"trades": [row, row], "cursor": "next"})
        pytest.fail(f"Unexpected endpoint: {path}")


def client(http: httpx.AsyncClient, clock: FakeClock) -> RestClient:
    return RestClient(http, FakeSigner(), limiter=TokenBucket(clock=clock, sleep=clock.sleep))


async def test_both_storage_tiers_pagination_yes_prices_and_disk_cache(tmp_path: Path) -> None:
    archive, clock = Archive(), FakeClock()
    anchors = {"OLD": 1000_000_000_000, "NEW": 1100_000_000_000}
    async with httpx.AsyncClient(
        base_url="https://example.test/trade-api/v2", transport=httpx.MockTransport(archive)
    ) as http:
        result = await ready_async(
            backfill(client(http, clock), tmp_path, scheduled_closes=anchors)
        )
        assert [m.ticker for m in result] == ["NEW", "OLD"]
        for m in result:
            assert (m.event_ticker, m.series_ticker, m.category) == ("EVENT", "SERIES", "Economics")
            assert m.scheduled_close_ns == anchors[m.ticker]
            assert m.actual_close_ns == 500_000_000_000
            # 700 seconds + 123456789 ns; 0.1234 dollars * 10000 = 1234 ticks.
            assert [(t.timestamp_ns, t.yes_price_ticks) for t in m.trades] == [
                (700_123_456_789, 1234),
                (850_000_000_000, 8000),
            ]
        assert [m.result for m in result] == ["no", "yes"]
        paths = {r.url.path.removeprefix("/trade-api/v2") for r in archive.calls}
        assert {
            "/historical/cutoff",
            "/historical/markets",
            "/markets",
            "/historical/trades",
            "/markets/trades",
        } <= paths
        assert clock.sleeps, "Backfill must use the existing rate-limited REST client"
        archive.offline = True
        # New client instance and same directory proves cache isn't only in memory.
        again = await ready_async(
            backfill(client(http, FakeClock()), tmp_path, scheduled_closes=anchors)
        )
        assert again == result
        # Anchors are caller-supplied normalization inputs, not frozen into the raw cache.
        missing = await ready_async(
            backfill(client(http, FakeClock()), tmp_path, scheduled_closes={})
        )
        assert all(m.scheduled_close_ns is None for m in missing)


async def test_no_implicit_scheduled_time_fallback(tmp_path: Path) -> None:
    archive = Archive()
    async with httpx.AsyncClient(
        base_url="https://example.test/trade-api/v2", transport=httpx.MockTransport(archive)
    ) as http:
        result = await ready_async(
            backfill(client(http, FakeClock()), tmp_path, scheduled_closes={})
        )
    assert len(result) == 2
    assert all(m.scheduled_close_ns is None for m in result)


async def test_failed_download_is_not_a_complete_cache(tmp_path: Path) -> None:
    archive = Archive()
    archive.fail_trade = True
    async with httpx.AsyncClient(
        base_url="https://example.test/trade-api/v2", transport=httpx.MockTransport(archive)
    ) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await ready_async(backfill(client(http, FakeClock()), tmp_path, scheduled_closes={}))
        archive.fail_trade = False
        result = await ready_async(
            backfill(client(http, FakeClock()), tmp_path, scheduled_closes={})
        )
        assert len(result) == 2 and all(len(m.trades) == 2 for m in result)


async def test_cursor_loop_is_not_silently_truncated(tmp_path: Path) -> None:
    archive = Archive()
    archive.loop = True
    async with httpx.AsyncClient(
        base_url="https://example.test/trade-api/v2", transport=httpx.MockTransport(archive)
    ) as http:
        with pytest.raises(PaginationError):
            await ready_async(backfill(client(http, FakeClock()), tmp_path, scheduled_closes={}))


@pytest.mark.parametrize("bad_price", ["0.12345", "NaN", "1.0001", "-0.1"])
async def test_malformed_prices_rejected_without_rounding(tmp_path: Path, bad_price: str) -> None:
    archive = Archive()
    archive.price = bad_price
    async with httpx.AsyncClient(
        base_url="https://example.test/trade-api/v2", transport=httpx.MockTransport(archive)
    ) as http:
        with pytest.raises(ValueError):
            await ready_async(backfill(client(http, FakeClock()), tmp_path, scheduled_closes={}))
