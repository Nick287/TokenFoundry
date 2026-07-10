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
    POST /api/auth/logout
    GET  /api/models
    GET  /api/usage
    GET  /api/usage/recent
    GET/POST /api/keys, DELETE /api/keys/{key}
"""
from __future__ import annotations

import json
import secrets
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import anthropic_adapter as aa
from . import copilot_client as cc
from . import image_client as ic
from . import store
from .config import get_settings

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
    # lookups, usage records, and the api_keys fallback. We deliberately do NOT
    # seed admin credentials: the management portal + its login are removed, and
    # all admin calls authenticate via the injected HUB_ADMIN_TOKEN. So the DB
    # holds no identity of any kind (no admin/admin, no persisted keys); the real
    # identities live in the control plane's Postgres + Key Vault.
    store.init_db()


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def _extract_client_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    xkey = request.headers.get("x-api-key")
    if xkey:
        return xkey.strip()
    return None


def _check_client_auth(request: Request) -> str | None:
    """Return the client key (for usage attribution); enforce auth if required."""
    key = _extract_client_key(request)
    s = get_settings()
    if store.get_require_auth(s.require_auth):
        # A deploy-time HUB_API_KEY (env, Key Vault-backed) is accepted alongside
        # portal-created keys. Since the hub is stateless (ephemeral SQLite), the
        # env key is the durable credential the control plane / APIM authenticate
        # with; portal-created keys (SQLite) remain valid as a fallback.
        env_ok = bool(s.hub_api_key) and bool(key) and secrets.compare_digest(
            key or "", s.hub_api_key
        )
        if not env_ok and (not key or not store.is_valid_api_key(key)):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key


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


def _record(
    *, client_key, model, served, endpoint, usage3, streamed, estimated,
    status=200, cached=0,
) -> None:
    i, o, t = usage3
    store.record_usage(
        api_key=client_key,
        model=model,
        served_model=served,
        endpoint=endpoint,
        input_tokens=i,
        output_tokens=o,
        total_tokens=t,
        streamed=streamed,
        estimated=estimated,
        status=status,
        cached_tokens=cached,
    )


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
    """Collect the final usage from a native Anthropic SSE stream.

    Anthropic reports input_tokens on `message_start` and the final output_tokens
    on `message_delta`, so we merge across events. Unlike the old OpenAI->Anthropic
    conversion, this reads Copilot's NATIVE Anthropic usage — input_tokens is a
    real value, not 0/estimate. Returns {input_tokens, output_tokens,
    cache_read_input_tokens} (zeros if absent)."""
    merged: dict[str, int] = {}
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
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                v = u.get(k)
                if isinstance(v, int) and v:
                    merged[k] = v
    return {
        "input_tokens": merged.get("input_tokens", 0),
        "output_tokens": merged.get("output_tokens", 0),
        "cache_read_input_tokens": merged.get("cache_read_input_tokens", 0),
    }


def _anthropic_has_image(req: dict[str, Any]) -> bool:
    """True if an Anthropic-shaped request carries an image content block."""
    for msg in req.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image":
                    return True
    return False


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
    client_key = _check_client_auth(request)
    body = await request.json()
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
            _record(client_key=client_key, model=model, served=None,
                    endpoint=endpoint, usage3=(0, 0, 0), streamed=False,
                    estimated=False, status=status)
            return JSONResponse(data, status_code=status)
        served = data.get("model") if isinstance(data, dict) else None
        i, o, t, cached = _norm_usage(data.get("usage"), responses_shape=responses_shape)
        _record(client_key=client_key, model=model, served=served,
                endpoint=endpoint, usage3=(i, o, t), streamed=False,
                estimated=False, cached=cached)
        return JSONResponse(data)

    # Streaming passthrough. Ask the backend to report usage for chat.
    if not responses_shape:
        body.setdefault("stream_options", {})
        if isinstance(body["stream_options"], dict):
            body["stream_options"].setdefault("include_usage", True)
    est_input = aa.estimate_prompt_tokens(body)

    async def gen() -> AsyncIterator[bytes]:
        collected: list[str] = []
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
        finally:
            if buf:
                yield _emit(buf)
            usage = _parse_sse_usage("".join(collected), responses_shape=responses_shape)
            i, o, t, cached = _norm_usage(usage, responses_shape=responses_shape)
            # Backend often omits prompt_tokens on streams — estimate input.
            input_estimated = not i
            if input_estimated:
                i = est_input
                t = i + o
            _record(client_key=client_key, model=model, served=None,
                    endpoint=endpoint, usage3=(i, o, t), streamed=True,
                    estimated=usage is None or input_estimated, cached=cached)

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
def _record_image(client_key, model, endpoint, data, status) -> None:
    """Record a usage row for an image request, reusing the Responses-shaped
    usage parser (Azure image ``usage`` matches that shape)."""
    if status == 200 and isinstance(data, dict):
        i, o, t, cached = _norm_usage(data.get("usage"), responses_shape=True)
        served = data.get("model")
    else:
        i, o, t, cached, served = 0, 0, 0, 0, None
    _record(client_key=client_key, model=model, served=served,
            endpoint=endpoint, usage3=(i, o, t), streamed=False,
            estimated=False, status=status, cached=cached)


@app.post("/v1/images/generations")
async def v1_images_generations(request: Request) -> Any:
    client_key = _check_client_auth(request)
    body = await request.json()
    if not ic.is_configured():
        raise HTTPException(
            status_code=503, detail="Image backend not configured"
        )
    model = body.get("model") or ic.get_model()
    status, data = await ic.generate(body)
    _record_image(client_key, model, "images.generations", data, status)
    return JSONResponse(data, status_code=status)


@app.post("/v1/images/edits")
async def v1_images_edits(request: Request) -> Any:
    client_key = _check_client_auth(request)
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
    _record_image(client_key, model, "images.edits", resp, status)
    return JSONResponse(resp, status_code=status)


# =========================================================================== #
# Anthropic-compatible endpoint
# =========================================================================== #
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
    client_key = _check_client_auth(request)
    req = await request.json()
    model = req.get("model")
    stream = bool(req.get("stream"))

    if not cc.is_authenticated():
        raise HTTPException(status_code=503, detail="Hub not logged in to Copilot")

    # Native Anthropic requests carry `image` content blocks (not OpenAI image_url).
    vision_headers = (
        {"Copilot-Vision-Request": "true"} if _anthropic_has_image(req) else None
    )
    # Copilot's native endpoint expects the Anthropic version header.
    headers = {"anthropic-version": "2023-06-01", **(vision_headers or {})}

    if not stream:
        req.pop("stream", None)
        try:
            status, data = await cc.post_json("/v1/messages", req, headers)
        except cc.NotAuthenticatedError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        if status != 200:
            _record(client_key=client_key, model=model, served=None,
                    endpoint="messages", usage3=(0, 0, 0), streamed=False,
                    estimated=False, status=status)
            return JSONResponse(data, status_code=status)
        u = data.get("usage") or {}
        i = int(u.get("input_tokens", 0) or 0)
        o = int(u.get("output_tokens", 0) or 0)
        _record(client_key=client_key, model=model, served=data.get("model"),
                endpoint="messages", usage3=(i, o, i + o),
                streamed=False, estimated=False,
                cached=int(u.get("cache_read_input_tokens", 0) or 0))
        return JSONResponse(data)

    # Streaming: forward Copilot's native Anthropic SSE unchanged; sniff usage
    # off the stream (input_tokens is real, on message_start) for accounting.
    async def gen() -> AsyncIterator[bytes]:
        collected: list[str] = []
        try:
            async for chunk in cc.stream("/v1/messages", req, headers):
                collected.append(chunk.decode("utf-8", "replace"))
                yield chunk
        finally:
            u = _parse_anthropic_sse_usage("".join(collected))
            i, o = u["input_tokens"], u["output_tokens"]
            _record(client_key=client_key, model=model, served=None,
                    endpoint="messages", usage3=(i, o, i + o),
                    streamed=True, estimated=not i,
                    cached=u["cache_read_input_tokens"])

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
        "pricing": store.get_pricing(),
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
    if "pricing" in body and isinstance(body["pricing"], dict):
        store.set_pricing(body["pricing"])
    if "image" in body and isinstance(body["image"], dict):
        img = body["image"]
        store.set_image_config(
            img.get("endpoint"), img.get("api_key"), img.get("model")
        )
    s = get_settings()
    img = store.get_image_config()
    return {
        "require_auth": store.get_require_auth(s.require_auth),
        "pricing": store.get_pricing(),
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


@app.get("/api/usage")
async def api_usage(
    since: float | None = None,
    until: float | None = None,
    x_admin_token: str | None = Header(default=None),
) -> Any:
    _check_admin(x_admin_token)
    return store.usage_summary_with_cost(since, until)


@app.get("/api/usage/recent")
async def api_usage_recent(
    limit: int = 50, x_admin_token: str | None = Header(default=None)
) -> Any:
    _check_admin(x_admin_token)
    return {"data": store.recent_usage(limit)}


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
