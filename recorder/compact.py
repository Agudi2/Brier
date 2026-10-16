"""Raw NDJSON to typed Parquet (Stage 4 RED stubs)."""

from collections.abc import Sequence
from pathlib import Path


def compact_file(path: str | Path, out_dir: str | Path | None = None) -> Path:
    """Default output is the source path without its .ndjson suffix, as a directory."""
    raise NotImplementedError("Stage 4 GREEN: compact NDJSON")


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError("Stage 4 GREEN: compaction CLI")


if __name__ == "__main__":
    raise SystemExit(main())
