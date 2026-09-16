# 变更记录

每条改动记两件事：**改了什么**，以及**要在已有环境上生效需要做什么**。

第二件才是这份文件存在的理由。这套系统的配置写在**五个互不相干的时刻**，而"改代码 + 重新部署镜像"只覆盖其中一个：

| 写入时刻 | 写了什么 |
| --- | --- |
| `./scripts/update-app.sh -g <rg>` | 控制平面镜像（`app/` + `portal/`） |
| `./scripts/deploy.sh -g <rg>` | 上面全部 **+ hub 镜像 + terraform** |
| `terraform apply` | Azure 资源、角色分配、服务级诊断、控制平面 env |
| 门户「推送 GitHub 部署配置」 | 仓库变量 `HUB_IMAGE_REF` / `HUB_EVENTHUB_*` … |
| 门户「重新同步模型」 | **APIM 的 API 操作、API 级与操作级策略、策略引用的命名值、API 级诊断** |
| 重跑 `deploy-hub` workflow | 每个账号的 hub 容器 |

后三个既不是 terraform 资源、也不随镜像更新生效。2026-08-26 一天之内就有三起事故都源于此：新账号的 hub 起在 12 天前的镜像上；客户的计费页面从来没有数据；客户报的流式故障，修复其实几周前就合入了。

**共同点是漂移不会报错。** 陈旧的镜像 tag 是合法的 tag，空的 Event Hub 坐标是受支持的配置，旧策略是能工作的策略——三者都能部署、能运行、能应答请求。所以本文件的每一条都必须写明它需要哪个动作，否则读的人只会做那个覆盖面最小的。

> 体检：`python scripts/check_env.py -g <rg>` 会逐层比对"环境实际有什么"与"当前代码期望什么"，并指出该跑哪个动作。

---

## 2026-09-16

### Responses 会话钉在同一个 hub 上 · `154c523` `850f0aa`
**要生效：新镜像 + 门户「重新同步模型」**

两步缺一不可。新镜像只是让生成策略的代码存在;策略和它依赖的命名值 `tf-hub-pool-joins` 是在「重新同步模型」那一刻才写进 APIM 的。**只更新镜像,网关行为一点不变。**

```bash
# 1. 控制平面镜像（app/ + portal/）
./scripts/update-app.sh -g <rg>
```

**2.** 门户 →「GitHub 账号」→ 任意一个 READY 账号 →「重新同步模型」

**不需要** `terraform apply`、不需要重新部署 hub、不需要「推送 GitHub 部署配置」。

同步**任意一个**账号即可 —— `ensure_pooled_provider_api()` 配的是 API 与池子,与具体账号无关,不必挨个点。但要知道这个按钮**自身**的既有副作用(非本次引入):它以 `prune=True` 运行,会删掉 hub 目录里已不存在的 `PLATFORM` 模型路由;`TENANT`(BYO)路由不受影响。

验证(`--output-file` 不能省:`az rest` 在 Windows 上会被策略 XML 的 BOM 噎住):

```bash
B="https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ApiManagement/service/<apim>"
az rest --method get --output-file p.xml \
  --url "$B/apis/llm-openai/operations/responses/policies/policy?api-version=2024-05-01&format=rawxml"
grep -c "tf-hub-pool-joins" p.xml   # 1 = 策略已就位
az rest --method get --url "$B/namedValues/tf-hub-pool-joins?api-version=2024-05-01" \
  --query "properties.value" -o tsv
```

最后一条应当列出池子里的**全部**后端。**在池子里却不在这个映射里的后端,接不到任何被钉住的会话** —— 出现这种情况说明它是绕过控制平面加进去的。

把我们的端点配进 Codex 跑长任务,会反复看到:

```
stream disconnected before completion: stream closed before response.completed
```

Codex 把它当成连接故障,最多重试 5 次;dev-19 在 2026-09-15 有 **127 次调用里 69 次**中招。它看起来像超时,我起初也是这么判的(先怪到 APIM 的 300 秒默认值,又怪到推理烧光 `max_output_tokens`)—— **两个都被实测推翻了**。

真正的原因在 hub 日志里,69 条无一例外是同一句上游原文:

```
401 {"error":{"message":"input item does not belong to this connection"}}
```

Codex 用 `store: false` 跑,于是要求 `include: reasoning.encrypted_content`,把上一轮的输出项连同加密的推理内容回填进下一轮的 `input`。**那个加密块是用生成它的 Copilot 连接的密钥封的**,换一个 hub 就解不开。而此时 hub 早已发出 `200 OK`(`StreamingResponse` 在生成器体运行之前就把响应头写出去了),所以这个 401 只能以流内 `error` 事件的形式补发,流就此结束、没有 `response.completed`。

APIM 的池亲和本来就能解决,只是**从未生效**:它签发的 `SessionId` cookie 需要客户端带回来,而 Codex 基于 reqwest,cookie store 默认关闭。所以现在由网关按会话自己算出这个 cookie —— 依次取 `thread-id`、`session-id`、body 里的 `prompt_cache_key`。三者都没有的请求**不钉**,维持轮询;用订阅密钥兜底会把一整把密钥的流量压在一个账号上,代价太大。

选 rendezvous 散列而不是 `hash % N`,是因为重映射在这里**不是变慢而是卡死**:加密块留在旧 hub 上,5 次重试全部确定性地打在新 hub,一次都不会对。3 万条会话的模拟里,新增第 4 个 hub 时 `% N` 要迁移 75%、rendezvous 25%;再加上按加入时间过滤(**新 hub 对比它更早的会话不可见**)后降到 0。删除 hub 两者都是 33%,且都是非迁不可的那部分。

只挂在 `responses` 这一个操作上,因为别的都不需要:`chat/completions` 和 Anthropic 原生 Messages 实测都能跨 hub 重放;而 Copilot 的 prompt cache **本来就跨 hub 共享** —— 456 次 Anthropic 调用均分在三个 hub 上,命中率仍有 96%。顺带修正了 `ensure_pooled_provider_api` 文档串里"亲和是为了 prompt cache 保温"这个说法,至少对 Anthropic 不成立。

⚠️ **开启这个策略会打断所有在途的 Responses 会话。** 它们的历史里混着多个 hub 的加密块,钉到任何一个都解不开 —— 而且比开启前更糟:之前靠重试还能碰上正确的 hub,之后 5 次重试全部落在同一台,永久卡死。**升级后让用户开新会话。** 新增账号不会有这个问题(加入时间过滤挡住了),只有本次上线这一次。

命名值 `tf-hub-pool-joins` 由 `add_hub_to_pools` / 账号移除自动维护。回填分两种情形,不能混:**首次建映射**(也就是本次升级)时把池中已有成员记为 `0`——它们确实早于所有在途会话,记别的会把整个池子对现有流量藏起来;**映射已存在、却冒出一个没见过的成员**(有人绕过控制平面手工加进池子)则记当前时间——给它 `0` 会让它成为更早那些会话的候选,把它们重映射到一台解不开其加密块的 hub 上,重演一次上面那个 ⚠️。

因为命名值是按请求解析的,**加删账号只改这个映射,不需要重推策略**,也就不存在"策略里还写着一个已经离开池子的后端"的时间窗。

---

## 2026-08-28

### Hub 失去 Copilot 登录时，门户会说出来 · `4db6699`
**要生效：新镜像**

`GitHubAccount.status` 是**部署**状态机：容器起来就 READY，之后再也不动，所以它无法表达"这个 hub 后来坏了"。

而 token 过期的 hub **依然会应答** —— 每个请求返回 503 "Hub not logged in to Copilot"。503 落在熔断器的 5xx 范围内，于是 APIM 摘掉它 60 秒、放回来、再摘掉。流量确实绕开了，**这正是没人发现的原因**：池子悄悄跑在 2/3 容量上，而三个账号在页面上全是绿的。

新增每 5 分钟一轮的轮询（复用早已写好但从无调用者的 `hub_client.fetch_status`），门户「Hub 健康」列显示三态：**已登录 / 登录失效 / 未知**。

「未知」不会被画成绿色。"没问过"不是健康——一个刚部署的 hub 和一个确认在工作的 hub 必须看起来不一样。同理，**连不上的 hub 不记为"登录失效"**：问不到和问了说没有，需要的处置完全不同。

顺带：
- 徽章不再折行（加了第 5 列后被挤到换行）
- 账号计数改为 `N / 30`，满 30 禁用添加。30 是 Azure 对后端池成员数的硬上限，而每个账号在每个 provider 池里恰好占 1 个位

**数据库**：新增一列 `github_accounts.hub_logged_in`（可空、无默认值）。由 `init_db` 的 `ADD COLUMN IF NOT EXISTS` 在启动时自动补，**不需要手工迁移**。可空是刻意的：已有行从未被轮询过，默认成 `true` 就等于凭"从没问过"断言每个 hub 都好着。

---

## 2026-08-27

### 成本明细显示虚拟密钥所属项目 · `5855850`
**要生效：新镜像**

按 `subscription` 分组时只显示 `vk_9c8b9aff08dc`——一个可靠但没用的标签。现在显示 `vk_9c8b9aff08dc (搜索团队)`。

前端零改动：门户早就会渲染 `label`，只是服务端从没给这个维度下发过。

### 环境体检脚本 · `904369d`
**要生效：无（新增独立脚本，不改任何现有代码）**

```bash
python scripts/check_env.py -g <resource-group>          # 只读体检
python scripts/check_env.py -g <rg> --terraform-plan     # 额外检查 plan 是否有破坏性变更
python scripts/check_env.py -g <rg> --fix                # 重新发布仓库变量 + 重新同步模型
```

七层逐项比对，**期望值从代码里 import 而不是抄一份**——`PROVIDER_APIS` 说每个 API 该有哪些操作，所以以后加操作，检查器自动就会要求它。

`--terraform-plan` 只 plan 不 apply，出现下列任一就停下：任何 destroy/replace；`db_url` / `jwt` / `admin_pwd` 三个 KV secret 有变更（原始 tfvars 丢失时临时填新值会让**所有已登录会话失效**）；Container Apps 的 workload profile 被移除。

---

## 2026-08-26

### 新建 hub 会用几周前的镜像 · `8e2fbd5`
**要生效：新镜像 + `terraform apply`**

⚠️ **terraform 那一步不能省。** 改动依赖一条新的角色分配：控制平面的**系统标识**在 ACR 上的 `AcrPull`。少了它，镜像解析会失败并静默回退到旧 tag——修复看起来上线了，实际是空转。

新 hub 的镜像来自 `HUB_IMAGE_REF` 仓库变量，而那个变量**只在有人点门户「推送部署配置」时才被写**。deploy.sh 不写、terraform 不写、update-app.sh 更不写。于是它漂移，且漂移不可见：陈旧的 tag 是合法字符串，hub 照常部署、拉取、运行，只是跑着两周前的代码。

dev-19 实测：变量停在 08-14，而三个在跑的 hub 是 08-25 的镜像，ACR 里有 6 个比配置的更新。

现在控制平面在 dispatch **之前**先问 ACR 要最新的 `gitmodel` tag。回退链：ACR 最新 → `TF_HUB_IMAGE_TAG` → 保持不动。

### 调用记录的「模型」列显示模型名 · `4428a21`
**要生效：新镜像**

该列渲染的是 `api ?? route`，而 `api` 每条文档都有值，所以回退分支**从未执行过**——整列显示的都是 `llm-anthropic` / `llm-google`。

这个写法在当初是对的：那时文档由 APIM 自己捕获，而它的 metric policy 读不到请求体，`route` 就是字面量 `"unknown"`。管线换成 hub 发事件后 `route` 有了真值，这个兜底就变成了拿差字段挡住好字段。

---

## 2026-08-25

### hub 转发上游接受的 beta · `8c7fe36`
**要生效：新镜像 + 重建 hub 镜像并重跑 `deploy-hub` + 门户「重新同步模型」**

⚠️ 三步都要。hub 的改动在 hub 镜像里；`count_tokens` 的 APIM 操作只在同步模型时才写入。**只换控制平面镜像的话，两样都不会生效。**

`context_management` 曾被列为"上游拒绝的字段"。观察真实且可复现，结论却是错的：上游接受它，但**必须带上 `anthropic-beta` 头**，而 hub 只转发 `anthropic-version`、把 beta 头整个丢掉。于是字段永远裸奔、永远 400，剥掉它之后 400 消失，证据也一并消失。

逐字转发 beta 头同样是错的，而且上线炸过：上游对 beta 列表是**全有或全无**校验，Claude Code 一次发十个，一个不认识的就整条失败。所以是允许列表，九个逐一验证过。

同时给 APIM 的 anthropic API 补上 `/v1/messages/count_tokens` 操作。**已有环境在点「重新同步模型」之前不会有它**——客户端收到 404 后静默退化成估算上下文长度，不报错。

---

## 更早（8/5 – 8/21）需要额外动作的改动

只列需要镜像之外动作的。完整历史见 `git log`。

| 提交 | 日期 | 要生效 | 内容 |
| --- | --- | --- | --- |
| `c021bdc` | 08-20 | 新镜像 + terraform + **同步模型** | API 级诊断与采样率控制 |
| `3355570` | 08-20 | terraform | tfvars 示例补全 |
| `02436b0` | 08-17 | terraform | APIM / App Insights 采样率变量 |
| `27a8e02` | 08-15 | terraform | 移除无用的 Azure Monitor 诊断设置 |
| `905af1f` | 08-13 | 新镜像 + **同步模型** | 精简 usage trace 的 metadata |
| `f28d113` | 08-11 | **重建 hub 镜像 + 重跑 deploy-hub** | 兼容两种凭据头，剥离上游拒绝的字段 |
| `26238d4` | 08-08 | terraform | terraform 真实 outputs、关闭 LlmLog、Cosmos 吞吐开关 |
| `60ee451` | 08-08 | **重建 hub 镜像 + 重跑 deploy-hub** | 停止丢失用量事件，停止把失败记成成功 |
| `db07a94` | 08-08 | 新镜像 + **同步模型** | 删除某 provider 最后一个 hub 时必然失败 |
| `c6dee0a` | 08-06 | 新镜像 + terraform + **推送部署配置** | 停止回滚已部署镜像，拆分两个镜像 tag |
| `b17a066` | 08-06 | 新镜像 + **同步模型** | 删除最后一个 hub 会在活动池里留下孤儿后端 |

**如果一套环境停在 8 月初**，需要按 `docs/DEPLOYMENT.zh.md`「更新已有环境」的顺序完整走一遍：terraform apply → `deploy.sh` → 推送部署配置 → 每个账号重跑 deploy-hub → 重新同步模型。顺序有硬依赖，先跑 `check_env.py` 看差在哪。
