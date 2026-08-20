"""Model classification: protocol vs vendor, kept apart on purpose.

`provider` decides ROUTING — which APIM API and pool a model is served from,
which auth header the client sends, which paths exist. `vendor` is the company
that made the model and exists only so the portal can answer "whose models are
we spending on".

Conflating them breaks in both directions:
  * routing by vendor would have the gateway build an `llm-xai` API and pool
    that no upstream endpoint answers — Grok is served over the OpenAI schema;
  * displaying the protocol as the vendor is what the portal did before, which
    filed Kimi and Grok under "OpenAI" and gave operators the wrong answer.

Every model named here was probed live through the dev-19 gateway on
2026-08-20, so these are observations rather than assumptions.
"""

from app.api.github_accounts import _provider_for_model
from app.models.enums import vendor_for_model

# --- protocol (routing) ------------------------------------------------------


def test_grok_and_kimi_route_over_the_openai_protocol():
    """Both speak the OpenAI-compatible schema, so they belong on llm-openai —
    NOT on a provider of their own."""
    for model in ("grok-4.5", "grok-4.6", "kimi-k3", "kimi-k2.7-code"):
        assert _provider_for_model(model) == "openai", model


def test_the_established_families_are_unchanged():
    assert _provider_for_model("claude-opus-5") == "anthropic"
    assert _provider_for_model("gpt-4o-mini") == "openai"
    assert _provider_for_model("gemini-3.5-flash") == "google"


def test_models_without_a_client_facing_api_are_skipped():
    """Registering these would create routes that answer nothing. `mai-*` and
    `trajectory-*` are upstream-internal; `text-embedding-*` is not chat."""
    for model in ("mai-code-1-flash-picker", "trajectory-compaction",
                  "text-embedding-3-small"):
        assert _provider_for_model(model) is None, model


# --- vendor (display) --------------------------------------------------------


def test_vendor_is_the_company_not_the_protocol():
    """The whole point of the field. Same protocol, three different vendors."""
    assert vendor_for_model("gpt-4o-mini") == "OpenAI"
    assert vendor_for_model("grok-4.6") == "xAI"
    assert vendor_for_model("kimi-k3") == "Moonshot"
    # ...and all three route identically:
    assert (
        _provider_for_model("gpt-4o-mini")
        == _provider_for_model("grok-4.6")
        == _provider_for_model("kimi-k3")
        == "openai"
    )


def test_vendor_covers_the_families_we_serve():
    assert vendor_for_model("claude-opus-5") == "Anthropic"
    assert vendor_for_model("gemini-3.5-flash") == "Google"
    assert vendor_for_model("mai-code-1-flash-picker") == "Microsoft"


def test_unknown_models_get_no_vendor_rather_than_a_guess():
    """A model we cannot identify shows its raw id in the portal. Inventing a
    vendor would put someone else's name on the spend."""
    assert vendor_for_model("trajectory-compaction") is None
    assert vendor_for_model("some-future-model-9") is None


def test_matching_is_case_insensitive():
    assert vendor_for_model("GPT-4o-Mini") == "OpenAI"
    assert _provider_for_model("Claude-Opus-5") == "anthropic"
