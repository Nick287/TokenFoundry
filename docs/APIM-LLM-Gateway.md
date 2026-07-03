# Azure APIM 作为大模型网关 —— 讨论与决策笔记

> 整理自与助手的讨论，事实点已对照微软官方文档源（`MicrosoftDocs/azure-docs`、`MicrosoftDocs/azure-ai-docs`）核对。
> 日期：2026-07-02。具体策略名/字段/GA 状态仍在迭代，落地前以最新官方文档为准。

## TL;DR（最重要的结论）

1. **APIM 能做大模型网关**，微软官方称之为 **AI Gateway / GenAI Gateway**，是现有 API 网关的扩展，不是独立产品。
2. **后端池（Backend Pool）负载均衡**是原生 GA 功能：支持 **round-robin / weighted / priority / session-aware**，配合熔断可聚合多个实例、突破单实例 TPM 上限。
3. **⚠️ 概念纠正（关键）**：APIM 的 **语义缓存（semantic cache）** 缓存的是"**整段问答的最终答案**"，命中即**跳过大模型**返回旧答案 —— 适合 FAQ/重复问答，**不适合多轮聊天**（几乎不命中，误命中会串答案）。
4. **多轮聊天/长上下文省钱**要的是另一套机制：**Azure OpenAI 自带的 Prompt Caching**（前缀缓存），**默认开启、无需配置**，对重复的输入前缀打折计费。
5. 对"连续聊天"场景的推荐组合：**APIM 同构后端池 + session-aware 会话粘性 + token 计量**，省钱靠**下游 Prompt Caching + 稳定的 prompt 前缀**；**不要开 APIM 语义缓存**。

---

## 1. APIM AI Gateway 能力总览

管理的 AI 端点需符合以下 schema 之一：

- **OpenAI Chat Completions / Responses API**
- **Anthropic Messages API**（目前仅 API Management **v2 层**支持）
- **Google Vertex AI API**

模型可部署在 Microsoft Foundry 或第三方（如 Amazon Bedrock）。另有 **Unified model API (preview)**：把多个后端通过单一 OpenAI 兼容端点暴露、自动格式转换、策略只配一次（想混多家 provider 时用）。

### 两套策略族（重要区分）

| 能力 | Azure OpenAI 专用 | 通用（OpenAI 兼容，**混第三方用这套**） |
| --- | --- | --- |
| Token 限流 | `azure-openai-token-limit` | **`llm-token-limit`** |
| Token 计量 | `azure-openai-emit-token-metric` | **`llm-emit-token-metric`** |
| 语义缓存查 | `azure-openai-semantic-cache-lookup` | **`llm-semantic-cache-lookup`** |
| 语义缓存存 | `azure-openai-semantic-cache-store` | **`llm-semantic-cache-store`** |
| 内容安全 | — | `llm-content-safety` |

---

## 2. 后端池负载均衡与熔断（Resiliency）

- **负载均衡模式**：round-robin、weighted、priority-based、**session-aware（会话粘性）**。
- **熔断（Circuit Breaker）**：支持动态 trip duration，会读取后端返回的 **`Retry-After`** 头，实现精准恢复。
- **Session awareness**：设置 session ID cookie，把同一会话的请求粘到**同一后端实例**。对 AI 聊天助手特别有用 —— 因为 **Prompt Cache 不跨实例共享**，粘住 = 命中率更高 = 更省。

> 池（type=Pool）用 Bicep/ARM/REST 定义，policy 里用 `set-backend-service backend-id="..."` 引用。

完整的 Bicep/policy 示例与硬约束详见下方 **§5 Backends 深入**。

---

## 3. ⚠️ 语义缓存 vs Prompt Caching（核心区别，别混）

| 维度 | **APIM 语义缓存** (`llm-semantic-cache-*`) | **Azure OpenAI Prompt Caching**（想省聊天上下文钱用这个） |
| --- | --- | --- |
| 缓存的是 | 整段问答的**最终答案** | 输入 token 的**前缀计算结果** |
| 命中后 | **跳过大模型**，返回旧答案 | **照常调大模型**，重复前缀**打折计费** |
| 省的是 | 命中时省整次调用 | 每次省重复输入部分 |
| 适合 | FAQ、重复问同样问题 | **多轮对话、长 system prompt** |
| 对聊天场景 | ❌ 几乎不命中；误命中会返回错答案 | ✅ 天生为长对话设计 |
| 触发/配置 | 需外部 Redis + embeddings 后端 + 策略 | **默认开启，无需配置，不可关闭** |

### 3a. APIM 语义缓存工作方式

```text
请求 → 算 embedding 向量 → 与历史 prompt 比对
  ├─ 够接近(命中) → 返回上次存的答案，不碰大模型
  └─ 不够接近      → 调大模型 → 存入缓存
```

- 需 **Azure Managed Redis** 或兼容 **RediSearch** 的外部缓存；embeddings 后端认证须 `system-assigned`。
- **`score-threshold` 是"距离/差异"阈值，越低越严格**（不是相似度！）。官方建议 **从 0.05 起步**，**> 0.2 易误命中**。
- `vary-by` 可分区缓存（如按 `context.Subscription.Id` 做跨用户隔离）。

### 3b. Azure OpenAI Prompt Caching（你要的省钱机制）

- **触发**：prompt ≥ **1024 token** 且**开头前缀逐字符一致**；此后每多 **128** 个相同 token 再命中一段。
- **计费**：命中的 `cached_tokens` 按输入价**打折**；**Provisioned(PTU) 最高输入全免**。
- **观测**：响应 `usage.prompt_tokens_details.cached_tokens` = 本次省下的 token 数。
- **保留期**：默认 in-memory（5~10 分钟不活动清除，最长 1 小时）；新模型可设 `prompt_cache_retention: "24h"`。
- **提高命中率**：
  1. 稳定内容（system prompt、few-shot、历史消息）放**最前面**，每轮新问题放**最后**。
  2. 前缀里**别放**时间戳/随机 ID/用户名等每轮变化的内容。
  3. 可传 `prompt_cache_key`（同用户/会话同值）提升路由命中。

---

## 4. 针对"连续聊天"场景的推荐架构

```text
Client ──> APIM (AI Gateway)
             ├─ llm-token-limit           # 治理/限流（可选）
             ├─ set-backend-service pool   # 同构 Azure OpenAI 多实例池
             │    └─ sessionAffinity       # 会话粘性 → 提升 prompt cache 命中
             ├─ retry (429/5xx → 换实例)
             └─ llm-emit-token-metric      # 计量，含 cached_tokens 维度
                   │
                   └──> Azure OpenAI 实例们
                          └─ Prompt Caching（默认开，自动省输入 token）
```

- **同构池**（同模型、同 API 形态）→ 缓存等价性问题消失，原生 Pool 直接可用。
- **省钱** = 下游 Prompt Caching（自动）+ 稳定 prompt 前缀 + 会话粘性。
- **不要开** APIM 语义缓存（聊天场景有害）。

### 待定决策（继续时确认）

1. 后端是 **多 Azure OpenAI 实例** 还是 **单实例**？（多实例注意同订阅同区域配额可能共享，需跨区或申配额）
2. **流式(SSE)** 还是 **非流式**？

### ⚠️ 流式(SSE)对 token 计量的影响

- `llm-token-limit` 在 `stream: true` 时 **prompt 和 completion token 全部为估算值**（非精确），图片输入按最多 **1200 token/张** 高估。
- 要精确计量：用非流式，或客户端带 `stream_options.include_usage`。

---

## 5. Backends 深入（来自 backends.md 官方文档）

### 5.1 概念与用途

**Backend（后端实体）** 封装后端服务信息，可跨 API 复用、便于治理。导入 Microsoft Foundry / AI 服务等 API 时，APIM **会自动创建 backend 实体**。用途：授权后端凭据、用 Key Vault 维护密钥、定义熔断规则、路由/负载均衡到多个后端。

- **引用方式**：`set-backend-service backend-id="myBackend"`；也可用 `base-url`（形如 `https://backend.com/api`，**结尾别加斜杠**）。
- **自动匹配**：运行时若某 backend 实体的 URL 与请求目标匹配，APIM **会自动使用它，无需显式 `set-backend-service`**。
- **条件路由**：可用 `<choose>` 按网关/位置/表达式切换后端（见 5.5）。

### 5.2 ⚠️ 硬约束（务必先看）

| 约束 | 说明 |
| --- | --- |
| 池后端上限 | **一个池最多 30 个后端** |
| 熔断规则数 | **每个后端只能配 1 条熔断规则** |
| 熔断层级 | **Consumption 层不支持**熔断 |
| 近似性 | 负载均衡与熔断都是**近似的** —— 网关多实例间**不同步**，各自基于本实例信息判断 |
| 优先级组语义 | **只有高优先级组全部后端因熔断不可用时**，才使用低优先级组 |
| CA 证书 | 在 backend 实体里配自定义 CA 证书**仅 v2 层**支持 |
| VNet 自链 | Developer/Premium 内部 VNet 下，网关 URL 与后端 URL 相同会报 `500 BackendConnectionFailure` |

### 5.3 🔴 Azure OpenAI + 熔断（关键提醒）

Azure OpenAI 过载时返回 `429 Too Many Requests`，其 **`Retry-After` 头的值可能非常大（例如 1 天）**。因此对 Azure OpenAI 后端**必须配熔断规则**来处理 429 并接受 `Retry-After`（`acceptRetryAfter: true`），否则实例会被长时间"卡死"。熔断跳闸时，APIM 对客户端返回 `503 Service Unavailable`，trip duration 过后自动恢复。

熔断 Bicep 示例（3 次 5xx / 1 小时触发，熔断 1 小时，接受 `Retry-After`）：

```bicep
resource be 'Microsoft.ApiManagement/service/backends@2023-09-01-preview' = {
  name: 'myAPIM/myBackend'
  properties: {
    url: 'https://mybackend.com'
    protocol: 'http'
    circuitBreaker: {
      rules: [
        {
          name: 'myBreakerRule'
          failureCondition: {
            count: 3
            interval: 'PT1H'
            statusCodeRanges: [ { min: 500, max: 599 } ]
            errorReasons: [ 'Server errors' ]
          }
          tripDuration: 'PT1H'
          acceptRetryAfter: true   // 关键：读取后端 Retry-After
        }
      ]
    }
  }
}
```

> 对 Azure OpenAI，`statusCodeRanges` 通常应覆盖 `429`（例如 `{ min: 429, max: 429 }`）以处理配额限流。

### 5.4 负载均衡池（Bicep，含 weight / priority / sessionAffinity）

三种模式：**Round-robin**（默认均分）、**Weighted**（按权重，适合蓝绿/金丝雀）、**Priority-based**（分组，先高优先级组，组内再按权重）。任一模式都可叠加 **session awareness**。

```bicep
resource pool 'Microsoft.ApiManagement/service/backends@2023-09-01-preview' = {
  name: 'myAPIM/myBackendPool'
  properties: {
    description: 'Load balancer for multiple backends'
    type: 'Pool'
    pool: {
      services: [
        { id: '/subscriptions/.../backends/backend-1', priority: 1, weight: 3 }
        { id: '/subscriptions/.../backends/backend-2', priority: 1, weight: 1 }
      ]
      sessionAffinity: {          // 可选：会话粘性
        sessionId: { source: 'Cookie', name: 'SessionId' }
      }
    }
  }
}
```

### 5.5 set-backend-service 条件路由示例

```xml
<inbound>
  <base />
  <choose>
    <when condition="@(context.Deployment.Gateway.Id == "factory-gateway")">
      <set-backend-service backend-id="backend-on-prem" />
    </when>
    <when condition="@(context.Deployment.Gateway.IsManaged == false)">
      <set-backend-service backend-id="self-hosted-backend" />
    </when>
    <otherwise />
  </choose>
</inbound>
```

### 5.6 Session awareness 的 cookie 处理

启用会话粘性后，**客户端必须自己处理 cookie**：存下 `Set-Cookie` 值并在后续请求带上。对 Assistants API 这类场景，可在 `outbound` 用 policy 从响应体取 `thread id` 拼进 cookie 的 `Path`：

```xml
<outbound>
  <base />
  <set-variable name="gwSetCookie" value="@{
    var payload = context.Response.Body.As<JObject>(preserveContent: true);
    var threadId = payload["id"];
    var v = context.Request.Headers.GetValueOrDefault("Set-Cookie", string.Empty);
    if(!string.IsNullOrEmpty(v)) { v = v + $";Path=/threads/{threadId};"; }
    return v;
  }" />
  <set-header name="Set-Cookie" exists-action="override">
    <value>@((string)context.Variables["gwSetCookie"])</value>
  </set-header>
</outbound>
```

### 5.7 后端认证凭据（Authorization credentials）

backend 实体可配置：**请求头** / **查询参数** / **客户端证书（mTLS）** / **托管标识（Managed Identity）**。

**托管标识授权 Azure OpenAI**（推荐，免密钥）：

- Client identity：System-assigned 或 user-assigned。
- **Resource ID 填 `https://cognitiveservices.azure.com`**。
- **需给该托管标识分配 `Cognitive Services User` RBAC 角色**。

### 5.8 context.Backend 变量

策略里可读 `context.Backend` 属性：

| 属性 | 说明 |
| --- | --- |
| `Id` | 后端实体资源标识 |
| `Type` | 后端类型：`Single` 或 `Pool` |
| `AzureRegion` | 后端区域（若指定） |

```xml
<set-header name="X-Backend-Type" exists-action="override">
  <value>@(context.Backend?.Type ?? "n/a")</value>
</set-header>
```

> 提示：熔断跳闸/恢复会产生 **Event Grid 事件**，可订阅以便在后端问题升级前预警。

---

## 6. 参考链接

### APIM AI Gateway

- [AI gateway capabilities in Azure API Management](https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities)
- [API Management backends — 负载均衡 & 会话粘性 & 熔断](https://learn.microsoft.com/en-us/azure/api-management/backends?tabs=portal)
- [set-backend-service policy](https://learn.microsoft.com/en-us/azure/api-management/set-backend-service-policy)
- [llm-token-limit policy](https://learn.microsoft.com/en-us/azure/api-management/llm-token-limit-policy)
- [llm-emit-token-metric policy](https://learn.microsoft.com/en-us/azure/api-management/llm-emit-token-metric-policy)
- [llm-semantic-cache-lookup policy](https://learn.microsoft.com/en-us/azure/api-management/llm-semantic-cache-lookup-policy)
- [llm-semantic-cache-store policy](https://learn.microsoft.com/en-us/azure/api-management/llm-semantic-cache-store-policy)
- [Enable semantic caching for LLM APIs in APIM](https://learn.microsoft.com/en-us/azure/api-management/azure-openai-enable-semantic-caching)
- [Set up an external cache in APIM](https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-cache-external)
- [Unified model API (preview)](https://learn.microsoft.com/en-us/azure/api-management/unified-model-api)
- [authentication-managed-identity policy](https://learn.microsoft.com/en-us/azure/api-management/authentication-managed-identity-policy)

### Azure OpenAI Prompt Caching

- [Prompt caching with Azure OpenAI](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/prompt-caching)
- [Provisioned throughput (PTU)](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/provisioned-throughput)

### 参考架构与示例

- [AI gateway reference architecture using API Management](https://learn.microsoft.com/en-us/ai/playbook/technology-guidance/generative-ai/dev-starters/genai-gateway/reference-architectures/apim-based)
- [Use a gateway in front of multiple Azure OpenAI deployments](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/azure-openai-gateway-multi-backend)
- [Blog: APIM circuit breaker + load balancing with Azure OpenAI](https://techcommunity.microsoft.com/blog/fasttrackforazureblog/using-azure-api-management-circuit-breaker-and-load-balancing-with-azure-openai-/4041003)
- [Quickstart: Create a Backend Pool with Bicep to load balance OpenAI](https://github.com/Azure-Samples/apim-lbpool-openai-quickstart)
- [Azure-Samples/ai-gateway（可运行 labs）](https://github.com/Azure-Samples/ai-gateway)
- [AI hub gateway landing zone accelerator](https://github.com/Azure-Samples/ai-hub-gateway-solution-accelerator)
- [Smart load balancing for OpenAI endpoints（博客）](https://techcommunity.microsoft.com/blog/fasttrackforazureblog/%F0%9F%9A%80-smart-load-balancing-for-openai-endpoints-and-azure-api-management/3991616)
- [Azure/azure-api-management-policy-toolkit](https://github.com/Azure/azure-api-management-policy-toolkit/)
</content>
