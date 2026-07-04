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
        try:
            async for chunk in cc.stream(path, body, vision_headers):
                collected.append(chunk.decode("utf-8", "replace"))
                yield chunk
        finally:
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
    client_key = _check_client_auth(request)
    req = await request.json()
    model = req.get("model")
    stream = bool(req.get("stream"))

    if not cc.is_authenticated():
        raise HTTPException(status_code=503, detail="Hub not logged in to Copilot")

    openai_payload = aa.anthropic_to_openai(req)
    vision_headers = (
        {"Copilot-Vision-Request": "true"}
        if aa.has_image_content(openai_payload)
        else None
    )

    if not stream:
        openai_payload.pop("stream", None)
        try:
            status, data = await cc.post_json(
                "/chat/completions", openai_payload, vision_headers
            )
        except cc.NotAuthenticatedError as e:
            raise HTTPException(status_code=503, detail=str(e))
        if status != 200:
            _record(client_key=client_key, model=model, served=None,
                    endpoint="messages", usage3=(0, 0, 0), streamed=False,
                    estimated=False, status=status)
            return JSONResponse(
                {"type": "error", "error": {"type": "api_error",
                 "message": json.dumps(data)}},
                status_code=status,
            )
        anth = aa.openai_to_anthropic_response(data, model)
        u = anth["usage"]
        _record(client_key=client_key, model=model, served=data.get("model"),
                endpoint="messages",
                usage3=(u["input_tokens"], u["output_tokens"],
                        u["input_tokens"] + u["output_tokens"]),
                streamed=False, estimated=False,
                cached=u.get("cache_read_input_tokens", 0))
        return JSONResponse(anth)

    # Streaming: translate OpenAI SSE -> Anthropic SSE.
    openai_payload["stream"] = True
    openai_payload.setdefault("stream_options", {"include_usage": True})
    est_input = aa.estimate_prompt_tokens(openai_payload)

    async def gen() -> AsyncIterator[bytes]:
        final_usage: dict[str, Any] | None = None
        try:
            source = cc.stream("/chat/completions", openai_payload, vision_headers)
            async for sse_bytes, usage in aa.stream_openai_to_anthropic(source, model):
                if usage is not None:
                    final_usage = usage
                yield sse_bytes
        finally:
            i = (final_usage or {}).get("input_tokens", 0)
            o = (final_usage or {}).get("output_tokens", 0)
            cached = (final_usage or {}).get("cache_read_input_tokens", 0)
            # Copilot's streaming SSE usually omits prompt_tokens — fall back
            # to a local estimate so input usage isn't silently lost.
            input_estimated = not i
            if input_estimated:
                i = est_input
            _record(client_key=client_key, model=model, served=None,
                    endpoint="messages", usage3=(i, o, i + o),
                    streamed=True, estimated=final_usage is None or input_estimated,
                    cached=cached)

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
