import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "fixtures/replay/golden_small.ndjson"
EXPECTED = ROOT / "fixtures/replay/golden_small.expected.json"
TOB_DTYPE = np.dtype(
    [
        ("t_recv_ns", "<i8"),
        ("market_id", "<u4"),
        ("yes_bid", "<i4"),
        ("yes_ask", "<i4"),
        ("bid_qty", "<i8"),
        ("ask_qty", "<i8"),
        ("flags", "<u4"),
    ],
    align=False,
)


@pytest.fixture(scope="session")
def replay_cli(tmp_path_factory: pytest.TempPathFactory) -> Path:
    build = tmp_path_factory.mktemp("replay-cli-build")
    configure = [
        "cmake",
        "-S",
        str(ROOT / "tests/engine"),
        "-B",
        str(build),
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    # Reuse downloaded source archives when available; build outputs stay isolated.
    # A fresh CI checkout lets FetchContent obtain the versions pinned by the project.
    for dependency in ("simdjson", "googletest", "benchmark"):
        source = ROOT / "build/_deps" / f"{dependency}-src"
        if source.is_dir():
            configure.append(f"-DFETCHCONTENT_SOURCE_DIR_{dependency.upper()}={source}")
    configured = subprocess.run(configure, text=True, capture_output=True, timeout=300)
    assert configured.returncode == 0, configured.stdout + configured.stderr
    compiled = subprocess.run(
        ["cmake", "--build", str(build), "--target", "brier_replay_cli_test", "--parallel", "2"],
        text=True,
        capture_output=True,
        timeout=300,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return build / "brier_replay"


def invoke(executable: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *args], cwd=cwd, text=True, capture_output=True, timeout=30
    )


def expected() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(EXPECTED.read_text())
    return result


@pytest.mark.parametrize("threads", [1, 2, 8])
def test_golden_summary_and_numpy_binary_rows(
    replay_cli: Path, tmp_path: Path, threads: int
) -> None:
    out = tmp_path / "output"
    run = invoke(
        replay_cli, str(GOLDEN), "--threads", str(threads), "--out", str(out), "--tob", cwd=tmp_path
    )
    assert run.returncode == 0, run.stderr
    oracle = expected()
    summary = json.loads((out / "summary.json").read_text())
    for key, value in oracle["summary"].items():
        assert summary[key] == value
    assert json.loads((out / "markets.json").read_text()) == oracle["markets"]
    # 8+4+4+4+8+8+4 = 40 bytes, with no native padding or serialized line index.
    assert TOB_DTYPE.itemsize == 40
    assert (out / "tob.bin").stat().st_size == 15 * 40
    rows = np.fromfile(out / "tob.bin", dtype=TOB_DTYPE)
    assert rows.tolist() == [tuple(row[:7]) for row in oracle["tob"]]
    # Assert little-endian bytes separately from NumPy's interpretation.
    raw = (out / "tob.bin").read_bytes()
    assert raw[:8] == (110).to_bytes(8, "little", signed=True)
    assert raw[12:16] == (4000).to_bytes(4, "little", signed=True)
    assert raw[20:28] == (200).to_bytes(8, "little", signed=True)


def test_outputs_are_byte_identical_across_threads(replay_cli: Path, tmp_path: Path) -> None:
    hashes = []
    for threads in (1, 2, 8):
        out = tmp_path / str(threads)
        run = invoke(
            replay_cli,
            str(GOLDEN),
            "--threads",
            str(threads),
            "--out",
            str(out),
            "--tob",
            cwd=tmp_path,
        )
        assert run.returncode == 0, run.stderr
        hashes.append(
            tuple(
                hashlib.sha256((out / name).read_bytes()).hexdigest()
                for name in ("summary.json", "markets.json", "tob.bin")
            )
        )
    assert hashes[0] == hashes[1] == hashes[2]


def test_default_output_and_tob_opt_in(replay_cli: Path, tmp_path: Path) -> None:
    run = invoke(replay_cli, str(GOLDEN), cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    out = tmp_path / "replay_out"
    assert (out / "summary.json").is_file()
    assert (out / "markets.json").is_file()
    assert not (out / "tob.bin").exists()


def test_empty_input_and_malformed_line_counts(replay_cli: Path, tmp_path: Path) -> None:
    source = tmp_path / "input.ndjson"
    source.write_text("")
    out = tmp_path / "empty"
    run = invoke(replay_cli, str(source), "--out", str(out), "--tob", cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert (out / "tob.bin").read_bytes() == b""
    assert json.loads((out / "markets.json").read_text()) == []
    summary = json.loads((out / "summary.json").read_text())
    assert summary["physical_lines"] == summary["malformed_lines"] == 0
    assert summary["markets"] == []
    source.write_text("{broken\n" + GOLDEN.read_text())
    out = tmp_path / "malformed"
    run = invoke(replay_cli, str(source), "--out", str(out), "--tob", cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    summary = json.loads((out / "summary.json").read_text())
    assert summary["physical_lines"] == 29  # One malformed line plus the 28-line fixture.
    assert summary["malformed_lines"] == 1
    assert summary["totals"] == expected()["summary"]["totals"]
    assert np.fromfile(out / "tob.bin", dtype=TOB_DTYPE).tolist() == [
        tuple(row[:7]) for row in expected()["tob"]
    ]


def test_help_documents_options(replay_cli: Path, tmp_path: Path) -> None:
    run = invoke(replay_cli, "--help", cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    for option in ("--threads", "--out", "--tob", "--validate"):
        assert option in run.stdout


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--unknown"],
        ["--threads"],
        ["--threads", "0"],
        ["--threads", "-1"],
        ["--threads", "abc"],
    ],
)
def test_bad_arguments_return_usage_error(
    replay_cli: Path, tmp_path: Path, args: list[str]
) -> None:
    run = invoke(replay_cli, *([] if not args else [str(GOLDEN), *args]), cwd=tmp_path)
    assert run.returncode == 2
    assert run.stderr.strip()


def test_input_and_output_errors_have_distinct_exit_codes(replay_cli: Path, tmp_path: Path) -> None:
    missing = invoke(replay_cli, str(tmp_path / "missing.ndjson"), cwd=tmp_path)
    assert missing.returncode == 3
    unsupported = invoke(
        replay_cli, str(ROOT / "fixtures/replay/parse_unsupported_mode.ndjson"), cwd=tmp_path
    )
    assert unsupported.returncode == 3
    assert not (tmp_path / "replay_out/summary.json").exists()
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("keep this file")
    run = invoke(replay_cli, str(GOLDEN), "--out", str(blocked), cwd=tmp_path)
    assert run.returncode == 4
    assert blocked.read_text() == "keep this file"


def test_validate_flag_reports_pending_stage_eight(replay_cli: Path, tmp_path: Path) -> None:
    run = invoke(replay_cli, str(GOLDEN), "--validate", cwd=tmp_path)
    assert run.returncode == 5
    assert "validat" in run.stderr.lower()
