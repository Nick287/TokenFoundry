"""Usage-record emission to Azure Event Hub.

The hub keeps no local usage store. Every completed `/v1/*` request produces one
event carrying the upstream GitHub Copilot `copilot_usage` object **verbatim**;
the control plane drains Event Hub Capture into Cosmos and does the cost
arithmetic there. Keeping the raw payload means a change in upstream's billing
schema is a re-import, not a data-loss event, and it keeps the hub free of any
price table.

Two properties this module must never violate:

1. **`emit()` never raises and never blocks the request path.** The producer is
   in *buffered* mode, so `send_event()` appends to an in-process buffer and
   returns; batching and the AMQP send happen on the SDK's own background task.
   Note it is `timeout=`, not buffered mode by itself, that makes this true —
   buffered `send_event()` WAITS for buffer space, so a stalled namespace would
   otherwise block every request once the buffer filled.
2. **Zero cost when unconfigured.** With `TF_EVENTHUB_FQDN` unset the module
   never imports a credential or opens a connection, so the hub still runs
   standalone (localhost, docker-compose) with no Azure dependency.

Counting, and why there are two numbers
---------------------------------------
A real incident on dev-15 lost 21 usage records and nothing said so: the counter
existed, `/api/status` returned it, and no consumer read it. Worse, the number
it returned could not distinguish *which* of four paths had failed, and two of
those paths were not counted at all. So:

* `dropped_count()` counts failed **hand-offs**. One event that fails three
  times contributes 3. It is an upper bound on loss and a good "how turbulent is
  it" gauge — it is NOT the number of lost records. Kept with its original name
  and meaning because `/api/status` and the existing tests depend on it.
* `lost_count()` counts **events given up on for good**, each at most once. This
  is the billing-data-is-actually-gone number, and the one to alert on.

Retry, and what it does not buy
-------------------------------
Events the broker refuses are parked in a bounded in-memory queue and re-sent
with exponential backoff. That recovers a transient outage, which is the
overwhelmingly likely cause of the dev-15 loss (the buffer holds 5000 and only
144 requests were in flight). It does NOT survive an ungraceful kill: durability
would need a persistent volume, which the hub deliberately does not have (see
`infra/main.tf` — SQLite lives in /tmp). `aclose()` counts whatever is still
queued at shutdown rather than letting it vanish silently.

Retrying means at-least-once delivery of the same `request_id`. That is safe
only because the importer upserts on it (`app/services/usage_capture_import.py`
uses `"id": str(request_id)`) and re-scans an overlapping window anyway. A
future consumer that is not idempotent would double-count.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

# Lazily built singletons — importing azure.* is deferred so an unconfigured
# hub never pays the import cost (and does not need the packages installed).
_producer: Any = None
_credential: Any = None

# Two flags, not one. They used to be a single `_init_failed`, which conflated
# "Event Hub is switched off for this deployment" (correct, silent) with
# "construction threw" (a real outage). The second latched forever and made
# every later emit return without a counter or a log — a billing feed that had
# stopped working with no signal whatsoever.
_disabled = False          # not configured: silent by design, nothing counted
_init_failed = False       # construction threw: every dropped event is real loss
_init_error = ""           # exception CLASS NAME, for /api/status (see _note)
_last_init_try = 0.0

# --- counters, split by cause so the next incident says which path failed ---
_failed_send = 0           # SDK exhausted its own retries and handed events back
_failed_enqueue = 0        # send_event() raised or timed out on the request path
_failed_init = 0           # producer unavailable because construction threw
_failed_retry = 0          # a retry attempt failed at hand-off
_failed_serialize = 0      # the record itself would not JSON-encode
_retried = 0               # retry attempts made
_recovered = 0             # retried events the broker later acknowledged
_lost = 0                  # given up on for good — counts EVENTS, once each

_LOG_THROTTLE_SECONDS = 60.0
_last_log: dict[str, float] = {}

# Recent failure reasons for /api/status. Bounded, and reset by a restart —
# a deliberate trade for needing no log-collection infrastructure.
_reasons: deque[dict[str, Any]] = deque(maxlen=8)

_RETRY_CAP_SECONDS = 60.0
_TICK_SECONDS = 0.5
_IDLE_SECONDS = 5.0
_SHUTDOWN_DRAIN_SECONDS = 5.0
_INIT_RETRY_SECONDS = 60.0
_INIT_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class _Pending:
    """One parked event.

    Holds the ALREADY-SERIALIZED body, never the record dict. That keeps the
    original `ts` and `hub_id` on a retried event (so the importer's time
    filtering stays correct), keeps `EventData` out of the queue (so the queue
    is testable without the azure SDK installed), and means re-serialization
    cannot introduce a second failure mode at drain time.
    """

    body: str
    attempts: int
    next_at: float


_retry_q: deque[_Pending] = deque()
_drainer: asyncio.Task[None] | None = None
_wake: asyncio.Event | None = None


def _cfg() -> Any:
    return get_settings()


def dropped_count() -> int:
    """Failed hand-offs since process start.

    NOT the number of lost records: an event that fails three times counts 3.
    Use `lost_count()` for "billing data is gone". Kept under this name and
    meaning because `/api/status` and the existing tests read it.
    """
    return (
        _failed_send + _failed_enqueue + _failed_init
        + _failed_retry + _failed_serialize
    )


def lost_count() -> int:
    """Events given up on for good. Counts EVENTS, each at most once."""
    return _lost


def retry_depth() -> int:
    return len(_retry_q)


def _note(kind: str, error: Any = None, count: int = 1) -> None:
    """Record one failure reason for /api/status. Never raises.

    Only the exception CLASS NAME is kept, never the message. `/api/status` is
    UNAUTHENTICATED, and Azure SDK error strings routinely carry the namespace
    FQDN, the event hub name, the managed-identity client id and IMDS URLs.
    The full message goes to the log, which is not world-readable.
    """
    if isinstance(error, BaseException):
        detail: str | None = type(error).__name__
    elif isinstance(error, str):
        detail = error[:80]
    else:
        detail = None
    _reasons.append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "kind": kind,
            "count": count,
            "error": detail,
        }
    )


def _log_throttled(key: str, msg: str, *args: Any) -> None:
    """One log line per key per minute. A latched failure would otherwise emit
    a line per request and drown the access log it is trying to explain."""
    now = time.monotonic()
    if now - _last_log.get(key, 0.0) < _LOG_THROTTLE_SECONDS:
        return
    _last_log[key] = now
    log.warning(msg, *args)


def _state() -> str:
    if _disabled:
        return "disabled"
    if _init_failed:
        return "init_failed"
    if _retry_q or _lost:
        return "degraded"
    return "ok"


def stats() -> dict[str, Any]:
    """The /api/status breakdown. Lives here so its shape is unit-testable
    without FastAPI, and so `server.api_status` stays a one-liner."""
    buffered = None
    if _producer is not None:
        with contextlib.suppress(Exception):
            buffered = _producer.total_buffered_event_count
    return {
        "state": _state(),
        "dropped": dropped_count(),
        "lost": _lost,
        "retried": _retried,
        "recovered": _recovered,
        "by_reason": {
            "send": _failed_send,
            "enqueue": _failed_enqueue,
            "init": _failed_init,
            "retry": _failed_retry,
            "serialize": _failed_serialize,
        },
        "retry_queue": len(_retry_q),
        "retry_capacity": _cfg().eventhub_retry_max_queue,
        "buffered": buffered,
        "recent": list(_reasons),
    }


def record_drop(kind: str, error: Any = None, count: int = 1) -> None:
    """Let a caller outside this module report a loss it detected itself.

    `server._spawn_emit` uses it: an event that could not even be scheduled is
    just as lost as one the broker refused, and it would otherwise be invisible.
    """
    global _failed_enqueue, _lost
    _failed_enqueue += count
    _lost += count
    _note(kind, error, count=count)


# --------------------------------------------------------------------------- #
# Recovering event bodies                                                      #
# --------------------------------------------------------------------------- #
def _body_text(ev: Any) -> str | None:
    """Best-effort JSON body of an EventData. None for anything opaque.

    Deliberately no `str(ev)` fallback: that would enqueue a repr and ship
    garbage to the billing store. An unreadable event is counted as lost.
    """
    try:
        body = ev.body_as_str()
    except Exception:  # noqa: BLE001 — a batch object, None, or a foreign type
        return None
    return body if isinstance(body, str) and body else None


def _attempt_of(ev: Any) -> int:
    """Attempt count stamped on a retried event; 0 for a first-time send.

    Rides an AMQP application property rather than the JSON body: the importer
    never reads properties, and the body must stay byte-identical so a re-import
    upserts onto the same document instead of creating a variant.
    """
    try:
        props = ev.properties or {}
    except Exception:  # noqa: BLE001
        return 0
    for key in ("tf-attempt", b"tf-attempt"):  # AMQP round-trips keys as bytes
        val = props.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return 0


def _make_event(body: str, attempts: int) -> Any:
    """Build an EventData carrying its attempt count.

    Separated out purely so tests can substitute it: `azure-eventhub` is not
    installed in the dev venv, and this is the only line of the drain path that
    needs it.
    """
    from azure.eventhub import EventData

    ev = EventData(body)
    if attempts:
        ev.properties = {"tf-attempt": attempts}
    return ev


# --------------------------------------------------------------------------- #
# Producer                                                                     #
# --------------------------------------------------------------------------- #
async def _on_error(events: Any, partition_id: Any, error: Any) -> None:
    """Buffered-producer failure callback (called from the SDK's flush task).

    MUST be a coroutine function: the *aio* producer `await`s these callbacks.
    A plain `def` returns None, the SDK awaits None, and the resulting
    TypeError is swallowed inside the SDK — which would silently disable the
    only counter that makes a broken billing feed visible.

    MUST NOT await anything or touch the producer. It runs INSIDE the SDK's
    send path: an await stalls flushing for every partition, and calling
    `send_event()` re-enters the very buffer whose failure we are being told
    about. Everything here is synchronous — count, recover the bodies, park
    them, return.
    """
    global _failed_send, _lost
    try:
        batch = list(events)
    except TypeError:  # None, or a batch object that isn't iterable
        batch = []
    # Floor of 1: this callback only fires on a real failure, and a batch we
    # cannot count is still a loss. Counting 0 here would be the same silent
    # zero the async-signature bug produced.
    n = max(len(batch), 1)
    _failed_send += n
    _note("send", error, count=n)
    log.warning("event hub send failed (partition=%s): %s", partition_id, error)

    for ev in batch:
        body = _body_text(ev)
        if body is None:
            _lost += 1  # cannot re-send what we cannot read
            continue
        _requeue(body, _attempt_of(ev) + 1)


async def _on_success(events: Any, partition_id: Any) -> None:
    """Deliberately near-silent: one log line per request would dwarf the
    access log. The only work done is counting recoveries, and only once
    something has actually been retried."""
    global _recovered
    if not _retried:
        return
    try:
        for ev in events or ():
            if _attempt_of(ev):
                _recovered += 1
    except Exception:  # noqa: BLE001 — a statistic must never break a send
        pass


async def _get_producer() -> Any:
    """Return the buffered producer, building it on first use.

    Returns None when Event Hub is not configured (`_disabled`) or a previous
    build attempt threw (`_init_failed`). Neither path does I/O, so a bad config
    costs nothing per request.

    There is no `await` between the `_producer is None` check and the
    assignment, so concurrent emits cannot double-construct. Adding one would
    make a lock mandatory.
    """
    global _producer, _credential, _init_failed, _disabled, _init_error

    if _producer is not None:
        return _producer
    if _disabled or _init_failed:
        return None

    st = _cfg()
    if not st.eventhub_enabled:
        _disabled = True  # NOT a failure. Nothing is counted, nothing is logged.
        return None

    try:
        from azure.eventhub.aio import EventHubProducerClient
        from azure.identity.aio import DefaultAzureCredential

        _credential = (
            DefaultAzureCredential(managed_identity_client_id=st.eventhub_client_id)
            if st.eventhub_client_id
            else DefaultAzureCredential()
        )
        _producer = EventHubProducerClient(
            fully_qualified_namespace=st.eventhub_fqdn,
            eventhub_name=st.eventhub_name,
            credential=_credential,
            buffered_mode=True,
            on_success=_on_success,
            on_error=_on_error,
            max_wait_time=st.eventhub_max_wait_seconds,
            max_buffer_length=st.eventhub_max_buffer,
        )
        log.info("event hub producer ready: %s/%s", st.eventhub_fqdn, st.eventhub_name)
        return _producer
    except Exception as exc:  # noqa: BLE001 — never let config break the gateway
        _init_failed = True
        _init_error = type(exc).__name__
        log.warning("event hub disabled (init failed): %s", exc)
        return None


def _envelope(record: dict[str, Any]) -> dict[str, Any]:
    """Add the fields that identify the EMITTER rather than the request.

    Only `hub_id` so far. Stamped here instead of at the call site because every
    hub publishes into the same Event Hub: without it a usage record cannot say
    which GitHub account's Copilot quota served the call, and this is the one
    place that cannot forget to add it. Empty config normalizes to None so the
    import side has a single absent case, not two.
    """
    return {"hub_id": _cfg().hub_id or None, **record}


# --------------------------------------------------------------------------- #
# Retry queue                                                                  #
# --------------------------------------------------------------------------- #
def _backoff_seconds(attempts: int, base: float, cap: float) -> float:
    """2, 4, 8, 16, 32, ... seconds, capped. Pure, so it is testable without a
    clock; `_delay` adds the jitter."""
    return min(cap, base * (2 ** max(0, attempts - 1)))


def _delay(attempts: int) -> float:
    # +/-20% jitter so a fleet of hubs does not stampede a recovering namespace.
    base = _backoff_seconds(attempts, _cfg().eventhub_retry_base_seconds, _RETRY_CAP_SECONDS)
    return base * random.uniform(0.8, 1.2)  # noqa: S311 — jitter, not crypto


def _requeue(body: str, attempts: int) -> None:
    """Park one serialized event for a later attempt. Never raises, never awaits.

    Eviction is explicit rather than `deque(maxlen=)` so that a discarded event
    can be counted and reported. A silent maxlen drop would be exactly the kind
    of invisible loss this module exists to make visible.
    """
    global _lost
    st = _cfg()
    if attempts > st.eventhub_retry_max_attempts:
        _lost += 1
        _note("gave_up", f"{attempts - 1} attempts")
        return
    cap = max(st.eventhub_retry_max_queue, 1)
    while len(_retry_q) >= cap:
        _retry_q.popleft()  # drop the OLDEST: it has had its chances, and a
        _lost += 1          # newer event is likelier to still land
        _note("overflow", f"retry queue full ({cap})")
    _retry_q.append(_Pending(body, attempts, time.monotonic() + _delay(attempts)))
    _ensure_drainer()


def _give_up_all(reason: str) -> None:
    global _lost
    if not _retry_q:
        return
    n = len(_retry_q)
    _lost += n
    _retry_q.clear()
    _note("gave_up", reason, count=n)
    log.warning("event hub: gave up on %d queued usage events (%s)", n, reason)


def _ensure_drainer() -> None:
    """Start the drain task, or wake it. Never raises.

    Created lazily rather than at startup so a healthy hub has no background
    task at all, and — more importantly — so this module needs no lifespan hook.
    Starlette only runs `on_startup`/`on_shutdown` through its default lifespan,
    so introducing a custom one here would silently disable the hub's existing
    startup handler.

    The loop identity check matters under test: `TestClient` builds a fresh
    event loop per context, and a task pinned to a dead loop never runs again.
    """
    global _drainer, _wake
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (import time, teardown): nothing to schedule onto
    if _drainer is not None and not _drainer.done() and _drainer.get_loop() is loop:
        if _wake is not None:
            _wake.set()
        return
    _wake = asyncio.Event()
    _drainer = loop.create_task(_drain_loop())


async def _drain_once() -> None:
    """One pass over the due items. Never raises."""
    global _failed_retry, _retried
    if not _retry_q:
        return
    producer = await _get_producer()
    if producer is None:
        if _disabled:
            # Unconfigured: nothing will ever drain this, so say so once rather
            # than holding events forever in a queue with no outlet.
            _give_up_all("event hub not configured")
        return  # _init_failed: leave them queued; _maybe_reinit may recover

    now = time.monotonic()
    snapshot = list(_retry_q)
    _retry_q.clear()
    due: list[_Pending] = []
    for item in snapshot:
        (due if item.next_at <= now else _retry_q).append(item)

    for item in due:
        try:
            ev = _make_event(item.body, item.attempts)
            _retried += 1
            # timeout is NOT optional: buffered send_event waits for buffer
            # space, and an outage is exactly when the buffer is full. Without
            # it the drainer wedges on its first item forever.
            await producer.send_event(ev, timeout=_cfg().eventhub_send_timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            _failed_retry += 1
            _note("retry", exc)
            _requeue(item.body, item.attempts + 1)


async def _maybe_reinit() -> None:
    """At most one credential retry per minute, on the drain task only.

    Without this a transient IMDS hiccup at first use disables billing for the
    entire life of the container. Bounded by `wait_for` because
    DefaultAzureCredential walks several sources and each can take seconds — an
    unbounded call here would wedge the drain loop.
    """
    global _init_failed, _last_init_try
    if not (_init_failed and _retry_q):
        return
    now = time.monotonic()
    if now - _last_init_try < _INIT_RETRY_SECONDS:
        return
    _last_init_try = now
    _init_failed = False  # let the next _get_producer() try to rebuild
    with contextlib.suppress(Exception, asyncio.TimeoutError):
        await asyncio.wait_for(_get_producer(), timeout=_INIT_TIMEOUT_SECONDS)


async def _drain_loop() -> None:
    while True:
        try:
            await _drain_once()
            await _maybe_reinit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop itself must not die
            log.warning("event hub retry loop error: %s", exc)
        if _retry_q:
            await asyncio.sleep(_TICK_SECONDS)
        elif _wake is not None:
            _wake.clear()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(_wake.wait(), timeout=_IDLE_SECONDS)
        else:
            await asyncio.sleep(_IDLE_SECONDS)


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
async def emit(record: dict[str, Any]) -> None:
    """Queue one usage record. Never raises, never waits on the network."""
    global _failed_enqueue, _failed_init, _failed_serialize, _lost

    # Serialize FIRST, so an unencodable record is its own failure mode (it used
    # to be counted as a generic drop) and never enters the retry queue, where
    # it would fail identically on every attempt.
    try:
        body = json.dumps(_envelope(record), default=str)
    except Exception as exc:  # noqa: BLE001
        _failed_serialize += 1
        _lost += 1
        _note("serialize", exc)
        log.warning("unserializable usage record: %s", exc)
        return

    producer = await _get_producer()
    if producer is None:
        if _init_failed:
            _failed_init += 1  # counted EVERY time...
            _lost += 1
            _note("init", _init_error)
            _log_throttled(  # ...but logged at most once a minute
                "init",
                "event hub unavailable (init failed: %s); %d usage events dropped so far",
                _init_error,
                _failed_init,
            )
        return  # _disabled: silent, by design
    try:
        await producer.send_event(
            _make_event(body, 0), timeout=_cfg().eventhub_send_timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 — billing must never break serving
        _failed_enqueue += 1
        _note("enqueue", exc)
        log.warning("dropped usage event: %s", exc)
        _requeue(body, 1)  # we still hold the body — park it rather than lose it


async def aclose() -> None:
    """Cancel the drainer, make one last attempt, flush, then tell the truth.

    Order is load-bearing: cancel -> final drain (producer still alive) ->
    close() (which flushes, and may fire _on_error) -> count whatever is left.
    """
    global _producer, _credential, _init_failed, _disabled, _drainer, _lost

    drainer, _drainer = _drainer, None
    if drainer is not None and not drainer.done():
        drainer.cancel()
        with contextlib.suppress(BaseException):
            await drainer

    if _producer is not None and _retry_q:
        for item in _retry_q:
            item.next_at = 0.0  # at shutdown, everything is due now
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(_drain_once(), timeout=_SHUTDOWN_DRAIN_SECONDS)

    producer, credential = _producer, _credential
    _producer = _credential = None
    _init_failed = _disabled = False
    for closeable in (producer, credential):
        if closeable is None:
            continue
        try:
            await closeable.close()  # buffered mode flushes on the way out
        except Exception as exc:  # noqa: BLE001
            log.warning("event hub close failed: %s", exc)

    # close() flushes, and whatever it CANNOT flush comes back through
    # _on_error, which parks it in a queue nobody will ever drain. Count it out
    # loud rather than letting it vanish with the process.
    if _retry_q:
        n = len(_retry_q)
        _lost += n
        _retry_q.clear()
        _note("shutdown", f"{n} undelivered", count=n)
        log.warning("event hub: %d usage events lost at shutdown", n)
