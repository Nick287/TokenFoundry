"""Usage + billing router — enforces the tenant-isolation red line.

The customer endpoint derives tenant_id from the token (tenant_scope), NEVER
from a request param. An admin endpoint can read any tenant explicitly.

Usage records are written by the APIM outbound policy (one document per LLM
call, carrying the full backend response under `raw_response`). Token counts
live inside that raw response in provider-specific shapes, so we normalize them
here at read time rather than at write time.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin, tenant_scope
from app.db import get_db
from app.models.orm import Project, VirtualKey
from app.models.schemas import UsageSummary
from app.services.usage_ingest import AppInsightsUsage, UsageStore

router = APIRouter()


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


def _summarize(tenant_id: str, rows: list[dict]) -> UsageSummary:
    summary = UsageSummary(tenant_id=tenant_id)
    for r in rows:
        prompt, completion, _cached = _extract_tokens(r)
        summary.total_prompt_tok += prompt
        summary.total_completion_tok += completion
        # cost/billed are not computed at write time yet; fall back to any value
        # already on the record (legacy) so the field is populated when present.
        summary.total_cost_usd += float(r.get("cost_usd", 0.0) or 0.0)
        summary.total_billed_usd += float(r.get("billed_usd", 0.0) or 0.0)
    summary.total_cost_usd = round(summary.total_cost_usd, 4)
    summary.total_billed_usd = round(summary.total_billed_usd, 4)
    return summary


def _to_record_view(r: dict, key_projects: dict[str, dict] | None = None) -> dict[str, Any]:
    """Flatten a raw usage document into a compact row for the portal's call
    log (time / model / key+project / tokens).

    `key_projects` maps a virtual-key id -> {"project_id", "project_name"} so the
    log can show the owning project alongside the key (resolved from PostgreSQL;
    the Cosmos document only carries the key id under `subscription`).
    """
    prompt, completion, cached = _extract_tokens(r)
    sub = r.get("subscription") or r.get("subscription_id")
    proj = (key_projects or {}).get(sub or "")
    return {
        "ts": r.get("ts"),
        "subscription": sub,
        "project_id": proj.get("project_id") if proj else None,
        "project_name": proj.get("project_name") if proj else None,
        "route": r.get("route", "unknown"),
        "api": r.get("api"),
        "prompt_tok": prompt,
        "completion_tok": completion,
        "cached_tok": cached,
    }


def _usage_breakdown_payload(
    key_ids: list[str] | None, hours: int, by: str
) -> dict[str, Any]:
    """Shared shape for the breakdown endpoints: per-group token split + trend.

    `key_ids` restricts to a tenant's virtual keys; None = platform-wide (admin).
    `by` selects the grouping dimension: "model" (default), "api"/"endpoint", or
    "subscription" (virtual key). Returns {"by", "hours", "groups", "trend",
    "totals"} where each group has total/prompt/cached/completion/reasoning token
    counts + calls, and trend carries per-bucket tokens + calls (dual-line chart).
    """
    ai = AppInsightsUsage()
    # Normalize the requested grouping to a canonical dimension name.
    group_by = {"endpoint": "api"}.get(by, by)
    if group_by not in ("model", "api", "subscription"):
        group_by = "model"
    groups = ai.token_usage_breakdown(key_ids, hours=hours, group_by=group_by)
    trend = ai.token_usage_trend(key_ids, hours=hours)
    totals = {
        k: sum(int(g.get(k, 0) or 0) for g in groups)
        for k in ("total", "prompt", "cached", "completion", "reasoning", "calls")
    }
    return {
        "by": group_by,
        "hours": hours,
        "groups": groups,
        "trend": trend,
        "totals": totals,
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
    tenant_id: str = Depends(tenant_scope),
    db: Session = Depends(get_db),
) -> UsageSummary:
    """Customer self-service: usage for the CALLER's tenant only."""
    key_ids = _tenant_key_ids(db, tenant_id)
    rows = UsageStore().query_by_subscriptions(key_ids)
    return _summarize(tenant_id, rows)


@router.get("/admin/usage/{tenant_id}", response_model=UsageSummary)
def tenant_usage(
    tenant_id: str,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UsageSummary:
    """Platform admin: usage for an explicitly named tenant."""
    key_ids = _tenant_key_ids(db, tenant_id)
    rows = UsageStore().query_by_subscriptions(key_ids)
    return _summarize(tenant_id, rows)


@router.get("/admin/usage/{tenant_id}/records")
def tenant_usage_records(
    tenant_id: str,
    page: int = 1,
    page_size: int = 25,
    _: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Platform admin: per-call usage log (Cosmos source) for one tenant,
    server-side paginated.

    Resolves the tenant's virtual keys, then returns the matching page of call
    records plus the total count so the portal can render page controls.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    key_ids = _tenant_key_ids(db, tenant_id)
    store = UsageStore()
    rows = store.query_by_subscriptions(
        key_ids, limit=page_size, skip=(page - 1) * page_size
    )
    key_projects = _key_project_map(db, tenant_id)
    return {
        "items": [_to_record_view(r, key_projects) for r in rows],
        "total": store.count_by_subscriptions(key_ids),
        "page": page,
        "page_size": page_size,
    }


@router.get("/admin/usage-telemetry")
def usage_telemetry(
    hours: int = 24,
    _: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Platform admin: call counts + latency from App Insights (separate data
    source from Cosmos usage). Best-effort — returns an empty summary if App
    Insights isn't configured."""
    return AppInsightsUsage().request_telemetry(hours=hours)


@router.get("/usage/breakdown")
def my_usage_breakdown(
    hours: int = 24,
    by: str = "model",
    tenant_id: str = Depends(tenant_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Customer self-service: token breakdown (App Insights) for the CALLER's
    tenant, grouped by model (default) or api/endpoint, split by token type
    (total / prompt / cached / completion / reasoning), plus a total-token trend.

    App Insights metering covers BOTH streaming and non-streaming calls, so this
    reflects true token consumption (the Cosmos call log skips SSE)."""
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
