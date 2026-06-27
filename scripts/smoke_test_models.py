#!/usr/bin/env python3
"""Token Foundry — end-to-end smoke test for every registered model.

Run this to call EVERY model behind the gateway with one virtual key and print a
pass/fail table. It exercises all three provider APIs and all three API formats
exactly as a real client would:

    provider    client path                          subscription header   format
    ---------   ----------------------------------   -------------------   -----------------
    anthropic   /llm-anthropic/v1/messages           x-api-key             Anthropic Messages
    openai      /llm-openai/v1/chat/completions      api-key               Chat Completions
    openai      /llm-openai/v1/responses             api-key               OpenAI Responses (gpt-5.x)
    google      /llm-google/v1/chat/completions      api-key               Chat Completions

The model list is auto-discovered from the control plane when admin credentials
are available; otherwise it falls back to a static list (verified 2026-06-27).

------------------------------------------------------------------------------
NO SECRETS LIVE IN THIS FILE. Configure via environment (or a local .env that is
git-ignored). Required:

    TF_GATEWAY_URL     e.g. https://<your-apim>.azure-api.net
    TF_VIRTUAL_KEY     an APIM subscription (virtual) key

Optional — enables auto-discovery of the live model list:

    TF_CONTROL_PLANE_URL   e.g. https://<your-app>.azurecontainerapps.io
    TF_ADMIN_USERNAME      default: admin
    TF_ADMIN_PASSWORD

Usage (no dependencies — pure Python stdlib; python-dotenv optional):
    python scripts/smoke_test_models.py                  # test all models
    python scripts/smoke_test_models.py claude-opus-4.7 gpt-4o   # test a subset
    python scripts/smoke_test_models.py --prompt "Write a haiku about tokens."

Exit code is non-zero if any model fails, so it doubles as a CI/health check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- Static fallback model list (verified against the live gateway 2026-06-27).
# Auto-discovery from the control plane overrides this when credentials are set.
# Each entry: (alias, provider). The endpoint + format are derived from the
# provider (and, for openai, the model name) in route_for().
STATIC_MODELS: list[tuple[str, str]] = [
    # anthropic — Messages API
    ("claude-opus-4.8", "anthropic"),
    ("claude-opus-4.7", "anthropic"),
    ("claude-opus-4.6", "anthropic"),
    ("claude-opus-4.5", "anthropic"),
    ("claude-sonnet-4.6", "anthropic"),
    ("claude-sonnet-4.5", "anthropic"),
    ("claude-haiku-4.5", "anthropic"),
    # google — Chat Completions
    ("gemini-3.5-flash", "google"),
    ("gemini-3.1-pro-preview", "google"),
    ("gemini-3-flash-preview", "google"),
    ("gemini-2.5-pro", "google"),
    # openai — Responses (gpt-5.x) + Chat Completions (the rest)
    ("gpt-5.5", "openai"),
    ("gpt-5.4", "openai"),
    ("gpt-5.4-mini", "openai"),
    ("gpt-5.3-codex", "openai"),
    ("gpt-5-mini", "openai"),
    ("gpt-4.1", "openai"),
    ("gpt-4.1-2025-04-14", "openai"),
    ("gpt-4o", "openai"),
    ("gpt-4o-2024-11-20", "openai"),
    ("gpt-4o-2024-08-06", "openai"),
    ("gpt-4o-2024-05-13", "openai"),
    ("gpt-4o-mini", "openai"),
    ("gpt-4", "openai"),
    ("gpt-4-0613", "openai"),
    ("gpt-3.5-turbo", "openai"),
]

# Per-provider client-facing API path + the subscription-key header the
# provider's own SDK naturally sends. Mirrors PROVIDER_APIS in
# app/services/apim_provisioner.py.
PROVIDER_API = {
    "anthropic": {"path": "llm-anthropic", "sub_header": "x-api-key"},
    "openai": {"path": "llm-openai", "sub_header": "api-key"},
    "google": {"path": "llm-google", "sub_header": "api-key"},
}

# Reasoning models spend output budget on hidden reasoning tokens, so a small
# cap can yield an empty answer. Keep this generous.
MAX_TOKENS = 2048
HTTP_TIMEOUT = 90  # seconds; reasoning models can be slow
RETRIES = 2  # extra attempts on transient errors (conn drop, 429, 5xx)
RETRY_BACKOFF = 2.0  # seconds, multiplied by attempt number


# --------------------------------------------------------------------------- #
# Config loading                                                              #
# --------------------------------------------------------------------------- #
def load_dotenv_if_present() -> None:
    """Load KEY=VALUE lines from a local .env (repo root) into os.environ.

    Uses python-dotenv if installed; otherwise a tiny built-in parser so the
    script has zero hard dependencies (stdlib urllib only).
    """
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
        return
    except Exception:
        pass
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(
            f"ERROR: missing required config '{name}'.\n"
            f"  Set it as an environment variable or in a local .env file.\n"
            f"  See the docstring at the top of {Path(__file__).name} for the full list."
        )
    return val


# --------------------------------------------------------------------------- #
# Minimal HTTP (stdlib only)                                                   #
# --------------------------------------------------------------------------- #
def http_post(url: str, headers: dict[str, str], body: dict) -> tuple[int, dict | str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, _parse_json(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_json(e.read())
    except Exception as e:  # network / timeout
        return 0, str(e)


def _parse_json(raw: bytes) -> dict | str:
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except Exception:
        return text


def http_post_retry(url: str, headers: dict[str, str], body: dict) -> tuple[int, dict | str]:
    """POST with retries on transient failures: connection drops (status 0),
    429 (rate limit), and 5xx. Permanent errors (4xx other than 429) return
    immediately so real misconfigurations aren't masked."""
    status, payload = 0, ""
    for attempt in range(RETRIES + 1):
        status, payload = http_post(url, headers, body)
        transient = status == 0 or status == 429 or 500 <= status <= 599
        if not transient or attempt == RETRIES:
            return status, payload
        time.sleep(RETRY_BACKOFF * (attempt + 1))
    return status, payload


# --------------------------------------------------------------------------- #
# Model discovery                                                              #
# --------------------------------------------------------------------------- #
def discover_models() -> list[tuple[str, str]]:
    """Pull the live model list from the control plane if credentials are set;
    otherwise return the static fallback."""
    cp = os.environ.get("TF_CONTROL_PLANE_URL", "").strip().rstrip("/")
    pw = os.environ.get("TF_ADMIN_PASSWORD", "").strip()
    if not cp or not pw:
        print("  (control-plane creds not set — using static model list)\n")
        return STATIC_MODELS
    user = os.environ.get("TF_ADMIN_USERNAME", "admin").strip()
    status, body = http_post(
        f"{cp}/api/login", {}, {"username": user, "password": pw}
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        print(f"  (control-plane login failed: HTTP {status} — using static list)\n")
        return STATIC_MODELS
    token = body["access_token"]
    req = urllib.request.Request(f"{cp}/api/routes", method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            routes = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  (control-plane /routes failed: {e} — using static list)\n")
        return STATIC_MODELS
    models = [(r["name"], r["provider"]) for r in routes]
    print(f"  (discovered {len(models)} models from control plane)\n")
    return models


# --------------------------------------------------------------------------- #
# Routing: model -> (endpoint path, format, request body, response extractor)  #
# --------------------------------------------------------------------------- #
def is_responses_model(alias: str) -> bool:
    """gpt-5.x (5.5/5.4/5.3-codex...) require the Responses API; the dotted '5.'
    distinguishes them from gpt-5-mini, which also accepts Chat Completions."""
    return alias.startswith("gpt-5.")


def route_for(alias: str, provider: str) -> dict:
    """Return {url_suffix, fmt, body, extract} for a model, given the gateway
    path is prefixed separately."""
    if provider == "anthropic":
        return {
            "suffix": "/v1/messages",
            "fmt": "messages",
            "body": {
                "model": alias,
                "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": PROMPT}],
            },
            "extract": extract_messages,
        }
    if provider == "openai" and is_responses_model(alias):
        return {
            "suffix": "/v1/responses",
            "fmt": "responses",
            "body": {"model": alias, "input": PROMPT, "max_output_tokens": MAX_TOKENS},
            "extract": extract_responses,
        }
    # openai (non-5.x) and google -> Chat Completions
    return {
        "suffix": "/v1/chat/completions",
        "fmt": "chat",
        "body": {
            "model": alias,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": PROMPT}],
        },
        "extract": extract_chat,
    }


def extract_chat(body: dict) -> tuple[str, dict]:
    msg = body["choices"][0]["message"]["content"]
    return (msg or "").strip(), body.get("usage", {}) or {}


def extract_messages(body: dict) -> tuple[str, dict]:
    parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip(), body.get("usage", {}) or {}


def extract_responses(body: dict) -> tuple[str, dict]:
    # Text lives in output[] -> the item with type=="message" -> content[].text.
    # Top-level output_text is often null on this backend.
    text = (body.get("output_text") or "").strip()
    if not text:
        chunks: list[str] = []
        for item in body.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        chunks.append(c.get("text", ""))
        text = "".join(chunks).strip()
    # Token usage on this backend rides in copilot_usage.token_details.
    usage: dict = {}
    cu = body.get("copilot_usage") or {}
    for d in cu.get("token_details", []):
        if d.get("token_type") == "input":
            usage["prompt_tokens"] = d.get("token_count")
        elif d.get("token_type") == "output":
            usage["completion_tokens"] = d.get("token_count")
    return text, usage


# --------------------------------------------------------------------------- #
# Runner                                                                       #
# --------------------------------------------------------------------------- #
def fmt_usage(usage: dict) -> str:
    p = usage.get("prompt_tokens")
    c = usage.get("completion_tokens")
    if p is None and c is None:
        return ""
    return f"in={p} out={c}"


def run_one(alias: str, provider: str) -> tuple[bool, str, str, str]:
    """Returns (ok, fmt_label, detail, usage_str)."""
    api = PROVIDER_API.get(provider)
    if not api:
        return False, "?", f"unknown provider '{provider}'", ""
    r = route_for(alias, provider)
    url = f"{GATEWAY}/{api['path']}{r['suffix']}"
    headers = {api["sub_header"]: VIRTUAL_KEY}
    status, body = http_post_retry(url, headers, r["body"])

    if status != 200:
        snippet = body if isinstance(body, str) else json.dumps(body)[:160]
        return False, r["fmt"], f"HTTP {status}: {snippet}", ""
    if not isinstance(body, dict):
        return False, r["fmt"], f"non-JSON response: {str(body)[:160]}", ""
    try:
        text, usage = r["extract"](body)
    except Exception as e:  # unexpected shape
        return False, r["fmt"], f"parse error: {e}: {json.dumps(body)[:160]}", ""
    if not text:
        return False, r["fmt"], "empty content (HTTP 200 but no text)", fmt_usage(usage)
    one_line = " ".join(text.split())
    return True, r["fmt"], one_line[:80], fmt_usage(usage)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test every Token Foundry model.")
    parser.add_argument("models", nargs="*", help="optional subset of aliases to test")
    parser.add_argument(
        "--prompt",
        default="Reply with a short friendly greeting (one sentence).",
        help="prompt sent to every model",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="seconds to wait between calls (avoid TPM limits on big runs)",
    )
    args = parser.parse_args()

    global PROMPT
    PROMPT = args.prompt

    models = discover_models()
    if args.models:
        wanted = set(args.models)
        models = [m for m in models if m[0] in wanted]
        missing = wanted - {m[0] for m in models}
        if missing:
            print(f"  (ignoring unknown aliases: {', '.join(sorted(missing))})\n")
    if not models:
        sys.exit("No models to test.")

    # group by provider for readable output
    models.sort(key=lambda m: (m[1], m[0]))

    print(f"Gateway : {GATEWAY}")
    print(f"Models  : {len(models)}")
    print(f"Prompt  : {PROMPT}\n")
    print(f"{'RESULT':<7} {'PROVIDER':<10} {'FORMAT':<10} {'MODEL':<24} REPLY / ERROR")
    print("-" * 100)

    passed = 0
    failed: list[str] = []
    for alias, provider in models:
        ok, fmt, detail, usage = run_one(alias, provider)
        mark = "PASS" if ok else "FAIL"
        tail = f"{detail}"
        if usage:
            tail = f"[{usage}] {tail}"
        print(f"{mark:<7} {provider:<10} {fmt:<10} {alias:<24} {tail}")
        if ok:
            passed += 1
        else:
            failed.append(alias)
        if args.sleep:
            time.sleep(args.sleep)

    print("-" * 100)
    print(f"\nTotal: {len(models)}   Passed: {passed}   Failed: {len(failed)}")
    if failed:
        print("Failed models: " + ", ".join(failed))
        return 1
    print("All models responded successfully.")
    return 0


if __name__ == "__main__":
    load_dotenv_if_present()
    GATEWAY = require("TF_GATEWAY_URL").rstrip("/")
    VIRTUAL_KEY = require("TF_VIRTUAL_KEY")
    PROMPT = ""  # set in main()
    raise SystemExit(main())
