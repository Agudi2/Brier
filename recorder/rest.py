"""Read-only REST market-data access (Stage 3 RED stubs)."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from recorder.auth import HeaderSigner
from recorder.ratelimit import Backoff, TokenBucket


class ForbiddenEndpoint(ValueError):
    """A method or path is outside the market-data allowlist."""


class PaginationError(ValueError):
    """A cursor loop or malformed page prevents reliable pagination."""


class RestClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        credentials: HeaderSigner,
        *,
        limiter: TokenBucket | None = None,
        backoff: Backoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        max_retries: int = 4,
        request_cost: float = 10.0,
    ) -> None:
        """The caller owns http; max_retries excludes the first attempt."""

    async def request(
        self, method: str, path: str, *, params: Mapping[str, str | int] | None = None
    ) -> httpx.Response:
        raise NotImplementedError("Stage 3 GREEN: read-only REST request")

    async def paginate(
        self, path: str, items_key: str, *, params: Mapping[str, str | int] | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("Stage 3 GREEN: pagination")
