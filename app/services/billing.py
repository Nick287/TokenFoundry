"""Cost calculation, resale markup, and chargeback.

There are two cost sources, in strict priority order:

1. **Upstream's own numbers** (`cost_from_copilot_usage`). GitHub Copilot returns
   a `copilot_usage` object on every response — per-token-type counts, the unit
   price for each, and a `total_nano_aiu` total. That is the authoritative
   figure: it is what we are actually charged, it already knows about cache
   read/write rate differences, and it moves the day upstream reprices without
   anyone editing a table here. Verified against Anthropic's published list
   prices (haiku-4.5: input $1/1M, output $5/1M — exact match).

2. **Our own price table** (`compute_cost`), per-route via
   `ModelRoute.price_in_per_1k / price_out_per_1k`. This is now only the
   fallback for calls where upstream gave us no `copilot_usage` — a
   non-Copilot backend (the image path goes to Azure OpenAI), or an upstream
   response shape we didn't recognise.

Markup is orthogonal to the source and applies identically to both. The three
tenant modes differ ONLY in markup:
  RESELL   -> billed = cost * (1 + markup_pct)
  BYO      -> markup_pct = 0; cost shown for visibility, platform fee separate
  INTERNAL -> markup_pct = 0; billed = cost, attributed to cost_center
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.orm import ModelRoute

# 1 AIU = $0.01, and the field is in NANO-AIU, so:
#   USD = total_nano_aiu * 1e-9 * 0.01 = total_nano_aiu / 1e11
NANO_AIU_PER_USD = 1e11

# One nano-AIU is $1e-11, so 11 decimals is exactly full fidelity — anything
# coarser silently discards value on cheap calls, and summing thousands of
# truncated rows drifts. The `compute_cost` fallback keeps its historical 6
# because our own table has nothing finer to express.
_USD_PLACES = 11

# `copilot_usage.token_details[].token_type` -> the UsageRecord field it feeds.
#
# cache_write earns its own column because upstream prices it separately and
# HIGHER than input ($125 vs $100 per 1M on Anthropic models). Without it the
# token columns silently understate what was billed for cache-heavy traffic, and
# a bill nobody can reconcile against the displayed tokens is worse than one
# extra column. The money itself is unaffected either way — cost comes from
# total_nano_aiu, never from re-multiplying these counts.
#
# Any token_type upstream adds that is NOT listed here is dropped from the flat
# columns. It survives verbatim in the document's `copilot_usage`, so adding a
# column later is a read-side change, not a data-loss recovery.
_TOKEN_TYPE_FIELDS = {
    "input": "prompt_tok",
    "output": "completion_tok",
    "cache_read": "cached_tok",
    "cache_write": "cache_write_tok",
}


@dataclass(frozen=True)
class CostBreakdown:
    cost_usd: float
    billed_usd: float


def _round_pair(cost: float, markup_pct: float, places: int) -> CostBreakdown:
    billed = cost * (1.0 + markup_pct)
    return CostBreakdown(
        cost_usd=round(cost, places), billed_usd=round(billed, places)
    )


def cost_from_copilot_usage(
    copilot_usage: Any, markup_pct: float = 0.0
) -> CostBreakdown | None:
    """Cost straight from upstream's `copilot_usage.total_nano_aiu`.

    Returns None when the object is missing or carries no usable total, so the
    caller can fall back to `compute_cost`. A total of **0 is a real answer**,
    not a missing one: upstream genuinely bills nothing for some models
    (gpt-4o-mini returns `cost_per_batch = 0` across the board), and we bill 0
    in turn rather than inventing a floor price.

    No arithmetic is done on `token_details` here. Upstream already summed it,
    and re-deriving the total would mean re-implementing its rounding — the
    per-type prices are kept for display and audit, not for our math.
    """
    if not isinstance(copilot_usage, dict):
        return None
    nano = copilot_usage.get("total_nano_aiu")
    if isinstance(nano, bool) or not isinstance(nano, int | float):
        return None
    return _round_pair(float(nano) / NANO_AIU_PER_USD, markup_pct, _USD_PLACES)


def tokens_from_copilot_usage(copilot_usage: Any) -> dict[str, int]:
    """Per-type token counts out of `copilot_usage.token_details`.

    Returns the UsageRecord-shaped keys (prompt_tok / completion_tok /
    cached_tok), zero-filled. This is a finer breakdown than the plain `usage`
    object gives — upstream splits cache reads out of the input count — so it
    is preferred when present.
    """
    out = dict.fromkeys(_TOKEN_TYPE_FIELDS.values(), 0)
    if not isinstance(copilot_usage, dict):
        return out
    details = copilot_usage.get("token_details")
    if not isinstance(details, list):
        return out
    for entry in details:
        if not isinstance(entry, dict):
            continue
        field = _TOKEN_TYPE_FIELDS.get(str(entry.get("token_type", "")))
        if not field:
            continue
        count = entry.get("token_count")
        if isinstance(count, bool) or not isinstance(count, int | float):
            continue
        out[field] += int(count)
    return out


def compute_cost(
    route: ModelRoute,
    prompt_tok: int,
    completion_tok: int,
    cached_tok: int = 0,
) -> CostBreakdown:
    """Fallback cost from OUR price table, for calls upstream didn't price.

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
    return _round_pair(cost, route.markup_pct, 6)
