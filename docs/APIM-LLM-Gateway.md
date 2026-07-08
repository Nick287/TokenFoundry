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
- **熔断（Circuit Breaker）**：支持动态 trip duration，会读取后端返回的 **`Retry-After`** 头，实现精准恢复。本项目配置为 **`429`（上游 TPM 限流）或 `5xx` 一次即熔断 60 秒**，把该 hub 从 pool 摘除、请求 failover 到其他 hub（详见 **§5.3**）。
- **Session awareness**：设置 session ID cookie，把同一会话的请求粘到**同一后端实例**。对 AI 聊天助手特别有用 —— 因为 **Prompt Cache 不跨实例共享**，粘住 = 命中率更高 = 更省。粘住的 hub 一旦 `429` 熔断，粘性失效、请求切到 pool 里别的 hub（牺牲一次缓存换可用性）。

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

## 4.5 每 key 独立限流（TPM + token-quota，per-subscription 动态取值）

> 2026-07-05 补充：目标是"创建虚拟密钥时可为**每把 key 独立设** TPM / token 配额"。
> 下面的 SKU 支持与表达式支持均**已核对官方文档**，`tokens-per-minute` 用策略表达式一项
> 更在 **dev-a01（Developer SKU）实测**确认。

### 4.5.1 结论（可落地）

- **每 key 独立"桶"早已实现**：策略 `counter-key="@(context.Subscription.Id)"` —— 虚拟密钥 ≙ APIM
  订阅，所以每把 key 各有独立计数器，**互不共享**（key A 打满不影响 B）。
- **缺的是"每 key 不同的限流数值"**：现状 `tokens-per-minute="50000"` 写死、`token-quota` 未配。
- **`llm-token-limit` 的三个数值属性都能按 key 动态取值**（见下表），所以"每 key 任意 TPM/配额"
  **技术可行、无 SKU 障碍**（Developer SKU 就支持）。

### 4.5.2 SKU 支持（APPLIES TO）与策略表达式支持 —— 已核对官方文档

| 策略 | APPLIES TO（服务层级） | 数值属性能否用策略表达式 `@()` |
| --- | --- | --- |
| **`llm-token-limit`** | Developer / Basic / **Basic v2** / Standard / **Standard v2** / Premium / **Premium v2** | `counter-key` ✅ · `token-quota` ✅ · `token-quota-period` ✅ · `tokens-per-minute` ✅（**dev-a01 实测**，文档未标但 APIM 接受） |
| `rate-limit` | **All tiers** | 数值 ❌（product 级，写死） |
| `rate-limit-by-key` | Developer / Basic / Basic v2 / Standard / Standard v2 / Premium / Premium v2 | `calls` ✅ · `renewal-period` ✅ |
| `quota` | **All tiers** | 数值 ❌（product 级，写死） |
| `quota-by-key` | **Developer / Basic / Standard / Premium**（⚠️ 无 v2 / 无 Consumption） | `calls` ✅ · `bandwidth` ✅ · `renewal-period` ✅ |
| `limit-concurrency` | All tiers | `max-count` ❌ |
| `ip-filter` | All tiers | address ✅ |

**规律**：`-by-key` 系列（含 `llm-token-limit`）为"每调用方不同限额"而设计，**数值属性支持表达式**；
product 级的 `rate-limit` / `quota` 数值写死、不支持表达式。这正是我们要"每 key 不同值"该用
`-by-key` / `llm-token-limit` 而非 product 级策略的原因。

> ⚠️ `quota-by-key` 的 SKU 比其他窄（**无 v2、无 Consumption**）。我们**不用它**——用
> `llm-token-limit` 的 `token-quota`（SKU 覆盖更全，且和 TPM 同一策略）。此行仅作备选知识。

### 4.5.3 dev-a01 实测记录（2026-07-05，Developer SKU）

问题：`llm-token-limit` 的 `tokens-per-minute` 文档**未标注**支持策略表达式（其余标了的都明写
"Policy expressions are allowed"），需实测确认能否"每 key 动态取值"。

方法：建隔离临时 API `tpm-expr-test`，用 ARM REST PUT 策略，测完即删（**不碰生产三个
`llm-*` API**）。

| 测试的 `tokens-per-minute` 值 | 结果 |
| --- | --- |
| `@(1000+500)`（简单表达式） | ✅ HTTP 201 接受 |
| `@{ 解析 JSON 映射 → 按 context.Subscription.Id 取 tpm }`（**贴近真实方案**）| ✅ HTTP 200 接受 |

结论：**APIM 接受 `tokens-per-minute` 用策略表达式**，包括"解析映射 + 读 `context.Subscription` +
类型转换"的真实形态。策略编译器完整校验通过。→ 方案里唯一的技术不确定点**消除**。

### 4.5.3b ⚠️ 实现期实测修正（2026-07-05，逐属性隔离测试）

上面 4.5.3 只测了 `tokens-per-minute`。**实现时逐属性隔离测试，发现 `llm-token-limit` 三个
数值属性的表达式支持并不一致**——这颠覆了 §4.5.2 基于文档的判断（文档说 `token-quota`
"Policy expressions are allowed"，但**实测在 `llm-token-limit` 上不成立**）：

| 属性 | 实测 | 证据（dev-a01 隔离 API PUT policy） |
| --- | --- | --- |
| `tokens-per-minute` | ✅ 接受 `@(int)` 表达式 | `@(5000)` → HTTP 200；`@("5000")`(string) → 400 |
| `token-quota` | ❌ **只接受字面量** | `@(5000)` → **400 "return type System.Int32 is not allowed"**；`5000`(字面量) → 201 |
| `token-quota-period` | ✅ 接受 `@(string)` 表达式 | `@("Daily")` → 200 |

**结论**：`token-quota` **不能**用"共享策略 + named value 表达式"实现每 key 任意值。
**可行的替代（已实测 HTTP 201）**：用 `<set-variable>` 从 named value 读该 key 的 quota
**档位**，再用 `<choose>` 按档走不同的、**写死字面量** `token-quota` 的 `llm-token-limit`
分支。即 quota 是**固定几档**（非任意值）；`tokens-per-minute` 仍任意值（表达式）。

另注：策略里引用 named value `{{tf-key-token-limits}}` 时，**该 named value 必须先存在**，
否则 APIM 校验策略即报 "Cannot find a property"。所以 provisioning 时须先确保 named value
存在（空 `{}` 亦可）再推策略。

### 4.5.4 设计（每 key 动态限流 · 已按实测修正）

- **数据源**：虚拟密钥表已有 `apim_subscription_id`（= 策略里的 `context.Subscription.Id`），
  作为"key → 限额"的锚点。新增字段：`tokens_per_minute`（任意值）、`token_quota`（**固定档位**）、
  `token_quota_period`（枚举 Hourly/Daily/Weekly/Monthly/Yearly）。
- **取值机制**：把 `{subId:{t,quota_tier,p}}` 映射存进 **APIM named value**；一份**共享**的
  API 级策略：`tokens-per-minute` 用 `@()` 表达式动态取（任意值）；`token-quota` 用 `<choose>`
  按档位分支（写死字面量）。签发/改 key 时更新 named value，删除 key 时移除该条。
- **速率 vs 总量（别混）**：`tokens-per-minute` = 每分钟峰值（429，窗口恒为 1 分钟、无周期）；
  `token-quota` + `token-quota-period` = 周期累计（403，到期重置）。二者可各自独立启用。
  `token-quota` 与 `token-quota-period` **必须成对**（APIM 要求）。
- **与美元预算的关系**：key 上旧的 `monthly_budget_usd`/`budget_action` 是**只存不用的死字段**
  （`budget_enforcer.py` 读的是独立的 `Budget` 表，不读 key 字段），随本改造删除；独立的
  `Budget` 表 + `budget_enforcer`（美元事后对账）**保留不动**。

策略骨架（示意，`{{tf-key-limits}}` 为 named value）：

```xml
<inbound>
  <base />
  <set-backend-service backend-id="..." />
  <set-variable name="tfLimits" value="@{
      var map = Newtonsoft.Json.Linq.JObject.Parse("{{tf-key-limits}}");
      var sid = context.Subscription?.Id ?? "none";
      return map[sid] != null ? map[sid].ToString() : "{}";
  }" />
  <llm-token-limit
      counter-key="@(context.Subscription?.Id ?? "anon")"
      tokens-per-minute="@{ var o=Newtonsoft.Json.Linq.JObject.Parse((string)context.Variables["tfLimits"]); return o["tpm"]!=null ? (int)o["tpm"] : 50000; }"
      token-quota="@{ var o=Newtonsoft.Json.Linq.JObject.Parse((string)context.Variables["tfLimits"]); return o["quota"]!=null ? (int)o["quota"] : 0; }"
      token-quota-period="@{ var o=Newtonsoft.Json.Linq.JObject.Parse((string)context.Variables["tfLimits"]); return o["period"]!=null ? (string)o["period"] : "Monthly"; }"
      estimate-prompt-tokens="false" />
</inbound>
```

> 注：`token-quota="0"` 需按"未设置"处理（不下发该属性），否则会把配额锁成 0。实现时对
> quota/period 缺失的 key 应**省略**这两个属性，而非填 0。

---

## 4.6 SKU 支持矩阵 —— 我们**实际依赖**的 APIM 能力 × 服务层级

> 2026-07-05 补充。上面 §4.5.2 那张表是"限流策略族"的通用对照；这一节是**针对本项目实际用到
> 的每一个 APIM 能力**逐个核实 SKU 支持——直接作为选生产层级的依据。能力清单从
> `app/services/apim_provisioner.py` 实际调用提取，SKU 结论对照官方 backends.md /
> genai-gateway-capabilities / 各策略文档。

### 4.6.1 能力 × SKU 表

| 我们用到的能力 | 代码位置 | SKU 支持 | Developer 可用 | Consumption |
| --- | --- | --- | --- | --- |
| `llm-token-limit`（限流；将来 per-key TPM/quota） | `apim_provisioner.py:334` | Dev/Basic/Std/Premium + **全部 v2** | ✅ | —（见下） |
| `llm-emit-token-metric`（token 计量 → App Insights） | :338 | 同上 | ✅ | — |
| **负载均衡后端池**（3 个 provider 池，架构核心） | :526,624 | GA，非 Consumption 均支持 | ✅ | ❌ |
| `sessionAffinity` 会话粘性（prompt-cache 保温） | :627 | 随池（preview ARM API） | ✅ | ❌ |
| **熔断 Circuit Breaker**（每后端 1 条规则） | :281,478 | **Consumption 明确不支持** | ✅ | ❌ |
| `authentication-managed-identity`（MI 写 Cosmos） | :342 | 非 Consumption | ✅ | 受限 |
| `send-one-way-request`（Cosmos 用量即发即忘） | :350 | 全层 | ✅ | ✅ |
| 后端 header 凭据（注入 hub key 作后端认证） | :280,474 | 全层 | ✅ | ✅ |

### 4.6.2 结论：当前实现锁定"非 Consumption 层"，经典 ↔ v2 可迁移

- ✅ **完全可用（现状 + 生产推荐）**：**Developer / Basic / Standard / Premium**（经典层）——
  我们用的每一个能力都支持。dev-a01 = Developer，全部实测正常。生产升 Standard/Premium **零改代码**。
- ⚠️ **Consumption 层被排除**（两处会坏）：
  1. **熔断不支持**（backends.md 原文："circuit breaker isn't supported in the Consumption
     tier"）——我们每个后端都配了熔断（处理 Azure OpenAI 的超大 `Retry-After`，见 §5.3）。
  2. **后端池 + sessionAffinity** 在 Consumption 也受限。
  → 但 Consumption 本就不适合生产 LLM 网关（无 SLA、冷启动），排除它无损失。
- 🔶 **迁移路径**：Developer（测）→ Standard / Premium（生产）无缝；若要 v2 层（Basic/Standard/
  Premium **v2**）用到的策略也都覆盖，同样可行。

### 4.6.3 🔶 已知隐患：Anthropic 原生 API 仅 v2 层

genai-gateway-capabilities 文档明确：**"Anthropic Messages API (currently supported in API
Management v2 tiers)"**。而 dev-a01 是 **Developer（经典层，非 v2）**。

- **为什么我们的 `llm-anthropic` 池现在能跑**：我们**没有**用 APIM 原生的"Anthropic API 类型"，
  而是把 Claude 当**普通 HTTP 后端**（经 GitModel hub 转发），套通用 `llm-token-limit` 策略——
  **绕过了那个 v2 限制**。
- **代价（取舍，非 bug）**：没吃到 APIM 对 Anthropic 的**原生**支持（原生 token 计数/schema 校验
  等）。功能正常，计量走 hub 返回值 + `llm-emit-token-metric` 估算。
- **何时需要处理**：若将来要 APIM 原生解析 Anthropic 请求/精确原生计费，需上 **v2 层**并改用
  原生 Anthropic API 类型；否则当前"通用后端"方式在所有经典层都通用，无需动。

### 4.6.4 一句话给决策者

> 选 **Standard 或 Premium（经典层）** 上生产：本项目所有 APIM 能力都支持，从 Developer 平滑升级、
> 零代码改动。**别选 Consumption**（熔断/池受限）。Anthropic 走通用后端，无需 v2；除非要 APIM
> 原生解析 Anthropic 才需上 v2 层。

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

### 5.3 🔴 熔断：上游 429 自动 failover（本项目实际配置）

**问题**：hub / Copilot 上游 TPM 打满时返回 `429`。若熔断只认 `5xx`（早期配置），这个 backend 会被当成"健康"，而 session affinity 又把同一会话粘在它上面 —— 结果是**一直 429 卡死、不切换**。Azure OpenAI 更糟：其 `Retry-After` 头的值可能非常大（例如 1 天），不接受 `Retry-After` 会让实例长时间"卡死"。

**本项目的解**：熔断规则同时覆盖 `429` 和 `5xx`，**一次**触发就把该 backend 熔断 **60 秒**，从 pool 里临时摘除 —— 请求随即 **failover 到同一 provider pool 里的其他 hub**（牺牲那个 hub 的 prompt cache 保温，换可用性）。

链路：`session 粘在 hub-A → A 上游 429 → 熔断 60s → 粘性失效 → 切到 pool 里的 hub-B`。60 秒 ≈ provider TPM 窗口刷新周期；同一快跳闸也适合 5xx（坏后端应尽快摘除，而非硬打一小时）。

**两个关键前提（务必理解）**：

1. **failover 需要 pool 里 ≥2 个 hub**。若某 provider pool 只有 1 个 hub（如刚接入 1 个 GitHub 账号时），该 hub 熔断后 pool 无成员可切，客户端收到 **`503 Service Unavailable`**，等 60 秒 trip 结束自动恢复。多 hub 才有真正的 failover。
2. **只熔"上游 429"，不熔"我们自己的 per-key 限流 429"**。per-key `llm-token-limit` 在 **inbound 阶段**拦截，请求**根本不到 backend** —— 熔断只统计 backend 的响应，因此一个 key 打爆自己的额度**不会**误熔断整个共享 hub、连累别的 key。这个隔离是**架构天然的**，无需额外判断。

> ⚠️ **Azure 硬限制：每个 backend 只能配 1 条熔断规则。** 但一条规则的 `failureCondition.statusCodeRanges` **可以列多个范围**，所以 `429` 与 `5xx` 合进同一条（共用 `count` / `interval` / `tripDuration`）。想给两者不同的触发阈值是做不到的。

熔断 Bicep 示例（**本项目实际值**：429 或 5xx，**1 次**触发，熔断 **60 秒**，接受 `Retry-After`）：

```bicep
resource be 'Microsoft.ApiManagement/service/backends@2023-09-01-preview' = {
  name: 'myAPIM/myBackend'
  properties: {
    url: 'https://mybackend.com'
    protocol: 'http'
    circuitBreaker: {
      rules: [
        {
          name: 'trip-on-429-or-5xx'
          failureCondition: {
            count: 1                       // 一次就熔断（429 是瞬时限流）
            interval: 'PT1M'
            statusCodeRanges: [            // 一条规则里列多个范围
              { min: 429, max: 429 }        // 上游 TPM 限流 → failover
              { min: 500, max: 599 }        // 后端不健康
            ]
          }
          tripDuration: 'PT1M'             // 熔断 60 秒，随即恢复
          acceptRetryAfter: true           // 关键：读取后端 Retry-After
        }
      ]
    }
  }
}
```

> 代码里由 `ApimProvisioner._breaker_rules()` 生成，单后端与所有 pool 成员共用同一规则（`app/services/apim_provisioner.py`）。

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
