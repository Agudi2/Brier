import base64
import logging
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from recorder.auth import Credentials
from tests.recorder.helpers import ready


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def credential_env(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey) -> str:
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    monkeypatch.setenv("KALSHI_API_KEY_ID", "secret-test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PEM", pem)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    return pem


def verify(headers: dict[str, str], key: rsa.RSAPrivateKey, message: bytes) -> None:
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"], validate=True),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )


@pytest.mark.parametrize("path", ["/trade-api/v2/markets", "/trade-api/v2/markets?limit=7"])
def test_signs_full_path_without_query(
    credential_env: str, rsa_key: rsa.RSAPrivateKey, path: str
) -> None:
    credentials = ready(Credentials.from_env)
    headers = ready(credentials.headers, 1703123456789, "GET", path)
    assert headers["KALSHI-ACCESS-KEY"] == "secret-test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"
    # Literal payload is specified by the authentication docs; 32 = SHA-256 digest bytes.
    verify(headers, rsa_key, b"1703123456789GET/trade-api/v2/markets")
    with pytest.raises(InvalidSignature):
        verify(headers, rsa_key, b"1703123456789GET/markets")
    with pytest.raises(InvalidSignature):
        verify(headers, rsa_key, b"1703123456789GET/trade-api/v2/markets?limit=7")


def test_path_environment_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential_env: str, rsa_key: rsa.RSAPrivateKey
) -> None:
    path = tmp_path / "private.pem"
    path.write_text(credential_env)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PEM")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(path))
    credentials = ready(Credentials.from_env)
    verify(
        ready(credentials.headers, 1, "GET", "/trade-api/v2/markets"),
        rsa_key,
        b"1GET/trade-api/v2/markets",
    )


@pytest.mark.parametrize(
    "missing, message",
    [
        (("KALSHI_API_KEY_ID",), "KALSHI_API_KEY_ID"),
        (("KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_PATH"), "KALSHI_PRIVATE_KEY"),
    ],
)
def test_missing_environment_is_clear(
    monkeypatch: pytest.MonkeyPatch, credential_env: str, missing: tuple[str, ...], message: str
) -> None:
    for name in missing:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match=message):
        ready(Credentials.from_env)


def test_repr_and_logs_redact_all_credentials(
    credential_env: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    credentials = ready(Credentials.from_env)
    ready(credentials.headers, 1, "GET", "/trade-api/v2/markets")
    visible = repr(credentials) + str(credentials) + caplog.text
    for secret in ("secret-test-key-id", credential_env, credential_env.splitlines()[1]):
        assert secret not in visible


def test_malformed_key_error_does_not_leak(
    monkeypatch: pytest.MonkeyPatch, credential_env: str, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "invalid-private-key-material"
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PEM", secret)
    with pytest.raises(ValueError) as error:
        ready(Credentials.from_env)
    assert secret not in str(error.value) + caplog.text


def test_ambiguous_key_sources_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential_env: str
) -> None:
    path = tmp_path / "private.pem"
    path.write_text(credential_env)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(path))
    with pytest.raises(ValueError, match="(?i)(both|one|ambiguous)"):
        ready(Credentials.from_env)
