"""APIM policy-generation tests — pure string assertions, no Azure calls.

Covers what the provider policies must and must not contain:
  * the caller-identity headers the hub reads for chargeback, which MUST be
    override-stamped so a client cannot bill somebody else;
  * the absence of the retired outbound Cosmos write (billing moved to the
    hub -> Event Hub -> import path, which also covers streaming);
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
    """An ApimProvisioner without running __init__ (no settings, no Azure
    client). The policy builder reads no instance state beyond class attrs."""
    return object.__new__(ApimProvisioner)


# --- inbound: caller identity for chargeback ---------------------------------


def test_policy_injects_caller_identity_headers():
    """The hub sees one shared credential for every APIM tenant, so these
    headers are the only way it can attribute a request. All three must be
    present on every provider API."""
    p = _provisioner()
    for provider in ("openai", "azure", "anthropic", "google"):
        xml = p._build_provider_policy("be-1", provider)
        assert 'name="x-tf-subscription"' in xml
        assert 'name="x-tf-api"' in xml
        assert 'name="x-tf-request-id"' in xml
        assert "context.Subscription?.Id" in xml
        assert "context.RequestId.ToString()" in xml


def test_identity_headers_cannot_be_spoofed_by_the_client():
    """Every identity header must be `exists-action="override"`. With
    "skip"/"append" a client could send its own x-tf-subscription and be billed
    as another tenant."""
    p = _provisioner()
    xml = p._build_provider_policy("be-1", "openai")
    for header in ("x-tf-subscription", "x-tf-api", "x-tf-request-id"):
        idx = xml.index(f'name="{header}"')
        assert 'exists-action="override"' in xml[idx : idx + 120], header


# --- outbound: the Cosmos write is gone --------------------------------------


def test_outbound_no_longer_writes_cosmos():
    """The outbound send-one-way-request was removed: it could not read SSE
    bodies, so it billed nothing for streaming calls. Billing now starts at the
    hub. Its managed-identity token fetch must be gone too."""
    p = _provisioner()
    for provider in ("openai", "azure", "anthropic", "google"):
        xml = p._build_provider_policy("be-1", provider)
        assert "send-one-way-request" not in xml
        assert "cosmosToken" not in xml
        assert "cosmos.azure.com" not in xml
        assert "/docs" not in xml


def test_nothing_in_outbound_reads_the_response_body():
    """Reading the response body at the gateway flattens streaming.

    `context.Response.Body.As<JObject>()` has to have the WHOLE body before it
    can parse, so APIM holds the response — headers included — until upstream
    finishes. Measured on dev-18: first byte at the client was 0.94s straight to
    the hub and 44.08s through APIM, with the entire body arriving in one burst.
    Deleting the one metadata that did this brought it to 3.08s with bytes
    spread over 71% of the elapsed time.

    This is the second time the same mistake shipped: the outbound
    send-one-way-request was removed for exactly this reason (see the test
    above) while `_USAGE_TRACE` kept its own copy of the same call. Hence a test
    on the WHOLE outbound section rather than on one element — the next thing to
    reach for the body should fail here, whatever it is called.
    """
    p = _provisioner()
    for provider in ("openai", "azure", "anthropic", "google"):
        xml = p._build_provider_policy("be-1", provider)
        outbound = xml.split("<outbound>", 1)[1].split("</outbound>", 1)[0]
        assert "Response.Body" not in outbound, provider
        assert "As&lt;Newtonsoft" not in outbound, provider


def test_usage_trace_still_carries_the_identity_metadata():
    """Guard against over-correcting. The trace is the real-time observability
    line; only the body-reading field had to go, and the fields that identify
    WHICH call this was must survive."""
    p = _provisioner()
    xml = p._build_provider_policy("be-1", "anthropic")
    for field in ("requestId", "api", "subscription", "model", "hub"):
        assert f'name="{field}"' in xml, field


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


def test_policy_emits_model_dimension():
    """llm-emit-token-metric must carry a `model` dimension sourced from the
    request body (captured into tfModel), on top of subscription + api, so usage
    can be broken down per model. Verified live on dev-a03."""
    p = _provisioner()
    for provider in ("openai", "anthropic", "google"):
        xml = p._build_provider_policy("be-1", provider)
        # the capture variable + the dimension both present
        assert 'name="tfModel"' in xml
        assert '<dimension name="model"' in xml
        # still has the original two dimensions
        assert '<dimension name="subscription"' in xml
        assert '<dimension name="api"' in xml
        # body read must preserve content so the backend still gets the model
        assert "preserveContent:true" in xml


def test_policy_emits_usage_trace():
    """The outbound policy emits a per-call `trace` (log-class telemetry, not
    pre-aggregated) carrying requestId/model/subscription/api. Verified on
    dev-a03: 5 non-stream calls -> 5 traces, itemCount=1 (no sampling). This is
    the App Insights observability line and is deliberately NOT the billing
    source — billing rides the hub -> Event Hub path.

    It used to assert a `usage` field too, read off the response body. That
    assertion was written from non-streaming runs, where the cost is invisible:
    parsing the body forces APIM to buffer the whole response, which turned
    streaming into a 44-second wait for the first byte (dev-18). The field is
    gone; token counts come from llm-emit-token-metric and from Cosmos.
    """
    p = _provisioner()
    for provider in ("openai", "anthropic", "google"):
        xml = p._build_provider_policy("be-1", provider)
        # trace present with our source + per-call fields
        assert '<trace source="tokenfoundry-usage"' in xml
        assert 'name="requestId"' in xml
        assert 'name="model"' in xml


def test_breaker_rules_cover_5xx_and_upstream_429():
    """Azure allows exactly ONE circuit-breaker rule per backend, but its
    failureCondition can list multiple status ranges. The single rule trips on a
    SINGLE upstream 429 (out of TPM -> failover) OR a single 5xx (unhealthy),
    with a short 60s trip. Our own per-key llm-token-limit 429 is rejected in
    inbound and never reaches the backend, so it can't trip this."""
    rules = ApimProvisioner._breaker_rules()
    # Azure hard limit: exactly one rule.
    assert len(rules) == 1
    rule = rules[0]
    ranges = {(r.min, r.max) for r in rule.failure_condition.status_code_ranges}
    # covers both upstream 429 and 5xx in the one rule
    assert (429, 429) in ranges
    assert (500, 599) in ranges
    # a single strike trips it, for a short 60s eject (transient limiter)
    assert rule.failure_condition.count == 1
    assert rule.trip_duration.total_seconds() <= 300


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
