"""Persistence layer for GitModel Hub (SQLite).

Stores two things:

* the long-lived Copilot OAuth token + generic settings (single `kv` table),
* locally issued hub API keys (used by Codex / Claude Code / curl clients).

Usage records are deliberately NOT stored here. They go straight to Azure Event
Hub (`hub.eventhub`) carrying upstream's `copilot_usage` verbatim, and the
control plane computes cost downstream — so the hub holds no usage table and no
price table. A local store would not have been a usable fallback anyway: this
SQLite DB lives on ephemeral container storage and is empty after any restart.

A small connection-per-call model keeps things thread-safe under the FastAPI
threadpool without needing an async DB driver.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);
"""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    db = get_settings().db_path
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA journal_mode={get_settings().journal_mode};")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """Apply in-place schema upgrades for existing databases.

    Drops the retired `usage` table: usage now goes to Event Hub, never to
    SQLite. Harmless on the ephemeral deployment (the DB starts empty every
    cold start); it matters for long-lived local dev databases.
    """
    c.execute("DROP TABLE IF EXISTS usage")


# --------------------------------------------------------------------------- #
# OAuth token (key/value)
# --------------------------------------------------------------------------- #
def get_oauth_token() -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key='oauth_token'").fetchone()
        return row["value"] if row else None


def set_oauth_token(token: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES('oauth_token', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (token,),
        )


def clear_oauth_token() -> None:
    with _conn() as c:
        c.execute("DELETE FROM kv WHERE key='oauth_token'")


# --------------------------------------------------------------------------- #
# Generic settings (kv) + admin credentials
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str | None = None) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _hash_password(password: str, salt: str) -> str:
    import hashlib

    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()


def ensure_admin_defaults() -> None:
    """Seed default admin/admin credentials on first run."""
    if get_setting("admin_username") is None:
        set_setting("admin_username", "admin")
    if get_setting("admin_pw_salt") is None or get_setting("admin_pw_hash") is None:
        salt = secrets.token_hex(8)
        set_setting("admin_pw_salt", salt)
        set_setting("admin_pw_hash", _hash_password("admin", salt))


def get_admin_username() -> str:
    return get_setting("admin_username", "admin") or "admin"


def verify_admin(username: str, password: str) -> bool:
    if username != get_admin_username():
        return False
    salt = get_setting("admin_pw_salt") or ""
    expected = get_setting("admin_pw_hash") or ""
    return bool(expected) and secrets.compare_digest(
        _hash_password(password, salt), expected
    )


def set_admin_credentials(username: str, password: str) -> None:
    salt = secrets.token_hex(8)
    set_setting("admin_username", username)
    set_setting("admin_pw_salt", salt)
    set_setting("admin_pw_hash", _hash_password(password, salt))


def get_require_auth(default: bool) -> bool:
    val = get_setting("require_auth")
    if val is None:
        return default
    return val == "1"


def set_require_auth(enabled: bool) -> None:
    set_setting("require_auth", "1" if enabled else "0")


# --------------------------------------------------------------------------- #
# Azure image backend config (endpoint / api key / default model) — JSON blob
# --------------------------------------------------------------------------- #
def get_image_config() -> dict[str, str]:
    """Return the saved Azure image backend config (any field may be empty)."""
    import json

    raw = get_setting("image_config")
    if raw:
        try:
            cfg = json.loads(raw)
            if isinstance(cfg, dict):
                return {
                    "endpoint": str(cfg.get("endpoint") or ""),
                    "api_key": str(cfg.get("api_key") or ""),
                    "model": str(cfg.get("model") or ""),
                }
        except (ValueError, TypeError):
            pass
    return {"endpoint": "", "api_key": "", "model": ""}


def set_image_config(
    endpoint: str | None, api_key: str | None, model: str | None
) -> dict[str, str]:
    """Persist the Azure image backend config.

    A blank/None ``api_key`` preserves the previously stored key, so the portal
    never has to re-echo the secret back to the browser to keep it.
    """
    import json

    current = get_image_config()
    new_key = (api_key or "").strip()
    cfg = {
        "endpoint": (endpoint or "").strip(),
        "api_key": new_key or current.get("api_key", ""),
        "model": (model or "").strip(),
    }
    set_setting("image_config", json.dumps(cfg))
    return cfg


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
def create_api_key(name: str) -> dict[str, Any]:
    key = "sk-hub-" + secrets.token_urlsafe(32)
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO api_keys(key, name, created_at, revoked) VALUES(?,?,?,0)",
            (key, name, now),
        )
    return {"key": key, "name": name, "created_at": now, "revoked": False}


def list_api_keys() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT key, name, created_at, revoked FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(key: str) -> None:
    with _conn() as c:
        c.execute("UPDATE api_keys SET revoked=1 WHERE key=?", (key,))


def is_valid_api_key(key: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM api_keys WHERE key=? AND revoked=0", (key,)
        ).fetchone()
        return row is not None

