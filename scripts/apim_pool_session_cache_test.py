#!/usr/bin/env python3
"""Token Foundry — hub pool: round-robin vs session-affinity, and cache effect.

Sibling of apim_azure_pool_session_cache_test.py. That one tests the Azure OpenAI
pool (llm-azure); THIS one tests the three HUB-backed pools created over two
independent GitModel hubs:

    llm-openai-pool     = llm-openai    + llm-openai-2      (old hub + new hub)
    llm-anthropic-pool  = llm-anthropic + llm-anthropic-2
    llm-google-pool     = llm-google    + llm-google-2

The two hubs were verified INDEPENDENT (a prefix warmed on the old hub does NOT
hit on the new hub — cached=0), so this pool genuinely spreads load and each
backend keeps its OWN prompt cache. That makes the round-robin-vs-affinity
contrast meaningful here (unlike the azure pool, whose two endpoints turned out
to share one underlying Global deployment).

For each provider it runs the same growing multi-turn chat twice:
  PHASE 1  no cookie   -> APIM round-robins turns across old/new hub
  PHASE 2  with cookie -> the first Set-Cookie: SessionId pins every later turn
                          to the same hub, so the prompt cache stays warm.

The SessionId cookie value is base64(backend-name) on this gateway, so we print
which backend each turn hit without touching App Insights. Each PHASE builds its
own unique-nonce prefix so runs/phases never pre-warm each other's cache.

Provider differences (all confirmed against the live gateway):
    provider    auth header   path/format                  cached-tokens field
    ---------   -----------   --------------------------   ---------------------------
    openai      api-key       /llm-openai/v1/chat/...       prompt_tokens_details.cached_tokens
    anthropic   x-api-key     /llm-anthropic/v1/messages    cache_read_input_tokens
    google      api-key       /llm-google/v1/chat/...       prompt_tokens_details.cached_tokens

------------------------------------------------------------------------------
NO SECRETS IN THIS FILE. Configure via env or a local .env (gitignored):

    TF_GATEWAY_URL   e.g. https://<your-apim>.azure-api.net
    TF_VIRTUAL_KEY   an APIM subscription (virtual) key

Usage (pure stdlib):
    python scripts/apim_pool_session_cache_test.py                    # all three
    python scripts/apim_pool_session_cache_test.py --provider openai  # just one
    python scripts/apim_pool_session_cache_test.py --turns 8 --prefix-tokens 3000
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HTTP_TIMEOUT = 90
FILLER = "You are an expert assistant; always consider the following reference note carefully. "
TOKENS_PER_FILLER = 15

# Per-provider wiring for the pooled hub APIs.
#   auth   : subscription-key header the provider's SDK naturally sends
#   suffix : gateway path for this provider
#   fmt    : "chat" (openai/google) | "messages" (anthropic)
#   default: a model that exists on both hubs
PROVIDERS = {
    "openai": {
        "auth": "api-key",
        "suffix": "/llm-openai/v1/responses",
        "fmt": "responses",
        "default": "gpt-5.5",
    },
    "anthropic": {
        "auth": "x-api-key",
        "suffix": "/llm-anthropic/v1/messages",
        "fmt": "messages",
        "default": "claude-opus-4.8",
    },
    "google": {
        "auth": "api-key",
        "suffix": "/llm-google/v1/chat/completions",
        "fmt": "chat",
        "default": "gemini-2.5-pro",
    },
}


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


def build_prefix(target_tokens: int) -> str:
    # Unique nonce up front so every phase/run starts from a cold, never-cached
    # prefix (byte-identical prefix is what the cache keys on).
    nonce = f"[session {uuid.uuid4().hex}] "
    return (nonce + FILLER * max(1, target_tokens // TOKENS_PER_FILLER)).strip()


def decode_backend(cookie_val: str) -> str:
    """The SessionId cookie value is base64(backend-name) on this gateway."""
    try:
        return base64.b64decode(urllib.parse.unquote(cookie_val)).decode()
    except Exception:
        return "?"


def build_body(fmt: str, model: str, prefix: str, history: list[dict], turn: int) -> dict:
    user_msg = f"(turn {turn}) Reply with a short sentence."
    if fmt == "messages":
        return {
            "model": model,
            "max_tokens": 256,
            "system": prefix,
            "messages": history + [{"role": "user", "content": user_msg}],
        }
    if fmt == "responses":
        # OpenAI Responses API: a single `input` STRING. Keep the big prefix at
        # the front every turn so the cacheable prefix stays byte-identical.
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        return {
            "model": model,
            "max_output_tokens": 256,
            "input": f"{prefix}\n{convo}\nuser: {user_msg}",
        }
    # chat (openai / google): system message leads, then history.
    return {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "system", "content": prefix}]
        + history
        + [{"role": "user", "content": user_msg}],
    }


def read_usage(fmt: str, data: dict) -> tuple[int, int]:
    """Return (prompt_tokens, cached_tokens) normalized across formats."""
    u = data.get("usage", {}) or {}
    if fmt == "messages":
        prompt = u.get("input_tokens", 0) or 0
        cached = u.get("cache_read_input_tokens", 0) or 0
    elif fmt == "responses":
        prompt = u.get("input_tokens", 0) or 0
        cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    else:
        prompt = u.get("prompt_tokens", 0) or 0
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    return int(prompt), int(cached)


def read_reply(fmt: str, data: dict) -> str:
    try:
        if fmt == "messages":
            parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            return "".join(parts).strip()
        if fmt == "responses":
            txt = (data.get("output_text") or "").strip()
            if txt:
                return txt
            chunks = []
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            chunks.append(c.get("text", ""))
            return "".join(chunks).strip()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return ""


def call(gateway: str, auth: str, key: str, suffix: str, fmt: str,
         model: str, prefix: str, history: list[dict], turn: int, cookie: str | None) -> dict:
    body = build_body(fmt, model, prefix, history, turn)
    req = urllib.request.Request(gateway + suffix, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header(auth, key)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            set_cookie = resp.headers.get("Set-Cookie", "") or ""
    except urllib.error.HTTPError as e:
        return {"status": e.code, "err": e.read().decode("utf-8", "replace")[:120]}
    except Exception as e:
        return {"status": 0, "err": str(e)[:120]}

    sid = ""
    for part in set_cookie.split(";"):
        if part.strip().lower().startswith("sessionid="):
            sid = part.strip()[len("SessionId="):]
            break
    prompt, cached = read_usage(fmt, data)
    return {
        "status": 200,
        "prompt": prompt,
        "cached": cached,
        "backend": decode_backend(sid) if sid else "",
        "set_cookie_raw": sid,
        "reply": read_reply(fmt, data),
    }


def run_phase(gateway: str, cfg: dict, key: str, model: str, turns: int,
              prefix_tokens: int, sticky: bool) -> dict:
    label = "SESSION AFFINITY (cookie)" if sticky else "ROUND-ROBIN (no cookie)"
    print(f"\n{'-'*92}\nPHASE: {label}")
    print(f"{'TURN':<5} {'BACKEND':<22} {'PROMPT':>7} {'CACHED':>7} {'HIT%':>6}  REPLY / ERROR")

    # Each phase builds its OWN cold prefix so round-robin can't pre-warm affinity.
    prefix = build_prefix(prefix_tokens)
    history: list[dict] = []
    cookie: str | None = None
    backends_seen: set[str] = set()
    last_backend = ""
    hits = 0
    prefix_ok = False
    for turn in range(1, turns + 1):
        r = call(gateway, cfg["auth"], key, cfg["suffix"], cfg["fmt"],
                 model, prefix, history, turn, cookie)
        if r["status"] != 200:
            print(f"{turn:<5} {'—':<22} {'—':>7} {'—':>7} {'—':>6}  HTTP {r['status']}: {r.get('err','')}")
            return {"error": True}

        if sticky and cookie is None and r.get("set_cookie_raw"):
            cookie = f"SessionId={r['set_cookie_raw']}"

        # Only the first sticky turn gets a Set-Cookie; later turns are still
        # pinned, so show "<backend> (pinned)" rather than a misleading blank.
        if r["backend"]:
            display_be = r["backend"]
            last_backend = r["backend"]
        elif last_backend:
            display_be = f"{last_backend} (pinned)"
        else:
            display_be = "(unknown)"
        backends_seen.add(r["backend"] or last_backend or "(unknown)")
        hit = (r["cached"] / r["prompt"] * 100) if r["prompt"] else 0.0
        if r["prompt"] >= 1024:
            prefix_ok = True
        if r["cached"] > 0:
            hits += 1
        print(f"{turn:<5} {display_be:<22} {r['prompt']:>7} {r['cached']:>7} {hit:>5.0f}%  "
              f"{' '.join(r['reply'].split())[:30]}")
        history.append({"role": "user", "content": f"(turn {turn}) Reply with a short sentence."})
        history.append({"role": "assistant", "content": r["reply"] or "ok"})
        if turn < turns:
            time.sleep(1.5)

    return {"hits": hits, "turns": turns, "backends": backends_seen, "prefix_ok": prefix_ok}


def run_provider(gateway: str, key: str, provider: str, model: str,
                 turns: int, prefix_tokens: int) -> tuple[str, dict, dict]:
    cfg = PROVIDERS[provider]
    print(f"\n{'='*92}")
    print(f"PROVIDER: {provider}  ({cfg['suffix']}, fmt={cfg['fmt']}, auth={cfg['auth']})")
    print(f"MODEL   : {model}   prefix ~{prefix_tokens} tok   turns {turns}")
    rr = run_phase(gateway, cfg, key, model, turns, prefix_tokens, sticky=False)
    sa = run_phase(gateway, cfg, key, model, turns, prefix_tokens, sticky=True)
    return provider, rr, sa


def main() -> int:
    p = argparse.ArgumentParser(description="Hub pool round-robin vs session affinity, cache effect.")
    p.add_argument("--provider", choices=sorted(PROVIDERS), help="test one provider (default: all)")
    p.add_argument("--model", help="override model (only valid with --provider)")
    p.add_argument("--turns", type=int, default=6)
    p.add_argument("--prefix-tokens", type=int, default=2500)
    args = p.parse_args()

    gateway = require("TF_GATEWAY_URL").rstrip("/")
    key = require("TF_VIRTUAL_KEY")
    if args.model and not args.provider:
        sys.exit("--model requires --provider")

    targets = [args.provider] if args.provider else list(PROVIDERS)
    print(f"Gateway : {gateway}")
    print(f"Testing : {', '.join(targets)}")

    results = []
    for prov in targets:
        model = args.model if (args.model and args.provider == prov) else PROVIDERS[prov]["default"]
        try:
            results.append(run_provider(gateway, key, prov, model, args.turns, args.prefix_tokens))
        except Exception as e:  # noqa: BLE001 — keep going to the next provider
            print(f"  ({prov} errored: {e})")

    print(f"\n{'='*92}\nSUMMARY (hub pools)")
    for prov, rr, sa in results:
        if rr.get("error") or sa.get("error"):
            print(f"  {prov:<12} ERROR during run")
            continue
        rr_be = sorted(rr["backends"])
        sa_be = sorted(sa["backends"])
        print(f"  {prov:<12} round-robin hit {rr['hits']}/{rr['turns']} (backends {rr_be})  |  "
              f"affinity hit {sa['hits']}/{sa['turns']} (backends {sa_be})")
    print("\n  Note: caching is best-effort, so a single small run can be noisy — affinity's edge "
          "shows over larger/repeated runs. Both hubs are independent, so pinning genuinely keeps "
          "a warm cache on one backend.")
    return 0


if __name__ == "__main__":
    load_dotenv_if_present()
    raise SystemExit(main())
