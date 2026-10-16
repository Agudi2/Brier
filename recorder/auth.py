"""Environment-only RSA credentials and query-free request signing."""

import base64
import os
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class HeaderSigner(Protocol):
    def headers(self, timestamp_ms: int, method: str, path: str) -> dict[str, str]: ...


class Credentials:
    __slots__ = ("_key_id", "_private_key")
    _key_id: str
    _private_key: rsa.RSAPrivateKey

    def __init__(self) -> None:
        raise TypeError("Create credentials with Credentials.from_env()")

    @classmethod
    def from_env(cls) -> "Credentials":
        key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        if not key_id.strip():
            raise ValueError("KALSHI_API_KEY_ID is required")
        if any(ord(char) < 33 or ord(char) > 126 for char in key_id):
            raise ValueError("KALSHI_API_KEY_ID contains invalid header characters")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        key_pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")
        if key_path and key_pem:
            raise ValueError("Set only one of KALSHI_PRIVATE_KEY_PATH and KALSHI_PRIVATE_KEY_PEM")
        if not key_path and not key_pem:
            raise ValueError("KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM is required")
        try:
            material = Path(key_path).read_bytes() if key_path else key_pem.encode("utf-8")
        except OSError:
            raise ValueError("Unable to read KALSHI_PRIVATE_KEY_PATH") from None
        try:
            private_key = serialization.load_pem_private_key(material, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError(
                "KALSHI_PRIVATE_KEY must contain an unencrypted RSA private key"
            ) from None
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("KALSHI_PRIVATE_KEY must contain an RSA private key")
        credentials = cls.__new__(cls)
        credentials._key_id = key_id
        credentials._private_key = private_key
        return credentials

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()

    def headers(self, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        if timestamp_ms < 0:
            raise ValueError("timestamp_ms must be nonnegative")
        sign_path = path.split("?", 1)[0]
        if not sign_path.startswith("/") or "#" in sign_path:
            raise ValueError("Signing requires an absolute URL path without a fragment")
        message = f"{timestamp_ms}{method.upper()}{sign_path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }
