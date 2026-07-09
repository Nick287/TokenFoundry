"""Hermetic tests for usage token parsing — no Azure, no network.

Guards _parse_usage_tokens across provider usage shapes. It returns a dict of
token counts (keyed like AppInsightsUsage._TOKEN_KEYS minus `total`), covering
all 9+ token types APIM/providers report. A field-mapping regression here once
silently zeroed `completion` (caught on dev-a03), so these lock the mapping.
"""

from app.services.usage_ingest import _parse_usage_tokens


def test_anthropic_usage_shape():
    u = (
        '{"input_tokens":13,"output_tokens":20,'
        '"cache_read_input_tokens":5,"cache_creation_input_tokens":7}'
    )
    t = _parse_usage_tokens(u)
    assert t["prompt"] == 13
    assert t["completion"] == 20
    assert t["cached"] == 5
    assert t["cache_creation"] == 7  # anthropic cache-WRITE, billed higher
    assert t["reasoning"] == 0


def test_openai_chat_usage_shape():
    u = (
        '{"prompt_tokens":100,"completion_tokens":50,'
        '"prompt_tokens_details":{"cached_tokens":30,"audio_tokens":4},'
        '"completion_tokens_details":{"reasoning_tokens":12,'
        '"accepted_prediction_tokens":8,"rejected_prediction_tokens":3,'
        '"audio_tokens":6}}'
    )
    t = _parse_usage_tokens(u)
    assert t["prompt"] == 100
    assert t["completion"] == 50  # reasoning is a SUBSET of completion, not added
    assert t["cached"] == 30
    assert t["reasoning"] == 12
    assert t["accepted_prediction"] == 8
    assert t["rejected_prediction"] == 3
    assert t["prompt_audio"] == 4
    assert t["completion_audio"] == 6
    assert t["cache_creation"] == 0  # openai has no cache-write field


def test_google_top_level_reasoning_tokens():
    """Google (gemini) reports thinking tokens at the TOP level as
    reasoning_tokens with completion_tokens=0 — the whole output is reasoning.
    Must be picked up AND folded into completion so prompt+completion matches the
    provider total_tokens."""
    u = (
        '{"completion_tokens":0,"prompt_tokens":17,'
        '"prompt_tokens_details":{"cached_tokens":0},'
        '"total_tokens":63,"reasoning_tokens":46}'
    )
    t = _parse_usage_tokens(u)
    assert t["prompt"] == 17
    assert t["reasoning"] == 46
    assert t["completion"] == 46  # folded so prompt+completion == 63
    assert t["prompt"] + t["completion"] == 63


def test_streaming_body_read_failed_is_zeros():
    for bad in ("BODY_READ_FAILED", "NO_USAGE_KEY", None, "", "not json", "[1,2]"):
        t = _parse_usage_tokens(bad)
        assert all(v == 0 for v in t.values()), f"{bad!r} should be all zeros"


def test_completion_not_confused_with_cache_creation():
    """Regression: the per-hub breakdown once mis-mapped completion to the cache-
    creation slot and zeroed it. With a dict return this can't happen, but assert
    the two are independent."""
    u = '{"output_tokens":42,"cache_creation_input_tokens":9}'
    t = _parse_usage_tokens(u)
    assert t["completion"] == 42
    assert t["cache_creation"] == 9
