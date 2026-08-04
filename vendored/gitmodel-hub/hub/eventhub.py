"""Usage-record emission to Azure Event Hub.

The hub keeps no local usage store. Every completed `/v1/*` request produces one
event carrying the upstream GitHub Copilot `copilot_usage` object **verbatim**;
the control plane drains Event Hub Capture into Cosmos and does the cost
arithmetic there. Keeping the raw payload means a change in upstream's billing
schema is a re-import, not a data-loss event, and it keeps the hub free of any
price table.

Two properties this module must never violate:

1. **`emit()` never raises and never blocks on the network.** It runs on the
   request path. The producer is in *buffered* mode, so `send_event()` only
   appends to an in-process buffer and returns; batching and the AMQP send
   happen on the SDK's own background task. Every failure mode — unconfigured,
   credential error, buffer full, broker down — degrades to a dropped event and
   a log line, never to a failed client request.
2. **Zero cost when unconfigured.** With `TF_EVENTHUB_FQDN` unset the module
   never imports a credential or opens a connection, so the hub still runs
   standalone (localhost, docker-compose) with no Azure dependency.

Data-loss window is whatever sits in the buffer, bounded by
`TF_EVENTHUB_MAX_WAIT_SECONDS`. `aclose()` flushes it, so a graceful shutdown
(including a Container Apps rolling update) loses nothing; only an ungraceful
kill does. Eliminating that entirely would require durable local storage, which
the hub deliberately does not have (see `infra/main.tf`: SQLite lives in /tmp).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

# Lazily built singletons — importing azure.* is deferred so an unconfigured
# hub never pays the import cost (and does not need the packages installed).
_producer: Any = None
_credential: Any = None
_init_failed = False

# Events the SDK could not deliver, plus events we never handed to the SDK.
# Surfaced on /healthz so a silently-broken billing feed is observable.
_dropped = 0


def dropped_count() -> int:
    """Number of usage events lost since process start."""
    return _dropped


async def _on_error(events: Any, partition_id: Any, error: Any) -> None:
    """Buffered-producer failure callback (called from the SDK's task).

    MUST be a coroutine function: the *aio* producer `await`s these callbacks.
    A plain `def` returns None, the SDK awaits None, and the resulting
    TypeError is swallowed inside the SDK — which would silently disable the
    only counter that makes a broken billing feed visible.
    """
    global _dropped
    try:
        lost = len(list(events))
    except TypeError:  # None, or a batch object that isn't iterable
        lost = 1
    # Floor of 1: this callback only fires on a real failure, and a batch we
    # cannot count is still a loss. Counting 0 here would be the same silent
    # zero the async-signature bug produced.
    _dropped += max(lost, 1)
    log.warning("event hub send failed (partition=%s): %s", partition_id, error)


async def _on_success(events: Any, partition_id: Any) -> None:
    # Deliberately silent: one log line per request would dwarf the access log.
    return None


async def _get_producer() -> Any:
    """Return the buffered producer, building it on first use.

    Returns None when Event Hub is not configured, or when a previous build
    attempt failed — we do not retry construction per request, since a bad
    config would otherwise cost a credential round-trip on every call.
    """
    global _producer, _credential, _init_failed

    if _producer is not None:
        return _producer
    if _init_failed:
        return None

    st = get_settings()
    if not st.eventhub_enabled:
        _init_failed = True
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
        log.info(
            "event hub producer ready: %s/%s", st.eventhub_fqdn, st.eventhub_name
        )
        return _producer
    except Exception as exc:  # noqa: BLE001 — never let config break the gateway
        _init_failed = True
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
    return {"hub_id": get_settings().hub_id or None, **record}


async def emit(record: dict[str, Any]) -> None:
    """Queue one usage record. Never raises."""
    global _dropped
    try:
        producer = await _get_producer()
        if producer is None:
            return
        from azure.eventhub import EventData

        await producer.send_event(EventData(json.dumps(_envelope(record), default=str)))
    except Exception as exc:  # noqa: BLE001 — billing must never break serving
        _dropped += 1
        log.warning("dropped usage event: %s", exc)


async def aclose() -> None:
    """Flush the buffer and release the connection. Never raises."""
    global _producer, _credential, _init_failed
    producer, credential = _producer, _credential
    _producer = _credential = None
    _init_failed = False
    for closeable in (producer, credential):
        if closeable is None:
            continue
        try:
            await closeable.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("event hub close failed: %s", exc)
