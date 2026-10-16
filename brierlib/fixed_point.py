"""Fixed-point parsing and formatting (Stage 1 RED stubs)."""


def parse_price(text: str) -> int:
    """Return $0.0001 ticks for a price in [0, 1]."""
    raise NotImplementedError("Stage 1 GREEN: parse_price")


def parse_quantity(text: str) -> int:
    """Return signed hundredths of a contract within int64 bounds."""
    raise NotImplementedError("Stage 1 GREEN: parse_quantity")


def format_price(ticks: int) -> str:
    """Format a valid price tick count with four fractional digits."""
    raise NotImplementedError("Stage 1 GREEN: format_price")
