"""APIM provisioning: turn control-plane records into APIM objects.

Responsibilities (management plane only — NEVER reimplements gateway behavior):
  * create/suspend Subscriptions  (virtual keys)
  * ensure Products                (tenant boundary / package tier)
  * register model backends + aliases (ModelRoute -> Unified Model API)

Uses azure-mgmt-apimanagement with DefaultAzureCredential (managed identity in
cloud). Per the plan's risk note, batch and back off — do NOT call per request,
and tolerate config-propagation delay.

⚠️ Unified Model API is preview: the exact management shape for adding a
model/alias must be validated against a live instance (Phase 0 of impl). The
add_model_route method below is structured so that piece is swappable — it
writes the Backend (stable API) and records the alias mapping; wiring the alias
into the unified API is isolated in `_attach_alias`.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import httpx
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.mgmt.apimanagement import ApiManagementClient
from azure.mgmt.apimanagement.models import (
    ApiCreateOrUpdateParameter,
    BackendCircuitBreaker,
    BackendContract,
    BackendCredentialsContract,
    CircuitBreakerFailureCondition,
    CircuitBreakerRule,
    FailureStatusCodeRange,
    NamedValueCreateContract,
    OperationContract,
    PolicyContract,
    SubscriptionCreateParameters,
    SubscriptionKeyParameterNamesContract,
)

from app.config import get_settings
from app.models.enums import TOKEN_QUOTA_AMOUNTS

logger = logging.getLogger(__name__)

# Per-provider client-facing APIs. Each provider gets its own APIM API with a
# native subscription-key header (so the provider's own SDK works with minimal
# config), its own operations (paths), and a fixed backend. Adding a provider =
# adding an entry here. The shared backend per provider holds the real upstream
# credential (header auth) and a circuit breaker.
#   api_id  : APIM api id / path  (clients call {gateway}/{path}/...)
#   sub_header : subscription-key header name the provider SDK naturally sends
#   backend : shared backend id for this provider
#   auth_header : header name used to send the REAL upstream key to the backend
#   ops : list of (operation_id, url_template) operations to expose
PROVIDER_APIS: dict[str, dict] = {
    "anthropic": {
        "api_id": "llm-anthropic",
        "display": "LLM Anthropic",
        "sub_header": "x-api-key",
        "backend": "llm-anthropic",
        "auth_header": "x-api-key",
        "bearer": False,
        "ops": [("messages", "/v1/messages")],
    },
    "openai": {
        "api_id": "llm-openai",
        "display": "LLM OpenAI",
        "sub_header": "api-key",
        "backend": "llm-openai",
        "auth_header": "Authorization",
        "bearer": True,
        "ops": [
            ("chat", "/v1/chat/completions"),
            ("responses", "/v1/responses"),
        ],
    },
    "google": {
        "api_id": "llm-google",
        "display": "LLM Google",
        "sub_header": "api-key",
        "backend": "llm-google",
        "auth_header": "Authorization",
        "bearer": True,
        "ops": [("chat", "/v1/chat/completions")],
    },
    "azure": {
        # Azure OpenAI: client SDK sends the subscription key in `api-key`, and
        # the REAL upstream Azure key is also an `api-key` header (NOT a Bearer
        # token — that's the key difference from "openai"). Uses Azure's new
        # OpenAI-compatible surface (/openai/v1/...) so the deployment travels in
        # the request body `model`, matching the shared-backend dispatch model.
        "api_id": "llm-azure",
        "display": "LLM Azure OpenAI",
        "sub_header": "api-key",
        "backend": "llm-azure",
        "auth_header": "api-key",
        "bearer": False,
        "ops": [
            ("chat", "/openai/v1/chat/completions"),
            ("responses", "/openai/v1/responses"),
        ],
    },
}
_LLM_PRODUCTS = ("starter", "unlimited")

# Providers whose `chat` operation speaks the OpenAI Chat Completions schema, so
# a streaming request accepts `stream_options.include_usage` to make the backend
# emit a final usage chunk (needed for accurate llm-emit-token-metric counts).
# Anthropic Messages and Google have no such parameter; the OpenAI/Azure
# `responses` op uses the Responses API which rejects it — hence injection is
# scoped to the `chat` op only (see _build_chat_stream_policy).
_CHAT_USAGE_PROVIDERS = ("openai", "azure")

# --- Per-key token limits (llm-token-limit driven by a shared named value) ----
# One APIM named value holds a JSON map {subId: {t?, qt?, p?}} of every key's
# limits (t=tokens/min, qt=quota TIER label, p=period); the shared API-level
# policy parses it per request and looks up the calling subscription. Missing
# keys/fields fall back to sentinels = "effectively unlimited". The named value
# is loaded into gateway memory (no per-request network call); parse is µs.
#
# Why a tier LABEL for quota (not the number): APIM's llm-token-limit
# `token-quota` attribute REJECTS policy expressions (verified on dev-a01), so
# the amount can't come from the named value — it must be a literal baked into a
# policy <choose> branch. So the map stores the tier label ("small"/...) and the
# policy branches on it (see enums.TOKEN_QUOTA_AMOUNTS). `tokens-per-minute` and
# `token-quota-period` DO accept expressions, so those come from the map directly.
KEY_LIMITS_NV = "tf-key-token-limits"
# APIM named value values cap at ~4096 chars; each entry is ~40-60 chars, so this
# map holds ~60-70 keys. _merge_key_limit raises past this so an over-cap issue
# surfaces at key-issuance (502) rather than silently dropping a limit.
_KEY_LIMITS_MAX_BYTES = 4000
# Sentinels for "no limit of this kind" — large enough to never bite in practice.
_TPM_UNLIMITED = 1_000_000_000
_PERIOD_DEFAULT = "Yearly"


def _merge_key_limit(
    mapping: dict,
    sub_id: str,
    tpm: int | None,
    quota_tier: str | None,
    period: str | None,
) -> dict:
    """Return a NEW map with sub_id's limit entry set (or removed if all empty).

    Entry shape uses short keys to conserve the ~4096-char named value budget:
    {"t": tpm, "qt": quota_tier, "p": period}; absent fields are omitted. A
    quota_tier of "none" (or None) is treated as absent. Pure — no Azure calls —
    so it's unit-testable. Raises ValueError if the resulting map would exceed the
    named value size cap (surfaces at issuance, never silent)."""
    out = dict(mapping)
    entry: dict[str, object] = {}
    if tpm is not None:
        entry["t"] = tpm
    if quota_tier is not None and quota_tier != "none":
        entry["qt"] = quota_tier
    if period is not None:
        entry["p"] = period
    if entry:
        out[sub_id] = entry
    else:
        out.pop(sub_id, None)
    serialized = json.dumps(out, separators=(",", ":"))
    if len(serialized) > _KEY_LIMITS_MAX_BYTES:
        raise ValueError(
            f"per-key limits map would exceed {_KEY_LIMITS_MAX_BYTES} bytes "
            f"({len(serialized)}); too many keys have custom limits for the "
            f"single-named-value scheme — see docs/APIM-LLM-Gateway.md §4.5"
        )
    return out


def _remove_key_limit(mapping: dict, sub_id: str) -> dict:
    """Return a NEW map with sub_id's entry removed (idempotent). Pure."""
    out = dict(mapping)
    out.pop(sub_id, None)
    return out


class ApimProvisioner:
    # Capture the request body's `model` into a variable so llm-emit-token-metric
    # can emit it as a dimension (per-model usage breakdown). Read ONCE with
    # preserveContent:true so the backend still receives the body; falls back to
    # "unknown" if the body isn't JSON or has no model. Placed before the metric
    # in the inbound policy. Verified on dev-a03: the model dimension shows the
    # real model name (claude-opus-4.8, etc.) in App Insights customMetrics.
    _MODEL_VAR = (
        '<set-variable name="tfModel" value="@{ try { var b = '
        "context.Request.Body.As&lt;Newtonsoft.Json.Linq.JObject&gt;(preserveContent:true); "
        "return (b != null &amp;&amp; b[&quot;model&quot;] != null) "
        "? (string)b[&quot;model&quot;] : &quot;unknown&quot;; } "
        'catch { return &quot;unknown&quot;; } }" />'
    )

    # Per-call usage log as an App Insights `trace` (LOG-class telemetry: not
    # pre-aggregated, so every call is retained — unlike llm-emit-token-metric,
    # which pre-aggregates and drops rows). Emitted in OUTBOUND alongside — NOT
    # replacing — the Cosmos write, so both the durable billing store (Cosmos,
    # permanent) and the queryable per-call log (traces, ~30d) are populated.
    #
    # severity=information matches the diagnostic verbosity. requestId/model/
    # subscription/api come from context and always populate. `usage` reads the
    # response body best-effort: on a NON-STREAM response it's the full usage JSON
    # (input/output/cache tokens); on an SSE stream the body isn't a JObject so it
    # degrades to "BODY_READ_FAILED" (same limitation as the Cosmos write — SSE
    # token accounting rides on llm-emit-token-metric instead). Verified on
    # dev-a03: 5 non-stream calls => 5 traces with full usage; itemCount=1 (no
    # sampling). No reflection in the expression (APIM rejects it).
    #
    # `hub` = the REAL pool member (per-account hub) this call was routed to. This
    # is the ONLY way to attribute usage to a specific hub: context.Backend.Id is
    # the POOL id (verified on dev-a03), and llm-emit-token-metric's customMetrics
    # carry no operation_Id to join against dependencies. But session affinity
    # encodes the selected backend name as base64 in the SessionId cookie, so we
    # decode it: prefer the response Set-Cookie (fresh/non-sticky calls emit it),
    # fall back to the request Cookie (a sticky call reuses its pinned SessionId
    # and APIM does NOT re-emit Set-Cookie — verified on dev-a03). "unknown" if
    # neither is present or decode fails.
    _HUB_EXPR = (
        "@{ "
        "try "
        "{ "
        "string raw = &quot;&quot;; "
        "var sc = context.Response.Headers.GetValueOrDefault(&quot;Set-Cookie&quot;,&quot;&quot;); "
        "var src = sc.Contains(&quot;SessionId=&quot;) ? sc "
        ": context.Request.Headers.GetValueOrDefault(&quot;Cookie&quot;,&quot;&quot;); "
        "var i = src.IndexOf(&quot;SessionId=&quot;); "
        "if (i &lt; 0) { return &quot;unknown&quot;; } "
        "raw = src.Substring(i + 10); "
        "var semi = raw.IndexOf(';'); "
        "if (semi &gt;= 0) { raw = raw.Substring(0, semi); } "
        "raw = System.Uri.UnescapeDataString(raw); "
        "while (raw.Length % 4 != 0) { raw = raw + &quot;=&quot;; } "
        "return System.Text.Encoding.UTF8.GetString(System.Convert.FromBase64String(raw)); "
        "} "
        "catch { return &quot;unknown&quot;; } "
        "}"
    )
    _USAGE_TRACE = (
        '<trace source="tokenfoundry-usage" severity="information">'
        '<message>@("llm-usage " + context.Api.Id + " " + context.RequestId)</message>'
        '<metadata name="requestId" value="@(context.RequestId.ToString())" />'
        '<metadata name="api" value="@(context.Api.Id)" />'
        '<metadata name="subscription" value="@(context.Subscription?.Id ?? &quot;none&quot;)" />'
        '<metadata name="model" value="@(context.Variables.GetValueOrDefault&lt;string&gt;(&quot;tfModel&quot;, &quot;unknown&quot;))" />'
        f'<metadata name="hub" value="{_HUB_EXPR}" />'
        '<metadata name="usage" value="@{ try { var b = '
        "context.Response.Body.As&lt;Newtonsoft.Json.Linq.JObject&gt;(preserveContent:true); "
        "var u = b[&quot;usage&quot;] as Newtonsoft.Json.Linq.JObject; "
        "return u != null ? u.ToString(Newtonsoft.Json.Formatting.None) : &quot;NO_USAGE_KEY&quot;; } "
        'catch { return &quot;BODY_READ_FAILED&quot;; } }" />'
        "</trace>"
    )

    def __init__(self) -> None:
        s = get_settings()
        self._sub_id = s.azure_subscription_id
        self._rg = s.resource_group
        self._service = s.apim_service_name
        self._cosmos_endpoint = s.cosmos_endpoint.rstrip("/")
        self._cosmos_db = s.cosmos_database
        self._cosmos_container = s.cosmos_usage_container
        self._client: ApiManagementClient | None = None

    @property
    def client(self) -> ApiManagementClient:
        if self._client is None:
            self._client = ApiManagementClient(
                credential=DefaultAzureCredential(),
                subscription_id=self._sub_id,
            )
        return self._client

    # --- Products (tenant boundary / package tier) ---

    def ensure_product_for_tenant(self, tenant_id: str) -> str:
        """Return an APIM product id to back a tenant's subscriptions.

        MVP: reuse the built-in 'starter' product that APIM ships with (already
        published). The signature takes tenant_id so this can later create a
        dedicated per-tenant product without changing callers.
        """
        product_id = "starter"
        try:
            self.client.product.get(self._rg, self._service, product_id)
        except ResourceNotFoundError:
            logger.warning(
                "APIM product '%s' not found for tenant %s; subscriptions may fail",
                product_id,
                tenant_id,
            )
        return product_id

    # --- Per-provider client-facing LLM APIs ---

    def ensure_provider_api(self, provider: str, upstream_url: str, secret: str) -> str:
        """Idempotently set up everything for one provider and return backend id.

        Creates/updates: the shared provider backend (real upstream key + circuit
        breaker), the provider's APIM API (native subscription-key header), its
        operations (paths), a simple inbound policy (set-backend-service + token
        limit/metering), and product associations. Provider-agnostic: driven by
        PROVIDER_APIS config.

        This is the SINGLE-backend path (BYO / a lone upstream). For the
        GitModel-hub fleet use `ensure_pooled_provider_api`, which targets the
        load-balanced pool instead.
        """
        cfg = PROVIDER_APIS.get(provider)
        if not cfg:
            logger.warning("unknown provider '%s'; skipping APIM wiring", provider)
            return ""

        backend_id = self._ensure_provider_backend(
            cfg["backend"], upstream_url, cfg["auth_header"], secret, cfg["bearer"]
        )
        self._ensure_api_and_ops(provider, cfg, backend_id)
        return backend_id

    def ensure_pooled_provider_api(self, provider: str) -> str:
        """Wire a provider's client-facing API to its load-balanced POOL
        (`llm-<provider>-pool`) instead of a single backend, so requests fan out
        across every GitHub-account hub with session affinity (keeping each chat
        session pinned to one hub for prompt-cache warmth — see
        docs/APIM-LLM-Gateway.md §2/§4).

        The pool itself is created during pool-join (`add_hub_to_pools`); this
        only (idempotently) creates the API, its operations, the inbound policy
        that targets the pool, the OpenAI-schema streaming op policy, and the
        product links. Returns the pool id used as the backend target.
        """
        cfg = PROVIDER_APIS.get(provider)
        if not cfg:
            logger.warning("unknown provider '%s'; skipping APIM wiring", provider)
            return ""
        pool_id = f"llm-{provider}-pool"
        self._ensure_api_and_ops(provider, cfg, pool_id)
        return pool_id

    def _ensure_api_and_ops(self, provider: str, cfg: dict, backend_id: str) -> None:
        """Create/update the provider's APIM API, its operations, the inbound
        policy (routing to `backend_id` — a single backend OR a pool), the
        OpenAI-schema streaming op policy, and product associations. Shared by
        `ensure_provider_api` (single backend) and `ensure_pooled_provider_api`
        (pool). `set-backend-service` references a pool exactly like a backend."""
        # The inbound policy references {{KEY_LIMITS_NV}}; APIM rejects a policy
        # referencing a missing named value, so ensure it exists FIRST.
        self.ensure_key_limits_nv()
        # API with the provider-native subscription-key header.
        self.client.api.begin_create_or_update(
            self._rg,
            self._service,
            cfg["api_id"],
            ApiCreateOrUpdateParameter(
                display_name=cfg["display"],
                path=cfg["api_id"],
                protocols=["https"],
                subscription_required=True,
                api_type="http",
                subscription_key_parameter_names=SubscriptionKeyParameterNamesContract(
                    header=cfg["sub_header"], query="subscription-key"
                ),
            ),
        ).result()

        # Operations (paths) for this provider.
        for op_id, url_template in cfg["ops"]:
            self.client.api_operation.create_or_update(
                self._rg,
                self._service,
                cfg["api_id"],
                op_id,
                OperationContract(
                    display_name=op_id,
                    method="POST",
                    url_template=url_template,
                ),
            )

        # Simple inbound policy: route to this provider's backend/pool + govern.
        self.client.api_policy.create_or_update(
            self._rg,
            self._service,
            cfg["api_id"],
            "policy",
            PolicyContract(
                value=self._build_provider_policy(backend_id, provider), format="rawxml"
            ),
        )

        # For OpenAI-schema providers, attach an operation-level policy to the
        # `chat` op that injects stream_options.include_usage on streaming
        # requests (so llm-emit-token-metric gets accurate counts). Scoped to
        # `chat` only — the `responses` op's Responses API rejects the field.
        if provider in _CHAT_USAGE_PROVIDERS:
            self.client.api_operation_policy.create_or_update(
                self._rg,
                self._service,
                cfg["api_id"],
                "chat",
                "policy",
                PolicyContract(
                    value=self._build_chat_stream_policy(), format="rawxml"
                ),
            )

        # Authorize subscription keys (scoped to these products) to call the API.
        for product_id in _LLM_PRODUCTS:
            try:
                self.client.product_api.create_or_update(
                    self._rg, self._service, product_id, cfg["api_id"]
                )
            except (ResourceNotFoundError, HttpResponseError) as exc:
                logger.warning("link %s to product %s skipped: %s", cfg["api_id"], product_id, exc)

    @staticmethod
    def _breaker_rules() -> list[CircuitBreakerRule]:
        """The single circuit-breaker rule shared by every backend (single + pool
        members).

        Azure APIM allows exactly ONE circuit-breaker rule per backend, and one
        rule has a single count/interval/tripDuration — but its failureCondition
        CAN list multiple status-code ranges. So both trip triggers ride one rule:

          * UPSTREAM 429 — the hub/Copilot behind this backend is out of TPM.
          * 5xx — the backend is genuinely unhealthy.

        A SINGLE such response trips the backend for 60s, ejecting that one hub
        from the pool so requests fail over to another account's hub (sacrificing
        that hub's warm prompt cache for availability). 60s because 429 is
        transient (a provider TPM window refreshes in ~a minute); the same fast
        trip suits 5xx too (a failing backend should be shed quickly, not hammered
        for an hour). Retry-After is honored.

        This does NOT catch our OWN per-key llm-token-limit 429: that limit runs
        in INBOUND and rejects before the request reaches the backend, so the
        circuit breaker (which only sees backend responses) never counts it. Only
        a real upstream 429 — returned BY the hub backend — trips this.
        """
        return [
            CircuitBreakerRule(
                name="trip-on-429-or-5xx",
                failure_condition=CircuitBreakerFailureCondition(
                    count=1,
                    interval=timedelta(minutes=1),
                    status_code_ranges=[
                        FailureStatusCodeRange(min=429, max=429),
                        FailureStatusCodeRange(min=500, max=599),
                    ],
                ),
                trip_duration=timedelta(seconds=60),
                accept_retry_after=True,
            ),
        ]

    def _ensure_provider_backend(
        self, backend_id: str, url: str, auth_header: str, secret: str, bearer: bool
    ) -> str:
        """Create/update the shared backend for a provider (real key + breaker)."""
        header_val = f"Bearer {secret}" if bearer else secret
        creds = BackendCredentialsContract(header={auth_header: [header_val]})
        circuit_breaker = BackendCircuitBreaker(rules=self._breaker_rules())
        backend = self.client.backend.create_or_update(
            self._rg,
            self._service,
            backend_id,
            BackendContract(
                url=url, protocol="http", credentials=creds, circuit_breaker=circuit_breaker
            ),
        )
        return backend.name or backend_id

    @staticmethod
    def _build_limit_block() -> str:
        """Build the inbound rate/quota-limit XML fragment for the shared policy.

        Design (dictated by dev-a01 testing of what llm-token-limit accepts):
          * tokens-per-minute + token-quota-period accept policy expressions, so
            they read the calling key's values straight from the KEY_LIMITS_NV map.
          * token-quota does NOT accept an expression, so quota is a fixed TIER:
            we read the tier LABEL from the map into tfQTier, then <choose> one
            branch per tier with the literal amount from TOKEN_QUOTA_AMOUNTS.
          * a key with no quota tier hits <otherwise> = a TPM-only limit (no quota).

        Branch count = number of tiers (period stays an expression, so it does NOT
        multiply the branches).

        ⚠️ Named value reference: {{KEY_LIMITS_NV}} is a COMPILE-TIME text
        substitution. It must be the WHOLE attribute value of one set-variable
        (`value="{{nv}}"`) — inlining it into `Parse("{{nv}}")` breaks the C#
        string literal once the value holds real JSON (its quotes/commas leak in).
        So we capture it into tfRaw first, then every expression parses tfRaw."""
        nv = KEY_LIMITS_NV

        def _lookup(field: str, default: str, cast_open: str, cast_close: str) -> str:
            """Expression that parses tfRaw, finds this subscription's entry, and
            returns entry[field] cast via cast_open/cast_close, else default."""
            return (
                "@{ try {"
                " var m=Newtonsoft.Json.Linq.JObject.Parse((string)context.Variables"
                "[&quot;tfRaw&quot;]);"
                " var e=m[context.Subscription.Id] as Newtonsoft.Json.Linq.JObject;"
                f" return (e!=null&amp;&amp;e[&quot;{field}&quot;]!=null)"
                f"?{cast_open}e[&quot;{field}&quot;]{cast_close}:{default};"
                f" }} catch {{ return {default}; }} }}"
            )

        tpm_expr = _lookup("t", str(_TPM_UNLIMITED), "(int)", "")
        period_var = _lookup(
            "p", f"&quot;{_PERIOD_DEFAULT}&quot;", "(string)", ""
        )
        qtier_var = _lookup("qt", "&quot;none&quot;", "(string)", "")
        tpm_attr = (
            "@(context.Variables.GetValueOrDefault&lt;int&gt;"
            f"(&quot;tfTpm&quot;, {_TPM_UNLIMITED}))"
        )
        period_attr = (
            "@(context.Variables.GetValueOrDefault&lt;string&gt;"
            f"(&quot;tfPeriod&quot;, &quot;{_PERIOD_DEFAULT}&quot;))"
        )
        # One <when> per quota tier with the literal amount baked in.
        whens = ""
        for tier, amount in TOKEN_QUOTA_AMOUNTS.items():
            whens += (
                f'\n      <when condition="@((string)context.Variables[&quot;tfQTier&quot;]'
                f'==&quot;{tier.value}&quot;)">'
                f'\n        <llm-token-limit counter-key="@(context.Subscription.Id)"'
                f'\n          tokens-per-minute="{tpm_attr}"'
                f'\n          token-quota="{amount}" token-quota-period="{period_attr}"'
                f'\n          estimate-prompt-tokens="false"'
                f'\n          remaining-tokens-header-name="x-remaining-tokens"'
                f'\n          remaining-quota-tokens-header-name="x-remaining-quota-tokens"'
                f'\n          tokens-consumed-header-name="x-consumed-tokens" />'
                f"\n      </when>"
            )
        # tfRaw captures the named value as a plain value (NOT inlined into an
        # expression string), so its JSON quotes/commas can't break the policy.
        return f"""<set-variable name="tfRaw" value="{{{{{nv}}}}}" />
    <set-variable name="tfTpm" value="{tpm_expr}" />
    <set-variable name="tfPeriod" value="{period_var}" />
    <set-variable name="tfQTier" value="{qtier_var}" />
    <choose>{whens}
      <otherwise>
        <llm-token-limit counter-key="@(context.Subscription.Id)"
          tokens-per-minute="{tpm_attr}"
          estimate-prompt-tokens="false"
          remaining-tokens-header-name="x-remaining-tokens"
          tokens-consumed-header-name="x-consumed-tokens" />
      </otherwise>
    </choose>"""

    def _build_provider_policy(self, backend_id: str, provider: str) -> str:
        """Inbound governance + outbound Cosmos usage write for a provider API.

        Each provider API binds one backend; the upstream (multi-model) backend
        dispatches by the body's `model`. Outbound writes one usage record per
        successful, NON-STREAMING call to the `usage` container
        (send-one-way-request, fire-and-forget, MI auth) — the Cosmos endpoint
        comes from settings so it always matches the deployed account (never a
        hardcoded host).

        Streaming (SSE) responses are deliberately NOT persisted to Cosmos: the
        outbound `As<JObject>()` body read would force APIM to buffer the whole
        response, defeating token-by-token passthrough, and an event-stream body
        isn't a single JSON object anyway. Streaming token accounting therefore
        rides on the native `llm-emit-token-metric` (App Insights), which counts
        inside the pipeline without reading the body. The outbound write is gated
        on the response Content-Type not being `text/event-stream` (provider-
        agnostic — all four providers stream with that media type).

        `provider` is accepted for symmetry with the per-operation streaming
        policy (see `_build_chat_stream_policy`); the API-level policy itself is
        provider-agnostic.
        """
        _ = provider  # API-level policy is provider-agnostic; kept for symmetry
        docs = f"{self._cosmos_endpoint}/dbs/{self._cosmos_db}/colls/{self._cosmos_container}/docs"
        limit_block = self._build_limit_block()
        return f"""<policies>
  <inbound>
    <base />
    <set-backend-service backend-id="{backend_id}" />
    {limit_block}
    {self._MODEL_VAR}
    <llm-emit-token-metric namespace="tokenfoundry">
      <dimension name="subscription" value="@(context.Subscription.Id)" />
      <dimension name="api" value="@(context.Api.Id)" />
      <dimension name="model" value="@(context.Variables.GetValueOrDefault&lt;string&gt;(&quot;tfModel&quot;, &quot;unknown&quot;))" />
    </llm-emit-token-metric>
    <authentication-managed-identity resource="https://cosmos.azure.com"
      output-token-variable-name="cosmosToken" ignore-error="true" />
  </inbound>
  <backend><base /></backend>
  <outbound>
    <base />
    {self._USAGE_TRACE}
    <choose>
      <when condition="@(context.Response.StatusCode == 200 &amp;&amp; context.Variables.ContainsKey(&quot;cosmosToken&quot;) &amp;&amp; !context.Response.Headers.GetValueOrDefault(&quot;Content-Type&quot;,&quot;&quot;).Contains(&quot;text/event-stream&quot;))">
        <send-one-way-request mode="new">
          <set-url>{docs}</set-url>
          <set-method>POST</set-method>
          <set-header name="Authorization" exists-action="override">
            <value>@(System.Net.WebUtility.UrlEncode("type=aad&amp;ver=1.0&amp;sig=" + (string)context.Variables["cosmosToken"]))</value>
          </set-header>
          <set-header name="x-ms-version" exists-action="override"><value>2018-12-31</value></set-header>
          <set-header name="x-ms-documentdb-partitionkey" exists-action="override">
            <value>@("[\\"" + context.Subscription.Id + "_" + DateTime.UtcNow.ToString("yyyyMM") + "\\"]")</value>
          </set-header>
          <set-body>@{{var doc=new JObject();doc["id"]=context.RequestId;doc["pk"]=context.Subscription.Id+"_"+DateTime.UtcNow.ToString("yyyyMM");doc["ts"]=DateTime.UtcNow.ToString("o");doc["subscription"]=context.Subscription.Id;doc["api"]=context.Api.Id;try{{doc["raw_response"]=context.Response.Body.As&lt;JObject&gt;(preserveContent:true);}}catch{{doc["raw_response"]=null;}}return doc.ToString();}}</set-body>
        </send-one-way-request>
      </when>
    </choose>
  </outbound>
  <on-error><base /></on-error>
</policies>"""

    @staticmethod
    def _build_chat_stream_policy() -> str:
        """Operation-level policy for the `chat` op: inject
        `stream_options.include_usage=true` on STREAMING chat-completions requests.

        Why operation-scoped and not API-level: `stream_options` is a Chat
        Completions-only field. The same provider API also exposes a `responses`
        op (OpenAI Responses API) which rejects the field with HTTP 400, so the
        injection must not reach it. Attaching this only to the `chat` operation
        keeps `responses` untouched.

        Only mutates the body when `stream == true`; non-streaming requests pass
        through unchanged. `<base />` preserves the inherited API-level policy
        (backend routing, token limit/metric, Cosmos write). `preserveContent:true`
        keeps the body readable by the backend after the rewrite.
        """
        return """<policies>
  <inbound>
    <base />
    <choose>
      <when condition="@{ try { var b = context.Request.Body.As&lt;JObject&gt;(preserveContent:true); return b != null &amp;&amp; b[&quot;stream&quot;] != null &amp;&amp; b[&quot;stream&quot;].Type == JTokenType.Boolean &amp;&amp; (bool)b[&quot;stream&quot;]; } catch { return false; } }">
        <set-body>@{ var b = context.Request.Body.As&lt;JObject&gt;(preserveContent:true); var so = b[&quot;stream_options&quot;] as JObject ?? new JObject(); so[&quot;include_usage&quot;] = true; b[&quot;stream_options&quot;] = so; return b.ToString(); }</set-body>
      </when>
    </choose>
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>"""

    # --- Subscriptions (virtual keys) ---

    def create_subscription(
        self, subscription_id: str, display_name: str, product_id: str
    ) -> str:
        """Create an APIM subscription (virtual key); return its primary key.

        Scope is the service-wide /apis (all APIs) rather than /products/<id>.
        On the Developer SKU, product-scoped subscriptions hit a low
        "Subscriptions limit reached for same user" quota (all keys land under
        the same default owner); the /apis scope is not subject to that limit.
        Per-key rate-limit + metering are keyed on the subscription id in the
        gateway policy, so they are unaffected by the scope change. product_id
        is retained in the signature for the tenant/product bookkeeping that
        still records which product a tenant maps to.
        """
        _ = product_id  # tenant->product mapping is tracked in PostgreSQL
        scope = (
            f"/subscriptions/{self._sub_id}/resourceGroups/{self._rg}"
            f"/providers/Microsoft.ApiManagement/service/{self._service}/apis"
        )
        params = SubscriptionCreateParameters(
            scope=scope, display_name=display_name, state="active"
        )
        created = self.client.subscription.create_or_update(
            resource_group_name=self._rg,
            service_name=self._service,
            sid=subscription_id,
            parameters=params,
        )
        # Primary key is only returned via list_secrets
        secrets = self.client.subscription.list_secrets(
            self._rg, self._service, created.name or subscription_id
        )
        return secrets.primary_key or ""

    def set_subscription_state(self, subscription_id: str, state: str) -> None:
        """state: 'active' | 'suspended' | 'cancelled' — used by budget enforcer."""
        try:
            sub = self.client.subscription.get(
                self._rg, self._service, subscription_id
            )
        except ResourceNotFoundError:
            logger.warning("subscription %s not found", subscription_id)
            return
        params = SubscriptionCreateParameters(scope=sub.scope, state=state)
        self.client.subscription.create_or_update(
            self._rg, self._service, subscription_id, params
        )

    def delete_subscription(self, subscription_id: str) -> None:
        """Permanently delete an APIM subscription (key revoke). Idempotent:
        a missing subscription is treated as already-deleted, not an error."""
        try:
            self.client.subscription.delete(
                self._rg, self._service, subscription_id, if_match="*"
            )
        except ResourceNotFoundError:
            logger.info("subscription %s already gone", subscription_id)

    # --- Per-key token limits (shared named value map) ---

    def ensure_key_limits_nv(self) -> None:
        """Ensure the KEY_LIMITS_NV named value exists (empty map if new). MUST run
        before pushing any policy that references {{KEY_LIMITS_NV}} — APIM rejects
        a policy referencing a non-existent named value ("Cannot find a property").
        Idempotent: a present named value is left untouched."""
        try:
            self.client.named_value.get(self._rg, self._service, KEY_LIMITS_NV)
            return
        except ResourceNotFoundError:
            pass
        self._write_key_limits({}, None)
        logger.info("created named value %s (empty)", KEY_LIMITS_NV)

    def _read_key_limits(self) -> tuple[dict, str | None]:
        """Read the KEY_LIMITS_NV named value -> (parsed map, etag). Missing NV or
        unparyable value both yield ({}, None) so callers start from an empty map."""
        try:
            nv = self.client.named_value.get(self._rg, self._service, KEY_LIMITS_NV)
        except ResourceNotFoundError:
            return {}, None
        try:
            parsed = json.loads(nv.value) if nv.value else {}
        except (ValueError, TypeError):
            logger.warning("named value %s not valid JSON; resetting", KEY_LIMITS_NV)
            parsed = {}
        etag = getattr(nv, "e_tag", None)
        return (parsed if isinstance(parsed, dict) else {}), etag

    def _write_key_limits(self, mapping: dict, etag: str | None) -> None:
        """Write the map back to the named value, using the etag for optimistic
        concurrency (if_match). Caller handles retry on precondition failure."""
        params = NamedValueCreateContract(
            display_name=KEY_LIMITS_NV,
            value=json.dumps(mapping, separators=(",", ":")),
            secret=False,
        )
        self.client.named_value.begin_create_or_update(
            self._rg,
            self._service,
            KEY_LIMITS_NV,
            params,
            if_match=etag or "*",
        ).result()

    def upsert_key_limits(
        self,
        subscription_id: str,
        tokens_per_minute: int | None,
        token_quota_tier: str | None,
        token_quota_period: str | None,
    ) -> None:
        """Set (or clear) a key's entry in the shared limits map. Read-merge-write
        with a small retry to tolerate concurrent issuance (etag precondition).
        Raises on over-cap (via _merge_key_limit) or persistent write failure so
        the caller can roll back the key issuance."""
        for attempt in range(3):
            current, etag = self._read_key_limits()
            merged = _merge_key_limit(
                current,
                subscription_id,
                tokens_per_minute,
                token_quota_tier,
                token_quota_period,
            )
            try:
                self._write_key_limits(merged, etag)
                return
            except HttpResponseError as exc:
                # 412 precondition failed -> someone else wrote; re-read and retry.
                if getattr(exc, "status_code", None) == 412 and attempt < 2:
                    logger.info("named value %s changed; retrying", KEY_LIMITS_NV)
                    continue
                raise

    def remove_key_limits(self, subscription_id: str) -> None:
        """Remove a key's entry from the shared limits map (best-effort, on key
        delete). Same retry-on-precondition as upsert; a missing entry is a no-op."""
        for attempt in range(3):
            current, etag = self._read_key_limits()
            if subscription_id not in current:
                return
            merged = _remove_key_limit(current, subscription_id)
            try:
                self._write_key_limits(merged, etag)
                return
            except HttpResponseError as exc:
                if getattr(exc, "status_code", None) == 412 and attempt < 2:
                    continue
                raise

    # --- Model backends + aliases ---

    def add_model_route(
        self,
        route_id: str,
        backend_url: str,
        header_auth: tuple[str, str] | None = None,
    ) -> str:
        """Register a backend for a model route. Returns the APIM backend id.

        header_auth: (header_name, secret_value) for BYO/key-auth providers
        (Kimi/DeepSeek/Anthropic). Managed-identity backends pass None.
        """
        creds = None
        if header_auth:
            name, value = header_auth
            creds = BackendCredentialsContract(header={name: [value]})
        # Circuit breaker: same rules as pooled backends — trip on sustained 5xx
        # (unhealthy) and on sustained UPSTREAM 429 (out of TPM → brief eject so
        # traffic fails over). See _breaker_rules for the rationale.
        circuit_breaker = BackendCircuitBreaker(rules=self._breaker_rules())
        contract = BackendContract(
            url=backend_url,
            protocol="http",
            credentials=creds,
            circuit_breaker=circuit_breaker,
        )
        backend = self.client.backend.create_or_update(
            self._rg, self._service, route_id, contract
        )
        return backend.name or route_id

    def attach_alias(self, alias: str, backend_id: str, api_format: str) -> None:
        """Deprecated single-alias wiring — superseded by ensure_llm_api +
        refresh_routing, which rebuild the whole routing policy declaratively.
        Kept as a no-op so any old callers don't break; routes.py now calls
        refresh_routing with the full route set instead.
        """
        logger.debug(
            "attach_alias is a no-op; routing handled by refresh_routing (%s->%s, %s)",
            alias,
            backend_id,
            api_format,
        )

    def remove_backend(self, backend_id: str) -> None:
        try:
            self.client.backend.delete(
                self._rg, self._service, backend_id, if_match="*"
            )
        except (ResourceNotFoundError, HttpResponseError) as exc:
            logger.warning("backend %s delete skipped: %s", backend_id, exc)

    # --- Backend pools (GitModel hub load-balancing + session affinity) ---
    #
    # Pools ride the PREVIEW ARM API (2023-09-01-preview): type=Pool +
    # sessionAffinity are not in the stable azure-mgmt SDK surface, so these use
    # raw ARM REST (same path we validated by hand with `az rest`). Adding a
    # GitHub account = registering its hub as a per-account backend and appending
    # it to the openai/anthropic/google pools; session affinity keeps a chat
    # session pinned to one hub so prompt caching stays warm.

    _POOL_API_VERSION = "2023-09-01-preview"

    def _arm_token(self) -> str:
        return DefaultAzureCredential().get_token("https://management.azure.com/.default").token

    def _backend_base(self) -> str:
        return (
            f"https://management.azure.com/subscriptions/{self._sub_id}"
            f"/resourceGroups/{self._rg}/providers/Microsoft.ApiManagement"
            f"/service/{self._service}/backends"
        )

    def add_hub_to_pools(self, account_id: str, hub_fqdn: str, hub_key: str) -> list[str]:
        """Register a hub (one GitHub account) into the 3 provider pools.

        For each of openai/anthropic/google:
          1. create a per-account single backend `llm-<provider>-<account_id>`
             pointing at the hub (provider-native auth header + circuit breaker),
          2. append it to the `llm-<provider>-pool` (creating the pool with
             session affinity if it doesn't exist yet — first account still gets
             a pool, per the plan).
        Returns the list of per-account backend ids created (for later removal).

        Idempotent: re-adding the same account is a no-op on the services list.
        """
        base = f"https://{hub_fqdn}"  # hub Container App ingress (https)
        created: list[str] = []
        for provider in ("openai", "anthropic", "google"):
            cfg = PROVIDER_APIS[provider]
            be_id = f"llm-{provider}-{account_id}"
            # 1) single backend for this hub (reuse the SDK path — stable API)
            self._ensure_provider_backend(
                be_id, base, cfg["auth_header"], hub_key, cfg["bearer"]
            )
            # 2) append to the pool (preview REST)
            self._pool_add_service(f"llm-{provider}-pool", be_id)
            created.append(be_id)
        return created

    def remove_hub_from_pools(self, account_id: str, backend_ids: list[str] | None = None) -> None:
        """Remove a hub's backends from the 3 pools and delete them. Idempotent."""
        for provider in ("openai", "anthropic", "google"):
            be_id = f"llm-{provider}-{account_id}"
            self._pool_remove_service(f"llm-{provider}-pool", be_id)
            self.remove_backend(be_id)
        # Delete any extra recorded backends not covered by the naming scheme.
        for be_id in backend_ids or []:
            if not be_id.endswith(account_id):
                self.remove_backend(be_id)

    def _pool_add_service(self, pool_id: str, backend_id: str) -> None:
        """GET pool -> append backend to services[] (preserving sessionAffinity)
        -> PUT with If-Match ETag. Creates the pool with session affinity if it
        doesn't exist yet."""
        svc_id = f"{self._backend_base().rsplit('/backends', 1)[0]}/backends/{backend_id}"
        member = {"id": svc_id, "priority": 1, "weight": 1}
        # Idempotency match key. We CONSTRUCT an absolute id
        # (https://management.azure.com/subscriptions/...), but ARM STORES the
        # service id as a RELATIVE path (/subscriptions/...). So a full-string
        # compare never matches an existing member and we'd append duplicates.
        # Match on the stable `/backends/<id>` suffix instead.
        svc_suffix = f"/backends/{backend_id}".lower()
        url = f"{self._backend_base()}/{pool_id}?api-version={self._POOL_API_VERSION}"
        headers = {"Authorization": f"Bearer {self._arm_token()}"}
        with httpx.Client(timeout=30.0) as hc:
            r = hc.get(url, headers=headers)
            if r.status_code == 200:
                body = r.json()
                props = body.get("properties", {})
                pool = props.get("pool") or {}
                services = pool.get("services") or []
                if any(s.get("id", "").lower().endswith(svc_suffix) for s in services):
                    return  # already a member — idempotent
                services.append(member)
                pool["services"] = services
                pool.setdefault(
                    "sessionAffinity",
                    {"sessionId": {"source": "Cookie", "name": "SessionId"}},
                )
                props["type"] = "Pool"
                props["pool"] = pool
                etag = r.headers.get("ETag")
                put_headers = dict(headers)
                if etag:
                    put_headers["If-Match"] = etag
                pr = hc.put(url, headers=put_headers, json={"properties": props})
                pr.raise_for_status()
            elif r.status_code == 404:
                # First account: create the pool with this one member + affinity.
                props = {
                    "description": f"Hub pool for {pool_id.rsplit('-pool', 1)[0]}",
                    "type": "Pool",
                    "pool": {
                        "services": [member],
                        "sessionAffinity": {
                            "sessionId": {"source": "Cookie", "name": "SessionId"}
                        },
                    },
                }
                pr = hc.put(url, headers=headers, json={"properties": props})
                pr.raise_for_status()
            else:
                r.raise_for_status()

    def _pool_remove_service(self, pool_id: str, backend_id: str) -> None:
        """GET pool -> drop backend from services[] -> PUT. No-op if pool or
        member is absent."""
        # Match on the lowercased `/backends/<id>` suffix — same normalization as
        # _pool_add_service. ARM stores the service id as a RELATIVE path with
        # potentially different casing than we'd construct, so a case-sensitive
        # endswith can silently fail to match and leave the (now-destroyed) hub's
        # backend orphaned in the pool.
        svc_suffix = f"/backends/{backend_id}".lower()
        url = f"{self._backend_base()}/{pool_id}?api-version={self._POOL_API_VERSION}"
        headers = {"Authorization": f"Bearer {self._arm_token()}"}
        with httpx.Client(timeout=30.0) as hc:
            r = hc.get(url, headers=headers)
            if r.status_code == 404:
                return
            r.raise_for_status()
            body = r.json()
            props = body.get("properties", {})
            pool = props.get("pool") or {}
            services = pool.get("services") or []
            kept = [
                s for s in services if not s.get("id", "").lower().endswith(svc_suffix)
            ]
            if len(kept) == len(services):
                return  # not a member — idempotent
            pool["services"] = kept
            props["pool"] = pool
            etag = r.headers.get("ETag")
            put_headers = dict(headers)
            if etag:
                put_headers["If-Match"] = etag
            pr = hc.put(url, headers=put_headers, json={"properties": props})
            pr.raise_for_status()
