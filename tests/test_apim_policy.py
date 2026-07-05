"""APIM policy-generation tests — pure string assertions, no Azure calls.

Covers the streaming (SSE) support wired into the provider policies:
  * outbound Cosmos write is gated off for `text/event-stream` responses
    (so streaming passes through token-by-token instead of being buffered);
  * the `chat` operation policy injects stream_options.include_usage for
    OpenAI-schema providers (openai/azure) and nobody else;
  * the injection is scoped to the `chat` op — the Responses API (`responses`
    op) must never receive stream_options.

These build the policy XML directly; `__init__` (which calls get_settings and
would touch Azure) is bypassed via object.__new__, so the tests stay hermetic
like test_billing.py.
"""

from app.services.apim_provisioner import _CHAT_USAGE_PROVIDERS, ApimProvisioner


def _provisioner() -> ApimProvisioner:
    """An ApimProvisioner with just the Cosmos attrs the policy builder reads,
    without running __init__ (no settings, no Azure client)."""
    p = object.__new__(ApimProvisioner)
    p._cosmos_endpoint = "https://cosmos.example.com"
    p._cosmos_db = "tokenfoundry"
    p._cosmos_container = "usage"
    return p


# --- outbound: streaming responses are not persisted to Cosmos ---------------


def test_outbound_excludes_event_stream_for_all_providers():
    p = _provisioner()
    for provider in ("openai", "azure", "anthropic", "google"):
        xml = p._build_provider_policy("be-1", provider)
        # The Cosmos write still exists for non-streaming calls...
        assert "send-one-way-request" in xml
        # ...but is gated on the response NOT being an SSE stream.
        assert "text/event-stream" in xml
        assert 'Headers.GetValueOrDefault(&quot;Content-Type&quot;,&quot;&quot;)' in xml


def test_provider_policy_is_provider_agnostic():
    """The API-level policy body is identical regardless of provider — the
    provider-specific behavior lives in the operation-level chat policy."""
    p = _provisioner()
    base = p._build_provider_policy("be-1", "anthropic")
    for provider in ("openai", "azure", "google"):
        assert p._build_provider_policy("be-1", provider) == base


# --- chat op: include_usage injection scope ----------------------------------


def test_chat_stream_policy_injects_include_usage():
    xml = ApimProvisioner._build_chat_stream_policy()
    assert "stream_options" in xml
    assert "include_usage" in xml
    # Only rewrites when the request asked for streaming.
    assert "stream" in xml
    # Inherits the API-level policy rather than replacing it.
    assert "<base />" in xml


def test_only_openai_schema_providers_get_chat_injection():
    """The provider set that receives the chat injection is exactly
    openai + azure — never anthropic/google."""
    assert _CHAT_USAGE_PROVIDERS == ("openai", "azure")
    assert "anthropic" not in _CHAT_USAGE_PROVIDERS
    assert "google" not in _CHAT_USAGE_PROVIDERS


def test_api_level_policy_has_no_stream_options():
    """stream_options must NOT live in the API-level policy (it would then apply
    to the responses op too, which rejects the field). It belongs only in the
    chat operation policy."""
    p = _provisioner()
    for provider in ("openai", "azure"):
        assert "stream_options" not in p._build_provider_policy("be-1", provider)


# --- per-key token limits: TPM expression + quota <choose> tiers -------------


def test_policy_references_key_limits_named_value():
    """The policy reads per-key limits from the shared named value, so it must
    reference {{tf-key-token-limits}} (APIM named value syntax)."""
    p = _provisioner()
    xml = p._build_provider_policy("be-1", "openai")
    assert "{{tf-key-token-limits}}" in xml


def test_policy_tpm_is_an_expression_not_hardcoded():
    """tokens-per-minute must be a policy expression reading the key's value, not
    the old hard-coded 50000."""
    p = _provisioner()
    xml = p._build_provider_policy("be-1", "openai")
    assert 'tokens-per-minute="@(' in xml
    assert '"50000"' not in xml


def test_policy_quota_uses_choose_with_literal_tiers():
    """token-quota can't take an expression, so quota is a <choose> with one
    branch per tier carrying the LITERAL amount from TOKEN_QUOTA_AMOUNTS."""
    from app.models.enums import TOKEN_QUOTA_AMOUNTS

    p = _provisioner()
    xml = p._build_provider_policy("be-1", "openai")
    assert "<choose>" in xml
    for amount in TOKEN_QUOTA_AMOUNTS.values():
        assert f'token-quota="{amount}"' in xml
    # quota must NOT be an expression (APIM rejects that on llm-token-limit).
    assert 'token-quota="@(' not in xml


def test_limit_policy_still_provider_agnostic():
    """Adding the limit block must keep the API-level policy identical across
    providers (provider-specific behavior stays in the chat op policy)."""
    p = _provisioner()
    base = p._build_provider_policy("be-1", "anthropic")
    for provider in ("openai", "azure", "google"):
        assert p._build_provider_policy("be-1", provider) == base
