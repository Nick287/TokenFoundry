"""Raw request/response archival for tenants that have opted into auditing.

This is a SEPARATE pipeline from `eventhub`, deliberately. Billing events are a
few hundred bytes and must never be lost; audit payloads are unbounded (a coding
agent's prompt carries whole files) and are merely nice to have. Putting the raw
bodies inside the billing event would couple them: one oversized prompt would
push the event past Event Hub's per-message ceiling and the *billing record*
would be the thing that disappears. So the raw text goes straight to Blob and the
billing event carries only a pointer.

Three properties this module must never violate:

1. **The request path never waits for a blob upload.** The blob name is derived
   from `(ts, subscription, request_id)` — all known before the upload starts —
   so `submit()` computes the pointer, hands the upload to a background task, and
   returns immediately. A slow storage account costs nothing at the gateway.
2. **Failure is silent and bounded.** Unconfigured, credential broken, storage
   down, too many uploads already in flight — every one degrades to a dropped
   payload plus a counter. The pointer in the usage event may therefore dangle;
   `dropped_count()` is what tells you that happened, and it is surfaced on
   /healthz next to the Event Hub counter.
3. **Nothing is archived unless the tenant opted in.** The gate is APIM's
   `x-tf-audit` header, which the gateway stamps with `exists-action="override"`
   from the control plane's per-subscription map. A client cannot switch its own
   auditing on (to poison the store) or off (to hide activity).

**What lands here is customer content** — source code, and in practice whatever
the customer pasted into a prompt, including secrets and personal data. The
container is therefore its own storage account with its own retention and its own
RBAC; the control plane's identity is NOT granted read access. See
terraform/modules/audit/.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
from datetime import datetime
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

# Lazily built singletons — importing azure.* is deferred so a hub with auditing
# switched off never pays the import cost or needs the package installed.
_client: Any = None
_credential: Any = None
_init_failed = False

# In-flight upload tasks. Bounded: past the cap we drop rather than let a slow
# storage account turn into unbounded memory growth on the gateway.
_inflight: set[asyncio.Task[None]] = set()
_MAX_INFLIGHT = 64

_dropped = 0

# Anything outside this set is replaced in the blob path. Subscription ids are
# already tame (APIM ids are alphanumeric+dash), but the value reaches us as a
# header and the path must not be forgeable into another tenant's prefix — the
# per-tenant prefix is what container-level access scoping would key on.
#
# `.` is excluded along with `/`, so a segment can never come out as `.` or
# `..`. Stripping the slash alone already makes escaping the prefix impossible
# (blob names are opaque strings, not resolved paths), but a literal `..`
# segment is a name Azure documents as best-avoided and that tooling further
# down the line may normalize. Nothing legitimate here contains a dot.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def dropped_count() -> int:
    """Audit payloads lost since process start (upload failed or never started)."""
    return _dropped


def enabled() -> bool:
    """True when the deployment has an audit container configured at all."""
    return get_settings().audit_enabled


def wants_audit(headers: Any) -> bool:
    """Whether THIS request's tenant opted in.

    Reads APIM's `x-tf-audit`. Default off: a missing header — which is every
    direct, non-APIM caller — means no archival.
    """
    try:
        return (headers.get("x-tf-audit") or "").strip() == "1"
    except Exception:  # noqa: BLE001 — a weird headers object must not 500 a call
        return False


def blob_path(ts: datetime, subscription: str | None, request_id: str) -> str:
    """Deterministic blob name for one request. Pure, so it is unit-testable.

    `YYYY/MM/DD/<subscription>/<request_id>.json.gz`

    Date first because retention and bulk export are time-scoped; tenant second
    so one customer's archive is a single prefix — that is what makes a
    per-tenant deletion (or a scoped SAS for a customer's own auditor) a prefix
    operation instead of a full scan.
    """
    sub = _UNSAFE.sub("_", (subscription or "unknown").strip() or "unknown")
    rid = _UNSAFE.sub("_", (request_id or "unknown").strip() or "unknown")
    return f"{ts:%Y/%m/%d}/{sub}/{rid}.json.gz"


def build_payload(
    *,
    request_id: str,
    ts: datetime,
    subscription: str | None,
    api_id: str | None,
    end_user: str | None,
    model: Any,
    endpoint: str,
    streamed: bool,
    status: int,
    request_body: Any,
    response_body: Any,
    max_bytes: int,
) -> bytes:
    """Serialize + gzip one audit record, truncating if it blows the size cap.

    Truncation over rejection: a 40 MB prompt still yields a record that proves
    who called what and when, which is most of the audit value. `truncated` marks
    it so nobody mistakes a clipped body for the whole exchange.

    gzip is not optional — SSE transcripts repeat their envelope on every chunk
    and compress by roughly an order of magnitude, and this store is charged by
    the byte for as long as retention lasts.
    """
    doc: dict[str, Any] = {
        "request_id": request_id,
        "ts": ts.isoformat(),
        "subscription": subscription,
        "api_id": api_id,
        "end_user": end_user,
        "model": model,
        "endpoint": endpoint,
        "streamed": streamed,
        "status": status,
        "request": request_body,
        "response": response_body,
        "truncated": False,
    }
    raw = json.dumps(doc, default=str, ensure_ascii=False).encode("utf-8")
    if len(raw) > max_bytes:
        # Clip the two unbounded fields and keep the envelope intact. Budget is
        # split between them so a huge prompt cannot squeeze out the response.
        half = max(1024, max_bytes // 2)
        doc["request"] = _clip(request_body, half)
        doc["response"] = _clip(response_body, half)
        doc["truncated"] = True
        raw = json.dumps(doc, default=str, ensure_ascii=False).encode("utf-8")
    return gzip.compress(raw)


def _clip(value: Any, limit: int) -> str:
    """Render a body as text no longer than `limit` characters, marking the cut."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[truncated {len(text) - limit} chars]"


async def _get_client() -> Any:
    """Return the container client, building it on first use. None when off."""
    global _client, _credential, _init_failed

    if _client is not None:
        return _client
    if _init_failed:
        return None

    st = get_settings()
    if not enabled():
        _init_failed = True
        return None

    try:
        from azure.identity.aio import DefaultAzureCredential
        from azure.storage.blob.aio import BlobServiceClient

        _credential = (
            DefaultAzureCredential(managed_identity_client_id=st.audit_client_id)
            if st.audit_client_id
            else DefaultAzureCredential()
        )
        service = BlobServiceClient(st.audit_account_url, credential=_credential)
        _client = service.get_container_client(st.audit_container)
        log.info("audit archive ready: %s/%s", st.audit_account_url, st.audit_container)
        return _client
    except Exception as exc:  # noqa: BLE001 — never let config break the gateway
        _init_failed = True
        log.warning("audit archive disabled (init failed): %s", exc)
        return None


async def _upload(path: str, blob: bytes) -> None:
    """Upload one payload. Never raises — a failed archive is a counter, not a 500."""
    global _dropped
    try:
        container = await _get_client()
        if container is None:
            _dropped += 1
            return
        await container.upload_blob(
            name=path,
            data=blob,
            overwrite=True,  # same request id = same exchange; retries are idempotent
            content_type="application/json",
            content_encoding="gzip",
        )
    except Exception as exc:  # noqa: BLE001
        _dropped += 1
        log.warning("dropped audit payload %s: %s", path, exc)


def submit(
    *,
    request_id: str,
    ts: datetime,
    subscription: str | None,
    api_id: str | None,
    end_user: str | None,
    model: Any,
    endpoint: str,
    streamed: bool,
    status: int,
    request_body: Any,
    response_body: Any,
) -> str | None:
    """Archive one exchange in the background; return its blob path immediately.

    The path is returned BEFORE the upload runs — that is the whole point, and
    the reason the usage event never waits on storage. It also means the pointer
    is a promise, not a receipt: if the upload later fails, `dropped_count()`
    rises and the pointer dangles.

    Returns None when auditing is off for this deployment, or when too many
    uploads are already in flight.
    """
    global _dropped
    if not enabled():
        return None
    if len(_inflight) >= _MAX_INFLIGHT:
        _dropped += 1
        log.warning("audit archive saturated (%d in flight); dropping", len(_inflight))
        return None

    path = blob_path(ts, subscription, request_id)
    try:
        blob = build_payload(
            request_id=request_id, ts=ts, subscription=subscription, api_id=api_id,
            end_user=end_user, model=model, endpoint=endpoint, streamed=streamed,
            status=status, request_body=request_body, response_body=response_body,
            max_bytes=get_settings().audit_max_bytes,
        )
        task = asyncio.create_task(_upload(path, blob))
    except Exception as exc:  # noqa: BLE001 — serialization must not break serving
        _dropped += 1
        log.warning("could not build audit payload for %s: %s", path, exc)
        return None

    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return path


async def aclose() -> None:
    """Drain in-flight uploads and release the connection. Never raises.

    Mirrors `eventhub.aclose()`: a graceful shutdown (including a Container Apps
    rolling update) finishes what it started, so only an ungraceful kill loses
    archives.
    """
    global _client, _credential, _init_failed
    if _inflight:
        await asyncio.gather(*list(_inflight), return_exceptions=True)
    client, credential = _client, _credential
    _client = _credential = None
    _init_failed = False
    for closeable in (client, credential):
        if closeable is None:
            continue
        try:
            await closeable.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("audit archive close failed: %s", exc)
