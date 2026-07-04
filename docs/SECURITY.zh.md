# 安全与数据模型

[English](SECURITY.md) | **中文**

Token Foundry 怎么存储密钥和数据:什么存在哪、存的是值还是引用、调用方和用户
怎么鉴权,以及已知的取舍。这里每一条都有代码依据 —— 给出了文件位置,你可以自己核对。

## 一句话总览 —— 各类数据存在哪

| 数据 | 存储 | 形态 | 为什么 |
|---|---|---|---|
| 虚拟密钥**值**(客户端用来调用的 `vk_…` 密钥) | **Key Vault** + APIM | 真实值在 APIM 的订阅存储里;PostgreSQL 里只记一个 **Key Vault 引用**(URI) | 签发时只给运营看**一次**,之后再不显示。 |
| BYO 供应商 API 密钥(客户自带的 Anthropic/OpenAI/… 密钥) | **Key Vault** + APIM 后端 | 真实值存在 Key Vault 和 APIM 后端凭据里;PostgreSQL 只存路由元数据 | 按路由隔离;由网关注入,绝不回传给客户端。 |
| 数据库连接串、JWT 签名密钥、种子管理员密码 | **Key Vault** | 密钥值,部署时写入一次;以 Key Vault 引用形式注入应用 | 应用从不在代码或明文配置里持有它们。 |
| 用户登录密码 | **PostgreSQL** | **PBKDF2-HMAC-SHA256 哈希**(24 万次迭代,每用户独立盐)—— 绝非明文 | 数据库登录;常数时间校验。 |
| 租户 / 项目 / 虚拟密钥元数据 / 模型路由 / 预算 / 用户 | **PostgreSQL** | 标识符、设置、引用 —— **不存任何密钥值** | 关系型控制平面状态。 |
| 每次调用的用量记录(每次 LLM 调用一条) | **Cosmos DB** | 供应商原始响应 JSON + 元数据;以**虚拟密钥 id** 为键,从不存密钥值 | 用于计量的高写入时序;90 天 TTL。 |

**串起这一切的那条铁律:** 控制平面**绝不在 PostgreSQL 里持久化原始密钥** ——
只存 Key Vault 引用([`app/services/keyvault.py`](../app/services/keyvault.py) 第 1–7 行)。
Key Vault 是 set/get/delete 的唯一收口点。

## Key Vault —— 密钥存储

Azure Key Vault 持有每一个真实密钥。配置为**仅 RBAC 授权**(不用访问策略),
软删除 7 天([`infra/modules/keyvault.bicep`](../infra/modules/keyvault.bicep))。

写入了什么、谁写的:

| 密钥名 | 内容 | 写入者 | 何时 |
|---|---|---|---|
| `vk-<key-id>` | 虚拟密钥值(APIM 订阅主密钥) | Container App **系统身份**(Key Vault Secrets Officer) | 每次签发密钥 |
| `route-<route-id>-backend` | 某 BYO 供应商的 API 密钥 | Container App 系统身份 | 每次添加 BYO 路由 |
| `tf-database-url` | 完整 PostgreSQL 连接串(含数据库密码) | Bicep,部署时 | 一次,基础设施部署时 |
| `tf-jwt-secret` | 登录 JWT 的 HS256 签名密钥 | Bicep,部署时 | 一次 |
| `tf-admin-password` | 种子管理员账号密码 | Bicep,部署时 | 一次 |

`set_secret` 返回密钥的**引用 id(URI)**;存进 PostgreSQL 的是这个 URI 而不是值
([`keyvault.py`](../app/services/keyvault.py) 第 51–58 行)。运行时应用把
`tf-database-url` / `tf-jwt-secret` / `tf-admin-password` 作为 **Key Vault 密钥引用**
由 Container Apps 注入为环境变量
([`infra/modules/containerapps.bicep`](../infra/modules/containerapps.bicep)),
因此这些值从不出现在源码或明文应用配置里。

有意使用**两个**托管身份
([`containerapps.bicep`](../infra/modules/containerapps.bicep)):

- 一个**用户分配**身份(`*-acrpull-id`)—— 预先授予 AcrPull + Key Vault
  **Secrets User**(读),这样第一个修订版能在应用自身身份存在之前就拉取镜像、
  在启动时解析密钥引用。
- **系统分配**身份 —— 运行时凭据。它要写订阅密钥 + BYO 密钥,所以需要 Key Vault
  **Secrets Officer**(读写),外加 APIM Service Contributor、Cosmos Data
  Contributor、Monitoring Reader(见下方 RBAC)。云上代码显式选择系统身份
  (`ManagedIdentityCredential`),以免裸用 `DefaultAzureCredential` 在写密钥时
  误选只读的拉取身份([`keyvault.py`](../app/services/keyvault.py) 第 21–34 行)。

## PostgreSQL —— 控制平面元数据(不存密钥)

PostgreSQL Flexible Server 16,数据库 `tokenfoundry`。持有控制平面的关系型状态。
完整 schema 见 [`app/models/orm.py`](../app/models/orm.py):

| 表 | 存什么 | 有敏感信息吗? |
|---|---|---|
| `tenants` | id、名称、模式(RESELL/BYO/INTERNAL)、计费账户、APIM product id、状态 | 无 |
| `projects` | id、tenant_id、名称、成本中心 | 无 |
| `virtual_keys` | id、project_id、APIM 订阅 id、**`keyvault_ref`**(URI)、允许的路由、TPM 档、预算、状态、过期 | **只有引用** —— 绝无密钥值 |
| `model_routes` | id、tenant_id(NULL = 平台池化)、别名、供应商、后端 id、`auth_mode`(MI 或 KV_SECRET)、定价 | 无(BYO 密钥在 Key Vault) |
| `budgets` | id、范围(TENANT/PROJECT/KEY)、目标 id、上限/已用 USD、动作 | 无 |
| `users` | id、用户名、**`password_hash`**、角色(ADMIN/CUSTOMER)、tenant_id、是否停用 | **只有哈希** —— 绝无明文 |

`virtual_keys` 模型里写得很直白:*"密钥 VALUE 绝不落在这里 —— 只有 Key Vault 引用"*
([`orm.py`](../app/models/orm.py) 第 85–88 行)。

**用户密码**用 **PBKDF2-HMAC-SHA256** 哈希,24 万次迭代,每用户 16 字节随机盐,
以 `pbkdf2_sha256$<迭代次数>$<盐_hex>$<哈希_hex>` 格式存储,并用
`hmac.compare_digest` 做常数时间校验
([`app/services/passwords.py`](../app/services/passwords.py))。无第三方加密依赖 ——
仅用 Python 标准库。

种子管理员在启动时从 `TF_ADMIN_PASSWORD`(一个 Key Vault 引用)创建一次,并**立即
哈希**;明文从不存储,且该播种是幂等的(管理员已存在则跳过)
([`app/init_db.py`](../app/init_db.py))。

## Cosmos DB —— 用量记录(不存密钥值)

Cosmos DB for NoSQL,**Serverless**,**`disableLocalAuth: true`** —— master key
关闭,访问**仅限 AAD**([`infra/modules/cosmos.bicep`](../infra/modules/cosmos.bicep))。
数据库 `tokenfoundry`,容器 `usage`,分区键 `/pk`(`<订阅id>_<yyyymm>`),原始文档
**90 天 TTL**。

每次成功的 LLM 调用,由 **APIM 出站策略**写一条文档
([`apim/policies/outbound-cosmos-write.xml`](../apim/policies/outbound-cosmos-write.xml)):

| 字段 | 内容 |
|---|---|
| `id` | APIM 请求 id |
| `pk` | 分区键,`<订阅id>_<yyyymm>` |
| `ts` | 时间戳(ISO 8601) |
| `subscription` | **虚拟密钥 id**(APIM 订阅 id)—— **不是密钥值** |
| `tenant` / `route` | 来自 `x-tf-*` 请求头,默认 `"unknown"`(仅用于观测 —— 见下) |
| `region`、`api` | Azure 区域、APIM API id |
| `raw_response` | 供应商的**完整响应 JSON**(token 在读取时从这里解析) |

写入时**不**解析 token —— 各家格式不同(`prompt_tokens`/`completion_tokens` vs.
Anthropic/Responses 的 `input_tokens`/`output_tokens`),所以原样存 JSON,读取时再
归一化([`app/api/usage.py`](../app/api/usage.py) 的 `_extract_tokens`)。

**Cosmos 里不落任何密钥。** 记录以虚拟密钥 **id** 为键(由 APIM 鉴权,无法伪造),
从不存密钥值。门户展示某租户用量时,先从 PostgreSQL 查出该租户的虚拟密钥 id,
再用这些 id 去查 Cosmos —— 所以 `x-tf-tenant` 请求头**仅用于观测,不用于隔离**
([`usage.py`](../app/api/usage.py) 的 `query_by_subscriptions`)。

## 鉴权

### 调用网关(数据平面)

客户端把**虚拟密钥**放在供应商原生请求头里调用(Anthropic 用 `x-api-key`,
OpenAI 系用 `api-key`/`Authorization`)。虚拟密钥**就是 APIM 订阅密钥**;APIM 校验它
并施加按密钥的 token 限流,以订阅 id 为键
(内联在 [`app/services/apim_provisioner.py`](../app/services/apim_provisioner.py) 的入站策略 XML)。

**真实的上游供应商密钥**客户端永远看不到:

- **平台池化**(RESELL/INTERNAL)路由用 APIM 的**托管身份**调用 Azure OpenAI
  (`auth_mode = MI`)—— 请求里根本没有密钥。
- **BYO** 路由从 **APIM 后端凭据**注入客户自己的密钥(`auth_mode = KV_SECRET`);
  该密钥同时存在 Key Vault。

### 登录门户(控制平面)

自托管、数据库登录。成功后后端签发一个短时效 **HS256 JWT**(`settings.jwt_secret`,
来自 Key Vault;默认 8 小时过期),携带 `sub`、`role`、`tenant_id`
([`app/services/tokens.py`](../app/services/tokens.py))。每个请求都用同一密钥校验
([`app/api/auth.py`](../app/api/auth.py))。

**租户隔离的红线:** 后端**绝不信任请求里的租户 id** —— 它从 token 里取出调用方的
租户,并强制每个客户查询按它过滤。`require_admin` 把守平台级操作;`tenant_scope`
为客户端点返回强制的租户,并拒绝没有租户的主体([`auth.py`](../app/api/auth.py)
第 97–108 行)。像 `GET /usage` 这样的客户端点,其租户取自 `Depends(tenant_scope)`,
而非任何参数。

本地 dev-token 捷径(`dev:<role>:<tenant>`)**仅在** `TF_ENVIRONMENT=local` 时存在,
让整套栈无需身份提供方即可端到端跑通。dev/prod 下它是关闭的
([`auth.py`](../app/api/auth.py) 第 35–47、84–86 行)。

## 身份与 RBAC(谁能动什么)

| 身份 | 角色 | 作用在 | 为什么 |
|---|---|---|---|
| Container App —— 系统 | API Management Service Contributor | APIM | 运行时开通 product / 订阅 / 后端 |
| Container App —— 系统 | Key Vault Secrets Officer | Key Vault | 写虚拟密钥 + BYO 密钥,并读回 |
| Container App —— 系统 | Cosmos DB Data Contributor | Cosmos | 读用量记录 |
| Container App —— 系统 | Monitoring Reader | App Insights | 用 KQL 查延迟/遥测 |
| Container App —— 用户分配 | AcrPull + Key Vault Secrets User | ACR + Key Vault | 拉镜像、启动时解析密钥引用(预先授予) |
| APIM —— 系统 | Cosmos DB Data Contributor | Cosmos | 出站策略每次调用写一条用量文档 |

Cosmos **仅限 AAD**(`disableLocalAuth: true`),Key Vault **仅 RBAC 授权** ——
两者都在配置层面拒绝共享密钥访问。

## 已知取舍与短板(诚实清单)

这些都是有意的 MVP 选择或标注过的 TODO —— 不是意外:

1. **即发即忘的用量写入。** Cosmos 写入用 `send-one-way-request` 且**不重试** ——
   若 Cosmos 短暂不可用,那次调用的用量记录就丢了。这么选是为了让用量采集绝不给
   LLM 链路增加延迟;计费级记账走第二阶段的 Event Hub
   ([`outbound-cosmos-write.xml`](../apim/policies/outbound-cosmos-write.xml) 第 8–9 行)。
2. **PostgreSQL 防火墙 = AllowAzureServices(0.0.0.0)。** 数据库对 Azure 服务可达,
   不只是本应用。模块注释已标注:*"生产环境收紧为 VNet 集成"*
   ([`infra/modules/postgres.bicep`](../infra/modules/postgres.bicep))。
3. **`raw_response` 原样存储、未做过滤。** 当前各家供应商(OpenAI/Anthropic/Google)
   只返回补全 + 用量,不回显 prompt,所以 prompt 不会被持久化 —— 但补全里可能含有
   模型生成的任意内容,而该字段没有任何脱敏。应把 Cosmos `usage` 容器视为可能含有
   用户生成内容,并据此保护。
4. **BYO 密钥轮换不是自动的。** BYO 密钥在创建时写入 APIM 后端凭据;更新 Key Vault
   里的副本**不会**让网关跟着轮换,除非重新开通该后端。
5. **语义缓存(第二阶段)必须按租户分区。** 尚未启用;启用时必须按订阅 id `vary-by`,
   否则会跨租户泄漏响应 —— 这一点在策略注释里已点明
   ([内联在 `app/services/apim_provisioner.py`](../app/services/apim_provisioner.py) 的入站策略 XML)。
6. **虚拟密钥无法找回,只能轮换。** 值只显示一次、只留 Key Vault 引用;丢了就重发一把
   新的,而非取回。(这是正确的密钥卫生,列出来是让这个行为不至于让人意外。)
7. **没有 JWT 刷新。** 登录 token 存活 8 小时,没有刷新端点;过期后客户端需重新登录。
