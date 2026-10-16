"""Buffered, bounded NDJSON writing (Stage 4 RED stubs)."""

from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO


class NdjsonWriter:
    def __init__(
        self,
        directory: str | Path,
        *,
        capacity: int = 4096,
        opener: Callable[[Path], BinaryIO] | None = None,
    ) -> None:
        """Rotate by each envelope's UTC receipt date; opener is owned by the writer thread."""

    async def start(self) -> None:
        raise NotImplementedError("Stage 4 GREEN: writer start")

    async def write(self, line: bytes) -> None:
        raise NotImplementedError("Stage 4 GREEN: writer enqueue")

    async def close(self) -> None:
        raise NotImplementedError("Stage 4 GREEN: writer drain and close")
