#!/usr/bin/env python3
"""Token Foundry — prompt-caching smoke test.

Verifies that PROMPT CACHING works end-to-end THROUGH THE GATEWAY: in a growing
multi-turn chat, the stable leading context (system + prior turns) should be
served from cache on later calls, so the *cached* portion of the input tokens is
billed at a discount instead of full price.

How OpenAI / Azure OpenAI automatic caching works (what this script checks):
  * Caching kicks in only when the prompt PREFIX is long enough — currently
    >= 1024 tokens — and is automatic (no flag needed).
  * The first call with a new prefix POPULATES the cache (cached_tokens = 0).
  * Subsequent calls that repeat that exact prefix HIT the cache; the number of
    cached input tokens shows up in the response usage:
        Chat Completions : usage.prompt_tokens_details.cached_tokens
        Responses API    : usage.input_tokens_details.cached_tokens
  * The cache is best-effort and short-lived (minutes); a hit is not guaranteed
    on every call, but a warm prefix should hit most of the time.

This script SIMULATES a real conversation: it starts with a large stable system
context, then loops — each turn appends the model's reply and a new user message,
so the prompt grows exactly like a real chat session. It prints the per-turn
token breakdown so you can SEE the cache warming up.

------------------------------------------------------------------------------
NO SECRETS IN THIS FILE. Configure via environment or a local .env (gitignored):

    TF_GATEWAY_URL     e.g. https://<your-apim>.azure-api.net
    TF_VIRTUAL_KEY     an APIM subscription (virtual) key

Usage (pure stdlib, python-dotenv optional):
    python scripts/cache_test.py                       # default: azure gpt-5.4-mini
    python scripts/cache_test.py --model gpt-5.4-mini --provider azure
    python scripts/cache_test.py --turns 6             # number of chat turns
    python scripts/cache_test.py --prefix-tokens 3000  # size of the stable prefix

Exit code is non-zero if caching never engaged (cached_tokens stayed 0 across
all turns after the prefix exceeded the threshold), so it can gate CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Per-provider client-facing path + subscription-key header (mirrors
# smoke_test_models.py / PROVIDER_APIS in apim_provisioner.py).
PROVIDER_API = {
    "openai": {"path": "llm-openai", "sub_header": "api-key", "suffix": "/v1/chat/completions"},
    "azure": {"path": "llm-azure", "sub_header": "api-key", "suffix": "/openai/v1/chat/completions"},
}

HTTP_TIMEOUT = 90
# One English word ~= 1.3 tokens; this filler sentence is ~12 tokens. We repeat
# it to build a stable prefix of a target token size (rough, but the model's
# reported prompt_tokens is what we actually assert on).
FILLER = "You are an expert assistant; always consider the following reference note carefully. "
TOKENS_PER_FILLER = 15  # conservative estimate for sizing the prefix


# --------------------------------------------------------------------------- #
# Config (mirrors smoke_test_models.py)                                        #
# --------------------------------------------------------------------------- #
def load_dotenv_if_present() -> None:
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
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"ERROR: missing required config '{name}' (set it in env or a local .env).")
    return val


# --------------------------------------------------------------------------- #
# HTTP (stdlib only)                                                           #
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


# --------------------------------------------------------------------------- #
# Usage extraction                                                            #
# --------------------------------------------------------------------------- #
def extract_usage(body: dict) -> dict:
    """Pull prompt/completion/cached tokens from a Chat Completions response,
    tolerating both the chat and responses usage shapes."""
    u = body.get("usage", {}) or {}
    prompt = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    completion = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
    cached = (
        (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        or (u.get("input_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    return {"prompt": int(prompt), "completion": int(completion), "cached": int(cached)}


def reply_text(body: dict) -> str:
    try:
        return (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# The conversation simulator                                                  #
# --------------------------------------------------------------------------- #
def build_prefix(target_tokens: int) -> str:
    """Build a stable system prompt of roughly `target_tokens` tokens.

    Leads with a per-run unique nonce so every run starts from a brand-new,
    never-cached prefix — otherwise repeated runs reuse text that's still warm
    in the prompt cache and turn 1 misleadingly shows a hit.
    """
    n = max(1, target_tokens // TOKENS_PER_FILLER)
    return (f"[session {uuid.uuid4().hex}] " + FILLER * n).strip()


def run(gateway: str, key: str, provider: str, model: str, turns: int, prefix_tokens: int) -> int:
    api = PROVIDER_API.get(provider)
    if not api:
        sys.exit(f"unknown provider '{provider}' (supported: {', '.join(PROVIDER_API)})")
    url = f"{gateway}/{api['path']}{api['suffix']}"
    headers = {api["sub_header"]: key}

    system_prefix = build_prefix(prefix_tokens)
    # The conversation always LEADS with the big stable system message — that's
    # the cacheable prefix. Each turn appends to `messages`, growing the context.
    messages = [{"role": "system", "content": system_prefix}]

    print(f"Gateway      : {gateway}")
    print(f"Provider/API : {provider}  ->  {api['path']}{api['suffix']}")
    print(f"Model        : {model}")
    print(f"Prefix       : ~{prefix_tokens} tokens (stable system context, the cacheable part)")
    print(f"Turns        : {turns}\n")
    print(f"{'TURN':<5} {'PROMPT_TOK':>10} {'CACHED_TOK':>10} {'HIT%':>6} {'COMPLETION':>10}  REPLY / ERROR")
    print("-" * 90)

    any_hit = False
    prefix_ok = False
    for turn in range(1, turns + 1):
        messages.append({"role": "user", "content": f"(turn {turn}) Reply with a short sentence."})
        body = {"model": model, "messages": messages, "max_completion_tokens": 256}
        status, resp = http_post(url, headers, body)

        if status != 200 or not isinstance(resp, dict):
            snippet = resp if isinstance(resp, str) else json.dumps(resp)[:200]
            print(f"{turn:<5} {'—':>10} {'—':>10} {'—':>6} {'—':>10}  HTTP {status}: {snippet}")
            return 2

        u = extract_usage(resp)
        text = reply_text(resp)
        hit_pct = (u["cached"] / u["prompt"] * 100) if u["prompt"] else 0.0
        if u["prompt"] >= 1024:
            prefix_ok = True
        if u["cached"] > 0:
            any_hit = True
        one_line = " ".join(text.split())[:40]
        print(
            f"{turn:<5} {u['prompt']:>10} {u['cached']:>10} {hit_pct:>5.0f}% "
            f"{u['completion']:>10}  {one_line}"
        )

        # Append the model's reply so next turn's prompt grows like a real chat.
        messages.append({"role": "assistant", "content": text or "ok"})
        # Small gap so the cache has a chance to warm (best-effort, minutes TTL).
        if turn < turns:
            time.sleep(2)

    print("-" * 90)
    # --- Verdict ---
    if not prefix_ok:
        print(
            f"\nINCONCLUSIVE: prompt never reached the ~1024-token caching threshold "
            f"(largest prefix was under it). Re-run with a bigger --prefix-tokens."
        )
        return 3
    if any_hit:
        print(
            "\nPASS: cache engaged — later turns billed some input tokens as cached "
            "(cached_tokens > 0). The gateway passes the cache usage through intact."
        )
        return 0
    print(
        "\nFAIL: prefix exceeded the threshold but cached_tokens stayed 0 on every turn.\n"
        "  Possible causes: this deployment doesn't support prompt caching, the cache\n"
        "  TTL expired between calls, or the prefix wasn't byte-identical across turns."
    )
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Verify prompt caching through the gateway.")
    p.add_argument("--provider", default="azure", choices=sorted(PROVIDER_API), help="gateway provider path")
    p.add_argument("--model", default="gpt-5.4-mini", help="model/deployment alias")
    p.add_argument("--turns", type=int, default=5, help="number of chat turns")
    p.add_argument("--prefix-tokens", type=int, default=2500, help="approx size of the stable cacheable prefix")
    args = p.parse_args()

    gateway = require("TF_GATEWAY_URL").rstrip("/")
    key = require("TF_VIRTUAL_KEY")
    return run(gateway, key, args.provider, args.model, args.turns, args.prefix_tokens)


if __name__ == "__main__":
    load_dotenv_if_present()
    raise SystemExit(main())
