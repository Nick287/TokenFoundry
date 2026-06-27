"""JWT issue/verify for self-hosted login.

HS256 signed with settings.jwt_secret (injected from Key Vault in cloud). The
token carries the same claims the Principal needs: subject, role, tenant_id.
auth.py verifies these tokens; this is the signing counterpart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from app.config import get_settings


def issue_token(subject: str, role: str, tenant_id: str | None) -> str:
    """Sign a short-lived HS256 JWT for an authenticated user."""
    settings = get_settings()
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "role": role,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Verify signature + expiry and return the claims. Raises JWTError if invalid."""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


__all__ = ["issue_token", "decode_token", "JWTError"]
