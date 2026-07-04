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
    OperationContract,
    PolicyContract,
    SubscriptionCreateParameters,
    SubscriptionKeyParameterNamesContract,
)

from app.config import get_settings

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


class ApimProvisioner:
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
        """
        cfg = PROVIDER_APIS.get(provider)
        if not cfg:
            logger.warning("unknown provider '%s'; skipping APIM wiring", provider)
            return ""

        backend_id = self._ensure_provider_backend(
            cfg["backend"], upstream_url, cfg["auth_header"], secret, cfg["bearer"]
        )

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

        # Simple inbound policy: route to this provider's backend + token govern.
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

        return backend_id

    def _ensure_provider_backend(
        self, backend_id: str, url: str, auth_header: str, secret: str, bearer: bool
    ) -> str:
        """Create/update the shared backend for a provider (real key + breaker)."""
        header_val = f"Bearer {secret}" if bearer else secret
        creds = BackendCredentialsContract(header={auth_header: [header_val]})
        circuit_breaker = BackendCircuitBreaker(
            rules=[
                CircuitBreakerRule(
                    name="trip-on-5xx",
                    failure_condition=CircuitBreakerFailureCondition(
                        count=3,
                        interval=timedelta(hours=1),
                        status_code_ranges=[FailureStatusCodeRange(min=500, max=599)],
                    ),
                    trip_duration=timedelta(hours=1),
                    accept_retry_after=True,
                )
            ]
        )
        backend = self.client.backend.create_or_update(
            self._rg,
            self._service,
            backend_id,
            BackendContract(
                url=url, protocol="http", credentials=creds, circuit_breaker=circuit_breaker
            ),
        )
        return backend.name or backend_id

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
        return f"""<policies>
  <inbound>
    <base />
    <set-backend-service backend-id="{backend_id}" />
    <llm-token-limit counter-key="@(context.Subscription.Id)"
      tokens-per-minute="50000" estimate-prompt-tokens="false"
      remaining-tokens-header-name="x-remaining-tokens"
      tokens-consumed-header-name="x-consumed-tokens" />
    <llm-emit-token-metric namespace="tokenfoundry">
      <dimension name="subscription" value="@(context.Subscription.Id)" />
      <dimension name="api" value="@(context.Api.Id)" />
    </llm-emit-token-metric>
    <authentication-managed-identity resource="https://cosmos.azure.com"
      output-token-variable-name="cosmosToken" ignore-error="true" />
  </inbound>
  <backend><base /></backend>
  <outbound>
    <base />
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
        # Circuit breaker: trip after 3 server errors (5xx) within an hour and
        # stop forwarding for an hour, honoring Retry-After. Protects the gateway
        # from a failing provider backend (resiliency for the LLM hub).
        circuit_breaker = BackendCircuitBreaker(
            rules=[
                CircuitBreakerRule(
                    name="trip-on-5xx",
                    failure_condition=CircuitBreakerFailureCondition(
                        count=3,
                        interval=timedelta(hours=1),
                        status_code_ranges=[FailureStatusCodeRange(min=500, max=599)],
                    ),
                    trip_duration=timedelta(hours=1),
                    accept_retry_after=True,
                )
            ]
        )
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
        svc_suffix = f"/backends/{backend_id}"
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
            kept = [s for s in services if not s.get("id", "").endswith(svc_suffix)]
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
