"""Cost calculation, resale markup, and chargeback.

Pricing is per-route (ModelRoute.price_in_per_1k / price_out_per_1k), so each
provider (Claude, Gemini, Kimi, ...) carries its own rates. The three tenant
modes differ ONLY in markup:
  RESELL   -> billed = cost * (1 + markup_pct)
  BYO      -> markup_pct = 0; cost shown for visibility, platform fee separate
  INTERNAL -> markup_pct = 0; billed = cost, attributed to cost_center
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.orm import ModelRoute


@dataclass(frozen=True)
class CostBreakdown:
    cost_usd: float
    billed_usd: float


def compute_cost(
    route: ModelRoute,
    prompt_tok: int,
    completion_tok: int,
    cached_tok: int = 0,
) -> CostBreakdown:
    """Compute raw provider cost and the amount billed to the tenant.

    Cached prompt tokens (e.g. Claude prompt caching) are billed at ~0.1x the
    input rate; they're already counted inside prompt_tok by most providers, so
    we discount the cached portion rather than adding it.
    """
    billable_prompt = max(prompt_tok - cached_tok, 0)
    cost = (
        billable_prompt / 1000.0 * route.price_in_per_1k
        + cached_tok / 1000.0 * route.price_in_per_1k * 0.1
        + completion_tok / 1000.0 * route.price_out_per_1k
    )
    billed = cost * (1.0 + route.markup_pct)
    return CostBreakdown(cost_usd=round(cost, 6), billed_usd=round(billed, 6))
