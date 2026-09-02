# 变更记录

每条改动记两件事：**改了什么**，以及**要在已有环境上生效需要做什么**。

第二件才是这份文件存在的理由。这套系统的配置写在**五个互不相干的时刻**，而"改代码 + 重新部署镜像"只覆盖其中一个：

| 写入时刻 | 写了什么 |
| --- | --- |
| `./scripts/update-app.sh -g <rg>` | 控制平面镜像（`app/` + `portal/`） |
| `./scripts/deploy.sh -g <rg>` | 上面全部 **+ hub 镜像 + terraform** |
| `terraform apply` | Azure 资源、角色分配、服务级诊断、控制平面 env |
| 门户「推送 GitHub 部署配置」 | 仓库变量 `HUB_IMAGE_REF` / `HUB_EVENTHUB_*` … |
| 门户「重新同步模型」 | **APIM 的 API 操作、策略、API 级诊断** |
| 重跑 `deploy-hub` workflow | 每个账号的 hub 容器 |

后三个既不是 terraform 资源、也不随镜像更新生效。2026-08-26 一天之内就有三起事故都源于此：新账号的 hub 起在 12 天前的镜像上；客户的计费页面从来没有数据；客户报的流式故障，修复其实几周前就合入了。

**共同点是漂移不会报错。** 陈旧的镜像 tag 是合法的 tag，空的 Event Hub 坐标是受支持的配置，旧策略是能工作的策略——三者都能部署、能运行、能应答请求。所以本文件的每一条都必须写明它需要哪个动作，否则读的人只会做那个覆盖面最小的。

> 体检：`python scripts/check_env.py -g <rg>` 会逐层比对"环境实际有什么"与"当前代码期望什么"，并指出该跑哪个动作。

---

## 2026-09-02

### 不再把 GitHub 的计费记录返回给客户端 · `a09f929` `4387d62`
**要生效：新控制平面镜像 + 重建 hub 镜像 + 每个账号重跑 `deploy-hub`**

⚠️ **只换控制平面镜像不生效。** 改动全在 hub 里，而 hub 是每账号一个容器、由 GitHub Action 部署的。

上游在**每一次**响应里都附带 `copilot_usage` —— token 数、`cost_per_batch`、`total_nano_aiu`，也就是我们的进货价。hub 原样转发，于是每个租户都能看到自己被收的零售价背后的批发价。实测确认：所有 26 次非流式 200 响应都带着它，`cost_per_batch` 是真实数值（input 为 5e11）。

规律是**调查出来的，不是猜的**：每个已注册模型在两种模式、四种协议下各调一遍，记录字段出现的 JSON 路径。结论是它**永远在顶层，从不嵌套** —— 包括 Responses API 的流里，它与 `response` **平级**而不在其内部，只看一个样本会判错。

所以脱敏是**一条规则**而不是四条，不按厂商也不按模型分支：删掉 JSON 对象顶层的 `copilot_usage`。按事件类型分支会漏掉 Google —— 它在**多个** chunk 上重复发送。

流式那一半的风险完全不同：生成器先把字节发给客户端，**事后**才从缓冲区扫出 `copilot_usage` 记账。剥错了对象（剥缓冲的那份而不是发出去的那份），每次流式调用都会变成 `unpriced` $0 —— 请求照常 200、客户端照常收到回答、日志无任何异常，只有月底对账才发现钱没记上。所以 `collected` 存原始、只重写 yield 出去的字节，并有专门的测试钉住这条。

标准 `usage` 字段保留：客户端要靠它数自己的 token，网关的 `llm-emit-token-metric` 也读它。

dev-19 与 dev-21 两套环境验证：四协议 × 两模式共 8 种组合全部剥离，Cosmos 里 `streamed` 真假两类文档的 `cost_source` 全部是 `copilot_usage`。

### 全新部署必然失败：镜像还没推完，terraform 就去建 Container App 了 · `d5a221e`
**要生效：无需动作（只影响全新部署）**

已有环境不受影响 —— 它们走 `update-app.sh`，那条路本来就是先构建后滚动。

`deploy.sh` 曾把完整 apply 丢到后台、与两个镜像构建并行，理由写在注释里：Container App「gated behind APIM ~30+ min，那时 4 分钟的构建早就完成了」。这句话在经典 APIM 层是对的，**在项目改用 StandardV2 之后就不成立了** —— 实测 APIM 只要 1分32秒，而 app 镜像要 7 分钟（它要先用 node 构建前端）。并行依赖的安全边际从 30 分钟塌到 90 秒，赛跑就不再是赛跑。

失败方式还很糟：Container App 停在 `provisioningState=Failed` 且没有任何修订，`terraform import` 拒绝接管它（"has not been provisioned successfully"），`bootstrap.sh` 在 apply 失败处退出、跳过 SP 创建 —— 客户第一次安装得到的是半成品环境和一句"资源已存在"的报错。

现在改成：单独 apply ACR（数秒）→ **阻塞**构建 app 镜像 → 再跑完整 apply。hub 镜像仍在后台并行，因为这次 apply 里没有任何资源引用它。

两次全新部署栽在旧脚本上；用新脚本的第一次（dev-21）一路跑到底，SP 也建了。

### 各模型该用哪个端点，写进了 README
**要生效：无需动作（纯文档）**

9 个模型（`gpt-5.x` 系列、`grok-4.5/4.6`）**只在 `/v1/responses` 上可用**，在 `/chat/completions` 上被上游拒绝，而门户把它们列为可用 —— 门户列的是模型目录，不是各自支持的协议。

更值得注意的是**流式发错端点会伪装成成功**：hub 在开始转发时就已发出 `200`，上游随后才拒绝，失败只能写在流体里。只看 HTTP 状态码的客户端会当成一次成功的空回答。计费不受影响（记的是上游真实状态 `400` + `unpriced`），但客户端必须自己解析流里的 `error` 事件。

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
