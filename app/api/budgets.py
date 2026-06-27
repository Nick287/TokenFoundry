"""Budget router: define budgets (admin) and report spend.

Spend accrual + BLOCK enforcement happens in the usage pipeline via
BudgetEnforcer; this router is the CRUD surface.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin
from app.db import get_db
from app.models.orm import Budget
from app.models.schemas import BudgetCreate, BudgetOut

router = APIRouter()


@router.post("/budgets", response_model=BudgetOut, status_code=status.HTTP_201_CREATED)
def create_budget(
    body: BudgetCreate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> Budget:
    budget = Budget(
        id=f"bg_{uuid.uuid4().hex[:12]}",
        scope=body.scope,
        target_id=body.target_id,
        period_type=body.period_type,
        limit_usd=body.limit_usd,
        spent_usd=0.0,
        action=body.action,
        tenant_id=body.tenant_id,
    )
    db.add(budget)
    db.commit()
    db.refresh(budget)
    return budget


@router.get("/budgets", response_model=list[BudgetOut])
def list_budgets(
    db: Session = Depends(get_db), _: Principal = Depends(require_admin)
) -> list[Budget]:
    return list(db.query(Budget).all())
