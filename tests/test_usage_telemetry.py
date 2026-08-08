"""Hermetic tests for the gateway-side call summary — no Azure, no network.

WHY THIS FILE EXISTS: the portal shows two failure counts side by side, one from
Cosmos (billing) and one from App Insights (gateway). They are NOT expected to be
equal — a request the circuit breaker sheds never reaches a hub, so it produces
no usage document — and the only way a reader can tell "different populations"
apart from "something is broken" is the per-status-code split. That split is
therefore load-bearing UI, and it had no coverage at all.

The Azure client is never constructed: `_run_kql` is replaced with canned rows,
which is the same seam every sub-query already goes through.
"""

from __future__ import annotations

import pytest

from app.services.usage_ingest import AppInsightsUsage


def _telemetry(
    base: list[dict] | None = None,
    codes: list[dict] | None = None,
) -> dict:
    """Run `request_telemetry` against canned KQL results.

    `_run_kql` is dispatched on a distinctive fragment of each query rather than
    call order, so re-ordering the sub-queries doesn't silently rewire the test.
    """
    store = AppInsightsUsage()
    store._resource_id = "/subscriptions/fake/providers/microsoft.insights/x"
    store._client = object()  # only truthiness is checked

    def fake_run_kql(kql: str, hours: int) -> list[dict]:
        # Dispatched on a fragment unique to each sub-query. "resultCode" alone
        # is NOT unique — the base query mentions it too, inside its countif —
        # and matching on it silently swaps the two result sets.
        if "count() by resultCode" in kql:
            return list(codes or [])
        if "percentile(duration" in kql:
            return list(base or [])
        return []

    store._run_kql = fake_run_kql  # type: ignore[assignment]
    return store.request_telemetry(24)


@pytest.fixture
def _base_rows() -> list[dict]:
    return [
        {"name": "POST /llm-openai", "calls": 100, "p50": 1.0, "p95": 2.0,
         "failures": 12},
        {"name": "POST /llm-anthropic", "calls": 50, "p50": 1.0, "p95": 2.0,
         "failures": 3},
    ]


def test_totals_split_into_succeeded_and_failed(_base_rows: list[dict]) -> None:
    out = _telemetry(base=_base_rows)
    assert out["total_calls"] == 150
    assert out["total_failures"] == 15
    assert out["total_ok"] == 135


def test_ok_and_failed_always_sum_to_total(_base_rows: list[dict]) -> None:
    """Two cards that don't add up to the third read as a bug in the dashboard
    even when every individual number is right."""
    out = _telemetry(base=_base_rows)
    assert out["total_ok"] + out["total_failures"] == out["total_calls"]


def test_status_codes_are_reported_for_reconciliation(_base_rows: list[dict]) -> None:
    """429 is the code that should match Cosmos; 503/404 are the codes that
    should NOT, because those requests never reached a hub."""
    out = _telemetry(
        base=_base_rows,
        codes=[{"resultCode": "200", "calls": 135},
               {"resultCode": "503", "calls": 9},
               {"resultCode": "429", "calls": 6}],
    )
    assert out["by_status"] == [
        {"status": "200", "calls": 135},
        {"status": "503", "calls": 9},
        {"status": "429", "calls": 6},
    ]


def test_failure_total_survives_a_failed_codes_query(_base_rows: list[dict]) -> None:
    """Each sub-query degrades independently. The headline comes from the base
    table's own `failures` column, so an empty codes query costs the chips but
    not the count."""
    out = _telemetry(base=_base_rows, codes=[])
    assert out["total_failures"] == 15
    assert out["by_status"] == []


def test_a_status_code_that_is_not_a_number_still_renders() -> None:
    """App Insights records client-side aborts with a non-numeric resultCode.
    Reaching for int() here would 500 the whole dashboard."""
    out = _telemetry(
        base=[{"name": "POST /llm-openai", "calls": 2, "failures": 1}],
        codes=[{"resultCode": None, "calls": 1}],
    )
    assert out["by_status"] == [{"status": "?", "calls": 1}]


def test_unconfigured_app_insights_still_exposes_the_keys() -> None:
    """The portal indexes these unconditionally; a missing key renders as
    `undefined`, which on a count looks exactly like a zero that was measured."""
    store = AppInsightsUsage()
    store._resource_id = None
    store._client = None
    out = store.request_telemetry(24)
    for key in ("total_calls", "total_ok", "total_failures", "by_status"):
        assert key in out, key


def test_no_telemetry_rows_returns_the_empty_shape_not_a_partial_one() -> None:
    """An empty base table short-circuits before the other queries run — that
    early return has to carry the same keys as the full one."""
    out = _telemetry(base=[])
    assert out["total_ok"] == 0
    assert out["total_failures"] == 0
    assert out["by_status"] == []
