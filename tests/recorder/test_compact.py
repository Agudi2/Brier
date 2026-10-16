import subprocess
import sys
from pathlib import Path

import orjson
import polars as pl
import pytest

from recorder.compact import compact_file
from tests.recorder.stage4_helpers import ready4

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/recorder/stage4.ndjson"
TABLES = {"envelopes", "deltas", "trades", "tickers", "rest_books"}


def test_compaction_inventory_and_integer_columns(tmp_path: Path) -> None:
    output = ready4(compact_file, FIXTURE, tmp_path / "tables")
    assert output == tmp_path / "tables"
    assert {path.stem for path in output.glob("*.parquet")} == TABLES
    envelopes = pl.read_parquet(output / "envelopes.parquet")
    # Fixture: 2 session + 1 metadata + 8 ws + 1 gap + 1 resync + 2 rest_book = 15.
    assert envelopes.height == 15
    counts = dict(envelopes.group_by("kind").len().iter_rows())
    assert counts == {
        "session": 2,
        "market_meta": 1,
        "ws": 8,
        "gap": 1,
        "resync": 1,
        "rest_book": 2,
    }
    assert envelopes["line_index"].to_list() == list(range(15))  # Zero-based source line identity.
    assert envelopes["raw"].to_list() == FIXTURE.read_text().splitlines()
    deltas = pl.read_parquet(output / "deltas.parquet")
    trades = pl.read_parquet(output / "trades.parquet")
    tickers = pl.read_parquet(output / "tickers.parquet")
    assert (deltas.height, trades.height, tickers.height) == (2, 2, 1)
    assert deltas.schema["price_ticks"] == pl.Int32
    assert deltas.schema["delta_qty"] == pl.Int64
    assert deltas.schema["t_recv_ns"] == pl.Int64
    assert deltas.schema["line_index"] == pl.Int64
    # Dollars * 10000: .30 -> 3000, .68 -> 6800; contracts * 100: -1.55 -> -155, 2 -> 200.
    assert deltas.select(
        "market_ticker", "side", "price_ticks", "delta_qty", "line_index"
    ).rows() == [("A", "yes", 3000, -155, 4), ("A", "no", 6800, 200, 5)]
    assert trades.schema["yes_price_ticks"] == pl.Int32
    assert trades.schema["no_price_ticks"] == pl.Int32
    assert trades.schema["qty"] == pl.Int64
    # The two input trades have 1.55 and .01 contracts -> 155 and 1 hundredths.
    assert trades.select("yes_price_ticks", "no_price_ticks", "qty", "trade_id").rows() == [
        (3000, 7000, 155, "T1"),
        (6400, 3600, 1, "T2"),
    ]
    assert tickers.schema["price_ticks"] == pl.Int32
    assert tickers.schema["yes_bid_ticks"] == pl.Int32
    assert tickers.schema["yes_ask_ticks"] == pl.Int32
    assert tickers.select("price_ticks", "yes_bid_ticks", "yes_ask_ticks").rows() == [
        (3000, 2900, 3100)
    ]


def test_rest_books_keep_typed_levels_and_empty_sides(tmp_path: Path) -> None:
    output = ready4(compact_file, FIXTURE, tmp_path)
    books = pl.read_parquet(output / "rest_books.parquet")
    assert books.height == 2
    assert books.schema["yes_prices"] == pl.List(pl.Int32)
    assert books.schema["no_prices"] == pl.List(pl.Int32)
    assert books.schema["yes_qty"] == pl.List(pl.Int64)
    assert books.schema["no_qty"] == pl.List(pl.Int64)
    assert books.schema["t_send_ns"] == pl.Int64
    # Same .30/.68 dollar bids and 1.55/2.00 quantities as the source snapshot.
    assert books.select("market_ticker", "yes_prices", "yes_qty", "no_prices", "no_qty").rows() == [
        ("A", [3000], [155], [6800], [200]),
        ("B", [], [], [], []),
    ]
    assert books.select("t_send_ns", "t_recv_ns", "line_index").rows() == [
        (109, 111, 11),
        (110, 112, 12),
    ]


def test_empty_input_emits_typed_empty_tables(tmp_path: Path) -> None:
    source = tmp_path / "empty.ndjson"
    source.write_bytes(b"")
    output = ready4(compact_file, source)
    assert output == source.with_suffix("")
    assert {path.stem for path in output.glob("*.parquet")} == TABLES
    assert all(pl.read_parquet(output / f"{name}.parquet").height == 0 for name in TABLES)
    assert pl.read_parquet(output / "deltas.parquet").schema["delta_qty"] == pl.Int64
    assert pl.read_parquet(output / "rest_books.parquet").schema["yes_prices"] == pl.List(pl.Int32)


def test_final_line_does_not_need_newline(tmp_path: Path) -> None:
    source = tmp_path / "single.ndjson"
    source.write_bytes(FIXTURE.read_bytes().splitlines()[0])
    output = ready4(compact_file, source)
    assert pl.read_parquet(output / "envelopes.parquet").height == 1


@pytest.mark.parametrize("field, value", [("price_dollars", "0.12345"), ("delta_fp", "0.001")])
def test_invalid_fixed_point_reports_source_line(tmp_path: Path, field: str, value: str) -> None:
    rows = [orjson.loads(line) for line in FIXTURE.read_bytes().splitlines()]
    rows[4]["msg"]["msg"][field] = value
    source = tmp_path / "bad.ndjson"
    source.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))
    with pytest.raises(ValueError, match="(?i)line 5"):
        ready4(compact_file, source)


def test_malformed_json_reports_source_line(tmp_path: Path) -> None:
    source = tmp_path / "bad.ndjson"
    source.write_bytes(FIXTURE.read_bytes().splitlines()[0] + b"\nnot-json\n")
    with pytest.raises(ValueError, match="(?i)line 2"):
        ready4(compact_file, source)


@pytest.mark.parametrize("update", [{"v": 2}, {"pricing_mode": "yes_leg"}])
def test_unsupported_version_or_pricing_is_rejected(
    tmp_path: Path, update: dict[str, int | str]
) -> None:
    row = orjson.loads(FIXTURE.read_bytes().splitlines()[0])
    row.update(update)
    source = tmp_path / "bad.ndjson"
    source.write_bytes(orjson.dumps(row) + b"\n")
    with pytest.raises(ValueError):
        ready4(compact_file, source)


def test_compaction_module_command(tmp_path: Path) -> None:
    process = subprocess.run(
        [sys.executable, "-m", "recorder.compact", str(FIXTURE), "--out", str(tmp_path / "tables")],
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr.decode()
    assert pl.read_parquet(tmp_path / "tables/envelopes.parquet").height == 15


@pytest.mark.parametrize(
    "text, expected", [("0.0000", 0), ("0.0001", 1), ("0.1234", 1234), ("1.0000", 10000)]
)
def test_compaction_preserves_subcent_ticks(tmp_path: Path, text: str, expected: int) -> None:
    rows = [orjson.loads(line) for line in FIXTURE.read_bytes().splitlines()]
    rows[4]["msg"]["msg"]["price_dollars"] = text
    source = tmp_path / "precision.ndjson"
    source.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))
    output = ready4(compact_file, source)
    # Dollar text * 10000 gives 0, 1, 1234, and 10000 ticks respectively.
    assert pl.read_parquet(output / "deltas.parquet")["price_ticks"][0] == expected
