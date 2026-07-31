#!/usr/bin/env python3
"""Token Foundry — locate the REAL upstream limit: TPM vs concurrency.

WHY THIS EXISTS
---------------
docs/CAPACITY.zh.md concluded "the constraint is concurrency, not TPM" from two
observations: shrinking max_tokens didn't avoid 429s, and a 300x bigger prompt
at 48 concurrency stayed clean at 3.2M TPM. But the data also contains an
unexplained contradiction:

    72 concurrency + ~10-token prompt   -> 0 x 429   (clean)
    72 concurrency + 3000-token prompt  -> 12 x 429  (over the edge)
    48 concurrency + 3000-token prompt  -> 0 x 429   (clean)

Same concurrency, only prompt size differs, and one of them tips over. So token
volume DOES contribute near the edge — "purely concurrency" was concluded too
early. There may be two gates and we never separated them.

THE EXPERIMENTS
---------------
A (tpm)    Pin concurrency LOW (well clear of the concurrency gate) and scale
           prompt size up. If a TPM gate exists, this finds it. If nothing 429s
           even at ~20M TPM, no TPM gate exists.
B (conc)   Pin tokens MINIMAL (tiny prompt, max_tokens=16) and ramp concurrency
           past the known 96 cliff. If 429s still appear at the same point as
           with big prompts, the gate is pure concurrency; if they move later,
           token volume was contributing.
C (io)     Same concurrency, same total tokens, but loaded on the INPUT side vs
           the OUTPUT side. If they tip at different points, input and output
           are metered differently.

Cache is deliberately DEFEATED in every phase (fresh nonce per request) so we
measure raw upstream throughput, not cache-assisted throughput.

READING THE OUTPUT
------------------
A level with ANY 503 is contaminated — the circuit breaker shortens wall-clock
and inflates TPM. Those rows are flagged. Judge only on 429 (upstream) and on
levels that ran clean.

NO SECRETS IN THIS FILE. Configure via env or a local .env:
    TF_GATEWAY_URL   https://<apim>.azure-api.net
    TF_VIRTUAL_KEY   an UNLIMITED APIM subscription key (a key with
                     tokens_per_minute set would trip OUR llm-token-limit first
                     and you'd be measuring our own policy, not Copilot)
Optional:
    TF_MODEL         default claude-opus-4-8

Usage:
    python tests/manual/find_upstream_limit.py --probe        # 429 header probe
    python tests/manual/find_upstream_limit.py --exp b        # cheap, fast
    python tests/manual/find_upstream_limit.py --exp a        # EXPENSIVE
    python tests/manual/find_upstream_limit.py --exp c
    python tests/manual/find_upstream_limit.py --exp all
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

PATH = "/llm-anthropic/v1/messages"
FILLER = ("You are an expert assistant. Consider this reference material "
          "carefully before answering any question. ")
TOKENS_PER_FILLER = 15
_MARKER_TOKENS = 6

# Seconds between levels. The breaker trips for 60s; anything less than ~120s
# lets a tripped level poison the next one (measured: an entire 144-request
# level came back 100% 503 purely as residue).
COOLDOWN = 130
TIMEOUT = 300


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
    """Filler of ~target_tokens, unique per call so the cache never helps."""
    per_seg = TOKENS_PER_FILLER + _MARKER_TOKENS
    n = max(1, target_tokens // per_seg)
    return "".join(f"[{nonce}#{i}] {FILLER}" for i in range(n)).strip()


def one_call(url, headers, prompt_tokens, max_tokens, timeout, *,
             force_long=False) -> dict:
    nonce = uuid.uuid4().hex[:8]
    if force_long:
        user = ("Write a detailed numbered list of 60 distinct facts about the "
                "ocean. Each item must be at least 25 words. Do not stop early.")
    else:
        user = "Reply with exactly one short sentence."
    body: dict = {"model": os.environ.get("TF_MODEL", "claude-opus-4-8"),
                  "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": user}]}
    if prompt_tokens > 0:
        # No cache_control: we want every request to be a genuine cold read.
        body["system"] = [{"type": "text",
                           "text": build_prefix(prompt_tokens, nonce)}]

    t0 = time.perf_counter()
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            dt = time.perf_counter() - t0
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                d = {}
            u = d.get("usage", {}) or {}
            inp = int(u.get("input_tokens", 0) or 0)
            cr = int(u.get("cache_read_input_tokens", 0) or 0)
            cw = int(u.get("cache_creation_input_tokens", 0) or 0)
            out = int(u.get("output_tokens", 0) or 0)
            cu = d.get("copilot_usage") or {}
            return {"status": 200, "sec": dt,
                    "prompt": inp + cr + cw, "output": out,
                    "total": inp + cr + cw + out,
                    "nano": int(cu.get("total_nano_aiu", 0) or 0)}
    except urllib.error.HTTPError as e:
        dt = time.perf_counter() - t0
        try:
            txt = e.read()[:200].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            txt = ""
        return {"status": e.code, "sec": dt, "prompt": 0, "output": 0,
                "total": 0, "nano": 0, "err": txt,
                "hdrs": {k.lower(): v for k, v in (e.headers or {}).items()}}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "sec": time.perf_counter() - t0, "prompt": 0,
                "output": 0, "total": 0, "nano": 0,
                "err": f"{type(e).__name__}: {e}"}


def run_level(url, headers, *, conc, n, prompt_tokens, max_tokens,
              force_long=False) -> dict:
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        res = list(ex.map(
            lambda _: one_call(url, headers, prompt_tokens, max_tokens,
                               TIMEOUT, force_long=force_long),
            range(n)))
    wall = time.perf_counter() - t0
    ok = [r for r in res if r["status"] == 200]
    lat = sorted(r["sec"] for r in ok)
    tot = sum(r["total"] for r in ok)
    return {
        "n": n, "conc": conc, "wall": wall, "ok": len(ok),
        "c429": sum(1 for r in res if r["status"] == 429),
        "c503": sum(1 for r in res if r["status"] == 503),
        "other": sum(1 for r in res if r["status"] not in (200, 429, 503)),
        "prompt_avg": (sum(r["prompt"] for r in ok) / len(ok)) if ok else 0,
        "out_avg": (sum(r["output"] for r in ok) / len(ok)) if ok else 0,
        "tpm": tot / wall * 60 if wall else 0,
        "in_tpm": sum(r["prompt"] for r in ok) / wall * 60 if wall else 0,
        "out_tpm": sum(r["output"] for r in ok) / wall * 60 if wall else 0,
        "rps": len(ok) / wall if wall else 0,
        "usd": sum(r["nano"] for r in ok) / 1e11,
        "p50": statistics.median(lat) if lat else 0,
        "p95": (lat[int(len(lat) * 0.95)] if len(lat) > 1 else (lat[0] if lat else 0)),
        "err": next((r.get("err", "") for r in res if r.get("err")), ""),
        "hdrs": next((r.get("hdrs") for r in res if r.get("hdrs")), None),
    }


HDR = (f"{'lvl':>6} {'conc':>5} {'ok':>4} {'429':>4} {'503':>4} {'err':>4} "
       f"{'RPS':>6} {'in/req':>7} {'TPM(tot)':>10} {'TPM(in)':>10} "
       f"{'p95':>6} {'USD':>7}  verdict")


def show(label, r) -> None:
    if r["c503"]:
        verdict = "CONTAMINATED (503 - breaker)"
    elif r["c429"]:
        verdict = f"*** 429 x{r['c429']} <-- LIMIT ***"
    elif r["other"]:
        verdict = f"errors x{r['other']}"
    else:
        verdict = "clean"
    print(f"{label:>6} {r['conc']:>5} {r['ok']:>4} {r['c429']:>4} {r['c503']:>4} "
          f"{r['other']:>4} {r['rps']:>6.2f} {r['prompt_avg']:>7.0f} "
          f"{r['tpm']:>10,.0f} {r['in_tpm']:>10,.0f} {r['p95']:>6.2f} "
          f"{r['usd']:>7.4f}  {verdict}")
    if r["err"]:
        print(f"        err: {r['err'][:120]}")


def probe_429(url, headers) -> None:
    """Cheapest possible experiment: force a 429 and read what upstream says.

    If the response carries retry-after or names the limit type, that answers the
    question directly and the expensive sweeps can be targeted rather than blind.
    """
    print("=" * 100)
    print("PROBE — force a 429 and dump its headers + body verbatim")
    print("=" * 100)
    print("Firing 120 concurrent tiny requests (known to be over the cliff)...")
    r = run_level(url, headers, conc=120, n=240, prompt_tokens=0, max_tokens=16)
    print(f"  ok={r['ok']} 429={r['c429']} 503={r['c503']} other={r['other']}")
    if r["hdrs"]:
        print("\n  --- 429 RESPONSE HEADERS ---")
        for k, v in sorted(r["hdrs"].items()):
            mark = ""
            if any(w in k for w in ("retry", "limit", "remain", "reset",
                                    "quota", "ratelimit")):
                mark = "   <<< RATE-LIMIT SIGNAL"
            print(f"    {k}: {v[:100]}{mark}")
    else:
        print("  (no 429 captured — the level stayed clean or only 503'd)")
    if r["err"]:
        print(f"\n  --- ERROR BODY ---\n    {r['err'][:300]}")


def exp_a(url, headers) -> None:
    """TPM ceiling: concurrency pinned low, prompt size scaled up."""
    print("\n" + "=" * 100)
    print("EXPERIMENT A — TPM ceiling (concurrency FIXED at 24, prompt scaled)")
    print("=" * 100)
    print("If a TPM gate exists it must show up here; concurrency is at 1/3 of")
    print("the known safe line (72), so the concurrency gate is out of play.\n")
    print(HDR)
    print("-" * len(HDR))
    rows = []
    for i, ptok in enumerate([3_000, 10_000, 30_000, 60_000]):
        r = run_level(url, headers, conc=24, n=48, prompt_tokens=ptok,
                      max_tokens=200)
        rows.append((ptok, r))
        show(f"{ptok // 1000}K", r)
        if i < 3:
            time.sleep(COOLDOWN)
    clean = [(p, r) for p, r in rows if not r["c429"] and not r["c503"]]
    hit = [(p, r) for p, r in rows if r["c429"]]
    print("\n  VERDICT:")
    if hit:
        p, r = hit[0]
        print(f"    TPM GATE FOUND — first 429 at prompt={p:,} tok, "
              f"TPM(in)={r['in_tpm']:,.0f}, TPM(total)={r['tpm']:,.0f}")
    elif clean:
        p, r = max(clean, key=lambda x: x[1]["tpm"])
        print(f"    NO TPM GATE — clean at {r['tpm']:,.0f} TPM "
              f"(prompt={p:,} tok/req, {r['conc']} concurrency)")
        print("    -> token volume alone does not trigger upstream 429")


def exp_b(url, headers) -> None:
    """Concurrency ceiling with token volume minimised."""
    print("\n" + "=" * 100)
    print("EXPERIMENT B — concurrency ceiling (tokens MINIMAL: ~10 in, 16 out)")
    print("=" * 100)
    print("Known: with a 3000-tok prompt, 48 was clean and 72 threw 12x429.")
    print("If the gate is pure concurrency, tiny tokens should tip at the SAME")
    print("point. If it tips later, token volume was contributing.\n")
    print(HDR)
    print("-" * len(HDR))
    levels = [48, 72, 96, 120, 144]
    for i, c in enumerate(levels):
        r = run_level(url, headers, conc=c, n=c * 3, prompt_tokens=0,
                      max_tokens=16)
        show(str(c), r)
        if r["c429"]:
            print(f"\n  VERDICT: first 429 at concurrency {c} "
                  f"(with only {r['prompt_avg']:.0f} prompt tok/req, "
                  f"TPM={r['tpm']:,.0f})")
            print("    -> compare against the 3000-tok result: same point means")
            print("       pure concurrency gate; later means tokens contribute")
            break
        if i < len(levels) - 1:
            time.sleep(COOLDOWN)
    else:
        print("\n  VERDICT: no 429 through concurrency 144 with minimal tokens")


def exp_c(url, headers) -> None:
    """Are input and output metered differently?"""
    print("\n" + "=" * 100)
    print("EXPERIMENT C — input-heavy vs output-heavy at equal concurrency")
    print("=" * 100)
    print("Same concurrency (24), load shifted between the input and output")
    print("side. Different tipping points would mean separate metering.\n")
    print(HDR)
    print("-" * len(HDR))
    r_in = run_level(url, headers, conc=24, n=48, prompt_tokens=30_000,
                     max_tokens=200)
    show("IN", r_in)
    time.sleep(COOLDOWN)
    r_out = run_level(url, headers, conc=24, n=48, prompt_tokens=0,
                      max_tokens=8000, force_long=True)
    show("OUT", r_out)
    print("\n  VERDICT:")
    print(f"    input-heavy : in={r_in['in_tpm']:,.0f} out={r_in['out_tpm']:,.0f} "
          f"TPM 429={r_in['c429']}")
    print(f"    output-heavy: in={r_out['in_tpm']:,.0f} out={r_out['out_tpm']:,.0f} "
          f"TPM 429={r_out['c429']}")
    if bool(r_in["c429"]) != bool(r_out["c429"]):
        print("    -> ASYMMETRIC: one side tips and the other doesn't; input and")
        print("       output are metered differently")
    else:
        print("    -> symmetric at this level (both clean or both tipped)")


def main() -> int:
    global COOLDOWN
    load_dotenv_if_present()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", action="store_true",
                    help="just force a 429 and dump its headers (near-zero cost)")
    ap.add_argument("--exp", choices=("a", "b", "c", "all"),
                    help="which experiment to run")
    ap.add_argument("--cooldown", type=int, default=COOLDOWN)
    args = ap.parse_args()

    gw = require("TF_GATEWAY_URL").rstrip("/")
    vk = require("TF_VIRTUAL_KEY")
    url = gw + PATH
    headers = {"x-api-key": vk, "Content-Type": "application/json",
               "anthropic-version": "2023-06-01"}
    COOLDOWN = args.cooldown

    print(f"target : {url}")
    print(f"model  : {os.environ.get('TF_MODEL', 'claude-opus-4-8')}")
    print(f"cooldown between levels: {COOLDOWN}s\n")

    if args.probe:
        probe_429(url, headers)
        return 0
    if not args.exp:
        ap.error("pass --probe or --exp {a,b,c,all}")
    if args.exp in ("b", "all"):
        exp_b(url, headers)
        if args.exp == "all":
            time.sleep(COOLDOWN)
    if args.exp in ("a", "all"):
        exp_a(url, headers)
        if args.exp == "all":
            time.sleep(COOLDOWN)
    if args.exp in ("c", "all"):
        exp_c(url, headers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
