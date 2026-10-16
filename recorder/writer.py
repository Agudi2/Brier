"""Bounded asynchronous admission to a single buffered disk writer."""

import asyncio
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import orjson


class NdjsonWriter:
    def __init__(
        self,
        directory: str | Path,
        *,
        capacity: int = 4096,
        opener: Callable[[Path], BinaryIO] | None = None,
    ) -> None:
        """Rotate by each envelope's UTC receipt date; opener is owned by the writer thread."""
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("Writer queue capacity must be a positive integer")
        self._directory = Path(directory)
        self._opener = opener
        self._queue: queue.Queue[tuple[str, bytes] | None] = queue.Queue(capacity)
        self._space = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready: asyncio.Future[None] | None = None
        self._finished: asyncio.Future[None] | None = None
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._closed = False
        self._closing: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._thread is not None or self._closed:
            raise RuntimeError("Writer has already been started or closed")
        self._loop = asyncio.get_running_loop()
        self._ready = self._loop.create_future()
        self._finished = self._loop.create_future()
        self._thread = threading.Thread(target=self._worker, name="ndjson-writer")
        self._thread.start()
        await self._ready

    def _notify(self, callback: Callable[[], None]) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(callback)

    def _started(self) -> None:
        assert self._ready is not None
        if not self._ready.done():
            self._ready.set_result(None)

    def _stopped(self) -> None:
        assert self._ready is not None and self._finished is not None
        if not self._ready.done():
            if self._error is not None:
                self._ready.set_exception(self._error)
            else:
                self._ready.set_result(None)
        if not self._finished.done():
            self._finished.set_result(None)
        self._space.set()

    def _worker(self) -> None:
        stream: BinaryIO | None = None
        current_date: str | None = None
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._notify(self._started)
            while True:
                item = self._queue.get()
                self._notify(self._space.set)
                if item is None:
                    break
                date, line = item
                if date != current_date:
                    if stream is not None:
                        stream.flush()
                        stream.close()
                    path = self._directory / f"{date}.ndjson"
                    stream = (
                        self._opener(path)
                        if self._opener is not None
                        else path.open("ab", buffering=1024 * 1024)
                    )
                    current_date = date
                assert stream is not None
                position = 0
                while position < len(line):
                    written = stream.write(line[position:])
                    if written is None or written <= 0:
                        raise OSError("NDJSON writer made no progress")
                    position += written
        except Exception as error:
            self._error = error
        finally:
            if stream is not None:
                try:
                    stream.flush()
                    stream.close()
                except Exception as error:
                    if self._error is None:
                        self._error = error
            self._notify(self._stopped)

    async def _enqueue(self, item: tuple[str, bytes] | None) -> None:
        assert self._finished is not None
        while True:
            if item is not None and self._closed:
                raise RuntimeError("Writer is closing")
            if self._error is not None:
                raise self._error
            if self._finished.done():
                raise RuntimeError("Writer thread has stopped")
            self._space.clear()
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                await self._space.wait()

    async def write(self, line: bytes) -> None:
        if self._closed or self._thread is None:
            raise RuntimeError("Writer must be started and open")
        if not isinstance(line, bytes) or not line.endswith(b"\n") or line.count(b"\n") != 1:
            raise ValueError("Expected one complete NDJSON line")
        try:
            envelope = orjson.loads(line)
        except orjson.JSONDecodeError:
            raise ValueError("Invalid JSON envelope") from None
        if not isinstance(envelope, dict) or envelope.get("v") != 1:
            raise ValueError("Expected a version 1 envelope")
        if not isinstance(envelope.get("kind"), str):
            raise ValueError("Envelope kind must be a string")
        received = envelope.get("t_recv_ns")
        if type(received) is not int or not -(1 << 63) <= received < (1 << 63):
            raise ValueError("Envelope t_recv_ns must be an int64 UTC timestamp")
        # Integer seconds avoid rounding the last nanoseconds of a day into the next day.
        date = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=received // 1_000_000_000)
        await self._enqueue((date.date().isoformat(), line))

    async def _drain(self) -> None:
        assert self._finished is not None and self._thread is not None
        try:
            if not self._finished.done():
                await self._enqueue(None)
        finally:
            await self._finished
            await asyncio.to_thread(self._thread.join)
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        self._closed = True
        if self._thread is None:
            return
        if self._closing is None:
            self._closing = asyncio.create_task(self._drain())
        await asyncio.shield(self._closing)
