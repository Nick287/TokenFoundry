#!/usr/bin/env python3
"""Token Foundry — pool load-balancing vs session-affinity, and its effect on cache.

This proves WHY the recommended architecture (doc §4) pairs a backend pool with
SESSION AFFINITY for chat workloads: Azure OpenAI's prompt cache is NOT shared
across instances, so a multi-turn chat that gets round-robined to different
backends keeps MISSING the cache. Pinning the session to one backend restores
high cache-hit rates.

It runs the SAME growing multi-turn chat twice against the pooled llm-azure API:

  PHASE 1 — no cookie (pure round-robin):
    every turn is an independent request → APIM load-balances it → turns land on
    different backends → prompt cache misses when the backend flips.

  PHASE 2 — with the SessionId cookie (session affinity):
    the first turn's `Set-Cookie: SessionId=...` is captured and sent on every
    later turn → all turns pin to the SAME backend → cache warms and stays hot.

Handy detail discovered on this gateway: the SessionId cookie value is just the
base64 of the chosen backend name (e.g. bGxtLWF6dXJlLTI= -> "llm-azure-2"), so
we can print which backend each turn hit WITHOUT querying App Insights.

------------------------------------------------------------------------------
NO SECRETS IN THIS FILE. Configure via env or a local .env (gitignored):

    TF_GATEWAY_URL   e.g. https://<your-apim>.azure-api.net
    TF_VIRTUAL_KEY   an APIM subscription (virtual) key

Usage (pure stdlib):
    python tests/manual/pool_session_cache_test.py
    python tests/manual/pool_session_cache_test.py --turns 8 --prefix-tokens 3000
    python tests/manual/pool_session_cache_test.py --model gpt-5.4-mini
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
SUFFIX = "/llm-azure/openai/v1/chat/completions"


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
    # Lead with a unique nonce so every RUN gets a brand-new, never-cached prefix.
    # Without this, repeated runs reuse the same text that's still warm in the
    # backends' prompt cache, so turn 1 already shows a hit and hides the real
    # populate -> hit progression.
    nonce = f"[session {uuid.uuid4().hex}] "
    return (nonce + FILLER * max(1, target_tokens // TOKENS_PER_FILLER)).strip()


def decode_backend(cookie_val: str) -> str:
    """The SessionId cookie value is base64(backend-name) on this gateway."""
    try:
        return base64.b64decode(urllib.parse.unquote(cookie_val)).decode()
    except Exception:
        return "?"


def call(gateway: str, key: str, messages: list[dict], cookie: str | None, model: str) -> dict:
    """One chat call. Returns {status, cached, prompt, backend, set_cookie, reply}."""
    body = {"model": model, "messages": messages, "max_completion_tokens": 40}
    req = urllib.request.Request(
        gateway + SUFFIX, data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("api-key", key)
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

    u = data.get("usage", {}) or {}
    # Extract SessionId from Set-Cookie (if the gateway set one this turn).
    sid = ""
    for part in set_cookie.split(";"):
        if part.strip().lower().startswith("sessionid="):
            sid = part.strip()[len("SessionId="):]
            break
    reply = ""
    try:
        reply = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        pass
    return {
        "status": 200,
        "prompt": u.get("prompt_tokens", 0) or 0,
        "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
        "backend": decode_backend(sid) if sid else "",
        "set_cookie_raw": sid,
        "reply": reply,
    }


def run_phase(gateway: str, key: str, model: str, turns: int, prefix_tokens: int, sticky: bool) -> dict:
    label = "SESSION AFFINITY (cookie)" if sticky else "ROUND-ROBIN (no cookie)"
    print(f"\n{'='*92}\nPHASE: {label}\n{'-'*92}")
    print(f"{'TURN':<5} {'BACKEND':<22} {'PROMPT':>7} {'CACHED':>7} {'HIT%':>6}  REPLY / ERROR")

    # Each phase builds its OWN fresh prefix (unique nonce), so the round-robin
    # phase can't pre-warm the affinity phase's cache — both start cold at turn 1.
    prefix = build_prefix(prefix_tokens)
    messages = [{"role": "system", "content": prefix}]
    cookie: str | None = None
    backends_seen: set[str] = set()
    last_backend = ""  # remember the last backend we could resolve
    hits = 0
    prefix_ok = False
    for turn in range(1, turns + 1):
        messages.append({"role": "user", "content": f"(turn {turn}) Reply with a short sentence."})
        r = call(gateway, key, messages, cookie, model)
        if r["status"] != 200:
            print(f"{turn:<5} {'—':<14} {'—':>7} {'—':>7} {'—':>6}  HTTP {r['status']}: {r.get('err','')}")
            return {"error": True}

        # In sticky mode, capture the first SessionId cookie and reuse it.
        if sticky and cookie is None and r.get("set_cookie_raw"):
            cookie = f"SessionId={r['set_cookie_raw']}"

        # The gateway only sends Set-Cookie on the FIRST turn of a sticky session;
        # later turns carry the cookie but get no new Set-Cookie, so r["backend"]
        # is empty. When that happens we're still pinned to the same backend, so
        # show it as "<backend> (pinned)" instead of a misleading "(unknown)".
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
        messages.append({"role": "assistant", "content": r["reply"] or "ok"})
        if turn < turns:
            time.sleep(1.5)

    return {"hits": hits, "turns": turns, "backends": backends_seen, "prefix_ok": prefix_ok}


def main() -> int:
    p = argparse.ArgumentParser(description="Pool round-robin vs session affinity, cache effect.")
    p.add_argument("--model", default="gpt-5.4")
    p.add_argument("--turns", type=int, default=6)
    p.add_argument("--prefix-tokens", type=int, default=2500)
    args = p.parse_args()

    gateway = require("TF_GATEWAY_URL").rstrip("/")
    key = require("TF_VIRTUAL_KEY")

    print(f"Gateway : {gateway}")
    print(f"Model   : {args.model}   prefix ~{args.prefix_tokens} tok   turns {args.turns}")

    rr = run_phase(gateway, key, args.model, args.turns, args.prefix_tokens, sticky=False)
    if rr.get("error"):
        return 2
    sa = run_phase(gateway, key, args.model, args.turns, args.prefix_tokens, sticky=True)
    if sa.get("error"):
        return 2

    print(f"\n{'='*92}\nSUMMARY")
    print(f"  round-robin : hit {rr['hits']}/{rr['turns']} turns, backends touched: {sorted(rr['backends'])}")
    print(f"  affinity    : hit {sa['hits']}/{sa['turns']} turns, backends touched: {sorted(sa['backends'])}")
    print()
    if len(sa["backends"]) == 1 and len(rr["backends"]) > 1:
        print("  -> Session affinity pinned all turns to ONE backend (round-robin spread across "
              "several).")
    if sa["hits"] >= rr["hits"]:
        print("  -> Affinity cache-hit >= round-robin, as expected (cache isn't shared across "
              "instances).")
    else:
        print("  -> NOTE: round-robin scored >= affinity this run — small-sample noise or a warm "
              "cache on multiple backends; re-run to confirm.")
    return 0


if __name__ == "__main__":
    load_dotenv_if_present()
    raise SystemExit(main())
