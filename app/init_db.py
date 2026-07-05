"""Database bootstrap — create tables and seed the first admin user.

Runs once at application startup (via the FastAPI lifespan in app/main.py),
NOT from any business endpoint. Idempotent: create_all skips existing tables,
and the admin seed is a check-then-insert guarded against the unique
constraint so concurrent replicas don't double-insert.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models.enums import UserRole
from app.models.orm import Base, User
from app.services.passwords import hash_password

logger = logging.getLogger(__name__)


def init_db() -> None:
    """Create all tables, then seed the admin user if absent."""
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    logger.info("init_db: tables ensured")
    _seed_admin()


def _ensure_columns() -> None:
    """Lightweight idempotent migrations for columns added after a table already
    exists (create_all won't ALTER existing tables). Postgres supports
    ADD COLUMN IF NOT EXISTS, so this is safe to run on every startup."""
    from sqlalchemy import text

    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled boolean NOT NULL DEFAULT false",
        "ALTER TABLE model_routes ADD COLUMN IF NOT EXISTS api_version varchar(64)",
        "ALTER TABLE github_accounts ADD COLUMN IF NOT EXISTS hub_key_kv_ref varchar(512)",
        "ALTER TABLE github_accounts ADD COLUMN IF NOT EXISTS admin_token_kv_ref varchar(512)",
        # Per-key gateway limits (replace the retired tpm_tier / budget columns).
        # token_quota is a TIER label (varchar) not a number — APIM's token-quota
        # attribute can't take an expression, so the amount is a policy literal.
        "ALTER TABLE virtual_keys ADD COLUMN IF NOT EXISTS tokens_per_minute integer",
        "ALTER TABLE virtual_keys ADD COLUMN IF NOT EXISTS token_quota_tier varchar(16)",
        "ALTER TABLE virtual_keys ADD COLUMN IF NOT EXISTS token_quota_period varchar(16)",
        # Retire the dead key-level fields (real $ budgets live in the Budget table).
        "ALTER TABLE virtual_keys DROP COLUMN IF EXISTS tpm_tier",
        "ALTER TABLE virtual_keys DROP COLUMN IF EXISTS monthly_budget_usd",
        "ALTER TABLE virtual_keys DROP COLUMN IF EXISTS budget_action",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception:
                logger.exception("init_db: ensure-column failed: %s", stmt)


def _seed_admin() -> None:
    settings = get_settings()
    if not settings.admin_password:
        logger.warning("init_db: TF_ADMIN_PASSWORD empty; skipping admin seed")
        return

    db = SessionLocal()
    try:
        existing = (
            db.query(User).filter(User.username == settings.admin_username).one_or_none()
        )
        if existing:
            logger.info("init_db: admin user already present")
            return
        admin = User(
            id=f"usr_{uuid.uuid4().hex[:12]}",
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            role=UserRole.ADMIN,
            tenant_id=None,
        )
        db.add(admin)
        db.commit()
        logger.info("init_db: seeded admin user '%s'", settings.admin_username)
    except IntegrityError:
        db.rollback()
        logger.info("init_db: admin user seeded concurrently by another replica")
    finally:
        db.close()
