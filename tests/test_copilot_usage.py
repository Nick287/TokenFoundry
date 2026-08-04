"""Hub-side extraction of upstream billing data and caller identity.

Every fixture here is a REAL response captured through the gateway (see the
tables in docs/ and the plan) — trimmed of message content, but the billing and
envelope fields are verbatim. That matters: the whole billing chain now trusts
upstream's numbers instead of a local price table, so a silent change in how we
find `copilot_usage` would produce zero-cost invoices with no error anywhere.

The four paths are covered deliberately, because they are NOT the same problem:
non-streaming is a single object, Anthropic streaming hides the object on
`message_delta`, and OpenAI streaming puts `copilot_usage` and `usage` on
DIFFERENT chunks — the case that a naive "read the usage chunk" parser fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The hub ships vendored (it deploys as its own container), so it isn't on the
# path as an installed package.
_HUB_ROOT = Path(__file__).resolve().parent.parent / "vendored" / "gitmodel-hub"
if str(_HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(_HUB_ROOT))

server = pytest.importorskip(
    "hub.server",
    reason="vendored hub deps not installed in this environment",
)


# The claude-haiku-4-5 object, exactly as upstream returned it. Arithmetic is
# self-consistent: 8 x 1e5 + 16 x 5e5 = 8,800,000 nano-AIU.
HAIKU_COPILOT_USAGE = {
    "token_details": [
        {
            "token_type": "input",
            "token_count": 8,
            "cost_per_batch": 100000000000,
            "batch_size": 1000000,
        },
        {
            "token_type": "cache_read",
            "token_count": 0,
            "cost_per_batch": 10000000000,
            "batch_size": 1000000,
        },
        {
            "token_type": "cache_write",
            "token_count": 0,
            "cost_per_batch": 125000000000,
            "batch_size": 1000000,
        },
        {
            "token_type": "output",
            "token_count": 16,
            "cost_per_batch": 500000000000,
            "batch_size": 1000000,
        },
    ],
    "total_nano_aiu": 8800000,
}


def test_anthropic_non_stream_top_level():
    """`/v1/messages` non-stream: sibling of `usage` on the response root."""
    body = {
        "type": "message",
        "model": "claude-haiku-4.5",
        "usage": {"input_tokens": 8, "output_tokens": 16},
        "copilot_usage": HAIKU_COPILOT_USAGE,
    }
    assert server._extract_copilot_usage(body) == HAIKU_COPILOT_USAGE


def test_openai_non_stream_top_level():
    """`/v1/chat/completions` non-stream: same position, different envelope."""
    body = {
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "usage": {"prompt_tokens": 8, "completion_tokens": 16, "total_tokens": 24},
        "copilot_usage": {"token_details": [], "total_nano_aiu": 0},
    }
    assert server._extract_copilot_usage(body) == {
        "token_details": [],
        "total_nano_aiu": 0,
    }


def test_anthropic_stream_rides_message_delta():
    """Anthropic SSE: the object appears once, on `message_delta`."""
    stream = (
        'data: {"type":"message_start","message":{"model":"claude-haiku-4.5"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":16},'
        '"copilot_usage":{"token_details":[],"total_nano_aiu":8800000}}\n\n'
        "data: [DONE]\n\n"
    )
    found = server._scan_sse_copilot_usage(stream)
    assert found is not None
    assert found["total_nano_aiu"] == 8800000


def test_openai_stream_usage_and_copilot_usage_on_different_chunks():
    """The regression this whole scan exists for.

    `_standardize_openai_usage_line()` splits `usage` off onto its own
    spec-compliant chunk and leaves `copilot_usage` behind on the finish chunk.
    A parser that peeks at the chunk carrying `usage` finds nothing and bills 0.
    """
    stream = (
        'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"}}]}\n\n'
        # finish chunk: carries copilot_usage, and NO usage
        'data: {"object":"chat.completion.chunk","choices":[{"finish_reason":"stop"}],'
        '"copilot_usage":{"token_details":[],"total_nano_aiu":8800000}}\n\n'
        # the synthesized usage-only chunk: carries usage, and NO copilot_usage
        'data: {"object":"chat.completion.chunk","choices":[],'
        '"usage":{"prompt_tokens":8,"completion_tokens":16,"total_tokens":24}}\n\n'
        "data: [DONE]\n\n"
    )
    found = server._scan_sse_copilot_usage(stream)
    assert found is not None, "copilot_usage must be found on a chunk without usage"
    assert found["total_nano_aiu"] == 8800000


def test_responses_stream_nested_under_response():
    """Responses-shaped streams nest the payload one level down."""
    stream = (
        'data: {"type":"response.completed","response":{"usage":{"total_tokens":24},'
        '"copilot_usage":{"token_details":[],"total_nano_aiu":8800000}}}\n\n'
    )
    found = server._scan_sse_copilot_usage(stream)
    assert found is not None
    assert found["total_nano_aiu"] == 8800000


def test_scan_tolerates_junk_and_missing_usage():
    """A stream with no billing data yields None, not an exception."""
    stream = (
        ": keep-alive\n\n"
        "data: not-json\n\n"
        'data: {"type":"content_block_delta"}\n\n'
        "data: [DONE]\n\n"
    )
    assert server._scan_sse_copilot_usage(stream) is None


def test_extract_copilot_usage_rejects_non_dict():
    assert server._extract_copilot_usage(None) is None
    assert server._extract_copilot_usage("copilot_usage") is None
    assert server._extract_copilot_usage({"copilot_usage": "nope"}) is None


# --------------------------------------------------------------------------- #
# Chargeback identity
# --------------------------------------------------------------------------- #
class _FakeRequest:
    """Just enough of starlette's Request for the header-reading helpers."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = {k.lower(): v for k, v in headers.items()}


def test_apim_header_is_the_billed_subscription():
    """Layer A: APIM stamps the tenant; the shared hub key cannot identify it."""
    req = _FakeRequest(
        {
            "authorization": "Bearer shared-hub-key",
            "x-tf-subscription": "vk_alice",
            "x-tf-api": "llm-anthropic",
            "x-tf-request-id": "apim-req-1",
        }
    )
    ident = server._extract_tenant(req)
    assert ident["subscription"] == "vk_alice"
    assert ident["api_id"] == "llm-anthropic"
    # The Cosmos document key comes from APIM, which is what makes re-imports
    # of the same event idempotent instead of duplicating rows.
    assert ident["request_id"] == "apim-req-1"
    assert ident["via_apim"] == "1"


def test_two_tenants_sharing_one_hub_key_are_still_separable():
    """The point of the whole header injection: same credential, two bills."""
    common = {"authorization": "Bearer shared-hub-key"}
    a = server._extract_tenant(_FakeRequest({**common, "x-tf-subscription": "vk_a"}))
    b = server._extract_tenant(_FakeRequest({**common, "x-tf-subscription": "vk_b"}))
    assert a["subscription"] != b["subscription"]
    # ...even though the key fingerprint is necessarily identical.
    assert a["client_key_fp"] == b["client_key_fp"]


def test_direct_caller_without_apim_falls_back_to_key_fingerprint():
    req = _FakeRequest({"x-api-key": "direct-key"})
    ident = server._extract_tenant(req)
    assert ident["via_apim"] == ""
    assert ident["subscription"] == ident["client_key_fp"]
    # A locally minted id, so the import side still has a unique document key.
    assert ident["request_id"]


def test_key_fingerprint_never_leaks_the_key():
    """Events land in Event Hub -> blobs -> Cosmos; a raw key must not travel."""
    fp = server._key_fingerprint("super-secret-key")
    assert fp is not None
    assert "super-secret-key" not in fp
    assert fp == server._key_fingerprint("super-secret-key")  # stable
    assert fp != server._key_fingerprint("super-secret-kez")
    assert server._key_fingerprint(None) is None


def test_end_user_from_both_vendor_conventions():
    """Layer B: optional by nature — absent is normal, not an error."""
    assert server._extract_end_user({"metadata": {"user_id": "u-42"}}) == "u-42"
    assert server._extract_end_user({"user": "u-42"}) == "u-42"
    assert server._extract_end_user({"metadata": {"user_id": "  u-42  "}}) == "u-42"
    assert server._extract_end_user({"messages": []}) is None
    assert server._extract_end_user({"user": ""}) is None
    assert server._extract_end_user(None) is None


# --------------------------------------------------------------------------- #
# Emitter identity (hub_id)                                                    #
# --------------------------------------------------------------------------- #
# Not caller identity but its counterpart: WHICH hub served the call. Every hub
# publishes into one shared Event Hub, so without this the records cannot be
# split back out per upstream GitHub account.
from hub import config as hub_config  # noqa: E402
from hub import eventhub  # noqa: E402


def _envelope_with_hub_id(value: str | None) -> dict:
    """Run _envelope under a given TF_HUB_ID, restoring the environment after.

    Deliberately not the monkeypatch fixture: settings are lru_cached, so the
    cache has to be cleared on the way in AND on the way out, and doing that
    explicitly is clearer than a fixture that hides half of it.
    """
    import os

    previous = os.environ.get("TF_HUB_ID")
    try:
        if value is None:
            os.environ.pop("TF_HUB_ID", None)
        else:
            os.environ["TF_HUB_ID"] = value
        hub_config.get_settings.cache_clear()
        return eventhub._envelope({"request_id": "r-1", "subscription": "vk_a"})
    finally:
        if previous is None:
            os.environ.pop("TF_HUB_ID", None)
        else:
            os.environ["TF_HUB_ID"] = previous
        hub_config.get_settings.cache_clear()


def test_every_event_carries_the_hub_that_emitted_it():
    env = _envelope_with_hub_id("gha_p2test01")
    assert env["hub_id"] == "gha_p2test01"
    # The record it wraps is passed through untouched.
    assert env["request_id"] == "r-1"
    assert env["subscription"] == "vk_a"


def test_unconfigured_hub_id_is_null_not_an_empty_string():
    """One absent case for the import side, not two."""
    assert _envelope_with_hub_id(None)["hub_id"] is None
    assert _envelope_with_hub_id("")["hub_id"] is None
    assert _envelope_with_hub_id("   ")["hub_id"] is None


def test_producer_callbacks_are_coroutines():
    """The *aio* buffered producer `await`s on_success/on_error.

    Written after a plain `def` shipped: the SDK awaited the returned None,
    raised TypeError inside its own send task, and swallowed it. Sends kept
    working, so nothing looked broken — but `_on_error` never ran either, which
    silently pinned `dropped_count()` at 0 and left a failing billing feed with
    no signal at all. A signature check is the only cheap way to catch this;
    the failure mode produces no exception on our side.
    """
    import inspect

    assert inspect.iscoroutinefunction(eventhub._on_success)
    assert inspect.iscoroutinefunction(eventhub._on_error)


async def test_on_error_counts_every_event_it_is_handed():
    """dropped_count() is the /api/status health signal; it must actually move."""
    before = eventhub.dropped_count()
    await eventhub._on_error([object(), object()], "0", RuntimeError("broker down"))
    assert eventhub.dropped_count() == before + 2
    # A non-iterable batch still counts as one loss rather than crashing.
    await eventhub._on_error(None, "0", RuntimeError("boom"))
    assert eventhub.dropped_count() == before + 3
