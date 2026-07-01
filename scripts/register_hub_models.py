#!/usr/bin/env python3
"""Token Foundry — bulk-register the gitmodel-hub models as model routes.

Registers every usable CHAT model from the hub into TokenFoundry via the admin
API (POST /api/routes). Idempotent-ish: deletes an existing route of the same
name before re-creating, so re-running updates cleanly.

Provider mapping (TokenFoundry only knows openai/anthropic/google/azure):
  - claude-*  -> anthropic   (Anthropic Messages API, /v1/messages)
  - gpt-*     -> openai      (Chat Completions / Responses, /v1/...)
  - gemini-*  -> openai      (hub says Gemini is most stable via OpenAI-compat)

backend_url is the hub ROOT (NO trailing /v1): the APIM operation templates
already prepend /v1/... — adding /v1 here would double it to /v1/v1/... .

Pricing is 0 for everything: no reliable public price exists for the newer
hub-internal model names, and inventing numbers for a billing system is wrong.
Update later via PATCH /api/routes/{id} when a real price list is available.

NO SECRETS IN THIS FILE. Configure via environment:
    TF_CONTROL_PLANE_URL   the app base URL (https://...azurecontainerapps.io)
    TF_ADMIN_USERNAME      default: admin
    TF_ADMIN_PASSWORD
    TF_HUB_URL             the hub ROOT (no /v1)
    TF_HUB_KEY             the hub API key
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Usable chat models from the hub's "supported models" list.
# Excluded: text-embedding-* (not chat), trajectory-compaction (experimental),
# mai-code-1-flash-picker (unknown protocol). Older dated Azure-OpenAI aliases
# (gpt-4-0613 etc.) are skipped to avoid a cluttered list; add if you need them.
ANTHROPIC_MODELS = [
    "claude-opus-4.8",
    "claude-opus-4.7",
    "claude-opus-4.6",
    "claude-opus-4.5",
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
]
# gpt-* go through the OpenAI-compatible endpoint.
# This list matches STATIC_MODELS in scripts/smoke_test_models.py (the list
# verified against the live gateway), including the dated Azure-OpenAI aliases
# and older versions, so registration and smoke test cover the same set.
OPENAI_MODELS = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5-mini",
    "gpt-4.1",
    "gpt-4.1-2025-04-14",
    "gpt-4o",
    "gpt-4o-2024-11-20",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-05-13",
    "gpt-4o-mini",
    "gpt-4",
    "gpt-4-0613",
    "gpt-3.5-turbo",
]
# gemini-* go through TokenFoundry's native google provider.
GOOGLE_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
]


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        sys.exit(f"Missing required env var: {name}")
    return val


def _post(url: str, token: str | None, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}


def _login(cp: str, user: str, pw: str) -> str:
    status, body = _post(f"{cp}/api/login", None, {"username": user, "password": pw})
    if status != 200:
        sys.exit(f"Login failed ({status}): {body}")
    return body["access_token"]


def _list_routes(cp: str, token: str) -> list[dict]:
    req = urllib.request.Request(f"{cp}/api/routes", method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _delete_route(cp: str, token: str, route_id: str) -> int:
    req = urllib.request.Request(f"{cp}/api/routes/{route_id}", method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def register(cp: str, token: str, name: str, provider: str, hub: str, key: str) -> None:
    body = {
        "name": name,
        "provider": provider,
        "backend_url": hub,           # hub ROOT, no /v1
        "backend_secret": key,
        "owner_scope": "PLATFORM",
        "auth_mode": "KV_SECRET",
        "price_in_per_1k": 0,
        "price_out_per_1k": 0,
        "markup_pct": 0,
    }
    status, resp = _post(f"{cp}/api/routes", token, body)
    mark = "OK " if status == 201 else "ERR"
    print(f"  [{mark}] {name:<28} {provider:<10} HTTP {status}"
          + ("" if status == 201 else f"  {resp.get('error','')}"))


def main() -> None:
    cp = _env("TF_CONTROL_PLANE_URL").rstrip("/")
    user = _env("TF_ADMIN_USERNAME", "admin")
    pw = _env("TF_ADMIN_PASSWORD")
    hub = _env("TF_HUB_URL").rstrip("/")
    key = _env("TF_HUB_KEY")

    token = _login(cp, user, pw)
    print(f"Logged in to {cp}")

    # Delete existing routes of the same names so re-runs are clean.
    existing = {r["name"]: r["id"] for r in _list_routes(cp, token)}
    wanted = ANTHROPIC_MODELS + OPENAI_MODELS + GOOGLE_MODELS
    for name in wanted:
        if name in existing:
            _delete_route(cp, token, existing[name])

    print(f"\nRegistering {len(ANTHROPIC_MODELS)} anthropic + "
          f"{len(OPENAI_MODELS)} openai + {len(GOOGLE_MODELS)} google models:\n")
    for name in ANTHROPIC_MODELS:
        register(cp, token, name, "anthropic", hub, key)
    for name in OPENAI_MODELS:
        register(cp, token, name, "openai", hub, key)
    for name in GOOGLE_MODELS:
        register(cp, token, name, "google", hub, key)

    total = len(_list_routes(cp, token))
    print(f"\nDone. {total} routes now registered.")


if __name__ == "__main__":
    main()
