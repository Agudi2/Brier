"""Exact fixed-point prices and contract quantities."""

_INT64_MAX = (1 << 63) - 1
_INT64_MIN = -(1 << 63)


def _parse_scaled(text: str, decimals: int, limit: int, allow_negative: bool) -> int:
    if not text:
        raise ValueError("empty fixed-point value")

    negative = text[0] == "-"
    if negative and not allow_negative:
        raise ValueError("negative prices are invalid")
    start = int(negative)
    if start == len(text) or not ("0" <= text[start] <= "9"):
        raise ValueError("fixed-point value must start with a digit")
    if text[start] == "0" and start + 1 < len(text) and "0" <= text[start + 1] <= "9":
        raise ValueError("leading zeroes are invalid")

    scale = 1
    for _ in range(decimals):
        scale *= 10
    whole = 0
    position = start
    while position < len(text) and "0" <= text[position] <= "9":
        digit = ord(text[position]) - ord("0")
        if whole > (limit // scale - digit) // 10:
            raise ValueError("fixed-point value is out of range")
        whole = whole * 10 + digit
        position += 1

    fractional = 0
    digits = 0
    if position < len(text) and text[position] == ".":
        position += 1
        if position == len(text):
            raise ValueError("fractional digits are missing")
        while position < len(text) and "0" <= text[position] <= "9":
            digits += 1
            if digits > decimals:
                raise ValueError("too many fractional digits")
            fractional = fractional * 10 + ord(text[position]) - ord("0")
            position += 1
    if position != len(text):
        raise ValueError("invalid fixed-point character")

    for _ in range(decimals - digits):
        fractional *= 10
    magnitude = whole * scale + fractional
    if magnitude > limit:
        raise ValueError("fixed-point value is out of range")
    return -magnitude if negative else magnitude


def parse_price(text: str) -> int:
    """Return $0.0001 ticks for a price in [0, 1]."""
    return _parse_scaled(text, decimals=4, limit=10000, allow_negative=False)


def parse_quantity(text: str) -> int:
    """Return signed hundredths of a contract within int64 bounds."""
    limit = -_INT64_MIN if text.startswith("-") else _INT64_MAX
    return _parse_scaled(text, decimals=2, limit=limit, allow_negative=True)


def format_price(ticks: int) -> str:
    """Format a valid price tick count with four fractional digits."""
    if not isinstance(ticks, int) or isinstance(ticks, bool) or not 0 <= ticks <= 10000:
        raise ValueError("price ticks must be an integer in [0, 10000]")
    return f"{ticks // 10000}.{ticks % 10000:04d}"
