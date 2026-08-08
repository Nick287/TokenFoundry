#!/usr/bin/env python3
"""Cut a stream mid-flight and check the usage event still lands.

THE RISK, stated in docs/copilot-usage-billing.md: on a streamed call the cost
data lives in a single late SSE event. If the connection dies before it — client
disconnect, timeout, network jitter — upstream still bills us and the record is
gone. Silently.

THE FIX being verified: the hub emits from inside its generator's `finally`.
That used to `await eventhub.emit(...)`, which raises "async generator ignored
GeneratorExit" when Starlette closes the generator on disconnect, so the emit
never completed. Worse, the OpenAI generator also had a `yield` in that
`finally`, which is illegal under GeneratorExit and aborted the block before the
emit was even reached. Both now go through a synchronous `_spawn_emit`.

WHY THIS SCRIPT EXISTS: `tests/test_hub_eventhub_reliability.py` asserts
`_spawn_emit` is not a coroutine — a statement about the code's shape, made
without opening a socket. Nothing in the repo has ever cut a real connection.
Every other SSE reader here drains to EOF.

WHAT IT PROVES, and what it cannot: a disconnected call that still produces a
Cosmos document with output tokens proves the emit survived. It does NOT prove
the token counts are complete — if the stream was cut before upstream sent its
usage chunk, the hub records what it saw, which may be an estimate. The report
distinguishes the two.

NO SECRETS IN THIS FILE. Configure via environment (or a git-ignored .env):

    TF_GATEWAY_URL         the APIM gateway
    TF_VIRTUAL_KEY         an APIM subscription key
    TF_CONTROL_PLANE_URL   the control plane (to read Cosmos back)
    TF_ADMIN_USERNAME      default: admin
    TF_ADMIN_PASSWORD

Usage:
    python tests/manual/stream_disconnect_test.py
    python tests/manual/stream_disconnect_test.py --after 3 --wait 420
    python tests/manual/stream_disconnect_test.py --provider anthropic

Exit codes: 0 the interrupted call was billed, 1 it was lost, 2 config missing.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

PROMPT = ("Count slowly from one to forty, one number per line, "
          "spelling each number as a word.")
MAX_TOKENS = 1200


def load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except Exception:  # noqa: BLE001 — optional
        pass
    here = Path(__file__).resolve()
    for candidate in (Path.cwd() / ".env", here.parent.parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
            if m:
                os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


def require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: env var {name} is required (see docstring)")
    return v


PROVIDERS = {
    "openai": {
        "path": "/llm-openai/v1/chat/completions",
        "header": "api-key",
        "model": "gpt-4o-mini",
        "body": lambda m: {
            "model": m, "max_tokens": MAX_TOKENS, "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": PROMPT}],
        },
    },
    "anthropic": {
        "path": "/llm-anthropic/v1/messages",
        "header": "x-api-key",
        "model": "claude-haiku-4.5",
        "body": lambda m: {
            "model": m, "max_tokens": MAX_TOKENS, "stream": True,
            "messages": [{"role": "user", "content": PROMPT}],
        },
    },
}


def abort_after_n_events(gw: str, key: str, cfg: dict, model: str,
                         after: int) -> dict:
    """Open the stream, read `after` SSE events, then hang up mid-response.

    Uses http.client rather than urllib because the abort has to be a real
    connection close while the server is still writing — that is what makes
    Starlette throw GeneratorExit into the hub's generator. urlopen offers no
    way to do that without draining first.
    """
    u = urlparse(gw)
    conn = http.client.HTTPSConnection(u.netloc, timeout=60)
    body = json.dumps(cfg["body"](model))
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        cfg["header"]: key,
    }
    if "messages" in cfg["path"]:
        headers["anthropic-version"] = "2023-06-01"

    t0 = time.perf_counter()
    conn.request("POST", u.path.rstrip("/") + cfg["path"], body=body, headers=headers)
    resp = conn.getresponse()
    status = resp.status
    if status != 200:
        detail = resp.read()[:200].decode("utf-8", "replace")
        conn.close()
        return {"status": status, "events": 0, "err": detail}

    events = 0
    saw_usage = False
    try:
        while events < after:
            line = resp.fp.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").strip()
            if not s.startswith("data:"):
                continue
            payload = s[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            events += 1
            if '"usage"' in payload:
                saw_usage = True
    finally:
        # The abort. No resp.read(), no graceful close — drop the socket while
        # the server is still writing.
        conn.sock.close() if conn.sock else None
        conn.close()
    return {
        "status": status, "events": events, "saw_usage": saw_usage,
        "sec": time.perf_counter() - t0,
    }


def cp_call(base: str, path: str, tok: str | None = None, body: dict | None = None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json",
                 **({"authorization": "Bearer " + tok} if tok else {})},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def count_streamed_since(base: str, tok: str, tenant: str,
                         lo: datetime) -> tuple[int, int]:
    """(streamed records since `lo`, of which with output tokens)."""
    total = with_out = 0
    page = 1
    while page <= 40:
        r = cp_call(base, f"/api/admin/usage/{tenant}/records"
                          f"?page={page}&page_size=200&hours=2", tok)
        items = r["items"]
        if not items:
            break
        for it in items:
            ts = it.get("ts")
            if not ts:
                continue
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) < lo:
                continue
            total += 1
            if it["completion_tok"] > 0:
                with_out += 1
        if len(items) < 200:
            break
        page += 1
    return total, with_out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="openai")
    ap.add_argument("--model", help="override the provider's default model")
    ap.add_argument("--after", type=int, default=5,
                    help="abort after this many SSE events (default 5)")
    ap.add_argument("--calls", type=int, default=3,
                    help="how many interrupted calls to make (default 3)")
    ap.add_argument("--wait", type=int, default=420,
                    help="seconds to wait for the capture+import cycle "
                         "(default 420; Capture flushes every 300s and the "
                         "importer polls every 300s)")
    args = ap.parse_args()

    load_dotenv_if_present()
    gw = require("TF_GATEWAY_URL").rstrip("/")
    key = require("TF_VIRTUAL_KEY")
    base = require("TF_CONTROL_PLANE_URL").rstrip("/")
    cfg = PROVIDERS[args.provider]
    model = args.model or cfg["model"]

    tok = cp_call(base, "/api/login", body={
        "username": os.environ.get("TF_ADMIN_USERNAME", "admin"),
        "password": require("TF_ADMIN_PASSWORD"),
    })["access_token"]
    tenant = cp_call(base, "/api/tenants", tok)[0]["id"]

    lo = datetime.now(UTC) - timedelta(seconds=5)
    before_total, before_out = count_streamed_since(base, tok, tenant, lo)

    print(f"provider : {args.provider}   model: {model}")
    print(f"aborting after {args.after} SSE events, {args.calls} times\n")

    aborted = 0
    for i in range(args.calls):
        r = abort_after_n_events(gw, key, cfg, model, args.after)
        if r["status"] != 200:
            print(f"  call {i + 1}: HTTP {r['status']} {r.get('err', '')[:80]}")
            continue
        aborted += 1
        print(f"  call {i + 1}: read {r['events']} events in {r['sec']:.2f}s, "
              f"then dropped the socket"
              + ("  (usage chunk had already arrived)" if r.get("saw_usage") else ""))
        time.sleep(1)

    if not aborted:
        print("\nno call was successfully started — nothing to verify")
        return 2

    print(f"\nwaiting {args.wait}s for capture + import "
          "(Capture 300s + importer poll 300s)...")
    time.sleep(args.wait)

    after_total, after_out = count_streamed_since(base, tok, tenant, lo)
    new_total = after_total - before_total
    new_out = after_out - before_out

    print(f"\nnew usage records since the test began : {new_total}")
    print(f"  of which carry output tokens          : {new_out}")
    print(f"  interrupted calls made                : {aborted}")

    if new_total >= aborted:
        print("\n✅ every interrupted call produced a usage record — the emit "
              "survived the disconnect")
        if new_out < aborted:
            print(f"⚠️  but {aborted - new_out} of them have zero output tokens: the "
                  "stream was cut before upstream's usage chunk, so the hub "
                  "recorded what it saw. The call is billed, the count is partial.")
        return 0
    print(f"\n❌ {aborted - new_total} interrupted call(s) produced NO record — "
          "the disconnect lost billing data")
    print("   (Cosmos can lag; re-run with a longer --wait before concluding.)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
