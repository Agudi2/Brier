from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from recorder.config import load_config
from tests.recorder.helpers import ready


@pytest.mark.parametrize("key", ["tickers", "series", "events"])
def test_each_selector_can_stand_alone(tmp_path: Path, key: str) -> None:
    path = tmp_path / "markets.yaml"
    path.write_text(f"{key}: [EXAMPLE-1, EXAMPLE-2]\n")
    config = ready(load_config, path)
    assert getattr(config, key) == ("EXAMPLE-1", "EXAMPLE-2")
    for other in {"tickers", "series", "events"} - {key}:
        assert getattr(config, other) == ()


def test_combined_selectors(tmp_path: Path) -> None:
    path = tmp_path / "markets.yaml"
    path.write_text("tickers: [M]\nseries: [S]\nevents: [E]\n")
    config = ready(load_config, str(path))
    assert (config.tickers, config.series, config.events) == (("M",), ("S",), ("E",))


@pytest.mark.parametrize(
    "content, message",
    [
        ("", "selector"),
        ("{}", "selector"),
        ("tickers: []\nseries: []\nevents: []", "selector"),
        ("- M", "mapping"),
        ("tickers: M", "tickers"),
        ("tickers: [1]", "tickers"),
        ("tickers: [true]", "tickers"),
        ("tickers: [null]", "tickers"),
        ("tickers: ['']", "tickers"),
        ("series: ['  ']", "series"),
        ("events: {}", "events"),
        ("tickres: [M]", "tickres"),
        ("tickers: [", "YAML"),
    ],
)
def test_malformed_config_has_clear_error(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "markets.yaml"
    path.write_text(content)
    with pytest.raises(ValueError, match=message):
        ready(load_config, path)


@given(
    st.lists(
        st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-", min_size=1, max_size=20),
        min_size=1,
        max_size=10,
        unique=True,
    )
)
def test_selector_round_trip_property(values: list[str]) -> None:
    # Quote every selector: YAML numeric-looking ticker strings must remain strings.
    import tempfile

    import yaml

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "markets.yaml"
        path.write_text(yaml.safe_dump({"tickers": values}))
        assert ready(load_config, path).tickers == tuple(values)
