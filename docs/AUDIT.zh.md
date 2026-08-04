# 原文审计归档（raw-body audit）

> 按租户开关、**默认关闭**的功能：开启后，网关会把该租户每次调用的
> **完整请求体 + 完整响应体**原样归档到一个**独立的存储账户**。
> 这是本系统里唯一会持久化**客户内容**的地方 —— 所以它的边界、权限和保留期
> 单独写一篇，而不是塞进计费文档的某一节。

## TL;DR

| 问题 | 答案 |
|---|---|
| 归档什么 | 请求体与响应体**全量**（流式存整条 SSE 原文）；仅图片像素被替换为占位符 |
| 存在哪 | **独立的存储账户**（不是计费 Capture 账户的一个容器），容器名 `audit` |
| 谁能写 | 各 hub 的托管标识，`Storage Blob Data Contributor`，**scope 到容器** |
| 谁能读 | **没有任何服务身份有读权限**，控制平面也没有；读需要**另行授给具名的人** |
| 存多久 | 生命周期策略 **90 天**删除 + **7 天**软删除，由平台执行 |
| 谁能开 | 只有平台管理员（`PATCH /tenants/{id}`），**客户端无法自行开关** |
| 默认 | 关。两道开关必须**同时**成立才会写入 |
| 失败方向 | 一律 fail **closed**：解析不出开关就当作"没开" |

---

## 1. 合规边界：到底会留下什么

**会留下的（要按客户内容对待）：**

- **请求体全量** —— 对编码类客户端，这里面就是**源代码**；也包括用户粘进 prompt
  的任何东西：密钥、令牌、个人信息。系统不做任何脱敏，也不假装能做。
- **响应体全量** —— 非流式是完整 JSON；流式是**整条 SSE 原文**
  （`vendored/gitmodel-hub/hub/server.py:635`、`:839`，复用了本来就为解析 usage
  而缓存的 `collected`，不额外占内存）。
- 一圈信封字段：`request_id` / `ts` / `subscription` / `api_id` / `end_user` /
  `model` / `endpoint` / `streamed` / `status`。

**刻意不留的：**

- **图片的 base64 像素**。gpt-image 一张图就是几 MB base64，留着会让归档体积和
  费用涨两个数量级，而审计要的东西一个都不在像素里。响应里的 `b64_json` 被替换为
  `[omitted N b64 chars]`，其余字段（revised prompt、内容过滤结论、model、usage）
  逐字保留；**请求里的 prompt 仍是全量的**
  （`_strip_image_bytes`，`hub/server.py:654`）。
- **图片编辑上传的源文件**，只记 `{field, filename, bytes}`（`hub/server.py:756`）。

**超限会截断，且明确标注：**

单条记录 gzip 前超过 `TF_AUDIT_MAX_BYTES`（默认 **4 MiB**）时，请求体与响应体
各自被裁到预算的一半，文档里 `truncated: true`，被裁处写
`…[truncated N chars]`。预算对半分是为了**不让一个超大 prompt 把响应挤没**
（`tests/test_audit.py::test_payload_huge_prompt_cannot_squeeze_out_the_response`）。

> 选择截断而不是整条丢弃：一条被裁的记录仍然证明了"谁在什么时候调了什么"，
> 那是审计价值的大头；而丢弃就是什么都没有。

**存储格式**：`gzip` 的单个 JSON 文档，`content-encoding: gzip`。
SSE 原文每个 chunk 重复一遍信封，压缩率大约一个数量级 —— 这个账在按字节
计费、且要存 90 天的地方不是可选项。

**blob 路径**（`hub/audit.py:blob_path`，纯函数、可单测）：

```text
YYYY/MM/DD/<subscription>/<request_id>.json.gz
```

日期在前是因为保留期和批量导出都按时间；租户在后，使得**一个客户的归档就是一个
前缀** —— 按客户删除、或给客户自己的审计方签一个受限 SAS，都是前缀操作而不是全表扫描。
`subscription` 与 `request_id` 都来自请求头，因此路径里非 `[A-Za-z0-9_-]` 的字符
一律替换为 `_`（**`.` 也在被替换之列**，所以任何段都不可能是 `.` 或 `..`）。
伪造一个 header **加不出新的路径层级**，也就跨不到别的租户前缀去。

---

## 2. 为什么是独立的存储账户，而不是一个容器

计费那条线（Event Hub Capture）落的是 token 数和价格，控制平面跨租户读它是正当的。
审计落的是**客户内容**。两者的保留期不同、审计要求不同、访问名单不同 ——
而让它们保持不同的唯一可靠办法，就是不共用一个账户。共用账户意味着：任何一次
"给控制平面加个读权限以便导入计费"的改动，都可能顺手让它能读到客户的源代码。

模块：[`terraform/modules/audit/`](../terraform/modules/audit/)。

> **`shared_access_key_enabled = true` 的说明**：账户密钥在运行期**没有任何东西
> 使用**（hub 用托管标识写）。它开着仅仅因为当前 azurerm provider 是走**存储数据
> 平面**创建容器的（与 Capture 账户同一个约束）。哪天 provider 改走 ARM，这里就该
> 翻成 `false` —— 这个账户上的一把账户密钥，等同于所有归档 prompt 的钥匙。

---

## 3. 访问控制：写是自动的，读不是

| 主体 | 权限 | 在哪授的 |
|---|---|---|
| 各 hub 的 UAMI | `Storage Blob Data Contributor`，**scope = 容器** | `vendored/gitmodel-hub/infra/main.tf` · `azurerm_role_assignment.audit_writer` |
| 控制平面 MI | **无** | 刻意不授 —— 它只转发坐标、记录 blob 路径 |
| 门户 / 运营用户 | **无** | 同上 |
| 人工审计者 | 需要时**另行**授 `Storage Blob Data Reader` | 手工，scope 用根输出 `audit_storage_account_id` |

角色授权写在 **hub 自己的 terraform** 里而不是控制平面里，是因为 hub 是按账号部署的
Container Apps、由另一个 SP 执行（方案 A），它们的 principal id 不存在于控制平面的
state 中。控制平面只负责**导出 scope**，再把它作为 GitHub Actions 变量
（`HUB_AUDIT_CONTAINER_SCOPE`）发布出去。

**"没有服务能读"是设计目标，不是遗漏。** 门户有朝一日能渲染客户的 prompt，
绝不应该是某次 `terraform apply` 的副作用。要看归档内容，就得有人**具名地**被授权，
并且那次授权本身在 Azure 的活动日志里留痕。

> **已知残留风险**：Azure 没有"只写不读"的 blob 角色。`Storage Blob Data Contributor`
> 是能授出的最小可写角色，它顺带带来了读能力 —— 即 hub 的身份理论上能读回归档。
> 这一点写在 `infra/main.tf` 的注释里，不藏着。

---

## 4. 保留期

- **90 天**后删除：`azurerm_storage_management_policy`，按 blob 创建时间。
  天数是模块变量 `retention_days`，并作为 `TF_AUDIT_RETENTION_DAYS` 注入控制平面
  （`app/config.py::audit_retention_days`）—— 这样控制平面说的保留期永远是基础设施里
  真实生效的那个，不会各说各话。
- **7 天**软删除：`blob_properties.delete_retention_policy`，防误删。
  含义是：删除后仍有 7 天可恢复窗口，**"90 天"在最坏情况下是 97 天**。
- 由**平台的生命周期管理**执行，不是我们跑的定时任务 —— 定时任务会悄无声息地停掉，
  然后客户内容就永久留下了。

---

## 5. 两道开关，缺一不写

```text
控制平面 Tenant.audit_enabled ──► APIM named value 里的 "a":1
                                        │
                              APIM policy 盖 x-tf-audit: 1  ─┐
                                                             ├─► hub 归档
              TF_AUDIT_ACCOUNT_URL + TF_AUDIT_CONTAINER 已配 ─┘
```

**① 部署侧**：`TF_AUDIT_ACCOUNT_URL` 与 `TF_AUDIT_CONTAINER` 都非空，
`audit_enabled` 才为真（`hub/config.py:164`）；否则 `hub/audit.py` 整体是 no-op，
hub 照常服务。这也让 hub 能脱离本平台独立运行。

**② 租户侧**：APIM 必须盖上 `x-tf-audit: 1`。hub 只认这个头
（`audit.wants_audit`），**默认关** —— 任何不经 APIM 的直连调用都不会被归档。

### 开关怎么下发

平台管理员调 `PATCH /tenants/{id} {"audit_enabled": true}`。控制平面把该租户名下
**所有**虚拟密钥对应的 APIM subscription id 一起写进已有的 named value
`tf-key-token-limits`，在各自的条目上加一个 `"a": 1`：

```jsonc
{"sub-abc": {"t": 50000, "qt": "small", "p": "Daily", "a": 1}}
//                                                    ^^^^^^ 审计开关
```

复用限流那张表而不是新开一个 named value，好处是**改开关不需要重新部署 hub、
也不需要改策略** —— 策略里那段表达式（`_AUDIT_FLAG_EXPR`）本来就在解析这张表。

APIM 那侧：

```xml
<set-header name="x-tf-audit" exists-action="override">
  <value>@{ try { ...读 map[context.Subscription.Id]["a"]... } catch { return "0"; } }</value>
</set-header>
```

- **`exists-action="override"` 就是同意边界**。客户端自己带的 `x-tf-audit` 会被
  无条件覆盖 —— 它既不能把自己的审计打开（去污染归档），也不能关掉（去掩盖行为）。
  这个头是 hub 能看到的**唯一**一份同意记录。
- **`catch { return "0" }` 是 fail closed**。map 坏了、条目缺了、类型不对，
  一律当作"没开"。反过来的默认值意味着：在一个从未同意的租户上归档它的源代码。

### 一个容易写坏的地方：两个操作，一张表

限流和审计开关由**不同的操作**写进**同一个** named value，所以两个方向都得防：

- `_merge_key_limit()` 更新限额时**原样带过** `"a"` —— 否则改一次 TPM 就悄悄
  把某个租户的审计关了，丢的是审计线索。
- `_merge_audit_flag()` 只碰 `"a"`，不碰 `t/qt/p` —— 否则改开关会顺手改掉限额。

两个方向都有单测钉住（`tests/test_audit.py` 的 "the clobber hazard" 一节）。

### 先网关，后数据库

`_apply_audit_flag()`（`app/api/tenants.py:94`）**先**推 APIM，成功了才写本地表；
推失败直接 502，不半应用。因为真正决定"客户内容有没有落盘"的是 named value 那张表，
不是数据库那个 bool。两者不一致时**数据库是在撒谎**：要么声称开着其实什么都没采，
要么声称关着而 body 还在往存储里落 —— 后者是同意违规，不允许悄悄发生。

新签发的密钥同理：租户已开审计时，签发时立刻给新 key 补上标记
（`app/api/keys.py:88`），否则"新 key 归档不到"这件事，恰恰是审计存在的意义所在。

---

## 6. 指针契约：`audit_blob` 是承诺，不是回执

计费事件（进而 Cosmos 文档）里带一个 `audit_blob` 字段，值是归档的 blob 路径，
**未开启时为 `null`（正常情况）**。

- **只有指针，没有内容**。请求体和响应体不会被复制进 Cosmos，所以用量文档对
  "本来就能看用量的人"依旧是安全的；顺着指针取内容需要一个本系统任何身份都不持有的
  blob 角色。
- **路径在上传完成之前就返回了**。路径由 `(ts, subscription, request_id)` 推导，
  上传交给后台任务，`submit()` 立即返回 —— 存储再慢也不会拖慢网关一毫秒。
  代价是：**上传后来失败的话，指针就是悬空的**。
- 判断悬空的信号是 hub 的 `/healthz` → **`audit_payloads_dropped`**
  （`hub/server.py:904`）。这个计数应该配告警。

会丢弃并计数的情形，全部只记日志、绝不影响请求成败：未配置、凭据初始化失败、
存储不可达、序列化异常、**在飞的上传超过 64 条**（有界，防止慢存储把网关拖成
无限内存增长）。

优雅关闭（含 Container Apps 滚动更新）会 `await` 完在飞的上传再退出
（`audit.aclose()`），所以只有被强杀才会丢。

---

## 7. 相关文件

| 文件 | 作用 |
|---|---|
| [`vendored/gitmodel-hub/hub/audit.py`](../vendored/gitmodel-hub/hub/audit.py) | 路径推导、打包、后台上传、丢弃计数 |
| [`vendored/gitmodel-hub/hub/server.py`](../vendored/gitmodel-hub/hub/server.py) | `_emit_usage` 里唯一的触发点；`_strip_image_bytes` |
| [`vendored/gitmodel-hub/infra/main.tf`](../vendored/gitmodel-hub/infra/main.tf) | hub UAMI 的容器级写权限 + 两个 env |
| [`terraform/modules/audit/`](../terraform/modules/audit/) | 独立存储账户、容器、保留期策略 |
| [`app/services/apim_provisioner.py`](../app/services/apim_provisioner.py) | `_AUDIT_FLAG_EXPR`、`_merge_audit_flag`、`set_audit_flag` |
| [`app/api/tenants.py`](../app/api/tenants.py) | 租户开关，先网关后 DB |
| [`app/services/usage_capture_import.py`](../app/services/usage_capture_import.py) | 把 `audit_blob` 指针写进 Cosmos 文档 |
| [`tests/test_audit.py`](../tests/test_audit.py) | 32 条：路径隔离、同意门、截断、抢写、fail-closed、指针不含正文 |

配置项（hub 容器 env）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TF_AUDIT_ACCOUNT_URL` | 空 | 空 = 整个功能关闭 |
| `TF_AUDIT_CONTAINER` | 空 | 同上；两者都要有 |
| `TF_AUDIT_CLIENT_ID` | 回落到 Event Hub 的 client id | 用户分配托管标识 |
| `TF_AUDIT_MAX_BYTES` | `4 MiB` | 超出即截断并标记 |

---

## 8. 取舍与未做

- **没有做脱敏**。prompt 里的密钥、PII 会原样进归档。做正则脱敏既会漏（源代码里的
  凭据形态千变万化），又会给人"已经安全了"的错觉 —— 所以边界划在**访问控制和保留期**
  上，而不是划在"内容已净化"上。开启前应当让客户知情。
- **没有做客户自助导出**。目前取归档要平台侧具名授权 + 手工按前缀取。按租户前缀签
  受限 SAS 是天然可做的下一步，但要先想清楚"谁有权代表客户来取"。
- **归档不参与计费**，也不进任何查询路径。它是冷数据。
- **端到端验证仍需 `terraform apply` + 真实调用**：开一个租户 → 各打一次
  流式/非流式 → 确认 blob 落在预期前缀、`truncated` 为 false、Cosmos 文档里
  `audit_blob` 与实际 blob 名一致 → 关掉开关 → 确认不再落盘。
