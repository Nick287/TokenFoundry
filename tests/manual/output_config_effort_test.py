#!/usr/bin/env python3
"""测试：通过 TokenFoundry 网关调用时，`output_config.effort` 是否端到端生效？

要验证的不是"请求能不能成功"。Copilot 后端对 `output_config` 做严格校验，hub
也会原样转发它；但单看一个 200 说明不了问题——一个悄悄丢掉该字段的后端同样会
返回 200。所以 effort 用两个互相独立的信号来判断：

  * 校验 —— 传一个非法档位，必须被拒绝，且报错里点名该字段。这证明上游确实
    解析了 `output_config.effort`，而不是忽略它。
  * 行为 —— 同一个问题，`max` 的输出 token 必须明显多于 `low`。这证明档位真的
    被应用了，而不只是被接受。
    （在 claude-sonnet-5 上实测，2026-11：low 约 60-70，max 约 440-560。）

非流式和流式都要测，因为 Claude Code 走的是流式，而两条路径出错的表现不同：
流式下 hub 在调用上游之前就已经发出了 `200 OK`，所以上游的拒绝只会以 SSE 流内
的 `error` 事件出现，永远不会体现在 HTTP 状态码上。只看状态码的客户端看不到它。

运行前在下面填写 GATEWAY_URL 和 VIRTUAL_KEY。它们故意留空——这个目录会被提交
到 git，写在这里的 key 会随下一次提交进入仓库。不要把填好的值提交上去。

用法：
    python tests/manual/output_config_effort_test.py

开销：6 次小调用（其中 2 次是 effort=max）。
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
# 一个答案很短、但有思考空间的问题：low 档会直接作答，max 档会充分推理。
# 如果问题没什么可想的，各档位表现会差不多，行为检查就失去了意义。
PROMPT = ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the "
          "ball. A second bat costs twice the first. How much do two bats and three "
          "balls cost? Answer with the number only.")
# `max` 的输出至少要是 `low` 的这么多倍，才算档位生效。
# 实测约 7-8 倍；取 2 倍是给每次运行之间的波动留余量。
MIN_RATIO = 2.0
TIMEOUT = 300.0

FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'通过' if ok else '失败'}  {label:<30} {detail}")
    if not ok:
        FAILURES.append(label)


def _url() -> str:
    return GATEWAY_URL.rstrip("/") + "/llm-anthropic/v1/messages"


def _headers() -> dict[str, str]:
    # Anthropic API 在 APIM 上注册的订阅密钥头是 `x-api-key`（原生协议）。
    # `api-key` 是 OpenAI / Google API 用的，在这里会得到 401 "missing subscription key"。
    return {"x-api-key": VIRTUAL_KEY, "anthropic-version": "2023-06-01"}


def _body(effort: str, stream: bool) -> dict:
    body = {"model": MODEL, "max_tokens": 4000,
            "output_config": {"effort": effort},
            "messages": [{"role": "user", "content": PROMPT}]}
    if stream:
        body["stream"] = True
    return body


def call(effort: str) -> tuple[int, int | None, str | None]:
    """非流式调用。返回 (状态码, output_tokens, 错误信息)。"""
    r = httpx.post(_url(), headers=_headers(), json=_body(effort, False), timeout=TIMEOUT)
    d = r.json()
    err = d.get("error")
    return (r.status_code, (d.get("usage") or {}).get("output_tokens"),
            err.get("message") if isinstance(err, dict) else None)


def call_stream(effort: str) -> tuple[int, int | None, str | None, bool]:
    """流式调用。返回 (状态码, output_tokens, 流内错误, 是否收到 message_stop)。"""
    out = err = None
    done = False
    with httpx.Client(timeout=TIMEOUT) as c, \
            c.stream("POST", _url(), headers=_headers(), json=_body(effort, True)) as r:
        status = r.status_code
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                o = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            if o.get("type") == "message_delta":
                out = (o.get("usage") or {}).get("output_tokens")
            elif o.get("type") == "message_stop":
                done = True
            if isinstance(o.get("error"), dict):
                err = o["error"].get("message")
    return status, out, err, done


def main() -> int:
    if not GATEWAY_URL or not VIRTUAL_KEY:
        print("GATEWAY_URL / VIRTUAL_KEY 为空——请在本文件顶部填写。")
        return 2
    print(f"网关 {GATEWAY_URL}   模型 {MODEL}\n")

    print("=== 1. 校验：非法档位被拒绝，且报错点名该字段 ===")
    st, _, err = call("bogus")
    check(st == 400 and "effort" in (err or ""), "非流式 effort=bogus",
          f"HTTP {st}  {err or ''}"[:150])
    st, _, err, _ = call_stream("bogus")
    # 这里不会是 400：hub 在调用上游之前就已经返回了 200。
    check(err is not None, "流式 effort=bogus（流内错误）",
          f"HTTP {st}  错误事件: {err}")

    print("\n=== 2. 行为：max 的输出明显多于 low（非流式）===")
    st_lo, out_lo, err_lo = call("low")
    check(st_lo == 200 and out_lo is not None, "非流式 effort=low",
          f"HTTP {st_lo}  output_tokens={out_lo} {err_lo or ''}")
    st_hi, out_hi, err_hi = call("max")
    check(st_hi == 200 and out_hi is not None, "非流式 effort=max",
          f"HTTP {st_hi}  output_tokens={out_hi} {err_hi or ''}")
    if out_lo and out_hi:
        ratio = out_hi / out_lo
        check(ratio >= MIN_RATIO, "非流式 max/low 倍数",
              f"{out_hi}/{out_lo} = {ratio:.1f} 倍（要求 >= {MIN_RATIO} 倍）")

    print("\n=== 3. 行为：流式路径同样生效 ===")
    st_lo, out_lo, err_lo, done_lo = call_stream("low")
    check(st_lo == 200 and done_lo and not err_lo, "流式 effort=low",
          f"HTTP {st_lo}  output_tokens={out_lo} message_stop={done_lo} {err_lo or ''}")
    st_hi, out_hi, err_hi, done_hi = call_stream("max")
    check(st_hi == 200 and done_hi and not err_hi, "流式 effort=max",
          f"HTTP {st_hi}  output_tokens={out_hi} message_stop={done_hi} {err_hi or ''}")
    if out_lo and out_hi:
        ratio = out_hi / out_lo
        check(ratio >= MIN_RATIO, "流式 max/low 倍数",
              f"{out_hi}/{out_lo} = {ratio:.1f} 倍（要求 >= {MIN_RATIO} 倍）")

    print(f"\n{'全部通过' if not FAILURES else '失败项：' + '、'.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
