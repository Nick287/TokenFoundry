"""Async GitHub Copilot client.

Handles the two-stage auth (long-lived OAuth token -> short-lived API token)
and forwards both buffered and streaming requests to the Copilot backend.

Mirrors the logic in the reference ``copilot.py`` / ``git_model.py`` but is
adapted for a long-running server: the OAuth token lives in the SQLite store
(or ``COPILOT_OAUTH_TOKEN``), and the API token is cached in memory.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

import httpx

from . import store
from .config import get_settings

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # public copilot.vim client_id
COPILOT_API_BASE = "https://api.githubcopilot.com"

# Mandatory editor-spoof headers — the Copilot backend rejects requests
# without them (HTTP 401 / 403).
EDITOR_HEADERS: dict[str, str] = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "GitHubCopilotChat/0.22.0",
}

_api_token_mem: dict[str, Any] = {}
_api_token_lock = asyncio.Lock()
_http_client: httpx.AsyncClient | None = None


class NotAuthenticatedError(RuntimeError):
    """Raised when no Copilot OAuth token is configured."""


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))
    return _http_client


# --------------------------------------------------------------------------- #
# OAuth token resolution
# --------------------------------------------------------------------------- #
def get_oauth_token() -> str:
    """Return the long-lived Copilot OAuth token or raise."""
    token = get_settings().copilot_oauth_token or store.get_oauth_token()
    if not token:
        raise NotAuthenticatedError(
            "No Copilot OAuth token configured. Log in via the web portal first."
        )
    return token


def is_authenticated() -> bool:
    return bool(get_settings().copilot_oauth_token or store.get_oauth_token())


def logout() -> None:
    store.clear_oauth_token()
    _api_token_mem.clear()


# --------------------------------------------------------------------------- #
# Device flow (web-portal friendly: start + poll)
# --------------------------------------------------------------------------- #
async def device_flow_start() -> dict[str, Any]:
    r = await _client().post(
        "https://github.com/login/device/code",
        headers={"Accept": "application/json"},
        data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
    )
    r.raise_for_status()
    info = r.json()
    return {
        "device_code": info["device_code"],
        "user_code": info["user_code"],
        "verification_uri": info["verification_uri"],
        "interval": info.get("interval", 5),
        "expires_in": info.get("expires_in", 900),
    }


async def device_flow_poll(device_code: str) -> dict[str, Any]:
    """Poll once. Returns {status: 'pending'|'success'|'error', ...}."""
    r = await _client().post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    data = r.json()
    if "access_token" in data:
        token = data["access_token"]
        store.set_oauth_token(token)
        _api_token_mem.clear()
        return {"status": "success"}
    err = data.get("error")
    if err in ("authorization_pending", "slow_down"):
        return {"status": "pending", "error": err}
    return {"status": "error", "error": err or "unknown", "detail": data}


# --------------------------------------------------------------------------- #
# Short-lived API token exchange (~30 min) — cached in memory
# --------------------------------------------------------------------------- #
async def _exchange_for_api_token() -> tuple[str, str, float]:
    oauth = get_oauth_token()
    r = await _client().get(
        "https://api.github.com/copilot_internal/v2/token",
        headers={
            "Authorization": f"token {oauth}",
            "Accept": "application/json",
            **EDITOR_HEADERS,
        },
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"Failed to exchange API token {r.status_code}: {r.text}. "
            "The account may not have a Copilot subscription or the OAuth "
            "token expired (log out and back in)."
        )
    data = r.json()
    endpoint = data.get("endpoints", {}).get("api", COPILOT_API_BASE)
    return data["token"], endpoint, float(data["expires_at"])


async def get_api_token() -> tuple[str, str]:
    now = time.time()
    if _api_token_mem and _api_token_mem.get("expires_at", 0) - 300 > now:
        return _api_token_mem["token"], _api_token_mem["endpoint"]
    async with _api_token_lock:
        if _api_token_mem and _api_token_mem.get("expires_at", 0) - 300 > time.time():
            return _api_token_mem["token"], _api_token_mem["endpoint"]
        token, endpoint, expires_at = await _exchange_for_api_token()
        _api_token_mem.update(token=token, endpoint=endpoint, expires_at=expires_at)
        return token, endpoint


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
async def list_models() -> list[dict[str, Any]]:
    token, endpoint = await get_api_token()
    r = await _client().get(
        f"{endpoint}/models",
        headers={"Authorization": f"Bearer {token}", **EDITOR_HEADERS},
    )
    r.raise_for_status()
    return r.json().get("data", [])


async def post_json(
    path: str,
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Buffered POST. Returns (status_code, json_body)."""
    token, endpoint = await get_api_token()
    r = await _client().post(
        f"{endpoint}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **EDITOR_HEADERS,
            **(extra_headers or {}),
        },
        json=payload,
    )
    try:
        body = r.json()
    except Exception:
        body = {"error": {"message": r.text}}
    return r.status_code, body


async def stream(
    path: str,
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """Streaming POST. Yields raw SSE bytes from the Copilot backend."""
    token, endpoint = await get_api_token()
    async with _client().stream(
        "POST",
        f"{endpoint}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **EDITOR_HEADERS,
            **(extra_headers or {}),
        },
        json=payload,
    ) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            raise RuntimeError(f"{resp.status_code}: {body.decode('utf-8', 'replace')}")
        async for chunk in resp.aiter_bytes():
            yield chunk
