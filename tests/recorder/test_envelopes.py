import orjson
import pytest
from hypothesis import given
from hypothesis import strategies as st

from recorder.ws import encode_ws
from tests.recorder.stage4_helpers import ready4


@pytest.mark.parametrize(
    "raw",
    [
        b'{ "type" : "future", "msg" : {"large":9007199254740993,"p":0.1200} }',
        '{"type": "ticker", "msg": {"text": "braces } and \\"quotes\\"", "symbol": "\\u2603"}}',
    ],
)
def test_server_json_is_spliced_verbatim(raw: str | bytes) -> None:
    line = ready4(encode_ws, raw, t_recv_ns=100, t_mono_ns=200, conn=1)
    original = raw.encode() if isinstance(raw, str) else raw
    assert original in line
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    envelope = orjson.loads(line)
    assert envelope == {
        "v": 1,
        "kind": "ws",
        "t_recv_ns": 100,
        "t_mono_ns": 200,
        "conn": 1,
        "msg": orjson.loads(raw),
    }


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"null", b'{"x":', b"{}{}"])
def test_malformed_server_objects_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        ready4(encode_ws, raw, t_recv_ns=100, t_mono_ns=200, conn=1)


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=15),
        st.one_of(st.integers(-(2**63), 2**63 - 1), st.text(max_size=30)),
        max_size=10,
    )
)
def test_envelope_round_trip_property(message: dict[str, int | str]) -> None:
    raw = orjson.dumps(message)
    line = ready4(encode_ws, raw, t_recv_ns=100, t_mono_ns=200, conn=1)
    assert orjson.loads(line)["msg"] == message
    assert raw in line
