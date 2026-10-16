"""Deterministic clocks and assertion reporting for Stage 4 tests."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
import pytest


def ready4[**P, T](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return call(*args, **kwargs)
    except NotImplementedError:
        pytest.fail("Stage 4 behavior is not implemented")


async def await4[T](result: Awaitable[T]) -> T:
    try:
        return await result
    except NotImplementedError:
        pytest.fail("Stage 4 async behavior is not implemented")


class Progress:
    def __init__(self) -> None:
        self.changed = asyncio.Event()

    async def until(self, task: asyncio.Task[None], check: Callable[[], bool]) -> None:
        async with asyncio.timeout(4):
            while not check():
                self.changed.clear()
                signal = asyncio.create_task(self.changed.wait())
                try:
                    await asyncio.wait((task, signal), return_when=asyncio.FIRST_COMPLETED)
                    if task.done():
                        await task
                        assert check(), "recorder stopped before the scripted scenario finished"
                finally:
                    signal.cancel()
                    try:
                        await signal
                    except asyncio.CancelledError:
                        pass


class MemorySink:
    def __init__(self, progress: Progress) -> None:
        self.lines: list[bytes] = []
        self.progress = progress

    async def write(self, line: bytes) -> None:
        self.lines.append(line)
        self.progress.changed.set()

    def rows(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = [orjson.loads(line) for line in self.lines]
        return rows if kind is None else [row for row in rows if row["kind"] == kind]


class ManualTimer:
    def __init__(self, progress: Progress) -> None:
        self.waiting: list[tuple[float, asyncio.Future[None]]] = []
        self.progress = progress

    async def sleep(self, delay: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiting.append((delay, future))
        self.progress.changed.set()
        await future

    def release(self, delay: float) -> None:
        for seconds, future in self.waiting:
            if seconds == delay and not future.done():
                future.set_result(None)
                return
        raise AssertionError(f"no active timer for {delay}")
