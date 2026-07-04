#!/usr/bin/env python3
"""Token Foundry — prompt-caching smoke test for OpenAI / Google / Anthropic.

Companion to azure_cache_test.py (which covers Azure OpenAI). This one verifies
prompt caching THROUGH THE GATEWAY for the other three providers, each of which
has a DIFFERENT caching shape — all confirmed by probing the live gateway:

  provider    auth header   endpoint / format          cached-tokens field
  ---------   -----------   ------------------------   -----------------------------
  openai      api-key       /v1/responses (gpt-5.x)    usage.input_tokens_details.cached_tokens
  google      api-key       /v1/chat/completions       usage.prompt_tokens_details.cached_tokens
  anthropic   x-api-key     /v1/messages               usage.cache_read_input_tokens

All three cache AUTOMATICALLY on this gateway's upstream (the hub adds Anthropic
cache_control for you), so no cache_control flag is needed here. Caching is
BEST-EFFORT: it needs a large stable prefix (roughly 1024+ tokens for
OpenAI/Azure) AND a turn or two of warm-up — the first call(s) populate the
cache and report cached=0, hits start shortly after, and an occasional lone miss
mid-run is normal. Google's implicit cache has a stricter bar, so it may report 0
(raise --prefix-tokens rather than assuming it's unsupported).

Like azure_cache_test.py, this SIMULATES a growing multi-turn chat: a big stable
context up front, then each turn appends the reply + a new message so the prompt
grows like a real session, and prints the per-turn cached-token breakdown.

------------------------------------------------------------------------------
NO SECRETS IN THIS FILE. Configure via env or a local .env (gitignored):

    TF_GATEWAY_URL     e.g. https://<your-apim>.azure-api.net
    TF_VIRTUAL_KEY     an APIM subscription (virtual) key

Usage (pure stdlib, python-dotenv optional):
    python tests/manual/apim_cache_test_providers.py                      # all three, default models
    python tests/manual/apim_cache_test_providers.py --provider anthropic # just one
    python tests/manual/apim_cache_test_providers.py --provider openai --model gpt-5.4
    python tests/manual/apim_cache_test_providers.py --turns 6 --prefix-tokens 3000

Notes from live testing (so results aren't misread):
  * gpt-5.5 caches fine — measured ~85% hit rate over 20 back-to-back calls
    (first hit on the 2nd call). An earlier "gpt-5.5 doesn't cache" impression
    was a too-short run plus a param bug, not the model.
  * gpt-5.x (reasoning models) REQUIRE max_output_tokens >= 16, else HTTP 400
    (which looks like a miss if you swallow the error). This script uses 256 so
    reasoning models have room to both think and emit a visible reply.

Exit code is non-zero if a tested provider that CAN cache never engaged.
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

HTTP_TIMEOUT = 90
FILLER = "You are an expert assistant; always consider the following reference note carefully. "
TOKENS_PER_FILLER = 15

# Per-provider wiring. `fmt` selects how we build the request body and read usage.
#   auth   : subscription-key header name the provider's SDK naturally sends
#   path   : gateway API path
#   suffix : operation path under it
#   fmt    : "responses" (OpenAI gpt-5.x) | "chat" (Google) | "messages" (Anthropic)
#   default: a sensible default model for a quick run
PROVIDERS = {
    "openai": {
        "auth": "api-key",
        "path": "llm-openai",
        "suffix": "/v1/responses",
        "fmt": "responses",
        "default": "gpt-5.5",
    },
    "google": {
        "auth": "api-key",
        "path": "llm-google",
        "suffix": "/v1/chat/completions",
        "fmt": "chat",
        "default": "gemini-2.5-pro",
    },
    "anthropic": {
        "auth": "x-api-key",
        "path": "llm-anthropic",
        "suffix": "/v1/messages",
        "fmt": "messages",
        "default": "claude-opus-4.8",
    },
}


# --------------------------------------------------------------------------- #
# Config + HTTP (mirrors azure_cache_test.py / smoke_test_models.py)           #
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
    except Exception as e:
        return 0, str(e)


def _parse_json(raw: bytes) -> dict | str:
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except Exception:
        return text


# --------------------------------------------------------------------------- #
# Per-format request builders + usage readers                                 #
# --------------------------------------------------------------------------- #
def build_prefix(target_tokens: int) -> str:
    n = max(1, target_tokens // TOKENS_PER_FILLER)
    return (FILLER * n).strip()


def build_body(fmt: str, model: str, prefix: str, history: list[dict], turn: int) -> dict:
    """Construct the request body for this provider's format, embedding the
    stable `prefix` as the leading (cacheable) context and the running history."""
    user_msg = f"(turn {turn}) Reply with a short sentence."
    if fmt == "messages":
        # Anthropic: system is a top-level field (the cacheable prefix); messages
        # alternate user/assistant.
        return {
            "model": model,
            "max_tokens": 256,
            "system": prefix,
            "messages": history + [{"role": "user", "content": user_msg}],
        }
    if fmt == "responses":
        # OpenAI Responses API: single `input` string. Keep the big prefix at the
        # front every turn so the cacheable prefix stays byte-identical.
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        return {
            "model": model,
            "max_output_tokens": 256,
            "input": f"{prefix}\n{convo}\nuser: {user_msg}",
        }
    # chat (Google + OpenAI-compatible): system message leads, then history.
    return {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "system", "content": prefix}]
        + history
        + [{"role": "user", "content": user_msg}],
    }


def read_usage(fmt: str, body: dict) -> dict:
    """Return {prompt, completion, cached} normalized across the three shapes."""
    u = body.get("usage", {}) or {}
    if fmt == "messages":
        prompt = u.get("input_tokens", 0) or 0
        completion = u.get("output_tokens", 0) or 0
        cached = u.get("cache_read_input_tokens", 0) or 0
    else:
        prompt = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
        completion = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
        cached = (
            (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            or (u.get("input_tokens_details") or {}).get("cached_tokens")
            or 0
        )
    return {"prompt": int(prompt), "completion": int(completion), "cached": int(cached)}


def read_reply(fmt: str, body: dict) -> str:
    try:
        if fmt == "messages":
            parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
            return "".join(parts).strip()
        if fmt == "responses":
            txt = (body.get("output_text") or "").strip()
            if txt:
                return txt
            chunks = []
            for item in body.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            chunks.append(c.get("text", ""))
            return "".join(chunks).strip()
        return (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return ""


def append_reply(fmt: str, history: list[dict], user_turn: int, reply: str) -> None:
    """Grow the conversation so the next prompt is longer (real-chat simulation).
    For `responses` we keep history as pseudo messages that build_body joins."""
    history.append({"role": "user", "content": f"(turn {user_turn}) Reply with a short sentence."})
    history.append({"role": "assistant", "content": reply or "ok"})


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #
def run_provider(
    gateway: str, key: str, provider: str, model: str, turns: int, prefix_tokens: int
) -> tuple[str, int]:
    cfg = PROVIDERS[provider]
    url = f"{gateway}/{cfg['path']}{cfg['suffix']}"
    headers = {cfg["auth"]: key}
    prefix = build_prefix(prefix_tokens)
    history: list[dict] = []

    print(f"\n{'='*90}")
    print(f"PROVIDER: {provider}  ({cfg['path']}{cfg['suffix']}, fmt={cfg['fmt']}, auth={cfg['auth']})")
    print(f"MODEL   : {model}   PREFIX ~{prefix_tokens} tok   TURNS {turns}")
    print(f"{'-'*90}")
    print(f"{'TURN':<5} {'PROMPT':>8} {'CACHED':>8} {'HIT%':>6} {'OUT':>6}  REPLY / ERROR")

    any_hit = False
    prefix_ok = False
    for turn in range(1, turns + 1):
        body = build_body(cfg["fmt"], model, prefix, history, turn)
        status, resp = http_post(url, headers, body)
        if status != 200 or not isinstance(resp, dict):
            snippet = resp if isinstance(resp, str) else json.dumps(resp)[:180]
            print(f"{turn:<5} {'—':>8} {'—':>8} {'—':>6} {'—':>6}  HTTP {status}: {snippet}")
            return provider, 2

        u = read_usage(cfg["fmt"], resp)
        reply = read_reply(cfg["fmt"], resp)
        hit = (u["cached"] / u["prompt"] * 100) if u["prompt"] else 0.0
        if u["prompt"] >= 1024:
            prefix_ok = True
        if u["cached"] > 0:
            any_hit = True
        print(
            f"{turn:<5} {u['prompt']:>8} {u['cached']:>8} {hit:>5.0f}% {u['completion']:>6}  "
            f"{' '.join(reply.split())[:40]}"
        )
        append_reply(cfg["fmt"], history, turn, reply)
        if turn < turns:
            time.sleep(5)

    # verdict per provider
    if not prefix_ok:
        print(f"  -> INCONCLUSIVE: prompt never hit the ~1024-token threshold; raise --prefix-tokens.")
        return provider, 3
    if any_hit:
        print(f"  -> PASS: caching engaged (cached_tokens > 0); gateway passes it through.")
        return provider, 0
    print(f"  -> FAIL/NA: prefix over threshold but cached stayed 0 (this model may not cache, "
          f"or Google's implicit cache didn't trigger).")
    return provider, 1


def main() -> int:
    p = argparse.ArgumentParser(description="Verify prompt caching for openai/google/anthropic.")
    p.add_argument("--provider", choices=sorted(PROVIDERS), help="test one provider (default: all)")
    p.add_argument("--model", help="override the model (only valid with --provider)")
    p.add_argument("--turns", type=int, default=5)
    p.add_argument("--prefix-tokens", type=int, default=2500)
    args = p.parse_args()

    gateway = require("TF_GATEWAY_URL").rstrip("/")
    key = require("TF_VIRTUAL_KEY")

    targets = [args.provider] if args.provider else list(PROVIDERS)
    if args.model and not args.provider:
        sys.exit("--model requires --provider")

    print(f"Gateway : {gateway}")
    print(f"Testing : {', '.join(targets)}")

    results = []
    for prov in targets:
        model = args.model if (args.model and args.provider == prov) else PROVIDERS[prov]["default"]
        results.append(run_provider(gateway, key, prov, model, args.turns, args.prefix_tokens))

    print(f"\n{'='*90}\nSUMMARY")
    worst = 0
    for prov, code in results:
        label = {0: "PASS", 1: "FAIL/NA", 2: "ERROR", 3: "INCONCLUSIVE"}.get(code, "?")
        print(f"  {prov:<12} {label}")
        worst = max(worst, code)
    return worst


if __name__ == "__main__":
    load_dotenv_if_present()
    raise SystemExit(main())
