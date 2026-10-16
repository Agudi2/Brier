"""Recorder command and graceful shutdown (Stage 4 RED stubs)."""

import asyncio
from collections.abc import Sequence
from typing import Protocol

from recorder.config import MarketConfig
from recorder.rest import RestClient
from recorder.writer import NdjsonWriter


class RunnableRecorder(Protocol):
    async def run(self, stop: asyncio.Event) -> None: ...


async def resolve_markets(config: MarketConfig, rest: RestClient) -> list[str]:
    raise NotImplementedError("Stage 4 GREEN: resolve market selectors")


async def capture(
    recorder: RunnableRecorder,
    writer: NdjsonWriter,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Start writer and recorder, install SIGINT/SIGTERM when stop is absent, drain on exit."""
    raise NotImplementedError("Stage 4 GREEN: capture lifecycle")


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError("Stage 4 GREEN: recording CLI")


if __name__ == "__main__":
    raise SystemExit(main())
