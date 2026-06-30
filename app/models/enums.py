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
