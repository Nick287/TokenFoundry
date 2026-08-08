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
    "ok_calls": 0,
    "failed_calls": 0,
    "failed_by_status": {},
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


# --------------------------------------------------------------------------- #
# Succeeded vs failed — a $0 call is not necessarily a served call             #
# --------------------------------------------------------------------------- #
def test_status_splits_calls_into_succeeded_and_failed() -> None:
    """The dev-16 campaign produced 46 upstream 429s. They cost nothing, so the
    money column was right while `calls` reported them as ordinary traffic."""
    store, _ = _store(
        [
            {"route": "gpt-4o-mini", "status": 200, "prompt_tok": 10, "completion_tok": 5},
            {"route": "gpt-4o-mini", "status": 429, "prompt_tok": 0, "completion_tok": 0},
            {"route": "gpt-4o-mini", "status": 500, "prompt_tok": 0, "completion_tok": 0},
        ]
    )
    out = store.cost_breakdown(["vk_a"], group_by="model")
    assert out[0]["calls"] == 3
    assert out[0]["ok_calls"] == 1
    assert out[0]["failed_calls"] == 2


def test_status_is_projected_so_the_split_has_something_to_read() -> None:
    """The split is silent if the column never leaves Cosmos — every row would
    carry `status: None` and count as OK, which looks exactly like success."""
    store, fake = _store([{"route": "gpt-4o-mini", "status": 429}])
    store.cost_breakdown(["vk_a"], group_by="model")
    assert "c.status" in fake.last_query


def test_missing_status_counts_as_succeeded_not_failed() -> None:
    """Documents predating the field carry no status. Counting them as failures
    would retroactively invent errors across the whole history."""
    store, _ = _store([{"route": "gpt-4o", "prompt_tok": 13}])
    out = store.cost_breakdown(["vk_a"], group_by="model")
    assert (out[0]["ok_calls"], out[0]["failed_calls"]) == (1, 0)


def test_totals_carry_the_same_split_as_the_groups() -> None:
    """Totals are queried independently of the group list, so the split has to be
    computed twice — a headline that disagreed with its own table is worse than
    no headline."""
    store, _ = _store(
        [
            {"status": 200, "cost_usd": 1.0},
            {"status": 200, "cost_usd": 1.0},
            {"status": 400, "cost_usd": 0.0},
        ]
    )
    totals = store.cost_totals(["vk_a"])
    assert (totals["calls"], totals["ok_calls"], totals["failed_calls"]) == (3, 2, 1)


def test_empty_totals_expose_the_split_keys() -> None:
    """A quiet window must still return the keys the portal indexes, or the
    dashboard renders `undefined` instead of a zero."""
    store, _ = _store([])
    totals = store.cost_totals(["vk_a"])
    assert totals["ok_calls"] == 0
    assert totals["failed_calls"] == 0
    assert totals["failed_by_status"] == {}


def test_failures_are_broken_out_per_status_code() -> None:
    """A bare failure total can't be reconciled against the gateway: only some
    failure kinds have a counterpart there. The per-code split is what makes the
    two sources comparable."""
    store, _ = _store(
        [
            {"route": "gpt-4o-mini", "status": 200},
            {"route": "gpt-4o-mini", "status": 429},
            {"route": "gpt-4o-mini", "status": 429},
            {"route": "gpt-4o-mini", "status": 400},
        ]
    )
    out = store.cost_breakdown(["vk_a"], group_by="model")
    assert out[0]["failed_by_status"] == {"429": 2, "400": 1}
    # The per-code counts must sum to the headline, or the card contradicts the
    # chips printed directly beneath it.
    assert sum(out[0]["failed_by_status"].values()) == out[0]["failed_calls"]


def test_each_group_gets_its_own_status_dict() -> None:
    """A dict built once and reused would merge every group's errors — every row
    would show every other row's failures."""
    store, _ = _store(
        [
            {"route": "gpt-4o-mini", "status": 429},
            {"route": "claude-opus-5", "status": 400},
        ]
    )
    out = {
        g["model"]: g["failed_by_status"]
        for g in store.cost_breakdown(["vk_a"], group_by="model")
    }
    assert out["gpt-4o-mini"] == {"429": 1}
    assert out["claude-opus-5"] == {"400": 1}


def test_successful_calls_never_enter_the_status_breakdown() -> None:
    """200 is not an error. Listing it as a chip under "failed" would be worse
    than showing nothing."""
    store, _ = _store([{"route": "gpt-4o", "status": 200}, {"route": "gpt-4o"}])
    assert store.cost_breakdown(["vk_a"], group_by="model")[0]["failed_by_status"] == {}


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


# --------------------------------------------------------------------------- #
# Time window                                                                  #
# --------------------------------------------------------------------------- #
# The window is the reason this section exists. The call log had NO time filter
# while the breakdown under it was pinned to 24h, so on a tenant whose last
# traffic was 39 hours old the page rendered a full 96-row call log directly
# above "no usage recorded in this window". Both were telling the truth; the
# page was not. These tests pin the two halves to the same filter.
def test_call_log_applies_the_window_when_given() -> None:
    store, fake = _store([])
    store.query_by_subscriptions(["vk_a"], since_iso="2026-08-04T00:00:00+00:00")
    assert "c.ts >= @since" in fake.last_query
    assert fake.last_params["@since"] == "2026-08-04T00:00:00+00:00"


def test_call_log_without_a_window_is_unfiltered_by_time() -> None:
    """`since_iso=None` means all time, and that is a real option — the log is
    also read as a ledger, not only as a dashboard slice."""
    store, fake = _store([])
    store.query_by_subscriptions(["vk_a"])
    # Check the WHERE clause specifically: `c.ts` also appears in the ORDER BY,
    # which is present either way.
    where = fake.last_query.split("WHERE", 1)[1].split("ORDER BY")[0]
    assert "c.ts" not in where
    assert "@since" not in fake.last_params


def test_count_applies_the_same_window_as_the_page_query() -> None:
    """If the count ignores the window it reports more pages than exist, and the
    pager hands the user pages that come back empty."""
    since = "2026-08-04T00:00:00+00:00"
    store, fake = _store([7])
    store.query_by_subscriptions(["vk_a"], since_iso=since)
    page_query = fake.last_query
    store.count_by_subscriptions(["vk_a"], since_iso=since)
    count_query = fake.last_query

    def where_of(q: str) -> str:
        return q.split("WHERE", 1)[1].split("ORDER BY")[0].strip()

    assert where_of(page_query) == where_of(count_query)
    assert fake.last_params["@since"] == since


def test_count_still_uses_the_only_cross_partition_aggregate_cosmos_accepts() -> None:
    """Adding the window must not turn this into `SELECT COUNT(1) AS n`, which
    Cosmos rejects cross-partition (see the module docstring)."""
    store, fake = _store([3])
    store.count_by_subscriptions(["vk_a"], since_iso="2026-08-04T00:00:00+00:00")
    assert fake.last_query.startswith("SELECT VALUE COUNT(1) FROM c WHERE ")


# --------------------------------------------------------------------------- #
# Trend granularity                                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("hours", "expected"),
    [(1, "hour"), (24, "hour"), (48, "hour"), (49, "day"), (168, "day"), (720, "day")],
)
def test_trend_bucket_switches_to_days_past_two_days(hours: int, expected: str) -> None:
    assert UsageStore.trend_bucket(hours) == expected


def test_long_window_returns_daily_buckets_not_hundreds_of_hourly_ones() -> None:
    """30 days of hourly points is 720 marks on a chart that has room for ~30."""
    store, _ = _store([])
    out = store.cost_trend(["vk_a"], hours=720)
    assert len(out) == 30


def test_daily_bucket_count_rounds_up_so_the_oldest_day_is_not_clipped() -> None:
    """A 70h window still spans 3 calendar days; flooring would drop one."""
    store, _ = _store([])
    assert len(store.cost_trend(["vk_a"], hours=70)) == 3


def test_daily_buckets_fold_rows_from_different_hours_of_the_same_day() -> None:
    from datetime import UTC, datetime

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    store, _ = _store(
        [
            {"ts": f"{day}T01:05:00+00:00", "prompt_tok": 4, "cost_usd": 0.1},
            {"ts": f"{day}T22:47:00+00:00", "prompt_tok": 6, "cost_usd": 0.2},
        ]
    )
    filled = [p for p in store.cost_trend(["vk_a"], hours=168) if p["calls"]]
    assert len(filled) == 1
    assert filled[0]["calls"] == 2
    assert filled[0]["tokens"] == 10
