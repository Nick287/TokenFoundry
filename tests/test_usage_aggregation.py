"""Cosmos-side usage aggregation.

These aggregates back the portal's cost view and any billing reconciliation, and
every failure mode here is silent:

* A dimension name reaching the SQL string from the caller would be an injection
  hole — Cosmos has no bind parameter for an identifier, so the projected column
  is built by string interpolation and the ONLY thing keeping that safe is the
  whitelist. A test that pins it is the difference between "safe" and "safe today".
* Legacy documents carry JSON null for columns that did not exist when they were
  written (verified in the deployed container: `cache_write_tok: null`), and
  `null + int` raises. One old row must not 500 the whole dashboard.
* An empty subscription list means "a tenant with no keys", NOT "no filter". Lose
  that distinction and one tenant's dashboard shows every tenant's spend.

WHY THESE TESTS DO NOT ASSERT SQL AGGREGATE SYNTAX
--------------------------------------------------
An earlier version of this module aggregated server-side (`SUM(...) ... GROUP BY`)
and these tests passed against a fake container while production returned 500:

    (BadRequest) Cross partition query only supports 'VALUE <AggregateFunc>'
                 for aggregates.

A fake proves the query TEXT is what we intended; it cannot prove Cosmos accepts
it. So the shape assertions here are limited to things a fake can honestly
witness — the filter, the parameterization, the projection — and the question of
what Cosmos executes is settled by `tests/manual/probe_cosmos_aggregates.py`
against a real account, not guessed at here.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.usage_ingest import UsageStore

_EMPTY_TOTALS = {
    "calls": 0,
    "prompt_tok": 0,
    "cached_tok": 0,
    "cache_write_tok": 0,
    "completion_tok": 0,
    "cost_usd": 0.0,
    "billed_usd": 0.0,
}


class _FakeContainer:
    """Captures the query/params it was called with; returns canned rows."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    def query_items(self, **kwargs: Any) -> list[dict]:
        self.calls.append(kwargs)
        return list(self.rows)

    @property
    def last_query(self) -> str:
        return self.calls[-1]["query"]

    @property
    def last_params(self) -> dict[str, Any]:
        return {p["name"]: p["value"] for p in self.calls[-1]["parameters"]}


def _store(rows: list[dict] | None = None) -> tuple[UsageStore, _FakeContainer]:
    """A store wired to a fake container, with a non-empty endpoint so the
    "Cosmos not configured" guard doesn't short-circuit the call under test."""
    store = UsageStore()
    store._endpoint = "https://fake.documents.azure.com"
    fake = _FakeContainer(rows)
    type(store)._container = property(lambda self: fake)  # type: ignore[assignment]
    return store, fake


@pytest.fixture(autouse=True)
def _restore_container_property() -> Any:
    """Put the real `_container` property back after each test — `_store` patches
    it on the CLASS, so leaking it would poison every later test."""
    original = UsageStore._container
    yield
    UsageStore._container = original  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Dimension whitelist — the injection boundary                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("dim", "field"),
    [
        ("model", "route"),
        ("api", "api"),
        ("subscription", "subscription"),
        ("backend", "hub_id"),
        ("end_user", "end_user"),
    ],
)
def test_each_dimension_projects_and_groups_on_its_document_field(
    dim: str, field: str
) -> None:
    store, fake = _store([{field: "x", "cost_usd": 1.0}])
    out = store.cost_breakdown(["vk_a"], since_iso="2026-08-04T00:00:00+00:00", group_by=dim)
    # The dimension is fetched...
    assert f"c.{field}" in fake.last_query
    # ...and the result is keyed by the REQUESTED name, not the document field,
    # because that is the contract the portal renders against.
    assert out[0][dim] == "x"


@pytest.mark.parametrize(
    "hostile",
    [
        "route; DROP DATABASE x",
        "route OR 1=1",
        "'; SELECT * FROM c --",
        "unknown_dimension",
        "",
    ],
)
def test_unknown_dimension_queries_nothing(hostile: str) -> None:
    """Rejected BEFORE any query is issued — not sanitized, not defaulted."""
    store, fake = _store()
    assert store.cost_breakdown(["vk_a"], group_by=hostile) == []
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Tenant scoping                                                               #
# --------------------------------------------------------------------------- #
def test_empty_key_list_never_queries() -> None:
    """A tenant with no keys must not degrade into an unfiltered query over
    every tenant's usage."""
    store, fake = _store()
    assert store.cost_breakdown([], group_by="model") == []
    assert store.cost_totals([]) == _EMPTY_TOTALS
    assert store.cost_trend([]) == []
    assert fake.calls == []


def test_none_key_list_is_platform_wide() -> None:
    """None (admin) is a DIFFERENT thing from [] — it means no filter at all."""
    store, fake = _store()
    store.cost_breakdown(None, group_by="model")
    assert "ARRAY_CONTAINS" not in fake.last_query
    assert "@ids" not in fake.last_params


def test_key_list_is_parameterized_not_interpolated() -> None:
    store, fake = _store()
    store.cost_breakdown(["vk_a", "vk_b"], group_by="model")
    assert "ARRAY_CONTAINS(@ids, c.subscription)" in fake.last_query
    assert fake.last_params["@ids"] == ["vk_a", "vk_b"]
    assert "vk_a" not in fake.last_query


def test_since_is_parameterized() -> None:
    store, fake = _store()
    store.cost_breakdown(["vk_a"], since_iso="2026-08-04T00:00:00+00:00", group_by="model")
    assert "c.ts >= @since" in fake.last_query
    assert fake.last_params["@since"] == "2026-08-04T00:00:00+00:00"


def test_unconfigured_cosmos_returns_empty_not_raises() -> None:
    """Local dev with no Cosmos account: the portal must still render."""
    store = UsageStore()
    store._endpoint = ""
    assert store.cost_breakdown(["vk_a"], group_by="model") == []
    assert store.cost_trend(["vk_a"]) == []
    assert store.cost_totals(["vk_a"])["calls"] == 0


# --------------------------------------------------------------------------- #
# Query shape (only what a fake can honestly witness)                          #
# --------------------------------------------------------------------------- #
def test_queries_project_columns_and_never_select_star() -> None:
    """`SELECT *` would drag the verbatim `copilot_usage` and raw provider
    `usage` blobs across the wire for every row — far larger than the handful of
    numbers being summed."""
    store, fake = _store()
    store.cost_breakdown(["vk_a"], group_by="model")
    assert "SELECT *" not in fake.last_query
    for f in ("prompt_tok", "cached_tok", "cache_write_tok", "completion_tok",
              "cost_usd", "billed_usd"):
        assert f"c.{f}" in fake.last_query


def test_no_server_side_aggregate_syntax_is_emitted() -> None:
    """The regression that shipped a 500: cross-partition Cosmos rejects every
    aggregate but a bare `SELECT VALUE <Agg>`, and the partition key
    (<subscription>_<yyyyMM>) makes EVERY breakdown cross-partition."""
    store, fake = _store()
    store.cost_breakdown(["vk_a"], group_by="model")
    store.cost_totals(["vk_a"])
    store.cost_trend(["vk_a"])
    for call in fake.calls:
        q = call["query"].upper()
        assert "GROUP BY" not in q
        assert "SUM(" not in q
        assert "COUNT(" not in q


def test_rows_are_capped_and_newest_first() -> None:
    """If the cap bites, what survives should be the most recent window — a
    partial view of "just now" is explicable; an arbitrary slice is not."""
    store, fake = _store()
    store.cost_breakdown(["vk_a"], group_by="model")
    assert "ORDER BY c.ts DESC" in fake.last_query
    assert "LIMIT @n" in fake.last_query
    assert fake.last_params["@n"] == UsageStore._MAX_ROWS


def test_all_queries_are_cross_partition() -> None:
    """pk is <subscription>_<yyyyMM>, so any multi-key or multi-month aggregate
    spans partitions by construction."""
    store, fake = _store()
    store.cost_breakdown(["vk_a"], group_by="model")
    store.cost_totals(["vk_a"])
    store.cost_trend(["vk_a"])
    assert all(c["enable_cross_partition_query"] for c in fake.calls)


# --------------------------------------------------------------------------- #
# Aggregation correctness                                                      #
# --------------------------------------------------------------------------- #
def test_breakdown_folds_rows_and_sorts_by_cost() -> None:
    store, _ = _store(
        [
            {"route": "gpt-4o-mini", "prompt_tok": 10, "cached_tok": 0,
             "cache_write_tok": 0, "completion_tok": 5, "cost_usd": 0.0, "billed_usd": 0.0},
            {"route": "gpt-4o-mini", "prompt_tok": 4, "cached_tok": 1,
             "cache_write_tok": 0, "completion_tok": 2, "cost_usd": 0.0, "billed_usd": 0.0},
            {"route": "claude-opus-4.8", "prompt_tok": 100, "cached_tok": 20,
             "cache_write_tok": 7, "completion_tok": 50, "cost_usd": 1.25, "billed_usd": 1.5},
        ]
    )
    out = store.cost_breakdown(["vk_a"], group_by="model")
    # Costliest first — a free model with more calls must not head a table whose
    # question is "where did the money go".
    assert [g["model"] for g in out] == ["claude-opus-4.8", "gpt-4o-mini"]
    assert out[1]["calls"] == 2
    assert out[1]["prompt_tok"] == 14
    assert out[0]["cache_write_tok"] == 7
    assert out[0]["billed_usd"] == 1.5


def test_legacy_null_columns_count_as_zero_not_crash() -> None:
    """Documents predating cache_write_tok store JSON null for it. `null + int`
    raises — one 2-month-old row would otherwise 500 the dashboard."""
    store, _ = _store(
        [
            {"route": "gpt-4o", "prompt_tok": 13, "cached_tok": None,
             "cache_write_tok": None, "completion_tok": None,
             "cost_usd": None, "billed_usd": None},
        ]
    )
    out = store.cost_breakdown(["vk_a"], group_by="model")
    assert out[0]["cache_write_tok"] == 0
    assert out[0]["cost_usd"] == 0.0
    assert out[0]["prompt_tok"] == 13


def test_null_dimension_becomes_unknown() -> None:
    """end_user is null unless the client sent one — normal, not an error, and it
    must not collapse into a blank row label."""
    store, _ = _store([{"end_user": None, "cost_usd": 0.5}])
    out = store.cost_breakdown(["vk_a"], group_by="end_user")
    assert out[0]["end_user"] == "unknown"
    assert out[0]["prompt_tok"] == 0


def test_totals_cover_rows_a_truncated_group_list_would_drop() -> None:
    store, _ = _store(
        [
            {"prompt_tok": 3, "cost_usd": 9.5, "billed_usd": 11.0},
            {"prompt_tok": 1, "cost_usd": 0.5, "billed_usd": 0.5},
        ]
    )
    totals = store.cost_totals(["vk_a"])
    assert totals["calls"] == 2
    assert totals["cost_usd"] == 10.0
    assert totals["billed_usd"] == 11.5
    assert totals["cache_write_tok"] == 0


def test_totals_with_no_rows_returns_zeros() -> None:
    store, _ = _store([])
    assert store.cost_totals(["vk_a"]) == _EMPTY_TOTALS


# --------------------------------------------------------------------------- #
# Trend                                                                        #
# --------------------------------------------------------------------------- #
def test_trend_zero_fills_quiet_hours() -> None:
    """Without zero-fill a quiet hour vanishes and the chart draws a straight
    line across the gap, implying traffic that never happened."""
    store, _ = _store([])
    out = store.cost_trend(["vk_a"], hours=6)
    assert len(out) == 6
    assert all(p["tokens"] == 0 and p["calls"] == 0 and p["cost_usd"] == 0.0 for p in out)
    # Oldest first, so the chart reads left-to-right in time.
    assert [p["ts"] for p in out] == sorted(p["ts"] for p in out)


def test_trend_buckets_by_utc_hour_and_sums_every_token_type() -> None:
    from datetime import UTC, datetime

    hour = datetime.now(UTC).strftime("%Y-%m-%dT%H")
    store, _ = _store(
        [
            {"ts": f"{hour}:05:00+00:00", "prompt_tok": 10, "cached_tok": 2,
             "cache_write_tok": 3, "completion_tok": 5, "cost_usd": 0.25},
            {"ts": f"{hour}:47:00+00:00", "prompt_tok": 0, "cached_tok": 0,
             "cache_write_tok": 0, "completion_tok": 0, "cost_usd": 0.25},
        ]
    )
    out = store.cost_trend(["vk_a"], hours=3)
    filled = [p for p in out if p["calls"]]
    # Both rows fall in the same UTC hour despite different minutes.
    assert len(filled) == 1
    assert filled[0]["calls"] == 2
    # 10 + 2 + 3 + 5 — cache_write included, or cache-heavy traffic reads as a dip.
    assert filled[0]["tokens"] == 20
    assert filled[0]["cost_usd"] == 0.5


def test_trend_ignores_rows_with_unusable_timestamps() -> None:
    """A malformed ts is a hub bug, not a reason to fail the whole chart."""
    store, _ = _store([{"ts": None, "prompt_tok": 5, "cost_usd": 1.0}])
    out = store.cost_trend(["vk_a"], hours=3)
    assert all(p["calls"] == 0 for p in out)
