# `copilot_usage` —— 上游真实成本口径（响应体内）

> 2026-07-30 在 dev-a12（3 账号池）实测记录。
> 一句话：**网关返回的 JSON 里有一个 `copilot_usage` 顶层字段，逐次调用给出
> 四类 token 的用量 + 单价 + 总成本**。这是目前唯一能直接拿到**上游真实计费金额**
> 的来源——比 LlmLog（只有 3 类 token、无价格）和 customMetrics（有 cached、
> 无价格）都完整。

---

## 0. 为什么之前没发现

我们此前所有的 token 验证脚本（`verify_token_three_way.py`、
`verify_token_vs_diagnostic.py`、`load_test_*.py`）都只读响应体里的 **`usage`**
字段，从未枚举顶层 key。`copilot_usage` 是 **`usage` 的兄弟字段**，一直都在，
只是没人看。

> ⚠️ 同时纠正一个此前的错误判断：查 GitHub 官方文档看到 Copilot 按
> "premium request × 模型倍率" 计费，曾据此判断"拿不到 token 级成本"。
> **那是 IDE/Chat 那条路径的计费方式**；我们走 `/v1/messages`、
> `/v1/chat/completions` API 拿到的是**按 token 计的 AIU 计费**，两套并存。

---

## 1. 字段结构

```json
"copilot_usage": {
  "token_details": [
    {"token_type": "input",       "token_count": 9,    "cost_per_batch": 500000000000,  "batch_size": 1000000},
    {"token_type": "cache_read",  "token_count": 5071, "cost_per_batch": 50000000000,   "batch_size": 1000000},
    {"token_type": "cache_write", "token_count": 0,    "cost_per_batch": 625000000000,  "batch_size": 1000000},
    {"token_type": "output",      "token_count": 13,   "cost_per_batch": 2500000000000, "batch_size": 1000000}
  ],
  "total_nano_aiu": 290550000
}
```

| 字段 | 含义 |
| --- | --- |
| `token_type` | 四类固定：`input` / `cache_read` / `cache_write` / `output` |
| `token_count` | 该类 token 的数量 |
| `batch_size` | 计价批量，恒为 `1000000`（即单价按每百万 token 计） |
| `cost_per_batch` | 每 `batch_size` 个 token 的成本，单位 **nano_aiu** |
| `total_nano_aiu` | 本次调用总成本 |

**计价单位是 AIU**（AI Unit），`nano_aiu` = AIU 的 1e-9。

### 总额校验（cache read 那次）

```text
input      9    × 500e9  / 1e6 =     4,500,000
cache_read 5071 × 50e9   / 1e6 =   253,550,000
cache_write 0   × 625e9  / 1e6 =             0
output     13   × 2500e9 / 1e6 =    32,500,000
                          合计 =   290,550,000  ✅ 与 total_nano_aiu 完全一致
```

→ **`total_nano_aiu = Σ(token_count × cost_per_batch / batch_size)`**，可逐笔核对。

---

## 2. 单价表（实测，与 Anthropic 官方价一致）

把 `cost_per_batch / batch_size` 换算成每百万 token：

**claude-opus-4-8**

| token_type | nano_aiu / MTok | 折合 $ | Anthropic 官方价 | 倍率 |
| --- | --- | --- | --- | --- |
| `input` | 500,000,000,000 | **$5.00** | $5.00 | 1× |
| `cache_read` | 50,000,000,000 | **$0.50** | $0.50 | **0.1×** |
| `cache_write` | 625,000,000,000 | **$6.25** | $6.25 | **1.25×** |
| `output` | 2,500,000,000,000 | **$25.00** | $25.00 | 5× input |

**claude-haiku-4.5**（正好是 opus 的 1/5）

| token_type | nano_aiu / MTok | 折合 $ |
| --- | --- | --- |
| `input` | 100,000,000,000 | $1.00 |
| `cache_read` | 10,000,000,000 | $0.10 |
| `cache_write` | 125,000,000,000 | $1.25 |
| `output` | 500,000,000,000 | $5.00 |

由此推出换算率：**1 AIU = $0.01**
（`cost_per_batch = 500,000,000,000 nano_aiu = 500 AIU`，对应 100 万 input token
的 $5.00 → `5.00 / 500 = 0.01`）

于是每次调用的美元成本 = **`total_nano_aiu / 1e9 × 0.01`**，
即 `total_nano_aiu / 1e11`。上面 cache read 那次：
`290,550,000 / 1e11 = $0.0029`。

> 单价随模型不同而不同，且**由上游下发**——我们不需要自己维护价格表，
> 也不用跟着模型调价改代码。

### ⚠️ gpt-4o-mini 是免费档（cost_per_batch = 0）

```json
{"token_type": "input",  "token_count": 10, "cost_per_batch": 0, "batch_size": 1000000}
...
"total_nano_aiu": 0
```

OpenAI 系模型实测 **`cost_per_batch` 全为 0、`total_nano_aiu` 为 0** ——
即 Copilot 订阅内不额外计费。所以：

- **不能假设所有模型都有成本**，按 `total_nano_aiu` 汇总时 0 是合法值
- 若要给客户计费，免费档模型需要我们自己定价（上游成本为 0 ≠ 售价为 0）

---

## 3. 三个计费相关的行为

### 3.1 缓存折扣真实生效（读比写便宜 11 倍）

同样 5071 token 的前缀，两次调用对比：

| 场景 | `total_nano_aiu` | 折合 $ |
| --- | --- | --- |
| cache_write（首次） | 3,216,375,000 | $0.0322 |
| **cache_read（复用）** | **290,550,000** | **$0.0029** |

→ 命中缓存省 **91%**。这印证了 `CAPACITY.zh.md` §2.9 里 82% 命中率的价值——
**省的是钱，不只是延迟**（§2.8 已证明缓存不影响并发上限）。

### 3.2 `cache_write` 只有一档单价，不区分 5m/1h TTL

Anthropic 官方 API 对 1h TTL 的写入收 **2×**（vs 5m 的 1.25×），
但实测两种 TTL 的 `cost_per_batch` **都是 625e9（1.25×）**：

```text
cache 5m write → cost_per_batch 625000000000
cache 1h write → cost_per_batch 625000000000   ← 同价
```

→ **这条路径不对 1h TTL 加价**。之前担心的"TTL 维度丢失导致算不出账"，
在 Copilot 计费口径下不成立（`usage.cache_creation` 仍会拆分 TTL，
但那是用量口径，不影响成本）。

### 3.3 thinking token 全额计费，但 `copilot_usage` 里看不出占比

`token_details` 只有四个固定条目（`input`/`cache_read`/`cache_write`/`output`），
**没有 thinking 条目**。实测核对 `copilot_usage` 的 output 计数：

| 场景 | `usage.output_tokens` | `..thinking_tokens` | `copilot_usage` 计费 output |
| --- | --- | --- | --- |
| 简单问题（未思考） | 300 | 0 | **300** |
| 难题 · effort=high | 2980 | **1901** | **2980** |

→ 计费 output **等于含 thinking 的 `output_tokens`**（不是 2980−1901=1079），
即 **thinking 按 $25/MTok 全额收费**。那次调用中 thinking 单独就是
`1901 × 2500e9 / 1e6 = 4,752,500,000 nano_aiu ≈ $0.0475`，
占该次总成本的 **64%**。

**两个字段必须配合使用：**

```text
成本  ← copilot_usage.total_nano_aiu               （含 thinking，但看不出占比）
用量  ← usage.output_tokens_details.thinking_tokens （能算占比，但无价格）
```

> ⚠️ **计费透明度风险**：thinking 是不可预测的成本项——同样的 `effort` 参数，
> 模型自行决定是否思考及思考多久（实测 low/medium/high = 880/1309/1859 tokens，
> 而简单问题下 adaptive 会直接返回 0）。用户看到的可见回复只有 1079 token，
> 账单却按 2980 计。若要做成本透明，**必须单独暴露 thinking 占比**，
> 否则从用户视角看像是多收费。

---

## 4. 完整响应 JSON（实测原文）

### 4.1 Anthropic 非流式（`POST /llm-anthropic/v1/messages`）

```json
{
  "content": [{"text": "Hi! How can I help you today?", "type": "text"}],
  "copilot_usage": {
    "token_details": [
      {"batch_size": 1000000, "cost_per_batch": 500000000000,  "token_count": 9,    "token_type": "input"},
      {"batch_size": 1000000, "cost_per_batch": 50000000000,   "token_count": 5071, "token_type": "cache_read"},
      {"batch_size": 1000000, "cost_per_batch": 625000000000,  "token_count": 0,    "token_type": "cache_write"},
      {"batch_size": 1000000, "cost_per_batch": 2500000000000, "token_count": 13,   "token_type": "output"}
    ],
    "total_nano_aiu": 290550000
  },
  "id": "msg_011CdXjmpZ8oTyJLAew5CcTo",
  "model": "claude-opus-4-8",
  "role": "assistant",
  "stop_details": null,
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "type": "message",
  "usage": {
    "cache_creation": {
      "ephemeral_1h_input_tokens": 0,
      "ephemeral_5m_input_tokens": 0
    },
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 5071,
    "inference_geo": "global",
    "input_tokens": 9,
    "output_tokens": 13,
    "output_tokens_details": {"thinking_tokens": 0}
  }
}
```

**`usage` 的 7 个顶层 key**（Anthropic 口径，用量而非成本）：

| key | 说明 |
| --- | --- |
| `input_tokens` | 未命中缓存的输入 token（**不含** cache_read/cache_write） |
| `output_tokens` | 输出 token（**含** thinking） |
| `cache_read_input_tokens` | 命中缓存的输入 token |
| `cache_creation_input_tokens` | 写入缓存的输入 token |
| `cache_creation` | 嵌套，按 TTL 拆分：`ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` |
| `output_tokens_details` | 嵌套，含 `thinking_tokens`（已计入 `output_tokens`） |
| `inference_geo` | 非 token，推理地理位置（如 `"global"`） |

> ⚠️ **计费口径的 prompt 总量 = `input_tokens + cache_read + cache_write`**。
> Anthropic 的 `input_tokens` **不含**缓存部分（与 OpenAI 的
> `prompt_tokens` 相反，后者是含的）——跨 provider 汇总时必须换算。

### 4.2 Anthropic 流式（SSE）

`copilot_usage` **只出现在 `message_delta` 事件**，`message_start` 里只有
部分 `usage`（input 侧已定，output 侧尚未产生）：

```text
event: message_start
data: {"message":{...,"usage":{"cache_read_input_tokens":5071,"input_tokens":9,"output_tokens":4,...}}}

event: content_block_delta
...

event: message_delta
data: {"copilot_usage":{"token_details":[...],"total_nano_aiu":290550000},"usage":{...}}

event: message_stop
```

→ **流式下要读 `copilot_usage` 必须解析到 `message_delta`**，
只取 `message_start` 会拿不到成本。

### 4.3 OpenAI 兼容（`POST /llm-openai/v1/chat/completions`）

`copilot_usage` 同样存在（gpt-4o-mini 为免费档，单价 0）：

```json
{
  "choices": [{"finish_reason": "stop", "index": 0,
               "message": {"content": "Hi! How can I assist you today?",
                           "padding": "abcdef", "role": "assistant"},
               "content_filter_results": {...}}],
  "id": "chatcmpl-E7EBkX19aLUcdlGmnXE1CHxdj7BLQ",
  "model": "gpt-4o-mini-2024-07-18",
  "usage": {
    "prompt_tokens": 10,
    "prompt_tokens_details": {"cached_tokens": 0},
    "completion_tokens": 10,
    "completion_tokens_details": {
      "accepted_prediction_tokens": 0,
      "rejected_prediction_tokens": 0
    },
    "total_tokens": 20
  },
  "service_tier": "default",
  "system_fingerprint": "fp_b0a8b9e62b",
  "prompt_filter_results": [{"content_filter_results": {...}, "prompt_index": 0}],
  "copilot_usage": {
    "token_details": [
      {"batch_size": 1000000, "cost_per_batch": 0, "token_count": 10, "token_type": "input"},
      {"batch_size": 1000000, "cost_per_batch": 0, "token_count": 0,  "token_type": "cache_read"},
      {"batch_size": 1000000, "cost_per_batch": 0, "token_count": 0,  "token_type": "cache_write"},
      {"batch_size": 1000000, "cost_per_batch": 0, "token_count": 10, "token_type": "output"}
    ],
    "total_nano_aiu": 0
  }
}
```

注意 OpenAI 侧还有几个 Azure 特有字段：`content_filter_results` /
`prompt_filter_results`（内容过滤）、`service_tier`、`system_fingerprint`，
以及 message 里的 `padding`（hub 侧填充，无语义）。

---

## 5. 与另外两条计量路径的对比

| 能力 | LlmLog | customMetrics | **copilot_usage** | `usage`（同响应体） |
| --- | --- | --- | --- | --- |
| prompt / completion / total | ✅ | ✅ | ✅ | ✅ |
| cached（读） | ❌ | ✅ | ✅ | ✅ |
| cache_write（写） | ❌ | ❌ | ✅ | ✅（还按 TTL 拆分） |
| **thinking 用量** | ❌ | ❌ | ❌（计费但不拆分） | ✅ |
| **单价** | ❌ | ❌ | ✅ | ❌ |
| **成本金额** | ❌ | ❌ | ✅ | ❌ |
| 逐笔可对账 | ✅（按 RequestId） | ❌（聚合） | ✅（响应体内） | ✅（响应体内） |
| 已落地到 Azure 遥测 | ✅ | ✅ | ❓ **未验证** | ❓ 部分（LlmLog/customMetrics 各取一部分） |

**`copilot_usage` 是唯一能给出上游真实成本的来源**，而且不需要我们自己维护
价格表。但**它和 `usage` 互补、缺一不可**——成本在 `copilot_usage`，
thinking 与 TTL 拆分在 `usage`。两者目前都只存在于**响应体**里。

---

## 6. 待验证 / 待办

1. **能否落到遥测**（关键）——`ApiManagementGatewayLlmLog` 的 schema 固定为
   PromptTokens/CompletionTokens/TotalTokens，几乎肯定不认识 `copilot_usage`；
   `llm-emit-token-metric` 策略也只认标准字段。若两条路都记不到，要做成本计费
   就必须在 **APIM outbound policy 里显式提取** `copilot_usage.total_nano_aiu`
   写入 trace/metric，或在 hub 侧落库。**注意要一并提取
   `usage.output_tokens_details.thinking_tokens`**（§3.3：成本在
   `copilot_usage`、thinking 占比在 `usage`，两者缺一不可）。
   > 注意 §4.2：流式响应的 `copilot_usage` 在 `message_delta` 事件里，
   > 而 APIM policy 读流式 body 历史上有 `BODY_READ_FAILED` 问题
   > （见 `customMetrics-diagnostic-troubleshooting.md` §2 路径 C）。
2. **其他模型的单价表**——只实测了 opus-4-8 / haiku-4.5 / gpt-4o-mini，
   gemini 系与其余 claude 型号未测。
3. **免费档的定价策略**——gpt-4o-mini 上游成本为 0，若要计费需自定价。

---

## 7. 复现

```bash
# 查看完整响应 JSON（含 copilot_usage）
python tests/manual/verify_opus_token_types.py --no-telemetry

# 或直接用最小请求观察
curl -s "$TF_GATEWAY_URL/llm-anthropic/v1/messages" \
  -H "x-api-key: $TF_VIRTUAL_KEY" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4-8","max_tokens":50,
       "messages":[{"role":"user","content":"Say hi."}]}' | jq .copilot_usage
```

---

## 8. 相关文档

- `docs/customMetrics-diagnostic-troubleshooting.md` —— 三条 token 计量路径
  （customMetrics / LlmLog / trace）的排查记录与选型
- `docs/CAPACITY.zh.md` §2.8 —— 证伪"TPM 配额"，说明缓存不影响并发上限
- `docs/CAPACITY.zh.md` §2.9 —— 三家 provider 的缓存行为差异与命中率
