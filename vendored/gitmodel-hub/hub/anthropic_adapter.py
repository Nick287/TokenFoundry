"""Anthropic Messages API <-> OpenAI Chat Completions translation.

⚠️ NOTE (2026-07): Copilot DOES expose a native Anthropic ``/v1/messages``
endpoint, so ``server.py`` now PASSES ``/v1/messages`` THROUGH natively instead
of converting. The bidirectional converters below (``anthropic_to_openai``,
``openai_to_anthropic_response``, ``stream_openai_to_anthropic``) are therefore
no longer on the request path — kept as a fallback in case the native endpoint
regresses. Native pass-through is exact (streaming input_tokens is a real value,
plus cache/thinking tokens) whereas the OpenAI conversion dropped streaming
input_tokens. ``has_image_content`` and ``estimate_prompt_tokens`` are STILL used
by the OpenAI passthrough path.

The converters translate the ``/v1/messages`` shape to/from ``/chat/completions``:

* system prompt, multimodal (image) content,
* tool definitions, ``tool_use`` / ``tool_result`` round-trips,
* streaming (Anthropic SSE event sequence synthesised from OpenAI deltas).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator


# --------------------------------------------------------------------------- #
# Model name mapping
# --------------------------------------------------------------------------- #
# Anthropic-native clients (Claude Code, the Anthropic SDK, etc.) often send
# the official public model IDs, but the Copilot backend only knows its own
# slugs. Map the well-known official names onto the Copilot equivalents so a
# client configured against api.anthropic.com works unchanged. Unknown names
# are passed through verbatim (so Copilot-native slugs still work).
MODEL_ALIASES: dict[str, str] = {
    # Claude 3.5 / 3.7 Sonnet
    "claude-3-5-sonnet-20240620": "claude-3.5-sonnet",
    "claude-3-5-sonnet-20241022": "claude-3.5-sonnet",
    "claude-3-5-sonnet-latest": "claude-3.5-sonnet",
    "claude-3-7-sonnet-20250219": "claude-3.7-sonnet",
    "claude-3-7-sonnet-latest": "claude-3.7-sonnet",
    # Claude 4.x Sonnet
    "claude-sonnet-4-20250514": "claude-sonnet-4",
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-sonnet-4-5-20250929": "claude-sonnet-4.5",
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    # Claude 4.x Opus
    "claude-opus-4-20250514": "claude-opus-4",
    "claude-opus-4-1-20250805": "claude-opus-4.1",
    "claude-opus-4-7": "claude-opus-4.7",
    # Claude 3.5 Haiku
    "claude-3-5-haiku-20241022": "claude-3.5-haiku",
    "claude-3-5-haiku-latest": "claude-3.5-haiku",
}


def map_model(name: str | None) -> str | None:
    """Translate an official Anthropic model ID to its Copilot slug.

    Matching is exact first, then a couple of tolerant fallbacks (strip a
    trailing date suffix). Unknown names pass through unchanged so that
    Copilot-native slugs keep working.
    """
    if not name:
        return name
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    # Tolerant fallback: "claude-...-YYYYMMDD" -> try without the date.
    if "-20" in name:
        base = name.rsplit("-20", 1)[0]
        if base in MODEL_ALIASES:
            return MODEL_ALIASES[base]
    return name


def has_image_content(openai_payload: dict[str, Any]) -> bool:
    """True if any message in an OpenAI-shaped payload carries an image part."""
    for msg in openai_payload.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """Very rough fallback token estimate (~4 chars/token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_prompt_tokens(openai_payload: dict[str, Any]) -> int:
    """Rough input-token estimate for an OpenAI-shaped request.

    Used as a fallback when the backend does not report ``prompt_tokens``
    (common with streaming Copilot responses). Walks every message's content
    — plain strings, text parts, and tool-call arguments — plus tool schemas.
    """
    total = 0
    for msg in openai_payload.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += estimate_tokens(part.get("text", ""))
                elif part.get("type") == "image_url":
                    # Images cost real tokens; charge a flat rough amount
                    # rather than measuring the (possibly huge) data URL.
                    total += 600
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            total += estimate_tokens(fn.get("name", ""))
            total += estimate_tokens(fn.get("arguments", "") or "")
    # Tool definitions are part of the prompt too.
    for tool in openai_payload.get("tools") or []:
        fn = tool.get("function", {}) or {}
        total += estimate_tokens(fn.get("name", ""))
        total += estimate_tokens(fn.get("description", ""))
        try:
            total += estimate_tokens(json.dumps(fn.get("parameters", {})))
        except (TypeError, ValueError):
            pass
    return total


def _content_to_openai(content: Any) -> Any:
    """Convert Anthropic message content to OpenAI content (str or parts)."""
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type')};base64,{src.get('data')}"
            else:
                url = src.get("url", "")
            parts.append({"type": "image_url", "image_url": {"url": url}})
    # Collapse a single text part back to a plain string.
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def _split_tool_result(
    tr: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split one Anthropic ``tool_result`` into (tool_message, image_parts).

    The OpenAI ``tool`` role only accepts text, so image blocks returned by a
    tool (e.g. Claude Code's ``Read`` on an image) cannot ride the tool message
    — they must be forwarded on a following ``user`` message. This returns the
    text-only ``tool`` message (keyed by ``tool_use_id``) plus any images as
    OpenAI ``image_url`` parts for the caller to attach downstream. Dropping
    them here is exactly the bug this guards against.
    """
    tr_content = tr.get("content")
    text = ""
    image_parts: list[dict[str, Any]] = []

    if isinstance(tr_content, list):
        text_chunks: list[str] = []
        for c in tr_content:
            ctype = c.get("type")
            if ctype == "text":
                text_chunks.append(c.get("text", ""))
            elif ctype == "image":
                src = c.get("source", {})
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type')};base64,{src.get('data')}"
                else:
                    url = src.get("url", "")
                if url:
                    image_parts.append(
                        {"type": "image_url", "image_url": {"url": url}}
                    )
        text = "".join(text_chunks)
    elif isinstance(tr_content, str):
        text = tr_content
    else:
        text = json.dumps(tr_content)

    # A tool message must not be empty when the result was image-only.
    if not text and image_parts:
        text = "[image returned by tool; see the following message]"

    tool_msg = {
        "role": "tool",
        "tool_call_id": tr.get("tool_use_id"),
        "content": text,
    }
    return tool_msg, image_parts


def anthropic_to_openai(req: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic /v1/messages request to OpenAI chat payload."""
    messages: list[dict[str, Any]] = []

    system = req.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "".join(
            b.get("text", "") for b in system if b.get("type") == "text"
        )
        if text:
            messages.append({"role": "system", "content": text})

    for msg in req.get("messages", []):
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, list):
            tool_results = [b for b in content if b.get("type") == "tool_result"]
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            other = [
                b for b in content if b.get("type") not in ("tool_result", "tool_use")
            ]

            # tool_result blocks become standalone OpenAI 'tool' messages.
            # Any images they carry can't ride a 'tool' message (OpenAI only
            # allows text there), so collect them and forward on a 'user'
            # message right after — otherwise they'd be silently dropped.
            tool_result_images: list[dict[str, Any]] = []
            for tr in tool_results:
                tool_msg, image_parts = _split_tool_result(tr)
                messages.append(tool_msg)
                tool_result_images.extend(image_parts)
            if tool_result_images:
                messages.append({"role": "user", "content": tool_result_images})

            if role == "assistant" and tool_uses:
                tool_calls = [
                    {
                        "id": tu.get("id"),
                        "type": "function",
                        "function": {
                            "name": tu.get("name"),
                            "arguments": json.dumps(tu.get("input", {})),
                        },
                    }
                    for tu in tool_uses
                ]
                text = "".join(
                    b.get("text", "") for b in other if b.get("type") == "text"
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": tool_calls,
                    }
                )
            elif other:
                messages.append({"role": role, "content": _content_to_openai(other)})
        else:
            messages.append({"role": role, "content": content})

    payload: dict[str, Any] = {
        "model": map_model(req.get("model")),
        "messages": messages,
    }
    if req.get("max_tokens") is not None:
        payload["max_tokens"] = req["max_tokens"]
    for key in ("temperature", "top_p", "stop_sequences", "stream"):
        if key in req:
            payload["stop" if key == "stop_sequences" else key] = req[key]

    if req.get("tools"):
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object"}),
                },
            }
            for t in req["tools"]
        ]
        tc = req.get("tool_choice")
        if isinstance(tc, dict):
            ttype = tc.get("type")
            if ttype == "auto":
                payload["tool_choice"] = "auto"
            elif ttype == "any":
                payload["tool_choice"] = "required"
            elif ttype == "tool" and tc.get("name"):
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc["name"]},
                }

    return payload


_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}


def openai_to_anthropic_response(
    resp: dict[str, Any], model: str
) -> dict[str, Any]:
    """Translate a buffered OpenAI chat completion into an Anthropic message."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}

    content_blocks: list[dict[str, Any]] = []
    text = msg.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        for part in text:
            if part.get("type") in ("text", "output_text"):
                content_blocks.append({"type": "text", "text": part.get("text", "")})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name"),
                "input": args,
            }
        )

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    usage = resp.get("usage") or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return {
        "id": resp.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": _STOP_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_read_input_tokens": cached,
        },
    }


# --------------------------------------------------------------------------- #
# Streaming: OpenAI SSE deltas -> Anthropic SSE events
# --------------------------------------------------------------------------- #
def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def _iter_openai_sse(line_buffer: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in line_buffer.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            out.append(json.loads(data))
        except json.JSONDecodeError:
            continue
    return out


async def stream_openai_to_anthropic(
    source: AsyncIterator[bytes], model: str
) -> AsyncIterator[tuple[bytes, dict[str, Any] | None]]:
    """Consume an OpenAI chat SSE stream and yield Anthropic SSE event bytes.

    Yields ``(sse_bytes, usage_or_None)``. The final tuple carries the usage
    dict (``{input_tokens, output_tokens}``) so the caller can record stats.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    ), None

    # Block bookkeeping. index 0 is reserved for text once it appears.
    text_open = False
    # OpenAI tool_call index -> anthropic content block index
    tool_blocks: dict[int, int] = {}
    next_block_index = 0
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None

    buffer = ""
    async for chunk in source:
        buffer += chunk.decode("utf-8", "replace")
        # Process complete SSE records (terminated by blank line).
        while "\n\n" in buffer:
            record, buffer = buffer.split("\n\n", 1)
            for data in _iter_openai_sse(record):
                if data.get("usage"):
                    u = data["usage"]
                    usage = {
                        "input_tokens": u.get("prompt_tokens", 0),
                        "output_tokens": u.get("completion_tokens", 0),
                        "cache_read_input_tokens": (
                            u.get("prompt_tokens_details") or {}
                        ).get("cached_tokens", 0),
                    }
                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {}) or {}
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

                # Text delta.
                text_piece = delta.get("content")
                if isinstance(text_piece, str) and text_piece:
                    if not text_open:
                        text_open = True
                        if next_block_index == 0:
                            text_index = 0
                            next_block_index = 1
                        else:
                            text_index = next_block_index
                            next_block_index += 1
                        # Remember the text block index.
                        tool_blocks[-1] = text_index
                        yield _sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": text_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        ), None
                    yield _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": tool_blocks[-1],
                            "delta": {"type": "text_delta", "text": text_piece},
                        },
                    ), None

                # Tool call deltas.
                for tc in delta.get("tool_calls") or []:
                    tc_idx = tc.get("index", 0)
                    fn = tc.get("function", {}) or {}
                    if tc_idx not in tool_blocks:
                        block_index = next_block_index
                        next_block_index += 1
                        tool_blocks[tc_idx] = block_index
                        yield _sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": block_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tc.get("id")
                                    or f"toolu_{uuid.uuid4().hex[:24]}",
                                    "name": fn.get("name", ""),
                                    "input": {},
                                },
                            },
                        ), None
                    args = fn.get("arguments")
                    if args:
                        yield _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": tool_blocks[tc_idx],
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": args,
                                },
                            },
                        ), None

    # Close any open blocks.
    if text_open:
        yield _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": tool_blocks[-1]},
        ), None
    for _, block_index in sorted(
        ((k, v) for k, v in tool_blocks.items() if k >= 0), key=lambda x: x[1]
    ):
        yield _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": block_index},
        ), None

    out_usage = usage or {"input_tokens": 0, "output_tokens": 0}
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": _STOP_MAP.get(finish_reason, "end_turn"),
                "stop_sequence": None,
            },
            "usage": {"output_tokens": out_usage.get("output_tokens", 0)},
        },
    ), None
    yield _sse("message_stop", {"type": "message_stop"}), out_usage
