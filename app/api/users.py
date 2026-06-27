"""User management router — admin CRUD + self-service password change.

Admin (require_admin) can list/create/update/reset/delete users. Any signed-in
user can change their OWN password via /me/password. Guards prevent locking the
platform out: you cannot delete or disable yourself, nor delete the last admin.

Reuses app/services/passwords for PBKDF2 hashing and the existing Principal/auth
helpers. Customers MUST be bound to an existing tenant (the isolation red line).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import Principal, get_principal, require_admin
from app.db import get_db
from app.models.enums import UserRole
from app.models.orm import Tenant, User
from app.models.schemas import (
    PasswordChange,
    PasswordReset,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services.passwords import hash_password, verify_password

router = APIRouter()


def _require_tenant_for_customer(db: Session, role: UserRole, tenant_id: str | None) -> None:
    """Customers must be bound to an existing tenant; admins must not."""
    if role == UserRole.CUSTOMER:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer users must be bound to a tenant",
            )
        if not db.get(Tenant, tenant_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found"
            )


@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db), _: Principal = Depends(require_admin)
) -> list[User]:
    return list(db.query(User).all())


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> User:
    if db.query(User).filter(User.username == body.username).one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="username already exists"
        )
    _require_tenant_for_customer(db, body.role, body.tenant_id)
    user = User(
        id=f"usr_{uuid.uuid4().hex[:12]}",
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        # admins are cross-tenant: force tenant_id null regardless of input
        tenant_id=body.tenant_id if body.role == UserRole.CUSTOMER else None,
        disabled=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    body: UserUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    is_self = user.username == principal.subject
    new_role = body.role if body.role is not None else user.role
    new_tenant = body.tenant_id if body.tenant_id is not None else user.tenant_id

    # Don't let an admin lock themselves out (demote or disable self).
    if is_self and body.role is not None and body.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="cannot demote yourself"
        )
    if is_self and body.disabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="cannot disable yourself"
        )

    _require_tenant_for_customer(db, new_role, new_tenant)

    user.role = new_role
    user.tenant_id = new_tenant if new_role == UserRole.CUSTOMER else None
    if body.disabled is not None:
        user.disabled = body.disabled
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/reset-password", response_model=UserOut)
def reset_password(
    user_id: str,
    body: PasswordReset,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> None:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if user.username == principal.subject:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="cannot delete yourself"
        )
    if user.role == UserRole.ADMIN:
        admin_count = db.query(User).filter(User.role == UserRole.ADMIN).count()
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cannot delete the last admin",
            )
    db.delete(user)
    db.commit()


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_my_password(
    body: PasswordChange,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> None:
    user = db.query(User).filter(User.username == principal.subject).one_or_none()
    if not user or not verify_password(body.old_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="current password incorrect"
        )
    user.password_hash = hash_password(body.new_password)
    db.commit()
