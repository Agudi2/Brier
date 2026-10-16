import logging
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from recorder.ratelimit import Backoff, TokenBucket
from recorder.rest import ForbiddenEndpoint, PaginationError, RestClient
from tests.recorder.helpers import FakeClock, FakeSigner, ready_async

BASE = "https://external-api.kalshi.com/trade-api/v2/"
TIMESTAMP = 1703123456789


def rest(
    http: httpx.AsyncClient,
    clock: FakeClock,
    signer: FakeSigner | None = None,
    *,
    max_retries: int = 4,
    limiter: TokenBucket | None = None,
) -> RestClient:
    return RestClient(
        http,
        signer or FakeSigner(),
        limiter=limiter or TokenBucket(rate=1000, capacity=1000, clock=clock, sleep=clock.sleep),
        backoff=Backoff(base=0.5, cap=2, jitter=0),
        sleep=clock.sleep,
        clock_ms=lambda: TIMESTAMP + int(clock.now * 1000),
        max_retries=max_retries,
    )


@pytest.mark.parametrize(
    "path",
    [
        "/markets",
        "/markets/M",
        "/markets/M/orderbook",
        "/markets/trades",
        "/markets/orderbooks",
        "/markets/candlesticks",
        "/events",
        "/events/E",
        "/series",
        "/series/S",
        "/series/S/markets/M/candlesticks",
        "/historical/cutoff",
        "/historical/markets",
        "/historical/markets/M",
        "/historical/markets/M/candlesticks",
        "/historical/trades",
    ],
)
async def test_allowed_market_data_paths(path: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        response = await ready_async(rest(http, FakeClock()).request("GET", path))
    assert response.json() == {"ok": True}
    assert len(calls) == 1
    assert calls[0].url.path == "/trade-api/v2" + path
    assert calls[0].method == "GET"


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/portfolio/balance"),
        ("GET", "/portfolio/orders"),
        ("GET", "/historical/orders"),
        ("GET", "/historical/fills"),
        ("GET", "/historical/positions"),
        ("GET", "/account/limits"),
        ("GET", "/unknown"),
        ("GET", "/markets/M/orders"),
        ("GET", "/events/E/orders"),
        ("GET", "/series/S/orders"),
        ("GET", "/markets/M/orderbook/extra"),
        ("GET", "/markets/../portfolio/balance"),
        ("GET", "/markets/%2e%2e/portfolio/balance"),
        ("GET", "/markets/M%2forderbook"),
        ("GET", "//external-api.kalshi.com/markets"),
        ("GET", "https://external-api.kalshi.com/trade-api/v2/markets"),
        ("GET", "https://elsewhere.invalid/markets"),
        ("GET", "markets"),
        ("GET", "/markets\\M"),
        ("GET", "/markets#fragment"),
        *[(method, "/markets") for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")],
    ],
)
async def test_forbidden_before_transport_or_signing(method: str, path: str) -> None:
    calls: list[httpx.Request] = []
    signer = FakeSigner()
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ForbiddenEndpoint):
            await ready_async(rest(http, clock, signer).request(method, path))
    assert calls == []
    assert signer.calls == []
    assert clock.sleeps == []


async def test_request_signing_path_and_headers(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    signer = FakeSigner()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        await ready_async(
            rest(http, FakeClock(), signer).request(
                "GET", "/markets", params={"status": "settled", "limit": 7}
            )
        )
    assert signer.calls == [(TIMESTAMP, "GET", "/trade-api/v2/markets")]
    assert seen[0].url.params["limit"] == "7"
    assert seen[0].headers["KALSHI-ACCESS-KEY"] == "test-id"
    assert seen[0].headers["KALSHI-ACCESS-TIMESTAMP"] == str(TIMESTAMP)
    assert seen[0].headers["KALSHI-ACCESS-SIGNATURE"] == "test-signature"
    assert "test-id" not in caplog.text and "test-signature" not in caplog.text


async def test_redirect_is_not_followed_even_if_http_client_enables_it() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/trade-api/v2/portfolio/balance"})

    async with httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await ready_async(rest(http, FakeClock()).request("GET", "/markets"))
    assert calls == ["/trade-api/v2/markets"]


async def test_429_backoff_cap_and_fresh_signatures() -> None:
    clock = FakeClock()
    signer = FakeSigner()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429 if calls <= 4 else 200, json={})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        response = await ready_async(rest(http, clock, signer).request("GET", "/markets"))
    assert response.status_code == 200 and calls == 5
    # .5*2**attempt capped at 2: cumulative times are 0, .5, 1.5, 3.5, 5.5 seconds.
    assert clock.sleeps == [0.5, 1, 2, 2]
    assert [call[0] for call in signer.calls] == [
        TIMESTAMP,
        TIMESTAMP + 500,
        TIMESTAMP + 1500,
        TIMESTAMP + 3500,
        TIMESTAMP + 5500,
    ]


async def test_429_honors_retry_after_above_backoff_cap() -> None:
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        await ready_async(rest(http, clock).request("GET", "/markets"))
    assert clock.sleeps == [7]
    assert calls == 2


async def test_429_exhausts_retry_budget() -> None:
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert calls <= 3, "retry budget exceeded"
        return httpx.Response(429, json={})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await ready_async(rest(http, clock, max_retries=2).request("GET", "/markets"))
    assert error.value.response.status_code == 429
    assert calls == 3  # One initial attempt + two retries; no sleep after the final failure.
    assert clock.sleeps == [0.5, 1]


async def test_token_cost_applies_to_retries() -> None:
    clock = FakeClock()
    calls: list[float] = []
    limiter = TokenBucket(rate=10, capacity=10, clock=clock, sleep=clock.sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(clock.now)
        return httpx.Response(429 if len(calls) == 1 else 200, json={})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        await ready_async(rest(http, clock, limiter=limiter).request("GET", "/markets"))
    # First attempt consumes 10 tokens; backoff refills 5, so another .5 sec is needed.
    assert clock.sleeps == [0.5, 0.5]
    assert calls == [0, 1]


async def test_non_429_http_error_is_not_retried() -> None:
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await ready_async(rest(http, clock).request("GET", "/markets/M"))
    assert calls == 1 and clock.sleeps == []


@pytest.mark.parametrize("terminal", ["", None, "missing"])
async def test_pagination_collects_pages_and_preserves_filters(terminal: str | None) -> None:
    requests: list[httpx.Request] = []
    params: dict[str, str | int] = {"series_ticker": "S", "limit": 2}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            assert "cursor" not in request.url.params
            return httpx.Response(200, json={"markets": [{"ticker": "M1"}], "cursor": "next +/="})
        assert len(requests) == 2
        assert request.url.params["cursor"] == "next +/="
        payload: dict[str, Any] = {"markets": [{"ticker": "M2"}]}
        if terminal != "missing":
            payload["cursor"] = terminal
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        rows = await ready_async(
            rest(http, FakeClock()).paginate("/markets", "markets", params=params)
        )
    assert rows == [{"ticker": "M1"}, {"ticker": "M2"}]
    assert params == {"series_ticker": "S", "limit": 2}
    for request in requests:
        assert request.url.params["series_ticker"] == "S"
        assert request.url.params["limit"] == "2"


async def test_empty_page_with_cursor_is_not_terminal() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert calls <= 2
        return httpx.Response(
            200,
            json={
                "markets": [] if calls == 1 else [{"ticker": "M"}],
                "cursor": "next" if calls == 1 else "",
            },
        )

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        rows = await ready_async(rest(http, FakeClock()).paginate("/markets", "markets"))
    assert rows == [{"ticker": "M"}] and calls == 2


async def test_repeated_cursor_is_reported() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert calls <= 2, "pagination did not detect a repeated cursor"
        return httpx.Response(200, json={"markets": [], "cursor": "loop"})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PaginationError, match="(?i)cursor"):
            await ready_async(rest(http, FakeClock()).paginate("/markets", "markets"))
    assert calls == 2


@pytest.mark.parametrize(
    "payload", [{}, {"markets": {}}, {"markets": [1]}, {"markets": [], "cursor": 42}]
)
async def test_malformed_pages_are_reported(payload: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PaginationError):
            await ready_async(rest(http, FakeClock()).paginate("/markets", "markets"))


@settings(max_examples=30)
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=30))
async def test_arbitrary_private_paths_never_reach_transport(suffix: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ForbiddenEndpoint):
            await ready_async(rest(http, FakeClock()).request("GET", "/portfolio/" + suffix))
    assert calls == []
