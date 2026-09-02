"""`copilot_usage` must not reach the client.

Upstream returns its own billing record inline on every call — token counts,
`cost_per_batch`, `total_nano_aiu` — and the hub forwarded the response body
verbatim, so every tenant received the wholesale price behind the retail one
they are billed on. Verified against the live gateway on 2026-08-28: present at
the top level of both a `/v1/messages` and a `/v1/chat/completions` 200, with
real values (`cost_per_batch: 500000000000` for input).

The constraint that makes this delicate is that the SAME object is what billing
runs on. `_extract_copilot_usage` reads it off the body and hands it to
`_emit_usage`, which ships it to Event Hub; the control plane prices from it at
import time. So the redaction has to happen on the way out and nowhere else —
strip it too early, or in place, and the call silently becomes `unpriced` with a
cost of $0. Nothing errors; the money just goes missing.

These tests pin both halves: the client copy is clean, and the object the rest
of the hub holds is untouched.

NON-STREAMING ONLY. The streaming paths forward upstream's SSE unchanged and
recover `copilot_usage` afterwards by scanning what they buffered, so redacting
there means yielding a rewritten copy while buffering the original — a
different change with a different failure mode, done separately.
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

# Shape as upstream actually returns it, trimmed. `cost_per_batch` is the field
# that makes this a leak rather than a curiosity: it is our unit cost.
COPILOT_USAGE = {
    "token_details": [
        {"batch_size": 1000000, "cost_per_batch": 500000000000,
         "token_count": 14, "token_type": "input"},
        {"batch_size": 1000000, "cost_per_batch": 50000000000,
         "token_count": 0, "token_type": "cache_read"},
    ],
    "total_nano_aiu": 7000000,
}


def _anthropic_body() -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-opus-4.7",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 14, "output_tokens": 5,
                  "cache_read_input_tokens": 0},
        "copilot_usage": COPILOT_USAGE,
    }


def _openai_body() -> dict:
    return {
        "id": "chatcmpl-1", "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 10, "total_tokens": 18},
        "copilot_usage": COPILOT_USAGE,
    }


def _rendered(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


# --- the redaction ----------------------------------------------------------


@pytest.mark.parametrize("body", [_anthropic_body(), _openai_body()],
                         ids=["anthropic", "openai"])
def test_the_client_never_sees_copilot_usage(body: dict) -> None:
    """Both protocols, because both carry it — this is upstream's field, not a
    property of the schema the client asked for."""
    assert "copilot_usage" not in _rendered(srv._client_json(body))


@pytest.mark.parametrize("body", [_anthropic_body(), _openai_body()],
                         ids=["anthropic", "openai"])
def test_the_standard_usage_object_stays(body: dict) -> None:
    """Clients count their own tokens from `usage`, and the gateway's
    llm-emit-token-metric reads it too. Removing both fields would fix the leak
    by breaking the feature."""
    out = _rendered(srv._client_json(body))
    assert out["usage"] == body["usage"]


def test_everything_else_is_forwarded_unchanged() -> None:
    body = _anthropic_body()
    out = _rendered(srv._client_json(body))
    assert out == {k: v for k, v in body.items() if k != "copilot_usage"}


def test_the_status_code_is_preserved() -> None:
    """Error bodies go through the same helper. A 400 that arrives as a 200
    would turn an upstream refusal into a silent success."""
    assert srv._client_json({"error": {"message": "nope"}}, 400).status_code == 400


# --- what billing still needs ----------------------------------------------


def test_the_source_object_is_not_mutated() -> None:
    """The whole risk of this change in one assertion.

    The same dict is handed to `_emit_usage` as `resp_body`, and
    `_extract_copilot_usage` reads the billing record off it. Popping the key in
    place would redact the audit archive and — depending on ordering — the
    billing event, turning the call into an `unpriced` $0 with nothing raised
    anywhere."""
    body = _anthropic_body()
    srv._client_json(body)
    assert body["copilot_usage"] == COPILOT_USAGE


def test_extraction_still_finds_it_after_a_response_was_built() -> None:
    """Ordering, stated as a property: building the client's copy must not cost
    the billing path its input, whichever runs first."""
    body = _openai_body()
    srv._client_json(body)
    assert srv._extract_copilot_usage(body) == COPILOT_USAGE


# --- shapes that must pass through untouched --------------------------------


def test_a_body_without_the_field_is_returned_as_is() -> None:
    """The common case for the image and count_tokens endpoints, which do not go
    to Copilot at all. No copy, no allocation."""
    body = {"input_tokens": 15}
    assert srv._without_copilot_usage(body) is body


def test_a_non_dict_body_is_left_alone() -> None:
    """Upstream is not obliged to answer with an object, and a list or a string
    must not become a crash on the response path."""
    assert srv._without_copilot_usage([1, 2]) == [1, 2]
    assert srv._without_copilot_usage("plain") == "plain"
    assert srv._without_copilot_usage(None) is None
