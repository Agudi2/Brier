"""Read-only, cached study backfill contract."""

from collections.abc import Mapping
from pathlib import Path

from recorder.rest import RestClient
from study.dataset import Market


async def backfill(
    client: RestClient,
    cache_dir: Path,
    *,
    scheduled_closes: Mapping[str, int],
) -> list[Market]:
    """Capture settled markets and trades; explicit anchors must be known before outcome."""
    raise NotImplementedError
