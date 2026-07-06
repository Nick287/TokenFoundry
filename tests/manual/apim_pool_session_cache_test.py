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
    python tests/manual/apim_pool_session_cache_test.py                    # all three
    python tests/manual/apim_pool_session_cache_test.py --provider openai  # just one
    python tests/manual/apim_pool_session_cache_test.py --turns 8 --prefix-tokens 3000
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
_MARKER_TOKENS = 6  # ~token cost of the per-segment "[<6hex>#<i>] " nonce marker

# Per-provider wiring for the pooled hub APIs.
#   auth       : subscription-key header the provider's SDK naturally sends
#   suffix     : gateway path for this provider
#   fmt        : "chat" (openai/google) | "messages" (anthropic)
#   default    : a model that exists on both hubs
#   min_prefix : per-provider FLOOR on prefix tokens (optional). A provider's
#                implicit prompt cache only kicks in above a minimum context
#                size; below it, cached_tokens is always 0. gemini-2.5-pro needs
#                ~2048 tokens (vs ~1024 for OpenAI/Anthropic), so google gets a
#                floor so its cache actually engages. Effective prefix =
#                max(--prefix-tokens, min_prefix).
PROVIDERS = {
    "openai": {
        "auth": "api-key",
        "suffix": "/llm-openai/v1/responses",
        "fmt": "responses",
        "default": "gpt-5.4-mini",
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
        "min_prefix": 2200,  # gemini-2.5-pro implicit cache needs ~2048 tok
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
    # A single nonce ONLY at the front isn't enough for prefix-caching providers
    # like Anthropic: everything AFTER the nonce is byte-identical filler across
    # runs, so Claude's prefix cache hits that shared tail from a breakpoint and
    # turn 1 already shows cached>0 (pre-warmed by an earlier run). Weave a short
    # per-run nonce into EVERY filler segment so the whole prefix — not just its
    # first line — is unique this run, guaranteeing a genuinely cold turn 1.
    #
    # Keep the marker SHORT (6 hex, not a full 32-char uuid): a full uuid per
    # segment nearly TRIPLES prompt_tokens, blowing the APIM per-subscription
    # token-limit (50k TPM) after a couple of turns. Account for the marker's
    # own token cost so the built prefix still lands near target_tokens.
    nonce = uuid.uuid4().hex[:6]
    per_seg = TOKENS_PER_FILLER + _MARKER_TOKENS
    n = max(1, target_tokens // per_seg)
    segments = [f"[{nonce}#{i}] {FILLER}" for i in range(n)]
    return "".join(segments).strip()


def decode_backend(cookie_val: str) -> str:
    """The SessionId cookie value is base64(backend-name) on this gateway.

    Two gotchas this handles (both bit the naive version):
      * URL-ENCODED PADDING: APIM percent-encodes the trailing '=' base64 padding
        as %3D, so a backend whose name base64-encodes WITH padding (e.g.
        'llm-openai-ext_gitmodelliang' -> '...==') arrives as '...%3D%3D'. Must
        unquote BEFORE b64decode, else it throws and the backend shows as '?'.
        (A name that happens to encode without padding, like the gha one, decoded
        fine — which is why only some rows were '?', misleading the routing view.)
      * TRAILING GARBAGE: some values carry a couple of stray bytes after the
        name (b'...liang\\r\\xff\\xff'); keep only the leading printable-ASCII run
        so the label is clean.
    """
    try:
        raw = urllib.parse.unquote(cookie_val)
        # Restore any base64 padding the transport stripped, then decode leniently.
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", "replace")
        # Trim at the first non-(printable-ascii) byte — drops stray tail bytes.
        out = []
        for ch in decoded:
            if 32 <= ord(ch) < 127:
                out.append(ch)
            else:
                break
        return "".join(out) or "?"
    except Exception:
        return "?"


def build_body(fmt: str, model: str, prefix: str, history: list[dict], turn: int) -> dict:
    user_msg = f"(turn {turn}) Reply with a short sentence."
    if fmt == "messages":
        # Anthropic prompt caching is EXPLICIT: unlike OpenAI/Google (implicit,
        # any long-enough prefix auto-caches), Claude only caches content marked
        # with a cache_control breakpoint. So the system prompt is sent as a
        # structured block with cache_control=ephemeral — without it,
        # cache_read_input_tokens stays 0 even for a large identical prefix.
        # (Copilot's hub now passes /v1/messages through natively, so this is real
        # Anthropic caching, not the implicit OpenAI cache the old conversion used.)
        return {
            "model": model,
            "max_tokens": 256,
            "system": [
                {"type": "text", "text": prefix,
                 "cache_control": {"type": "ephemeral"}}
            ],
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
    """Return (total_prompt_tokens, cached_tokens) normalized across formats.

    `prompt` is normalized to the TOTAL input tokens (cache included) so the
    HIT% = cached/prompt formula is comparable across providers.

    ⚠️ Anthropic vs OpenAI/Google differ in what the base field counts:
      * OpenAI/Google: prompt_tokens INCLUDES cached tokens (cached is a subset),
        so prompt is used as-is.
      * Anthropic: input_tokens EXCLUDES cache. Cache-read tokens live in
        cache_read_input_tokens and first-write tokens in
        cache_creation_input_tokens. So total input = input_tokens +
        cache_read_input_tokens + cache_creation_input_tokens. Without this,
        cached/input_tokens overshoots 100% (e.g. 2061/60 = 3435%).
    """
    u = data.get("usage", {}) or {}
    if fmt == "messages":
        base = u.get("input_tokens", 0) or 0
        cached = u.get("cache_read_input_tokens", 0) or 0
        creation = u.get("cache_creation_input_tokens", 0) or 0
        prompt = base + cached + creation
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
    print(f"{'TURN':<5} {'BACKEND':<44} {'PROMPT':>7} {'CACHED':>7} {'HIT%':>6}  REPLY / ERROR")

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
            print(f"{turn:<5} {'—':<44} {'—':>7} {'—':>7} {'—':>6}  HTTP {r['status']}: {r.get('err','')}")
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
        print(f"{turn:<5} {display_be:<44} {r['prompt']:>7} {r['cached']:>7} {hit:>5.0f}%  "
              f"{' '.join(r['reply'].split())[:30]}")
        history.append({"role": "user", "content": f"(turn {turn}) Reply with a short sentence."})
        history.append({"role": "assistant", "content": r["reply"] or "ok"})
        if turn < turns:
            time.sleep(1.5)

    return {"hits": hits, "turns": turns, "backends": backends_seen, "prefix_ok": prefix_ok}


def run_provider(gateway: str, key: str, provider: str, model: str,
                 turns: int, prefix_tokens: int) -> tuple[str, dict, dict]:
    cfg = PROVIDERS[provider]
    # Raise the prefix to this provider's cache floor if needed (e.g. google's
    # gemini-2.5-pro won't cache below ~2048 tok). max() so an explicit larger
    # --prefix-tokens still wins.
    floor = cfg.get("min_prefix", 0)
    eff_prefix = max(prefix_tokens, floor)
    print(f"\n{'='*92}")
    print(f"PROVIDER: {provider}  ({cfg['suffix']}, fmt={cfg['fmt']}, auth={cfg['auth']})")
    note = f"  (raised from {prefix_tokens} to meet cache floor {floor})" if eff_prefix > prefix_tokens else ""
    print(f"MODEL   : {model}   prefix ~{eff_prefix} tok   turns {turns}{note}")
    rr = run_phase(gateway, cfg, key, model, turns, eff_prefix, sticky=False)
    sa = run_phase(gateway, cfg, key, model, turns, eff_prefix, sticky=True)
    return provider, rr, sa


def main() -> int:
    p = argparse.ArgumentParser(description="Hub pool round-robin vs session affinity, cache effect.")
    p.add_argument("--provider", choices=sorted(PROVIDERS), help="test one provider (default: all)")
    p.add_argument("--model", help="override model (only valid with --provider)")
    p.add_argument("--turns", type=int, default=6)
    p.add_argument("--prefix-tokens", type=int, default=1500)
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
