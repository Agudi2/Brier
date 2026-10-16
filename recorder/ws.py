"""Authenticated public streams with verbatim envelopes and explicit recovery boundaries."""

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import orjson
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from recorder.auth import HeaderSigner
from recorder.ratelimit import Backoff
from recorder.rest import RestClient

_CHANNELS = ("orderbook_delta", "trade", "ticker")
_TICKER = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")


class LineSink(Protocol):
    async def write(self, line: bytes) -> None: ...


def _raw_object(raw: str | bytes) -> tuple[bytes, dict[str, Any]]:
    data = raw.encode() if isinstance(raw, str) else raw
    if b"\n" in data or b"\r" in data:
        raise ValueError("Verbatim NDJSON payloads must be single-line JSON objects")
    try:
        value = orjson.loads(data)
    except orjson.JSONDecodeError:
        raise ValueError("Server payload is not valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Server payload must be a JSON object")
    return data, value


def _splice(kind: str, field: str, raw: bytes, **fields: Any) -> bytes:
    prefix = orjson.dumps({"v": 1, "kind": kind, **fields})[:-1]
    return prefix + b',"' + field.encode() + b'":' + raw + b"}\n"


def encode_ws(raw: str | bytes, *, t_recv_ns: int, t_mono_ns: int, conn: int) -> bytes:
    """Wrap a JSON object verbatim in one v1 NDJSON envelope."""
    data, _ = _raw_object(raw)
    return _splice("ws", "msg", data, t_recv_ns=t_recv_ns, t_mono_ns=t_mono_ns, conn=conn)


@dataclass(frozen=True)
class _Subscription:
    channel: str
    tickers: tuple[str, ...]


class _Stopped(Exception):
    pass


async def _until_stop[T](operation: Awaitable[T], stop: asyncio.Event) -> T:
    pending = asyncio.ensure_future(operation)
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((pending, stopping), return_when=asyncio.FIRST_COMPLETED)
        if pending.done():
            return await pending
        raise _Stopped
    finally:
        for task in (pending, stopping):
            if not task.done():
                task.cancel()
        await asyncio.gather(pending, stopping, return_exceptions=True)


class Recorder:
    def __init__(
        self,
        url: str,
        tickers: Sequence[str],
        sink: LineSink,
        rest: RestClient,
        credentials: HeaderSigner,
        *,
        batch_size: int = 100,
        snapshot_interval: float = 60,
        clock_ns: Callable[[], int] = time.time_ns,
        mono_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff: Backoff | None = None,
    ) -> None:
        """Use public channels and no-leg pricing, recording initial metadata and REST books."""
        endpoint = urlsplit(url)
        if endpoint.scheme not in ("ws", "wss") or not endpoint.hostname:
            raise ValueError("A WebSocket URL is required")
        if endpoint.scheme == "ws" and endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Unencrypted WebSockets are supported only on loopback")
        if endpoint.username or endpoint.password or endpoint.fragment:
            raise ValueError("WebSocket URL must not contain credentials or a fragment")
        if not tickers or any(not _TICKER.fullmatch(ticker) for ticker in tickers):
            raise ValueError("At least one valid market ticker is required")
        if not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if not math.isfinite(snapshot_interval) or snapshot_interval <= 0:
            raise ValueError("snapshot_interval must be finite and positive")
        self._url = url
        self._sign_path = endpoint.path or "/"
        self._tickers = tuple(dict.fromkeys(tickers))
        self._sink = sink
        self._rest = rest
        self._credentials = credentials
        self._batch_size = batch_size
        self._interval = snapshot_interval
        self._clock_ns = clock_ns
        self._mono_ns = mono_ns
        self._sleep = sleep
        self._backoff = backoff if backoff is not None else Backoff()
        self._next_id = 0
        self._conn = 0
        self._pending: dict[int, _Subscription] = {}
        self._subscriptions: dict[int, _Subscription] = {}
        self._sequences: dict[int, int] = {}
        self._received = False
        # The transport's DEBUG handshake logging would expose authentication headers.
        self._logger = logging.getLogger("recorder.ws.transport")
        self._logger.setLevel(logging.INFO)

    async def _event(self, kind: str, **fields: Any) -> None:
        await self._sink.write(
            orjson.dumps({"v": 1, "kind": kind, "t_recv_ns": self._clock_ns(), **fields}) + b"\n"
        )

    async def _metadata(self) -> None:
        for ticker in self._tickers:
            response = await self._rest.request("GET", f"/markets/{ticker}")
            market = response.json().get("market")
            if not isinstance(market, dict) or not isinstance(market.get("price_ranges"), list):
                raise ValueError("Market response does not contain price_ranges")
            fields = {
                key: value
                for key, value in market.items()
                if key not in ("v", "kind", "t_recv_ns", "ticker")
            }
            await self._event("market_meta", ticker=ticker, **fields)

    async def _books(self, tickers: Sequence[str]) -> None:
        for ticker in tickers:
            sent = self._clock_ns()
            response = await self._rest.request("GET", f"/markets/{ticker}/orderbook")
            received = self._clock_ns()
            raw, _ = _raw_object(response.content)
            await self._sink.write(
                _splice(
                    "rest_book",
                    "book",
                    raw,
                    t_send_ns=sent,
                    t_recv_ns=received,
                    ticker=ticker,
                )
            )

    async def _command(self, ws: ClientConnection, command: str, params: dict[str, Any]) -> int:
        self._next_id += 1
        identifier = self._next_id
        await ws.send(orjson.dumps({"id": identifier, "cmd": command, "params": params}).decode())
        return identifier

    async def _subscribe(
        self, ws: ClientConnection, channel: str, tickers: tuple[str, ...]
    ) -> None:
        if channel not in _CHANNELS:
            raise ValueError("Only public market-data channels are permitted")
        identifier = await self._command(
            ws,
            "subscribe",
            {
                "channels": [channel],
                "market_tickers": list(tickers),
            },
        )
        self._pending[identifier] = _Subscription(channel, tickers)

    async def _unsubscribe(self, ws: ClientConnection, sid: int) -> None:
        self._subscriptions.pop(sid, None)
        self._sequences.pop(sid, None)
        await self._command(ws, "unsubscribe", {"sids": [sid]})

    async def _recover(self, ws: ClientConnection, sid: int, subscription: _Subscription) -> None:
        affected = [(sid, subscription)]
        if subscription.channel != "orderbook_delta":
            affected.extend(
                (other_sid, other)
                for other_sid, other in self._subscriptions.items()
                if other.channel == "orderbook_delta"
                and set(other.tickers).intersection(subscription.tickers)
            )
        await self._event("resync", tickers=list(subscription.tickers))
        for old_sid, _ in affected:
            await self._unsubscribe(ws, old_sid)
        await self._books(subscription.tickers)
        for _, old in affected:
            await self._subscribe(ws, old.channel, old.tickers)

    async def _handle(self, ws: ClientConnection, raw: str | bytes) -> None:
        received, monotonic = self._clock_ns(), self._mono_ns()
        data, message = _raw_object(raw)
        self._received = True
        kind = message.get("type")
        raw_sid, raw_seq = message.get("sid"), message.get("seq")
        sid = raw_sid if isinstance(raw_sid, int) and not isinstance(raw_sid, bool) else None
        seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else None
        subscription = self._subscriptions.get(sid) if sid is not None else None
        gap = False
        if sid is not None and subscription is not None and seq is not None:
            previous = self._sequences.get(sid)
            if previous is not None and seq != previous + 1:
                gap = True
                # Mark the discontinuity before the offending delta can reach replay.
                await self._event(
                    "gap",
                    t_recv_ns=received,
                    conn=self._conn,
                    sid=sid,
                    expected_seq=previous + 1,
                    got_seq=seq,
                    tickers=list(subscription.tickers),
                )
            self._sequences[sid] = seq
        await self._sink.write(
            _splice("ws", "msg", data, t_recv_ns=received, t_mono_ns=monotonic, conn=self._conn)
        )
        if kind == "subscribed":
            details = message.get("msg", {})
            identifier = message.get("id")
            pending = self._pending.pop(identifier, None) if isinstance(identifier, int) else None
            if pending is None or not isinstance(details, dict):
                raise RuntimeError("Unexpected subscription acknowledgement")
            new_sid = details.get("sid")
            if type(new_sid) is not int or details.get("channel") != pending.channel:
                raise RuntimeError("Invalid subscription acknowledgement")
            self._subscriptions[new_sid] = pending
        elif kind == "unsubscribed" and sid is not None:
            self._subscriptions.pop(sid, None)
            self._sequences.pop(sid, None)
        elif kind == "error":
            raise RuntimeError("WebSocket server rejected a market-data command")
        if gap:
            assert sid is not None and subscription is not None
            await self._recover(ws, sid, subscription)

    async def _sample(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._sleep(self._interval)
            if not stop.is_set():
                await self._books(self._tickers)

    async def _session(self, ws: ClientConnection, stop: asyncio.Event) -> None:
        await self._metadata()
        await self._books(self._tickers)
        for offset in range(0, len(self._tickers), self._batch_size):
            batch = self._tickers[offset : offset + self._batch_size]
            for channel in _CHANNELS:
                await self._subscribe(ws, channel, batch)
        sampler = asyncio.create_task(self._sample(stop))
        stopping = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                receiving = asyncio.create_task(ws.recv())
                try:
                    done, _ = await asyncio.wait(
                        (receiving, stopping, sampler), return_when=asyncio.FIRST_COMPLETED
                    )
                    if receiving in done:
                        await self._handle(ws, await receiving)
                    if sampler in done:
                        await sampler
                        if not stop.is_set():
                            raise RuntimeError("REST sampling stopped unexpectedly")
                    if stopping in done:
                        break
                finally:
                    if not receiving.done():
                        receiving.cancel()
                    await asyncio.gather(receiving, return_exceptions=True)
        finally:
            sampler.cancel()
            stopping.cancel()
            await asyncio.gather(sampler, stopping, return_exceptions=True)

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            retry_after: str | None = None
            try:
                headers = self._credentials.headers(
                    self._clock_ns() // 1_000_000, "GET", self._sign_path
                )
                ws = await _until_stop(
                    connect(
                        self._url,
                        additional_headers=headers,
                        logger=self._logger,
                        proxy=None,
                        close_timeout=5,
                        max_size=16 * 1024 * 1024,
                    ),
                    stop,
                )
            except _Stopped:
                return
            except InvalidStatus as error:
                status = error.response.status_code
                if 400 <= status < 500 and status != 429:
                    raise RuntimeError(
                        f"WebSocket handshake rejected with status {status}"
                    ) from None
                retry_after = error.response.headers.get("Retry-After")
            except (OSError, TimeoutError, InvalidHandshake):
                pass
            else:
                self._conn += 1
                self._pending.clear()
                self._subscriptions.clear()
                self._sequences.clear()
                self._received = False
                try:
                    await self._event(
                        "session",
                        conn=self._conn,
                        event="open",
                        t_mono_ns=self._mono_ns(),
                        pricing_mode="no_leg",
                    )
                    if self._conn > 1:
                        await self._event("resync", tickers=list(self._tickers))
                    await self._session(ws, stop)
                except ConnectionClosed:
                    if self._received:
                        attempt = 0
                finally:
                    try:
                        await ws.close()
                    finally:
                        await self._event(
                            "session",
                            conn=self._conn,
                            event="close",
                            t_mono_ns=self._mono_ns(),
                            pricing_mode="no_leg",
                        )
                if stop.is_set():
                    return
            try:
                await _until_stop(self._sleep(self._backoff.delay(attempt, retry_after)), stop)
            except _Stopped:
                return
            attempt += 1
