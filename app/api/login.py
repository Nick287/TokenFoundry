"""Login router — self-hosted, database-backed auth (no Entra).

POST /login  : verify username/password against the users table, issue a JWT.
GET  /me     : echo the caller's principal (requires a valid token).

The issued JWT is what auth.py verifies on every protected endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import Principal, get_principal
from app.db import get_db
from app.models.orm import User
from app.models.schemas import LoginRequest, MeResponse, TokenResponse
from app.services.passwords import verify_password
from app.services.tokens import issue_token

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.username == body.username).one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
        )
    if user.disabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="account disabled",
        )
    token = issue_token(subject=user.username, role=user.role.value, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token, role=user.role, tenant_id=user.tenant_id)


@router.get("/me", response_model=MeResponse)
def me(principal: Principal = Depends(get_principal)) -> MeResponse:
    return MeResponse(
        subject=principal.subject, role=principal.role, tenant_id=principal.tenant_id
    )
