"""Usage + billing router — enforces the tenant-isolation red line.

The customer endpoint derives tenant_id from the token (tenant_scope), NEVER
from a request param. An admin endpoint can read any tenant explicitly.

Usage records reach Cosmos through the hub -> Event Hub -> Capture -> importer
chain (app/services/usage_capture_import.py), which normalizes tokens and prices
each call from upstream's own `copilot_usage` at WRITE time. So the breakdowns
here are plain aggregations over already-normalized columns.

App Insights survives on exactly one endpoint (`/admin/usage-telemetry`), for
latency and failure counts. That is genuinely different data, not a second
opinion: Cosmos only ever sees calls that COMPLETED, so it cannot show a request
the gateway rejected. Everything token- or money-shaped comes from Cosmos, so
the portal and the invoice are reading the same rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin, tenant_scope
from app.db import get_db
from app.models.orm import Project, VirtualKey
from app.models.schemas import UsageSummary
from app.services.usage_ingest import AppInsightsUsage, UsageStore

router = APIRouter()

# Cosmos keeps usage documents for 90 days (container TTL), so a window wider
# than that can only ever return the same rows while scanning more partitions.
# Clamped rather than rejected: a stale bookmark asking for 365d should show 90
# days of data, not a 422.
_MAX_WINDOW_HOURS = 24 * 90


def _window_since(hours: int | None) -> str | None:
    """Start of the requested window as an ISO-8601 UTC string, or None.

    None means "all time" and is a deliberate option, not a missing value: the
    per-call log is often read as a ledger ("show me everything this key ever
    did") rather than as a dashboard slice.
    """
    if hours is None:
        return None
    hours = min(max(hours, 1), _MAX_WINDOW_HOURS)
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _tenant_key_ids(db: Session, tenant_id: str) -> list[str]:
    """Virtual-key ids belonging to a tenant (via its projects).

    Usage documents are tagged tenant='unknown' on the data plane, so a tenant's
    usage is resolved by matching its virtual keys against the document's
    `subscription` field. Returns [] if the tenant has no keys yet.
    """
    rows = (
        db.query(VirtualKey.id)
        .join(Project, VirtualKey.project_id == Project.id)
        .filter(Project.tenant_id == tenant_id)
        .all()
    )
    return [r[0] for r in rows]


def _extract_tokens(record: dict) -> tuple[int, int, int]:
    """Return (prompt, completion, cached) tokens from a usage record.

    Handles both the new APIM-written shape (tokens nested in raw_response) and
    any legacy flat shape. Providers name the fields differently:
      * OpenAI / Google chat: prompt_tokens / completion_tokens
      * Anthropic + OpenAI Responses: input_tokens / output_tokens
    """
    # Legacy flat record (worker/KQL era) — already normalized.
    if "prompt_tok" in record or "completion_tok" in record:
        return (
            int(record.get("prompt_tok", 0) or 0),
            int(record.get("completion_tok", 0) or 0),
            int(record.get("cached_tok", 0) or 0),
        )

    raw = record.get("raw_response")
    if not isinstance(raw, dict):
        return (0, 0, 0)
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return (0, 0, 0)

    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    cached = (
        usage.get("cache_read_input_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or (usage.get("input_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    return (int(prompt), int(completion), int(cached))


def _extract_cache_write(record: dict) -> int:
    """Cache-WRITE tokens, i.e. what it cost to populate the prompt cache.

    Separate from `_extract_tokens` rather than a fourth tuple element: the
    aggregation path has no use for it and changing that signature would touch
    code that is right as it is.

    Only upstream splits this out. The hub's own normalized counts have no
    cache-write equivalent, which is why the importer writes an explicit
    `cache_write_tok: 0` for those rows — so a missing key means "old document",
    not "no cache writes".

    Anthropic calls it `cache_creation_input_tokens`; OpenAI's schema has no
    equivalent, so this stays 0 there rather than being faked from another
    field.
    """
    if "cache_write_tok" in record:
        return int(record.get("cache_write_tok", 0) or 0)
    raw = record.get("raw_response")
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("cache_creation_input_tokens", 0) or 0)


def _summarize(tenant_id: str, rows: list[dict]) -> UsageSummary:
    summary = UsageSummary(tenant_id=tenant_id)
    for r in rows:
        prompt, completion, _cached = _extract_tokens(r)
        summary.total_prompt_tok += prompt
        summary.total_completion_tok += completion
        # Priced at write time from upstream's own `copilot_usage` (see
        # usage_capture_import). `or 0.0` still guards pre-cutover rows, which
        # carry no cost at all rather than a wrong one.
        summary.total_cost_usd += float(r.get("cost_usd", 0.0) or 0.0)
        summary.total_billed_usd += float(r.get("billed_usd", 0.0) or 0.0)
    summary.total_cost_usd = round(summary.total_cost_usd, 4)
    summary.total_billed_usd = round(summary.total_billed_usd, 4)
    return summary


def _to_record_view(r: dict, key_projects: dict[str, dict] | None = None) -> dict[str, Any]:
    """Flatten a raw usage document into a compact row for the portal's call
    log (time / model / key+project / tokens / cost).

    `key_projects` maps a virtual-key id -> {"project_id", "project_name"} so the
    log can show the owning project alongside the key (resolved from PostgreSQL;
    the Cosmos document only carries the key id under `subscription`).
    """
    prompt, completion, cached = _extract_tokens(r)
    sub = r.get("subscription") or r.get("subscription_id")
    proj = (key_projects or {}).get(sub or "")
    status = r.get("status")
    return {
        "ts": r.get("ts"),
        "subscription": sub,
        "project_id": proj.get("project_id") if proj else None,
        "project_name": proj.get("project_name") if proj else None,
        "route": r.get("route", "unknown"),
        "api": r.get("api"),
        # Surfaced so a rejected call is distinguishable from a served one. An
        # upstream 429 produces a zero-token document; without this the log
        # shows it as a successful call that happened to use no tokens.
        # Documents written before `status` existed carry None — passed through
        # as null rather than defaulted, so the portal can show "—".
        "status": int(status) if isinstance(status, int) else None,
        "prompt_tok": prompt,
        "completion_tok": completion,
        "cached_tok": cached,
        "cache_write_tok": _extract_cache_write(r),
        # Priced at WRITE time from upstream's own `copilot_usage`, so the log
        # and the invoice cannot drift apart. cost = upstream's price;
        # billed = after our per-model markup.
        "cost_usd": float(r.get("cost_usd", 0.0) or 0.0),
        "billed_usd": float(r.get("billed_usd", 0.0) or 0.0),
        # Without this a $0.00 is ambiguous, and the portal would present the
        # ambiguity as fact. "copilot_usage" = upstream priced it and the answer
        # really is that number; "unpriced" = upstream priced NOTHING for this
        # call (non-Copilot backend, or a response shape we did not recognise)
        # and the 0 is a placeholder. The importer already refuses to guess from
        # a local price table for exactly this reason — surfacing the flag is
        # what keeps that refusal visible instead of silently rendering as $0.
        "cost_source": r.get("cost_source"),
        # True when the hub had to ESTIMATE token counts because upstream
        # returned none. Such rows must not be used to settle a billing dispute,
        # so the log has to say so rather than showing them like measured ones.
        "estimated": bool(r.get("estimated")),
        "streamed": bool(r.get("streamed")),
    }


def _usage_breakdown_payload(
    key_ids: list[str] | None, hours: int, by: str
) -> dict[str, Any]:
    """Shared shape for the breakdown endpoints: per-group token + cost split.

    `key_ids` restricts to a tenant's virtual keys; None = platform-wide (admin).
    `by` selects the grouping dimension: "model" (default), "api"/"endpoint",
    "subscription" (virtual key), "backend"/"hub", or "end_user".

    Returns {"by", "hours", "bucket", "groups", "trend", "totals"}. Each group
    carries per-type token counts, `calls`, and — new with the Cosmos source —
    `cost_usd`/`billed_usd`, so "which model spent the money" is answerable
    without leaving this payload.

    `bucket` reports the trend granularity the window resolved to ("hour" or
    "day") so the chart can label points correctly instead of inferring it.

    `totals` is queried independently rather than summed from `groups`: the group
    list can be truncated on a high-cardinality dimension, and the headline
    numbers should stay whole when it is.
    """
    store = UsageStore()
    # Normalize the requested grouping to a canonical dimension name.
    group_by = {"endpoint": "api", "hub": "backend"}.get(by, by)
    if group_by not in UsageStore._AGG_DIMS:
        group_by = "model"
    hours = min(max(hours, 1), _MAX_WINDOW_HOURS)
    since = _window_since(hours)
    return {
        "by": group_by,
        "hours": hours,
        "bucket": UsageStore.trend_bucket(hours),
        "groups": store.cost_breakdown(key_ids, since_iso=since, group_by=group_by),
        "trend": store.cost_trend(key_ids, since_iso=since, hours=hours),
        "totals": store.cost_totals(key_ids, since_iso=since),
    }


def _key_project_map(db: Session, tenant_id: str) -> dict[str, dict]:
    """Map each of a tenant's virtual-key ids -> its owning project (id + name).

    Used to label the call log: Cosmos records the key id, the human-readable
    project comes from PostgreSQL.
    """
    rows = (
        db.query(VirtualKey.id, Project.id, Project.name)
        .join(Project, VirtualKey.project_id == Project.id)
        .filter(Project.tenant_id == tenant_id)
        .all()
    )
    return {
        r[0]: {"project_id": r[1], "project_name": r[2]} for r in rows
    }


@router.get("/usage", response_model=UsageSummary)
def my_usage(
    hours: int | None = None,
    tenant_id: str = Depends(tenant_scope),
    db: Session = Depends(get_db),
) -> UsageSummary:
    """Customer self-service: usage for the CALLER's tenant only.

    `hours` omitted = lifetime total, which is what this endpoint has always
    returned and what the customer page shows."""
    key_ids = _tenant_key_ids(db, tenant_id)
    rows = UsageStore().query_by_subscriptions(key_ids, since_iso=_window_since(hours))
    return _summarize(tenant_id, rows)


@router.get("/admin/usage/{tenant_id}", response_model=UsageSummary)
def tenant_usage(
    tenant_id: str,
    hours: int | None = None,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UsageSummary:
    """Platform admin: usage for an explicitly named tenant, optionally windowed."""
    key_ids = _tenant_key_ids(db, tenant_id)
    rows = UsageStore().query_by_subscriptions(key_ids, since_iso=_window_since(hours))
    return _summarize(tenant_id, rows)


@router.get("/admin/usage/{tenant_id}/records")
def tenant_usage_records(
    tenant_id: str,
    page: int = 1,
    page_size: int = 25,
    hours: int | None = None,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Platform admin: per-call usage log (Cosmos source) for one tenant,
    server-side paginated.

    Resolves the tenant's virtual keys, then returns the matching page of call
    records plus the total count so the portal can render page controls. The
    count applies the same window as the page query — otherwise the pager offers
    pages that come back empty.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    key_ids = _tenant_key_ids(db, tenant_id)
    since = _window_since(hours)
    store = UsageStore()
    rows = store.query_by_subscriptions(
        key_ids, limit=page_size, skip=(page - 1) * page_size, since_iso=since
    )
    key_projects = _key_project_map(db, tenant_id)
    return {
        "items": [_to_record_view(r, key_projects) for r in rows],
        "total": store.count_by_subscriptions(key_ids, since_iso=since),
        "page": page,
        "page_size": page_size,
        "hours": hours,
    }


@router.get("/admin/usage-telemetry")
def usage_telemetry(
    hours: int = 24,
    _: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Platform admin: call counts + latency from App Insights (separate data
    source from Cosmos usage). Best-effort — returns an empty summary if App
    Insights isn't configured."""
    return AppInsightsUsage().request_telemetry(
        hours=min(max(hours, 1), _MAX_WINDOW_HOURS)
    )


@router.get("/usage/breakdown")
def my_usage_breakdown(
    hours: int = 24,
    by: str = "model",
    tenant_id: str = Depends(tenant_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Customer self-service: token + cost breakdown for the CALLER's tenant,
    grouped by model (default), api/endpoint, subscription, backend or end_user.

    Sourced from Cosmos, which covers streaming and non-streaming calls alike and
    carries the same per-call cost the invoice is built from — so a customer
    querying this sees the numbers they will be billed on, not an estimate."""
    key_ids = _tenant_key_ids(db, tenant_id)
    return _usage_breakdown_payload(key_ids, hours=hours, by=by)


@router.get("/admin/usage/{tenant_id}/breakdown")
def tenant_usage_breakdown(
    tenant_id: str,
    hours: int = 24,
    by: str = "model",
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Platform admin: token breakdown for an explicitly named tenant."""
    key_ids = _tenant_key_ids(db, tenant_id)
    return _usage_breakdown_payload(key_ids, hours=hours, by=by)


@router.get("/admin/usage-breakdown")
def platform_usage_breakdown(
    hours: int = 24,
    by: str = "model",
    _: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Platform admin: token breakdown across ALL keys/tenants (no subscription
    filter). Useful for the platform dashboard's per-model view."""
    return _usage_breakdown_payload(None, hours=hours, by=by)
