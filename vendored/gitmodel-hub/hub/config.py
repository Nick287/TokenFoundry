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


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v.strip())
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

        # `anthropic-version` sent upstream when the caller did not supply one.
        # This is the API contract version, not a feature flag: 2023-06-01 has
        # been the current value since the Messages API launched (2023-01-01 is
        # the legacy predecessor), and unlike `anthropic-beta` it does not roll
        # per feature. Configurable so a future bump is a redeploy rather than a
        # code change; callers that send their own header are forwarded as-is.
        self.anthropic_version = (
            os.environ.get("HUB_ANTHROPIC_VERSION", "").strip() or "2023-06-01"
        )

        # Admin login rate limiting (brute-force protection). After
        # `login_max_fails` consecutive failures from one client IP, that IP is
        # locked out for `login_lock_seconds`. Set max_fails <= 0 to disable.
        self.login_max_fails = _int("HUB_LOGIN_MAX_FAILS", 5)
        self.login_lock_seconds = _int("HUB_LOGIN_LOCK_SECONDS", 15 * 60)

        # Azure Event Hub — the hub's only usage-record outlet. One event per
        # completed /v1 request carrying the upstream `copilot_usage` verbatim;
        # the control plane drains Event Hub Capture into Cosmos and does the
        # cost arithmetic there. Leave `eventhub_fqdn` empty (the default) and
        # emission is a no-op, so the hub still runs standalone with no Azure
        # dependency. Auth is the container's user-assigned managed identity —
        # `eventhub_client_id` is that identity's client id; blank falls back to
        # DefaultAzureCredential's own resolution (system-assigned MI, az login).
        self.eventhub_fqdn = os.environ.get("TF_EVENTHUB_FQDN", "").strip()
        self.eventhub_name = os.environ.get("TF_EVENTHUB_NAME", "").strip()
        self.eventhub_client_id = os.environ.get("TF_EVENTHUB_CLIENT_ID", "").strip()

        # Which deployment this is. Every hub publishes to the SAME Event Hub, so
        # without it a usage record cannot say which GitHub account's Copilot
        # quota actually served the call — fine for billing a tenant (that keys
        # off the APIM subscription), not fine for reconciling against the
        # per-account bill GitHub sends us. The control plane injects its own
        # GitHubAccount.id (`gha_…`), so the Cosmos field joins straight back to
        # that row. Blank on a standalone hub, which is honest: there is no
        # account registry to point at.
        self.hub_id = os.environ.get("TF_HUB_ID", "").strip()

        # Buffered-producer tuning. `max_wait_time` bounds how long an event
        # sits in the SDK buffer before being flushed — it is also the data-loss
        # window if the instance is killed ungracefully (a graceful shutdown
        # flushes). `max_buffer_length` bounds memory.
        #
        # CORRECTION: an earlier comment here claimed that past the buffer limit
        # "the SDK invokes the error callback and the event is dropped rather
        # than blocking the request path". That is not what buffered mode does —
        # `send_event()` WAITS for buffer space. Without an explicit timeout a
        # stalled namespace therefore blocks the request path, which is the
        # opposite of the promise in eventhub.py's docstring. Hence
        # `eventhub_send_timeout_seconds` below, which is passed on every send.
        self.eventhub_max_wait_seconds = _int("TF_EVENTHUB_MAX_WAIT_SECONDS", 5)
        self.eventhub_max_buffer = _int("TF_EVENTHUB_MAX_BUFFER", 5000)

        # Retry of events the broker would not take. Kept in memory only: the
        # hub is deliberately stateless (see infra/main.tf — SQLite lives in
        # /tmp), so this buys recovery from a transient outage, not durability
        # across an ungraceful kill.
        #
        # Sizing: a usage record carries `usage`/`copilot_usage` but NOT the
        # request/response bodies (those go to hub.audit), so ~1-2 KB each;
        # 1000 items is a couple of MB worst case. Six attempts with 2s
        # exponential backoff capped at 60s covers roughly two minutes of
        # outage, after which an event is given up on and counted as lost.
        self.eventhub_send_timeout_seconds = _float("TF_EVENTHUB_SEND_TIMEOUT_SECONDS", 5.0)
        self.eventhub_retry_max_queue = _int("TF_EVENTHUB_RETRY_MAX_QUEUE", 1000)
        self.eventhub_retry_max_attempts = _int("TF_EVENTHUB_RETRY_MAX_ATTEMPTS", 6)
        self.eventhub_retry_base_seconds = _float("TF_EVENTHUB_RETRY_BASE_SECONDS", 2.0)

        # Audit archive — raw request/response bodies for tenants that opted in
        # (APIM stamps `x-tf-audit`). Deliberately a DIFFERENT storage account
        # from Event Hub Capture: this one holds customer content, so it gets its
        # own retention and its own RBAC, and the control plane is not granted
        # read access to it. Empty `audit_account_url` (the default) disables
        # archival outright, regardless of what the header says.
        self.audit_account_url = os.environ.get("TF_AUDIT_ACCOUNT_URL", "").strip()
        self.audit_container = os.environ.get("TF_AUDIT_CONTAINER", "").strip()
        # Same managed identity as Event Hub unless overridden; blank falls back
        # to DefaultAzureCredential's own resolution.
        self.audit_client_id = (
            os.environ.get("TF_AUDIT_CLIENT_ID", "").strip() or self.eventhub_client_id
        )
        # Cap on the UNCOMPRESSED record. Past it the bodies are clipped and the
        # record is flagged `truncated` — it bounds gateway memory and per-blob
        # cost, not correctness. 4 MB holds a very large agent prompt whole.
        self.audit_max_bytes = _int("TF_AUDIT_MAX_BYTES", 4 * 1024 * 1024)

    @property
    def eventhub_enabled(self) -> bool:
        return bool(self.eventhub_fqdn and self.eventhub_name)

    @property
    def audit_enabled(self) -> bool:
        return bool(self.audit_account_url and self.audit_container)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
