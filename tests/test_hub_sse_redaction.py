"""`copilot_usage` must not reach the client on a STREAM either.

The non-streaming half was the easy half: extract, then return a copy. Streaming
inverts the order and that is the whole risk. The generator sends bytes to the
client as they arrive and only afterwards reconstructs the call for billing, by
scanning what it buffered:

    collected.append(text)          # what billing will read
    yield <bytes to client>
    ...
    finally:
        copilot_usage=_scan_sse_copilot_usage("".join(collected))

So redacting the buffered copy instead of the yielded one leaves every streamed
call `unpriced` at $0 — request still 200, client still served, nothing logged,
nothing raised. The money simply stops being recorded. `test_the_buffered_copy`
below is the assertion that stands between us and that.

The rule itself is uniform, established by calling every registered model in
both modes: `copilot_usage` is a TOP-LEVEL key of the object on a `data:` line,
in all four protocols. Only the carrier differs, and these fixtures are real
captured responses of each — including Google's, which emits the field on
SEVERAL chunks rather than one, and the Responses API's, where it sits beside
`response` rather than inside it.

Fixtures over hand-written SSE on purpose: a redaction built from an imagined
shape only fails once upstream is on the other end of it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HUB_ROOT = Path(__file__).resolve().parent.parent / "vendored" / "gitmodel-hub"
if str(_HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(_HUB_ROOT))

srv = pytest.importorskip(
    "hub.server",
    reason="vendored hub deps not installed in this environment",
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sse"
ALL = ["anthropic_messages.sse", "openai_chat.sse",
       "google_chat.sse", "openai_responses.sse"]


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _redact_stream(raw: str) -> str:
    """What the client receives: every complete event rewritten, tail included.

    Mirrors the generators' loop so the fixtures exercise the same splitting the
    live path does.
    """
    out, buf = [], raw
    parts = buf.split("\n\n")
    for event in parts[:-1]:
        out.append(srv._redact_sse_event(event).decode("utf-8") + "\n\n")
    if parts[-1]:
        out.append(srv._redact_sse_event(parts[-1]).decode("utf-8"))
    return "".join(out)


# --- the redaction, against real captures -----------------------------------


@pytest.mark.parametrize("name", ALL)
def test_no_protocol_leaks_it(name: str) -> None:
    assert "copilot_usage" in _load(name), "fixture no longer carries the field"
    assert "copilot_usage" not in _redact_stream(_load(name))


def test_google_repeats_are_all_removed() -> None:
    """Google puts it on more than one chunk. A redaction that stopped at the
    first match — or that only handled a designated 'usage event' — would leave
    the rest in place, and the leak would depend on how many chunks a particular
    answer happened to take."""
    raw = _load("google_chat.sse")
    assert raw.count("copilot_usage") > 1
    assert "copilot_usage" not in _redact_stream(raw)


@pytest.mark.parametrize("name", ALL)
def test_the_stream_is_still_valid_sse(name: str) -> None:
    """Every `data:` line must still parse, and the event count must not change
    — a client reading these is a real SDK, not a string matcher."""
    before, after = _load(name), _redact_stream(_load(name))
    assert after.count("data:") == before.count("data:")
    for line in after.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                json.loads(payload)


def test_the_usage_object_survives() -> None:
    """Anthropic carries `usage` on the SAME `message_delta` event as
    `copilot_usage`. Removing the event, or the whole object, would take the
    client's own token counts with it."""
    after = _redact_stream(_load("anthropic_messages.sse"))
    deltas = [json.loads(ln[5:]) for ln in after.splitlines()
              if ln.startswith("data:") and '"message_delta"' in ln]
    assert deltas, "no message_delta event in the fixture"
    assert "usage" in deltas[0]
    assert "copilot_usage" not in deltas[0]


# --- the half that keeps billing alive --------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_the_buffered_copy_still_prices_the_call(name: str) -> None:
    """THE test of this change.

    Billing reads what the generator buffered, not what it sent. If the two ever
    become the same object, every streamed call silently becomes unpriced — a
    failure with no error, no log line, and no symptom until someone reconciles
    a month of invoices.
    """
    raw = _load(name)                      # what `collected` holds: the original
    assert srv._scan_sse_copilot_usage(raw) is not None


def test_redacting_does_not_mutate_the_source() -> None:
    raw = _load("openai_chat.sse")
    _redact_stream(raw)
    assert "copilot_usage" in raw


# --- chunk boundaries -------------------------------------------------------


def test_a_split_line_is_not_redacted_until_it_is_whole() -> None:
    """Why the generators buffer to `\\n\\n` instead of rewriting each network
    chunk. Half an object does not parse, so a naive per-chunk redaction would
    pass the line through untouched — and whether that happened would depend on
    where the network split the bytes."""
    line = 'data: {"type":"message_delta","copilot_usage":{"total_nano_aiu":7}}'
    half = line[:40]
    assert srv._redact_sse_line(half) == half        # unparseable, left alone
    assert "copilot_usage" not in srv._redact_sse_line(line)


# --- lines that must pass through untouched ---------------------------------


@pytest.mark.parametrize("line", [
    "event: message_delta",
    "data: [DONE]",
    "data:",
    ": keep-alive comment",
    "",
    'data: {"type":"ping"}',
    "data: not json at all",
])
def test_lines_without_the_field_are_returned_unchanged(line: str) -> None:
    """Identity, not just equality: an untouched line must not be re-serialised,
    because re-encoding JSON we did not need to change risks altering formatting
    a client is parsing."""
    assert srv._redact_sse_line(line) is line


def test_a_json_array_payload_is_left_alone() -> None:
    assert srv._redact_sse_line('data: [1,2]') == 'data: [1,2]'
