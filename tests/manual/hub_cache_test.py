#!/usr/bin/env python3
"""Token Foundry — prompt-caching test against the UPSTREAM HUB (direct, no APIM).

Companion / A-B counterpart to apim_cache_test_providers.py. That script goes
THROUGH the APIM gateway; THIS one hits the upstream hub DIRECTLY, so you can
compare the two side by side and see whether the gateway layer changes caching
behavior at all (it shouldn't — caching is an upstream/model feature; APIM just
forwards).

Differences from the APIM script (all confirmed by probing the live hub):
  * Base URL is the hub, not the APIM gateway.
  * Auth is a single Bearer token for ALL providers (the hub is an OpenAI-
    compatible front). NOTE: on the hub, Anthropic caching engages under Bearer
    auth (the hub adds cache_control on that path); through APIM, Anthropic uses
    its native x-api-key header instead. Same cache field either way.
  * No /llm-<provider> path prefix — endpoints are the bare /v1/... paths.

Cached-tokens fields (identical to the APIM side — the hub emits them, APIM just
passes them through):
  openai     /v1/responses           usage.input_tokens_details.cached_tokens
  google     /v1/chat/completions    usage.prompt_tokens_details.cached_tokens
  anthropic  /v1/messages            usage.cache_read_input_tokens

------------------------------------------------------------------------------
NO SECRETS IN THIS FILE. Configure via env or a local .env (gitignored):

    TF_HUB_URL      the upstream hub base URL, e.g. https://<hub>.azurecontainerapps.io
    TF_HUB_KEY      a hub API key (sk-hub-...)

Usage (pure stdlib, python-dotenv optional):
    python tests/manual/hub_cache_test.py                       # all three
    python tests/manual/hub_cache_test.py --provider anthropic  # just one
    python tests/manual/hub_cache_test.py --turns 6 --prefix-tokens 3000

Run this and apim_cache_test_providers.py back to back to compare hub-direct vs
APIM.
Exit code is non-zero if a tested provider that CAN cache never engaged.

Notes from live testing (so results aren't misread):
  * Caching is BEST-EFFORT: the first call(s) with a fresh prefix populate the
    cache and report cached=0; hits start a turn or two later. A lone miss
    mid-run is normal, not a failure.
  * gpt-5.5 caches fine despite a slower warm-up — measured ~85% hit rate over
    20 back-to-back calls (first hit on the 2nd call). Earlier "gpt-5.5 doesn't
    cache" impressions were a too-short run + a param bug (see below), not the
    model.
  * gpt-5.x (reasoning models) REQUIRE max_output_tokens >= 16; a smaller value
    returns HTTP 400, not a cache miss. This script uses 256 so reasoning models
    have room to both think and emit a visible reply.
  * Google's implicit cache has a stricter bar; if it reports 0, raise
    --prefix-tokens rather than assuming it's unsupported.
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

# Per-provider wiring for the HUB (direct). All use Bearer auth; no path prefix.
#   suffix : bare operation path on the hub
#   fmt    : "responses" (OpenAI gpt-5.x) | "chat" (Google) | "messages" (Anthropic)
PROVIDERS = {
    "openai": {"suffix": "/v1/responses", "fmt": "responses", "default": "gpt-5.5"},
    "google": {"suffix": "/v1/chat/completions", "fmt": "chat", "default": "gemini-2.5-pro"},
    "anthropic": {"suffix": "/v1/messages", "fmt": "messages", "default": "claude-opus-4.8"},
}


# --------------------------------------------------------------------------- #
# Config + HTTP                                                                #
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
# Per-format request builders + usage readers (same shapes as the APIM script) #
# --------------------------------------------------------------------------- #
def build_prefix(target_tokens: int) -> str:
    n = max(1, target_tokens // TOKENS_PER_FILLER)
    return (FILLER * n).strip()


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
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        return {
            "model": model,
            "max_output_tokens": 256,
            "input": f"{prefix}\n{convo}\nuser: {user_msg}",
        }
    return {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "system", "content": prefix}]
        + history
        + [{"role": "user", "content": user_msg}],
    }


def read_usage(fmt: str, body: dict) -> dict:
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
    history.append({"role": "user", "content": f"(turn {user_turn}) Reply with a short sentence."})
    history.append({"role": "assistant", "content": reply or "ok"})


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #
def run_provider(
    hub: str, key: str, provider: str, model: str, turns: int, prefix_tokens: int
) -> tuple[str, int]:
    cfg = PROVIDERS[provider]
    url = f"{hub}{cfg['suffix']}"
    headers = {"Authorization": f"Bearer {key}"}  # hub: single Bearer for all providers
    prefix = build_prefix(prefix_tokens)
    history: list[dict] = []

    print(f"\n{'='*90}")
    print(f"PROVIDER: {provider}  (HUB {cfg['suffix']}, fmt={cfg['fmt']}, auth=Bearer)")
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
            time.sleep(2)

    if not prefix_ok:
        print(f"  -> INCONCLUSIVE: prompt never hit the ~1024-token threshold; raise --prefix-tokens.")
        return provider, 3
    if any_hit:
        print(f"  -> PASS: caching engaged (cached_tokens > 0).")
        return provider, 0
    print(f"  -> FAIL/NA: prefix over threshold but cached stayed 0.")
    return provider, 1


def main() -> int:
    p = argparse.ArgumentParser(description="Verify prompt caching directly against the upstream hub.")
    p.add_argument("--provider", choices=sorted(PROVIDERS), help="test one provider (default: all)")
    p.add_argument("--model", help="override the model (only valid with --provider)")
    p.add_argument("--turns", type=int, default=5)
    p.add_argument("--prefix-tokens", type=int, default=2500)
    args = p.parse_args()

    hub = require("TF_HUB_URL").rstrip("/")
    key = require("TF_HUB_KEY")

    targets = [args.provider] if args.provider else list(PROVIDERS)
    if args.model and not args.provider:
        sys.exit("--model requires --provider")

    print(f"HUB     : {hub}   (DIRECT — no APIM)")
    print(f"Testing : {', '.join(targets)}")

    results = []
    for prov in targets:
        model = args.model if (args.model and args.provider == prov) else PROVIDERS[prov]["default"]
        results.append(run_provider(hub, key, prov, model, args.turns, args.prefix_tokens))

    print(f"\n{'='*90}\nSUMMARY (hub-direct)")
    worst = 0
    for prov, code in results:
        label = {0: "PASS", 1: "FAIL/NA", 2: "ERROR", 3: "INCONCLUSIVE"}.get(code, "?")
        print(f"  {prov:<12} {label}")
        worst = max(worst, code)
    return worst


if __name__ == "__main__":
    load_dotenv_if_present()
    raise SystemExit(main())
