"""Runtime configuration for GitModel Hub.

Everything is driven by environment variables (optionally loaded from a `.env`
file discovered by walking up from the current working directory). Sensible
defaults make the hub run on localhost with zero configuration.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _find_dotenv() -> Path | None:
    cur = Path.cwd().resolve()
    for d in [cur, *cur.parents]:
        p = d / ".env"
        if p.is_file():
            return p
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        key, _, val = s.partition("=")
        key, val = key.strip(), val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


# Inject .env into the process environment (without overriding existing vars).
_DOTENV_PATH = _find_dotenv()
if _DOTENV_PATH:
    for _k, _v in _parse_env_file(_DOTENV_PATH).items():
        os.environ.setdefault(_k, _v)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return int(v.strip())
    except ValueError:
        return default


class Settings:
    """Hub settings resolved from the environment."""

    def __init__(self) -> None:
        # Where to store the SQLite DB + oauth token. Defaults to a `db/`
        # folder next to the project root (the parent of the `hub` package).
        data_dir = os.environ.get("HUB_DATA_DIR")
        self.data_dir = (
            Path(data_dir).expanduser()
            if data_dir
            else Path(__file__).resolve().parent.parent / "db"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / "hub.db"

        # SQLite journal mode. Defaults to WAL (fast, single-writer friendly)
        # for local disks. WAL relies on memory-mapped shared memory and does
        # NOT work on network filesystems such as Azure Files (SMB), where it
        # fails with "database is locked"; set HUB_DB_JOURNAL_MODE=TRUNCATE (or
        # DELETE) there. Only valid SQLite modes are accepted.
        _jm = os.environ.get("HUB_DB_JOURNAL_MODE", "WAL").strip().upper()
        _valid = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}
        self.journal_mode = _jm if _jm in _valid else "WAL"

        # Network binding for the server.
        self.host = os.environ.get("HUB_HOST", "127.0.0.1")
        self.port = int(os.environ.get("HUB_PORT", "8088"))

        # When True every /v1/* request must present a valid hub API key.
        self.require_auth = _bool("HUB_REQUIRE_AUTH", False)

        # Optional admin token guarding the management endpoints / portal
        # actions (login, key management). Empty => no portal auth (localhost).
        self.admin_token = os.environ.get("HUB_ADMIN_TOKEN", "").strip()

        # Allow seeding the Copilot OAuth token directly from the environment.
        self.copilot_oauth_token = os.environ.get("COPILOT_OAUTH_TOKEN", "").strip()

        # A single hub /v1 API key injected at deploy time (control-plane
        # managed, Key Vault-backed). Accepted for /v1/* auth IN ADDITION to any
        # keys created via the portal, so an orchestrator authenticates without
        # the portal flow — the inbound counterpart to COPILOT_OAUTH_TOKEN
        # (outbound). Because the hub is stateless (ephemeral SQLite), this env
        # key is the durable credential; portal-created keys don't survive a
        # cold start, but this one is re-injected every deploy.
        self.hub_api_key = os.environ.get("HUB_API_KEY", "").strip()

        # Admin login rate limiting (brute-force protection). After
        # `login_max_fails` consecutive failures from one client IP, that IP is
        # locked out for `login_lock_seconds`. Set max_fails <= 0 to disable.
        self.login_max_fails = _int("HUB_LOGIN_MAX_FAILS", 5)
        self.login_lock_seconds = _int("HUB_LOGIN_LOCK_SECONDS", 15 * 60)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
