"""Per-key token-limit tests — pure functions + schema pairing validation.

Hermetic like test_apim_policy.py: the merge/remove helpers are module-level
pure functions (no Azure), and the schema validator is plain pydantic. No
TestClient, no DB, no Azure client.
"""
import json

import pytest

from app.models.enums import TokenQuotaPeriod, TokenQuotaTier
from app.models.schemas import VirtualKeyCreate
from app.services.apim_provisioner import (
    _KEY_LIMITS_MAX_BYTES,
    _merge_key_limit,
    _remove_key_limit,
)

# --- _merge_key_limit: entry shape -------------------------------------------


def test_merge_tpm_only():
    out = _merge_key_limit({}, "vk_1", 5000, None, None)
    assert out == {"vk_1": {"t": 5000}}


def test_merge_quota_tier_and_period():
    out = _merge_key_limit({}, "vk_1", None, "small", "Daily")
    assert out == {"vk_1": {"qt": "small", "p": "Daily"}}


def test_merge_all_three():
    out = _merge_key_limit({}, "vk_1", 5000, "large", "Monthly")
    assert out == {"vk_1": {"t": 5000, "qt": "large", "p": "Monthly"}}


def test_merge_none_tier_is_omitted():
    """A 'none' quota tier means no quota gate -> not stored."""
    out = _merge_key_limit({}, "vk_1", 5000, "none", "Daily")
    assert out == {"vk_1": {"t": 5000, "p": "Daily"}}
    assert "qt" not in out["vk_1"]


def test_merge_all_empty_removes_entry():
    """No limits at all -> the key is removed from the map, not left as {}."""
    start = {"vk_1": {"t": 5000}}
    out = _merge_key_limit(start, "vk_1", None, None, None)
    assert "vk_1" not in out


def test_merge_does_not_mutate_input():
    start = {"vk_a": {"t": 1}}
    _merge_key_limit(start, "vk_b", 2, None, None)
    assert start == {"vk_a": {"t": 1}}  # unchanged


def test_merge_overwrites_existing_key():
    start = {"vk_1": {"t": 100}}
    out = _merge_key_limit(start, "vk_1", 200, None, None)
    assert out == {"vk_1": {"t": 200}}


def test_merge_over_cap_raises():
    """Past the named value size cap, merge raises so issuance fails loudly
    instead of silently dropping a limit."""
    # Build a map already near the cap, then add one more.
    big = {f"vk_{i}": {"t": 1000000, "qt": "medium", "p": "Monthly"} for i in range(200)}
    assert len(json.dumps(big, separators=(",", ":"))) > _KEY_LIMITS_MAX_BYTES
    with pytest.raises(ValueError, match="exceed"):
        _merge_key_limit(big, "vk_new", 5000, None, None)


# --- _remove_key_limit -------------------------------------------------------


def test_remove_existing():
    out = _remove_key_limit({"vk_1": {"t": 1}, "vk_2": {"t": 2}}, "vk_1")
    assert out == {"vk_2": {"t": 2}}


def test_remove_absent_is_noop():
    out = _remove_key_limit({"vk_2": {"t": 2}}, "vk_1")
    assert out == {"vk_2": {"t": 2}}


def test_remove_does_not_mutate_input():
    start = {"vk_1": {"t": 1}}
    _remove_key_limit(start, "vk_1")
    assert start == {"vk_1": {"t": 1}}


# --- schema pairing validation -----------------------------------------------


def test_schema_tpm_only_ok():
    k = VirtualKeyCreate(project_id="pj_1", tokens_per_minute=5000)
    assert k.tokens_per_minute == 5000


def test_schema_quota_tier_with_period_ok():
    k = VirtualKeyCreate(
        project_id="pj_1",
        token_quota_tier=TokenQuotaTier.SMALL,
        token_quota_period=TokenQuotaPeriod.DAILY,
    )
    assert k.token_quota_tier == TokenQuotaTier.SMALL


def test_schema_all_empty_ok():
    k = VirtualKeyCreate(project_id="pj_1")
    assert k.tokens_per_minute is None


def test_schema_tier_without_period_rejected():
    with pytest.raises(ValueError, match="together"):
        VirtualKeyCreate(project_id="pj_1", token_quota_tier=TokenQuotaTier.SMALL)


def test_schema_period_without_tier_rejected():
    with pytest.raises(ValueError, match="together"):
        VirtualKeyCreate(project_id="pj_1", token_quota_period=TokenQuotaPeriod.DAILY)


def test_schema_none_tier_with_period_rejected():
    """NONE tier counts as 'no quota', so a lone period is still one-sided."""
    with pytest.raises(ValueError, match="together"):
        VirtualKeyCreate(
            project_id="pj_1",
            token_quota_tier=TokenQuotaTier.NONE,
            token_quota_period=TokenQuotaPeriod.DAILY,
        )


def test_schema_tpm_over_int32_rejected():
    """tokens-per-minute is an int32 (APIM) and an integer DB column; a value
    past the cap must be rejected, not silently overflow the DB."""
    with pytest.raises(ValueError):
        VirtualKeyCreate(project_id="pj_1", tokens_per_minute=50_000_000_000)


def test_schema_tpm_at_cap_ok():
    k = VirtualKeyCreate(project_id="pj_1", tokens_per_minute=100_000_000)
    assert k.tokens_per_minute == 100_000_000
