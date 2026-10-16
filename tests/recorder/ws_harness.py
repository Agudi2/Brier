"""Loopback protocol peer and mocked REST market-data service."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
import orjson
import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from recorder.ratelimit import Backoff, TokenBucket
from recorder.rest import RestClient
from recorder.ws import Recorder
from tests.recorder.helpers import FakeSigner
from tests.recorder.stage4_helpers import MemorySink, Progress, await4

BOOK = {"orderbook_fp": {"yes_dollars": [["0.3000", "1.55"]], "no_dollars": [["0.6800", "2.00"]]}}
RANGES = [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]


class Peer:
    def __init__(self, progress: Progress) -> None:
        self.progress = progress
        self.connections: list[ServerConnection] = []
        self.commands: list[tuple[int, dict[str, Any]]] = []
        self.sent: list[str] = []
        self.members: dict[tuple[int, int], list[str]] = {}
        self.book_subs: list[tuple[int, int, list[str]]] = []
        self.seq: dict[tuple[int, int], int] = {}
        self.headers: list[dict[str, str]] = []
        self.errors: list[Exception] = []

    async def send(self, conn: int, message: dict[str, Any] | str) -> None:
        raw = message if isinstance(message, str) else orjson.dumps(message).decode()
        self.sent.append(raw)
        await self.connections[conn - 1].send(raw)
        self.progress.changed.set()

    async def handler(self, ws: ServerConnection) -> None:
        self.connections.append(ws)
        conn = len(self.connections)
        assert ws.request is not None
        self.headers.append(dict(ws.request.headers))
        sid = 0
        try:
            async for raw in ws:
                command = orjson.loads(raw)
                self.commands.append((conn, command))
                if command["cmd"] == "subscribe":
                    params = command["params"]
                    tickers = params.get("market_tickers", [params.get("market_ticker")])
                    for channel in params["channels"]:
                        sid += 1
                        self.members[conn, sid] = tickers
                        self.seq[conn, sid] = 0
                        await self.send(
                            conn,
                            {
                                "id": command["id"],
                                "type": "subscribed",
                                "msg": {"channel": channel, "sid": sid},
                            },
                        )
                        if channel == "orderbook_delta":
                            self.book_subs.append((conn, sid, tickers))
                            for ticker in tickers:
                                self.seq[conn, sid] += 1
                                await self.send(
                                    conn,
                                    {
                                        "type": "orderbook_snapshot",
                                        "sid": sid,
                                        "seq": self.seq[conn, sid],
                                        "msg": {
                                            "market_ticker": ticker,
                                            "yes_dollars_fp": [["0.3000", "1.55"]],
                                            "no_dollars_fp": [["0.6800", "2.00"]],
                                        },
                                    },
                                )
                elif command["cmd"] == "unsubscribe":
                    for old_sid in command["params"]["sids"]:
                        await self.send(
                            conn, {"id": command["id"], "type": "unsubscribed", "sid": old_sid}
                        )
                else:
                    raise AssertionError("unexpected protocol command")
                self.progress.changed.set()
        except ConnectionClosed:
            pass
        except Exception as error:
            self.errors.append(error)
            self.progress.changed.set()
            raise

    def coverage(self, conn: int) -> set[tuple[str, str]]:
        return {
            (channel, ticker)
            for connection, command in self.commands
            if connection == conn and command["cmd"] == "subscribe"
            for channel in command["params"]["channels"]
            for ticker in command["params"].get(
                "market_tickers", [command["params"].get("market_ticker")]
            )
        }


class Harness:
    def __init__(self, tickers: Sequence[str]) -> None:
        self.tickers = tickers
        self.progress = Progress()
        self.sink = MemorySink(self.progress)
        self.peer = Peer(self.progress)
        self.signer = FakeSigner()
        self.stop = asyncio.Event()
        self.rest_paths: list[str] = []
        self.wall_ns = 100
        self.mono_ns = 200
        self.rest_latency_ns = 0

    def rest_handler(self, request: httpx.Request) -> httpx.Response:
        self.rest_paths.append(request.url.path)
        self.wall_ns += self.rest_latency_ns
        self.mono_ns += self.rest_latency_ns
        assert len(self.rest_paths) < 200, "uncontrolled REST sampling loop"
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json=BOOK)
        ticker = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200, json={"market": {"ticker": ticker, "event_ticker": "E", "price_ranges": RANGES}}
        )

    async def initial(self, task: asyncio.Task[None], conn: int = 1) -> None:
        expected = {
            (channel, ticker)
            for channel in ("orderbook_delta", "trade", "ticker")
            for ticker in self.tickers
        }
        await self.progress.until(
            task,
            lambda: (
                expected <= self.peer.coverage(conn)
                and sum(
                    row["msg"].get("type") == "orderbook_snapshot" and row["conn"] == conn
                    for row in self.sink.rows("ws")
                )
                >= len(self.tickers)
                and {row["ticker"] for row in self.sink.rows("market_meta")} == set(self.tickers)
                and {row["ticker"] for row in self.sink.rows("rest_book")} == set(self.tickers)
            ),
        )
        assert self.peer.errors == []


@asynccontextmanager
async def recording(
    harness: Harness,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[asyncio.Task[None]]:
    server_logger = logging.getLogger("tests.stage4.server")
    server_logger.setLevel(logging.INFO)
    async with serve(harness.peer.handler, "127.0.0.1", 0, logger=server_logger) as server:
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(
            base_url="https://external-api.kalshi.com/trade-api/v2/",
            transport=httpx.MockTransport(harness.rest_handler),
        ) as http:
            rest = RestClient(http, FakeSigner(), limiter=TokenBucket(rate=100000, capacity=100000))
            recorder = Recorder(
                f"ws://127.0.0.1:{port}/trade-api/ws/v2",
                harness.tickers,
                harness.sink,
                rest,
                harness.signer,
                batch_size=2,
                snapshot_interval=60,
                clock_ns=lambda: harness.wall_ns,
                mono_ns=lambda: harness.mono_ns,
                sleep=sleep,
                backoff=Backoff(base=0.25, cap=0.25, jitter=0),
            )
            task = asyncio.create_task(await4(recorder.run(harness.stop)))
            try:
                yield task
            finally:
                harness.stop.set()
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, pytest.fail.Exception):
                    await task
