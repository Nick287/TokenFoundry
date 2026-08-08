"""The hub's usage pipeline must not lose events silently.

Written after a real incident: dev-15 lost 21 usage records during a load test
and nothing said so. The counter existed and `/api/status` returned it, but it
could not say WHICH of four paths had failed — and two of those paths were not
counted at all. Every test here pins one of the failure modes that made that
possible.

Style follows tests/test_copilot_usage.py: sys.path insert + importorskip,
module-level functions, explicit save/restore of the module globals (these
modules are global-state by design, so doing the restore by hand is clearer
than monkeypatch), and DELTA assertions rather than absolute counters, since
the counters are process-wide and other tests move them.

`azure-eventhub` is deliberately NOT a test dependency — the queue holds
serialized strings and `_make_event` is the single seam that needs the SDK, so
substituting it keeps the whole retry path testable in a bare venv.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

import pytest

_HUB_ROOT = Path(__file__).resolve().parent.parent / "vendored" / "gitmodel-hub"
if str(_HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(_HUB_ROOT))

eventhub = pytest.importorskip(
    "hub.eventhub", reason="vendored hub deps not installed in this environment"
)
server = pytest.importorskip(
    "hub.server", reason="vendored hub deps not installed in this environment"
)


@contextlib.contextmanager
def _swap(obj: Any, name: str, value: Any):
    """Save/restore a module global."""
    prev = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, prev)


@contextlib.contextmanager
def _clean_queue():
    """Run with an empty retry queue and put the old one back."""
    prev = list(eventhub._retry_q)
    eventhub._retry_q.clear()
    try:
        yield
    finally:
        eventhub._retry_q.clear()
        eventhub._retry_q.extend(prev)


class _FakeEvent:
    """Just enough of EventData for _body_text / _attempt_of."""

    def __init__(self, body: str, attempt: int = 0) -> None:
        self._body = body
        self.properties: dict[Any, Any] = {b"tf-attempt": attempt} if attempt else {}

    def body_as_str(self) -> str:
        return self._body


class _FakeProducer:
    def __init__(self, fail: int = 0) -> None:
        self.sent: list[Any] = []
        self._fail = fail

    async def send_event(self, ev: Any, timeout: float | None = None) -> None:
        if self._fail:
            self._fail -= 1
            raise RuntimeError("broker down")
        self.sent.append(ev)


def _fake_make_event(body: str, attempts: int) -> _FakeEvent:
    return _FakeEvent(body, attempts)


# --------------------------------------------------------------------------- #
# Recovering bodies: what can be retried, and what must be counted instead     #
# --------------------------------------------------------------------------- #
async def test_on_error_parks_recoverable_bodies_for_retry():
    """The bodies used to be discarded outright — `events` was counted and
    dropped on the floor, so a transient broker failure was unrecoverable."""
    with _clean_queue():
        await eventhub._on_error(
            [_FakeEvent('{"request_id":"r1"}')], "0", RuntimeError("x")
        )
        assert eventhub.retry_depth() == 1
        assert eventhub._retry_q[0].attempts == 1


async def test_on_error_counts_opaque_events_as_lost_rather_than_queueing_junk():
    """`str(ev)` would enqueue a repr and ship garbage to the billing store, so
    an unreadable event is a loss, not a retry."""
    with _clean_queue():
        d0, l0 = eventhub.dropped_count(), eventhub.lost_count()
        await eventhub._on_error([object(), object()], "0", RuntimeError("x"))
        assert eventhub.dropped_count() == d0 + 2
        assert eventhub.lost_count() == l0 + 2
        assert eventhub.retry_depth() == 0


def test_attempt_count_survives_the_amqp_round_trip():
    """AMQP hands property keys back as bytes; reading only `str` keys would
    reset every retry to attempt 0 and let an event loop forever."""
    assert eventhub._attempt_of(_FakeEvent("{}", 3)) == 3
    assert eventhub._attempt_of(_FakeEvent("{}")) == 0
    assert eventhub._attempt_of(object()) == 0


# --------------------------------------------------------------------------- #
# Backoff and bounds                                                           #
# --------------------------------------------------------------------------- #
def test_backoff_is_monotonic_and_capped():
    seq = [eventhub._backoff_seconds(n, 2.0, 60.0) for n in range(1, 8)]
    assert seq == [2, 4, 8, 16, 32, 60, 60]


def test_attempt_cap_gives_up_exactly_once():
    """Bounded retries are what stop a permanently-rejected event from looping
    forever; giving up must count the event once and only once."""
    with _clean_queue():
        cap = eventhub._cfg().eventhub_retry_max_attempts
        before = eventhub.lost_count()
        eventhub._requeue('{"request_id":"r"}', cap + 1)
        assert eventhub.retry_depth() == 0
        assert eventhub.lost_count() == before + 1


def test_full_queue_evicts_the_oldest_and_says_so():
    """Explicit eviction rather than deque(maxlen=): a silent maxlen drop is
    exactly the invisible loss this module exists to surface."""
    with _clean_queue(), _swap(eventhub, "_cfg", lambda: _cfg_with(max_queue=2)):
        before = eventhub.lost_count()
        for i in range(3):
            eventhub._requeue(f'{{"request_id":"r{i}"}}', 1)
        assert eventhub.retry_depth() == 2
        assert eventhub.lost_count() == before + 1
        assert eventhub._reasons[-1]["kind"] in {"overflow", "gave_up"}


def _cfg_with(**over: Any) -> Any:
    """A settings stand-in with the retry knobs overridden."""
    real = eventhub.get_settings()

    class _S:
        eventhub_retry_max_queue = over.get("max_queue", real.eventhub_retry_max_queue)
        eventhub_retry_max_attempts = over.get(
            "max_attempts", real.eventhub_retry_max_attempts
        )
        eventhub_retry_base_seconds = over.get("base", 0.0)
        eventhub_send_timeout_seconds = over.get("timeout", 1.0)
        eventhub_enabled = over.get("enabled", real.eventhub_enabled)
        hub_id = real.hub_id

    return _S()


# --------------------------------------------------------------------------- #
# Draining                                                                     #
# --------------------------------------------------------------------------- #
async def test_drain_resends_and_stamps_the_attempt():
    """The attempt count has to ride the event, or a re-failed retry restarts
    at 1 and never reaches the cap."""
    fake = _FakeProducer()
    with _clean_queue(), _swap(eventhub, "_producer", fake), _swap(
        eventhub, "_make_event", _fake_make_event
    ), _swap(eventhub, "_cfg", lambda: _cfg_with()):
        eventhub._requeue('{"request_id":"r1"}', 1)
        for item in eventhub._retry_q:
            item.next_at = 0.0
        await eventhub._drain_once()
        assert eventhub.retry_depth() == 0
        assert len(fake.sent) == 1
        assert eventhub._attempt_of(fake.sent[0]) == 1


async def test_drain_requeues_when_the_send_fails_again():
    fake = _FakeProducer(fail=1)
    with _clean_queue(), _swap(eventhub, "_producer", fake), _swap(
        eventhub, "_make_event", _fake_make_event
    ), _swap(eventhub, "_cfg", lambda: _cfg_with()):
        eventhub._requeue('{"request_id":"r1"}', 1)
        for item in eventhub._retry_q:
            item.next_at = 0.0
        await eventhub._drain_once()
        assert eventhub.retry_depth() == 1
        assert eventhub._retry_q[0].attempts == 2


async def test_items_not_yet_due_stay_queued():
    """Without this the drain would hot-loop, retrying instantly and burning
    the attempt budget during the very outage the backoff exists for."""
    fake = _FakeProducer()
    with _clean_queue(), _swap(eventhub, "_producer", fake), _swap(
        eventhub, "_make_event", _fake_make_event
    ), _swap(eventhub, "_cfg", lambda: _cfg_with(base=3600.0)):
        eventhub._requeue('{"request_id":"r1"}', 1)
        await eventhub._drain_once()
        assert eventhub.retry_depth() == 1
        assert fake.sent == []


async def test_unconfigured_hub_gives_up_queued_events_instead_of_hoarding():
    """Nothing will ever drain a queue with no Event Hub behind it; holding the
    events would report a healthy queue depth that can only grow."""
    with _clean_queue(), _swap(eventhub, "_producer", None), _swap(
        eventhub, "_disabled", True
    ):
        eventhub._retry_q.append(eventhub._Pending('{"request_id":"r"}', 1, 0.0))
        before = eventhub.lost_count()
        await eventhub._drain_once()
        assert eventhub.retry_depth() == 0
        assert eventhub.lost_count() == before + 1


# --------------------------------------------------------------------------- #
# unconfigured vs init-failed — the path that used to be entirely silent       #
# --------------------------------------------------------------------------- #
async def test_unconfigured_emit_is_silent():
    """The standalone case (no TF_EVENTHUB_FQDN). Not a failure: no counter,
    nothing logged, and `state` says so."""
    with _swap(eventhub, "_producer", None), _swap(
        eventhub, "_disabled", True
    ), _swap(eventhub, "_init_failed", False):
        before = eventhub.dropped_count()
        await eventhub.emit({"request_id": "r1"})
        assert eventhub.dropped_count() == before
        assert eventhub.stats()["state"] == "disabled"


async def test_init_failure_counts_every_dropped_event():
    """The bug: `_init_failed` latched and every later emit returned silently,
    so a billing feed that had stopped working produced no signal at all."""
    with _swap(eventhub, "_producer", None), _swap(
        eventhub, "_disabled", False
    ), _swap(eventhub, "_init_failed", True), _swap(
        eventhub, "_init_error", "ClientAuthenticationError"
    ):
        d0, l0 = eventhub.dropped_count(), eventhub.lost_count()
        await eventhub.emit({"request_id": "r1"})
        await eventhub.emit({"request_id": "r2"})
        assert eventhub.dropped_count() == d0 + 2
        assert eventhub.lost_count() == l0 + 2
        assert eventhub.stats()["state"] == "init_failed"


async def test_unserializable_record_is_its_own_failure_mode():
    """Serializing before the producer lookup keeps a poison record out of the
    retry queue, where it would fail identically on every attempt.

    A circular reference is used because `default=str` swallows almost
    everything else — an exotic type is stringified rather than raising, so
    this path is genuinely hard to reach. That is fine; the point is that when
    it IS reached the record is counted under its own reason instead of being
    parked forever.
    """
    circular: dict[str, Any] = {"request_id": "r"}
    circular["self"] = circular
    with _clean_queue():
        d0 = eventhub.dropped_count()
        await eventhub.emit(circular)
        assert eventhub.dropped_count() == d0 + 1
        assert eventhub.stats()["by_reason"]["serialize"] >= 1
        assert eventhub.retry_depth() == 0


async def test_enqueue_failure_parks_the_body():
    """We still hold the serialized body when send_event raises, so the event
    is recoverable rather than lost."""
    fake = _FakeProducer(fail=1)
    with _clean_queue(), _swap(eventhub, "_producer", fake), _swap(
        eventhub, "_make_event", _fake_make_event
    ), _swap(eventhub, "_cfg", lambda: _cfg_with()):
        await eventhub.emit({"request_id": "r1"})
        assert eventhub.retry_depth() == 1


# --------------------------------------------------------------------------- #
# The two numbers, and what /api/status publishes                             #
# --------------------------------------------------------------------------- #
def test_dropped_counts_attempts_while_lost_counts_events():
    """Deliberate asymmetry, and the reason both exist: `dropped` is an upper
    bound useful during an incident, `lost` is the exact billing-data-is-gone
    number to alert on."""
    stats = eventhub.stats()
    assert stats["dropped"] == eventhub.dropped_count()
    assert stats["lost"] == eventhub.lost_count()
    assert set(stats["by_reason"]) == {
        "send", "enqueue", "init", "retry", "serialize",
    }


def test_status_never_publishes_an_exception_message():
    """/api/status is UNAUTHENTICATED. Azure SDK error strings carry the
    namespace FQDN, the event hub name, the MI client id and IMDS URLs, so only
    the class name may be exposed; the message goes to the log."""
    secret = "https://tokenfoundry-ehns-SECRET.servicebus.windows.net/?client_id=abc"
    eventhub._note("send", RuntimeError(secret))
    recent = eventhub.stats()["recent"][-1]
    assert recent["error"] == "RuntimeError"
    assert "SECRET" not in str(recent)


# --------------------------------------------------------------------------- #
# Anthropic streamed usage — the truncation that hid cache-write tokens        #
# --------------------------------------------------------------------------- #
_ANTHROPIC_STREAM = (
    'data: {"type":"message_start","message":{"usage":{'
    '"input_tokens":121,"cache_read_input_tokens":1990,'
    '"cache_creation_input_tokens":500,'
    '"cache_creation":{"ephemeral_5m_input_tokens":500,"ephemeral_1h_input_tokens":0},'
    '"inference_geo":"us"}}}\n\n'
    'data: {"type":"message_delta","usage":{"input_tokens":0,"output_tokens":20,'
    '"output_tokens_details":{"thinking_tokens":7}}}\n\n'
)


def test_streamed_anthropic_usage_keeps_cache_write_tokens():
    """cache_creation_input_tokens is Anthropic's cache-WRITE count, billed at
    1.25x input — the most expensive token type on Opus. The old three-key
    allowlist dropped it, so a streamed call could not satisfy
    tests/test_usage_parse.py::test_anthropic_total_includes_cache_creation."""
    u = server._parse_anthropic_sse_usage(_ANTHROPIC_STREAM)
    assert u["cache_creation_input_tokens"] == 500


def test_streamed_anthropic_usage_keeps_every_upstream_field():
    """These fields exist only in that one response; dropping them is
    unrecoverable, and copilot_usage does not carry them."""
    u = server._parse_anthropic_sse_usage(_ANTHROPIC_STREAM)
    assert u["cache_creation"]["ephemeral_5m_input_tokens"] == 500
    assert u["output_tokens_details"]["thinking_tokens"] == 7
    assert u["inference_geo"] == "us"


def test_a_later_zero_does_not_erase_an_earlier_real_value():
    """message_delta re-reports input_tokens as 0. The old code filtered zeros
    via `if isinstance(v, int) and v`; generalising to all keys has to keep
    that or the input count collapses to 0 on every streamed call."""
    u = server._parse_anthropic_sse_usage(_ANTHROPIC_STREAM)
    assert u["input_tokens"] == 121
    assert u["output_tokens"] == 20


def test_canonical_counters_are_always_present():
    """The call site subscripts these three; a verbatim merge alone would
    KeyError on a stream that never reported them."""
    u = server._parse_anthropic_sse_usage("data: [DONE]\n\n")
    assert u == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
    }


# --------------------------------------------------------------------------- #
# Scheduling from a generator's finally                                        #
# --------------------------------------------------------------------------- #
_TENANT = {
    "request_id": "req-1",
    "subscription": "vk_test",
    "api_id": "llm-openai",
    "client_key_fp": None,
    "via_apim": True,
    "audit": False,
}


def test_spawn_emit_does_not_await():
    """It is called from inside a `finally` during GeneratorExit, where any
    suspension raises. If this ever becomes a coroutine the streaming paths
    start losing events again."""
    assert not asyncio.iscoroutinefunction(server._spawn_emit)


async def test_spawn_emit_schedules_without_blocking():
    sent: list[dict] = []

    async def _fake_emit(record):
        sent.append(record)

    with _swap(server.eventhub, "emit", _fake_emit):
        server._spawn_emit(
            tenant=_TENANT, end_user=None, model="gpt-4o-mini", served=None,
            endpoint="chat", usage3=(1, 2, 3), streamed=True, estimated=False,
        )
        assert sent == []  # nothing ran yet: create_task does not execute inline
        await server.drain_emits()
    assert len(sent) == 1
    assert sent[0]["request_id"] == "req-1"


async def test_saturation_is_counted_not_silent():
    """A bounded in-flight set is fine; dropping past it without a counter is
    the same invisible loss this whole change exists to remove."""
    async def _never(_record):
        await asyncio.sleep(3600)

    stuck = [asyncio.create_task(_never(None)) for _ in range(server._MAX_PENDING_EMITS)]
    try:
        with _swap(server, "_PENDING_EMITS", set(stuck)), _swap(
            server.eventhub, "emit", _never
        ):
            before = eventhub.lost_count()
            server._spawn_emit(
                tenant=_TENANT, end_user=None, model="m", served=None,
                endpoint="chat", usage3=(0, 0, 0), streamed=True, estimated=False,
            )
            assert eventhub.lost_count() == before + 1
    finally:
        for t in stuck:
            t.cancel()
        await asyncio.gather(*stuck, return_exceptions=True)


def test_usage_record_is_pure_and_matches_the_importer_contract():
    """_usage_record is the billing contract with
    app/services/usage_capture_import.py::event_to_document."""
    from datetime import UTC, datetime

    from app.services.usage_capture_import import event_to_document

    rec = server._usage_record(
        tenant=_TENANT, end_user="alice@example.com", model="claude-opus-4.8",
        served="claude-opus-4-8", endpoint="messages", usage3=(10, 20, 30),
        streamed=True, estimated=False, ts=datetime.now(UTC), audit_blob=None,
        cached=5, usage={"input_tokens": 10}, copilot_usage=None,
    )
    doc = event_to_document(rec, {})
    assert doc["id"] == rec["request_id"]
    assert doc["streamed"] is True


# --------------------------------------------------------------------------- #
# The empty stream — an upstream refusal that every surface read as success    #
#                                                                              #
# StreamingResponse puts `200 OK` on the wire BEFORE the generator body runs,  #
# and the first call upstream happens inside that body. So when upstream        #
# answered 429 mid-generator, the client got a 200 with an empty stream, the    #
# gateway logged a success, and the hub recorded status=200 with an ESTIMATED   #
# prompt count for a call that consumed nothing. dev-17 did this 58 times under #
# load, and the non-streaming path — which has always written (0, 0, 0) and the #
# real status on a non-200 — did not. These pin the two paths together.        #
# --------------------------------------------------------------------------- #
copilot_client = pytest.importorskip(
    "hub.copilot_client", reason="vendored hub deps not installed"
)


def test_upstream_status_is_data_not_a_formatted_string():
    """The status has to survive as an int. Scraping it back out of a message
    is not a basis for a billing record."""
    exc = copilot_client.UpstreamStatusError(429, '{"error":"rate limited"}')
    assert exc.status == 429
    assert exc.body == '{"error":"rate limited"}'
    # Still a RuntimeError, which is what stream() raised before.
    assert isinstance(exc, RuntimeError)


def test_sse_error_event_is_well_formed_and_terminates_the_stream():
    """The client has already been told 200, so this event is the only signal
    left. It must parse as SSE and end the stream, or an SDK will hang."""
    import json as _json

    raw = server._sse_error(429, '{"error":"too many requests"}').decode()
    lines = [ln for ln in raw.split("\n") if ln.startswith("data:")]
    assert len(lines) == 2, raw
    payload = _json.loads(lines[0][5:].strip())
    assert payload["error"]["code"] == 429
    assert payload["error"]["type"] == "upstream_error"
    assert lines[1].strip() == "data: [DONE]"


def _run_stream(monkeypatch, *, raiser, path="/v1/chat/completions", body=None):
    """Drive a streaming request end-to-end and return (chunks, emitted-kwargs).

    Uses the real app so the fix is exercised through `StreamingResponse`, which
    is where the whole problem lives — a unit test of the generator in isolation
    would not reproduce "headers already sent".
    """
    from fastapi.testclient import TestClient

    emitted: list[dict] = []
    monkeypatch.setattr(server.store, "get_require_auth", lambda _d: False)
    monkeypatch.setattr(server.cc, "is_authenticated", lambda: True)
    monkeypatch.setattr(server.cc, "stream", raiser)
    monkeypatch.setattr(server, "_spawn_emit", lambda **kw: emitted.append(kw))

    with TestClient(server.app) as client:
        resp = client.post(path, json=body or {
            "model": "gpt-4o-mini", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        return resp, resp.content.decode(), emitted


def test_upstream_refusal_is_recorded_under_its_real_status(monkeypatch):
    """The bug: status=200 for a call upstream refused. The portal's
    succeeded/failed split and the billing ledger both read this field."""
    async def _boom(*_a, **_kw):
        raise copilot_client.UpstreamStatusError(429, "too many requests")
        yield b""  # pragma: no cover — makes this an async generator

    _resp, _text, emitted = _run_stream(monkeypatch, raiser=_boom)

    assert len(emitted) == 1
    assert emitted[0]["status"] == 429


def test_upstream_refusal_bills_nothing(monkeypatch):
    """Nothing was consumed, so nothing may be billed. Substituting the prompt
    ESTIMATE here is what put `in=10, out=0` on 58 dev-17 records that returned
    no content at all — the non-streaming path writes (0, 0, 0)."""
    async def _boom(*_a, **_kw):
        raise copilot_client.UpstreamStatusError(429, "too many requests")
        yield b""  # pragma: no cover

    _resp, _text, emitted = _run_stream(monkeypatch, raiser=_boom)

    assert emitted[0]["usage3"] == (0, 0, 0)
    # (0, 0, 0) after a refusal is a measurement, not a guess. Flagging it as
    # estimated would file a real failure under "we approximated".
    assert emitted[0]["estimated"] is False


def test_client_is_told_instead_of_getting_an_empty_stream(monkeypatch):
    """A 200 with zero events is indistinguishable from "the model said
    nothing", so the client cannot know to retry. It gets an error event now."""
    async def _boom(*_a, **_kw):
        raise copilot_client.UpstreamStatusError(429, "too many requests")
        yield b""  # pragma: no cover

    _resp, text, _emitted = _run_stream(monkeypatch, raiser=_boom)

    assert "upstream_error" in text
    assert '"code": 429' in text or '"code":429' in text
    assert "[DONE]" in text


def test_a_healthy_stream_still_estimates_its_prompt(monkeypatch):
    """Guard against over-correcting: the backend routinely omits prompt_tokens
    on streams, and estimating there is correct — content WAS delivered."""
    async def _ok(*_a, **_kw):
        yield (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')

    _resp, _text, emitted = _run_stream(monkeypatch, raiser=_ok)

    assert len(emitted) == 1
    assert emitted[0]["status"] == 200
    assert emitted[0]["usage3"][0] > 0, "prompt should be estimated for real content"
    assert emitted[0]["estimated"] is True
