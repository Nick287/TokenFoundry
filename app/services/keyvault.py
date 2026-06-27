"""Key Vault wrapper for subscription keys and BYO provider secrets.

The control plane never persists raw secrets in PostgreSQL — only Key Vault
references. This module is the single choke point for set/get/delete so the
isolation rule (per-tenant secrets, BYO never leaks across tenants) lives in
one place.
"""

from __future__ import annotations

import logging

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient

from app.config import get_settings

logger = logging.getLogger(__name__)


def _credential():
    """Pick a credential that works both locally and in Container Apps.

    In cloud the app has BOTH a system-assigned and a user-assigned identity;
    a bare DefaultAzureCredential can pick the WRONG one (the read-only pull
    identity) when writing secrets. We prefer the system-assigned identity
    explicitly there, and fall back to DefaultAzureCredential locally (az login).
    """
    settings = get_settings()
    if settings.is_local:
        return DefaultAzureCredential()
    # Cloud: system-assigned managed identity (no client_id) — the one granted
    # Key Vault Secrets Officer for read/write.
    return ManagedIdentityCredential()


class KeyVaultService:
    def __init__(self, vault_uri: str | None = None) -> None:
        settings = get_settings()
        self._uri = vault_uri or settings.keyvault_uri
        self._client: SecretClient | None = None

    @property
    def client(self) -> SecretClient:
        if self._client is None:
            self._client = SecretClient(
                vault_url=self._uri, credential=_credential()
            )
        return self._client

    def set_secret(self, name: str, value: str) -> str:
        """Store a secret; return its Key Vault reference (the secret id)."""
        try:
            result = self.client.set_secret(name, value)
        except Exception:
            logger.exception("Key Vault set_secret failed for %s at %s", name, self._uri)
            raise
        return result.id or f"{self._uri}/secrets/{name}"

    def get_secret(self, name: str) -> str | None:
        secret = self.client.get_secret(name)
        return secret.value

    def delete_secret(self, name: str) -> None:
        self.client.begin_delete_secret(name)

    @staticmethod
    def subscription_key_name(virtual_key_id: str) -> str:
        # KV secret names allow only alphanumerics and hyphens — the key id
        # carries an underscore (vk_xxx), so normalize it. Also avoids the
        # doubled prefix (vk-vk_...).
        return virtual_key_id.replace("_", "-")

    @staticmethod
    def backend_secret_name(route_id: str) -> str:
        # Same KV naming constraint (no underscores).
        return f"route-{route_id}-backend".replace("_", "-")
