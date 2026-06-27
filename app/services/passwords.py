"""Password hashing — PBKDF2-HMAC-SHA256 via the standard library only.

No third-party crypto dependency (keeps the image slim). Stored format:
    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
verify_password is constant-time via hmac.compare_digest.
"""

from __future__ import annotations

import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 240_000
_SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """Return an encoded PBKDF2 hash for a plaintext password."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a plaintext password against an encoded hash."""
    try:
        algo, iter_str, salt_hex, hash_hex = encoded.split("$", 3)
        if algo != _ALGO:
            return False
        iterations = int(iter_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)
