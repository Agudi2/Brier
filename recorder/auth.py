"""Environment-only credentials and request signing (Stage 3 RED stubs)."""

from typing import Protocol


class HeaderSigner(Protocol):
    def headers(self, timestamp_ms: int, method: str, path: str) -> dict[str, str]: ...


class Credentials:
    @classmethod
    def from_env(cls) -> "Credentials":
        raise NotImplementedError("Stage 3 GREEN: credentials from environment")

    def headers(self, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        raise NotImplementedError("Stage 3 GREEN: request signing")
