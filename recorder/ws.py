"""Public WebSocket recording (Stage 4 RED stubs)."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from recorder.auth import HeaderSigner
from recorder.ratelimit import Backoff
from recorder.rest import RestClient


class LineSink(Protocol):
    async def write(self, line: bytes) -> None: ...


def encode_ws(raw: str | bytes, *, t_recv_ns: int, t_mono_ns: int, conn: int) -> bytes:
    """Wrap a JSON object verbatim in one v1 NDJSON envelope."""
    raise NotImplementedError("Stage 4 GREEN: verbatim envelope")


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

    async def run(self, stop: asyncio.Event) -> None:
        raise NotImplementedError("Stage 4 GREEN: record public WebSocket session")
