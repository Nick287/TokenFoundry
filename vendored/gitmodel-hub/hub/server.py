"""FastAPI application exposing OpenAI- and Anthropic-compatible endpoints
backed by a personal GitHub Copilot subscription, plus a management portal.

Endpoints
---------
OpenAI-compatible (for Codex, OpenAI SDK, curl):
    GET  /v1/models
    POST /v1/chat/completions      (stream + non-stream)
    POST /v1/responses             (stream + non-stream)

Anthropic-compatible (for Claude Code, Anthropic SDK):
    POST /v1/messages              (stream + non-stream)

Management portal API:
    GET  /api/status
    POST /api/auth/device/start
    POST /api/auth/device/poll
    POST /api/auth/copilot/logout
    POST /api/auth/copilot/token   (install a token minted by the control plane)
    GET  /api/models
    GET/POST /api/keys, DELETE /api/keys/{key}

There is no usage-query endpoint: the hub keeps no usage store. Every completed
request is emitted to Azure Event Hub (see `hub.eventhub`) carrying upstream's
`copilot_usage` verbatim, and the control plane reports off Cosmos.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import anthropic_adapter as aa
from . import audit, eventhub, store
from . import copilot_client as cc
from . import image_client as ic
from .config import get_settings

log = logging.getLogger(__name__)

app = FastAPI(title="GitModel Hub", version="0.1.0")

# In-memory admin sessions: token -> expiry epoch. Cleared on restart.
_SESSIONS: dict[str, float] = {}
_SESSION_TTL = 12 * 3600  # 12h

# In-memory admin login throttle: client IP -> {"fails": int, "until": epoch}.
# Cleared on restart. Thresholds come from settings (env-configurable).
_LOGIN_ATTEMPTS: dict[str, dict[str, float]] = {}


@app.on_event("startup")
def _startup() -> None:
    # Create the (ephemeral) SQLite tables the runtime still needs — require_auth
    # lookups and the api_keys fallback. Usage is NOT among them: it goes to
    # Event Hub. We deliberately do NOT seed admin credentials: the management
    # portal + its login are removed, and all admin calls authenticate via the
    # injected HUB_ADMIN_TOKEN. So the DB holds no identity of any kind (no
    # admin/admin, no persisted keys); the real identities live in the control
    # plane's Postgres + Key Vault.
    store.init_db()


@app.on_event("shutdown")
async def _shutdown() -> None:
    # Usage events scheduled by a streaming response's `finally` are ordinary
    # background tasks; a rolling update must not race them. They feed BOTH
    # closers below, so they are awaited first.
    await drain_emits()
    # Flush whatever is still sitting in the Event Hub producer's buffer, so a
    # graceful stop (Container Apps rolling update, SIGTERM) loses no usage.
    await eventhub.aclose()
    # Same for audit uploads still in flight: finish them, then release the
    # storage connection.
    await audit.aclose()


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def _client_key_candidates(request: Request) -> list[str]:
    """Credentials the caller presented, most authoritative first.

    A request proxied by APIM carries two of them: the gateway overwrites
    `x-api-key` with this hub's own credential, while the caller's
    `Authorization` rides through untouched and holds whatever they configured
    — against a pooled backend that is never this hub's key. Preferring
    `x-api-key` and falling back to `Authorization` keeps direct callers, who
    send only the latter, working unchanged.
    """
    keys: list[str] = []
    xkey = (request.headers.get("x-api-key") or "").strip()
    if xkey:
        keys.append(xkey)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            keys.append(token)
    return keys


def _extract_client_key(request: Request) -> str | None:
    """The key a request is attributed to when no validation has run."""
    keys = _client_key_candidates(request)
    return keys[0] if keys else None


def _check_client_auth(request: Request) -> str | None:
    """Return the client key (for usage attribution); enforce auth if required."""
    keys = _client_key_candidates(request)
    s = get_settings()
    if not store.get_require_auth(s.require_auth):
        return keys[0] if keys else None
    # A deploy-time HUB_API_KEY (env, Key Vault-backed) is accepted alongside
    # portal-created keys. Since the hub is stateless (ephemeral SQLite), the
    # env key is the durable credential the control plane / APIM authenticate
    # with; portal-created keys (SQLite) remain valid as a fallback.
    for key in keys:
        env_ok = bool(s.hub_api_key) and secrets.compare_digest(key, s.hub_api_key)
        if env_ok or store.is_valid_api_key(key):
            return key
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _new_session() -> str:
    import time

    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + _SESSION_TTL
    return token


def _valid_session(token: str | None) -> bool:
    import time

    if not token:
        return False
    exp = _SESSIONS.get(token)
    if not exp:
        return False
    if exp < time.time():
        _SESSIONS.pop(token, None)
        return False
    return True


def _check_admin(x_admin_token: str | None) -> None:
    """Require a valid admin session token (or the env override token)."""
    env_token = get_settings().admin_token
    if env_token and (x_admin_token or "") == env_token:
        return
    if _valid_session(x_admin_token):
        return
    raise HTTPException(status_code=401, detail="Admin login required")


# --------------------------------------------------------------------------- #
# Admin login brute-force throttle (per client IP, in-memory)
# --------------------------------------------------------------------------- #
def _client_ip(request: Request) -> str:
    """Best-effort real client IP, honoring the proxy's X-Forwarded-For.

    Azure Container Apps (and most reverse proxies) put the original client
    address first in X-Forwarded-For; request.client.host is the proxy.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_check_locked(ip: str) -> None:
    """Raise 429 if this IP is currently locked out."""
    import time

    rec = _LOGIN_ATTEMPTS.get(ip)
    if rec and rec.get("until", 0) > time.time():
        wait = int(rec["until"] - time.time())
        raise HTTPException(
            status_code=429,
            detail=f"尝试次数过多，请 {wait // 60 + 1} 分钟后再试",
        )


def _login_fail(ip: str) -> None:
    """Record one failed attempt; lock the IP once the threshold is hit."""
    import time

    s = get_settings()
    if s.login_max_fails <= 0:  # throttling disabled
        return
    rec = _LOGIN_ATTEMPTS.get(ip) or {"fails": 0.0, "until": 0.0}
    rec["fails"] = rec.get("fails", 0) + 1
    if rec["fails"] >= s.login_max_fails:
        rec["until"] = time.time() + s.login_lock_seconds
        rec["fails"] = 0.0  # reset counter; re-counts after the lock expires
    _LOGIN_ATTEMPTS[ip] = rec


def _login_success(ip: str) -> None:
    """Clear an IP's failure record on a successful login."""
    _LOGIN_ATTEMPTS.pop(ip, None)


def _norm_usage(usage: dict[str, Any] | None, *, responses_shape: bool) -> tuple[int, int, int, int]:
    if not usage:
        return 0, 0, 0, 0
    if responses_shape:
        i = usage.get("input_tokens", 0)
        o = usage.get("output_tokens", 0)
        t = usage.get("total_tokens", i + o)
        # Responses API nests cache info under input_tokens_details.
        details = usage.get("input_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
    else:
        i = usage.get("prompt_tokens", 0)
        o = usage.get("completion_tokens", 0)
        t = usage.get("total_tokens", i + o)
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
    return i, o, t, cached


# --------------------------------------------------------------------------- #
# Usage attribution + emission
# --------------------------------------------------------------------------- #
def _key_fingerprint(key: str | None) -> str | None:
    """Stable, non-reversible identifier for an API key.

    The raw key must never leave the hub: usage events land in Event Hub, then
    in Capture blobs, then in Cosmos — three places a credential has no business
    being. A truncated SHA-256 still groups a caller's requests together, which
    is all attribution needs.
    """
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _extract_tenant(request: Request) -> dict[str, str | None]:
    """Who is being billed for this request.

    APIM's inbound policy stamps `x-tf-subscription` / `x-tf-api` /
    `x-tf-request-id` with `exists-action="override"`, so a client cannot forge
    them. Every APIM tenant shares one hub credential, which is why the header
    — not the API key — is the authoritative tenant id.

    Direct (non-APIM) callers have no such header; they fall back to the key
    fingerprint, and get a locally minted request id so the import side still
    has a unique document key.
    """
    h = request.headers
    sub = (h.get("x-tf-subscription") or "").strip()
    fp = _key_fingerprint(_extract_client_key(request))
    return {
        "subscription": sub or fp,
        "api_id": (h.get("x-tf-api") or "").strip() or None,
        "request_id": (h.get("x-tf-request-id") or "").strip() or uuid.uuid4().hex,
        "client_key_fp": fp,
        "via_apim": "1" if sub else "",
        # Raw-body archival opt-in, also stamped by APIM with override. Default
        # off, so a direct caller (no header) is never archived.
        "audit": "1" if audit.wants_audit(h) else "",
    }


def _extract_end_user(body: Any) -> str | None:
    """The end user *inside* a tenant, if the client bothered to say.

    Both vendors have a standard field for this — Anthropic `metadata.user_id`,
    OpenAI `user` — so no custom protocol is needed. Optional by nature: a
    client that omits it simply cannot be split below the subscription level.
    """
    if not isinstance(body, dict):
        return None
    meta = body.get("metadata")
    if isinstance(meta, dict):
        uid = meta.get("user_id")
        if isinstance(uid, str) and uid.strip():
            return uid.strip()
    user = body.get("user")
    if isinstance(user, str) and user.strip():
        return user.strip()
    return None


def _extract_copilot_usage(obj: Any) -> dict[str, Any] | None:
    """Pull upstream's `copilot_usage` out of a response object, verbatim.

    This object carries token counts, unit price (`cost_per_batch / batch_size`)
    and total price (`total_nano_aiu`) as billed by GitHub. The hub deliberately
    does NO arithmetic on it: it ships as-is and the control plane converts to
    USD at import time, so an upstream pricing-schema change is a re-import
    rather than a data-loss event.
    """
    if isinstance(obj, dict):
        cu = obj.get("copilot_usage")
        if isinstance(cu, dict):
            return cu
    return None


def _scan_sse_copilot_usage(text: str) -> dict[str, Any] | None:
    """Find `copilot_usage` anywhere in a full SSE stream.

    It must be a whole-stream scan, not a peek at the chunk carrying `usage`:
    on the OpenAI-compatible path the two end up on DIFFERENT chunks, because
    `_standardize_openai_usage_line()` splits `usage` off into its own
    spec-compliant chunk and leaves `copilot_usage` behind on the finish chunk.
    (Verified end-to-end through the gateway.) On the Anthropic path it rides
    `message_delta`; on Responses-shaped streams it is nested under `.response`.
    """
    found: dict[str, Any] | None = None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        cand = _extract_copilot_usage(obj)
        if cand is None and isinstance(obj, dict):
            cand = _extract_copilot_usage(obj.get("response"))
        if cand is not None:
            found = cand
    return found


def _usage_record(
    *, tenant, end_user, model, served, endpoint, usage3, streamed, estimated,
    ts, audit_blob, status=200, cached=0, usage=None, copilot_usage=None,
) -> dict[str, Any]:
    """Build the Event Hub payload. PURE — no I/O, no awaits.

    This dict is the billing contract with
    `app/services/usage_capture_import.py::event_to_document`. Splitting it out
    of `_emit_usage` makes that contract directly testable, and — the reason it
    exists — lets the streaming paths build the record synchronously so they can
    schedule the send from inside a `finally` (see `_spawn_emit`).
    """
    i, o, t = usage3
    return {
        "request_id": tenant["request_id"],
        "ts": ts.isoformat(),
        "subscription": tenant["subscription"],
        "api_id": tenant["api_id"],
        "client_key_fp": tenant["client_key_fp"],
        "via_apim": bool(tenant["via_apim"]),
        "end_user": end_user,
        "model": model,
        "served_model": served,
        "endpoint": endpoint,
        "streamed": streamed,
        "status": status,
        # Normalized counts, kept so the import side does not have to know
        # each vendor's usage shape. `estimated` flags the streaming case
        # where upstream omitted prompt_tokens and we guessed.
        "input_tokens": i,
        "output_tokens": o,
        "total_tokens": t,
        "cached_tokens": cached,
        "estimated": estimated,
        # Raw upstream payloads — the billing source of truth.
        "usage": usage,
        "copilot_usage": copilot_usage,
        # Pointer into the audit archive, or None when the tenant did not
        # opt in. A promise, not a receipt: see audit.submit.
        "audit_blob": audit_blob,
    }


def _archive(tenant, *, ts, end_user, model, endpoint, streamed, status,
             req_body, resp_body) -> str | None:
    """Trigger raw-body archival for tenants that opted in. Synchronous:
    `audit.submit` returns the blob path without waiting for the upload."""
    if not tenant.get("audit"):
        return None
    return audit.submit(
        request_id=tenant["request_id"],
        ts=ts,
        subscription=tenant["subscription"],
        api_id=tenant["api_id"],
        end_user=end_user,
        model=model,
        endpoint=endpoint,
        streamed=streamed,
        status=status,
        request_body=req_body,
        response_body=resp_body,
    )


# Usage sends scheduled from a streaming response's `finally`. They are ordinary
# background tasks, so a rolling update must await them — see `drain_emits`.
_PENDING_EMITS: set[asyncio.Task[None]] = set()
_MAX_PENDING_EMITS = 256


def _spawn_emit(*, req_body=None, resp_body=None, **kw) -> None:
    """Schedule one usage event WITHOUT awaiting. Never raises.

    Safe to call from an async generator's `finally`, which is the entire point.
    On client disconnect Starlette throws `GeneratorExit` at the `yield`; an
    `await` in the `finally` then raises "async generator ignored
    GeneratorExit", losing the billing event for exactly those requests that
    were interrupted — silently, with no counter. Everything here is
    synchronous: `audit.submit` already is, and `create_task` does not suspend.

    `StreamingResponse(background=...)` looks like the obvious alternative and
    is not: on the ASGI>=2.4 disconnect path Starlette raises `ClientDisconnect`
    and never awaits the background task.

    The record is built here rather than inside the task so a pending emit holds
    ~1 KB instead of pinning the whole SSE transcript in memory.
    """
    try:
        ts = datetime.now(UTC)  # stamped NOW, not whenever the task gets to run
        audit_blob = _archive(
            kw["tenant"], ts=ts, end_user=kw.get("end_user"), model=kw.get("model"),
            endpoint=kw.get("endpoint"), streamed=kw.get("streamed", False),
            status=kw.get("status", 200), req_body=req_body, resp_body=resp_body,
        )
        record = _usage_record(ts=ts, audit_blob=audit_blob, **kw)
        if len(_PENDING_EMITS) >= _MAX_PENDING_EMITS:
            eventhub.record_drop("saturated", f"{len(_PENDING_EMITS)} emits in flight")
            log.warning("usage emit saturated (%d in flight); dropping", len(_PENDING_EMITS))
            return
        task = asyncio.create_task(eventhub.emit(record))
        _PENDING_EMITS.add(task)
        task.add_done_callback(_PENDING_EMITS.discard)
    except Exception as exc:  # noqa: BLE001 — a raise here would propagate out of
        eventhub.record_drop("schedule", exc)  # the finally and mask the real error
        log.warning("could not schedule usage event: %s", exc)


async def drain_emits() -> None:
    """Await every scheduled emit. Called from shutdown; also the test seam."""
    if _PENDING_EMITS:
        await asyncio.gather(*list(_PENDING_EMITS), return_exceptions=True)


async def _emit_usage(
    *, tenant, end_user, model, served, endpoint, usage3, streamed, estimated,
    status=200, cached=0, usage=None, copilot_usage=None,
    req_body=None, resp_body=None,
) -> None:
    """Ship one completed request to Event Hub. Never raises (see `eventhub`).

    Used by the NON-streaming call sites, which can safely await. The streaming
    ones go through `_spawn_emit` instead.
    """
    ts = datetime.now(UTC)
    audit_blob = _archive(
        tenant, ts=ts, end_user=end_user, model=model, endpoint=endpoint,
        streamed=streamed, status=status, req_body=req_body, resp_body=resp_body,
    )
    await eventhub.emit(
        _usage_record(
            tenant=tenant, end_user=end_user, model=model, served=served,
            endpoint=endpoint, usage3=usage3, streamed=streamed,
            estimated=estimated, ts=ts, audit_blob=audit_blob, status=status,
            cached=cached, usage=usage, copilot_usage=copilot_usage,
        )
    )


def _sse_error(status: int, body: str) -> bytes:
    """One SSE `error` event, for an upstream failure discovered mid-stream.

    On the streaming path there is no status code left to fail with:
    `StreamingResponse` puts `200 OK` and its headers on the wire before the
    generator body runs, and the first call upstream happens inside that body.
    When upstream then answers 429, the only honest thing still available is an
    in-band event — which is also what the OpenAI API does.

    Without this the client observes a 200 with an empty stream and no way to
    distinguish "throttled, retry" from "the model produced nothing". During the
    dev-17 campaign that happened 58 times and every dashboard read them as
    successes.

    The upstream body is truncated: it is an error payload, not content, and it
    is going to a client that only needs to know what happened and whether to
    retry.
    """
    payload = {
        "error": {
            "message": f"upstream returned {status}",
            "type": "upstream_error",
            "code": status,
            "upstream": body[:500],
        }
    }
    return (
        f"data: {json.dumps(payload)}\n\n"
        "data: [DONE]\n\n"
    ).encode("utf-8")


def _standardize_openai_usage_line(line: str) -> str:
    """Rewrite a single OpenAI-stream SSE `data:` line to the OpenAI spec shape.

    The GitHub Copilot backend's OpenAI streaming chunks deviate from the spec
    in two ways that break APIM's LLM logging / emit-token-metric token capture:

      1. **No `object` field.** OpenAI streaming chunks carry
         `"object":"chat.completion.chunk"`; Copilot omits it entirely. APIM
         keys off this field to recognize a chunk as an OpenAI completion chunk
         and parse its `usage` — so without it, APIM records completion=0 for
         EVERY streaming call. (Verified on dev-a05: a real Azure OpenAI backend
         behind the SAME APIM logs streaming completion tokens exactly, because
         its chunks include `object`; the Copilot hub's do not.)
      2. **`usage` glued onto the `finish_reason` chunk** (choices non-empty),
         whereas the spec puts `usage` in a separate trailing `choices: []`
         chunk.

    This normalizes BOTH: it stamps `object: "chat.completion.chunk"` on the
    chunk, and — when `usage` rides a non-empty-choices chunk — splits it into a
    spec-compliant finish chunk + a separate `choices: []` usage chunk (both
    stamped with `object`). Chunks that are already fine just get the `object`
    stamp. Pure/string-only so it is unit-testable without a backend.
    """
    _OBJ = "chat.completion.chunk"
    if not line.startswith("data:"):
        return line
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return line
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return line
    if not isinstance(obj, dict):
        return line
    usage = obj.get("usage")
    choices = obj.get("choices")
    # Case A: usage on a non-empty-choices chunk (the non-standard Copilot
    # layout) — split into finish chunk + separate usage chunk, both stamped.
    if usage and isinstance(choices, list) and len(choices) > 0:
        finish_chunk = {k: v for k, v in obj.items() if k != "usage"}
        finish_chunk["object"] = _OBJ
        usage_chunk = {
            k: obj[k] for k in ("id", "created", "model", "system_fingerprint")
            if k in obj
        }
        usage_chunk["object"] = _OBJ
        usage_chunk["choices"] = []
        usage_chunk["usage"] = usage
        return (
            "data: " + json.dumps(finish_chunk, separators=(",", ":")) + "\n\n"
            + "data: " + json.dumps(usage_chunk, separators=(",", ":"))
        )
    # Case B: any other chunk — just ensure the `object` field is present (this
    # is what APIM needs to parse the stream). Untouched if already correct.
    if obj.get("object") == _OBJ:
        return line
    obj["object"] = _OBJ
    return "data: " + json.dumps(obj, separators=(",", ":"))


def _parse_sse_usage(text: str, *, responses_shape: bool) -> dict[str, Any] | None:
    usage: dict[str, Any] | None = None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if responses_shape:
            resp = obj.get("response") if isinstance(obj, dict) else None
            cand = (resp or {}).get("usage") if isinstance(resp, dict) else None
            cand = cand or obj.get("usage")
        else:
            cand = obj.get("usage")
        if cand:
            usage = cand
    return usage


def _parse_anthropic_sse_usage(text: str) -> dict[str, Any]:
    """Collect the final usage from a native Anthropic SSE stream, VERBATIM.

    Anthropic reports input_tokens on `message_start` and the final output_tokens
    on `message_delta`, so the object has to be merged across events rather than
    taken from the last one.

    Merges EVERY key, not a three-key allowlist. The allowlist silently dropped
    `cache_creation_input_tokens` — Anthropic's cache-WRITE count, billed at
    1.25x input, the single most expensive token type on Opus — along with the
    `cache_creation` 5m/1h split, `output_tokens_details` (thinking tokens),
    `inference_geo` and `speed`. Verified on dev-15: 78/78 non-streamed
    Anthropic rows carried those fields and 0/51 streamed ones did, so a
    streamed call could not satisfy
    `tests/test_usage_parse.py::test_anthropic_total_includes_cache_creation`.
    Costs were unaffected (money comes from `copilot_usage`), but the raw
    archive was lossy and those fields exist only in that one response.

    The three canonical counters are guaranteed present because the call site
    subscripts them; everything else rides along as upstream sent it.
    """
    merged: dict[str, Any] = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        # message_start carries usage under .message.usage; message_delta under .usage
        u = (obj.get("message") or {}).get("usage") or obj.get("usage")
        if isinstance(u, dict):
            for k, v in u.items():
                # A later 0/empty must not erase an earlier real value:
                # message_delta re-reports some fields as 0. This preserves the
                # old `if isinstance(v, int) and v` semantics while generalising
                # it to non-numeric fields.
                if k in merged and not v:
                    continue
                merged[k] = v
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        **merged,
    }


def _anthropic_has_image(req: dict[str, Any]) -> bool:
    """True if an Anthropic-shaped request carries an image block anywhere.

    Images may sit directly in a message's content list, nested inside a
    `tool_result` block's own content list, or in a structured `system` field,
    so this recurses one level and scans all three. Used to set the
    `Copilot-Vision-Request` header on passthrough, where the body is never
    converted to the OpenAI shape — miss an image here and vision silently
    fails for that request.
    """

    def _scan(blocks: Any) -> bool:
        if not isinstance(blocks, list):
            return False
        for part in blocks:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image":
                return True
            # tool_result carries its own nested content list.
            if part.get("type") == "tool_result" and _scan(part.get("content")):
                return True
        return False

    for msg in req.get("messages") or []:
        if isinstance(msg, dict) and _scan(msg.get("content")):
            return True
    return _scan(req.get("system"))


def _upstream_version_header(request: Request) -> dict[str, str]:
    """The `anthropic-version` to forward upstream.

    Copilot's native endpoint expects the header. Prefer the caller's value so a
    client pinned to a different contract version keeps working; fall back to
    the configured default otherwise.
    """
    return {
        "anthropic-version": (
            request.headers.get("anthropic-version")
            or get_settings().anthropic_version
        )
    }


# =========================================================================== #
# OpenAI-compatible endpoints
# =========================================================================== #
def _openai_models_payload(models: list[dict[str, Any]]) -> dict[str, Any]:
    data = []
    for m in models:
        data.append(
            {
                "id": m.get("id"),
                "object": "model",
                "created": 0,
                "owned_by": m.get("vendor", "github-copilot"),
            }
        )
    return {"object": "list", "data": data}


@app.get("/v1/models")
async def v1_models(request: Request) -> JSONResponse:
    _check_client_auth(request)
    try:
        models = await cc.list_models()
    except cc.NotAuthenticatedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    payload = _openai_models_payload(models)
    if ic.is_configured():
        payload["data"].append(
            {
                "id": ic.get_model(),
                "object": "model",
                "created": 0,
                "owned_by": "azure-openai",
            }
        )
    return JSONResponse(payload)


async def _passthrough(
    request: Request, path: str, *, responses_shape: bool
) -> Any:
    _check_client_auth(request)
    body = await request.json()
    tenant = _extract_tenant(request)
    end_user = _extract_end_user(body)
    model = body.get("model")
    stream = bool(body.get("stream"))
    endpoint = path.strip("/").replace("/", ".")

    if not cc.is_authenticated():
        raise HTTPException(status_code=503, detail="Hub not logged in to Copilot")

    vision_headers = (
        {"Copilot-Vision-Request": "true"}
        if aa.has_image_content(body)
        else None
    )

    if not stream:
        try:
            status, data = await cc.post_json(path, body, vision_headers)
        except cc.NotAuthenticatedError as e:
            raise HTTPException(status_code=503, detail=str(e))
        if status != 200:
            await _emit_usage(tenant=tenant, end_user=end_user, model=model,
                              served=None, endpoint=endpoint, usage3=(0, 0, 0),
                              streamed=False, estimated=False, status=status,
                              req_body=body, resp_body=data)
            return JSONResponse(data, status_code=status)
        served = data.get("model") if isinstance(data, dict) else None
        i, o, t, cached = _norm_usage(data.get("usage"), responses_shape=responses_shape)
        await _emit_usage(tenant=tenant, end_user=end_user, model=model,
                          served=served, endpoint=endpoint, usage3=(i, o, t),
                          streamed=False, estimated=False, cached=cached,
                          usage=data.get("usage"),
                          copilot_usage=_extract_copilot_usage(data),
                          req_body=body, resp_body=data)
        return JSONResponse(data)

    # Streaming passthrough. Ask the backend to report usage for chat.
    if not responses_shape:
        body.setdefault("stream_options", {})
        if isinstance(body["stream_options"], dict):
            body["stream_options"].setdefault("include_usage", True)
    est_input = aa.estimate_prompt_tokens(body)

    async def gen() -> AsyncIterator[bytes]:
        collected: list[str] = []
        # The status this call is RECORDED under. Not the status the client saw
        # — that went out as 200 before this generator started — but what
        # actually happened, which is what the billing ledger and the portal's
        # succeeded/failed split need.
        upstream_status = 200
        # SSE events are line-delimited but cc.stream yields raw BYTE chunks that
        # may split a line mid-way; buffer text and only rewrite COMPLETE events
        # (terminated by a blank line) so _standardize_openai_usage_line always
        # sees a whole `data:` line. The tail (partial event) is held over.
        buf = ""

        def _emit(event_text: str) -> bytes:
            # event_text is one SSE event ("data: {...}"), possibly needing the
            # usage split. Rejoin the (possibly two) data lines it produces.
            out_lines = [
                _standardize_openai_usage_line(ln) if ln.startswith("data:") else ln
                for ln in event_text.split("\n")
            ]
            return ("\n".join(out_lines)).encode("utf-8")

        try:
            async for chunk in cc.stream(path, body, vision_headers):
                text = chunk.decode("utf-8", "replace")
                collected.append(text)
                buf += text
                # Flush every complete event (delimited by "\n\n").
                while "\n\n" in buf:
                    event, buf = buf.split("\n\n", 1)
                    yield _emit(event) + b"\n\n"
            # NORMAL completion only. This tail flush used to live in the
            # `finally`, where a `yield` during GeneratorExit raises "async
            # generator ignored GeneratorExit" and kills the finally BEFORE the
            # usage emit runs — losing the billing event for every interrupted
            # request. Accounting is unaffected by the move: it reads
            # `collected`, which already holds these bytes.
            if buf:
                yield _emit(buf)
        except cc.UpstreamStatusError as exc:
            # Upstream refused AFTER we had already committed to 200. Yielding
            # is legal here (an ordinary exception, unlike GeneratorExit), so
            # the client gets a real signal instead of an empty stream. Not
            # re-raised: a clean SSE error reads better at the client than an
            # aborted connection, and the failure is recorded below either way.
            upstream_status = exc.status
            log.warning("upstream %s on streamed %s: %s",
                        exc.status, endpoint, exc.body[:200])
            yield _sse_error(exc.status, exc.body)
        finally:
            # Synchronous only — no await, no yield. See _spawn_emit.
            raw = "".join(collected)
            usage = _parse_sse_usage(raw, responses_shape=responses_shape)
            i, o, t, cached = _norm_usage(usage, responses_shape=responses_shape)
            # Estimate the prompt ONLY if the stream actually delivered
            # something. When upstream refused, nothing was consumed and
            # nothing should be billed — substituting `est_input` there
            # manufactures billable input for a call that returned no content,
            # which is what dev-17 recorded 58 times (status 200, in=10, out=0).
            # The non-streaming path has always written (0, 0, 0) on a non-200;
            # this makes streaming agree with it.
            got_content = bool(collected)
            input_estimated = got_content and not i
            if input_estimated:
                i = est_input
                t = i + o
            _spawn_emit(
                tenant=tenant, end_user=end_user, model=model, served=None,
                endpoint=endpoint, usage3=(i, o, t), streamed=True,
                # (0, 0, 0) after an upstream refusal is a measurement, not an
                # estimate — flagging it as estimated would hide a real failure
                # behind "we guessed".
                estimated=got_content and (usage is None or input_estimated),
                cached=cached, status=upstream_status,
                usage=usage, copilot_usage=_scan_sse_copilot_usage(raw),
                req_body=body, resp_body=raw,
            )

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def v1_chat_completions(request: Request) -> Any:
    return await _passthrough(request, "/chat/completions", responses_shape=False)


@app.post("/v1/responses")
async def v1_responses(request: Request) -> Any:
    return await _passthrough(request, "/responses", responses_shape=True)


# =========================================================================== #
# Image generation endpoints (Azure OpenAI gpt-image backend)
# =========================================================================== #
def _strip_image_bytes(data: Any) -> Any:
    """Replace base64 image payloads with a size marker for the audit archive.

    Deliberate exception to "archive the whole body". A gpt-image response is
    megabytes of base64 pixels per image; keeping them would multiply the audit
    store's size and cost by two orders of magnitude while adding nothing an
    audit asks for. Everything that carries meaning — the revised prompt, the
    content-filter verdict, the model, usage — is JSON around the pixels and is
    kept verbatim. The prompt itself (the request body) is archived in full.
    """
    if not isinstance(data, dict):
        return data
    items = data.get("data")
    if not isinstance(items, list):
        return data
    out = dict(data)
    out["data"] = [
        {
            **{k: v for k, v in item.items() if k != "b64_json"},
            "b64_json": f"[omitted {len(item['b64_json'])} b64 chars]",
        }
        if isinstance(item, dict) and isinstance(item.get("b64_json"), str)
        else item
        for item in items
    ]
    return out


async def _emit_image_usage(
    tenant, end_user, model, endpoint, data, status, req_body=None
) -> None:
    """Emit a usage event for an image request, reusing the Responses-shaped
    usage parser (Azure image ``usage`` matches that shape).

    The image backend is Azure OpenAI, not Copilot, so there is no
    `copilot_usage` here — the import side sees `copilot_usage: null` and must
    fall back to the local price table for these rows.
    """
    if status == 200 and isinstance(data, dict):
        i, o, t, cached = _norm_usage(data.get("usage"), responses_shape=True)
        served = data.get("model")
        usage = data.get("usage")
    else:
        i, o, t, cached, served, usage = 0, 0, 0, 0, None, None
    await _emit_usage(tenant=tenant, end_user=end_user, model=model,
                      served=served, endpoint=endpoint, usage3=(i, o, t),
                      streamed=False, estimated=False, status=status,
                      cached=cached, usage=usage,
                      req_body=req_body, resp_body=_strip_image_bytes(data))


@app.post("/v1/images/generations")
async def v1_images_generations(request: Request) -> Any:
    _check_client_auth(request)
    body = await request.json()
    tenant = _extract_tenant(request)
    if not ic.is_configured():
        raise HTTPException(
            status_code=503, detail="Image backend not configured"
        )
    model = body.get("model") or ic.get_model()
    status, data = await ic.generate(body)
    await _emit_image_usage(
        tenant, _extract_end_user(body), model, "images.generations", data, status,
        req_body=body,
    )
    return JSONResponse(data, status_code=status)


@app.post("/v1/images/edits")
async def v1_images_edits(request: Request) -> Any:
    _check_client_auth(request)
    tenant = _extract_tenant(request)
    if not ic.is_configured():
        raise HTTPException(
            status_code=503, detail="Image backend not configured"
        )
    form = await request.form()
    data: dict[str, Any] = {}
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for field, value in form.multi_items():
        if hasattr(value, "read"):  # an UploadFile
            content = await value.read()
            files.append(
                (
                    field,
                    (
                        value.filename or field,
                        content,
                        value.content_type or "application/octet-stream",
                    ),
                )
            )
        else:
            data[field] = value
    model = data.get("model") or ic.get_model()
    status, resp = await ic.edit(data, files)
    await _emit_image_usage(
        tenant, _extract_end_user(data), model, "images.edits", resp, status,
        # Form fields (prompt, size, ...) verbatim; the uploaded source images
        # are recorded by name and size only, for the same reason the generated
        # ones are — see _strip_image_bytes.
        req_body={
            **data,
            "_files": [
                {"field": f, "filename": meta[0], "bytes": len(meta[1])}
                for f, meta in files
            ],
        },
    )
    return JSONResponse(resp, status_code=status)


# =========================================================================== #
# Anthropic-compatible endpoints
# =========================================================================== #
@app.post("/v1/messages/count_tokens")
async def v1_messages_count_tokens(request: Request) -> Any:
    """Anthropic token-counting endpoint — passed through to Copilot.

    Claude Code calls this to size the context before a turn. It is optional:
    with no route here the client falls back to estimating locally, which is how
    this gateway ran until now and worked fine. Serving it replaces that
    estimate with the upstream's own model-specific count.

    Declared before /v1/messages purely for readability — Starlette matches on
    the full path, so the more specific route does not need to come first.

    No usage row is recorded: counting is free and billed nothing upstream, so
    logging it would inflate the request count with rows that have no tokens.
    """
    _check_client_auth(request)
    body = await request.json()
    if not cc.is_authenticated():
        raise HTTPException(status_code=503, detail="Hub not logged in to Copilot")

    mapped = aa.map_model(body.get("model"))
    if mapped != body.get("model"):
        body = {**body, "model": mapped}
    body, _ = aa.strip_unsupported(body)
    try:
        status, data = await cc.post_json(
            "/v1/messages/count_tokens", body, _upstream_version_header(request)
        )
    except cc.NotAuthenticatedError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except RuntimeError as e:
        # A failed token exchange raises rather than returning a status, and an
        # unhandled one here becomes an opaque 500. Surface the upstream reason
        # so a client sees "Bad credentials" instead of Internal Server Error.
        raise HTTPException(status_code=502, detail=str(e)) from e
    return JSONResponse(data, status_code=status)


@app.post("/v1/messages")
async def v1_messages(request: Request) -> Any:
    """Anthropic Messages API — passed THROUGH to Copilot's native /v1/messages.

    Copilot exposes a native Anthropic endpoint, so we forward the request as-is
    (no OpenAI<->Anthropic conversion). This is why usage is exact: Copilot's
    native response reports input_tokens on message_start and cache/thinking
    tokens directly, which the old conversion path dropped (streaming input_tokens
    used to be 0). Pass-through also means tool_use, image blocks, and the full
    Anthropic SSE event shape are handled by Copilot, not re-implemented here.
    """
    _check_client_auth(request)
    req = await request.json()
    # Passthrough forwards the body verbatim, so anything the Copilot backend
    # does not recognise fails the whole request. Drop those fields first.
    req, dropped = aa.strip_unsupported(req)
    if dropped:
        log.info("dropped unsupported fields for upstream: %s", ", ".join(dropped))
    tenant = _extract_tenant(request)
    end_user = _extract_end_user(req)
    model = req.get("model")
    stream = bool(req.get("stream"))

    if not cc.is_authenticated():
        raise HTTPException(status_code=503, detail="Hub not logged in to Copilot")

    # Native Anthropic requests carry `image` content blocks (not OpenAI image_url).
    vision_headers = (
        {"Copilot-Vision-Request": "true"} if _anthropic_has_image(req) else None
    )
    # Copilot's native endpoint expects the Anthropic version header.
    headers = {**_upstream_version_header(request), **(vision_headers or {})}

    if not stream:
        req.pop("stream", None)
        try:
            status, data = await cc.post_json("/v1/messages", req, headers)
        except cc.NotAuthenticatedError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        if status != 200:
            await _emit_usage(tenant=tenant, end_user=end_user, model=model,
                              served=None, endpoint="messages", usage3=(0, 0, 0),
                              streamed=False, estimated=False, status=status,
                              req_body=req, resp_body=data)
            return JSONResponse(data, status_code=status)
        u = data.get("usage") or {}
        i = int(u.get("input_tokens", 0) or 0)
        o = int(u.get("output_tokens", 0) or 0)
        await _emit_usage(tenant=tenant, end_user=end_user, model=model,
                          served=data.get("model"), endpoint="messages",
                          usage3=(i, o, i + o), streamed=False, estimated=False,
                          cached=int(u.get("cache_read_input_tokens", 0) or 0),
                          usage=data.get("usage"),
                          copilot_usage=_extract_copilot_usage(data),
                          req_body=req, resp_body=data)
        return JSONResponse(data)

    # Streaming: forward Copilot's native Anthropic SSE unchanged; sniff usage
    # off the stream (input_tokens is real, on message_start) for accounting.
    async def gen() -> AsyncIterator[bytes]:
        collected: list[str] = []
        upstream_status = 200
        try:
            async for chunk in cc.stream("/v1/messages", req, headers):
                collected.append(chunk.decode("utf-8", "replace"))
                yield chunk
        except cc.UpstreamStatusError as exc:
            # Same shape as the OpenAI generator: the 200 is already sent, so
            # the refusal can only be delivered in-band, and it must be RECORDED
            # as the upstream status rather than as a success.
            upstream_status = exc.status
            log.warning("upstream %s on streamed messages: %s",
                        exc.status, exc.body[:200])
            yield _sse_error(exc.status, exc.body)
        finally:
            # Synchronous only — no await, no yield. See _spawn_emit: awaiting
            # here loses the event whenever the client disconnects mid-stream.
            raw = "".join(collected)
            u = _parse_anthropic_sse_usage(raw)
            i, o = u["input_tokens"], u["output_tokens"]
            _spawn_emit(
                tenant=tenant, end_user=end_user, model=model, served=None,
                endpoint="messages", usage3=(i, o, i + o), streamed=True,
                # Nothing delivered means nothing measured, not something
                # guessed — see the OpenAI generator for why this matters.
                estimated=bool(collected) and not i,
                cached=u["cache_read_input_tokens"], status=upstream_status,
                usage=u, copilot_usage=_scan_sse_copilot_usage(raw),
                req_body=req, resp_body=raw,
            )

    return StreamingResponse(gen(), media_type="text/event-stream")


# =========================================================================== #
# Management portal API
# =========================================================================== #
# Human login endpoints (/api/login, /api/logout, /api/me, /api/admin/password)
# are REMOVED: the portal is gone and there is no admin/admin identity. The
# remaining /api/* endpoints below are machine-only and authenticate via the
# injected HUB_ADMIN_TOKEN (see _check_admin).
@app.get("/api/settings")
async def api_get_settings(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    s = get_settings()
    img = store.get_image_config()
    return {
        "require_auth": store.get_require_auth(s.require_auth),
        "image": {
            "endpoint": img.get("endpoint", ""),
            "model": img.get("model", "") or ic.DEFAULT_IMAGE_MODEL,
            "configured": bool(img.get("endpoint") and img.get("api_key")),
        },
    }


@app.post("/api/settings")
async def api_set_settings(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    body = await request.json()
    if "require_auth" in body:
        store.set_require_auth(bool(body["require_auth"]))
    if "image" in body and isinstance(body["image"], dict):
        img = body["image"]
        store.set_image_config(
            img.get("endpoint"), img.get("api_key"), img.get("model")
        )
    s = get_settings()
    img = store.get_image_config()
    return {
        "require_auth": store.get_require_auth(s.require_auth),
        "image": {
            "endpoint": img.get("endpoint", ""),
            "model": img.get("model", "") or ic.DEFAULT_IMAGE_MODEL,
            "configured": bool(img.get("endpoint") and img.get("api_key")),
        },
    }


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "logged_in": cc.is_authenticated(),
        "require_auth": store.get_require_auth(get_settings().require_auth),
        # Failed HAND-OFFS since process start. An upper bound on loss, not the
        # loss itself: one event that fails three times counts 3. Unchanged key
        # and meaning so existing alerts keep working.
        "usage_events_dropped": eventhub.dropped_count(),
        # Events given up on for good, counted once each. THIS is the
        # billing-data-is-gone number and the one to alert on.
        "usage_events_lost": eventhub.lost_count(),
        # Audit payloads that never reached the archive. A usage record can
        # carry an `audit_blob` pointer to a blob that failed to upload, so a
        # non-zero value here means some pointers dangle.
        "audit_payloads_dropped": audit.dropped_count(),
        # Breakdown by cause + the last few failure reasons, so the NEXT
        # incident says which path broke without needing log collection (the
        # hub's Container App Environment has no log destination configured).
        # Exception CLASS NAMES only — this route is unauthenticated.
        "usage_events": eventhub.stats(),
    }


@app.post("/api/auth/device/start")
async def api_device_start(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    return await cc.device_flow_start()


@app.post("/api/auth/device/poll")
async def api_device_poll(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    body = await request.json()
    device_code = body.get("device_code")
    if not device_code:
        raise HTTPException(status_code=400, detail="device_code required")
    return await cc.device_flow_poll(device_code)


@app.post("/api/auth/copilot/logout")
async def api_copilot_logout(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    cc.logout()
    return {"ok": True}


@app.post("/api/auth/copilot/token")
async def api_copilot_install_token(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Any:
    """Install an OAuth token obtained elsewhere, without a redeploy.

    The control plane runs the device flow (it owns the Key Vault copy of the
    token and the account record), so it needs a way to hand the result to a
    live hub. Without this the only way to replace an expired token is a full
    terraform redeploy — minutes of rollout for a value that takes milliseconds
    to swap, and a new revision on every 8-hour token expiry.

    400 means the token itself was rejected by GitHub; the hub keeps whatever it
    had. 502 means we could not reach GitHub to find out, which is the caller's
    cue to retry rather than to re-run the device flow.
    """
    _check_admin(x_admin_token)
    body = await request.json()
    token = (body or {}).get("access_token")
    if not token:
        raise HTTPException(status_code=400, detail="access_token required")
    try:
        return await cc.install_oauth_token(token)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"could not reach GitHub: {exc}"
        ) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/models")
async def api_models(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    try:
        models = await cc.list_models()
    except cc.NotAuthenticatedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    chat_models = [
        {
            "id": m.get("id"),
            "vendor": m.get("vendor"),
            "type": (m.get("capabilities") or {}).get("type"),
        }
        for m in models
    ]
    if ic.is_configured():
        chat_models.append(
            {"id": ic.get_model(), "vendor": "azure-openai", "type": "image"}
        )
    return {"data": chat_models}


@app.get("/api/keys")
async def api_keys_list(x_admin_token: str | None = Header(default=None)) -> Any:
    _check_admin(x_admin_token)
    return {"data": store.list_api_keys()}


@app.post("/api/keys")
async def api_keys_create(
    request: Request, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    body = await request.json()
    name = (body.get("name") or "unnamed").strip()
    return store.create_api_key(name)


@app.delete("/api/keys/{key}")
async def api_keys_revoke(
    key: str, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    store.revoke_api_key(key)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Portal (static) — REMOVED
# --------------------------------------------------------------------------- #
# The human-facing management portal (the "/" page and its admin login) is
# intentionally gone: the hub is a headless backend now. All administration
# happens through the TokenFoundry control plane, which authenticates to the
# hub's /api/* endpoints with the injected HUB_ADMIN_TOKEN. Only the machine
# surfaces remain: /v1/* (service calls, HUB_API_KEY auth) and /api/status
# (control-plane health check).
