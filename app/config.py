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

    # --- Azure subscription / resource targets ---
    azure_subscription_id: str = ""
    resource_group: str = ""
    apim_service_name: str = ""
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

    # --- Metadata DB (PostgreSQL Flexible Server) ---
    # Full SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host:5432/tokenfoundry
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/tokenfoundry"

    # --- Usage store (Cosmos DB for NoSQL) ---
    cosmos_endpoint: str = ""
    cosmos_database: str = "tokenfoundry"
    cosmos_usage_container: str = "usage"

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
