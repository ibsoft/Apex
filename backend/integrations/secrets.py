"""Encryption of stored connection credentials.

Credentials (passwords, tokens, api keys) live in the connections table next to
non-secret config, so the payload is encrypted at rest. The Fernet key is
derived from ``config.SECRET_KEY`` (PBKDF2), which the deployment already keeps
on disk chmod 0600 as ``DATA_DIR/session.key`` - no new secret to manage. If the
secret key rotates, old rows cannot be decrypted and are reported as such rather
than silently returning garbage.
"""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken


def _derive_key() -> bytes:
    from config import config

    salt = b"apex-integrations-v1"
    digest = hashlib.pbkdf2_hmac(
        "sha256", str(config.SECRET_KEY).encode("utf-8"), salt, 200_000, dklen=32
    )
    return base64.urlsafe_b64encode(digest)


try:
    _FERNET = Fernet(_derive_key())
except Exception:  # pragma: no cover - cryptography missing in a broken venv
    _FERNET = None  # type: ignore[assignment]


def encrypt(plain: dict) -> str:
    """Encrypt a JSON-serialisable dict, or raise if the crypto cannot start."""
    if _FERNET is None:  # pragma: no cover
        raise RuntimeError("cryptography is not installed; cannot store credentials")
    return _FERNET.encrypt(json.dumps(plain, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decrypt(blob: str) -> dict:
    """Decrypt a previously encrypted payload, or raise InvalidToken if the
    secret key changed since it was written."""
    if _FERNET is None:  # pragma: no cover
        raise RuntimeError("cryptography is not installed")
    raw = _FERNET.decrypt(blob.encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def decrypt_safe(blob: str) -> dict | None:
    """Like :func:`decrypt` but returns None on any failure, so callers can
    report "re-enter the credentials" instead of crashing."""
    try:
        return decrypt(blob)
    except (InvalidToken, ValueError, TypeError, RuntimeError):
        return None