"""Pydantic schemas: API request/response DTOs + the Cosmos UsageRecord.

Kept separate from ORM models so the API contract can evolve independently of
the storage schema.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    AuthMode,
    BudgetAction,
    BudgetScope,
    DeployStatus,
    KeyStatus,
    OwnerScope,
    Provider,
    TenantMode,
    TenantStatus,
    UserRole,
)

# ----- Auth (self-hosted login) -----


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: UserRole
    tenant_id: str | None = None


class MeResponse(BaseModel):
    subject: str
    role: UserRole
    tenant_id: str | None = None


# ----- User management -----


class UserCreate(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.CUSTOMER
    tenant_id: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    role: UserRole
    tenant_id: str | None
    disabled: bool
    created_at: datetime


class UserUpdate(BaseModel):
    role: UserRole | None = None
    tenant_id: str | None = None
    disabled: bool | None = None


class PasswordReset(BaseModel):
    new_password: str


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


# ----- Tenant -----


class TenantCreate(BaseModel):
    name: str
    mode: TenantMode
    billing_account_id: str | None = None


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    mode: TenantMode
    billing_account_id: str | None
    apim_product_ids: list[str]
    status: TenantStatus
    created_at: datetime


class TenantUpdate(BaseModel):
    name: str | None = None
    mode: TenantMode | None = None


# ----- Project -----


class ProjectCreate(BaseModel):
    tenant_id: str
    name: str
    cost_center: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tenant_id: str
    name: str
    cost_center: str | None
    created_at: datetime


class ProjectUpdate(BaseModel):
    name: str | None = None
    cost_center: str | None = None


# ----- VirtualKey -----


class VirtualKeyCreate(BaseModel):
    project_id: str
    allowed_route_ids: list[str] = Field(default_factory=list)
    tpm_tier: str | None = None
    monthly_budget_usd: float | None = None
    budget_action: BudgetAction = BudgetAction.ALERT
    expires_at: datetime | None = None


class VirtualKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    apim_subscription_id: str | None
    allowed_route_ids: list[str]
    tpm_tier: str | None
    monthly_budget_usd: float | None
    budget_action: BudgetAction
    expires_at: datetime | None
    status: KeyStatus
    created_at: datetime


class VirtualKeySecret(VirtualKeyOut):
    """Returned ONCE at creation — carries the actual subscription key value."""

    key_value: str


# ----- ModelRoute -----


class ModelRouteCreate(BaseModel):
    name: str  # client-facing alias, e.g. "claude-sonnet"
    provider: Provider
    tenant_id: str | None = None
    deployment_name: str | None = None
    api_version: str | None = None  # Azure OpenAI API version
    backend_url: str | None = None
    owner_scope: OwnerScope = OwnerScope.PLATFORM
    auth_mode: AuthMode = AuthMode.MI
    # For BYO / header-auth backends: secret provided once, stored in Key Vault.
    backend_secret: str | None = None
    price_in_per_1k: float = 0.0
    price_out_per_1k: float = 0.0
    markup_pct: float = 0.0


class ModelRouteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    provider: Provider
    tenant_id: str | None
    deployment_name: str | None
    api_version: str | None
    owner_scope: OwnerScope
    auth_mode: AuthMode
    price_in_per_1k: float
    price_out_per_1k: float
    markup_pct: float
    created_at: datetime


class ModelRouteUpdate(BaseModel):
    name: str | None = None
    deployment_name: str | None = None
    api_version: str | None = None
    price_in_per_1k: float | None = None
    price_out_per_1k: float | None = None
    markup_pct: float | None = None


# ----- GitHubAccount (GitModel hub instances) -----


class DeviceStartOut(BaseModel):
    """Returned by POST /github-accounts/device/start — what the user needs to
    authorize the GitHub Copilot account in their browser."""
    account_id: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


class DevicePollOut(BaseModel):
    """Returned by POST /github-accounts/device/poll — current auth+deploy state.
    status walks pending -> deploying -> ready (or failed)."""
    account_id: str
    status: DeployStatus
    github_login: str | None = None
    detail: str | None = None


class GitHubAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    github_login: str | None
    status: DeployStatus
    error_detail: str | None
    resource_group: str | None
    container_app_fqdn: str | None
    backend_ids: list[str]
    created_at: datetime


# ----- Budget -----


class BudgetCreate(BaseModel):
    scope: BudgetScope
    target_id: str
    limit_usd: float
    period_type: str = "monthly"
    action: BudgetAction = BudgetAction.ALERT
    tenant_id: str | None = None


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    scope: BudgetScope
    target_id: str
    period_type: str
    limit_usd: float
    spent_usd: float
    action: BudgetAction
    tenant_id: str | None
    reset_at: datetime | None


# ----- UsageRecord (Cosmos DB for NoSQL — high-write time series) -----


class UsageRecord(BaseModel):
    """One row per LLM call. partition key = subscriptionId+yyyymm."""

    ts: datetime
    subscription_id: str
    tenant_id: str
    project_id: str | None = None
    route: str  # model alias used
    prompt_tok: int = 0
    completion_tok: int = 0
    cached_tok: int = 0
    region: str | None = None
    cost_usd: float = 0.0
    billed_usd: float = 0.0
    request_id: str

    def partition_key(self) -> str:
        return f"{self.subscription_id}_{self.ts:%Y%m}"


# ----- Usage aggregate (read model for dashboards / portal) -----


class UsageSummary(BaseModel):
    tenant_id: str
    route: str | None = None
    total_prompt_tok: int = 0
    total_completion_tok: int = 0
    total_cost_usd: float = 0.0
    total_billed_usd: float = 0.0
    period_start: datetime | None = None
    period_end: datetime | None = None
