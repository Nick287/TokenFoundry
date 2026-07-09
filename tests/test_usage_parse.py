"""Hermetic tests for usage token parsing — no Azure, no network.

Guards the _parse_usage_tokens tuple contract (prompt, completion, cached,
creation, reasoning) across provider usage shapes. A tuple-order regression here
silently zeroed `completion` in the per-hub breakdown once (caught on dev-a03),
so these lock the field mapping down.
"""

from app.services.usage_ingest import _parse_usage_tokens


def test_anthropic_usage_shape():
    u = (
        '{"input_tokens":13,"output_tokens":20,'
        '"cache_read_input_tokens":5,"cache_creation_input_tokens":7}'
    )
    prompt, completion, cached, creation, reasoning = _parse_usage_tokens(u)
    assert prompt == 13
    assert completion == 20
    assert cached == 5
    assert creation == 7
    assert reasoning == 0


def test_openai_chat_usage_shape():
    u = (
        '{"prompt_tokens":100,"completion_tokens":50,'
        '"prompt_tokens_details":{"cached_tokens":30},'
        '"completion_tokens_details":{"reasoning_tokens":12}}'
    )
    prompt, completion, cached, creation, reasoning = _parse_usage_tokens(u)
    assert prompt == 100
    assert completion == 50
    assert cached == 30
    assert creation == 0
    assert reasoning == 12


def test_streaming_body_read_failed_is_zeros():
    assert _parse_usage_tokens("BODY_READ_FAILED") == (0, 0, 0, 0, 0)
    assert _parse_usage_tokens("NO_USAGE_KEY") == (0, 0, 0, 0, 0)


def test_garbage_and_empty_are_zeros():
    assert _parse_usage_tokens(None) == (0, 0, 0, 0, 0)
    assert _parse_usage_tokens("") == (0, 0, 0, 0, 0)
    assert _parse_usage_tokens("not json") == (0, 0, 0, 0, 0)
    assert _parse_usage_tokens("[1,2,3]") == (0, 0, 0, 0, 0)  # JSON but not a dict


def test_completion_is_second_element_not_creation():
    """Regression: the per-hub breakdown once unpacked the tuple as
    (p, c, cr, comp, reason), mapping `completion` to the creation slot and
    zeroing it. Assert completion is position 1 and distinct from creation."""
    u = '{"output_tokens":42,"cache_creation_input_tokens":0}'
    result = _parse_usage_tokens(u)
    assert result[1] == 42  # completion
    assert result[3] == 0  # creation
