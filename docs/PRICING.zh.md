# 价格与选型

[English](PRICING.md) | **中文**

Token Foundry 运行起来每档要花多少钱 —— 从"我该选哪个 SKU"的角度讲。

> **本页的数字怎么来的。** 所有单价取自 **Azure 零售价格 API**
> (`https://prices.azure.com/api/retail/prices`,**Southeast Asia**、USD、按需价),
> 所有用量取自 `az` 对 **dev-17** 运行态的读数(SKU、副本数、vCPU/内存、存储大小)。
> 唯一的估算项是 Log Analytics 的月摄入 GB 数,已按区间给出。
>
> **dev-17 已于 2026-08-09 回收,dev-18 已于 2026-08-15 回收,当前环境是 dev-19。**
> 数字没有重测,也不需要重测:三套环境由**同一份 terraform** 建出,dev-19 的
> `terraform apply` 是 45 added / 0 destroyed,资源集与 dev-18 逐条相同。**规格没变,
> 单价没变,所以金额没变。** 这里保留 dev-17 的标注是为了不篡改出处——数字确实是在
> 那套环境上读的。
>
> 自那以后有两处**降低**了成本,金额待重测:
>
> * `ApiManagementGatewayLogs` 已停止采集(2026-08-15),约 −1 KB/次调用的 LAW 摄入。
> * `_USAGE_TRACE` 里读响应体的字段已删除,`traces` 行变小。
>
> 上一版本页是手工估的,而且估的是**另一套环境**:Central US 的 Developer 档 APIM,
> 并且完全没有 Event Hubs、审计 Blob、用量采集存储。下表按 2026-08 的实际部署重算。
>
> 换区域、换协议价请用
> [Azure 定价计算器](https://azure.microsoft.com/zh-cn/pricing/calculator/)复核。
>
> ⚠️ **本页只覆盖 Azure 基础设施成本,不覆盖 GitHub Copilot 侧的用量。**
> 下表的规模假设(账号数、并发档位)来自合作伙伴环境的测试账号,只能用于评估;
> GitHub 账号本身的容量是黑盒,上游不公布每账号配额,且同一批账号的实测结果本身
> 就是双峰的。**客户环境的容量与成本必须用客户自己的账号重新实测**——
> 详见 [CAPACITY.zh.md §0](CAPACITY.zh.md#0-️-这些数字的适用范围先读这一节)。

---

## 当前实际账单(读自 dev-17,3 个 GitHub 账号;dev-19 规格相同)

| 组件 | 配置 | **USD / 月** |
|---|---|---|
| **APIM Standard v2** | 1 unit × 730h × $0.9589 | **700.00** |
| **Container App —— hub ×3** | 各 1 vCPU / 2 GiB,`minReplicas=1` | **94.61** |
| **Event Hubs Capture** | 730h × $0.100 —— **定额,与流量无关** | **73.00** |
| Log Analytics + App Insights | 2–15 GB × $2.99 | 5.98 – 44.85 |
| Event Hubs 吞吐单元 | 1 TU × 730h × $0.030 | 21.90 |
| PostgreSQL B1ms | 突发型,730h × $0.026 | 18.98 |
| Container App —— 控制平面 | 0.5 vCPU / 1 GiB,`minReplicas=1` | 15.77 |
| 容器注册表 Basic | 30.42d × $0.1666 | 5.07 |
| PostgreSQL 存储 | 32 GB × $0.138 | 4.42 |
| Cosmos DB serverless | RU + 存储,按我们的量 | 0.50 – 3.00 |
| 存储账户 ×3 | 审计 + 用量采集 + tfstate,LRS | 0.50 – 2.00 |
| Key Vault | 按操作计费 | 0.20 – 1.00 |
| Event Hubs 入口事件 | ~0.06M × $0.028/1M | 0.00 |
| APIM 调用数 | ≤50M/月已含(我们约 0.06M) | 0.00 |
| **合计** | | **≈ $941 – $985 / 月** |

**APIM 占整张账单的 71–74%。**

### 三个反直觉的地方

**1. Event Hubs Capture 单项 $73/月,比 PostgreSQL + ACR + Cosmos + 全部存储加起来还贵。**
它按 **吞吐单元-小时**计费($0.100/h),**与事件量无关**。我们一整天的压测也只产生约
1,800 个事件 —— 这笔钱买的不是流量,是"开着"这个能力本身。

**2. 每多一个 GitHub 账号是固定 +$31.54/月,不是"看流量 $5–40"。**
每个 hub 是 1 vCPU / 2 GiB 且 `minReplicas=1`,**永不缩零**。Container Apps 的空闲费率
($0.000004/vCPU-秒)确实是活跃费率($0.000034)的 1/8.5,但空闲 ≠ 免费。
同一个 hub 若 100% 活跃则是 $110.38/月,而不是 $31.54。

**3. 我们在为一个"每秒几千请求"的网关付钱,而上游把我们卡在 ~17 req/s。**
见 [CAPACITY.zh.md](CAPACITY.zh.md):可持续并发是 48,实测约 17 RPS,天花板来自
**GitHub Copilot 账号级配额**,不是 APIM。网关严重过配。

---

## 省钱杠杆(按金额排序,都已按实际单价折算)

| 杠杆 | 每月省 | 代价 |
|---|---|---|
| **hub `minReplicas` 1 → 0**(3 个) | −$94.61 | 冷启动延迟。已知固定开销里有约 3.5s 与模型无关,冷启会再叠加。 |
| **关掉 Event Hubs Capture** | −$73.00 | 需要改代码:导入器从"轮询 Blob"改成"直接消费 Event Hub"。当前架构是 Capture → Blob → 导入器 → Cosmos。 |
| Log Analytics 设每日上限 / 采样 | 最多 −$40 | 现在是 `dailyQuotaGb = -1`(无上限)。上量后这一项会变大,见下方"随调用量的成本"。 |

**三项都做:$941 → 约 $773/月。**

### 不在杠杆表里的那 $550:APIM 层级(有意保留)

Standard v2 → Basic v2 每月省 **$550**,但**我们有意不做**。理由记在这里,免得反复重议:

**为将来的 VNet 集成留门。** VNet 集成是 **Standard v2 起**才有的能力(Basic v2 没有)。
一旦需要把网关放进虚拟网络,再从 Basic v2 迁回来的代价和风险高于这笔月费。

顺带把两条容易记混的事实钉住 —— **Basic v2 在技术上是够用的**,所以这是一个取舍,
不是一个限制:

| 说法 | 实际 |
|---|---|
| "只有 Standard v2 支持 Anthropic" | ❌ 官方文档写的是 **v2 tiers**(复数)。`llm-token-limit` 的 **APPLIES TO** 逐字列出 `Basic v2`;`llm-emit-token-metric` 是 **All API Management tiers**。两者的 Anthropic Messages API 都标注为 "currently supported in API Management **v2 tiers**"。 |
| 经典层(Developer / Basic / Standard / Premium) | ❌ **不支持** Anthropic 原生计量 —— v2 才是硬要求。 |
| Basic v2 的含用量 | 10M 次/月,超出 $0.030/万次(**每百万次 $3**)。20M 次/月 = $150 + $30 = $180。 |

> 文档原话:"Anthropic Messages API (currently supported in API Management v2 tiers)"。
> `currently` 是微软自己的措辞,且没有按 tier 拆分的 Anthropic schema 支持矩阵 ——
> 若将来真要降级到 Basic v2,应当先实测,不要只依据这段文字。

---

## 随调用量的成本

按今天实测的系数外推(**1.74 KB/用量文档**、**3.85 KB 计费遥测/次调用**、
**约 17 RPS / 3 个账号**的上游天花板):

| | 今天(dev) | **1000 万次/月** | **2000 万次/月** |
|---|---|---|---|
| 平均 RPS | ~0 | 3.86 | 7.72 |
| 需要的 GitHub 账号数¹ | 3 | **3** | **5** |
| Container App —— hub | 94.61(空闲) | **253 – 331** | **370 – 552** |
| APIM Standard v2 | 700.00 | 700.00 | 700.00 |
| Log Analytics(无采样) | 5.98 – 44.85 | **109.6** | **219.2** |
| Event Hubs(TU + Capture) | 94.90 | 94.90 | 94.90 |
| Cosmos² | 0.50 – 3.00 | **30.23** | **37.10** |
| 其余(PG / ACR / KV / Blob / 控制平面) | 45.00 | 55.00 | 55.00 |
| **合计** | **$941 – 985** | **$1,244 – 1,322** | **$1,476 – 1,658** |
| 单价 / 千次调用 | —— | $0.124 – 0.132 | $0.074 – 0.083 |

¹ 按**峰谷比 3 倍**估算。峰值 RPS ÷ 每账号约 5.7 RPS(实测)。这个比值是假设不是测量,
且它同时决定账号数和 hub 那一行 —— **是整张表最敏感的输入**。

² **`cosmos_throughput_rus` 的默认值是 0(serverless)。** 曾经默认 400(预配),
dev-18 就是为了验证它而建的,结果把这个默认值推翻了。表面算术很有说服力:预配
$0.0222/百万 RU、serverless $0.285/百万 RU,**差 12.8 倍**,盈亏平衡在利用率约 8%
(400 RU/s 下约 800 万次调用/月)。

> ⚠️ **但这个盈亏点自相矛盾:让预配变便宜的调用量,恰好是预配扛不住的调用量。**
> 800 万次/月平均只有约 **33 RU/s**,而 dev-18 战役的平均值就是 33 RU/s ——
> 同样的负载被 Cosmos **限流 2,904 次,并在 400 RU/s 上打满 45 分钟**。
>
> 原因是**形状,不是大小**。整分钟的峰值需求是 255 RU/s,舒服地低于 400 ——
> 但 Cosmos 按**秒**执行,而这个负载天生是尖的:导入器每 300 秒把一整个 Capture
> blob 一次性刷进去,单次跨分区聚合最坏 5,000 RU,**一次就吃掉 400 RU/s 预算的
> 12.5 秒**。要压住它得 1000+ RU/s,盈亏点因此被推到 2000 万次/月开外。
> serverless 没有可撞的天花板,这也是前四轮战役从未暴露此问题的原因。
>
> **两种情况都没丢数据**(导入器整块入库后才推进水位线,SDK 自动重试 429),
> 代价是导入延迟而非计费缺口。详见 [CAPACITY.zh.md](CAPACITY.zh.md) §7.6。

> ⚠️ **这个开关只在创建环境时有效,而且 terraform 的 plan 会骗你。**
> `EnableServerless` 是账户级、创建时固定的能力;azurerm 把 `capabilities` 当作
> computed,所以从配置里**移除它不产生任何 diff** —— plan 只会显示一个人畜无害的
> "容器 in-place 更新,+ throughput = 400",然后 apply 在 Azure 侧失败。
> (在 dev-17 上实测:账户在整个 plan 里只出现在 "Refreshing state",零计划变更。)
> 失败是安全的、不丢数据,但**切不动已有环境**。
>
> dev-17 因此在 `terraform.tfvars` 里被显式钉成 `cosmos_throughput_rus = 0`。
> 现在 **0 就是默认值**,所以新环境什么都不用写 —— dev-19 的 tfvars 里没有这一行,
> 建出来就是 serverless(实测 `capabilities: [{"name":"EnableServerless"}]`)。
> 反过来,**已经按预配建出来的环境必须显式钉住自己的值**,否则下次 apply 会尝试
> 把它改成 serverless 并失败:dev-18 当时就钉着 400。

**上量后成本结构会翻转**:今天 APIM 占 71–74%,到 2000 万次/月时
**hub Container Apps 和 Log Analytics 合计已超过 APIM**。届时优先级从"选层级"
变成"**开采样、控占空比**"。

> **Container Apps 的占空比是区间的来源。** hub 只要有 ≥1 个请求在飞就按**活跃**费率
> 计费($0.000034/vCPU-秒),否则按**空闲**($0.000004,差 8.5 倍)。2000 万次/月时
> 每个 hub 平均有 4.6 个请求在飞 —— 即便低谷降到均值 1/3 仍有约 1.5 个,**依然活跃**。
> 所以 100% 活跃是上界,只有夜间流量真正归零才会落到区间下沿。

---
---

## APIM —— 层级表(官方标价,Central US)

### 经典层(Classic)

| 层级 | 价格/月 | 吞吐量/unit¹ | SLA | 内置缓存 | 可扩单元 | 多区域 | 适合 |
|---|---|---|---|---|---|---|---|
| **Consumption** | 首 100 万次 $0,之后 $0.042 / 万次 | 自动 | 99.95% | 仅外部 | 自动 | ❌ | 尖峰/无服务器、稳态量低 |
| **Developer** | **$48.04** | **500 req/s** | ❌ **无** | 10 MB | 1(固定) | ❌ | 评估、开发、演示 |
| **Basic** | **$147.17** | **1,000 req/s** | 99.95% | 50 MB | 2 | ❌ | 入门级生产 |
| **Standard** | **$686.72** | **2,500 req/s** | 99.95% | 1 GB | 4 | ❌ | 中等量生产 |
| **Premium** | **$2,795.17**(每 unit²) | **4,000 req/s** | 99.99%³ | 5 GB | 12 / 区域 | ✔️ | 高流量 / 企业 / 高可用 |

### v2 层(更快开通、VNet 集成)

Southeast Asia 实际单价(零售价格 API,2026-08):

| 层级 | $/小时/unit | **$/月** | 含请求数 | SLA | 缓存 | VNet |
|---|---|---|---|---|---|---|
| **Basic v2** | $0.20548 | **$150.00** | 10M/月,之后 $0.030 / 万次 | 99.95% | 250 MB | ❌ |
| **Standard v2** ← *我们* | $0.95890 | **$700.00** | 50M/月,之后 $0.025 / 万次 | 99.95% | 1 GB | VNet 集成 |
| **Premium v2** | $3.83562 | **$2,801.00** | 无限 | 99.99% | 5 GB | VNet 集成 + 注入 |

> Premium v2 的增量 unit 是 $1.91781/小时($1,400/月),即首个 unit 的 50%。
> Standard v2 的 secondary unit 是 $0.68493/小时($500/月)。

¹ **吞吐量是官方参考值,不是硬上限、也不是 SLA。** Azure 自己的说明:这些数字来自
1,000 个并发 HTTPS 连接、最小负载、**无策略**、低延迟后端的测试。我们的策略
(token 限流、emit-metric、Cosmos 出站写入、每 key 的 `<choose>` 配额块)会给每个请求
加处理量,所以**实际吞吐更低** —— 定容前请压测。
² Premium:同实例的增量 unit 按**首个 unit 的 50%** 计费。
³ 99.99% 需要 ≥1 个 unit 部署在两个或更多可用区/区域。

---

## 环境的其余部分 —— 实际单价与用量

Terraform 实际开通的(见 `terraform/modules/`)。**单价来自零售价格 API,用量来自
`az` 对 dev-17 的读数** —— 不是估算,除了标注区间的两项。

| 资源 | 部署的 SKU / 配置 | 单价(Southeast Asia) | **$/月** |
|---|---|---|---|
| **Event Hubs 命名空间** | Standard,1 TU,auto-inflate 关 | $0.030 / TU-小时 | **21.90** |
| **Event Hubs Capture** | 已启用,300s / 100MB 触发 | $0.100 / TU-小时 | **73.00** |
| **Container App(控制平面)** | 0.5 vCPU / 1 GiB,`minReplicas=1` | 空闲 $0.000004/vCPU-秒<br>内存 $0.000004/GiB-秒 | **15.77** |
| **Container App(每个 hub)** | 1 vCPU / 2 GiB,`minReplicas=maxReplicas=1` | 同上 | **31.54 / 个** |
| **PostgreSQL** | `Standard_B1ms` 突发型,32 GB,7 天备份 | $0.026/小时 + $0.138/GB-月 | **23.40** |
| **Cosmos DB** | Serverless | $0.285 / 100 万 RU + $0.138/GB-月 | 0.50 – 3.00 |
| **Log Analytics + App Insights** | PerGB2018,30 天保留,**无每日上限** | $2.99 / GB 摄入 | 5.98 – 44.85 |
| **容器注册表** | Basic | $0.1666/天 + $0.10/GB-月 | **5.07** |
| **存储账户 ×3** | Standard LRS(审计 / 用量采集 / tfstate) | $0.020/GB-月 + $0.050/万次写 | 0.50 – 2.00 |
| **Key Vault** | Standard | 按操作 | 0.20 – 1.00 |

> **Container Apps 的空闲费率不是零。** 活跃 $0.000034/vCPU-秒 vs 空闲
> $0.000004 —— 相差 8.5 倍,但只要 `minReplicas ≥ 1` 就一直在计费。一个 hub 若
> 100% 活跃是 **$110.38/月**,全空闲是 **$31.54/月**;我们的 dev 负载接近后者。
>
> **每接入一个 GitHub 账号 = 固定 +$31.54/月**,与流量无关。

> **每接入一个 GitHub 账号 = 固定 +$31.54/月**,与流量无关。每个 hub 是*独立*的
> Container App,在自己的资源组里,带自己的托管环境和托管标识。

---

## 整套环境月度估算

按上表的实际单价,以 **3 个 GitHub 账号**的 hub 计:

| 档位 | APIM | 其余全部 | **合计** |
|---|---|---|---|
| **最小**(Basic v2) | $150 | $241 – $285 | **≈ $391 – $435 / 月** |
| **中等**(Standard v2)← *我们* | $700 | $241 – $285 | **≈ $941 – $985 / 月** |
| **最大**(Premium v2 ×1) | $2,801 | $241 – $285 | **≈ $3,042 – $3,086 / 月** |

"其余全部"里最大的三块是 **hub Container Apps($94.61)**、**Event Hubs
含 Capture($94.90)**、**Log Analytics($5.98–44.85,唯一真正会摆动的一项)**。

**每接入一个 GitHub 账号加 $31.54**(固定,不随流量变)。

---

## 何时升级(从用户角度)

**我们在 Standard v2 档。** 关于往哪走,实测数据把问题反过来了 —— 见
[CAPACITY.zh.md](CAPACITY.zh.md):

> 可持续并发 **48**,实测约 **17 RPS**,天花板来自 **GitHub Copilot 的账号级配额**,
> 不是 APIM。三个环境(dev-15 / dev-16 / dev-17)一致复现。

所以**当前的 APIM 层级不是瓶颈,而是超配** —— 这是**有意的**(见上方"为将来的
VNet 集成留门")。要提高吞吐,加的是 **GitHub 账号**(每个 +$31.54/月空闲、
+$110.38 满载),**不是 APIM 层级**。

APIM 只在以下情况才需要**继续往上**升到 Premium v2:

- **需要 VNet 注入**(仅 Premium v2)。Standard v2 只有 VNet *集成*。
- **需要 99.99% SLA**(仅 Premium v2)。Basic v2 / Standard v2 都是 99.95%。
- **需要 >4 unit 或多区域**。

**往下降到 Basic v2 是技术可行的**(省 $550/月),我们选择不做 —— 理由见上。若将来重议,
需要先确认两件事:月调用是否会超 10M 含量(超出 $0.030/万次,即每百万次 $3);
以及是否已经用上了 Standard v2 起才有的 VNet 集成。

**定容经验法则:**

- **纯评估 / 演示,且确定不碰 VNet** → **Basic v2**($150)。满足 Anthropic 原生计量,
  含 10M 次/月,有 99.95% SLA。
- **面向客户、单区域,或需要给 VNet 留门** → **Standard v2**($700) ← *我们在这里*。
  含 50M 次/月、1GB 缓存、VNet 集成、最多 4 unit。
- **高流量、跨区域、高可用** → **Premium v2**($2,801/unit,增量 unit 半价) ——
  唯一给 99.99% SLA 和 VNet 注入的档。

> **Anthropic 注意(来自我们的网关):** APIM 对 Anthropic Messages API 的*原生*支持
> **仅 v2 层**(官方原文:"currently supported in API Management **v2 tiers**")。
> 我们同时也把 Claude 当普通 HTTP 后端绕过了这一点,所以经典层也能*转发* —— 见
> [docs/APIM-LLM-Gateway.md §4.6](APIM-LLM-Gateway.md)。要让 APIM **原生计量**
> Anthropic token,就必须是 v2 层;**Basic v2 和 Standard v2 都算**。

---

## 省钱的杠杆(按实测金额排序)

> APIM 那 $550 不在此列 —— 见上方"有意保留"。下面是**还没做、且没有理由不做**的。

1. **hub 的 `minReplicas` 设 0** —— 3 个 hub **省 $94.61/月**。代价是冷启动;当前
   `minReplicas=maxReplicas=1`,永不缩零。
2. **Event Hubs Capture 省 $73/月**,但要改代码:导入器改成直接消费 Event Hub,而不是
   轮询 Capture 产出的 Blob。注意 Capture 按 **TU-小时定额**计费,**和事件量无关** ——
   我们一整天压测才约 1,800 个事件,这笔钱买的是"开着"本身。
3. **Log Analytics 采样 / 每日上限** —— 现在 `apim_sampling_percentage = 100`(每次调用
   全记)且 `dailyQuotaGb = -1`(无上限)。dev 环境最多 $44.85/月,但**上量后这是增长
   最快的一项**。实测每次调用产生 **3.85 KB** 计费遥测:

   | 表 | B/次 | 受 10% 采样影响? |
   |---|---|---|
   | `dependencies` | 1,286 | ✔️ |
   | `requests` | 1,218 | ✔️ |
   | `traces`(我们的 `_USAGE_TRACE` 策略) | 974 | ✔️ |
   | `customMetrics`(`llm-emit-token-metric`) | 346 | ❌ **不受影响** |
   | `exceptions` | 116 | ✔️ |

   > 上表是 2026-08 初的测量,有两处已经变化,**下次重测时要一并修正**:
   >
   > * `traces` 那 974 B 偏高了。`_USAGE_TRACE` 里读响应体的 `usage` 字段已被删除
   >   (它会压平流式,见 CAPACITY §7.7),现在只剩五个短属性。**未重新测量。**
   > * 表里**漏了 `ApiManagementGatewayLogs`**。它当时是开着的,dev-19 实测约
   >   **1 KB/次**——也就是说真实总量当时接近 4.9 KB 而非 3.85 KB。该类别已于
   >   2026-08-15 停止采集(与 `AppRequests` 逐行重复且无人读取),所以这一项现在归零。

   官方文档:Request / Dependency / Exception / Trace 都是**按请求**发出、同属一个
   diagnostic,采样一并作用;而自定义指标走 `"metrics": true` 的独立开关、是**预聚合
   时间序列**(实测 1,811 次请求只产生 774 行)。所以 10% 采样是
   **3,940 B → 705 B,降 82% 而非 90%** —— customMetrics 那 346 B 是地板。

   1000 万次/月 $110 → $20;2000 万次/月 $219 → $39。

   > ⚠️ **采样会破坏精确对账。** 闭合判据用的就是 App Insights `requests` 的计数,
   > 只记 10% 就得靠 `sum(itemCount)` 外推,从"逐条相等"退化成"统计估计"。今天能把
   > 36 次流式拒绝、540 个 token 差**逐条追到成因**,靠的正是精确相等。customMetrics
   > 和 Cosmos 不受影响,丢的是**网关侧请求计数的精度**。
   >
   > 另有两个坑:`always_log_errors = true` 让错误全量、成功打折,**失败率会被放大**
   > (1.3% 在 10% 采样下显示成约 7%);熔断丢弃的 503 **只存在于 `requests`**,
   > Cosmos 没有对应文档。
   >
   > 折中做法:平时 10%,做对账战役时临时调回 100%(APIM diagnostic 的属性改动秒级
   > 生效,不用重建)。**降之前先把 `usage_ingest.py` 的四条 KQL 从 `count()` 改成
   > `sum(itemCount)`。**

4. **Cosmos 默认是 serverless**(`cosmos_throughput_rus = 0`)。曾经默认预配 400,
   dev-18 实测把它推翻了:33 RU/s 的平均负载被限流 2,904 次、打满 45 分钟,而
   预配变便宜的调用量恰好就是预配扛不住的调用量。已存在的环境切不动,也不该切
   —— 见上方注 ²。

**三项都做:$941 → 约 $773/月**(dev 量级),且对 dev 环境几乎没有实际损失。

---

*单价:Azure 零售价格 API(`prices.azure.com`),**Southeast Asia**、USD、按需价,
2026-08 取数。用量:`az` 对 dev-17 运行态的读数(该环境已回收,dev-19 由同一份 terraform 建出、规格相同)。换区域 / 协议价请用
[Azure 定价计算器](https://azure.microsoft.com/zh-cn/pricing/calculator/)复核。
经典层(Developer / Basic / Standard / Premium)那张表沿用旧的 Central US 标价,
未重新取数 —— 我们没有部署经典层。*
