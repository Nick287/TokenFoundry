#!/usr/bin/env python3
"""Token Foundry — ramp load test: find a hub/Copilot account's RPS + TPM ceiling.

Ramps concurrency (1 -> 2 -> 4 -> 8 -> ...) and, for each level, fires a fixed
number of chat completions through the APIM gateway, measuring:

    * success / 429 (upstream throttled) / 503 (circuit breaker open) / other
    * achieved RPS  (successful requests / wall-clock of the level)
    * achieved TPM  (total tokens of successful calls, extrapolated per minute)
    * latency p50 / p95

It stops early once a level's error rate crosses a threshold — that's the
ceiling. Use an UNLIMITED virtual key (no tokens_per_minute) so you measure the
UPSTREAM (Copilot subscription) limit rather than our own llm-token-limit policy.

WHAT THE STATUS CODES MEAN HERE (4 rate-limit layers, outer to inner):
  429 from our llm-token-limit  -> the KEY's per-minute budget (avoid: use an
                                    unlimited key)
  429 from upstream             -> the Copilot account is out of TPM  <-- the ceiling
  503                           -> APIM circuit breaker tripped on a prior 429/5xx
                                    and this pool has no other hub to fail over to;
                                    it clears after ~60s (see docs §5.3)
  Slow/timeout                  -> the single hub Container App is saturated

NO SECRETS IN THIS FILE. Configure via env:
    TF_GATEWAY_URL   https://<apim>.azure-api.net
    TF_VIRTUAL_KEY   an APIM subscription key (prefer an UNLIMITED one)
Optional:
    TF_MODEL         default gpt-4o-mini
    TF_PATH          default /llm-openai/v1/chat/completions
    TF_AUTH_HEADER   default api-key   (anthropic: x-api-key)

Usage:
    python tests/manual/load_test_ramp.py
    python tests/manual/load_test_ramp.py --levels 1,2,4,8,16 --per-level 20
    python tests/manual/load_test_ramp.py --max-tokens 200   # bigger responses => TPM pressure
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
from concurrent.futures import ThreadPoolExecutor

PROMPT = "Write a short paragraph about the ocean."

# --------------------------------------------------------------------------- #
# Tunables — edit here for a different default run; every one is still
# overridable on the command line (e.g. --levels 24,48 --per-level 48).
# --------------------------------------------------------------------------- #

# Concurrency levels to ramp through, in order.
LEVELS = "1,2,4,8,16"
# Requests fired per level. Small values are noisy: 12 requests gave ±20% run-to-
# run variance on a12; 48 is much steadier when you care about the exact ceiling.
PER_LEVEL = 12
# max_tokens per request — raise it to push TPM (token) pressure rather than RPS.
MAX_TOKENS = 60
# Per-request HTTP timeout (seconds). Keep well above p95 or you'll count
# healthy-but-queued requests as failures.
TIMEOUT = 90
# Seconds to idle between levels. Gives a tripped APIM circuit breaker (60s trip)
# room to reset so the next level starts clean.
COOLDOWN = 20
# Stop ramping once a level's error rate exceeds this — that level is the ceiling.
STOP_ERROR_RATE = 0.5


def load_dotenv_if_present() -> None:
    """Load a local .env (KEY=VALUE lines) into os.environ without a dependency.

    Same helper as verify_token_vs_diagnostic.py, so this script can be run bare
    (`python tests/manual/load_test_ramp.py`) when the repo root .env carries
    TF_GATEWAY_URL / TF_VIRTUAL_KEY.
    """
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
        sys.exit(f"ERROR: env var {name} is required (see docstring). "
                 f"Set it inline or put it in the repo-root .env")
    return v


def one_call(url: str, headers: dict, body: dict, timeout: int) -> dict:
    t0 = time.perf_counter()
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            dt = time.perf_counter() - t0
            try:
                u = json.loads(raw).get("usage", {}) or {}
            except json.JSONDecodeError:
                u = {}
            tot = int(u.get("total_tokens", 0) or 0)
            if not tot:  # anthropic shape
                tot = int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0)
            return {"status": r.status, "sec": dt, "tokens": tot}
    except urllib.error.HTTPError as e:
        dt = time.perf_counter() - t0
        body_txt = ""
        try:
            body_txt = e.read()[:120].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return {"status": e.code, "sec": dt, "tokens": 0, "err": body_txt}
    except Exception as e:  # noqa: BLE001 — timeouts / conn resets are data too
        return {"status": 0, "sec": time.perf_counter() - t0, "tokens": 0,
                "err": type(e).__name__}


def one_call_stream(url: str, headers: dict, body: dict, timeout: int) -> dict:
    """Same contract as one_call, but over SSE.

    Streaming is the path most likely to lose a usage event: the hub emits from
    inside an async generator's `finally`, so a client disconnect or a truncated
    stream exercises code that a buffered request never touches. Every load test
    in this repo was non-streaming, which left that path unmeasured under load.

    `ttft` (time to first token) is recorded separately because the streaming
    contract is about latency-to-first-byte, and a stream can start promptly and
    still finish slowly — averaging that into one number would hide both.
    """
    t0 = time.perf_counter()
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ttft = None
    tot = 0
    chunks = 0
    upstream_err: dict | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:  # HTTPResponse iterates line-by-line
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                if ttft is None:
                    ttft = time.perf_counter() - t0
                chunks += 1
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # An in-band error event. The hub emits one when upstream
                # refuses AFTER the 200 and its headers have gone out, which on
                # a streaming call is the only signal left. It is a well-formed
                # SSE event, so counting events alone now reports a refused call
                # as a success — dev-18 measured 288/288 "ok" at concurrency 96
                # while Cosmos recorded 34 of them as upstream 429s.
                if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
                    upstream_err = obj["error"]
                    continue
                # Usage rides a late chunk; on Responses-shaped streams it is
                # nested under .response. Last one wins.
                for cand in (obj, obj.get("response") if isinstance(obj, dict) else None):
                    if not isinstance(cand, dict):
                        continue
                    u = cand.get("usage")
                    if isinstance(u, dict):
                        t = int(u.get("total_tokens", 0) or 0)
                        if not t:
                            t = (int(u.get("input_tokens", 0) or 0)
                                 + int(u.get("output_tokens", 0) or 0))
                        if t:
                            tot = t
            dt = time.perf_counter() - t0
            # Two ways a 200 is not a success on this path:
            #   * no SSE event at all — a broken stream, which would otherwise
            #     inflate the RPS column as a very fast success
            #   * an in-band error event — upstream refused after the headers
            #     were already committed, so the status line cannot say so
            # The second is reported under the UPSTREAM code, which is what the
            # billing record carries too, so the two sides line up.
            if upstream_err is not None:
                code = upstream_err.get("code")
                return {"status": int(code) if isinstance(code, int) else 502,
                        "sec": dt, "tokens": tot, "ttft": ttft, "chunks": chunks,
                        "err": f"upstream {code}: "
                               f"{str(upstream_err.get('message'))[:80]}"}
            status = r.status if chunks else 0
            return {"status": status, "sec": dt, "tokens": tot,
                    "ttft": ttft, "chunks": chunks,
                    **({} if chunks else {"err": "no SSE events"})}
    except urllib.error.HTTPError as e:
        dt = time.perf_counter() - t0
        body_txt = ""
        try:
            body_txt = e.read()[:120].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return {"status": e.code, "sec": dt, "tokens": 0, "err": body_txt}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "sec": time.perf_counter() - t0, "tokens": 0,
                "err": type(e).__name__}


def run_level(url, headers, body, conc: int, n: int, timeout: int,
              stream: bool = False) -> dict:
    caller = one_call_stream if stream else one_call
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        results = list(ex.map(lambda _: caller(url, headers, body, timeout), range(n)))
    wall = time.perf_counter() - t0

    ok = [r for r in results if r["status"] == 200]
    c429 = [r for r in results if r["status"] == 429]
    c503 = [r for r in results if r["status"] == 503]
    other = [r for r in results if r["status"] not in (200, 429, 503)]
    lat = sorted(r["sec"] for r in ok)
    ttfts = sorted(r["ttft"] for r in ok if r.get("ttft") is not None)
    tokens = sum(r["tokens"] for r in ok)
    return {
        "conc": conc, "n": n, "wall": wall,
        "ok": len(ok), "c429": len(c429), "c503": len(c503), "other": len(other),
        "rps": len(ok) / wall if wall else 0,
        "tpm": tokens / wall * 60 if wall else 0,
        "tokens": tokens,
        "p50": statistics.median(lat) if lat else 0,
        "p95": (lat[int(len(lat) * 0.95)] if len(lat) > 1 else (lat[0] if lat else 0)),
        "ttft50": statistics.median(ttfts) if ttfts else 0,
        # A level nobody actually served: everything 503'd and it took under a
        # second per 100 requests, i.e. they were rejected at the gateway rather
        # than sent upstream. Distinguishing this from a genuine collapse
        # matters — the numbers look identical in the table.
        "shed": len(c503) == n and n > 0 and wall < max(2.0, n / 100),
        "sample_err": next((r.get("err", "") for r in results if r.get("err")), ""),
    }


def main() -> int:
    load_dotenv_if_present()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--levels", default=LEVELS,
                    help=f"comma-separated concurrency levels (default {LEVELS})")
    ap.add_argument("--per-level", type=int, default=PER_LEVEL,
                    help=f"requests per level (default {PER_LEVEL})")
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                    help=f"max_tokens per request (default {MAX_TOKENS})")
    ap.add_argument("--timeout", type=int, default=TIMEOUT,
                    help=f"per-request timeout seconds (default {TIMEOUT})")
    ap.add_argument("--cooldown", type=int, default=COOLDOWN,
                    help=f"seconds between levels; lets a tripped breaker reset "
                         f"(default {COOLDOWN})")
    ap.add_argument("--precool", type=int, default=0,
                    help="seconds to wait BEFORE the first level. --cooldown "
                         "only separates levels within one invocation, so when "
                         "each level is its own run (the per-level discipline "
                         "in CAPACITY.zh.md 6.2) the first one inherits "
                         "whatever state the previous run left. Use 150 there.")
    ap.add_argument("--stop-error-rate", type=float, default=STOP_ERROR_RATE,
                    help=f"stop ramping once error rate exceeds this, 0-1 "
                         f"(default {STOP_ERROR_RATE})")
    ap.add_argument("--stream", action="store_true",
                    help="drive SSE instead of buffered responses. The hub emits "
                         "its usage event from inside a generator's finally on "
                         "this path, so it is the one most likely to lose events "
                         "under load — and the one no load test covered.")
    args = ap.parse_args()

    gw = require("TF_GATEWAY_URL").rstrip("/")
    vk = require("TF_VIRTUAL_KEY")
    model = os.environ.get("TF_MODEL", "gpt-4o-mini")
    path = os.environ.get("TF_PATH", "/llm-openai/v1/chat/completions")
    auth = os.environ.get("TF_AUTH_HEADER", "api-key")

    url = gw + path
    headers = {auth: vk, "Content-Type": "application/json"}
    if "messages" in path:  # anthropic native
        headers["anthropic-version"] = "2023-06-01"
    body = {"model": model, "max_tokens": args.max_tokens,
            "messages": [{"role": "user", "content": PROMPT}]}
    if args.stream:
        body["stream"] = True
        headers["Accept"] = "text/event-stream"
        if "messages" not in path:
            # Chat Completions omits usage on streams unless asked. Without it
            # the TPM column reads 0 and the run looks free.
            body["stream_options"] = {"include_usage": True}

    print(f"target : {url}")
    print(f"model  : {model}   max_tokens={args.max_tokens}   "
          f"mode={'STREAM' if args.stream else 'direct'}")
    print(f"levels : {args.levels}   requests/level={args.per_level}\n")
    ttft_col = f" {'ttft s':>7}" if args.stream else ""
    hdr = (f"{'conc':>4} {'ok':>4} {'429':>4} {'503':>4} {'err':>4} "
           f"{'RPS':>7} {'TPM':>9} {'p50 s':>7} {'p95 s':>7}{ttft_col}")
    print(hdr)
    print("-" * len(hdr))

    if args.precool:
        print(f"precool {args.precool}s (let any breaker from a PRIOR run reset)")
        time.sleep(args.precool)

    rows = []
    for lvl in [int(x) for x in args.levels.split(",")]:
        r = run_level(url, headers, body, lvl, args.per_level, args.timeout,
                      stream=args.stream)
        rows.append(r)
        line = (f"{r['conc']:>4} {r['ok']:>4} {r['c429']:>4} {r['c503']:>4} "
                f"{r['other']:>4} {r['rps']:>7.2f} {r['tpm']:>9.0f} "
                f"{r['p50']:>7.2f} {r['p95']:>7.2f}")
        if args.stream:
            line += f" {r['ttft50']:>7.2f}"
        print(line)
        if r["shed"]:
            # Every request 503'd and the whole level finished in about the time
            # it takes to REJECT n requests, not serve them. That is a breaker
            # that was already open when the level started — most likely tripped
            # by a previous run — so this row says nothing about capacity at
            # this concurrency. Measured on dev-19: a streaming 48 level fired
            # with zero gap after a non-streaming 96 collapse returned 144/144
            # 503 at 0.00 RPS, and read as a total failure until the timestamps
            # showed the two runs were 33 seconds apart.
            print(f"     ^ NOT A CAPACITY RESULT: all {r['n']} shed in "
                  f"{r['wall']:.1f}s — a breaker was already open. Re-run this "
                  f"level with --precool 150.")
        if r["sample_err"]:
            print(f"     err sample: {r['sample_err'][:110]}")
        err_rate = 1 - (r["ok"] / r["n"] if r["n"] else 0)
        if err_rate > args.stop_error_rate:
            print(f"\n>>> error rate {err_rate:.0%} > {args.stop_error_rate:.0%} "
                  f"— ceiling reached, stopping ramp")
            break
        if lvl != [int(x) for x in args.levels.split(",")][-1]:
            time.sleep(args.cooldown)

    best_rps = max((r["rps"] for r in rows), default=0)
    best_tpm = max((r["tpm"] for r in rows), default=0)
    print("\n" + "=" * 60)
    print(f"PEAK sustained RPS : {best_rps:.2f} req/s")
    print(f"PEAK sustained TPM : {best_tpm:.0f} tokens/min")
    print("=" * 60)
    print("Note: 429 = upstream Copilot throttle (the real ceiling); "
          "503 = APIM circuit breaker open (single-hub pool, clears in ~60s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
