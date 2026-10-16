"""Allowlisted GET requests for public market data and historical market data."""

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from recorder.auth import HeaderSigner
from recorder.ratelimit import Backoff, TokenBucket

_API_ROOT = "/trade-api/v2"
_TICKER = r"[A-Za-z0-9_-]+"
_ALLOWED = re.compile(
    rf"(?:/markets(?:/{_TICKER}(?:/orderbook)?)?"
    rf"|/events(?:/{_TICKER})?"
    rf"|/series(?:/{_TICKER}(?:/markets/{_TICKER}/candlesticks)?)?"
    rf"|/historical/(?:cutoff|trades|markets(?:/{_TICKER}(?:/candlesticks)?)?))"
)


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
        """The caller owns http, based at /trade-api/v2; retries exclude the initial attempt."""
        if not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        if not math.isfinite(request_cost) or request_cost <= 0:
            raise ValueError("request_cost must be finite and positive")
        self._http = http
        self._credentials = credentials
        self._limiter = limiter if limiter is not None else TokenBucket()
        self._backoff = backoff if backoff is not None else Backoff()
        self._sleep = sleep
        self._clock_ms = clock_ms
        self._max_retries = max_retries
        self._request_cost = request_cost

    async def request(
        self, method: str, path: str, *, params: Mapping[str, str | int] | None = None
    ) -> httpx.Response:
        clean_path = path.split("?", 1)[0]
        if method != "GET" or "#" in path or _ALLOWED.fullmatch(clean_path) is None:
            raise ForbiddenEndpoint("Only allowlisted market-data GET endpoints are permitted")
        # Validate the original path before URL normalization can erase dot segments or escapes.
        query = dict(params) if params is not None else None
        request = self._http.build_request("GET", path, params=query)
        if request.url.path != _API_ROOT + clean_path:
            raise ForbiddenEndpoint("The HTTP client must use the /trade-api/v2 API root")
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire(self._request_cost)
            request.headers.update(
                self._credentials.headers(self._clock_ms(), "GET", request.url.path)
            )
            # Redirects are errors: no redirected request can bypass the path allowlist.
            response = await self._http.send(request, follow_redirects=False)
            if response.status_code == 429 and attempt < self._max_retries:
                delay = self._backoff.delay(attempt, response.headers.get("Retry-After"))
                await response.aclose()
                await self._sleep(delay)
                continue
            response.raise_for_status()
            return response
        raise RuntimeError("REST retry loop ended without a response")

    async def paginate(
        self, path: str, items_key: str, *, params: Mapping[str, str | int] | None = None
    ) -> list[dict[str, Any]]:
        query = dict(params) if params is not None else {}
        seen: set[str] = set()
        initial_cursor = query.get("cursor")
        if initial_cursor:
            if not isinstance(initial_cursor, str):
                raise PaginationError("Pagination cursor must be a string")
            seen.add(initial_cursor)
        rows: list[dict[str, Any]] = []
        while True:
            response = await self.request("GET", path, params=query)
            try:
                page = response.json()
            except ValueError:
                raise PaginationError("Pagination response is not valid JSON") from None
            if not isinstance(page, dict):
                raise PaginationError("Pagination response must be an object")
            items = page.get(items_key)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise PaginationError(f"Pagination field {items_key} must be a list of objects")
            cursor = page.get("cursor")
            if cursor is not None and not isinstance(cursor, str):
                raise PaginationError("Pagination cursor must be a string or null")
            rows.extend(items)
            if not cursor:
                return rows
            if cursor in seen:
                raise PaginationError("Repeated pagination cursor")
            seen.add(cursor)
            query["cursor"] = cursor
