"""Shared enums for the unified tenant/key/route data model.

These encode the three tenant modes and the pricing/auth distinctions that let
ONE schema express RESELL / BYO / INTERNAL — per the plan, the schema stays
fixed and only ownerScope + pricing fields + Budget.action differ.
"""

from enum import StrEnum


class TenantMode(StrEnum):
    RESELL = "RESELL"        # platform pools backends, resells with markup
    BYO = "BYO"              # customer brings their own model deployment
    INTERNAL = "INTERNAL"    # internal teams, chargeback only


class OwnerScope(StrEnum):
    PLATFORM = "PLATFORM"    # backend owned/pooled by the platform (RESELL/INTERNAL)
    TENANT = "TENANT"        # backend owned by the tenant (BYO)


class AuthMode(StrEnum):
    MI = "MI"                # managed identity to the backend (platform-owned)
    KV_SECRET = "KV_SECRET"  # per-tenant secret in Key Vault (BYO isolation)


class BudgetAction(StrEnum):
    ALERT = "alert"          # notify only
    BLOCK = "block"          # suspend subscription when exceeded


class TokenQuotaPeriod(StrEnum):
    """Fixed-window period for a virtual key's token quota. Values are the EXACT
    strings APIM's llm-token-limit `token-quota-period` accepts, so the DB value
    drops straight into the gateway policy expression — no mapping. Fixed window,
    UTC-aligned: the counter resets at the natural boundary (e.g. Daily -> UTC
    00:00), NOT a rolling window from first use."""
    HOURLY = "Hourly"
    DAILY = "Daily"
    WEEKLY = "Weekly"
    MONTHLY = "Monthly"
    YEARLY = "Yearly"


class TokenQuotaTier(StrEnum):
    """Preset token-quota amount. APIM's llm-token-limit `token-quota` attribute
    does NOT accept policy expressions (verified on dev-a01), so a per-key quota
    can't be an arbitrary value pushed via named value — it must be a literal in
    a <choose> branch. Hence fixed tiers: the policy has one branch per tier with
    the literal amount baked in. TOKEN_QUOTA_AMOUNTS maps each tier to its value;
    NONE means no quota gate. Edit these numbers to retune the tiers."""
    NONE = "none"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


# Tier -> literal token amount baked into the policy's <choose> branch.
# NONE has no entry (no quota gate applied). Retune freely.
TOKEN_QUOTA_AMOUNTS: dict[TokenQuotaTier, int] = {
    TokenQuotaTier.SMALL: 1_000_000,
    TokenQuotaTier.MEDIUM: 10_000_000,
    TokenQuotaTier.LARGE: 100_000_000,
}


class BudgetScope(StrEnum):
    TENANT = "TENANT"
    PROJECT = "PROJECT"
    KEY = "KEY"


class KeyStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    EXPIRED = "expired"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Provider(StrEnum):
    OPENAI = "openai"            # OpenAI / Kimi / DeepSeek (OpenAI-compatible, Bearer auth)
    ANTHROPIC = "anthropic"      # Claude (Anthropic Messages API)
    GOOGLE = "google"            # Gemini (OpenAI-compatible endpoint)
    AZURE = "azure"              # Azure OpenAI (api-key header + deployment routing)


class UserRole(StrEnum):
    ADMIN = "admin"              # platform operator (Entra ID)
    CUSTOMER = "customer"        # tenant user (Entra External ID / CIAM)


class DeployStatus(StrEnum):
    """Lifecycle of a GitHub-account-backed hub instance (GitModel).

    Each GitHub account = one deployed hub Container App that fronts that
    account's Copilot subscription. The control plane drives this state machine:
    device-flow login -> deploy infra -> register in APIM pools -> ready.
    """
    PENDING = "pending"          # device flow started, awaiting GitHub authorization
    DEPLOYING = "deploying"      # authorized; terraform apply + pool-join in progress
    READY = "ready"             # hub deployed and joined to the provider pools
    FAILED = "failed"            # deploy or pool-join errored (see error_detail)
    DELETING = "deleting"        # terraform destroy + pool-removal in progress
