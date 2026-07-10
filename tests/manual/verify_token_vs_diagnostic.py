#!/usr/bin/env python3
"""Token Foundry — verify gateway token counts against the official Diagnostic Log.

For each (provider × mode) combination this script:

  1. Sends ONE request through the APIM gateway and records the AUTHORITATIVE
     token usage straight from the upstream response (the client-visible
     `usage` — this is ground truth, provider-billed).
  2. Waits for Azure Monitor to ingest the LLM diagnostic log.
  3. Queries the dedicated table `ApiManagementGatewayLlmLog` for that exact
     call (matched by the response id) and reads its Prompt/Completion/Total.
  4. Prints a side-by-side table: TRUTH vs DIAGNOSTIC, with a PASS/FAIL verdict.

Matrix covered (2 providers × 2 modes = 4 calls):

    provider    mode         client path                        auth header
    ---------   ----------   --------------------------------   -----------
    openai      non-stream   /llm-openai/v1/chat/completions    api-key
    openai      stream       /llm-openai/v1/chat/completions    api-key
    anthropic   non-stream   /llm-anthropic/v1/messages         x-api-key
    anthropic   stream       /llm-anthropic/v1/messages         x-api-key

Why this exists: dev-a05 testing found the official LlmLog records completion=0
for OpenAI STREAMING (APIM doesn't parse the OpenAI SSE response body), while
anthropic streaming and all non-streaming calls are exact. This script makes
that regression reproducible on demand and turns "is metering correct?" into a
one-command answer for any environment.

------------------------------------------------------------------------------
NO SECRETS IN THIS FILE. Configure via environment (or a git-ignored .env):

    TF_GATEWAY_URL     e.g. https://<your-apim>.azure-api.net
    TF_VIRTUAL_KEY     an APIM subscription (virtual) key
    TF_LAW_CUSTOMER_ID the Log Analytics workspace GUID (customerId) the APIM
                       diagnostic writes to. Find it with:
                         az monitor log-analytics workspace show \
                           -g <rg> -n <law> --query customerId -o tsv

Optional:
    TF_OPENAI_MODEL      default: gpt-4o-mini
    TF_ANTHROPIC_MODEL   default: claude-haiku-4.5
    TF_INGEST_WAIT_SECS  default: 420  (how long to wait for log ingestion)

Prereq: `az` CLI logged in with reader on the workspace. The script shells out
to `az monitor log-analytics query`.

Usage:
    python tests/manual/verify_token_vs_diagnostic.py            # all 4
    python tests/manual/verify_token_vs_diagnostic.py --only openai
    python tests/manual/verify_token_vs_diagnostic.py --only anthropic
    python tests/manual/verify_token_vs_diagnostic.py --wait 600
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Config / env                                                                #
# --------------------------------------------------------------------------- #


def load_dotenv_if_present() -> None:
    """Load a local .env (KEY=VALUE lines) into os.environ without a dependency."""
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
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: environment variable {name} is required (see module docstring).")
    return val


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


def http_post_raw(url: str, headers: dict[str, str], body: dict) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _sse_usage(raw: bytes, *, anthropic: bool) -> dict:
    """Extract the final usage object from an SSE stream body."""
    usage: dict = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        # anthropic: usage on message_start (.message.usage) + message_delta (.usage)
        # openai: usage on the trailing chunk (needs include_usage)
        cand = obj.get("usage")
        if anthropic:
            cand = (obj.get("message") or {}).get("usage") or cand
        if cand:
            # merge so anthropic's input (message_start) + output (message_delta) combine
            usage = {**usage, **cand}
    return usage


# --------------------------------------------------------------------------- #
# Token normalization → (prompt, completion, total, response_id)              #
# --------------------------------------------------------------------------- #


def norm_openai(usage: dict) -> tuple[int, int, int]:
    p = int(usage.get("prompt_tokens", 0) or 0)
    c = int(usage.get("completion_tokens", 0) or 0)
    t = int(usage.get("total_tokens", p + c) or (p + c))
    return p, c, t


def norm_anthropic(usage: dict) -> tuple[int, int, int]:
    # input_tokens EXCLUDES cache; add cache read+creation for the true prompt total.
    p = int(usage.get("input_tokens", 0) or 0)
    p += int(usage.get("cache_read_input_tokens", 0) or 0)
    p += int(usage.get("cache_creation_input_tokens", 0) or 0)
    c = int(usage.get("output_tokens", 0) or 0)
    return p, c, p + c


# --------------------------------------------------------------------------- #
# One provider×mode call → ground-truth record                               #
# --------------------------------------------------------------------------- #


def call_openai(gw: str, vk: str, model: str, *, stream: bool) -> dict:
    url = f"{gw}/llm-openai/v1/chat/completions"
    headers = {"api-key": vk, "Content-Type": "application/json"}
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with a short sentence."}],
        "max_tokens": 40,
    }
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    status, raw = http_post_raw(url, headers, body)
    if stream:
        usage = _sse_usage(raw, anthropic=False)
        rid = _first_id(raw, prefix="chatcmpl-")
    else:
        obj = json.loads(raw)
        usage = obj.get("usage", {}) or {}
        rid = obj.get("id", "")
    p, c, t = norm_openai(usage)
    return {"status": status, "prompt": p, "completion": c, "total": t, "rid": rid}


def call_anthropic(gw: str, vk: str, model: str, *, stream: bool) -> dict:
    url = f"{gw}/llm-anthropic/v1/messages"
    headers = {
        "x-api-key": vk,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    body: dict = {
        "model": model,
        "max_tokens": 40,
        "messages": [{"role": "user", "content": "Reply with a short sentence."}],
    }
    if stream:
        body["stream"] = True
    status, raw = http_post_raw(url, headers, body)
    if stream:
        usage = _sse_usage(raw, anthropic=True)
        rid = _first_id(raw, prefix="msg_")
    else:
        obj = json.loads(raw)
        usage = obj.get("usage", {}) or {}
        rid = obj.get("id", "")
    p, c, t = norm_anthropic(usage)
    return {"status": status, "prompt": p, "completion": c, "total": t, "rid": rid}


def _first_id(raw: bytes, *, prefix: str) -> str:
    """Pull the first `"id":"<prefix>..."` out of an SSE body."""
    text = raw.decode("utf-8", "replace")
    key = '"id":"' + prefix
    i = text.find(key)
    if i < 0:
        return ""
    start = i + len('"id":"')
    end = text.find('"', start)
    return text[start:end] if end > start else ""


# --------------------------------------------------------------------------- #
# Diagnostic log query (az CLI → ApiManagementGatewayLlmLog)                  #
# --------------------------------------------------------------------------- #


def query_diagnostic(customer_id: str, rid: str) -> dict | None:
    """Return the LlmLog row for a response id, or None if not ingested yet."""
    kql = (
        "ApiManagementGatewayLlmLog "
        "| where TimeGenerated > ago(1h) "
        f"| where RequestId == '{rid}' "
        "| project PromptTokens, CompletionTokens, TotalTokens, "
        "ModelName, IsStreamCompletion "
        "| take 1"
    )
    try:
        out = subprocess.run(
            [
                "az", "monitor", "log-analytics", "query",
                "-w", customer_id,
                "--analytics-query", kql,
                "-o", "json",
            ],
            capture_output=True, text=True, timeout=90,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"    (az query failed: {e})")
        return None
    if out.returncode != 0:
        print(f"    (az query error: {out.stderr.strip()[:200]})")
        return None
    try:
        rows = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if not rows:
        return None
    r = rows[0]
    return {
        "prompt": int(r.get("PromptTokens", 0) or 0),
        "completion": int(r.get("CompletionTokens", 0) or 0),
        "total": int(r.get("TotalTokens", 0) or 0),
        "model": r.get("ModelName") or "",
        "stream": r.get("IsStreamCompletion"),
    }


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


def main() -> int:
    load_dotenv_if_present()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=["openai", "anthropic"], help="test one provider")
    ap.add_argument("--wait", type=int, default=int(os.environ.get("TF_INGEST_WAIT_SECS", "420")),
                    help="seconds to wait for log ingestion before querying")
    args = ap.parse_args()

    gw = require("TF_GATEWAY_URL").rstrip("/")
    vk = require("TF_VIRTUAL_KEY")
    customer_id = require("TF_LAW_CUSTOMER_ID")
    oai_model = os.environ.get("TF_OPENAI_MODEL", "gpt-4o-mini")
    ant_model = os.environ.get("TF_ANTHROPIC_MODEL", "claude-haiku-4.5")

    plan: list[tuple[str, str, bool]] = []
    if args.only in (None, "openai"):
        plan += [("openai", oai_model, False), ("openai", oai_model, True)]
    if args.only in (None, "anthropic"):
        plan += [("anthropic", ant_model, False), ("anthropic", ant_model, True)]

    # Phase 1: fire all calls, capture ground truth.
    print("=" * 78)
    print("Phase 1 — calling gateway, capturing authoritative usage")
    print("=" * 78)
    records = []
    for provider, model, stream in plan:
        mode = "stream" if stream else "non-stream"
        fn = call_openai if provider == "openai" else call_anthropic
        rec = fn(gw, vk, model, stream=stream)
        rec.update(provider=provider, mode=mode, model=model)
        ok = rec["status"] == 200 and rec["rid"]
        print(f"  {provider:9} {mode:10} HTTP {rec['status']} "
              f"id={rec['rid'][:28] or '(none)':28} "
              f"truth: p={rec['prompt']} c={rec['completion']} t={rec['total']} "
              f"{'' if ok else '  <-- WARN: no id / non-200'}")
        records.append(rec)

    # Phase 2: wait for ingestion.
    print("\n" + "=" * 78)
    print(f"Phase 2 — waiting {args.wait}s for Azure Monitor ingestion")
    print("=" * 78)
    for remaining in range(args.wait, 0, -30):
        print(f"  ...{remaining}s left")
        time.sleep(min(30, remaining))

    # Phase 3: query diagnostic + compare.
    print("\n" + "=" * 78)
    print("Phase 3 — TRUTH (upstream usage) vs DIAGNOSTIC (ApiManagementGatewayLlmLog)")
    print("=" * 78)
    header = f"{'provider':10} {'mode':11} {'field':7} {'truth':>7} {'diag':>7}  verdict"
    print(header)
    print("-" * len(header))
    all_pass = True
    for rec in records:
        if not rec["rid"]:
            print(f"{rec['provider']:10} {rec['mode']:11} (no response id — cannot match)")
            all_pass = False
            continue
        diag = query_diagnostic(customer_id, rec["rid"])
        if diag is None:
            print(f"{rec['provider']:10} {rec['mode']:11} (not found in diagnostic log — "
                  f"ingestion delay or dropped)")
            all_pass = False
            continue
        for field in ("prompt", "completion", "total"):
            tv, dv = rec[field], diag[field]
            ok = tv == dv
            all_pass = all_pass and ok
            print(f"{rec['provider']:10} {rec['mode']:11} {field:7} {tv:>7} {dv:>7}  "
                  f"{'PASS' if ok else 'FAIL <<<'}")

    print("\n" + "=" * 78)
    print("RESULT:", "ALL PASS ✅" if all_pass else "MISMATCHES FOUND ❌ (see FAIL rows)")
    print("=" * 78)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
