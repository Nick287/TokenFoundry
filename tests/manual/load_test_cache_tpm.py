#!/usr/bin/env python3
"""Token Foundry — does prompt caching raise the TPM ceiling, or only latency?

`load_test_ramp.py` fires a ~10-token prompt, which is two orders of magnitude
below every provider's cache floor (openai ~1280, anthropic ~1991, google ~2200
tokens). So every request there is a COLD miss and the TPM figures it reports
(41.6k / 88k / 159k for 1/2/3 accounts) are a worst-case floor.

This script answers the question that leaves open: when the cache IS warm, does
the Copilot account let us push more tokens per minute, or does the quota count
cached tokens at full price so caching only buys latency?

It runs the SAME concurrency twice and compares:

    PHASE COLD  tiny prompt, no cookie          <- reproduces load_test_ramp
    PHASE WARM  ~3000-token prefix + SessionId  <- cache hot from turn 2 on

Each worker owns a unique prefix (per-worker nonce) and a sticky session, then
fires `--turns` sequential requests. Turn 1 is a cold miss by construction; the
rest should hit. Reported per phase:

    * RPS / TPM (billed tokens: what the provider counted)
    * cached tokens and hit rate
    * 429 (upstream throttle -> the real ceiling) / 503 (breaker open)
    * p50 / p95 latency

READ THE TPM COLUMNS CAREFULLY — there are two:
    TPM(billed) counts every prompt token, cached or not. This is the number
                comparable to load_test_ramp.py.
    TPM(new)    excludes cached prompt tokens — the tokens actually computed.
If caching raised the ceiling, TPM(billed) goes UP in WARM. If the quota charges
cached tokens at full price, TPM(billed) stays flat while p95 drops.

NO SECRETS IN THIS FILE. Configure via env or a local .env:
    TF_GATEWAY_URL   https://<apim>.azure-api.net
    TF_VIRTUAL_KEY   an APIM subscription key (prefer an UNLIMITED one, so you
                     measure the Copilot quota and not our llm-token-limit)
Optional:
    TF_MODEL         default gpt-4o-mini

Usage:
    python tests/manual/load_test_cache_tpm.py
    python tests/manual/load_test_cache_tpm.py --conc 72 --turns 6
    python tests/manual/load_test_cache_tpm.py --only warm --prefix-tokens 4000
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------- #
# Tunables                                                                     #
# --------------------------------------------------------------------------- #

# Concurrency to hold for both phases. 72 = the measured 3-account sweet spot
# (see docs/CAPACITY.zh.md §2.7) — zero 429/503 there when cold, so any errors
# appearing in WARM are attributable to the cache change, not to overload.
CONC = 72
# Sequential requests per worker. Turn 1 is always a cold miss; turns 2..N should
# hit. 6 turns => 5/6 of requests warm, enough to move the aggregate.
TURNS = 6
# Prefix size for the WARM phase. Must clear the highest provider floor (google
# ~2200) with margin so the cache reliably engages.
PREFIX_TOKENS = 3000
MAX_TOKENS = 1200
TIMEOUT = 120
# Idle between phases so a breaker tripped by the first phase can reset (60s trip).
COOLDOWN = 120

FILLER = "You are an expert assistant; always consider the following reference note carefully. "
TOKENS_PER_FILLER = 15
_MARKER_TOKENS = 6

COLD_PROMPT = "Write a short paragraph about the ocean."


def load_dotenv_if_present() -> None:
    for path in (".env", os.path.join(os.path.dirname(__file__), "..", "..", ".env")):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return


def require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"ERROR: env var {name} is required (see docstring).")
    return v


def build_prefix(target_tokens: int, nonce: str) -> str:
    """A cacheable filler prefix that is unique per worker.

    The nonce is woven into EVERY segment, not just the front: providers that do
    prefix caching would otherwise hit on the byte-identical tail left over from
    a previous run/worker, and turn 1 would not be a genuine cold miss.
    """
    per_seg = TOKENS_PER_FILLER + _MARKER_TOKENS
    n = max(1, target_tokens // per_seg)
    return "".join(f"[{nonce}#{i}] {FILLER}" for i in range(n)).strip()


def _cached_tokens(usage: dict) -> int:
    """Cached prompt tokens across provider shapes."""
    det = usage.get("prompt_tokens_details") or {}
    if isinstance(det, dict) and det.get("cached_tokens") is not None:
        return int(det.get("cached_tokens") or 0)
    # anthropic
    return int(usage.get("cache_read_input_tokens", 0) or 0)


def one_call(url, headers, body, timeout, cookie: str | None) -> dict:
    t0 = time.perf_counter()
    hdrs = dict(headers)
    if cookie:
        hdrs["Cookie"] = cookie
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            dt = time.perf_counter() - t0
            set_cookie = r.headers.get("Set-Cookie") or ""
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                obj = {}
            u = obj.get("usage", {}) or {}
            prompt = int(u.get("prompt_tokens", u.get("input_tokens", 0)) or 0)
            total = int(u.get("total_tokens", 0) or 0)
            if not total:
                total = prompt + int(u.get("completion_tokens",
                                          u.get("output_tokens", 0)) or 0)
            return {"status": 200, "sec": dt, "tokens": total,
                    "cached": _cached_tokens(u), "prompt": prompt,
                    "set_cookie": set_cookie.split(";")[0] if set_cookie else ""}
    except urllib.error.HTTPError as e:
        dt = time.perf_counter() - t0
        try:
            txt = e.read()[:120].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            txt = ""
        return {"status": e.code, "sec": dt, "tokens": 0, "cached": 0,
                "prompt": 0, "set_cookie": "", "err": txt}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "sec": time.perf_counter() - t0, "tokens": 0,
                "cached": 0, "prompt": 0, "set_cookie": "", "err": type(e).__name__}


def worker(url, headers, model, mode: str, turns: int, max_tokens: int,
           timeout: int, prefix_tokens: int) -> list[dict]:
    """One virtual user: `turns` SEQUENTIAL requests.

    mode:
      "cold"    tiny prompt, no session      -> reproduces load_test_ramp.py
      "warm"    big prefix + sticky session  -> cache hot from turn 2
      "bigcold" big prefix, but a FRESH nonce every turn and no session, so the
                prompt is the same SIZE as warm while the cache never hits. This
                is the control that isolates "does a cached token cost quota?"
                from "does a bigger prompt cost quota?" — warm vs bigcold differ
                only in hit rate, not in token count.

    Sequential (not parallel) inside a worker is the point — the cache can only
    be warm on turn N if turn N-1 already returned. Concurrency comes from
    running many workers at once.
    """
    out = []
    cookie = None
    warm = mode == "warm"
    prefix = build_prefix(prefix_tokens, uuid.uuid4().hex[:6]) if warm else None
    history: list[dict] = []
    for t in range(turns):
        if warm:
            msgs = [{"role": "system", "content": prefix}]
            msgs += history
            msgs.append({"role": "user", "content": f"(turn {t}) Reply briefly."})
        elif mode == "bigcold":
            # Fresh nonce EVERY turn => same token count as warm, zero cache hits.
            msgs = [
                {"role": "system",
                 "content": build_prefix(prefix_tokens, uuid.uuid4().hex[:6])},
                {"role": "user", "content": f"(turn {t}) Reply briefly."},
            ]
        else:
            msgs = [{"role": "user", "content": COLD_PROMPT}]
        body = {"model": model, "max_tokens": max_tokens, "messages": msgs}
        r = one_call(url, headers, body, timeout, cookie)
        r["turn"] = t
        out.append(r)
        if warm:
            # Pin every later turn to the backend that served turn 1, so the
            # prompt cache we just populated is the one we come back to.
            if not cookie and r.get("set_cookie"):
                cookie = r["set_cookie"]
            history.append({"role": "user", "content": f"(turn {t}) Reply briefly."})
            history.append({"role": "assistant", "content": "OK."})
    return out


def run_phase(url, headers, model, *, mode: str, conc: int, turns: int,
              max_tokens: int, timeout: int, prefix_tokens: int) -> dict:
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        nested = list(ex.map(
            lambda _: worker(url, headers, model, mode, turns, max_tokens,
                             timeout, prefix_tokens),
            range(conc)))
    wall = time.perf_counter() - t0
    res = [r for chunk in nested for r in chunk]

    ok = [r for r in res if r["status"] == 200]
    lat = sorted(r["sec"] for r in ok)
    tokens = sum(r["tokens"] for r in ok)
    cached = sum(r["cached"] for r in ok)
    prompt = sum(r["prompt"] for r in ok)
    # Warm turns only (turn>0) — turn 1 is a cold miss by construction, so
    # including it understates the steady-state hit rate.
    warm_ok = [r for r in ok if r["turn"] > 0]
    warm_hits = sum(1 for r in warm_ok if r["cached"] > 0)
    return {
        "n": len(res), "wall": wall, "ok": len(ok),
        "c429": sum(1 for r in res if r["status"] == 429),
        "c503": sum(1 for r in res if r["status"] == 503),
        "other": sum(1 for r in res if r["status"] not in (200, 429, 503)),
        "rps": len(ok) / wall if wall else 0,
        "tpm_billed": tokens / wall * 60 if wall else 0,
        "tpm_new": (tokens - cached) / wall * 60 if wall else 0,
        "cached": cached, "prompt": prompt,
        "hit_pct": (cached / prompt * 100) if prompt else 0,
        "warm_hit": f"{warm_hits}/{len(warm_ok)}" if warm_ok else "-",
        "p50": statistics.median(lat) if lat else 0,
        "p95": (lat[int(len(lat) * 0.95)] if len(lat) > 1 else (lat[0] if lat else 0)),
        "sample_err": next((r.get("err", "") for r in res if r.get("err")), ""),
    }


def show(tag: str, r: dict) -> None:
    print(f"{tag:<6} {r['ok']:>4} {r['c429']:>4} {r['c503']:>4} {r['other']:>4} "
          f"{r['rps']:>7.2f} {r['tpm_billed']:>11,.0f} {r['tpm_new']:>11,.0f} "
          f"{r['hit_pct']:>6.1f}% {r['warm_hit']:>9} {r['p50']:>7.2f} {r['p95']:>7.2f}")
    if r["sample_err"]:
        print(f"       err sample: {r['sample_err'][:100]}")


def main() -> int:
    load_dotenv_if_present()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conc", type=int, default=CONC)
    ap.add_argument("--turns", type=int, default=TURNS)
    ap.add_argument("--prefix-tokens", type=int, default=PREFIX_TOKENS)
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    ap.add_argument("--cooldown", type=int, default=COOLDOWN)
    ap.add_argument("--only", choices=("cold", "bigcold", "warm"),
                    help="run just one phase")
    ap.add_argument("--phases", default="cold,bigcold,warm",
                    help="comma-separated phases to run, in order "
                         "(default cold,bigcold,warm)")
    args = ap.parse_args()

    gw = require("TF_GATEWAY_URL").rstrip("/")
    vk = require("TF_VIRTUAL_KEY")
    model = os.environ.get("TF_MODEL", "gpt-4o-mini")
    url = gw + "/llm-openai/v1/chat/completions"
    headers = {"api-key": vk, "Content-Type": "application/json"}

    total_req = args.conc * args.turns
    print(f"target : {url}")
    print(f"model  : {model}   max_tokens={args.max_tokens}")
    print(f"conc   : {args.conc} workers x {args.turns} sequential turns "
          f"= {total_req} requests/phase")
    print(f"prefix : {args.prefix_tokens} tokens (WARM phase only)\n")

    hdr = (f"{'phase':<6} {'ok':>4} {'429':>4} {'503':>4} {'err':>4} {'RPS':>7} "
           f"{'TPM(billed)':>11} {'TPM(new)':>11} {'hit':>7} {'warmhit':>9} "
           f"{'p50 s':>7} {'p95 s':>7}")
    print(hdr)
    print("-" * len(hdr))

    phases = [args.only] if args.only else [
        p.strip() for p in args.phases.split(",") if p.strip()
    ]
    results = {}
    for i, ph in enumerate(phases):
        r = run_phase(url, headers, model, mode=ph, conc=args.conc,
                      turns=args.turns, max_tokens=args.max_tokens,
                      timeout=args.timeout, prefix_tokens=args.prefix_tokens)
        results[ph] = r
        show(ph.upper(), r)
        if i != len(phases) - 1:
            time.sleep(args.cooldown)

    def delta(a: float, b: float) -> str:
        if not a:
            return "n/a"
        return f"{b / a:.2f}x ({(b - a) / a * 100:+.0f}%)"

    if "bigcold" in results and "warm" in results:
        bc, w = results["bigcold"], results["warm"]
        print("\n" + "=" * 78)
        print("VERDICT A — do CACHED tokens cost quota? (same prompt SIZE, "
              "only hit rate differs)")
        print("=" * 78)
        print(f"  cache hit    {bc['hit_pct']:>9.1f}% -> {w['hit_pct']:>9.1f}%")
        print(f"  429 (quota)  {bc['c429']:>10} -> {w['c429']:>10}")
        print(f"  503          {bc['c503']:>10} -> {w['c503']:>10}")
        print(f"  TPM(billed)  {bc['tpm_billed']:>10,.0f} -> {w['tpm_billed']:>10,.0f}   "
              f"{delta(bc['tpm_billed'], w['tpm_billed'])}")
        print(f"  p95 latency  {bc['p95']:>10.2f}s -> {w['p95']:>9.2f}s   "
              f"{delta(bc['p95'], w['p95'])}")
        print("\n  How to read it:")
        print("    WARM has FEWER 429 than BIGCOLD -> cached tokens are cheaper on")
        print("                                       quota (or free)")
        print("    429 counts about EQUAL          -> cached tokens are charged at")
        print("                                       full price; cache buys latency")

    if "cold" in results and "warm" in results:
        c, w = results["cold"], results["warm"]
        print("\n" + "=" * 78)
        print("VERDICT B — tiny prompt vs big cached prompt (what load_test_ramp "
              "measured vs reality)")
        print("=" * 78)
        print(f"  TPM(billed)  {c['tpm_billed']:>10,.0f} -> {w['tpm_billed']:>10,.0f}   "
              f"{delta(c['tpm_billed'], w['tpm_billed'])}")
        print(f"  TPM(new)     {c['tpm_new']:>10,.0f} -> {w['tpm_new']:>10,.0f}   "
              f"{delta(c['tpm_new'], w['tpm_new'])}")
        print(f"  RPS          {c['rps']:>10.2f} -> {w['rps']:>10.2f}   "
              f"{delta(c['rps'], w['rps'])}")
        print(f"  429 (quota)  {c['c429']:>10} -> {w['c429']:>10}")
        print("\n  If both run clean at the same concurrency, TPM is a RESULT of")
        print("  prompt size, not a quota — plan capacity by CONCURRENCY instead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
