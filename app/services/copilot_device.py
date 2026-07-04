"""GitHub Copilot device-flow login (control-plane side).

A lightweight, dependency-free port of the device-flow bits from
`vendored/gitmodel-hub/hub/copilot_client.py`. The vendored client stores the
OAuth token in GitModel's own SQLite `store`; here we DON'T store anything — we
just drive the two GitHub HTTP calls and hand the token back to the caller
(app/api/github_accounts.py), which persists it in Key Vault and injects it into
the per-account hub at deploy time.

Flow:
  start()  -> POST github.com/login/device/code   -> {device_code, user_code, ...}
  poll()   -> POST github.com/login/oauth/access_token (device_code grant)
              -> pending | success(access_token) | error
  whoami() -> GET api.github.com/user (with the token) -> {login, id}

The client_id is copilot.vim's public one — the same value the vendored client
uses, so the resulting OAuth token is accepted by Copilot's internal token
endpoint (which the deployed hub calls to exchange for a short-lived API token).
"""

from __future__ import annotations

from typing import Any

import httpx

# copilot.vim public client_id (must match the vendored hub so the token the
# hub later exchanges is valid). See vendored/gitmodel-hub/hub/copilot_client.py.
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"

_HTTP_TIMEOUT = 20.0


def start_device_flow() -> dict[str, Any]:
    """Begin device flow. Returns device_code (kept server-side to poll) plus the
    user_code / verification_uri the user needs to authorize in a browser."""
    with httpx.Client(timeout=_HTTP_TIMEOUT) as hc:
        r = hc.post(
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
        "interval": int(info.get("interval", 5)),
        "expires_in": int(info.get("expires_in", 900)),
    }


def poll_device_flow(device_code: str) -> dict[str, Any]:
    """Poll once. Returns one of:
      {"status": "pending"}                       — user hasn't authorized yet
      {"status": "success", "access_token": ...}  — authorized; token returned
      {"status": "error", "error": ..., "detail": ...}
    Unlike the vendored client, the token is RETURNED, not stored — the caller
    persists it (Key Vault) and injects it into the hub deployment.
    """
    with httpx.Client(timeout=_HTTP_TIMEOUT) as hc:
        r = hc.post(
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
        return {"status": "success", "access_token": data["access_token"]}
    err = data.get("error")
    if err in ("authorization_pending", "slow_down"):
        return {"status": "pending", "error": err}
    return {"status": "error", "error": err or "unknown", "detail": data}


def whoami(access_token: str) -> dict[str, Any]:
    """Resolve the GitHub login + id for a token, so we can label the account.
    Best-effort — returns {} on failure (the account still works, just unlabeled)."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as hc:
            r = hc.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"token {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            r.raise_for_status()
            u = r.json()
        return {"login": u.get("login"), "id": str(u.get("id")) if u.get("id") else None}
    except Exception:  # noqa: BLE001 — labeling is best-effort
        return {}
