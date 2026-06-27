"""Budget enforcement: the lagging $ ceiling APIM can't do itself.

APIM's llm-token-limit enforces TPM, not dollars. This service accumulates
spend against Budget records and, when a BLOCK-action budget is exceeded,
suspends the offending APIM subscription(s) via the provisioner. Soft overage
is tolerated by design (eventual enforcement).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import BudgetAction, KeyStatus
from app.models.orm import Budget, VirtualKey
from app.services.apim_provisioner import ApimProvisioner

logger = logging.getLogger(__name__)


class BudgetEnforcer:
    def __init__(self, provisioner: ApimProvisioner | None = None) -> None:
        self._provisioner = provisioner or ApimProvisioner()

    def add_spend(self, db: Session, budget: Budget, amount_usd: float) -> bool:
        """Add spend; enforce if over a BLOCK budget. Returns True if enforced."""
        budget.spent_usd = (budget.spent_usd or 0.0) + amount_usd
        db.add(budget)

        if budget.spent_usd < budget.limit_usd:
            db.commit()
            return False

        if budget.action == BudgetAction.ALERT:
            logger.info(
                "budget %s exceeded (%.2f/%.2f) — alert only",
                budget.id,
                budget.spent_usd,
                budget.limit_usd,
            )
            db.commit()
            return False

        # BLOCK: suspend the affected key(s)
        enforced = self._suspend_keys_for_budget(db, budget)
        db.commit()
        return enforced

    def _suspend_keys_for_budget(self, db: Session, budget: Budget) -> bool:
        keys = self._keys_in_scope(db, budget)
        any_enforced = False
        for key in keys:
            if key.status == KeyStatus.SUSPENDED or not key.apim_subscription_id:
                continue
            self._provisioner.set_subscription_state(
                key.apim_subscription_id, "suspended"
            )
            key.status = KeyStatus.SUSPENDED
            db.add(key)
            any_enforced = True
            logger.warning(
                "suspended key %s (budget %s exceeded)", key.id, budget.id
            )
        return any_enforced

    @staticmethod
    def _keys_in_scope(db: Session, budget: Budget) -> list[VirtualKey]:
        from app.models.enums import BudgetScope

        stmt = select(VirtualKey)
        if budget.scope == BudgetScope.KEY:
            stmt = stmt.where(VirtualKey.id == budget.target_id)
        elif budget.scope == BudgetScope.PROJECT:
            stmt = stmt.where(VirtualKey.project_id == budget.target_id)
        else:  # TENANT — join via project
            from app.models.orm import Project

            project_ids = select(Project.id).where(Project.tenant_id == budget.target_id)
            stmt = stmt.where(VirtualKey.project_id.in_(project_ids))
        return list(db.scalars(stmt).all())
