"""Stream raw envelopes into typed Parquet batches without changing the source."""

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, overload

import orjson
import polars as pl
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from brierlib.fixed_point import parse_price, parse_quantity

_COMMON: dict[str, pl.DataType | type[pl.DataType]] = {
    "line_index": pl.Int64,
    "t_recv_ns": pl.Int64,
}
_SCHEMAS = {
    "envelopes": pl.Schema(
        {
            **_COMMON,
            "v": pl.Int32,
            "kind": pl.String,
            "t_mono_ns": pl.Int64,
            "conn": pl.Int64,
            "message_type": pl.String,
            "raw": pl.String,
        }
    ),
    "deltas": pl.Schema(
        {
            **_COMMON,
            "conn": pl.Int64,
            "sid": pl.Int64,
            "seq": pl.Int64,
            "market_ticker": pl.String,
            "side": pl.String,
            "price_ticks": pl.Int32,
            "delta_qty": pl.Int64,
        }
    ),
    "trades": pl.Schema(
        {
            **_COMMON,
            "conn": pl.Int64,
            "sid": pl.Int64,
            "seq": pl.Int64,
            "market_ticker": pl.String,
            "trade_id": pl.String,
            "yes_price_ticks": pl.Int32,
            "no_price_ticks": pl.Int32,
            "qty": pl.Int64,
            "taker_side": pl.String,
            "ts_ms": pl.Int64,
        }
    ),
    "tickers": pl.Schema(
        {
            **_COMMON,
            "conn": pl.Int64,
            "sid": pl.Int64,
            "market_ticker": pl.String,
            "price_ticks": pl.Int32,
            "yes_bid_ticks": pl.Int32,
            "yes_ask_ticks": pl.Int32,
            "ts_ms": pl.Int64,
        }
    ),
    "rest_books": pl.Schema(
        {
            **_COMMON,
            "t_send_ns": pl.Int64,
            "market_ticker": pl.String,
            "yes_prices": pl.List(pl.Int32),
            "yes_qty": pl.List(pl.Int64),
            "no_prices": pl.List(pl.Int32),
            "no_qty": pl.List(pl.Int64),
        }
    ),
}
_BATCH_LINES = 10000


@overload
def _integer(value: Any, field: str, *, optional: Literal[False] = False) -> int: ...


@overload
def _integer(value: Any, field: str, *, optional: Literal[True]) -> int | None: ...


def _integer(value: Any, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or not -(1 << 63) <= value < (1 << 63):
        raise ValueError(f"{field} must be an int64")
    return value


def _text(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _price(message: dict[str, Any], field: str, *, optional: bool = False) -> int | None:
    value = message.get(field)
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be fixed-point text")
    return parse_price(value)


def _quantity(value: Any, *, signed: bool = False) -> int:
    if not isinstance(value, str):
        raise ValueError("Quantity must be fixed-point text")
    quantity = parse_quantity(value)
    if not signed and quantity < 0:
        raise ValueError("Absolute quantities cannot be negative")
    return quantity


def _levels(value: Any) -> tuple[list[int], list[int]]:
    if not isinstance(value, list):
        raise ValueError("Orderbook levels must be arrays")
    prices, quantities = [], []
    for level in value:
        if not isinstance(level, list) or len(level) != 2 or not isinstance(level[0], str):
            raise ValueError("Orderbook levels must be fixed-point price/quantity pairs")
        prices.append(parse_price(level[0]))
        quantities.append(_quantity(level[1]))
    return prices, quantities


def _convert(raw: str, index: int, buffers: dict[str, list[dict[str, Any]]]) -> None:
    envelope = orjson.loads(raw)
    if not isinstance(envelope, dict) or type(envelope.get("v")) is not int or envelope["v"] != 1:
        raise ValueError("Unsupported envelope version")
    kind = _text(envelope.get("kind"), "kind")
    common = {"line_index": index, "t_recv_ns": _integer(envelope.get("t_recv_ns"), "t_recv_ns")}
    if kind == "session" and envelope.get("pricing_mode") != "no_leg":
        raise ValueError("Unsupported session pricing_mode")
    message = envelope.get("msg") if kind == "ws" else None
    if kind == "ws" and not isinstance(message, dict):
        raise ValueError("WebSocket envelope msg must be an object")
    message_type = _text(message.get("type"), "type", optional=True) if message else None
    buffers["envelopes"].append(
        {
            **common,
            "v": 1,
            "kind": kind,
            "t_mono_ns": _integer(envelope.get("t_mono_ns"), "t_mono_ns", optional=True),
            "conn": _integer(envelope.get("conn"), "conn", optional=True),
            "message_type": message_type,
            "raw": raw,
        }
    )
    if kind == "rest_book":
        book = envelope.get("book")
        if not isinstance(book, dict) or not isinstance(book.get("orderbook_fp"), dict):
            raise ValueError("REST book must contain orderbook_fp")
        levels = book["orderbook_fp"]
        yes_prices, yes_qty = _levels(levels.get("yes_dollars"))
        no_prices, no_qty = _levels(levels.get("no_dollars"))
        sent = _integer(envelope.get("t_send_ns"), "t_send_ns")
        if sent is not None and sent > common["t_recv_ns"]:
            raise ValueError("REST receipt must not precede request send time")
        buffers["rest_books"].append(
            {
                **common,
                "t_send_ns": sent,
                "market_ticker": _text(envelope.get("ticker"), "ticker"),
                "yes_prices": yes_prices,
                "yes_qty": yes_qty,
                "no_prices": no_prices,
                "no_qty": no_qty,
            }
        )
        return
    if message_type not in ("orderbook_delta", "trade", "ticker"):
        return
    assert isinstance(message, dict)
    payload = message.get("msg")
    if not isinstance(payload, dict):
        raise ValueError("Market-data message payload must be an object")
    stream = {
        **common,
        "conn": _integer(envelope.get("conn"), "conn"),
        "sid": _integer(message.get("sid"), "sid"),
        "market_ticker": _text(payload.get("market_ticker"), "market_ticker"),
    }
    if message_type == "orderbook_delta":
        if payload.get("side") not in ("yes", "no"):
            raise ValueError("Delta side must be yes or no")
        buffers["deltas"].append(
            {
                **stream,
                "seq": _integer(message.get("seq"), "seq"),
                "side": payload["side"],
                "price_ticks": _price(payload, "price_dollars"),
                "delta_qty": _quantity(payload.get("delta_fp"), signed=True),
            }
        )
    elif message_type == "trade":
        buffers["trades"].append(
            {
                **stream,
                "seq": _integer(message.get("seq"), "seq", optional=True),
                "trade_id": _text(payload.get("trade_id"), "trade_id", optional=True),
                "yes_price_ticks": _price(payload, "yes_price_dollars"),
                "no_price_ticks": _price(payload, "no_price_dollars"),
                "qty": _quantity(payload.get("count_fp")),
                "taker_side": _text(payload.get("taker_side"), "taker_side", optional=True),
                "ts_ms": _integer(payload.get("ts_ms"), "ts_ms", optional=True),
            }
        )
    else:
        buffers["tickers"].append(
            {
                **stream,
                "price_ticks": _price(payload, "price_dollars", optional=True),
                "yes_bid_ticks": _price(payload, "yes_bid_dollars", optional=True),
                "yes_ask_ticks": _price(payload, "yes_ask_dollars", optional=True),
                "ts_ms": _integer(payload.get("ts_ms"), "ts_ms", optional=True),
            }
        )


def compact_file(path: str | Path, out_dir: str | Path | None = None) -> Path:
    """Default output is the source path without its .ndjson suffix, as a directory."""
    source = Path(path)
    output = Path(out_dir) if out_dir is not None else source.with_suffix("")
    if output.resolve() == source.resolve():
        raise ValueError("Compaction output must differ from the input file")
    output.parent.mkdir(parents=True, exist_ok=True)
    buffers: dict[str, list[dict[str, Any]]] = {name: [] for name in _SCHEMAS}
    with tempfile.TemporaryDirectory(prefix="brier-compact-", dir=output.parent) as temporary:
        staging = Path(temporary)
        writers: dict[str, Any] = {}
        try:
            for name, schema in _SCHEMAS.items():
                arrow_schema = pl.DataFrame(schema=schema).to_arrow().schema
                writers[name] = pq.ParquetWriter(
                    staging / f"{name}.parquet", arrow_schema, compression="zstd"
                )

            def flush() -> None:
                for name, batch in buffers.items():
                    if batch:
                        frame = pl.DataFrame(batch, schema=_SCHEMAS[name], strict=True)
                        writers[name].write_table(frame.to_arrow())
                        batch.clear()

            with source.open("rb") as stream:
                for index, line in enumerate(stream):
                    try:
                        raw = line.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8")
                        _convert(raw, index, buffers)
                    except (ValueError, TypeError, KeyError, OverflowError) as error:
                        raise ValueError(f"line {index + 1}: {error}") from None
                    if (index + 1) % _BATCH_LINES == 0:
                        flush()
            flush()
        finally:
            for writer in writers.values():
                writer.close()
        # Parsing and validation complete before any existing output table is replaced.
        output.mkdir(parents=True, exist_ok=True)
        for name in _SCHEMAS:
            (staging / f"{name}.parquet").replace(output / f"{name}.parquet")
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compact raw NDJSON into typed Parquet tables")
    parser.add_argument("path", type=Path)
    parser.add_argument("--out", type=Path, help="Output directory (default: input without suffix)")
    args = parser.parse_args(argv)
    try:
        output = compact_file(args.path, args.out)
    except (OSError, ValueError, pl.exceptions.PolarsError) as error:
        print(f"Compaction failed: {error}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
