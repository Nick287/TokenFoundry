"""Batch import of usage events: Event Hub Capture blobs -> Cosmos.

The path a billing record travels:

    hub  --(one event per request)-->  Event Hub
         --(Capture, every ~5 min)-->  Avro blobs in Blob Storage
         --(this module)------------>  one Cosmos document per request

Why a batch importer and not an Event Hub consumer: Capture already drains the
hub for us, so there is no consumer group to run, no checkpoint store, no
partition leases. The cost is latency — nothing here can be fresher than
Capture's flush interval. That is acceptable because this is the BILLING line;
the real-time view is App Insights (see usage_ingest.AppInsightsUsage).

Cost is taken from upstream's own `copilot_usage.total_nano_aiu`, never
recomputed from a local price table. See app.services.billing for why.

Idempotency: the Cosmos document id is the APIM request id, and writes are
upserts. Capture is at-least-once and this job re-scans an overlap window, so
the same event WILL be imported more than once — that has to be a no-op, and it
is. The same property makes it safe for several control-plane replicas to run
this concurrently: they duplicate work, they don't corrupt anything.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

from app.config import get_settings
from app.db import SessionLocal
from app.models.orm import ImportWatermark, ModelRoute
from app.services.billing import cost_from_copilot_usage, tokens_from_copilot_usage
from app.services.usage_ingest import UsageStore

logger = logging.getLogger(__name__)

WATERMARK_SOURCE = "usage_capture"

# How far back before the watermark to re-scan on every run. A blob's
# last_modified is set when Capture finishes writing it, and listing is not
# transactional with that, so a blob can become visible with a timestamp
# slightly behind one we already recorded. Re-reading it is free (upsert), and
# missing it would silently lose a customer's billing data — so the overlap is
# deliberately generous.
_OVERLAP = timedelta(minutes=10)

# Cap on blobs handled per run, so a first run against a long backlog can't turn
# into an unbounded single pass. The watermark advances to whatever was actually
# processed, so the next run picks up exactly where this one stopped.
_MAX_BLOBS_PER_RUN = 500


def _parse_ts(value: Any) -> datetime:
    """Event timestamp -> aware UTC datetime, falling back to now.

    The hub stamps `ts` itself, so a malformed one means a hub bug, not a
    hostile input; we still refuse to crash the whole batch over one record.

    ALWAYS converted to UTC, never merely made aware. The document stores
    `ts.isoformat()`, and the usage aggregation filters time with a string
    comparison (`c.ts >= @since`) — which is only correct while every stored
    timestamp shares one offset. A record written as `+08:00` would sort as if
    it were 8 hours later than it was, quietly landing in the wrong window.
    """
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    return datetime.now(UTC)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)


def event_to_document(event: dict, markups: dict[str, float]) -> dict[str, Any] | None:
    """One hub usage event -> one Cosmos document.

    Returns None for an event with no request id: that is the document key, and
    without it we'd be writing records that can never be deduplicated.

    The document shape is deliberately the FLAT one the read path already
    understands (`subscription` / `prompt_tok` / `cost_usd`, see
    app/api/usage.py::_extract_tokens), and the partition key keeps the
    `<subscription>_<yyyyMM>` format the retired APIM policy used — so history
    written before this change stays queryable alongside.
    """
    request_id = event.get("request_id")
    if not request_id:
        return None

    subscription = event.get("subscription") or "unknown"
    ts = _parse_ts(event.get("ts"))
    model = event.get("model") or "unknown"
    copilot_usage = event.get("copilot_usage")

    # Prefer upstream's own per-type split: it separates cache reads out of the
    # input count, which the plain `usage` object does not. Fall back to the
    # counts the hub already normalized.
    tokens = tokens_from_copilot_usage(copilot_usage)
    if not any(tokens.values()):
        tokens = {
            "prompt_tok": _int(event.get("input_tokens")),
            "completion_tok": _int(event.get("output_tokens")),
            "cached_tok": _int(event.get("cached_tokens")),
            # The hub's normalized counts have no cache-write equivalent — only
            # upstream splits that out. Written as an explicit 0 rather than
            # omitted so every document carries the same key set, which is what
            # lets the aggregation SUM() it without a per-row null check.
            "cache_write_tok": 0,
        }

    cost = cost_from_copilot_usage(copilot_usage, markups.get(model, 0.0))
    if cost is not None:
        cost_source = "copilot_usage"
    else:
        # Upstream priced nothing for this call — a non-Copilot backend (the
        # image path is Azure OpenAI) or a response shape we didn't recognise.
        # Record 0 and SAY SO in cost_source rather than guessing from a local
        # table: a wrong number that looks authoritative is worse than a zero
        # that is visibly flagged.
        cost_source = "unpriced"

    return {
        "id": str(request_id),
        "pk": f"{subscription}_{ts:%Y%m}",
        "ts": ts.isoformat(),
        "subscription": subscription,
        "api": event.get("api_id"),
        "route": model,
        "served_model": event.get("served_model"),
        "endpoint": event.get("endpoint"),
        "streamed": bool(event.get("streamed")),
        "status": _int(event.get("status")),
        # Chargeback layer B: only present when the client sent
        # metadata.user_id / user. Absent is normal, not an error.
        "end_user": event.get("end_user"),
        # Layer A fallback identity when APIM didn't stamp a subscription — a
        # non-reversible fingerprint of the calling key, never the key itself.
        "client_key_fp": event.get("client_key_fp"),
        "via_apim": bool(event.get("via_apim")),
        # Which deployed hub served the call — the control plane's own
        # GitHubAccount.id, so this joins straight to that row. Not a billing
        # input (tenants are billed by `subscription`); it exists so a month's
        # cost can be split by upstream account and reconciled against the bill
        # GitHub sends for each one. None for events emitted before this field
        # existed, or by a hub deployed outside the control plane.
        "hub_id": event.get("hub_id") or None,
        **tokens,
        "total_tok": _int(event.get("total_tokens")),
        # True when the hub had to estimate token counts (upstream returned
        # none). Such rows should not be trusted for billing disputes.
        "estimated": bool(event.get("estimated")),
        "cost_usd": cost.cost_usd if cost else 0.0,
        "billed_usd": cost.billed_usd if cost else 0.0,
        "cost_source": cost_source,
        # Kept verbatim for audit: it carries upstream's own unit prices, so a
        # disputed invoice can be recomputed from the document alone without
        # knowing what our price table said that day.
        "copilot_usage": copilot_usage,
        "usage": event.get("usage"),
        # Path of the archived raw request/response inside the audit storage
        # account, or None (the normal case — archival is off unless a platform
        # operator switched the tenant on). Just a POINTER: the bodies are not
        # copied into Cosmos, so this document stays safe for anyone who may
        # already read usage data. Following the pointer needs a blob role that
        # nothing in this system holds.
        #
        # It is also a promise rather than a receipt. The hub returns the path
        # before the upload finishes, so a dangling pointer means the upload
        # failed — check the emitting hub's /healthz audit_payloads_dropped.
        "audit_blob": event.get("audit_blob") or None,
    }


def _route_markups() -> dict[str, float]:
    """model name -> markup_pct, read once per run.

    Markup is a per-route property, and the event carries the requested model
    alias, which is exactly ModelRoute.name. Unknown model => 0 markup (bill
    cost through) rather than a guess.
    """
    db = SessionLocal()
    try:
        rows = db.query(ModelRoute.name, ModelRoute.markup_pct).all()
        return {r[0]: float(r[1] or 0.0) for r in rows}
    except Exception:  # noqa: BLE001 — a DB hiccup must not stop billing import
        logger.warning("usage-import: could not read route markups", exc_info=True)
        return {}
    finally:
        db.close()


def _read_watermark() -> datetime | None:
    db = SessionLocal()
    try:
        row = db.get(ImportWatermark, WATERMARK_SOURCE)
        if row is None or not row.position:
            return None
        return datetime.fromisoformat(row.position)
    except (ValueError, TypeError):
        logger.warning("usage-import: unparseable watermark; starting from scratch")
        return None
    finally:
        db.close()


def _write_watermark(position: datetime) -> None:
    db = SessionLocal()
    try:
        row = db.get(ImportWatermark, WATERMARK_SOURCE)
        if row is None:
            db.add(
                ImportWatermark(source=WATERMARK_SOURCE, position=position.isoformat())
            )
        else:
            row.position = position.isoformat()
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        # Not fatal: the next run re-reads the older watermark and re-imports
        # the same blobs, which upsert makes harmless.
        logger.warning("usage-import: failed to persist watermark", exc_info=True)
    finally:
        db.close()


def _decode_avro(data: bytes) -> list[dict]:
    """Capture Avro blob -> the JSON event bodies inside it.

    Each Avro record is Capture's envelope (SequenceNumber / Offset /
    EnqueuedTimeUtc / Properties / Body); `Body` is the JSON the hub emitted.
    A record we can't decode is skipped, not fatal — one malformed event must
    not cost us a whole blob's worth of billing.
    """
    import fastavro

    out: list[dict] = []
    for record in fastavro.reader(BytesIO(data)):
        body = record.get("Body") if isinstance(record, dict) else None
        if body is None:
            continue
        try:
            parsed = json.loads(
                body.decode("utf-8") if isinstance(body, bytes | bytearray) else body
            )
        except (ValueError, UnicodeDecodeError):
            logger.warning("usage-import: skipping undecodable event body")
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


class UsageCaptureImporter:
    """Drains Capture blobs into Cosmos. Call `run_once()` on a timer."""

    def __init__(self) -> None:
        s = get_settings()
        self._account = s.usage_capture_storage_account
        self._container_name = s.usage_capture_container
        self._store = UsageStore()

    @property
    def configured(self) -> bool:
        return bool(self._account and self._container_name)

    def _container(self) -> ContainerClient:
        return ContainerClient(
            account_url=f"https://{self._account}.blob.core.windows.net",
            container_name=self._container_name,
            credential=DefaultAzureCredential(),
        )

    def run_once(self) -> dict[str, int]:
        """Import every blob newer than the watermark. Returns run counters.

        Never raises: this runs on a background timer where an exception would
        just kill the loop. Everything is logged and retried on the next tick,
        because the watermark only advances over blobs that actually landed.
        """
        stats = {"blobs": 0, "events": 0, "written": 0, "skipped": 0}
        if not self.configured:
            return stats

        watermark = _read_watermark()
        since = (watermark - _OVERLAP) if watermark else None
        markups = _route_markups()
        high_water = watermark

        try:
            container = self._container()
            # Sorted by modification time so a mid-run failure still leaves the
            # watermark on a contiguous prefix — never past a blob we skipped.
            pending = sorted(
                (
                    b
                    for b in container.list_blobs()
                    if since is None or b.last_modified > since
                ),
                key=lambda b: b.last_modified,
            )[:_MAX_BLOBS_PER_RUN]

            for blob in pending:
                data = container.download_blob(blob.name).readall()
                events = _decode_avro(data)
                stats["blobs"] += 1
                stats["events"] += len(events)
                for event in events:
                    doc = event_to_document(event, markups)
                    if doc is None:
                        stats["skipped"] += 1
                        continue
                    self._store.upsert(doc)
                    stats["written"] += 1
                # Advance only after the whole blob is in — a partial blob must
                # be re-read, and re-reading is free.
                if high_water is None or blob.last_modified > high_water:
                    high_water = blob.last_modified
        except Exception:  # noqa: BLE001 — background job, must not die
            logger.exception("usage-import: run failed after %s", stats)

        if high_water is not None and high_water != watermark:
            _write_watermark(high_water)
        if stats["blobs"]:
            logger.info("usage-import: %s", stats)
        else:
            # Log the idle pass too, at DEBUG. "No log line" previously meant
            # both "found nothing" and "never ran", and the two need completely
            # different fixes — that ambiguity already sent one investigation
            # down the wrong path. The watermark is included because it is the
            # single most useful value when usage appears to be missing: if it
            # is advancing, the gap is upstream of this job.
            logger.debug("usage-import: no new blobs (watermark=%s)", watermark)
        return stats
