"""Raw-body audit archival — the parts that can be tested without Azure.

Three separate surfaces, and they fail in different ways, so they are covered
separately:

* `hub.audit` decides WHAT gets written and WHERE. A bug here writes one
  tenant's prompts under another tenant's prefix, or lets an unbounded body
  through.
* `_merge_audit_flag` / `_merge_key_limit` decide WHO is archived. These two
  write the same named value from different operations, so the real hazard is
  one silently clobbering the other's field — a limits update turning archival
  off (losing an audit trail) or on (archiving without consent).
* `event_to_document` decides what the billing store learns about it: a
  pointer, never the content.

All hermetic — no Azure, no network, no blob client.
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.apim_provisioner import (
    _AUDIT_FLAG_EXPR,
    _KEY_LIMITS_MAX_BYTES,
    _merge_audit_flag,
    _merge_key_limit,
)
from app.services.usage_capture_import import event_to_document

# The hub ships vendored (it deploys as its own container), so it isn't on the
# path as an installed package. hub.audit itself imports only stdlib + hub.config,
# so it loads even where the hub's Azure deps are absent.
_HUB_ROOT = Path(__file__).resolve().parent.parent / "vendored" / "gitmodel-hub"
if str(_HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(_HUB_ROOT))

from hub import audit  # noqa: E402

TS = datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)


# --- blob_path: tenant isolation is a path property ---------------------------


def test_blob_path_shape():
    """Date first (retention/export are time-scoped), tenant second (one
    customer's archive is a single prefix)."""
    assert (
        audit.blob_path(TS, "vk_abc123", "req-1")
        == "2026/03/07/vk_abc123/req-1.json.gz"
    )


def test_blob_path_sanitizes_traversal():
    """The subscription arrives as an HTTP header. A forged one must not be able
    to write into another tenant's prefix — that prefix IS the isolation
    boundary a scoped SAS or a per-tenant deletion keys on. Stripping `/` is
    what makes escaping impossible; stripping `.` too keeps a segment from ever
    reading as `.` or `..`, which Azure documents as a name to avoid."""
    path = audit.blob_path(TS, "../../vk_victim", "req-1")
    assert ".." not in path
    assert path == "2026/03/07/______vk_victim/req-1.json.gz"
    # Exactly four segments: date x3, tenant, blob. A forged value adds none.
    assert len(path.split("/")) == 5


def test_blob_path_sanitizes_request_id():
    path = audit.blob_path(TS, "vk_a", "a/b?c")
    assert path == "2026/03/07/vk_a/a_b_c.json.gz"


def test_blob_path_missing_subscription():
    assert audit.blob_path(TS, None, "req-1").startswith("2026/03/07/unknown/")
    assert audit.blob_path(TS, "   ", "req-1").startswith("2026/03/07/unknown/")


def test_blob_path_is_deterministic():
    """submit() returns the pointer BEFORE the upload runs, which only works
    because the path depends on nothing the upload produces."""
    a = audit.blob_path(TS, "vk_a", "req-1")
    b = audit.blob_path(TS, "vk_a", "req-1")
    assert a == b


# --- wants_audit: the consent gate -------------------------------------------


def test_wants_audit_reads_apim_header():
    assert audit.wants_audit({"x-tf-audit": "1"}) is True


def test_wants_audit_defaults_off():
    """No header = every direct, non-APIM caller. Archive nothing."""
    assert audit.wants_audit({}) is False
    assert audit.wants_audit({"x-tf-audit": "0"}) is False
    assert audit.wants_audit({"x-tf-audit": "true"}) is False


def test_wants_audit_survives_a_broken_headers_object():
    class Hostile:
        def get(self, _name):
            raise RuntimeError("boom")

    assert audit.wants_audit(Hostile()) is False


# --- build_payload: size is the thing that bites -----------------------------


def _decode(blob: bytes) -> dict:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def _payload(request_body, response_body, max_bytes=4 * 1024 * 1024) -> dict:
    return _decode(
        audit.build_payload(
            request_id="req-1",
            ts=TS,
            subscription="vk_a",
            api_id="llm-anthropic",
            end_user="alice",
            model="claude-haiku-4-5",
            endpoint="/v1/messages",
            streamed=False,
            status=200,
            request_body=request_body,
            response_body=response_body,
            max_bytes=max_bytes,
        )
    )


def test_payload_keeps_both_bodies_verbatim():
    doc = _payload({"messages": [{"role": "user", "content": "hi"}]}, {"id": "msg_1"})
    assert doc["request"] == {"messages": [{"role": "user", "content": "hi"}]}
    assert doc["response"] == {"id": "msg_1"}
    assert doc["truncated"] is False
    assert doc["subscription"] == "vk_a"
    assert doc["end_user"] == "alice"
    assert doc["status"] == 200


def test_payload_is_gzipped():
    """Not optional: an SSE transcript repeats its envelope per chunk, and this
    store is charged by the byte for the whole retention window."""
    blob = audit.build_payload(
        request_id="req-1", ts=TS, subscription="vk_a", api_id=None, end_user=None,
        model="m", endpoint="/v1/messages", streamed=True, status=200,
        request_body={"p": "x" * 50_000}, response_body=None, max_bytes=4 * 1024 * 1024,
    )
    assert blob[:2] == b"\x1f\x8b"  # gzip magic
    assert len(blob) < 5_000  # ~10x, as the docstring claims


def test_payload_truncates_over_cap_and_says_so():
    doc = _payload("q" * 20_000, "r" * 20_000, max_bytes=8_000)
    assert doc["truncated"] is True
    assert "[truncated" in doc["request"]
    assert "[truncated" in doc["response"]


def test_payload_envelope_survives_truncation():
    """A clipped body still proves who called what and when — that is most of
    the audit value, and why truncation beats rejection."""
    doc = _payload("q" * 100_000, "r" * 100_000, max_bytes=4_096)
    assert doc["request_id"] == "req-1"
    assert doc["subscription"] == "vk_a"
    assert doc["endpoint"] == "/v1/messages"
    assert doc["ts"] == TS.isoformat()


def test_payload_huge_prompt_cannot_squeeze_out_the_response():
    """Budget is split, so a 1 MB prompt next to a 20-char answer must not leave
    the answer clipped to nothing."""
    doc = _payload("q" * 1_000_000, "the answer is 42", max_bytes=8_000)
    assert doc["response"] == "the answer is 42"


def test_payload_serializes_non_json_values():
    """Bodies come from upstream and from multipart forms; a stray datetime must
    not raise inside the request path. `default=str` renders it — not as
    isoformat, which is fine: this field is the archived body, reproduced as
    faithfully as JSON allows, not a parsed timestamp anyone computes on."""
    doc = _payload({"when": TS}, {"ok": True})
    assert doc["request"]["when"] == str(TS)
    assert doc["response"] == {"ok": True}


# --- _merge_audit_flag: who gets archived ------------------------------------


def test_audit_flag_set_on_empty_map():
    assert _merge_audit_flag({}, ["vk_1"], True) == {"vk_1": {"a": 1}}


def test_audit_flag_bulk_covers_every_key_of_the_tenant():
    """The toggle is per TENANT while the map is per subscription — flipping one
    tenant must touch all of its keys in a single read-merge-write."""
    out = _merge_audit_flag({}, ["vk_1", "vk_2", "vk_3"], True)
    assert out == {"vk_1": {"a": 1}, "vk_2": {"a": 1}, "vk_3": {"a": 1}}


def test_audit_flag_preserves_limits():
    out = _merge_audit_flag({"vk_1": {"t": 5000, "qt": "small", "p": "Daily"}}, ["vk_1"], True)
    assert out == {"vk_1": {"t": 5000, "qt": "small", "p": "Daily", "a": 1}}


def test_audit_flag_off_preserves_limits():
    out = _merge_audit_flag({"vk_1": {"t": 5000, "a": 1}}, ["vk_1"], False)
    assert out == {"vk_1": {"t": 5000}}


def test_audit_flag_off_drops_an_otherwise_empty_entry():
    """Otherwise switching auditing off leaves a growing tail of `{}` entries in
    a size-capped named value."""
    assert _merge_audit_flag({"vk_1": {"a": 1}}, ["vk_1"], False) == {}


def test_audit_flag_off_is_idempotent():
    assert _merge_audit_flag({}, ["vk_1"], False) == {}


def test_audit_flag_leaves_other_tenants_alone():
    out = _merge_audit_flag({"vk_other": {"t": 9}}, ["vk_1"], True)
    assert out["vk_other"] == {"t": 9}


def test_audit_flag_does_not_mutate_input():
    start = {"vk_1": {"t": 1}}
    _merge_audit_flag(start, ["vk_1"], True)
    assert start == {"vk_1": {"t": 1}}


def test_audit_flag_over_cap_raises():
    big = {f"vk_{i}": {"t": 1000000, "qt": "medium", "p": "Monthly"} for i in range(200)}
    assert len(json.dumps(big, separators=(",", ":"))) > _KEY_LIMITS_MAX_BYTES
    with pytest.raises(ValueError, match="exceed"):
        _merge_audit_flag(big, ["vk_new"], True)


# --- the clobber hazard: two operations, one named value ---------------------


def test_limits_update_preserves_the_audit_flag():
    """A limits change must not silently switch a tenant's archival off — that
    is a hole in exactly the record an audit exists to provide."""
    out = _merge_key_limit({"vk_1": {"t": 100, "a": 1}}, "vk_1", 200, None, None)
    assert out == {"vk_1": {"t": 200, "a": 1}}


def test_limits_update_does_not_invent_an_audit_flag():
    """And the other direction is worse: archiving a tenant that never consented."""
    out = _merge_key_limit({"vk_1": {"t": 100}}, "vk_1", 200, None, None)
    assert "a" not in out["vk_1"]


def test_clearing_all_limits_still_drops_an_audited_key():
    """`a` alone keeps the entry alive; without it the key would vanish and stop
    being archived as a side effect of clearing a rate limit."""
    out = _merge_key_limit({"vk_1": {"t": 100, "a": 1}}, "vk_1", None, None, None)
    assert out == {"vk_1": {"a": 1}}


def test_round_trip_limits_then_audit_then_limits():
    m = _merge_key_limit({}, "vk_1", 5000, "small", "Daily")
    m = _merge_audit_flag(m, ["vk_1"], True)
    m = _merge_key_limit(m, "vk_1", 9000, "small", "Daily")
    assert m == {"vk_1": {"t": 9000, "qt": "small", "p": "Daily", "a": 1}}


# --- the policy expression fails closed --------------------------------------


def test_audit_flag_expr_defaults_to_zero_on_any_failure():
    """A broken or missing map must archive NOTHING. The alternative is writing
    customer source code to disk for a tenant that never consented."""
    assert 'catch { return "0"; }' in _AUDIT_FLAG_EXPR


def test_audit_flag_expr_reads_the_shared_limits_variable():
    """Reuses the token-limits named value (captured into tfRaw), so toggling a
    tenant needs no new named value and no hub redeploy."""
    assert 'context.Variables["tfRaw"]' in _AUDIT_FLAG_EXPR
    assert 'e["a"]' in _AUDIT_FLAG_EXPR


# --- Cosmos document: a pointer, never the content ---------------------------


def _event(**over) -> dict:
    base = {
        "request_id": "req-1",
        "ts": TS.isoformat(),
        "subscription": "vk_a",
        "model": "claude-haiku-4-5",
        "status": 200,
    }
    base.update(over)
    return base


def test_document_carries_the_audit_pointer():
    doc = event_to_document(_event(audit_blob="2026/03/07/vk_a/req-1.json.gz"), {})
    assert doc["audit_blob"] == "2026/03/07/vk_a/req-1.json.gz"


def test_document_audit_pointer_is_none_when_not_archived():
    """The normal case: archival is off unless an operator switched the tenant on."""
    assert event_to_document(_event(), {})["audit_blob"] is None
    assert event_to_document(_event(audit_blob=""), {})["audit_blob"] is None


def test_document_never_carries_the_bodies():
    """Usage documents are readable by anything with Cosmos access. The raw
    bodies must stay behind a blob role that nothing in this system holds."""
    doc = event_to_document(
        _event(audit_blob="2026/03/07/vk_a/req-1.json.gz"), {}
    )
    serialized = json.dumps(doc)
    assert "request" not in doc
    assert "response" not in doc
    assert "messages" not in serialized
