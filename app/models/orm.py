"""SQLAlchemy ORM models for control-plane metadata (PostgreSQL).

These are the "account records" — slow-changing, relational, joined. High-write
usage records live in Cosmos (see app/services/usage_ingest.py), NOT here.

Maps directly to the unified data model in the plan:
  Tenant, Project, VirtualKey, ModelRoute, Budget
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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
    TokenQuotaPeriod,
    TokenQuotaTier,
    UserRole,
)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[TenantMode] = mapped_column(Enum(TenantMode), nullable=False)
    billing_account_id: Mapped[str | None] = mapped_column(String(128))
    # APIM Product IDs backing this tenant (a tenant = one or a few Products)
    apim_product_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus), default=TenantStatus.ACTIVE
    )

    projects: Mapped[list[Project]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cost_center: Mapped[str | None] = mapped_column(String(128))

    tenant: Mapped[Tenant] = relationship(back_populates="projects")
    keys: Mapped[list[VirtualKey]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class VirtualKey(Base, TimestampMixin):
    """virtual key ≙ APIM Subscription. The key VALUE never lands here —
    only a Key Vault reference. allowed_route_ids controls which model
    aliases this key may call (visible via /models)."""

    __tablename__ = "virtual_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    apim_subscription_id: Mapped[str | None] = mapped_column(String(256))
    keyvault_ref: Mapped[str | None] = mapped_column(String(512))
    allowed_route_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Per-key gateway rate/quota limits — applied by APIM's llm-token-limit via a
    # shared named-value map keyed on this key's apim_subscription_id (see
    # apim_provisioner). All optional: a NULL / NONE means "no limit of that kind".
    #  * tokens_per_minute: arbitrary int (APIM accepts an expression for it).
    #  * token_quota_tier:  a preset TIER, not an arbitrary number — APIM's
    #    token-quota attribute rejects expressions, so the amount is a literal in
    #    a policy <choose> branch (see enums.TOKEN_QUOTA_AMOUNTS).
    #  * token_quota_period: the fixed window the quota resets on (UTC-aligned).
    tokens_per_minute: Mapped[int | None] = mapped_column(Integer)
    token_quota_tier: Mapped[TokenQuotaTier | None] = mapped_column(
        Enum(TokenQuotaTier, native_enum=False)
    )
    token_quota_period: Mapped[TokenQuotaPeriod | None] = mapped_column(
        Enum(TokenQuotaPeriod, native_enum=False)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[KeyStatus] = mapped_column(Enum(KeyStatus), default=KeyStatus.ACTIVE)

    project: Mapped[Project] = relationship(back_populates="keys")


class ModelRoute(Base, TimestampMixin):
    """A model alias -> backend mapping. tenant_id is NULL for platform-pooled
    routes (RESELL/INTERNAL) and set for BYO (TENANT-owned) routes."""

    __tablename__ = "model_routes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)  # client-facing alias
    provider: Mapped[Provider] = mapped_column(Enum(Provider), nullable=False)
    apim_backend_or_pool_id: Mapped[str | None] = mapped_column(String(256))
    deployment_name: Mapped[str | None] = mapped_column(String(128))
    # Azure OpenAI API version (e.g. "2024-10-21"); NULL for non-Azure providers.
    api_version: Mapped[str | None] = mapped_column(String(64))
    owner_scope: Mapped[OwnerScope] = mapped_column(
        Enum(OwnerScope), default=OwnerScope.PLATFORM
    )
    auth_mode: Mapped[AuthMode] = mapped_column(Enum(AuthMode), default=AuthMode.MI)
    # Pricing (per 1K tokens) + resale markup. INTERNAL/BYO use markup_pct=0.
    price_in_per_1k: Mapped[float] = mapped_column(Float, default=0.0)
    price_out_per_1k: Mapped[float] = mapped_column(Float, default=0.0)
    markup_pct: Mapped[float] = mapped_column(Float, default=0.0)


class Budget(Base, TimestampMixin):
    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[BudgetScope] = mapped_column(Enum(BudgetScope), nullable=False)
    # Scope target id (tenant_id / project_id / virtual_key_id depending on scope)
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    period_type: Mapped[str] = mapped_column(String(32), default="monthly")
    limit_usd: Mapped[float] = mapped_column(Float, nullable=False)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    action: Mapped[BudgetAction] = mapped_column(
        Enum(BudgetAction), default=BudgetAction.ALERT
    )
    # denormalized for fast tenant-scoped filtering in the customer portal
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)

    __table_args__ = ()


class User(Base, TimestampMixin):
    """Self-hosted login account (database-backed auth, no Entra).

    role/tenant_id mirror the Principal claims: admins are platform operators
    (tenant_id NULL, cross-tenant); customers are bound to one tenant. The
    password is stored only as a PBKDF2 hash (see app/services/passwords.py).
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.CUSTOMER)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    disabled: Mapped[bool] = mapped_column(default=False)


class GitHubAccount(Base, TimestampMixin):
    """A GitHub account whose Copilot subscription backs one deployed hub.

    Each account = one GitModel hub Container App (deployed in its OWN resource
    group) registered as a backend in the openai/anthropic/google APIM pools.
    "Adding a model" becomes "adding a GitHub account": device-flow login ->
    terraform deploy a hub -> join the pools. Multiple accounts load-balance
    within the pools (with session affinity so prompt caching stays warm).

    The OAuth token is NEVER stored here — only a Key Vault reference. deploy
    state is a DeployStatus machine driven by the control plane orchestrator.
    """

    __tablename__ = "github_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # gha_xxx
    github_login: Mapped[str | None] = mapped_column(String(128), index=True)
    github_user_id: Mapped[str | None] = mapped_column(String(64))
    # Key Vault reference to the Copilot OAuth token (secret name); value stays in KV.
    oauth_token_kv_ref: Mapped[str | None] = mapped_column(String(512))
    # Key Vault references to the deploy-time injected hub secrets (values in KV):
    #   hub_key_kv_ref     -> HUB_API_KEY   (inbound /v1 credential, also the APIM backend key)
    #   admin_token_kv_ref -> HUB_ADMIN_TOKEN (management-API override token)
    hub_key_kv_ref: Mapped[str | None] = mapped_column(String(512))
    admin_token_kv_ref: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[DeployStatus] = mapped_column(
        Enum(DeployStatus), default=DeployStatus.PENDING
    )
    error_detail: Mapped[str | None] = mapped_column(String(2048))
    # Device-flow handle kept only while status=pending (to poll GitHub).
    device_code: Mapped[str | None] = mapped_column(String(128))
    # Deployed-infra coordinates (populated once terraform apply succeeds).
    resource_group: Mapped[str | None] = mapped_column(String(128))
    container_app_fqdn: Mapped[str | None] = mapped_column(String(256))
    tf_state_key: Mapped[str | None] = mapped_column(String(256))
    # Names of the per-account APIM backends added to the three provider pools,
    # e.g. ["llm-openai-<id>", "llm-anthropic-<id>", "llm-google-<id>"]. Used to
    # remove them from the pools on delete.
    backend_ids: Mapped[list[str]] = mapped_column(JSON, default=list)


# NOTE: UsageRecord is intentionally NOT an ORM model — it's a high-write,
# append-only time series stored in Cosmos DB for NoSQL. Its shape lives in
# app/models/schemas.py (UsageRecord pydantic model) and is written via
# app/services/usage_ingest.py.
