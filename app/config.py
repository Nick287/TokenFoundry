"""Centralized configuration via pydantic-settings.

All values come from environment variables (injected by Container Apps from
Key Vault references / app settings). Local dev reads a .env file. Secrets are
never hardcoded — see .env.example for the contract.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="TF_", extra="ignore"
    )

    # --- App ---
    environment: str = Field(default="local", description="local | dev | prod")
    api_prefix: str = "/api"
    # Root log level. INFO by default because this service's own logs are
    # low-volume and diagnostic — the usage importer's per-run counters, the
    # re-login audit trail, which fields GitHub returned from a device flow.
    # Without configuration Python's root logger defaults to WARNING and every
    # one of those lines is silently discarded, which turns routine debugging
    # into guesswork against an empty log. DEBUG is deliberately NOT the default:
    # the Azure SDKs are extremely chatty at that level and would bury us.
    log_level: str = Field(default="INFO", description="DEBUG | INFO | WARNING | ERROR")

    # --- Azure subscription / resource targets ---
    azure_subscription_id: str = ""
    resource_group: str = ""
    apim_service_name: str = ""
    # Telemetry sampling for the per-API diagnostics this service writes.
    #
    # Terraform owns the SERVICE-level diagnostic; it cannot own the per-API ones
    # because the llm-* APIs are created here at runtime, when a GitHub account
    # is added. Both must carry the same number: an API-level diagnostic
    # OVERRIDES the service-level one, and fields it omits do NOT fall back —
    # measured on dev-19, where a service-level 10% left the LLM APIs logging
    # 73/73 and 20/20, i.e. fully unsampled.
    apim_sampling_percentage: int = 100
    # ACR + Key Vault names + region — injected by terraform (TF_ACR_NAME /
    # TF_KEYVAULT_NAME / TF_AZURE_LOCATION, plus TF_ACR_LOGIN_SERVER for image
    # refs). The Portal's "push SP creds to GitHub" flow (app/api/deploy_config.py)
    # feeds these straight into the HUB_ACR_NAME / HUB_KEYVAULT_NAME / HUB_LOCATION
    # GitHub Actions variables the deploy-hub.yml workflow reads — no string
    # parsing in the app.
    acr_login_server: str = ""
    acr_name: str = ""
    azure_location: str = ""
    keyvault_name: str = ""
    # Tag of the HUB image (gitmodel:<tag>). The Portal publishes
    # gitmodel:<hub_image_tag> as HUB_IMAGE_REF so hub deploys pull a tag that
    # actually exists in ACR.
    #
    # Terraform injects this from its own `hub_image_tag` variable, which is
    # deliberately separate from the app's `image_tag`: deploy.sh builds both
    # images together, but update-app.sh rebuilds only the app, so the newest
    # tokenfoundry tag routinely names a gitmodel image that was never built.
    #
    # The default is empty, not "latest". "latest" reads like "newest" but is
    # just an ordinary tag name, and nothing in this repo ever pushes it —
    # `gitmodel:latest` resolves to no image at all (verified against ACR:
    # "manifest tagged by 'latest' is not found"). An empty value fails visibly
    # at the point of use instead of producing a plausible-looking ref that
    # cannot be pulled.
    hub_image_tag: str = ""

    # --- Metadata DB (PostgreSQL Flexible Server) ---
    # Full SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host:5432/tokenfoundry
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/tokenfoundry"

    # --- Usage store (Cosmos DB for NoSQL) ---
    cosmos_endpoint: str = ""
    cosmos_database: str = "tokenfoundry"
    cosmos_usage_container: str = "usage"

    # --- Usage pipeline (hub -> Event Hub -> Capture blobs -> Cosmos) ---
    # The first three are pass-through only: the control plane never produces to
    # the Event Hub, it just republishes the coordinates as HUB_EVENTHUB_*
    # Actions variables so each hub's deploy can point its producer at them.
    eventhub_namespace_id: str = ""
    eventhub_fqdn: str = ""
    eventhub_name: str = ""
    # These are what the import job reads. Capture writes Avro blobs on a fixed
    # interval; running the import faster than that only re-lists the same
    # blobs, so the interval is the floor on the job's schedule.
    usage_capture_storage_account: str = ""
    usage_capture_container: str = "usage-capture"
    usage_capture_interval_seconds: int = 300

    # How often to ask every deployed hub for its /api/status. This is the only
    # reader of the hubs' usage-event drop counters — before it existed, a hub
    # could silently stop reporting billing events and the sole evidence was a
    # counter nobody fetched (dev-15 lost 21 records that way). Longer than the
    # capture interval on purpose: the counters move slowly and each pass costs
    # one HTTP round-trip per hub.
    hub_status_interval_seconds: int = 300

    # --- Audit archive (raw bodies, opt-in per tenant) ---
    # Pass-through only, and it stays that way on purpose: these are republished
    # as HUB_AUDIT_* Actions variables so each hub can write payloads, and the
    # control plane holds NO role on that storage account. It records blob paths
    # next to usage documents; reading one takes a role granted out of band to a
    # named person. retention_days is echoed from infra so anything the app says
    # about how long content is kept matches what lifecycle management actually
    # enforces.
    audit_account_url: str = ""
    audit_container: str = "audit"
    audit_container_scope: str = ""
    audit_retention_days: int = 90

    # --- Key Vault (subscription keys + BYO provider secrets) ---
    keyvault_uri: str = ""

    # --- Hub deploy via GitHub Action (方案 A) ---
    # The control plane triggers a GitHub Action (workflow_dispatch) that runs the
    # per-account hub terraform with Service Principal auth, polls the run, then
    # reads terraform outputs from the remote state blob. It does NOT run
    # terraform itself. tfstate_* identify the remote-state blob container.
    tfstate_storage_account: str = ""
    tfstate_container: str = ""
    github_repo_owner: str = "Nick287"
    github_repo_name: str = "TokenFoundry"
    github_workflow_file: str = "deploy-hub.yml"
    github_ref: str = "master"
    # KV secret name holding the GitHub token (actions:read+write) the control
    # plane uses to trigger + poll the workflow.
    github_token_secret: str = "hub-deploy-github-token"

    # --- Observability (Application Insights via azure-monitor-query) ---
    app_insights_resource_id: str = ""
    # Log Analytics workspace customerId (GUID). The token-metering breakdown
    # queries the dedicated ApiManagementGatewayLlmLog table via
    # query_workspace(customerId) — query_resource against the App Insights
    # component can't see that table. Injected as TF_LOG_ANALYTICS_WORKSPACE_ID.
    log_analytics_workspace_id: str = ""

    # --- AuthN: dual identity sources ---
    # Platform admins -> Microsoft Entra ID; customers -> Entra External ID (CIAM)
    entra_tenant_id: str = ""
    entra_api_audience: str = ""
    external_id_authority: str = ""
    external_id_audience: str = ""

    # --- AuthN: self-hosted (database-backed) login ---
    # Backend signs its own HS256 JWTs; secret is injected from Key Vault in
    # cloud. The first admin user is seeded at startup from these credentials.
    jwt_secret: str = "dev-insecure-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480
    admin_username: str = "admin"
    admin_password: str = ""

    @property
    def is_local(self) -> bool:
        return self.environment == "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
