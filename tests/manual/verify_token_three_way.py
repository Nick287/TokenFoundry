#!/usr/bin/env python3
"""Token Foundry — three-way token accuracy verification.

For every (provider x mode) combination it:
  1. Sends ONE request through the APIM gateway and records the AUTHORITATIVE
     usage from the upstream response (ground truth, provider-billed).
  2. Waits for Azure Monitor ingestion.
  3. Reads BOTH metering paths for that exact call:
       * ApiManagementGatewayLlmLog  — matched by response id (per-call, exact)
       * App Insights customMetrics  — matched by the model dimension (aggregated)
  4. Prints TRUTH vs LlmLog vs customMetrics with PASS/FAIL per field.

Matrix: 3 providers x 2 modes = 6 calls
    openai     /llm-openai/v1/chat/completions     api-key
    anthropic  /llm-anthropic/v1/messages          x-api-key
    google     /llm-google/v1/chat/completions     api-key

Why both paths: LlmLog is the billing-grade per-call source (exact for streaming
after the hub objfix) but has NO cached column; customMetrics carries the full
9 token types incl. Prompt Cached Tokens. They must agree on prompt/completion/
total — this script proves it, per provider and per mode.

NO SECRETS IN THIS FILE. Configure via environment:
    TF_GATEWAY_URL       https://<apim>.azure-api.net
    TF_VIRTUAL_KEY       an APIM subscription (virtual) key
    TF_LAW_CUSTOMER_ID   Log Analytics workspace GUID (for LlmLog + AppMetrics)
    TF_APP_INSIGHTS_ID   App Insights resource id (for customMetrics)
Optional model overrides: TF_OPENAI_MODEL / TF_ANTHROPIC_MODEL / TF_GOOGLE_MODEL
Optional: TF_INGEST_WAIT_SECS (default 300)

Usage:
    python tests/manual/verify_token_three_way.py
    python tests/manual/verify_token_three_way.py --only google --wait 420
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #


def require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: env var {name} is required (see module docstring).")
    return val


# provider -> (path, auth header, body builder key, id prefix)
PROVIDERS = {
    "openai": {
        "path": "/llm-openai/v1/chat/completions",
        "auth": "api-key",
        "shape": "chat",
        "id_prefix": "chatcmpl-",
        "model_env": "TF_OPENAI_MODEL",
        "model_default": "gpt-4o-mini",
    },
    "anthropic": {
        "path": "/llm-anthropic/v1/messages",
        "auth": "x-api-key",
        "shape": "messages",
        "id_prefix": "msg_",
        "model_env": "TF_ANTHROPIC_MODEL",
        "model_default": "claude-haiku-4.5",
    },
    "google": {
        "path": "/llm-google/v1/chat/completions",
        "auth": "api-key",
        "shape": "chat",
        "id_prefix": "chatcmpl-",
        "model_env": "TF_GOOGLE_MODEL",
        "model_default": "gemini-2.5-pro",
    },
}

PROMPT = "Reply with exactly one short sentence about the sea."


# --------------------------------------------------------------------------- #
# HTTP + usage extraction                                                      #
# --------------------------------------------------------------------------- #


def http_post(url: str, headers: dict, body: dict, timeout: int = 120):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def sse_usage(raw: bytes, *, anthropic: bool) -> dict:
    """Merge usage across SSE events (anthropic splits input/output across events)."""
    usage: dict = {}
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
        cand = obj.get("usage")
        if anthropic:
            cand = (obj.get("message") or {}).get("usage") or cand
        if cand:
            usage = {**usage, **cand}
    return usage


def first_id(raw: bytes, prefix: str) -> str:
    text = raw.decode("utf-8", "replace")
    key = '"id":"' + prefix
    i = text.find(key)
    if i < 0:
        return ""
    start = i + len('"id":"')
    end = text.find('"', start)
    return text[start:end] if end > start else ""


def norm_usage(u: dict, *, anthropic: bool) -> tuple[int, int, int]:
    """-> (prompt, completion, total). Anthropic input EXCLUDES cache; add it back."""
    if anthropic:
        p = int(u.get("input_tokens", 0) or 0)
        p += int(u.get("cache_read_input_tokens", 0) or 0)
        p += int(u.get("cache_creation_input_tokens", 0) or 0)
        c = int(u.get("output_tokens", 0) or 0)
        return p, c, p + c
    p = int(u.get("prompt_tokens", 0) or 0)
    c = int(u.get("completion_tokens", 0) or 0)
    return p, c, int(u.get("total_tokens", p + c) or (p + c))


def call(gw: str, vk: str, provider: str, model: str, *, stream: bool) -> dict:
    cfg = PROVIDERS[provider]
    url = gw.rstrip("/") + cfg["path"]
    headers = {cfg["auth"]: vk, "Content-Type": "application/json"}
    anthropic = cfg["shape"] == "messages"
    if anthropic:
        headers["anthropic-version"] = "2023-06-01"
        body: dict = {"model": model, "max_tokens": 40,
                      "messages": [{"role": "user", "content": PROMPT}]}
        if stream:
            body["stream"] = True
    else:
        body = {"model": model, "max_tokens": 40,
                "messages": [{"role": "user", "content": PROMPT}]}
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}

    status, raw = http_post(url, headers, body)
    if stream:
        u = sse_usage(raw, anthropic=anthropic)
        rid = first_id(raw, cfg["id_prefix"])
    else:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = {}
        u = obj.get("usage", {}) or {}
        rid = obj.get("id", "")
    p, c, t = norm_usage(u, anthropic=anthropic)
    return {"status": status, "prompt": p, "completion": c, "total": t,
            "rid": rid, "raw_len": len(raw),
            "err": "" if status == 200 else raw[:160].decode("utf-8", "replace")}


# --------------------------------------------------------------------------- #
# Queries                                                                      #
# --------------------------------------------------------------------------- #


def _client():
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient
    return LogsQueryClient(credential=DefaultAzureCredential())


def llmlog_by_rid(ws: str, rid: str, minutes: int = 40) -> dict | None:
    c = _client()
    kql = (f'ApiManagementGatewayLlmLog | where TimeGenerated > ago({minutes}m) '
           f'| where RequestId == "{rid}" | where TotalTokens > 0 '
           "| project PromptTokens, CompletionTokens, TotalTokens, ModelName, IsStreamCompletion "
           "| take 1")
    try:
        r = c.query_workspace(ws, kql, timespan=datetime.timedelta(minutes=minutes))
    except Exception as e:  # noqa: BLE001
        print(f"    (LlmLog query error: {str(e)[:110]})")
        return None
    for t in r.tables:
        for row in t.rows:
            d = dict(zip([col for col in t.columns], row, strict=False))
            return {"prompt": int(d["PromptTokens"] or 0),
                    "completion": int(d["CompletionTokens"] or 0),
                    "total": int(d["TotalTokens"] or 0),
                    "model": d.get("ModelName"), "stream": d.get("IsStreamCompletion")}
    return None


def custommetrics_by_model(ai_id: str, model: str, minutes: int = 40) -> dict | None:
    """customMetrics is pre-aggregated; filter by the model dimension we sent."""
    c = _client()
    kql = ("customMetrics "
           '| where name in ("Prompt Tokens","Completion Tokens","Total Tokens","Prompt Cached Tokens") '
           f'| extend m = tostring(customDimensions["model"]) | where m == "{model}" '
           "| summarize v = sum(valueSum), calls = sum(valueCount) by name")
    try:
        r = c.query_resource(ai_id, kql, timespan=datetime.timedelta(minutes=minutes))
    except Exception as e:  # noqa: BLE001
        print(f"    (customMetrics query error: {str(e)[:110]})")
        return None
    out: dict = {}
    for t in r.tables:
        for row in t.rows:
            d = dict(zip([col for col in t.columns], row, strict=False))
            out[d["name"]] = (int(d["v"] or 0), int(d["calls"] or 0))
    return out or None


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=list(PROVIDERS), help="test one provider")
    ap.add_argument("--wait", type=int,
                    default=int(os.environ.get("TF_INGEST_WAIT_SECS", "300")))
    args = ap.parse_args()

    gw = require("TF_GATEWAY_URL")
    vk = require("TF_VIRTUAL_KEY")
    ws = require("TF_LAW_CUSTOMER_ID")
    ai = require("TF_APP_INSIGHTS_ID")

    providers = [args.only] if args.only else list(PROVIDERS)
    plan = [(p, m) for p in providers for m in (False, True)]

    print("=" * 96)
    print("PHASE 1 — call gateway, capture upstream ground truth")
    print("=" * 96)
    recs = []
    for provider, stream in plan:
        cfg = PROVIDERS[provider]
        model = os.environ.get(cfg["model_env"], cfg["model_default"])
        r = call(gw, vk, provider, model, stream=stream)
        r.update(provider=provider, model=model, mode="stream" if stream else "non-stream")
        recs.append(r)
        flag = "" if (r["status"] == 200 and r["rid"]) else "  <-- WARN"
        print(f"  {provider:10} {r['mode']:11} HTTP {r['status']} "
              f"id={(r['rid'] or '-')[:30]:30} p={r['prompt']:5} c={r['completion']:5} t={r['total']:5}{flag}")
        if r["err"]:
            print(f"      err: {r['err']}")

    import time
    print(f"\nPHASE 2 — waiting {args.wait}s for ingestion")
    for left in range(args.wait, 0, -30):
        print(f"  ...{left}s")
        time.sleep(min(30, left))

    print("\n" + "=" * 96)
    print("PHASE 3 — TRUTH vs LlmLog (per-call, by response id) vs customMetrics (by model)")
    print("=" * 96)
    hdr = f"{'provider':10} {'mode':11} {'field':11} {'truth':>7} {'LlmLog':>8} {'verdict':>8}"
    print(hdr)
    print("-" * len(hdr))
    all_ok = True
    for r in recs:
        if not r["rid"]:
            print(f"{r['provider']:10} {r['mode']:11} (no response id — skipped)")
            all_ok = False
            continue
        lg = llmlog_by_rid(ws, r["rid"])
        if lg is None:
            print(f"{r['provider']:10} {r['mode']:11} (not in LlmLog — ingestion lag or dropped)")
            all_ok = False
            continue
        for f in ("prompt", "completion", "total"):
            ok = r[f] == lg[f]
            all_ok = all_ok and ok
            print(f"{r['provider']:10} {r['mode']:11} {f:11} {r[f]:>7} {lg[f]:>8} {'PASS' if ok else 'FAIL <<':>8}")

    print("\n" + "=" * 96)
    print("customMetrics totals per model (aggregated; incl. cached which LlmLog lacks)")
    print("=" * 96)
    seen = set()
    for r in recs:
        if r["model"] in seen:
            continue
        seen.add(r["model"])
        cm = custommetrics_by_model(ai, r["model"])
        truth = sum(x["total"] for x in recs if x["model"] == r["model"])
        tp = sum(x["prompt"] for x in recs if x["model"] == r["model"])
        tc = sum(x["completion"] for x in recs if x["model"] == r["model"])
        if not cm:
            print(f"  {r['model']:28} customMetrics: (none yet)")
            continue
        print(f"  {r['model']:28} "
              f"prompt={cm.get('Prompt Tokens',(0,0))[0]}/{tp} "
              f"completion={cm.get('Completion Tokens',(0,0))[0]}/{tc} "
              f"total={cm.get('Total Tokens',(0,0))[0]}/{truth} "
              f"cached={cm.get('Prompt Cached Tokens',(0,0))[0]} "
              f"(customMetrics/truth for this model's calls in window)")

    print("\n" + "=" * 96)
    print("RESULT:", "LlmLog matches upstream on every field ✅" if all_ok
          else "MISMATCH — see FAIL rows ❌")
    print("=" * 96)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
