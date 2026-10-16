import asyncio
import io
import threading
from collections.abc import Buffer
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import orjson
import pytest

from recorder.writer import NdjsonWriter
from tests.recorder.stage4_helpers import await4, ready4


def record(index: int, ns: int) -> bytes:
    return orjson.dumps({"v": 1, "kind": "ws", "t_recv_ns": ns, "msg": {"index": index}}) + b"\n"


async def test_midnight_rotation_uses_receipt_time_even_when_writes_are_queued(
    tmp_path: Path,
) -> None:
    before = int(datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC).timestamp()) * 1_000_000_000
    after = (
        before + 1_000_000_000
    )  # Advancing the fake receipt clock by one second crosses UTC midnight.
    lines = [record(0, before), record(1, after), record(2, after)]
    writer = NdjsonWriter(tmp_path, capacity=2)
    await await4(writer.start())
    for line in lines:
        await await4(writer.write(line))
    await await4(writer.close())
    assert (tmp_path / "2026-10-01.ndjson").read_bytes() == lines[0]
    assert (tmp_path / "2026-10-02.ndjson").read_bytes() == b"".join(lines[1:])


async def test_restarting_writer_appends_without_truncation(tmp_path: Path) -> None:
    lines = [record(i, 0) for i in range(3)]
    for line in lines:
        writer = NdjsonWriter(tmp_path)
        await await4(writer.start())
        await await4(writer.write(line))
        await await4(writer.close())
    assert (tmp_path / "1970-01-01.ndjson").read_bytes() == b"".join(lines)


async def test_bounded_queue_backpressure_and_io_off_event_loop(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    thread_ids: list[int] = []
    output: list[bytes] = []
    loop_thread = threading.get_ident()

    class GatedFile(io.BytesIO):
        def write(self, data: Buffer, /) -> int:
            thread_ids.append(threading.get_ident())
            entered.set()
            assert release.wait(3), "writer I/O did not get released"
            return super().write(data)

        def close(self) -> None:
            if not self.closed:
                output.append(self.getvalue())
            super().close()

    def opener(path: Path) -> BinaryIO:
        thread_ids.append(threading.get_ident())
        return GatedFile()

    writer = NdjsonWriter(tmp_path, capacity=1, opener=opener)
    await await4(writer.start())
    third: asyncio.Task[None] | None = None
    lines = [record(i, 0) for i in range(3)]
    try:
        await await4(writer.write(lines[0]))
        assert await asyncio.to_thread(entered.wait, 1), "writer thread never started I/O"
        await await4(writer.write(lines[1]))
        third = asyncio.create_task(await4(writer.write(lines[2])))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not third.done(), "full queue must block producers instead of dropping records"
        # This callback can run while the disk worker is blocked.
        heartbeat: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(heartbeat.set_result, True)
        assert await heartbeat
    finally:
        release.set()
        if third is not None:
            await third
        await await4(writer.close())
    assert output == [b"".join(lines)]
    assert thread_ids and all(identity != loop_thread for identity in thread_ids)
    assert len(set(thread_ids)) == 1  # A single dedicated worker owns the file.


@pytest.mark.parametrize(
    "line", [b"garbage\n", b"{}\n", b'{"t_recv_ns":"bad"}\n', b'{"t_recv_ns":0}\n{}\n']
)
async def test_writer_rejects_invalid_envelope_lines(tmp_path: Path, line: bytes) -> None:
    writer = NdjsonWriter(tmp_path)
    await await4(writer.start())
    try:
        with pytest.raises(ValueError):
            await await4(writer.write(line))
    finally:
        await await4(writer.close())
    assert list(tmp_path.glob("*.ndjson")) == []


async def test_disk_error_is_reported_instead_of_hanging(tmp_path: Path) -> None:
    def opener(path: Path) -> BinaryIO:
        raise OSError("disk unavailable")

    writer = NdjsonWriter(tmp_path, capacity=1, opener=opener)
    await await4(writer.start())
    async with asyncio.timeout(2):
        with pytest.raises(OSError, match="disk unavailable"):
            await await4(writer.write(record(0, 0)))
            await await4(writer.close())


@pytest.mark.parametrize("capacity", [0, -1])
def test_nonpositive_queue_capacity_is_rejected(tmp_path: Path, capacity: int) -> None:
    with pytest.raises(ValueError):
        ready4(NdjsonWriter, tmp_path, capacity=capacity)


async def test_output_directory_is_created_and_closed_writer_rejects_new_records(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "raw" / "nested"
    writer = NdjsonWriter(directory)
    await await4(writer.start())
    line = record(0, 0)
    await await4(writer.write(line))
    await await4(writer.close())
    await await4(writer.close())
    assert (directory / "1970-01-01.ndjson").read_bytes() == line
    with pytest.raises(RuntimeError):
        await await4(writer.write(record(1, 0)))
