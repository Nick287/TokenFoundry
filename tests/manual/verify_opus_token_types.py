#!/usr/bin/env python3
"""Token Foundry — does our metering capture every token type on claude-opus-4.8?

Specifically checks the four things that can each silently go missing:

    input / output    the basics
    cache             cache_creation_input_tokens + cache_read_input_tokens
    thinking          extended-thinking tokens (billed as OUTPUT by Anthropic —
                      there is no separate usage field, so the check is that
                      output_tokens visibly jumps when thinking is on)

...across BOTH stream and non-stream, because streaming is where usage
traditionally gets lost (SSE splits usage across events; anthropic reports
input on message_start and output on message_delta).

PHASE 1 calls the gateway and records the upstream usage verbatim — that is the
ground truth, it is what the provider bills.
PHASE 2 (optional, needs az login + TF_LAW_CUSTOMER_ID) re-reads the same calls
from ApiManagementGatewayLlmLog by RequestId and from App Insights customMetrics
by model, and diffs both against the truth.

Run PHASE 1 alone with --no-telemetry to skip the Azure queries entirely.

NO SECRETS IN THIS FILE. Configure via env or a local .env:
    TF_GATEWAY_URL       https://<apim>.azure-api.net
    TF_VIRTUAL_KEY       an APIM subscription key
Optional (PHASE 2):
    TF_LAW_CUSTOMER_ID   Log Analytics workspace GUID -> LlmLog
    TF_APP_INSIGHTS_ID   App Insights resource id     -> customMetrics
    TF_INGEST_WAIT_SECS  default 300

Usage:
    python tests/manual/verify_opus_token_types.py --no-telemetry
    python tests/manual/verify_opus_token_types.py --wait 360
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

MODEL = "claude-opus-4.8"
PATH = "/llm-anthropic/v1/messages"

# Cache needs a prefix above the provider floor (~1991 tok for claude) or
# cache_creation stays 0 and the "cache" column proves nothing. The prefix also
# needs a per-RUN nonce woven through it: a byte-identical prefix left warm by
# an earlier run reports cache_READ on what should be the cache_WRITE call, so
# cache_creation_input_tokens would read 0 and look like a bug.
FILLER = ("You are an expert assistant. Consider this reference material "
          "carefully before answering any question. ")
PREFIX_REPEATS = 220  # ~3000 tokens

# Thinking on claude-opus-4.8 uses the NEWER API shape. The older
# {"thinking": {"type": "enabled", "budget_tokens": N}} is rejected with
#   "thinking.type.enabled is not supported for this model. Use
#    thinking.type.adaptive and output_config.effort"
# "adaptive" also means the model DECIDES whether to think, so the prompt has to
# actually warrant it — an easy question returns thinking_tokens=0 legitimately.
# Opus 4.8 reports thinking in usage.output_tokens_details.thinking_tokens; it
# is still BILLED inside output_tokens but is individually observable.
THINK_EFFORT = "high"
THINK_PROMPT = (
    "A farmer must cross a river with a wolf, a goat and a cabbage. The boat "
    "holds the farmer plus one item. The wolf eats the goat if left alone with "
    "it; the goat eats the cabbage if left alone with it. Now suppose there are "
    "TWO goats and the boat holds two items. Enumerate a shortest valid "
    "crossing schedule and prove it is minimal."
)
EASY_PROMPT = "In one sentence: why is the sea salty?"
MAX_TOKENS_THINK = 4000
MAX_TOKENS_PLAIN = 300


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


def sse_usage(raw: bytes) -> tuple[dict, str]:
    """Merge usage across SSE events and pull the message id.

    Anthropic splits usage: input_tokens + cache_* land on message_start,
    output_tokens on message_delta. Taking only the last event loses the input
    side, so events are merged.
    """
    usage: dict = {}
    rid = ""
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if not rid:
            rid = msg.get("id") or obj.get("id") or ""
        for cand in (msg.get("usage"), obj.get("usage")):
            if cand:
                usage.update(cand)
    return usage, rid


def call(gw: str, vk: str, *, stream: bool, cached: bool, thinking: bool,
         nonce: str = "") -> dict:
    url = gw.rstrip("/") + PATH
    headers = {"x-api-key": vk, "Content-Type": "application/json",
               "anthropic-version": "2023-06-01"}
    body: dict = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS_THINK if thinking else MAX_TOKENS_PLAIN,
        "messages": [{"role": "user",
                      "content": THINK_PROMPT if thinking else EASY_PROMPT}],
    }
    if cached:
        # Explicit breakpoint — anthropic caching is opt-in per block.
        prefix = f"[{nonce}] " + FILLER * PREFIX_REPEATS
        body["system"] = [{"type": "text", "text": prefix,
                           "cache_control": {"type": "ephemeral"}}]
    if thinking:
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": THINK_EFFORT}
    if stream:
        body["stream"] = True

    t0 = time.perf_counter()
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        return {"status": e.code, "err": e.read()[:300].decode("utf-8", "replace"),
                "sec": time.perf_counter() - t0}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "err": f"{type(e).__name__}: {e}",
                "sec": time.perf_counter() - t0}

    if stream:
        u, rid = sse_usage(raw)
        think_text = raw.count(b'"thinking"')
    else:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = {}
        u = obj.get("usage", {}) or {}
        rid = obj.get("id", "")
        think_text = sum(1 for b in (obj.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "thinking")

    inp = int(u.get("input_tokens", 0) or 0)
    cw = int(u.get("cache_creation_input_tokens", 0) or 0)
    cr = int(u.get("cache_read_input_tokens", 0) or 0)
    out = int(u.get("output_tokens", 0) or 0)
    # Opus 4.8 breaks thinking out here. It is INCLUDED in output_tokens (so
    # don't add it to the total), but it is separately observable.
    det = u.get("output_tokens_details") or {}
    think_tok = int(det.get("thinking_tokens", 0) or 0) if isinstance(det, dict) else 0
    return {
        "status": status, "sec": time.perf_counter() - t0, "rid": rid,
        "input": inp, "cache_write": cw, "cache_read": cr, "output": out,
        "think_tokens": think_tok,
        # billed prompt, comparable to openai's prompt_tokens
        "prompt": inp + cw + cr, "total": inp + cw + cr + out,
        "think_blocks": think_text, "raw_usage": u, "err": "",
    }


# --------------------------------------------------------------------------- #
# PHASE 2 — read the same calls back from the two metering paths               #
# --------------------------------------------------------------------------- #


def _client():
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient
    return LogsQueryClient(credential=DefaultAzureCredential())


def llmlog_by_rid(ws: str, rid: str, minutes: int = 60) -> dict | None:
    kql = (f'ApiManagementGatewayLlmLog | where TimeGenerated > ago({minutes}m) '
           f'| where RequestId == "{rid}" | where TotalTokens > 0 '
           "| project PromptTokens, CompletionTokens, TotalTokens, IsStreamCompletion "
           "| take 1")
    try:
        r = _client().query_workspace(
            ws, kql, timespan=datetime.timedelta(minutes=minutes))
    except Exception as e:  # noqa: BLE001
        print(f"    (LlmLog query error: {str(e)[:120]})")
        return None
    for t in r.tables:
        for row in t.rows:
            d = dict(zip(list(t.columns), row, strict=False))
            return {"prompt": int(d["PromptTokens"] or 0),
                    "completion": int(d["CompletionTokens"] or 0),
                    "total": int(d["TotalTokens"] or 0),
                    "stream": d.get("IsStreamCompletion")}
    return None


def custommetrics(ai_id: str, model: str, minutes: int = 60) -> dict | None:
    kql = ("customMetrics "
           f'| extend m = tostring(customDimensions["model"]) | where m == "{model}" '
           "| summarize v = sum(valueSum), calls = sum(valueCount) by name")
    try:
        r = _client().query_resource(
            ai_id, kql, timespan=datetime.timedelta(minutes=minutes))
    except Exception as e:  # noqa: BLE001
        print(f"    (customMetrics query error: {str(e)[:120]})")
        return None
    out: dict = {}
    for t in r.tables:
        for row in t.rows:
            d = dict(zip(list(t.columns), row, strict=False))
            out[d["name"]] = (int(d["v"] or 0), int(d["calls"] or 0))
    return out or None


def main() -> int:
    load_dotenv_if_present()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-telemetry", action="store_true",
                    help="only call the gateway; skip the Azure read-back")
    ap.add_argument("--wait", type=int,
                    default=int(os.environ.get("TF_INGEST_WAIT_SECS", "300")))
    args = ap.parse_args()

    gw = require("TF_GATEWAY_URL")
    vk = require("TF_VIRTUAL_KEY")

    # 2 modes x 3 feature combos. The cache pair runs twice in a row so the
    # second call can READ what the first one WROTE.
    plan = [
        ("plain",     dict(cached=False, thinking=False)),
        ("cache-w",   dict(cached=True,  thinking=False)),
        ("cache-r",   dict(cached=True,  thinking=False)),
        ("thinking",  dict(cached=False, thinking=True)),
    ]

    print("=" * 100)
    print(f"PHASE 1 — upstream ground truth   model={MODEL}")
    print("=" * 100)
    hdr = (f"{'case':<10} {'mode':<11} {'HTTP':>4} {'input':>7} {'c_write':>8} "
           f"{'c_read':>7} {'output':>7} {'think_t':>8} {'total':>7} "
           f"{'blk':>4} {'sec':>6}")
    print(hdr)
    print("-" * len(hdr))

    recs = []
    for stream in (False, True):
        # One nonce per mode: cache-w writes this prefix, cache-r then reads it.
        # A fresh nonce per RUN keeps an earlier run from pre-warming the write.
        nonce = uuid.uuid4().hex[:8]
        for case, kw in plan:
            r = call(gw, vk, stream=stream, nonce=nonce, **kw)
            r.update(case=case, mode="stream" if stream else "non-stream")
            recs.append(r)
            if r.get("err"):
                print(f"{case:<10} {r['mode']:<11} {r['status']:>4}  ERR "
                      f"{r['err'][:60]}")
                continue
            print(f"{case:<10} {r['mode']:<11} {r['status']:>4} {r['input']:>7} "
                  f"{r['cache_write']:>8} {r['cache_read']:>7} {r['output']:>7} "
                  f"{r['think_tokens']:>8} {r['total']:>7} "
                  f"{r['think_blocks']:>4} {r['sec']:>6.1f}")

    print("\n" + "=" * 100)
    print("PHASE 1 VERDICT — is each token type observable upstream?")
    print("=" * 100)
    ok = [r for r in recs if not r.get("err")]
    for mode in ("non-stream", "stream"):
        m = [r for r in ok if r["mode"] == mode]
        if not m:
            print(f"  {mode:<11} no successful calls")
            continue
        has_in = all(r["input"] > 0 for r in m)
        has_out = all(r["output"] > 0 for r in m)
        cw = max((r["cache_write"] for r in m), default=0)
        cr = max((r["cache_read"] for r in m), default=0)
        th = [r for r in m if r["case"] == "thinking"]
        pl = [r for r in m if r["case"] == "plain"]
        think_tok = max((r["think_tokens"] for r in m), default=0)
        think_txt = f"OK {think_tok}" if think_tok else "MISSING"
        print(f"  {mode:<11} input={'OK' if has_in else 'MISSING'}  "
              f"output={'OK' if has_out else 'MISSING'}  "
              f"cache_write={'OK ' + str(cw) if cw else 'MISSING'}  "
              f"cache_read={'OK ' + str(cr) if cr else 'MISSING'}  "
              f"thinking={think_txt}")
        if th and pl:
            print(f"{'':<13}  (output {pl[0]['output']} -> {th[0]['output']} "
                  f"with thinking on)")

    print("\n  Note: thinking tokens are BILLED INSIDE output_tokens, but "
          "claude-opus-4.8")
    print("  reports them separately in usage.output_tokens_details."
          "thinking_tokens.")

    if args.no_telemetry:
        print("\n(--no-telemetry: skipping the Azure read-back)")
        return 0

    ws = os.environ.get("TF_LAW_CUSTOMER_ID")
    ai = os.environ.get("TF_APP_INSIGHTS_ID")
    if not ws and not ai:
        print("\nPHASE 2 skipped: set TF_LAW_CUSTOMER_ID / TF_APP_INSIGHTS_ID "
              "(and run az login) to read the metering paths back.")
        return 0

    print(f"\nPHASE 2 — waiting {args.wait}s for ingestion")
    for left in range(args.wait, 0, -30):
        print(f"  ...{left}s")
        time.sleep(min(30, left))

    if ws:
        print("\n" + "=" * 100)
        print("LlmLog (per-call, matched by response id)")
        print("=" * 100)
        h2 = (f"{'case':<10} {'mode':<11} {'field':<11} {'truth':>8} "
              f"{'LlmLog':>8} {'verdict':>9}")
        print(h2)
        print("-" * len(h2))
        for r in ok:
            if not r.get("rid"):
                print(f"{r['case']:<10} {r['mode']:<11} (no response id)")
                continue
            lg = llmlog_by_rid(ws, r["rid"])
            if lg is None:
                print(f"{r['case']:<10} {r['mode']:<11} (not found — lag or dropped)")
                continue
            for f, truth in (("prompt", r["prompt"]),
                             ("completion", r["output"]),
                             ("total", r["total"])):
                good = truth == lg[f]
                print(f"{r['case']:<10} {r['mode']:<11} {f:<11} {truth:>8} "
                      f"{lg[f]:>8} {'PASS' if good else 'FAIL <<':>9}")

    if ai:
        print("\n" + "=" * 100)
        print("customMetrics (aggregated by model — the only path with cached)")
        print("=" * 100)
        cm = custommetrics(ai, MODEL)
        if not cm:
            print("  (none yet)")
        else:
            t_prompt = sum(r["prompt"] for r in ok)
            t_out = sum(r["output"] for r in ok)
            t_cached = sum(r["cache_read"] for r in ok)
            for name, (v, calls) in sorted(cm.items()):
                print(f"  {name:<28} sum={v:<10} calls={calls}")
            print(f"\n  upstream truth for this run: prompt={t_prompt} "
                  f"completion={t_out} cache_read={t_cached}")
            print("  (customMetrics aggregates ALL calls in the window, so it is a "
                  "superset unless the model is otherwise idle)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
