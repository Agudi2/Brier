import asyncio
import logging

import httpx
import orjson
import pytest

from recorder.rest import ForbiddenEndpoint, RestClient
from tests.recorder.helpers import FakeSigner
from tests.recorder.stage4_helpers import ManualTimer
from tests.recorder.ws_harness import BOOK, RANGES, Harness, recording


async def test_batched_public_subscriptions_handshake_metadata_and_every_server_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    harness = Harness(["A", "B", "C"])
    async with recording(harness) as task:
        await harness.initial(task)
        # Whitespace, a large integer, and decimal spelling must survive raw recording.
        extra = '{ "type":"future_public", "msg":{"x":9007199254740993,"p":0.1200} }'
        await harness.peer.send(1, extra)
        await harness.peer.send(
            1,
            {"type": "ticker", "sid": 99, "msg": {"market_ticker": "A", "price_dollars": "0.3000"}},
        )
        await harness.progress.until(
            task, lambda: len(harness.sink.rows("ws")) == len(harness.peer.sent)
        )
        harness.stop.set()
        await asyncio.wait_for(task, 2)
    commands = [command for _, command in harness.peer.commands]
    assert commands and all(command["cmd"] == "subscribe" for command in commands)
    ids = [command["id"] for command in commands]
    assert ids == sorted(set(ids))
    for command in commands:
        assert set(command["params"]) == {"channels", "market_tickers"}
        assert 1 <= len(command["params"]["market_tickers"]) <= 2
        assert set(command["params"]["channels"]) <= {"orderbook_delta", "trade", "ticker"}
    assert harness.signer.calls[0][1:] == ("GET", "/trade-api/ws/v2")
    headers = harness.peer.headers[0]
    assert headers["kalshi-access-key"] == "test-id"
    assert headers["kalshi-access-signature"] == "test-signature"
    sessions = harness.sink.rows("session")
    assert [(row["conn"], row["event"]) for row in sessions] == [(1, "open"), (1, "close")]
    assert all(row["pricing_mode"] == "no_leg" for row in sessions)
    rows = harness.sink.rows("ws")
    assert len(rows) == len(harness.peer.sent)
    assert [row["msg"] for row in rows] == [orjson.loads(raw) for raw in harness.peer.sent]
    for raw, line in zip(
        harness.peer.sent,
        [line for line in harness.sink.lines if orjson.loads(line)["kind"] == "ws"],
        strict=True,
    ):
        assert raw.encode() in line
    assert all(row["t_recv_ns"] == 100 and row["t_mono_ns"] == 200 for row in rows)
    metadata = harness.sink.rows("market_meta")
    assert {row["ticker"] for row in metadata} == {"A", "B", "C"}
    assert all(row["price_ranges"] == RANGES and row["event_ticker"] == "E" for row in metadata)
    assert harness.sink.rows("gap") == []  # A ticker message without seq does not create a gap.
    assert b"test-id" not in b"".join(harness.sink.lines)
    assert "test-signature" not in caplog.text and "test-id" not in caplog.text


async def test_gap_resubscribes_whole_sid_and_records_rest_and_fresh_snapshots() -> None:
    harness = Harness(["A", "B", "C"])
    async with recording(harness) as task:
        await harness.initial(task)
        _, sid, members = next(
            entry for entry in harness.peer.book_subs if set(entry[2]) == {"A", "B"}
        )
        assert set(members) == {"A", "B"}
        _, other_sid, _ = next(entry for entry in harness.peer.book_subs if entry[2] == ["C"])
        await harness.peer.send(
            1,
            {
                "type": "orderbook_delta",
                "sid": other_sid,
                "seq": 2,
                "msg": {
                    "market_ticker": "C",
                    "price_dollars": "0.3000",
                    "delta_fp": "1.00",
                    "side": "yes",
                },
            },
        )
        # A and B snapshots used seq 1 and 2 on the shared sid; seq 4 is missing seq 3.
        await harness.peer.send(
            1,
            {
                "type": "orderbook_delta",
                "sid": sid,
                "seq": 4,
                "msg": {
                    "market_ticker": "A",
                    "price_dollars": "0.3000",
                    "delta_fp": "-1.00",
                    "side": "yes",
                },
            },
        )

        def recovered() -> bool:
            all_rows = harness.sink.rows()
            gap_indices = [i for i, row in enumerate(all_rows) if row["kind"] == "gap"]
            if not gap_indices:
                return False
            after = all_rows[gap_indices[0] + 1 :]
            return {row["ticker"] for row in after if row["kind"] == "rest_book"} == {
                "A",
                "B",
            } and sum(
                row["kind"] == "ws" and row["msg"].get("type") == "orderbook_snapshot"
                for row in after
            ) >= 2

        await harness.progress.until(task, recovered)
        harness.stop.set()
        await asyncio.wait_for(task, 2)
    gaps = harness.sink.rows("gap")
    assert len(gaps) == 1
    assert (gaps[0]["sid"], gaps[0]["expected_seq"], gaps[0]["got_seq"]) == (sid, 3, 4)
    assert set(gaps[0]["tickers"]) == {"A", "B"}
    assert any(set(row["tickers"]) == {"A", "B"} for row in harness.sink.rows("resync"))
    assert any(
        command["cmd"] == "unsubscribe" and sid in command["params"]["sids"]
        for _, command in harness.peer.commands
    )
    assert sum(set(entry[2]) == {"A", "B"} for entry in harness.peer.book_subs) == 2
    assert sum(entry[2] == ["C"] for entry in harness.peer.book_subs) == 1
    assert all(row["book"] == BOOK for row in harness.sink.rows("rest_book"))


async def test_reconnect_resets_sid_sequences_and_resyncs_every_market() -> None:
    harness = Harness(["A", "B"])
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        if seconds == 0.25:
            delays.append(seconds)
            await asyncio.sleep(0)
        else:
            await asyncio.Future[None]()

    async with recording(harness, sleep=sleep) as task:
        await harness.initial(task)
        await harness.peer.connections[0].close(code=1012, reason="scripted reconnect")
        await harness.initial(task, conn=2)

        def resynced() -> bool:
            rows = harness.sink.rows()
            opens = [
                i
                for i, row in enumerate(rows)
                if row["kind"] == "session" and row["event"] == "open" and row["conn"] == 2
            ]
            return bool(opens) and {
                row["ticker"] for row in rows[opens[0] :] if row["kind"] == "rest_book"
            } == {"A", "B"}

        await harness.progress.until(task, resynced)
        harness.stop.set()
        await asyncio.wait_for(task, 2)
    assert delays == [0.25]
    assert [(row["conn"], row["event"]) for row in harness.sink.rows("session")] == [
        (1, "open"),
        (1, "close"),
        (2, "open"),
        (2, "close"),
    ]
    assert all(row["pricing_mode"] == "no_leg" for row in harness.sink.rows("session"))
    assert harness.sink.rows("gap") == []  # New connection sids start over at seq 1.
    assert any(set(row["tickers"]) == {"A", "B"} for row in harness.sink.rows("resync"))


async def test_periodic_rest_snapshots_have_send_and_receipt_window() -> None:
    harness = Harness(["A", "B"])
    timer = ManualTimer(harness.progress)
    async with recording(harness, sleep=timer.sleep) as task:
        await harness.initial(task)
        await harness.progress.until(
            task, lambda: any(delay == 60 and not future.done() for delay, future in timer.waiting)
        )
        baseline = len(harness.sink.rows("rest_book"))
        harness.wall_ns += 60_000_000_000
        harness.mono_ns += 60_000_000_000
        harness.rest_latency_ns = 15
        timer.release(60)
        await harness.progress.until(
            task, lambda: len(harness.sink.rows("rest_book")) >= baseline + 2
        )
        sampled = harness.sink.rows("rest_book")[baseline:]
        harness.stop.set()
        await asyncio.wait_for(task, 2)
    assert {row["ticker"] for row in sampled} == {"A", "B"}
    assert len(sampled) == 2
    assert all(row["t_recv_ns"] - row["t_send_ns"] == 15 and row["book"] == BOOK for row in sampled)


async def test_rest_sampling_accepts_real_decimal_ticker_shape() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=BOOK)

    async with httpx.AsyncClient(
        base_url="https://external-api.kalshi.com/trade-api/v2/",
        transport=httpx.MockTransport(handler),
    ) as http:
        try:
            response = await RestClient(http, FakeSigner()).request(
                "GET", "/markets/FED-23DEC-T3.00/orderbook"
            )
        except ForbiddenEndpoint:
            pytest.fail("documented decimal tickers must be allowed for REST validation snapshots")
    assert response.json() == BOOK
    assert calls == ["/trade-api/v2/markets/FED-23DEC-T3.00/orderbook"]
