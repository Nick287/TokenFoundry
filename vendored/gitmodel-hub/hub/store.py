"""Persistence layer for GitModel Hub (SQLite).

Stores three things:

* the long-lived Copilot OAuth token (single row),
* locally issued hub API keys (used by Codex / Claude Code / curl clients),
* per-request token usage records for statistics.

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

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    api_key       TEXT,
    model         TEXT,
    served_model  TEXT,
    endpoint      TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    streamed      INTEGER NOT NULL DEFAULT 0,
    estimated     INTEGER NOT NULL DEFAULT 0,
    status        INTEGER NOT NULL DEFAULT 200
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model);
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
    """Apply in-place schema upgrades for existing databases."""
    cols = {row["name"] for row in c.execute("PRAGMA table_info(usage)")}
    if "cached_tokens" not in cols:
        c.execute(
            "ALTER TABLE usage ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0"
        )


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
# Pricing (USD per 1M tokens, split input / output) — used for cost estimates
# --------------------------------------------------------------------------- #
# Public list prices (USD per 1M tokens) as of the Anthropic pricing page;
# the admin can edit them in the portal. "default" is the fallback applied to
# any model not listed. Source: platform.claude.com/docs/.../pricing
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "default": {"input": 3.0, "output": 15.0},
    # Claude Sonnet (3.5 / 3.7 / 4 / 4.5 / 4.6) — $3 / $15
    "claude-3.5-sonnet": {"input": 3.0, "output": 15.0},
    "claude-3.7-sonnet": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4.5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4.6": {"input": 3.0, "output": 15.0},
    # Claude Opus 4 / 4.1 (older tier) — $15 / $75
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-opus-4.1": {"input": 15.0, "output": 75.0},
    # Claude Opus 4.5 / 4.6 / 4.7 / 4.8 (current tier) — $5 / $25
    "claude-opus-4.5": {"input": 5.0, "output": 25.0},
    "claude-opus-4.6": {"input": 5.0, "output": 25.0},
    "claude-opus-4.7": {"input": 5.0, "output": 25.0},
    "claude-opus-4.8": {"input": 5.0, "output": 25.0},
    # Claude Haiku — 4.5: $1 / $5, 3.5: $0.80 / $4
    "claude-haiku-4.5": {"input": 1.0, "output": 5.0},
    "claude-3.5-haiku": {"input": 0.80, "output": 4.0},
    # OpenAI-side models (rough public list prices, for reference)
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-5.5": {"input": 1.25, "output": 10.0},
    "gpt-5.3-codex": {"input": 1.25, "output": 10.0},
    # Google Gemini (rates observed from Copilot's copilot_usage payload)
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.0},
    "gemini-3.1-pro-preview": {"input": 2.0, "output": 12.0},
    "gemini-3.5-flash": {"input": 1.5, "output": 9.0},
    # Azure OpenAI image model (token-based; image output tokens dominate)
    "gpt-image-2": {"input": 5.0, "output": 40.0},
}


def get_pricing() -> dict[str, dict[str, float]]:
    """Return the saved pricing table merged over the defaults.

    Each model has ``input`` / ``output`` and an optional ``cache_read`` rate
    (USD per 1M tokens). When ``cache_read`` is absent it defaults to
    ``input * 0.1`` — matching Anthropic's cache-hit pricing (0.1x base input).
    """
    import json

    pricing = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
    raw = get_setting("pricing")
    if raw:
        try:
            saved = json.loads(raw)
            if isinstance(saved, dict):
                for model, rates in saved.items():
                    if isinstance(rates, dict):
                        entry = {
                            "input": float(rates.get("input", 0) or 0),
                            "output": float(rates.get("output", 0) or 0),
                        }
                        if rates.get("cache_read") is not None:
                            entry["cache_read"] = float(rates.get("cache_read") or 0)
                        pricing[model] = entry
        except (ValueError, TypeError):
            pass
    # Fill in a default cache_read (0.1x input) wherever it's missing.
    for rates in pricing.values():
        rates.setdefault("cache_read", round(rates.get("input", 0) * 0.1, 6))
    return pricing


def set_pricing(pricing: dict[str, Any]) -> None:
    import json

    clean: dict[str, dict[str, float]] = {}
    for model, rates in (pricing or {}).items():
        if not isinstance(rates, dict):
            continue
        try:
            entry = {
                "input": float(rates.get("input", 0) or 0),
                "output": float(rates.get("output", 0) or 0),
            }
            if rates.get("cache_read") is not None:
                entry["cache_read"] = float(rates.get("cache_read") or 0)
            clean[str(model)] = entry
        except (ValueError, TypeError):
            continue
    set_setting("pricing", json.dumps(clean))


def estimate_cost(
    pricing: dict[str, dict[str, float]], model: str | None,
    input_tokens: int, output_tokens: int, cached_tokens: int = 0,
) -> float:
    """USD cost for a row given the pricing table (per 1M tokens).

    ``cached_tokens`` are a subset of ``input_tokens`` that hit the prompt
    cache; they are billed at the (cheaper) ``cache_read`` rate, and the
    remaining uncached input at the normal ``input`` rate.
    """
    rates = pricing.get(model or "") or pricing.get("default") or {}
    in_rate = rates.get("input", 0)
    cache_rate = rates.get("cache_read", in_rate * 0.1)
    cached = max(0, min(cached_tokens or 0, input_tokens or 0))
    uncached = (input_tokens or 0) - cached
    cost_in = (uncached / 1_000_000) * in_rate
    cost_cache = (cached / 1_000_000) * cache_rate
    cost_out = (output_tokens / 1_000_000) * rates.get("output", 0)
    return cost_in + cost_cache + cost_out


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


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
def record_usage(
    *,
    api_key: str | None,
    model: str | None,
    served_model: str | None,
    endpoint: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    streamed: bool,
    estimated: bool,
    status: int = 200,
    cached_tokens: int = 0,
) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO usage(ts, api_key, model, served_model, endpoint, "
            "input_tokens, output_tokens, total_tokens, cached_tokens, "
            "streamed, estimated, status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                api_key,
                model,
                served_model,
                endpoint,
                int(input_tokens or 0),
                int(output_tokens or 0),
                int(total_tokens or 0),
                int(cached_tokens or 0),
                1 if streamed else 0,
                1 if estimated else 0,
                int(status),
            ),
        )


def usage_summary(since: float | None, until: float | None) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if until is not None:
        where.append("ts <= ?")
        params.append(until)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # Same predicate but table-qualified, for queries that JOIN api_keys.
    uclause = clause.replace("ts ", "u.ts ") if clause else ""

    with _conn() as c:
        totals = c.execute(
            f"SELECT COUNT(*) AS requests, "
            f"COALESCE(SUM(input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
            f"COALESCE(SUM(total_tokens),0) AS total_tokens "
            f"FROM usage {clause}",
            params,
        ).fetchone()

        by_model = c.execute(
            f"SELECT model, COUNT(*) AS requests, "
            f"COALESCE(SUM(input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
            f"COALESCE(SUM(total_tokens),0) AS total_tokens "
            f"FROM usage {clause} GROUP BY model ORDER BY total_tokens DESC",
            params,
        ).fetchall()

        by_day = c.execute(
            f"SELECT date(ts, 'unixepoch', 'localtime') AS day, "
            f"COALESCE(SUM(total_tokens),0) AS total_tokens, "
            f"COUNT(*) AS requests "
            f"FROM usage {clause} GROUP BY day ORDER BY day",
            params,
        ).fetchall()

        # Per-API-key token totals, joined to api_keys for a friendly name.
        by_api_key = c.execute(
            f"SELECT u.api_key AS key, "
            f"COALESCE(k.name, '') AS key_name, "
            f"COUNT(*) AS requests, "
            f"COALESCE(SUM(u.input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(u.output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(u.cached_tokens),0) AS cached_tokens, "
            f"COALESCE(SUM(u.total_tokens),0) AS total_tokens "
            f"FROM usage u LEFT JOIN api_keys k ON u.api_key = k.key {uclause} "
            f"GROUP BY u.api_key ORDER BY total_tokens DESC",
            params,
        ).fetchall()

        # (key, model) breakdown — used to compute per-key cost (cost is
        # per-model, but a key spans models). Not returned directly.
        by_key_model = c.execute(
            f"SELECT u.api_key AS key, u.model AS model, "
            f"COALESCE(SUM(u.input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(u.output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(u.cached_tokens),0) AS cached_tokens "
            f"FROM usage u {uclause} GROUP BY u.api_key, u.model",
            params,
        ).fetchall()

    return {
        "totals": dict(totals),
        "by_model": [dict(r) for r in by_model],
        "by_day": [dict(r) for r in by_day],
        "by_api_key": [dict(r) for r in by_api_key],
        "_by_key_model": [dict(r) for r in by_key_model],
        "pricing": pricing_table(),
    }


def pricing_table() -> dict[str, dict[str, float]]:
    """Public alias kept stable for callers/tests."""
    return get_pricing()


def usage_summary_with_cost(since: float | None, until: float | None) -> dict[str, Any]:
    """usage_summary plus per-model, per-key and total USD cost estimates."""
    data = usage_summary(since, until)
    pricing = data["pricing"]
    total_cost = 0.0
    for row in data["by_model"]:
        cost = estimate_cost(
            pricing, row.get("model"),
            row.get("input_tokens", 0), row.get("output_tokens", 0),
            row.get("cached_tokens", 0),
        )
        row["cost"] = round(cost, 4)
        total_cost += cost
    data["totals"]["cost"] = round(total_cost, 4)

    # Per-key cost: cost is per-model, so sum each (key, model) slice.
    cost_by_key: dict[Any, float] = {}
    for row in data.pop("_by_key_model", []):
        cost_by_key[row.get("key")] = cost_by_key.get(row.get("key"), 0.0) + estimate_cost(
            pricing, row.get("model"),
            row.get("input_tokens", 0), row.get("output_tokens", 0),
            row.get("cached_tokens", 0),
        )
    for row in data["by_api_key"]:
        row["cost"] = round(cost_by_key.get(row.get("key"), 0.0), 4)

    return data


def recent_usage(limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, api_key, model, served_model, endpoint, input_tokens, "
            "output_tokens, total_tokens, cached_tokens, streamed, estimated, status "
            "FROM usage ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
