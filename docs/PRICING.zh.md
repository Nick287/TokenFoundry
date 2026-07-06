# 价格与选型

[English](PRICING.md) | **中文**

Token Foundry 运行起来每档要花多少钱 —— 从"我该选哪个 SKU"的角度讲。下面 APIM 的
价格和吞吐量是 **Azure 官方标价**(Central US、美元、按月,取自 Azure 定价页)。其余资源
都是**近似估算** —— **请务必用
[Azure 定价计算器](https://azure.microsoft.com/zh-cn/pricing/calculator/)**
按你的区域、币种、协议核实。

> APIM 是整套栈的成本大头,也是吞吐量天花板,所以本文以它打头。其余资源
> (Postgres、Cosmos、Key Vault、ACR、监控、存储)相对便宜,且多按用量计费。

---

## 一句话 —— 三个档位

| | **最小(评估)** | **中等(生产)** | **最大(企业)** |
|---|---|---|---|
| **APIM 层级** | Developer | Standard | Premium |
| **APIM / 月** | **$48** | **$687** | **$2,795**(每 unit) |
| **吞吐量** | ~500 req/s | ~2,500 req/s | ~4,000 req/s(每 unit,可扩到 12/区域) |
| **SLA** | ❌ 无 | 99.95% | 99.99%(多可用区/区域) |
| **可扩单元** | 1(不可扩) | 最多 4 | 每区域最多 12,支持多区域 |
| **整套环境 / 月(估)** | **≈ $80–110** | **≈ $800–1,000** | **≈ $3,000–3,500+** |
| **适合谁** | 开发 / 演示 / 本仓库的 dev-a0x | 真实客户、单区域、需要 SLA | 高流量、跨区域、高可用 |

> **我们当前跑的是 `Developer_1`**(最小档) —— 有意为之:它用于评估,**无 SLA**,
> **且不可扩**。在给真实流量前,第一个要改的就是 APIM 层级(见下方"何时升级")。

---

## APIM —— 层级表(官方标价,Central US)

### 经典层(Classic)

| 层级 | 价格/月 | 吞吐量/unit¹ | SLA | 内置缓存 | 可扩单元 | 多区域 | 适合 |
|---|---|---|---|---|---|---|---|
| **Consumption** | 首 100 万次 $0,之后 $0.042 / 万次 | 自动 | 99.95% | 仅外部 | 自动 | ❌ | 尖峰/无服务器、稳态量低 |
| **Developer** ← *我们* | **$48.04** | **500 req/s** | ❌ **无** | 10 MB | 1(固定) | ❌ | 评估、开发、演示 |
| **Basic** | **$147.17** | **1,000 req/s** | 99.95% | 50 MB | 2 | ❌ | 入门级生产 |
| **Standard** | **$686.72** | **2,500 req/s** | 99.95% | 1 GB | 4 | ❌ | 中等量生产 |
| **Premium** | **$2,795.17**(每 unit²) | **4,000 req/s** | 99.99%³ | 5 GB | 12 / 区域 | ✔️ | 高流量 / 企业 / 高可用 |

### v2 层(更快开通、VNet 集成)

| 层级 | 价格/月 | 含请求数 | SLA | 缓存 | VNet |
|---|---|---|---|---|---|
| **Basic v2** | $150.01 | 10M/月,之后 $3 / 1M | 99.95% | 250 MB | ❌ |
| **Standard v2** | $700 | 50M/月,之后 $2.50 / 1M | 99.95% | 1 GB | VNet 集成 |
| **Premium v2** | $2,801 | 无限 | 99.99% | 5 GB | VNet 集成 + 注入 |

¹ **吞吐量是官方参考值,不是硬上限、也不是 SLA。** Azure 自己的说明:这些数字来自
1,000 个并发 HTTPS 连接、最小负载、**无策略**、低延迟后端的测试。我们的策略
(token 限流、emit-metric、Cosmos 出站写入、每 key 的 `<choose>` 配额块)会给每个请求
加处理量,所以**实际吞吐更低** —— 定容前请压测。
² Premium:同实例的增量 unit 按**首个 unit 的 50%** 计费。
³ 99.99% 需要 ≥1 个 unit 部署在两个或更多可用区/区域。

---

## 环境的其余部分(每月,估算)

这些是 Terraform 实际开通的(见 `terraform/modules/`),按我们部署的 SKU。价格是
**粗略估算** —— 请用计算器核实。

| 资源 | 我们部署的 SKU | 计费模型 | 估/月 | 说明 |
|---|---|---|---|---|
| **PostgreSQL** | `B_Standard_B1ms`(突发型,1 vCore) | 按小时 + 存储 | ≈ $15–20 | 最便宜的突发型。生产负载升到 General Purpose。 |
| **Cosmos DB** | Serverless | 按 RU + 存储 | ≈ $5–25 | 按请求付费。随用量记录写入量 + 90 天 TTL 伸缩。 |
| **Key Vault** | Standard | 按操作 | ≈ $1–5 | 每次密钥操作几分之一美分;我们的量级可忽略。 |
| **容器注册表** | Basic | 固定 + 存储 | ≈ $5 | 存 `tokenfoundry:<tag>` + `gitmodel:<tag>`。 |
| **Log Analytics + App Insights** | PerGB2018 | 按 GB 摄入 | ≈ $5–50 | 随遥测量伸缩;最能摆动的一项。采样能压住它。 |
| **存储(tfstate)** | Standard LRS | 按 GB + 操作 | < $1 | 极小 —— 几 KB 的 Terraform state blob。 |
| **Container App(控制平面)** | Consumption | 按 vCPU-秒 + 内存-秒 | ≈ $5–40 | 空闲时缩到近零;一个常驻副本更贵。 |

> **每 GitHub 账号的 GitModel hub** 是*独立*的 Container App、在自己的资源组里 ——
> 每个账号多一个 Consumption 计划的 Container App(依流量 ≈ $5–40)。每接入一个账号加一个。

---

## 整套环境月度估算

| 档位 | APIM | 其余全部 | **合计(估)** |
|---|---|---|---|
| **最小**(Developer) | $48 | ≈ $35–60 | **≈ $80–110 / 月** |
| **中等**(Standard) | $687 | ≈ $100–300 | **≈ $800–1,000 / 月** |
| **最大**(Premium ×1) | $2,795 | ≈ $200–700 | **≈ $3,000–3,500 / 月** |

**每接入一个 GitHub 账号加 ≈ $5–40**(各自是一个 hub Container App)。"其余全部"主要随
**Log Analytics 摄入量**和 **Container App** 常驻 vs 缩零而摆动。

---

## 何时升级(从用户角度)

**你在 Developer 档(我们的默认)。以下任一为真时就该离开它:**

- **你需要 SLA。** Developer **完全没有** —— 宕机 Azure 不赔。Basic/Standard 给
  99.95%,Premium 给 99.99%。仅这一条就让 Developer 不适合任何面向客户的场景。
- **你逼近 ~500 req/s**(Developer 的参考值,叠加我们的策略后更低)。下一档:
  Basic(1,000)、Standard(2,500)、Premium(4,000/unit)。
- **你需要扩容。** Developer **固定 1 unit**,无法横向扩。其他每档都能加 unit;
  Premium 既能加 unit *又能*加区域。
- **你需要多区域 / 可用区。** 只有 **Premium**(经典层)能把一个实例分布到多区域,
  并支持可用区来拿 99.99% SLA。

**定容经验法则:**

- **评估 / 内部 / 本仓库的 dev 环境** → **Developer**($48)。我们现在跑的。
- **首批真实客户、单区域、需要 SLA** → **Standard**($687) —— 甜点档:SLA +
  2,500 req/s + 自动扩到 4 unit,不用付 Premium 的溢价。
- **高流量、跨区域、高可用** → **Premium**($2,795/unit) —— 唯一支持多区域 + 可用区
  的经典层。
- **尖峰、稳态量低** → 考虑 **Consumption**(按调用付费)而非专用层 —— 但注意它无
  内置缓存、策略支持也不同。

> **Anthropic 注意(来自我们的网关):** APIM 对 Anthropic Messages API 的*原生*支持
> **仅 v2 层**。我们通过把 Claude 当普通 HTTP 后端绕过了这一点,所以在经典
> Developer/Standard/Premium 上也能用 —— 见
> [docs/APIM-LLM-Gateway.md §4.6](APIM-LLM-Gateway.md)。如果你想让 APIM 原生解析
> Anthropic 请求,那会把你推到 **v2** 层。

---

## 省钱的杠杆

1. **APIM 层级占账单 80% 以上。** 别过度配置 —— 从你的 SLA + 吞吐真正需要的层级起步,
   之后再加 unit(所有非 Developer 层都自动扩)。
2. **Log Analytics 摄入量**是第二大摆动项。APIM 诊断采样是旋钮;调低它砍遥测成本。
3. **Container Apps 空闲缩零** —— 非关键 hub 把最小副本设 0,避免常驻计费。
4. **Cosmos 是 serverless** —— 只为用量记录写入 + 90 天 TTL 存储付费,无空闲成本。
5. **Consumption 版 APIM** 按调用计费 —— 对低稳态量的网关,可能比 Developer 还便宜、
   且带 SLA。代价:无内置缓存、有冷启动、策略支持缩减。

*价格取自 Azure 定价页 Central US、美元。非 APIM 资源为参考估算 —— 请用
[Azure 定价计算器](https://azure.microsoft.com/zh-cn/pricing/calculator/)核实。*
