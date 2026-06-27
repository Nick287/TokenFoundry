"""Usage + billing router — enforces the tenant-isolation red line.

The customer endpoint derives tenant_id from the token (tenant_scope), NEVER
from a request param. An admin endpoint can read any tenant explicitly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin, tenant_scope
from app.db import get_db
from app.models.schemas import UsageSummary
from app.services.usage_ingest import UsageStore

router = APIRouter()


def _summarize(tenant_id: str, rows: list[dict]) -> UsageSummary:
    summary = UsageSummary(tenant_id=tenant_id)
    for r in rows:
        summary.total_prompt_tok += int(r.get("prompt_tok", 0))
        summary.total_completion_tok += int(r.get("completion_tok", 0))
        summary.total_cost_usd += float(r.get("cost_usd", 0.0))
        summary.total_billed_usd += float(r.get("billed_usd", 0.0))
    summary.total_cost_usd = round(summary.total_cost_usd, 4)
    summary.total_billed_usd = round(summary.total_billed_usd, 4)
    return summary


@router.get("/usage", response_model=UsageSummary)
def my_usage(tenant_id: str = Depends(tenant_scope)) -> UsageSummary:
    """Customer self-service: usage for the CALLER's tenant only."""
    rows = UsageStore().query_tenant(tenant_id)
    return _summarize(tenant_id, rows)


@router.get("/admin/usage/{tenant_id}", response_model=UsageSummary)
def tenant_usage(
    tenant_id: str,
    _: Principal = Depends(require_admin),
    __: Session = Depends(get_db),
) -> UsageSummary:
    """Platform admin: usage for an explicitly named tenant."""
    rows = UsageStore().query_tenant(tenant_id)
    return _summarize(tenant_id, rows)
