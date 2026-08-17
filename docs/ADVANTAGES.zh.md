# Token Foundry —— 方案优势说明

> 面向技术决策者 · 基于 2026-08 的实际部署（新加坡）与实测数据
> 当前环境 dev-19；文中标注 dev-15 / dev-16 / dev-17 / dev-18 的数字是各自环境上的
> 原始测量，环境虽已回收，但四套均由同一份 terraform 建出、规格一致，故结论沿用。
> 本文所有性能与成本数字均来自仓库内的实测记录，出处逐条标注。
>
> ⚠️ **但请把这些数字读成"评估依据"，不是"承诺值"。** 所有实测都跑在
> **合作伙伴环境的测试 GitHub 账号**上。GitHub 账号本身的容量是**黑盒**：
> 上游不公布每账号配额，触顶时只返回一个不带 `Retry-After`、不带余量的 429，
> 而且同一批账号、同一套参数，实测结果本身就是双峰的（72 并发三次跑出
> 44% / 0% / 40% 失败率）。**容量与成本的最终结论，必须在客户自己的订阅、
> 客户自己的 GitHub 账号上实测得出**——本文提供的是方法、形状与可复现脚本。
> 完整说明见 [CAPACITY.zh.md §0](CAPACITY.zh.md#0-️-这些数字的适用范围先读这一节)。

---

## 一句话

**一套部署在 Azure 上、由你自己掌控的 LLM 网关**：对外提供 Anthropic / OpenAI / Google
三家的**原生 API 格式**，对内用一组可自由增删的算力后端做负载均衡，
token 用量与成本按租户、按项目、按终端用户逐次计量。

客户拿到的不是一个"代理地址"，而是一个**可运营的 token 中枢**——
有配额、有分账、有仪表盘、有容量规划公式。

---

## 架构

![Token Foundry 架构](architecture.png)

三层，职责边界清晰：

| 层 | 组件 | 职责 |
|---|---|---|
| **数据平面** | Azure API Management（v2） | 鉴权、限流、路由、负载均衡、熔断、注入调用方身份 |
| **算力层** | GitModel hub（每账号一套 Container App） | 转发上游、原样回报用量事件 |
| **控制平面** | FastAPI + React（单个 Container App） | 开通租户/密钥、配置网关、导入用量、呈现报表 |

> **唯一不变量：控制平面绝不介入请求路径。** LLM 流量只走
> `客户端 → APIM → hub → 上游`。控制平面发版、重启、甚至挂掉，都不影响正在跑的推理请求。
> 这是把「运营系统」和「生产链路」解耦的关键——多数自建网关方案在这里是耦合的。

详见 [architecture.zh.md](architecture.zh.md)。

---

## 八项核心优势

### 1. 纯血 Claude —— 原生 Messages API，不是翻译层

大量"多模型网关"的做法是把一切转成 OpenAI Chat Completions 格式再转回去。
代价是**信息损耗**：流式响应下的 `input_tokens` 拿不到、cache token 明细丢失、
extended thinking 的 token 计数失真、`tool_use`/`tool_result` 的往返容易走样。

Token Foundry 走的是**原生透传**：`POST /v1/messages` 直达上游 Anthropic 端点，
请求与响应结构一字不改。

- **精确性**：流式场景下 `input_tokens` 是真实值，且 cache 读写、thinking token 各列齐全
  （[anthropic_adapter.py:1-18](../vendored/gitmodel-hub/hub/anthropic_adapter.py#L1-L18) 记录了从转译切换到原生透传的原因）
- **零改造接入**：APIM 的 `llm-anthropic` API 沿用 Anthropic **原生的 `x-api-key` 请求头**，
  客户端只改一个 `ANTHROPIC_BASE_URL`，SDK / Claude Code / 现有代码一律不动
- **模型名兼容**：官方模型 ID（`claude-sonnet-4-5-20250929` 这类带日期的）自动映射，
  不认识的名字原样透传——上游上新模型不需要改代码

**可用模型**：Claude **Opus 5 等最新旗舰**，以及 Opus 4.8 / 4.7 / 4.6 / 4.5、
Sonnet 4.6 / 4.5、Haiku 4.5。实时清单以门户「可用模型」页为准。

### 2. 不只是 Claude —— 三家供应商，各走各的原生格式

| 供应商 | APIM 路径 | 鉴权头 | 代表模型 |
|---|---|---|---|
| Anthropic | `/llm-anthropic/v1/messages` | `x-api-key` | Opus 5 等最新旗舰、Opus 4.8、Sonnet 4.6、Haiku 4.5 |
| OpenAI | `/llm-openai/v1/responses`<br/>`/llm-openai/v1/chat/completions` | `api-key` | gpt-5.5、gpt-5.4、gpt-5.4-mini、gpt-5.3-codex、gpt-4.1、gpt-4o 系列 |
| Google | `/llm-google/...` | `api-key` | gemini-3.5-flash、gemini-3.1-pro-preview、gemini-3-flash-preview、gemini-2.5-pro |

**每家一个独立的 APIM API，沿用各自原生的订阅密钥请求头。**
这意味着客户可以同时用 Anthropic SDK、OpenAI SDK、Codex、Claude Code
指向同一个网关，各自按自己习惯的方式工作，不需要学一套"我们自己的 API"。

清单见 [register_hub_models.py:40-76](../scripts/register_hub_models.py#L40-L76)。

### 3. 模型更新及时 —— 上新即接入，不用发版

上游放出新模型后，运营者在门户点一下「重新同步模型清单」，
控制平面就会从 hub 拉取当前可用模型列表并自动注册成路由
（[github_accounts.py:324](../app/api/github_accounts.py#L324) `resync-catalog`）。

**不需要改代码、不需要重新部署、不需要等我们发版本。**
这是"模型名未识别就原样透传"这个设计带来的直接收益。

### 4. 负载均衡自己可控 —— 是网关配置，不是应用代码

多个算力后端组成 **APIM 后端池**，策略全部声明在网关对象上：

- **会话粘性（session affinity）**：同一会话稳定落到同一后端。
  ⚠️ 但它**带不来缓存收益**——我们自己测过并写进了文档：Anthropic 的 prompt 缓存在
  **上游**、按**内容**寻址，既不属于某个后端也不属于某个账号，因此亲和收益实测
  **恒为零**（两套环境、六个账号复现）。保留它是为了会话级的连接稳定性，不要指望
  它省钱（[CAPACITY.zh.md](CAPACITY.zh.md) §4.3）
- **熔断**：某后端返回 `429` 或 `5xx`，立即摘除 60 秒并遵守 `Retry-After`，
  请求**自动 failover 到其它后端**
- **两种 429 分开处理**：上游过载的 429 触发熔断；我们自己给客户设的 per-key 限流 429
  在 inbound 就拦截，**不会**误伤后端池

对比自建代理：熔断、重试、分布式限流计数器、按供应商的 token 计量——
这些在 APIM 里是**配置**，在自建方案里是需要长期维护的代码。

**扩容也是配置**：APIM 通过增加 unit 横向扩展，支持按计划或负载自动伸缩，
单实例可跨多区域。扩容时控制平面、策略、路由逻辑一行都不用改。

### 5. 承载量可控可规划 —— 有公式，不是拍脑袋

容量与账号数**近似线性**，且有三轮独立环境的实测背书
（[CAPACITY.zh.md](CAPACITY.zh.md)，dev-15 / dev-16 / dev-17 / dev-19 四个环境结论一致）。

| 账号数 | 安全并发 | 突发上限 | 崩塌点 |
|---|---|---|---|
| 1 | **24** | 32 | 32–48 |
| 2 | **48** | —— | 72 |
| 3 | **48** | 72 | 96 |

**规划公式：可持续并发 ≈ 16–24 × 账号数。**

横向扩容的实测收益（gpt-4o-mini，`max_tokens=1200`）：

| 指标 | 1 账号 | 2 账号 | 3 账号 | 倍数 |
|---|---|---|---|---|
| 稳定 TPM | 41,602 | 88,057 | **158,961** | **3.8×** |
| 稳定 RPS | 4.63 | 9.82 | **17.83** | **3.9×** |
| p95 延迟 | ~6.2s | ~5–6s | **4.28s** | 反而更低 |

比吞吐提升更重要的是**失败模式的质变**：

| 账号数 | 超限时的行为 |
|---|---|
| 1 | 熔断后无处 failover → 整池 503，全挂 60 秒 |
| 2 | 429 出现但 **0 × 503** —— failover 生效 |
| 3 | 甜点区 **429 / 503 双零** |

**这就是"承载量在后台可控"的实际含义**：需要更大容量，门户里加一个账号即可，
容量按公式线性上抬，且可用性同时改善——不是"加机器碰运气"。

峰值能力参考：24 并发 × 100K prompt 实测跑到 **2,464 万 TPM 零错误**
（2026-08-09，dev-17）。上限不在我们这一侧。

> ⚠️ **上表的「安全并发」取的是保守侧，不是最好那次。** dev-19 上 72 并发连续三次
> 零错误，我们**仍未**把安全线从 48 上调——因为 dev-17 的同一档位跑出过
> 44% / 0% / 40% 的双峰。差别在**账号**不在代码，这正是开篇那条声明的实例。

### 6. 微软数据中心 —— 可靠性与合规是继承来的

全套 Azure 托管 PaaS，没有需要自己运维的虚拟机：

- **APIM v2** 网关，支持多区域部署与自动伸缩
- **托管身份（Managed Identity）全程无密钥**访问 Azure 资源；
  Cosmos DB 以 `disableLocalAuth` 仅 AAD 模式运行
- **上游密钥从不出现在数据路径上**：真实供应商密钥存在 Key Vault、绑定到 APIM 后端；
  客户端始终只持有一个按租户隔离的**虚拟密钥**，可独立挂起/吊销
- **PostgreSQL 只存引用，Key Vault 存所有密钥，Cosmos 只存虚拟密钥 id**——绝不存其值
- 网络出口在 Azure 骨干网内，跨区访问上游的稳定性远好于自建线路

详见 [SECURITY.zh.md](SECURITY.zh.md)。

### 7. 部署自动化 —— 一条命令起一套环境

```bash
az login && az account set --subscription <id>
./scripts/bootstrap.sh -g tokenfoundry-rg-<env>
```

一次 `terraform apply` + 并行 `az acr build` 构建两个镜像，
结尾自动做 `/healthz` 冒烟测试。

- **APIM v2 SKU 约 1–2 分钟建成**（经典 tier 需要 30–45 分钟）——整套环境分钟级可用
- **Terraform 覆盖全部 PaaS**，state 按环境用 workspace 隔离，
  资源名由资源组 id 派生（`substr(md5(rg.id), 0, 13)`），新环境永不与旧环境撞名，
  连软删除的 Key Vault / APIM 残留都不撞
- **一个镜像、一个 Container App**：API 与构建好的 React SPA 打进同一镜像（无需 nginx sidecar），
  日常应用更新只需 `./scripts/update-app.sh` —— 重新构建 + 滚动修订版，跳过 Terraform
- 后续两步在**门户里点点完成**，不需要 shell

详见 [DEPLOYMENT.zh.md](DEPLOYMENT.zh.md)。

### 8. 算力账号增删方便 —— 门户里三分钟一套

添加一个后端算力：门户 → **+ 添加账号** → 设备流登录 → 自动完成后续全部工作：

```
设备流登录 → 触发 GitHub Action → Terraform 部署专属 hub Container App
          → 自动加入 3 个供应商后端池（会话粘性）
          → 自动注册其 chat 模型为池化路由 → 账号状态 READY
```

- **全流程自动**，运营者只做"登录"这一个动作
- **加账号 = 给池加成员**（幂等操作），不是新增一堆重复路由配置
- **删除有完整 teardown**：一个 DELETE 请求，自动从池中摘除、销毁 hub 资源、清理密钥
- 另有**重新登录**（凭据过期时原地恢复）和**重新同步模型清单**两个运维端点

端点齐全：`POST /github-accounts/device/start|poll`、`GET /github-accounts`、
`DELETE /github-accounts/{id}`、`POST .../relogin/*`、`POST .../resync-catalog`。

---

## 计费与分账：口径来自上游，不是我们的价目表

这是一个容易被低估、但对运营方至关重要的设计：

**成本数字直接取自上游在每个响应里返回的 `copilot_usage`**（含各类 token 数量、单价、总价），
我们不维护自己的价目表。

> 维护第二张价目表意味着上游每次调价都要人工跟进，而两张表**迟早会漂移**。
> 用上游自己的口径，永远不会对不上。

链路：

```
客户端 → APIM（注入 x-tf-subscription / x-tf-api / x-tf-request-id）
      → hub（取出 copilot_usage 原样上报）
      → Event Hub → Capture(Avro) → 导入作业 → Cosmos DB → 门户
```

几个值得注意的工程细节：

- **注入的身份头带 `exists-action="override"`** —— 否则客户端可以伪造 `x-tf-subscription`，把账算到别人头上
- **文档 id 就是 APIM 的 request id** —— Capture 的 at-least-once 重复投递天然幂等，不会重复计费
- **流式与非流式一视同仁** —— `copilot_usage` 在 SSE 流里单独占一个 chunk，hub 扫描全流取出它
- **用量采集零应用延迟** —— buffered producer 发送只是一次内存写入；
  Event Hub 挂了只丢事件、**绝不影响请求成败**
- **实测零丢失**：dev-19 战役 **1,946 次调用**（含一次并发 96 的完整吞吐塌陷、
  252 次熔断），账本**精确闭合**——`网关 200+429+4xx (1694) == Cosmos 文档 (1694) +
  Σ hub lost (0)`，三个 hub 全程 `dropped=0 / lost=0`。**塌陷发生在网关层，没有
  污染计费管线。**
- **连上游的拒绝也如实入账**：49 条零 token 文档 = 网关 429 **13** 次 + 流式拒绝
  **36** 次。后者网关**必然**记成 200（响应头在拒绝到达之前就已提交，状态码改不了），
  若只信网关就会把它们当成成功

**门户上的两个数据源，刻意分开展示：**

| 来源 | 用途 | 内容 |
|---|---|---|
| **Cosmos DB** | 计费（精确、逐次调用） | 按模型 / API / 虚拟密钥 / 后端 / **终端用户**分组，input、cache_read、cache_write、output 各列 + 成本 |
| **App Insights** | 遥测（可采样，当前 100%） | 调用次数、p50/p95、**网关 vs 后端延迟拆分**、按状态码拆分的失败数、每小时趋势 |

采样数据**绝不用于计费**——这正是两个来源分开的原因。

**计费数据可见延迟**：Capture 300s + 导入轮询 300s，最坏约 10 分钟、平均约 5 分钟。
计费场景完全够用；要看近实时用量走 App Insights 那条线。

---

## 运营能力清单

| 能力 | 说明 |
|---|---|
| **多租户隔离** | 租户是计费与隔离边界，三种模式：`RESELL`（池化转售加价）/ `BYO`（客户自带 key 隔离在 Key Vault）/ `INTERNAL`（仅内部计费） |
| **项目分组** | 租户下按项目给虚拟密钥分组，做成本归属 |
| **虚拟密钥** | 一个 APIM 订阅。值只显示一次并存入 Key Vault，可独立挂起 / 吊销 |
| **每密钥限额** | TPM 限流 + token 配额档位，超 TPM 返回 `429`、超配额返回 `403`，**在网关内强制**，不经过应用代码 |
| **预算强制** | 超预算自动暂停订阅，此后请求 401 |
| **终端用户级分账** | 客户端可选传 `metadata.user_id`，用量按终端用户下钻 |
| **双门户** | 运营控制台（管理端）+ 客户门户（客户只能看到自己的租户，跨租户访问被中间件拒绝） |
| **原文审计** | 可选能力，默认关闭，独立存储账户、按租户开关、独立保留期（[AUDIT.zh.md](AUDIT.zh.md)） |

---

## 成本结构（透明）

实际账单（读自 dev-17；dev-19 由同一份 terraform 建出、规格相同），
3 个算力账号，Southeast Asia 按需价：

| 组件 | **USD / 月** |
|---|---|
| APIM Standard v2（1 unit） | 700.00 |
| Container App —— hub × 3 | 94.61 |
| Event Hubs（TU + Capture） | 94.90 |
| 其余（Log Analytics / PG / Cosmos / ACR / KV / 存储 / 控制平面） | 51 – 95 |
| **合计** | **≈ $941 – 985 / 月** |

规模化后的单位成本（含全部基础设施）：

| 调用量 | 月成本 | **单价 / 千次调用** |
|---|---|---|
| 1,000 万次 / 月 | $1,244 – 1,322 | **$0.124 – 0.132** |
| 2,000 万次 / 月 | $1,476 – 1,658 | **$0.074 – 0.083** |

**成本随规模显著摊薄**——固定成本（APIM 占 71–74%）不随调用量增长，
每增加一个算力账号是固定 +$31.54/月。

价格全部取自 Azure 零售价格 API，明细与省钱杠杆见 [PRICING.zh.md](PRICING.zh.md)。

---

## 前提与边界（据实说明）

技术决策者会问的问题，先在这里答清楚：

| 项 | 说明 |
|---|---|
| **APIM SKU 有硬要求** | 必须 v2 tier（`StandardV2_1`）。经典 tier 不支持 Anthropic 原生 token 计量，`GatewayLlmLogs` 日志类别也不存在。Basic v2 技术上够用，选 Standard v2 是为将来的 VNet 集成留门（月差 $550，是有意的取舍） |
| **吞吐天花板在上游** | 网关侧远未触顶（2,464 万 TPM 零错误），瓶颈是**上游账号级并发配额**。扩容的唯一有效路径是加账号，不是加 CPU（≥1 vCPU 后硬件已非瓶颈） |
| **算力账号需自行准备** | 每个算力后端对应一个上游订阅账号，需由客户或运营方自行提供，并确认其使用方式符合上游服务条款 |
| **计费数据有约 5 分钟延迟** | Capture 300s + 导入 300s。近实时监控走 App Insights |
| **用量丢失窗口** | producer 缓冲区里的几秒。正常关闭会 flush，只有实例被强杀才会丢。彻底消除需要给 hub 挂持久卷，会推翻"hub 无状态"的设计前提 |
| **Anthropic 的会话粘性收益为零** | 实测（两套环境、六个账号）Anthropic 的 prompt 缓存跨后端共享，会话粘性带来的缓存收益恒为零，且分布不均时可能拖累吞吐。已记录在 [CAPACITY.zh.md](CAPACITY.zh.md) §4.3，是待优化项 |
| **区域容量是部署失败头号原因** | 不同 Azure 区域配额不同且随时间变化。实测 `southeastasia` 稳定可用；新环境应准备试 2–3 个区域 |

---

## 与常见替代方案的对比

| 维度 | 直接用官方 API | 自建代理网关 | **Token Foundry** |
|---|---|---|---|
| Claude 原生格式 | ✅ | ⚠️ 多数走转译，流式 token 失真 | ✅ 原生透传 |
| 多供应商 | ❌ 各买各的 | ✅ 但要各自实现 | ✅ 三家各走原生格式 |
| 限流 / 配额 | ❌ 无租户概念 | ⚠️ 要自建分布式计数器 | ✅ 网关内置策略 |
| 熔断 / failover | ❌ | ⚠️ 要引入 Polly / Hystrix 类库 | ✅ backend 对象上的配置 |
| 逐次调用计费 | ⚠️ 只有账号级 | ⚠️ 要为每家重写 token 计量 | ✅ 上游口径 + 幂等入库 |
| 多租户分账 | ❌ | ⚠️ 自己实现 | ✅ 租户 / 项目 / 密钥 / 终端用户四级 |
| 扩容方式 | 提工单 | 改架构 | **门户加一个账号** |
| 容量可预测性 | 黑盒 | 未知 | **有实测公式与崩塌点** |

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [architecture.zh.md](architecture.zh.md) | 系统分层、接入时序、实体模型 |
| [CAPACITY.zh.md](CAPACITY.zh.md) | 容量与限流实测：三轮触顶实验、扩容曲线、缓存行为 |
| [PRICING.zh.md](PRICING.zh.md) | 分档价格、月度估算、省钱杠杆、何时升级 |
| [SECURITY.zh.md](SECURITY.zh.md) | 密钥存储、鉴权、RBAC、取舍清单 |
| [DEPLOYMENT.zh.md](DEPLOYMENT.zh.md) | 三阶段部署流程、区域选择、销毁 |
| [APIM-LLM-Gateway.md](APIM-LLM-Gateway.md) | APIM LLM 网关设计：池、会话粘性、prompt 缓存、SKU 支持 |
