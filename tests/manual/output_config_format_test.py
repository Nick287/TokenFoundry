#!/usr/bin/env python3
"""测试：通过 TokenFoundry 网关调用时，`output_config.format`（结构化输出）是否
端到端生效？

要验证的是 schema 是否被**强制执行**，而不是模型碰巧输出了 JSON。所以提示词故意
写成散文式——"用两句友好的话介绍一下东京"——它本身绝不会产出 JSON。只有后端真的
应用了 schema，才会把它变成一个对象；因此这里拿到符合 schema 的对象，就证明该字段
生效了。另外会附带一次不带 `format` 的对照调用，展示同一提示词在不受约束时的输出。

同时也做校验检查：格式错误的 `format` 必须被拒绝，且报错里点名该字段，证明上游
解析了它而不是丢掉了它。（Copilot 后端对 `output_config` 严格校验——不认识的子字段
会返回 "Extra inputs are not permitted"——所以不应出现"悄悄接受"的情况。）

非流式和流式都要测，因为 Claude Code 走的是流式：流式下 JSON 以多个 `text_delta`
片段到达，必须拼接完整后才能解析；而且上游的拒绝会以 HTTP 200 下的 SSE 流内
`error` 事件出现，因为 hub 在调用上游之前就已经发出了响应头。

运行前在下面填写 GATEWAY_URL 和 VIRTUAL_KEY。这个目录会被提交到 git，写在这里的
key 会随下一次提交进入仓库。不要把填好的值提交上去。

用法：
    python tests/manual/output_config_format_test.py

开销：5 次小调用。
退出码：0 全部通过，1 有检查失败，2 GATEWAY_URL/VIRTUAL_KEY 未填写。
"""
from __future__ import annotations

import json
import sys

import httpx

# --- 在本地填写；不要提交 ------------------------------------------------------
GATEWAY_URL = ""   # 例如 https://tokenfoundry-apim-xxxx.azure-api.net
VIRTUAL_KEY = ""   # APIM 订阅密钥（虚拟密钥）
# -----------------------------------------------------------------------------

MODEL = "claude-sonnet-5"
PROMPT = "Tell me about Tokyo in two friendly sentences."
SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "population_millions": {"type": "number"},
    },
    "required": ["city", "population_millions"],
    "additionalProperties": False,
}
FORMAT = {"type": "json_schema", "schema": SCHEMA}
TIMEOUT = 300.0

FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'通过' if ok else '失败'}  {label:<30} {detail}")
    if not ok:
        FAILURES.append(label)


def _url() -> str:
    return GATEWAY_URL.rstrip("/") + "/llm-anthropic/v1/messages"


def _headers() -> dict[str, str]:
    # 用 `x-api-key` 而不是 `api-key`：Anthropic API 在 APIM 上以原生协议的
    # 请求头作为订阅密钥。
    return {"x-api-key": VIRTUAL_KEY, "anthropic-version": "2023-06-01"}


def _body(fmt: dict | None, stream: bool) -> dict:
    body: dict = {"model": MODEL, "max_tokens": 1000,
                  "messages": [{"role": "user", "content": PROMPT}]}
    if fmt is not None:
        body["output_config"] = {"format": fmt}
    if stream:
        body["stream"] = True
    return body


def call(fmt: dict | None) -> tuple[int, str, str | None]:
    """非流式调用。返回 (状态码, 响应文本, 错误信息)。"""
    r = httpx.post(_url(), headers=_headers(), json=_body(fmt, False), timeout=TIMEOUT)
    d = r.json()
    err = d.get("error")
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    return r.status_code, text, err.get("message") if isinstance(err, dict) else None


def call_stream(fmt: dict | None) -> tuple[int, str, str | None]:
    """流式调用。把 text_delta 片段拼接起来。返回 (状态码, 响应文本, 流内错误)。"""
    text, err = "", None
    with httpx.Client(timeout=TIMEOUT) as c, \
            c.stream("POST", _url(), headers=_headers(), json=_body(fmt, True)) as r:
        status = r.status_code
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                o = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            if o.get("type") == "content_block_delta" and o["delta"].get("type") == "text_delta":
                text += o["delta"]["text"]
            if isinstance(o.get("error"), dict):
                err = o["error"].get("message")
    return status, text, err


def schema_valid(text: str) -> tuple[bool, str]:
    """`text` 是否是严格符合 SCHEMA 的 JSON 对象：两个必填字段都在、类型正确、
    没有多余字段（additionalProperties: false）。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False, f"不是 JSON: {text[:70]!r}"
    if not isinstance(obj, dict):
        return False, f"是 JSON 但不是对象: {obj!r}"
    if set(obj) != {"city", "population_millions"}:
        return False, f"字段不对: {sorted(obj)}"
    if not isinstance(obj["city"], str) or not isinstance(obj["population_millions"], (int, float)):
        return False, f"类型不对: {obj}"
    return True, json.dumps(obj, ensure_ascii=False)


def main() -> int:
    if not GATEWAY_URL or not VIRTUAL_KEY:
        print("GATEWAY_URL / VIRTUAL_KEY 为空——请在本文件顶部填写。")
        return 2
    print(f"网关 {GATEWAY_URL}   模型 {MODEL}\n")

    print("=== 1. 校验：格式错误的 format 被拒绝，且报错点名该字段 ===")
    st, _, err = call({"type": "json_schema", "schema": "not-an-object"})
    check(st == 400 and "format" in (err or ""), "非流式 schema=<字符串>",
          f"HTTP {st}  {err or ''}"[:150])
    st, _, err = call_stream({"type": "no_such_type"})
    # 这里不会是 400：流式下 hub 已经先返回了 200 OK。
    check(err is not None, "流式 type=no_such_type（流内错误）",
          f"HTTP {st}  错误事件: {err}")

    print("\n=== 2. 强制执行：散文式提示词返回符合 schema 的 JSON ===")
    st, text, err = call(FORMAT)
    ok, detail = schema_valid(text)
    check(st == 200 and not err and ok, "非流式 带 format", f"HTTP {st}  {err or detail}")
    st, text, err = call_stream(FORMAT)
    ok, detail = schema_valid(text)
    check(st == 200 and not err and ok, "流式 带 format", f"HTTP {st}  {err or detail}")

    print("\n=== 对照：同一提示词不带 format（仅供参考，不计入结果）===")
    st, text, err = call(None)
    print(f"  HTTP {st}  {err or repr(text[:90])}")

    print(f"\n{'全部通过' if not FAILURES else '失败项：' + '、'.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
