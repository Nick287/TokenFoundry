#!/usr/bin/env python3
"""An interactive streaming console for the gateway. Type, watch tokens arrive.

You type a message, the reply streams back character by character, and a single
line afterwards tells you what it cost and how fast it was. Conversation
history is kept, so turn 2 onwards actually exercises the multi-turn path (and
the prompt cache) rather than sending isolated one-shots.

Commands (anything else is sent to the model):
    /model <name>   switch model mid-conversation; history is kept
    /models         list the aliases this gateway serves
    /reset          clear the conversation
    /raw            toggle printing each SSE event type as it arrives
    /quit           exit  (Ctrl-D and Ctrl-C also work)

WHAT IT GETS RIGHT, each learned the hard way:

  * Anthropic splits streamed usage — `input_tokens` and the cache counters
    ride `message_start`, `output_tokens` rides `message_delta`, whose fragment
    repeats `input_tokens: 0`. Overwriting blindly zeroes the real value. A
    three-key allowlist here once dropped `cache_creation_input_tokens` from 51
    of 51 records.
  * An SSE `error` event is a FAILURE, not content. The hub emits one when
    upstream refuses after the 200 is already committed; counting events alone
    reports those as successes (docs/CAPACITY.zh.md §7.6).
  * Anthropic's `input_tokens` EXCLUDES cached tokens, so the billed prompt is
    input + cache_read + cache_write.

NO SECRETS IN THIS FILE. Configure via environment or a git-ignored .env:

    TF_GATEWAY_URL   the APIM gateway, e.g. https://<apim>.azure-api.net
    TF_VIRTUAL_KEY   an APIM subscription key

Usage:
    python tests/manual/stream_console_test.py
    python tests/manual/stream_console_test.py --model claude-opus-5
    echo "hi" | python tests/manual/stream_console_test.py    # piped, one shot
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PATH = "/llm-anthropic/v1/messages"
DEFAULT_MODEL = "claude-opus-4.8"


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
        sys.exit(f"ERROR: env var {name} is required (see the docstring)")
    return v


def merge_usage(into: dict, new: dict) -> None:
    """Fold one usage fragment in without letting a later 0 erase a real value.

    Merges everything rather than an allowlist: cache_creation_input_tokens is
    billed at 1.25x input and was silently lost that way once.
    """
    for k, v in new.items():
        if k in into and not v:
            continue
        into[k] = v


def list_models(gw: str, key: str) -> list[str]:
    """Ask the gateway what it serves. Anthropic aliases only — this console
    speaks the Messages API, so offering an OpenAI alias would just 404."""
    req = urllib.request.Request(
        gw + "/llm-anthropic/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        print(f"  (could not list models: {type(e).__name__})")
        return []
    items = d.get("data") if isinstance(d, dict) else None
    return sorted(m.get("id", "") for m in (items or []) if m.get("id"))


def stream_turn(url: str, key: str, model: str, messages: list[dict],
                max_tokens: int, timeout: int, *, raw: bool) -> dict:
    """Send the conversation, print the reply as it arrives, return the stats."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={
            "x-api-key": key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    usage: dict = {}
    parts: list[str] = []
    events = 0
    ttft = None
    upstream_error = None
    t0 = time.perf_counter()

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for line_bytes in r:
                line = line_bytes.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                events += 1
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if isinstance(obj.get("error"), dict):
                    upstream_error = obj["error"]
                    continue

                etype = obj.get("type")
                if raw:
                    print(f"\n  [{etype}]", end="", flush=True)
                if etype == "message_start":
                    merge_usage(usage, (obj.get("message") or {}).get("usage") or {})
                elif etype == "message_delta":
                    merge_usage(usage, obj.get("usage") or {})
                elif etype == "content_block_delta":
                    piece = (obj.get("delta") or {}).get("text") or ""
                    if piece:
                        if ttft is None:
                            ttft = time.perf_counter() - t0
                        parts.append(piece)
                        print(piece, end="", flush=True)
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace")
        return {"ok": False, "err": f"HTTP {e.code}: {detail}",
                "sec": time.perf_counter() - t0}
    except Exception as e:  # noqa: BLE001 — a console must report, not crash
        return {"ok": False, "err": f"{type(e).__name__}: {e}",
                "sec": time.perf_counter() - t0}

    elapsed = time.perf_counter() - t0
    text = "".join(parts)
    if upstream_error is not None:
        return {"ok": False, "sec": elapsed, "events": events,
                "err": f"upstream {upstream_error.get('code')}: "
                       f"{str(upstream_error.get('message'))[:120]}"}
    if not text:
        # A 200 carrying nothing. Before the streaming fix this is exactly what
        # an upstream refusal looked like, with no error event to explain it.
        return {"ok": False, "sec": elapsed, "events": events,
                "err": "200 but no content — empty stream"}

    inp = int(usage.get("input_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    return {
        "ok": True, "text": text, "sec": elapsed, "ttft": ttft, "events": events,
        "prompt": inp + cr + cw, "input": inp, "cache_read": cr,
        "cache_write": cw, "output": out,
        "tok_per_s": (out / elapsed) if elapsed > 0 else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--raw", action="store_true",
                    help="print each SSE event type as it arrives")
    args = ap.parse_args()

    load_dotenv_if_present()
    gw = require("TF_GATEWAY_URL").rstrip("/")
    key = require("TF_VIRTUAL_KEY")
    url = gw + PATH

    model = args.model
    raw = args.raw
    messages: list[dict] = []
    interactive = sys.stdin.isatty()

    print(f"gateway : {gw}")
    print(f"model   : {model}   max_tokens={args.max_tokens}")
    if interactive:
        print("type a message and press Enter. /quit to exit, /? for commands.\n")

    while True:
        try:
            text = input("\n\033[1myou ›\033[0m " if interactive else "")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        text = text.strip()
        if not text:
            continue

        if text in ("/quit", "/exit", "/q"):
            break
        if text in ("/?", "/help"):
            print("  /model <name>   switch model (history kept)")
            print("  /models         list this gateway's Anthropic aliases")
            print("  /reset          clear the conversation")
            print("  /raw            toggle SSE event tracing")
            print("  /quit           exit")
            continue
        if text == "/models":
            for m in list_models(gw, key):
                print(f"  {m}{'   <- current' if m == model else ''}")
            continue
        if text.startswith("/model"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                model = parts[1].strip()
                print(f"  model -> {model}  (history kept: {len(messages)} messages)")
            else:
                print(f"  current model: {model}")
            continue
        if text == "/reset":
            messages = []
            print("  conversation cleared")
            continue
        if text == "/raw":
            raw = not raw
            print(f"  raw event tracing: {'on' if raw else 'off'}")
            continue

        messages.append({"role": "user", "content": text})
        print(f"\n\033[1m{model} ›\033[0m ", end="", flush=True)
        r = stream_turn(url, key, model, messages, args.max_tokens,
                        args.timeout, raw=raw)

        if not r["ok"]:
            print(f"\n\033[31m  FAILED: {r['err']}\033[0m")
            # Drop the turn that failed: leaving it in history would resend a
            # message the model never answered, and every later turn would
            # carry it.
            messages.pop()
            continue

        # Keep the reply so the next turn is a real continuation.
        messages.append({"role": "assistant", "content": r["text"]})
        cache = ""
        if r["cache_read"] or r["cache_write"]:
            cache = f"  cache_read={r['cache_read']} cache_write={r['cache_write']}"
        print(f"\n\033[2m  {r['ttft']:.2f}s to first token · {r['sec']:.2f}s total · "
              f"{r['tok_per_s']:.1f} tok/s · {r['events']} events · "
              f"prompt={r['prompt']} output={r['output']}{cache}\033[0m")

    print("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
