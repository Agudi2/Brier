import asyncio
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import orjson
import pytest

from recorder.config import MarketConfig
from recorder.rest import RestClient
from recorder.run import capture, resolve_markets
from recorder.writer import NdjsonWriter
from tests.recorder.helpers import FakeSigner
from tests.recorder.stage4_helpers import await4


async def test_selector_resolution_pages_and_deduplicates() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/trade-api/v2/markets"
        if request.url.params.get("series_ticker") == "S":
            if request.url.params.get("cursor") == "next":
                return httpx.Response(200, json={"markets": [{"ticker": "C"}], "cursor": ""})
            return httpx.Response(
                200, json={"markets": [{"ticker": "B"}, {"ticker": "A"}], "cursor": "next"}
            )
        assert request.url.params["event_ticker"] == "E"
        return httpx.Response(
            200, json={"markets": [{"ticker": "C"}, {"ticker": "D"}], "cursor": None}
        )

    async with httpx.AsyncClient(
        base_url="https://external-api.kalshi.com/trade-api/v2/",
        transport=httpx.MockTransport(handler),
    ) as http:
        result = await await4(
            resolve_markets(MarketConfig(("A",), ("S",), ("E",)), RestClient(http, FakeSigner()))
        )
    assert result == ["A", "B", "C", "D"]
    assert len(calls) == 3  # Two series pages and one event page; direct A needs no list lookup.


async def test_empty_selector_results_fail_clearly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    async with httpx.AsyncClient(
        base_url="https://external-api.kalshi.com/trade-api/v2/",
        transport=httpx.MockTransport(handler),
    ) as http:
        with pytest.raises(ValueError, match="(?i)market"):
            await await4(
                resolve_markets(MarketConfig((), ("S",), ()), RestClient(http, FakeSigner()))
            )


async def test_capture_drains_before_propagating_recorder_failure(tmp_path: Path) -> None:
    writer = NdjsonWriter(tmp_path, capacity=1)
    lines = [
        orjson.dumps({"v": 1, "kind": "ws", "t_recv_ns": 0, "msg": {"i": i}}) + b"\n"
        for i in range(3)
    ]

    class FailingRecorder:
        async def run(self, stop: asyncio.Event) -> None:
            for line in lines:
                await writer.write(line)
            raise RuntimeError("scripted recorder failure")

    with pytest.raises(RuntimeError, match="scripted recorder failure"):
        await await4(capture(FailingRecorder(), writer, stop=asyncio.Event()))
    assert (tmp_path / "1970-01-01.ndjson").read_bytes() == b"".join(lines)


async def test_sigint_flushes_every_queued_line_in_child_process(tmp_path: Path) -> None:
    # Signal delivery is isolated to a child; the parent never changes its own handlers.
    script = r"""import asyncio, json, sys
from pathlib import Path
from recorder.run import capture
from recorder.writer import NdjsonWriter
writer = NdjsonWriter(Path(sys.argv[1]), capacity=2)
class Producer:
    async def run(self, stop):
        for i in range(50):
            line = json.dumps({"v":1,"kind":"ws","t_recv_ns":0,"msg":{"i":i}}).encode()+b"\n"
            await writer.write(line)
        print("QUEUED", flush=True)
        await stop.wait()
asyncio.run(capture(Producer(), writer))
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert child.stdout is not None
    try:
        marker = await asyncio.wait_for(child.stdout.readline(), 5)
        assert marker == b"QUEUED\n", "capture failed before records were queued"
        child.send_signal(signal.SIGINT)
        _, stderr = await asyncio.wait_for(child.communicate(), 5)
        assert child.returncode == 0, stderr.decode()
    finally:
        if child.returncode is None:
            child.kill()
            await child.communicate()
    rows = [
        orjson.loads(line) for line in (tmp_path / "1970-01-01.ndjson").read_bytes().splitlines()
    ]
    assert [row["msg"]["i"] for row in rows] == list(range(50))


@pytest.mark.parametrize("module", ["recorder.run", "recorder.compact"])
def test_command_help_is_available_without_credentials(module: str) -> None:
    process = subprocess.run(
        [sys.executable, "-m", module, "--help"], capture_output=True, timeout=5
    )
    assert process.returncode == 0, process.stderr.decode()
    assert b"usage:" in process.stdout.lower()
    if module == "recorder.run":
        assert b"--markets" in process.stdout and b"--output" in process.stdout


def test_recorder_command_validates_market_config_before_credentials(tmp_path: Path) -> None:
    config = tmp_path / "markets.yaml"
    config.write_text("tickers: []\n")
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "recorder.run",
            "--markets",
            str(config),
            "--output",
            str(tmp_path / "raw"),
        ],
        capture_output=True,
        timeout=5,
    )
    assert process.returncode != 0
    assert b"selector" in process.stderr.lower()
