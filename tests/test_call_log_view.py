"""Call-log row shaping — what the portal is allowed to render as fact.

`_to_record_view` flattens one Cosmos usage document into the row the call log
shows. These tests are less about field plumbing than about the two places where
a missing distinction would make the portal *lie*:

  * a $0.00 that means "upstream priced nothing" must not look like a $0.00 that
    means "this call was free", and
  * an ESTIMATED token count must not be presented like a measured one.

Both are the same failure this project keeps re-learning: a zero rendered
without its provenance reads as authoritative. The importer already refuses to
guess a price from a local table for exactly this reason; the flag it writes is
only useful if the view carries it through.

Hermetic — plain dicts in, plain dicts out, no Azure.
"""

from app.api.usage import _extract_cache_write, _to_record_view


def _doc(**over) -> dict:
    """A realistic post-cutover document, overridable per test."""
    base = {
        "ts": "2026-08-15T05:46:36Z",
        "subscription": "vk_abc",
        "route": "claude-opus-4.8",
        "api": "llm-anthropic",
        "status": 200,
        "prompt_tok": 100,
        "completion_tok": 50,
        "cached_tok": 20,
        "cache_write_tok": 7,
        "cost_usd": 0.0123,
        "billed_usd": 0.0150,
        "cost_source": "copilot_usage",
        "estimated": False,
        "streamed": True,
    }
    base.update(over)
    return base


# --- the fields the call log gained -----------------------------------------


def test_view_carries_cache_write_and_cost():
    v = _to_record_view(_doc())
    assert v["cache_write_tok"] == 7
    assert v["cost_usd"] == 0.0123
    assert v["billed_usd"] == 0.0150


def test_billed_is_reported_separately_from_cost():
    """Markup lives between the two. Collapsing them would hide what we add on
    top of upstream's price."""
    v = _to_record_view(_doc(cost_usd=1.0, billed_usd=1.25))
    assert v["cost_usd"] == 1.0
    assert v["billed_usd"] == 1.25


# --- the two distinctions worth protecting ----------------------------------


def test_unpriced_zero_is_distinguishable_from_a_real_zero():
    """Both rows show $0.00. Only `cost_source` says which one is a fact."""
    real = _to_record_view(_doc(cost_usd=0.0, billed_usd=0.0))
    unknown = _to_record_view(_doc(cost_usd=0.0, billed_usd=0.0,
                                   cost_source="unpriced"))

    assert real["cost_usd"] == unknown["cost_usd"] == 0.0
    assert real["cost_source"] == "copilot_usage"
    assert unknown["cost_source"] == "unpriced"


def test_estimated_rows_are_flagged():
    """The hub estimates when upstream returns no usage. Such a row must not be
    used to settle a billing dispute, so it cannot render like a measured one."""
    assert _to_record_view(_doc(estimated=True))["estimated"] is True
    assert _to_record_view(_doc(estimated=False))["estimated"] is False


def test_missing_flags_do_not_become_silent_truths():
    """Pre-cutover documents carry none of these keys. `estimated` defaults to
    False (the honest reading: we have no evidence it was estimated), but
    `cost_source` stays None so the portal shows "unknown" rather than claiming
    upstream priced it."""
    v = _to_record_view({"ts": "t", "route": "m"})
    assert v["estimated"] is False
    assert v["cost_source"] is None
    assert v["cost_usd"] == 0.0
    assert v["cache_write_tok"] == 0


# --- cache-write extraction across shapes -----------------------------------


def test_cache_write_from_the_flat_importer_shape():
    assert _extract_cache_write({"cache_write_tok": 42}) == 42


def test_cache_write_from_a_raw_anthropic_response():
    doc = {"raw_response": {"usage": {"cache_creation_input_tokens": 9}}}
    assert _extract_cache_write(doc) == 9


def test_cache_write_is_zero_rather_than_invented_for_openai():
    """OpenAI's schema has no cache-write equivalent. 0 is the truthful answer;
    borrowing another field would manufacture a number."""
    doc = {"raw_response": {"usage": {"prompt_tokens": 10,
                                      "prompt_tokens_details": {"cached_tokens": 4}}}}
    assert _extract_cache_write(doc) == 0


def test_explicit_zero_beats_the_raw_fallback():
    """The importer writes cache_write_tok: 0 deliberately for hub-normalized
    rows. That 0 is an answer, not a missing key, so it must win over any
    raw_response sniffing."""
    doc = {"cache_write_tok": 0,
           "raw_response": {"usage": {"cache_creation_input_tokens": 99}}}
    assert _extract_cache_write(doc) == 0
