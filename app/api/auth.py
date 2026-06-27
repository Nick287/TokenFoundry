"""Authentication + authorization — the multi-tenant isolation red line.

Two identity sources (per plan):
  * platform admins  -> Microsoft Entra ID            (role=admin)
  * customers         -> Microsoft Entra External ID  (role=customer)

A validated principal carries `role` and `tenant_id` claims. The golden rule:
the backend NEVER trusts a tenant id from the request body/query — it derives
the caller's tenant from the token and forces every customer query to filter by
it. `require_admin` gates platform-only operations; `tenant_scope` yields the
caller's enforced tenant for customer endpoints.

Token signature validation against the issuer JWKS is wired via python-jose;
for local dev (no IdP) a TF_ENVIRONMENT=local short-circuit accepts a dev token
so the stack runs end-to-end without Entra. That bypass is OFF in dev/prod.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from app.config import get_settings
from app.models.enums import UserRole


@dataclass(frozen=True)
class Principal:
    subject: str
    role: UserRole
    tenant_id: str | None  # None allowed for admins (cross-tenant)


def _decode_local_dev(token: str) -> Principal:
    """Local-only: token of form 'dev:<role>:<tenant_id>' — never in cloud."""
    try:
        _, role, tenant = token.split(":", 2)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="bad dev token"
        ) from exc
    return Principal(
        subject="dev-user",
        role=UserRole(role),
        tenant_id=tenant or None,
    )


def _decode_jwt(token: str) -> Principal:
    """Validate a self-hosted HS256 JWT and extract role + tenant.

    Verifies signature and expiry against settings.jwt_secret (the secret is
    injected from Key Vault in cloud). Claims are issued by app/services/tokens
    at login: sub, role, tenant_id. (The earlier Entra/JWKS path is replaced by
    this database-backed login; AAD can be layered back in later.)
    """
    from app.services.tokens import JWTError, decode_token

    try:
        claims = decode_token(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        ) from exc

    try:
        role = UserRole(claims.get("role", "customer"))
    except ValueError:
        role = UserRole.CUSTOMER

    tenant_id = claims.get("tenant_id")
    return Principal(
        subject=claims.get("sub", "unknown"), role=role, tenant_id=tenant_id
    )


def get_principal(authorization: str = Header(default="")) -> Principal:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    token = authorization[7:]
    if get_settings().is_local and token.startswith("dev:"):
        return _decode_local_dev(token)
    return _decode_jwt(token)


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin role required"
        )
    return principal


def tenant_scope(principal: Principal = Depends(get_principal)) -> str:
    """Return the caller's enforced tenant id for customer endpoints.

    Customers MUST be bound to a tenant; admins acting on a customer endpoint
    are rejected here (they use admin endpoints with explicit tenant params).
    """
    if principal.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no tenant bound to principal",
        )
    return principal.tenant_id
