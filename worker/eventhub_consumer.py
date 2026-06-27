"""Event Hub consumer worker (Phase 2).

Phase 1 uses App Insights KQL pull (see app/services/usage_ingest.py); this
worker becomes the billing source of truth in Phase 2: consume structured usage
events from Event Hub, compute cost via app.services.billing, write raw records
to Cosmos, roll up to PostgreSQL, and drive BudgetEnforcer.

Stub entrypoint — wired in Phase 2.
"""

from __future__ import annotations


def main() -> None:  # pragma: no cover - Phase 2
    raise NotImplementedError(
        "Event Hub consumer is implemented in Phase 2; Phase 1 uses KQL pull."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
