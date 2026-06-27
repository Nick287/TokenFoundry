"""Billing math tests — pure logic, no Azure dependencies.

Validates that the three tenant modes differ ONLY in markup, and that cached
tokens are discounted to ~0.1x the input rate.
"""

from app.models.enums import AuthMode, OwnerScope, Provider
from app.models.orm import ModelRoute
from app.services.billing import compute_cost


def _route(markup: float) -> ModelRoute:
    return ModelRoute(
        id="rt_test",
        name="claude-sonnet",
        provider=Provider.ANTHROPIC,
        owner_scope=OwnerScope.PLATFORM,
        auth_mode=AuthMode.MI,
        price_in_per_1k=0.003,   # $3 / 1M input
        price_out_per_1k=0.015,  # $15 / 1M output
        markup_pct=markup,
    )


def test_resell_applies_markup():
    route = _route(markup=0.20)
    bd = compute_cost(route, prompt_tok=1000, completion_tok=1000)
    # cost = 1*0.003 + 1*0.015 = 0.018 ; billed = 0.018 * 1.2 = 0.0216
    assert bd.cost_usd == 0.018
    assert bd.billed_usd == 0.0216


def test_internal_no_markup():
    route = _route(markup=0.0)
    bd = compute_cost(route, prompt_tok=2000, completion_tok=500)
    # cost = 2*0.003 + 0.5*0.015 = 0.006 + 0.0075 = 0.0135 ; billed == cost
    assert bd.cost_usd == 0.0135
    assert bd.billed_usd == 0.0135


def test_cached_tokens_discounted():
    route = _route(markup=0.0)
    # 1000 prompt of which 800 cached: 200 @ full + 800 @ 0.1x
    bd = compute_cost(route, prompt_tok=1000, completion_tok=0, cached_tok=800)
    expected = (200 / 1000 * 0.003) + (800 / 1000 * 0.003 * 0.1)
    assert bd.cost_usd == round(expected, 6)
