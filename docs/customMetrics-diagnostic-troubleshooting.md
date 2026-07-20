# customMetrics / Diagnostic / LlmLog 排查记录

> 2026-07-20。一次耗时很长的排查，结论对将来配 APIM token 计量至关重要。
> 核心一句话：**API 级诊断漏配 `metrics=true` 会静默关闭 emit-token-metric（customMetrics），
> 表现为 Token 细分 UI 空白、cached 拿不到。**

---

## 1. 现象

Portal「用量 → Token 细分（模型 / 端点 / 密钥）」三个分组**全空**，只有「Hub（后端）」
和「调用与延迟」有数据。而计费口径的 `ApiManagementGatewayLlmLog` 表**有数据、且准确**。

一度以为是 UI bug 或数据源选错，实际是 **emit-token-metric 被静默关闭**，customMetrics 表
（App Insights 里叫 `AppMetrics`）没有任何 token 数据。

## 2. Azure 里三条 token 数据路径（先分清）

| 路径 | 策略 / 机制 | 落地表 | cached | 流式 |
| --- | --- | --- | --- | --- |
| **A. emit-token-metric** | `llm-emit-token-metric` 策略 | App Insights **customMetrics / AppMetrics** | ✅ `Prompt Cached Tokens` | ✅ 精确* |
| **B. LLM 诊断日志** | diagnostic `largeLanguageModel` | **ApiManagementGatewayLlmLog** | ❌ 无 cached 列 | ✅ 精确（objfix 后）|
| C. 自研 trace | APIM outbound policy 写 trace | App Insights **AppTraces**（`llm-usage`）| ✅ 非流式 | ❌ 流式 `BODY_READ_FAILED` |

\* 流式精确的前提：请求带 `stream_options.include_usage`。本项目 policy（`_build_chat_stream_policy`）
**自动注入** include_usage，所以流式也精确（实测非流式 / 流式 / cached 全部逐值对上上游真实值）。

**关键事实：cached 只有路径 A（customMetrics）能拿到。**
- LlmLog（路径 B）表 schema 只有 `PromptTokens / CompletionTokens / TotalTokens`，无 cached 列；
  `RequestMessages / ResponseMessages` 记的是对话文本（role/content），不含 usage → 挖不出 cached。
  （官方文档 api-management-howto-llm-logs 全文 0 次 "cache"，逐字印证。）
- AppTraces（路径 C）流式 `BODY_READ_FAILED`（policy 读不到 SSE body）。

## 3. 根因（审计日志 + 实验双重证明）

### APIM 规则：**API 级诊断 override 服务级诊断**

- 服务级 diagnostic：`metrics=true`（emit-token-metric 靠它把指标发到 customMetrics）。
- 我们为了配 LlmLog，给每个 LLM API 建了 **API 级** diagnostic（`applicationinsights`），body 里
  只写了 `largeLanguageModel`，**漏了 `metrics`**。
- API 级诊断存在时会 **override** 服务级 → 对该 API，`metrics` 变成默认 off →
  **emit-token-metric 被静默关闭，customMetrics 全空**。而 LlmLog（largeLanguageModel）照常
  工作，所以表面看不出问题，直到发现 Token 细分（和 cached）空了。

### 证据

1. **审计日志**（a05 Activity Log）：customMetrics 数据恰好停在 **2026-07-10 14:46:40 的
   `diagnostics/delete llm-openai/applicationinsights`**（那次为配 largeLanguageModel 删+重建
   API 级诊断）。按天分布：7/09 n=27，7/10 n=3（14:47 后彻底停）。
2. **删诊断实验**（a10）：删掉 llm-openai 的 API 级 largeLanguageModel 诊断 →
   customMetrics **立即复活**（9 种指标全回来，含 `Prompt Cached Tokens`）。
3. **两全实验**（a10）：给 API 级诊断**同时**设 `metrics=true` + `largeLanguageModel` →
   customMetrics（cached=1024）**和** LlmLog（流式精确）**同时工作**。

## 4. 排查中被排除的“假根因”（都验证过，别再走弯路）

| 假设 | 结论 | 依据 |
| --- | --- | --- |
| 订阅 feature `EnableCustomMetricsV2` Pending 卡死 | ❌ **无关** | 官方文档：custom metrics 的 **log-based 版本永远保留全维度、always 存 Log Analytics**；opt-in 只影响 Metrics Store（时序库）+ 额外计费。a05 opt-in=None 却曾有带维度数据，矛盾即解。 |
| `customMetricsOptedInType='WithDimensions'` 没设 | ❌ **无关** | 同上，只影响 Metrics Store，不影响 AppMetrics 表。且它还额外收费，不该开。 |
| APIM MI 缺 `Monitoring Metrics Publisher` 角色 | ❌ **不是本因** | emit 走 logger connectionString 上报，不依赖该角色（a05 数据早于角色授予时间）。角色是官方推荐、可留，但不是 customMetrics 空的原因。 |
| LlmLog 的 `ResponseMessages` 能挖出 cached | ❌ 挖不出 | messages 记的是对话文本，不含 usage 对象。 |

## 5. 修复（一行配置）

`app/services/apim_provisioner.py` 的 `_ensure_api_llm_diagnostic` PUT body 里加 `"metrics": True`：

```python
body = {
    "properties": {
        "loggerId": logger_id,
        "metrics": True,   # ← 关键：不加则 API 级诊断 override 服务级、关闭 emit-token-metric
        "largeLanguageModel": {
            "logs": "enabled",
            "requests":  {"messages": "all", "maxSizeInBytes": 32768},
            "responses": {"messages": "all", "maxSizeInBytes": 32768},
        },
    }
}
```

效果：**customMetrics（cached + 9 类 token + subscription/api/model 维度）和 LlmLog（流式精确、
计费/审计）在同一个 API 级诊断上并存。**

## 6. 结论 / 数据源选型

- **Token 细分 UI 用 customMetrics**（`usage_ingest.py` 走 `query_resource` + customMetrics）：
  cached + 9 类 token + 维度，且本项目架构下流式也精确。
- **LlmLog 保留并存**：作计费口径的独立交叉核对 / 审计源（90 天、Log Analytics）。
- 两者不是二选一 —— **必须让 API 级诊断同时带 `metrics` 和 `largeLanguageModel`**。

## 7. 精度验证记录（a10 实测）

| 场景 | customMetrics | 上游真实值 | 结果 |
| --- | --- | --- | --- |
| 非流式 prompt/completion/total | 60 / 53 / 113 | 60 / 53 / 113 | ✅ |
| 流式（policy 注入 include_usage） | 含上 | 一致 | ✅ |
| cached #1 | 3072 | 3072（3×1024） | ✅ |
| cached #2（独立验证） | 6912 | 6912（3×2304） | ✅ |

> 注：Azure OpenAI prompt cache 门槛 ≥1024 token，短 prompt 不触发缓存（cached=0 是正常，非丢数据）。

## 8. 参考

- [Log token usage, prompts, and completions for language model APIs（LLM logs / ApiManagementGatewayLlmLog）](https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-llm-logs)
- [emit-metric policy（自定义指标前提：启用 App Insights logging + custom metrics with dimensions）](https://learn.microsoft.com/en-us/azure/api-management/emit-metric-policy)

