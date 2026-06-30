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
    python scripts/smoke_test_models.py                  # test all models, BOTH modes
    python scripts/smoke_test_models.py claude-opus-4.7 gpt-4o   # test a subset
    python scripts/smoke_test_models.py --prompt "Write a haiku about tokens."
    python scripts/smoke_test_models.py --stream gpt-4o          # streaming only
    python scripts/smoke_test_models.py --no-stream gpt-4o       # non-streaming only

By default every model is exercised TWICE — once non-streaming (direct) and once
streaming (SSE) — so a single run validates both paths. Use --stream or
--no-stream to restrict the run to a single mode.

Streaming mode sends `stream: true` and verifies the gateway actually streams
(Content-Type text/event-stream, multiple SSE chunks) AND that a final usage
chunk arrives — which for openai/azure chat proves the gateway injected
`stream_options.include_usage`. anthropic and the gpt-5.x responses op emit usage
natively; google chat may omit it (soft warning, not a failure).

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
# Streaming (SSE): verify token-by-token passthrough + final usage chunk        #
# --------------------------------------------------------------------------- #
def http_post_stream(
    url: str, headers: dict[str, str], body: dict
) -> tuple[int, str, list[dict], str]:
    """POST a streaming request and collect SSE events.

    Returns (status, content_type, events, error). `events` is the list of JSON
    objects parsed from each `data:` line (excluding the `[DONE]` sentinel).
    Single attempt (no retry — replaying a partially-consumed stream is messy);
    streaming is opt-in via --stream and meant for spot verification.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in headers.items():
        req.add_header(k, v)
    events: list[dict] = []
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "") or ""
            for raw in resp:  # HTTPResponse iterates line-by-line
                line = raw.decode("utf-8", "replace").strip()
                # Skip blanks, SSE comments (":..."), and `event:` name lines.
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        pass  # tolerate keep-alive / non-JSON frames
            return resp.status, ctype, events, ""
    except urllib.error.HTTPError as e:
        return e.code, "", [], e.read().decode("utf-8", "replace")[:200]
    except Exception as e:  # network / timeout
        return 0, "", [], str(e)


def parse_stream_chat(events: list[dict]) -> tuple[str, dict]:
    """OpenAI / Google Chat Completions SSE → (text, usage).

    Text is assembled from `choices[].delta.content`; `usage` only appears in a
    trailing chunk when `stream_options.include_usage=true` was set (which the
    gateway injects for openai/azure chat — see _build_chat_stream_policy)."""
    text: list[str] = []
    usage: dict = {}
    for ev in events:
        for ch in ev.get("choices", []) or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text.append(delta["content"])
        u = ev.get("usage")
        if u:
            usage = {
                "prompt_tokens": u.get("prompt_tokens"),
                "completion_tokens": u.get("completion_tokens"),
            }
    return "".join(text).strip(), usage


def parse_stream_messages(events: list[dict]) -> tuple[str, dict]:
    """Anthropic Messages SSE → (text, usage). Text from `content_block_delta`;
    input tokens from `message_start`, output tokens from `message_delta`
    (Anthropic always streams usage, regardless of any include_usage flag)."""
    text: list[str] = []
    usage: dict = {}
    for ev in events:
        t = ev.get("type")
        if t == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                text.append(d["text"])
        elif t == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            if u.get("input_tokens") is not None:
                usage["prompt_tokens"] = u["input_tokens"]
        elif t == "message_delta":
            u = ev.get("usage") or {}
            if u.get("output_tokens") is not None:
                usage["completion_tokens"] = u["output_tokens"]
    return "".join(text).strip(), usage


def parse_stream_responses(events: list[dict]) -> tuple[str, dict]:
    """OpenAI Responses SSE → (text, usage). Text from `response.output_text.delta`;
    usage from the terminal `response.completed` event. The Responses op is NOT
    given include_usage (the API rejects it), but it emits usage by default."""
    text: list[str] = []
    usage: dict = {}
    for ev in events:
        t = ev.get("type")
        if t == "response.output_text.delta" and ev.get("delta"):
            text.append(ev["delta"])
        elif t in ("response.completed", "response.incomplete"):
            u = (ev.get("response") or {}).get("usage") or {}
            if u:
                usage = {
                    "prompt_tokens": u.get("input_tokens"),
                    "completion_tokens": u.get("output_tokens"),
                }
    return "".join(text).strip(), usage


STREAM_PARSERS = {
    "chat": parse_stream_chat,
    "messages": parse_stream_messages,
    "responses": parse_stream_responses,
}


# --------------------------------------------------------------------------- #
# Runner                                                                       #
# --------------------------------------------------------------------------- #
def fmt_usage(usage: dict) -> str:
    p = usage.get("prompt_tokens")
    c = usage.get("completion_tokens")
    if p is None and c is None:
        return ""
    return f"in={p} out={c}"


def run_one(alias: str, provider: str, stream: bool = False) -> tuple[bool, str, str, str]:
    """Returns (ok, fmt_label, detail, usage_str)."""
    api = PROVIDER_API.get(provider)
    if not api:
        return False, "?", f"unknown provider '{provider}'", ""
    r = route_for(alias, provider)
    url = f"{GATEWAY}/{api['path']}{r['suffix']}"
    headers = {api["sub_header"]: VIRTUAL_KEY}
    if stream:
        return _run_one_stream(alias, provider, api, r, url, headers)
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


def _run_one_stream(
    alias: str, provider: str, api: dict, r: dict, url: str, headers: dict
) -> tuple[bool, str, str, str]:
    """Streaming variant: send stream:true, verify SSE passthrough + usage.

    Checks, in order:
      1. HTTP 200 and Content-Type is text/event-stream (true passthrough, not a
         buffered JSON body);
      2. multiple data: events arrived and assembled into non-empty text;
      3. a final usage chunk was present. For openai/azure `chat` this proves the
         gateway's stream_options.include_usage injection worked; anthropic and
         the responses op emit usage natively. (google chat may omit usage even
         with the flag — treated as a soft warning, not a failure.)
    """
    body = dict(r["body"], stream=True)
    status, ctype, events, err = http_post_stream(url, headers, body)
    if status != 200:
        return False, r["fmt"], f"HTTP {status}: {err}", ""
    if "text/event-stream" not in ctype.lower():
        return False, r["fmt"], f"not streamed (Content-Type: {ctype or 'none'})", ""
    if not events:
        return False, r["fmt"], "no SSE data events received", ""

    parser = STREAM_PARSERS.get(r["fmt"])
    if not parser:
        return False, r["fmt"], f"no stream parser for fmt '{r['fmt']}'", ""
    try:
        text, usage = parser(events)
    except Exception as e:  # unexpected shape
        return False, r["fmt"], f"stream parse error: {e}", ""
    if not text:
        return False, r["fmt"], f"empty content ({len(events)} events, no text)", fmt_usage(usage)

    has_usage = usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None
    one_line = " ".join(text.split())
    detail = f"{len(events)} chunks | {one_line[:60]}"
    if not has_usage and provider != "google":
        # For openai/azure chat this means include_usage injection didn't take.
        return False, r["fmt"], f"streamed OK but NO usage chunk — {detail}", ""
    if not has_usage:
        detail = f"[no usage — google may omit] {detail}"
    return True, r["fmt"], detail, fmt_usage(usage)


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
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--stream",
        action="store_true",
        help="run ONLY the streaming (SSE) test: send stream:true and verify SSE "
        "passthrough + a final usage chunk (validates the gateway's streaming "
        "support / include_usage injection). Default runs both modes.",
    )
    mode_group.add_argument(
        "--no-stream",
        action="store_true",
        help="run ONLY the non-streaming (direct) test. Default runs both modes.",
    )
    args = parser.parse_args()

    global PROMPT
    PROMPT = args.prompt

    # Default: exercise BOTH paths (direct + stream). --stream / --no-stream
    # restrict the run to a single mode.
    if args.stream:
        modes = [True]
    elif args.no_stream:
        modes = [False]
    else:
        modes = [False, True]

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

    mode_label = {
        (False,): "non-streaming",
        (True,): "streaming (SSE)",
    }.get(tuple(modes), "both (direct + streaming SSE)")
    print(f"Gateway : {GATEWAY}")
    print(f"Models  : {len(models)}")
    print(f"Mode    : {mode_label}")
    print(f"Prompt  : {PROMPT}\n")
    print(f"{'RESULT':<7} {'PROVIDER':<10} {'MODE':<7} {'FORMAT':<10} {'MODEL':<24} REPLY / ERROR")
    print("-" * 110)

    passed = 0
    failed: list[str] = []
    for alias, provider in models:
        for stream in modes:
            ok, fmt, detail, usage = run_one(alias, provider, stream=stream)
            mark = "PASS" if ok else "FAIL"
            mode_tag = "stream" if stream else "direct"
            tail = f"{detail}"
            if usage:
                tail = f"[{usage}] {tail}"
            print(f"{mark:<7} {provider:<10} {mode_tag:<7} {fmt:<10} {alias:<24} {tail}")
            if ok:
                passed += 1
            else:
                failed.append(f"{alias} ({mode_tag})")
            if args.sleep:
                time.sleep(args.sleep)

    total = len(models) * len(modes)
    print("-" * 110)
    print(f"\nTotal: {total}   Passed: {passed}   Failed: {len(failed)}")
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
