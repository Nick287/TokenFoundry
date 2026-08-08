"""Import-side billing: upstream `copilot_usage` -> a Cosmos document.

Pure logic, no Azure. Two things are being locked down:

1. **The money.** `total_nano_aiu / 1e11` is now the authoritative cost, so the
   conversion factor and the treatment of a genuine zero are load-bearing.
2. **The document shape.** The read path (app/api/usage.py) and the historical
   rows written by the retired APIM policy both expect a specific flat shape and
   partition key. Drift here doesn't fail — it just makes usage silently vanish
   from the portal.
"""

from __future__ import annotations

from app.services.billing import cost_from_copilot_usage, tokens_from_copilot_usage
from app.services.usage_capture_import import event_to_document

# claude-haiku-4-5, verbatim from upstream. 8 x 1e5 + 16 x 5e5 = 8,800,000.
HAIKU_COPILOT_USAGE = {
    "token_details": [
        {"token_type": "input", "token_count": 8,
         "cost_per_batch": 100000000000, "batch_size": 1000000},
        {"token_type": "cache_read", "token_count": 3,
         "cost_per_batch": 10000000000, "batch_size": 1000000},
        {"token_type": "cache_write", "token_count": 5,
         "cost_per_batch": 125000000000, "batch_size": 1000000},
        {"token_type": "output", "token_count": 16,
         "cost_per_batch": 500000000000, "batch_size": 1000000},
    ],
    "total_nano_aiu": 8800000,
}

# gpt-4o-mini: upstream prices every token type at zero.
FREE_COPILOT_USAGE = {
    "token_details": [
        {"token_type": "input", "token_count": 8,
         "cost_per_batch": 0, "batch_size": 1000000},
        {"token_type": "output", "token_count": 16,
         "cost_per_batch": 0, "batch_size": 1000000},
    ],
    "total_nano_aiu": 0,
}


def _event(**overrides) -> dict:
    base = {
        "request_id": "apim-req-1",
        "ts": "2026-08-04T10:30:00+00:00",
        "subscription": "vk_alice",
        "api_id": "llm-anthropic",
        "model": "claude-haiku-4.5",
        "served_model": "claude-haiku-4.5",
        "endpoint": "/v1/messages",
        "streamed": True,
        "status": 200,
        "input_tokens": 8,
        "output_tokens": 16,
        "total_tokens": 24,
        "cached_tokens": 3,
        "estimated": False,
        "usage": {"input_tokens": 8, "output_tokens": 16},
        "copilot_usage": HAIKU_COPILOT_USAGE,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #
def test_nano_aiu_converts_to_usd():
    # 1 AIU = $0.01 and the field is nano-AIU, so 8.8e6 nano-AIU = $0.000088.
    bd = cost_from_copilot_usage(HAIKU_COPILOT_USAGE)
    assert bd is not None
    assert bd.cost_usd == 0.000088
    assert bd.billed_usd == 0.000088  # no markup


def test_unit_prices_match_anthropic_list_price():
    """Sanity check on upstream's own numbers, not on our code.

    If this ever fails, upstream repriced — which is exactly the event this
    design is meant to absorb without a code change. It is here so the repricing
    is *noticed*, not so it breaks anything.
    """
    by_type = {d["token_type"]: d for d in HAIKU_COPILOT_USAGE["token_details"]}
    # cost_per_batch is nano-AIU per batch_size tokens; /1e11 -> USD per 1M.
    usd_per_1m = {
        t: d["cost_per_batch"] / 1e11 * (1_000_000 / d["batch_size"])
        for t, d in by_type.items()
    }
    assert round(usd_per_1m["input"], 6) == 1.0
    assert round(usd_per_1m["output"], 6) == 5.0


def test_markup_applies_on_top_of_upstream_cost():
    bd = cost_from_copilot_usage(HAIKU_COPILOT_USAGE, markup_pct=0.20)
    assert bd is not None
    assert bd.cost_usd == 0.000088
    assert bd.billed_usd == round(0.000088 * 1.2, 11)


def test_zero_is_a_real_price_not_a_missing_one():
    """A free model must bill 0, not fall through to a guessed floor price."""
    bd = cost_from_copilot_usage(FREE_COPILOT_USAGE, markup_pct=0.50)
    assert bd is not None
    assert bd.cost_usd == 0.0
    assert bd.billed_usd == 0.0


def test_missing_or_malformed_returns_none_so_caller_can_fall_back():
    assert cost_from_copilot_usage(None) is None
    assert cost_from_copilot_usage({}) is None
    assert cost_from_copilot_usage({"total_nano_aiu": "8800000"}) is None
    assert cost_from_copilot_usage({"total_nano_aiu": True}) is None


def test_precision_survives_a_cheap_call():
    """Rounding to 6 places (our old precision) would zero this out."""
    bd = cost_from_copilot_usage({"total_nano_aiu": 100})
    assert bd is not None
    assert bd.cost_usd == 1e-09


# --------------------------------------------------------------------------- #
# Token split
# --------------------------------------------------------------------------- #
def test_token_details_split_cache_reads_out_of_input():
    t = tokens_from_copilot_usage(HAIKU_COPILOT_USAGE)
    assert t == {
        "prompt_tok": 8,
        "completion_tok": 16,
        "cached_tok": 3,
        # Upstream prices cache WRITES separately and higher than input
        # ($125 vs $100 per 1M). This used to be dropped on the floor, which made
        # the token columns understate cache-heavy traffic against its own bill.
        "cache_write_tok": 5,
    }


def test_token_details_tolerates_garbage():
    assert tokens_from_copilot_usage(None) == {
        "prompt_tok": 0, "completion_tok": 0, "cached_tok": 0, "cache_write_tok": 0,
    }
    assert tokens_from_copilot_usage({"token_details": "nope"})["prompt_tok"] == 0
    assert tokens_from_copilot_usage(
        {"token_details": [{"token_type": "input", "token_count": None}]}
    )["prompt_tok"] == 0


# --------------------------------------------------------------------------- #
# Document shape
# --------------------------------------------------------------------------- #
def test_document_keys_match_the_retired_apim_policy():
    """`id` + `pk` must not drift, or history fractures across the cutover."""
    doc = event_to_document(_event(), {})
    assert doc is not None
    assert doc["id"] == "apim-req-1"
    assert doc["pk"] == "vk_alice_202608"


def test_document_uses_the_flat_shape_the_read_path_expects():
    doc = event_to_document(_event(), {})
    assert doc is not None
    assert doc["subscription"] == "vk_alice"
    assert doc["prompt_tok"] == 8
    assert doc["completion_tok"] == 16
    assert doc["cached_tok"] == 3
    assert doc["cache_write_tok"] == 5
    assert doc["cost_usd"] == 0.000088


# --------------------------------------------------------------------------- #
# Timestamp normalization
# --------------------------------------------------------------------------- #
# The aggregation filters time with a STRING comparison (`c.ts >= @since`), which
# is only correct while every stored timestamp shares one offset. A record kept
# as "+08:00" would sort as though it happened 8 hours later than it did — no
# error, just calls landing in the wrong window and a bill that won't reconcile.
def test_offset_timestamps_are_converted_to_utc_not_merely_kept():
    doc = event_to_document(_event(ts="2026-08-04T18:30:00+08:00"), {})
    assert doc is not None
    # Same instant, expressed as UTC: 18:30+08:00 == 10:30Z.
    assert doc["ts"].startswith("2026-08-04T10:30:00")
    assert doc["ts"].endswith("+00:00")


def test_naive_timestamps_are_assumed_utc():
    doc = event_to_document(_event(ts="2026-08-04T10:30:00"), {})
    assert doc is not None
    assert doc["ts"] == "2026-08-04T10:30:00+00:00"


def test_partition_key_follows_the_utc_month():
    """An offset timestamp near a month boundary must be bucketed by its UTC
    month, or a call lands in a partition the billing period never reads."""
    doc = event_to_document(_event(ts="2026-09-01T02:00:00+08:00"), {})
    assert doc is not None
    # 02:00+08:00 on Sep 1 is 18:00Z on Aug 31 — an AUGUST record.
    assert doc["pk"] == "vk_alice_202608"


def test_zulu_timestamps_still_parse():
    doc = event_to_document(_event(ts="2026-08-04T10:30:00Z"), {})
    assert doc is not None
    assert doc["ts"] == "2026-08-04T10:30:00+00:00"
    assert doc["cost_source"] == "copilot_usage"
    # Kept verbatim so a disputed invoice is recomputable from the document.
    assert doc["copilot_usage"] == HAIKU_COPILOT_USAGE


def test_markup_is_looked_up_by_route_name():
    doc = event_to_document(_event(), {"claude-haiku-4.5": 0.20})
    assert doc is not None
    assert doc["billed_usd"] == round(0.000088 * 1.2, 11)
    # An unknown route bills cost through rather than guessing a markup.
    doc2 = event_to_document(_event(model="some-new-model"), {"claude-haiku-4.5": 0.2})
    assert doc2 is not None
    assert doc2["billed_usd"] == doc2["cost_usd"]


def test_unpriced_call_is_flagged_rather_than_estimated():
    """No copilot_usage (e.g. the Azure OpenAI image path) -> 0, visibly so."""
    doc = event_to_document(_event(copilot_usage=None), {})
    assert doc is not None
    assert doc["cost_usd"] == 0.0
    assert doc["cost_source"] == "unpriced"
    # It still falls back to the hub's own token counts.
    assert doc["prompt_tok"] == 8
    assert doc["completion_tok"] == 16


def test_event_without_request_id_is_dropped():
    """Without the document key there is no dedup, so it must not be written."""
    assert event_to_document(_event(request_id=None), {}) is None


def test_missing_subscription_does_not_lose_the_record():
    doc = event_to_document(_event(subscription=None), {})
    assert doc is not None
    assert doc["subscription"] == "unknown"
    assert doc["pk"] == "unknown_202608"


def test_end_user_and_streaming_flags_survive_into_cosmos():
    doc = event_to_document(_event(end_user="u-42", streamed=True), {})
    assert doc is not None
    assert doc["end_user"] == "u-42"
    assert doc["streamed"] is True
    # Absent end_user is normal, not an error.
    assert event_to_document(_event(), {})["end_user"] is None  # type: ignore[index]


def test_partition_key_follows_the_event_month_not_import_time():
    """Re-importing an old blob must land in the month it happened."""
    doc = event_to_document(_event(ts="2026-01-31T23:59:59Z"), {})
    assert doc is not None
    assert doc["pk"] == "vk_alice_202601"


def test_hub_id_identifies_which_upstream_account_served_the_call():
    """Every hub publishes to ONE Event Hub, so the record has to say which.

    Not a billing input — cost is charged to `subscription` either way. It is
    what lets a month be split by GitHub account and reconciled against the
    bill GitHub sends for that account.
    """
    doc = event_to_document(_event(hub_id="gha_p2test01"), {})
    assert doc is not None
    assert doc["hub_id"] == "gha_p2test01"
    # Billing must not depend on it: same subscription, same partition.
    assert doc["pk"] == "vk_alice_202608"


def test_hub_id_absent_is_null_not_a_guess():
    """Events from before this field existed, or from a standalone hub.

    A stamped-but-empty TF_HUB_ID must read the same as never stamped, so the
    query side has one absent case to handle rather than two.
    """
    assert event_to_document(_event(), {})["hub_id"] is None  # type: ignore[index]
    assert event_to_document(_event(hub_id=""), {})["hub_id"] is None  # type: ignore[index]


# --------------------------------------------------------------------------- #
# The read projection: status has to survive the trip to the portal            #
# --------------------------------------------------------------------------- #
def test_record_view_exposes_the_gateway_status() -> None:
    """Cosmos has stored `status` on every document since the importer was
    written, but the call log dropped it — so a 429 (zero tokens, zero cost)
    rendered identically to a served call that happened to be free.
    """
    from app.api.usage import _to_record_view

    assert _to_record_view({"status": 429})["status"] == 429
    assert _to_record_view({"status": 200})["status"] == 200


def test_record_view_reports_a_missing_status_as_null_not_zero() -> None:
    """Documents predating the field have no status. 0 would render as a real
    status code in the log; null renders as a dash."""
    from app.api.usage import _to_record_view

    assert _to_record_view({})["status"] is None
