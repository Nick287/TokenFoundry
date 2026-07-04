"""Async Azure OpenAI image client (gpt-image-2).

A second backend alongside ``copilot_client``: where Copilot serves the *text*
endpoints, this serves *image generation / editing* through an Azure OpenAI
``gpt-image`` deployment. Configuration (endpoint URL + API key + default model)
is stored in the SQLite ``kv`` table via :mod:`hub.store` and managed from the
web portal — nothing is read from the environment and no secret lives in code.

The Azure "v1" surface is OpenAI-compatible:

    POST {base}/images/generations    (JSON)
    POST {base}/images/edits          (multipart/form-data)

where ``{base}`` ends in ``/openai/v1``. Responses follow the standard OpenAI
Images shape — ``data[].b64_json`` plus a ``usage`` block with
``input_tokens`` / ``output_tokens`` / ``total_tokens``.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import store

# Default deployment/model name for the Azure gpt-image resource.
DEFAULT_IMAGE_MODEL = "gpt-image-2"

_http_client: httpx.AsyncClient | None = None


class NotConfiguredError(RuntimeError):
    """Raised when the Azure image backend has no endpoint / API key set."""


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        # Image generation is slow (tens of seconds at high quality); give it a
        # generous read timeout but a short connect timeout.
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))
    return _http_client


# --------------------------------------------------------------------------- #
# Configuration (persisted in SQLite via store)
# --------------------------------------------------------------------------- #
def get_config() -> dict[str, str]:
    """Return ``{endpoint, api_key, model}`` (any may be empty)."""
    return store.get_image_config()


def is_configured() -> bool:
    cfg = get_config()
    return bool(cfg.get("endpoint") and cfg.get("api_key"))


def get_model() -> str:
    return get_config().get("model") or DEFAULT_IMAGE_MODEL


# --------------------------------------------------------------------------- #
# URL normalization
# --------------------------------------------------------------------------- #
def _derive_urls(endpoint: str) -> tuple[str, str]:
    """Derive (generations_url, edits_url) from a configured endpoint.

    Accepts any of:
      * the full ``.../openai/v1/images/generations`` URL (what the user pasted),
      * the ``.../openai/v1/images/edits`` URL,
      * a bare ``.../openai/v1`` base,
      * a bare resource host ``https://<res>.services.ai.azure.com`` (``/openai/v1``
        is appended).

    and returns both operation URLs under the same base.
    """
    base = (endpoint or "").strip().rstrip("/")
    # Strip a trailing operation suffix if present.
    for suffix in ("/images/generations", "/images/edits"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    # Ensure the base targets the OpenAI v1 surface.
    if not base.endswith("/openai/v1"):
        if base.endswith("/openai"):
            base += "/v1"
        else:
            base += "/openai/v1"
    return f"{base}/images/generations", f"{base}/images/edits"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_config().get('api_key', '')}"}


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
def _prepare_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy the payload, injecting the configured default model when absent."""
    body = dict(payload or {})
    if not body.get("model"):
        body["model"] = get_model()
    return body


async def generate(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Text-to-image. Buffered POST returning (status_code, json_body)."""
    if not is_configured():
        raise NotConfiguredError("Azure image backend not configured")
    gen_url, _ = _derive_urls(get_config()["endpoint"])
    r = await _client().post(
        gen_url,
        headers={"Content-Type": "application/json", **_headers()},
        json=_prepare_payload(payload),
    )
    try:
        body = r.json()
    except Exception:
        body = {"error": {"message": r.text}}
    return r.status_code, body


async def edit(
    data: dict[str, Any], files: list[tuple[str, tuple[str, bytes, str]]]
) -> tuple[int, dict[str, Any]]:
    """Image edit / inpainting. Multipart POST returning (status_code, json_body).

    ``data`` holds the scalar form fields (prompt, size, …); ``files`` holds the
    ``image`` (and optional ``mask``) parts in httpx's ``files=`` tuple shape:
    ``(field_name, (filename, content_bytes, content_type))``.
    """
    if not is_configured():
        raise NotConfiguredError("Azure image backend not configured")
    _, edit_url = _derive_urls(get_config()["endpoint"])
    form = {k: v for k, v in (data or {}).items() if v is not None}
    if not form.get("model"):
        form["model"] = get_model()
    r = await _client().post(
        edit_url,
        headers=_headers(),  # let httpx set the multipart Content-Type + boundary
        data=form,
        files=files,
    )
    try:
        body = r.json()
    except Exception:
        body = {"error": {"message": r.text}}
    return r.status_code, body
