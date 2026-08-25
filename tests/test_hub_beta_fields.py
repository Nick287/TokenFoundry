"""Beta-gated request fields: forward when the header says to, strip otherwise.

The hub forwards `/v1/messages` verbatim, so any field the Copilot backend does
not recognise fails the whole request. Four fields were being stripped for that
reason. Testing them individually against the backend on 2026-08-26 showed the
list conflated two different situations:

    fallbacks            400 "Extra inputs are not permitted"  even WITH header
    mcp_servers          400 "unsupported beta header(s): mcp-client-..."
    container            400 "unsupported beta header(s): code-execution-..."
    context_management   400 without header, but 200 WITH it

The first three are genuinely missing from the backend. The fourth was only
missing its header — which the hub was also dropping, so the field could never
have worked. Stripping then hid the evidence: the client got a 200 and believed
its context editing had applied.

That matters because Claude Code sends `context_management` on every turn. The
cost of the old behaviour was every long agent session silently losing the
cleanup that keeps it inside the context window.

These tests pin the distinction, since both halves look identical from outside:
a stripped field and an unsupported field both produce a working 200.
"""

import sys
from pathlib import Path

import pytest

# Same bootstrap as tests/test_hub_eventhub_reliability.py: the vendored hub is
# a separate tree, so its package root has to be on sys.path, and importorskip
# keeps the suite green in environments without the hub's dependencies.
_HUB_ROOT = Path(__file__).resolve().parent.parent / "vendored" / "gitmodel-hub"
if str(_HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(_HUB_ROOT))

aa = pytest.importorskip(
    "hub.anthropic_adapter",
    reason="vendored hub deps not installed in this environment",
)

BETA = "context-management-2025-06-27"
CM = {"edits": [{"type": "clear_tool_uses_20250919"}]}


def _payload(**over) -> dict:
    base = {"model": "claude-sonnet-4.6", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    base.update(over)
    return base


# --- the conditional half ----------------------------------------------------


def test_context_management_survives_when_its_beta_header_is_present():
    """The whole point of the change. Upstream accepts the field with this
    header, so passing it through is what makes the feature work at all."""
    payload, dropped = aa.strip_unsupported(_payload(context_management=CM), BETA)
    assert "context_management" in payload
    assert dropped == []


def test_context_management_is_stripped_without_the_header():
    """Forwarding it bare would 400 the entire request. A degraded answer beats
    no answer, so the fallback stays."""
    payload, dropped = aa.strip_unsupported(_payload(context_management=CM), None)
    assert "context_management" not in payload
    assert dropped == ["context_management"]


def test_a_different_beta_does_not_unlock_it():
    """Presence of *some* beta header is not the condition — the matching one is."""
    payload, dropped = aa.strip_unsupported(
        _payload(context_management=CM), "fast-mode-2026-02-01")
    assert "context_management" not in payload
    assert dropped == ["context_management"]


def test_multi_value_beta_header_is_parsed_as_a_list():
    """`anthropic-beta` can carry several betas comma-separated. A substring
    test would pass here by accident and fail on a prefix collision; parsing is
    what makes it correct."""
    payload, dropped = aa.strip_unsupported(
        _payload(context_management=CM), f"fast-mode-2026-02-01, {BETA} ")
    assert "context_management" in payload
    assert dropped == []


def test_default_argument_keeps_the_old_safe_behaviour():
    """Any caller not yet updated to pass the header must still get the strip —
    otherwise adding this parameter would start 400-ing their requests."""
    payload, dropped = aa.strip_unsupported(_payload(context_management=CM))
    assert "context_management" not in payload


# --- the unconditional half --------------------------------------------------


def test_genuinely_unsupported_fields_are_stripped_even_with_their_headers():
    """These were each tested with their own documented beta and still refused.
    They must not follow context_management out of the list."""
    payload, dropped = aa.strip_unsupported(
        _payload(fallbacks=[{"model": "claude-opus-4-8"}],
                 mcp_servers=[{"type": "url", "url": "x", "name": "y"}],
                 container="c"),
        "server-side-fallback-2026-06-01,mcp-client-2025-11-20,skills-2025-10-02")
    for field in ("fallbacks", "mcp_servers", "container"):
        assert field not in payload, field
    assert set(dropped) == {"fallbacks", "mcp_servers", "container"}


# --- shape guarantees --------------------------------------------------------


def test_untouched_payload_is_returned_unchanged():
    """The common path must not allocate a copy."""
    p = _payload()
    out, dropped = aa.strip_unsupported(p, BETA)
    assert out is p
    assert dropped == []


def test_input_is_never_mutated():
    p = _payload(context_management=CM)
    aa.strip_unsupported(p, None)
    assert "context_management" in p


def test_other_fields_are_preserved():
    p = _payload(context_management=CM, temperature=0.5, tools=[])
    out, _ = aa.strip_unsupported(p, None)
    assert out["temperature"] == 0.5
    assert out["tools"] == []
    assert out["model"] == "claude-sonnet-4.6"


# --- the header itself is filtered, not forwarded --------------------------- #
#
# The first version of this change forwarded the caller's `anthropic-beta`
# verbatim. That shipped and immediately broke every Claude Code request:
#
#     400 "unsupported beta header(s): advisor-tool-2026-03-01"
#
# The backend validates the list ALL-OR-NOTHING, so one unknown entry fails the
# whole request — and Claude Code sends several betas at once. These tests exist
# because that regression was invisible to every test above: the field-level
# logic was correct the entire time.


def test_claude_codes_multi_beta_header_is_filtered_not_rejected():
    """The exact shape that broke: one supported beta alongside unsupported
    ones. Only the supported one may go upstream."""
    out = aa.forwardable_betas(
        "context-management-2025-06-27,advisor-tool-2026-03-01,"
        "fine-grained-tool-streaming-2025-05-14")
    # fine-grained-tool-streaming is unverified, so it is withheld too.
    assert out == {"context-management-2025-06-27"}


def test_an_entirely_unsupported_header_yields_nothing():
    """Nothing forwarded means no header sent at all — the pre-change
    behaviour, which worked."""
    assert aa.forwardable_betas("advisor-tool-2026-03-01") == set()


def test_absent_header_yields_nothing():
    assert aa.forwardable_betas(None) == set()
    assert aa.forwardable_betas("") == set()


def test_allowlist_holds_only_verified_betas():
    """Guard against additions made from documentation rather than from a live
    upstream response. Copilot supports its own subset, so 'Anthropic documents
    it' is not evidence — each of these answered 200 when sent alone."""
    assert aa.FORWARDABLE_BETAS == frozenset({
        "claude-code-20250219",
        "context-1m-2025-08-07",
        "context-management-2025-06-27",
        "effort-2025-11-24",
        "fallback-credit-2026-06-01",
        "interleaved-thinking-2025-05-14",
        "mid-conversation-system-2026-04-07",
        "prompt-caching-scope-2026-01-05",
        "thinking-token-count-2026-05-13",
    })


def test_the_one_refused_beta_stays_out():
    """advisor-tool is the only one of Claude Code's ten the backend rejects,
    and the rejection fails the WHOLE request — so it must never be forwarded,
    however many supported betas ride alongside it."""
    assert "advisor-tool-2026-03-01" not in aa.FORWARDABLE_BETAS
    forwarded = aa.forwardable_betas(
        "context-1m-2025-08-07,advisor-tool-2026-03-01,effort-2025-11-24")
    assert forwarded == {"context-1m-2025-08-07", "effort-2025-11-24"}


def test_whitespace_and_ordering_do_not_matter():
    assert aa.forwardable_betas(" advisor-tool-2026-03-01 , context-management-2025-06-27 ") == {
        "context-management-2025-06-27"
    }
