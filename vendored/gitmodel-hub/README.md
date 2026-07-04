# GitModel Hub

一个本地运行的**模型 API 网关**：用个人 GitHub Copilot 订阅作为后端，对外同时暴露
**OpenAI 兼容** 与 **Anthropic 兼容** 接口，可直接给 **Codex**、**Claude Code**、
OpenAI / Anthropic SDK、`curl` 等下游客户端使用。自带一个 **Web 门户**，支持
GitHub 登录、token 用量统计与费用估算、API key 管理。

> ⚠️ **免责声明**：在 IDE 正常编辑流程之外使用 Copilot 订阅违反 GitHub Copilot
> 服务条款，账号有被封风险。本项目仅供 **个人学习研究**，请勿用于生产或对外服务。

---

## ✨ 功能

- **OpenAI 兼容**：`GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/responses`（含流式）
- **Anthropic 兼容**：`POST /v1/messages`（含流式、工具调用 `tool_use` / `tool_result`、图片输入）
- **自动鉴权**：完成 Copilot 两段式鉴权（长期 OAuth token → 短期 API token，内存缓存并自动续期）
- **模型名兼容**：官方 Anthropic 模型 ID 自动映射到 Copilot slug，未识别的名字原样透传
- **Web 门户**
  - GitHub Device Flow 登录
  - 用量统计：总量 / 按模型 / 按天趋势 / 最近请求明细
  - **费用估算**：按可配置的模型单价（美元 / 百万 token）换算预估成本
  - 可用模型列表
  - 生成 / 复制 / 吊销下游客户端用的本地 API key
- **安全**
  - 后台账号密码登录（默认 `admin` / `admin`，**首次使用请立即修改**）
  - 登录失败限流（按 IP 锁定，防暴力破解）
  - `/v1/*` 可选 API key 鉴权

---

## 🚀 快速开始

### 本地运行（可选）

> 不想在宿主机安装 Python？直接用下面的 [Dev Container](#dev-container)，
> 环境与依赖都在容器内自动就绪。

```powershell
pip install -r requirements.txt
python -m hub
```

默认监听 `http://127.0.0.1:8088`，浏览器打开即可看到门户。

1. 用默认账号 `admin` / `admin` 登录后台（**随后在「设置」里改掉**）。
2. 在「概览」点击「开始登录」，按提示完成 GitHub 授权（Device Flow）。
3. 在「API Keys」生成一个 key 给下游客户端使用。

### Dev Container

已内置 [Dev Container](https://containers.dev/) 配置（`.devcontainer/devcontainer.json`），
基于官方 `python:3.13` 镜像（与生产对齐），用 **pip** 安装依赖。

在 VS Code 中安装 **Dev Containers** 扩展后，命令面板执行
**Dev Containers: Reopen in Container**，容器创建时会自动
`pip install -r requirements.txt`。随后在容器内运行：

```bash
python -m hub
```

容器已绑定 `0.0.0.0:8088` 并转发到宿主机，浏览器打开 `http://localhost:8088` 即可。

#### 内置工具与部署

容器通过 Dev Container Features 预装了 **Terraform** 与 **Azure CLI**，可直接在容器内
部署 `infra/`（见下方 Azure 一节）。首次部署前需先登录 Azure：

```bash
az login        # 无浏览器环境会给出 device code，在宿主机浏览器完成
cd infra && terraform apply
```

> ⚠️ **Terraform 版本对齐**：容器内的 Terraform 锁定为 `1.9.8`，与宿主机工具链一致。
> 因为 `infra/terraform.tfstate` 在宿主机与容器间共享，而 state 文件**不向后兼容**，
> 切勿在容器内升级 Terraform，否则宿主机将无法再读取该 state。
>
> `.devcontainer/devcontainer-lock.json` 锁定了 Features 的精确版本（类似
> `package-lock.json`），请一并提交以保证团队/CI 构建一致。

### Docker

```bash
docker compose up -d --build
```

数据（SQLite，含 Copilot token / 后台凭据 / 用量）持久化到 `./db-container`。

---

## ⚙️ 配置（环境变量 / `.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HUB_HOST` | `127.0.0.1` | 监听地址 |
| `HUB_PORT` | `8088` | 端口 |
| `HUB_DATA_DIR` | 项目下 `db/` | SQLite 与 token 存放目录 |
| `HUB_REQUIRE_AUTH` | `false` | 为 `true` 时 `/v1/*` 必须带有效 key（对外暴露时建议开启） |
| `HUB_ADMIN_TOKEN` | 空 | 可选的后台「万能令牌」，带 `x-admin-token` 即放行，免账号密码 |
| `HUB_LOGIN_MAX_FAILS` | `5` | 同一 IP 连续登录失败多少次后锁定（设 `0` 禁用限流） |
| `HUB_LOGIN_LOCK_SECONDS` | `900` | 锁定时长（秒） |
| `COPILOT_OAUTH_TOKEN` | 空 | 直接注入已有的 `ghu_…` token，跳过登录 |

---

## 🔌 客户端接入

### Codex / OpenAI

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8088/v1"
export OPENAI_API_KEY="<门户生成的 key>"   # 关闭鉴权时可任意填
# 模型: gpt-5.5 / gpt-4.1 / gpt-5.3-codex ...
```

### Claude Code / Anthropic

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8088"
export ANTHROPIC_AUTH_TOKEN="<门户生成的 key>"
# 模型: claude-sonnet-4.6 / claude-opus-4.7 ...
```

### Google Gemini（OpenAI 兼容）

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8088/v1"
export OPENAI_API_KEY="<门户生成的 key>"
# 模型: gemini-2.5-pro / gemini-3-flash-preview / gemini-3.1-pro-preview / gemini-3.5-flash
```

> **注意**：Gemini 是推理模型，可见输出前会先消耗大量 reasoning token，`max_tokens`
> 请给大些（如 `2048`），否则容易被推理占满导致输出被截断。建议走上面的
> **OpenAI 兼容端点**（`/v1/chat/completions`），路径最直接。

> **模型名兼容**：直接传 Copilot 的 slug（如 `claude-sonnet-4.6`）即可；也支持官方
> Anthropic 模型 ID（如 `claude-3-5-sonnet-20241022`、`claude-sonnet-4-5-20250929`），
> 网关会自动映射到对应的 Copilot slug，未识别的名字按原样透传。映射表见
> `hub/anthropic_adapter.py` 的 `MODEL_ALIASES`。实际可用的模型 ID 以门户
> 「可用模型列表」/ `GET /api/models` 为准。
>
> **图片输入（多模态）** 已支持：网关在检测到图片内容时自动附加
> `Copilot-Vision-Request: true` 头。

### 图片生成（Azure GPT Image）

网关可挂一个 **Azure OpenAI `gpt-image`** 部署作为图片后端，对外暴露 OpenAI 兼容的
`POST /v1/images/generations`（文生图）与 `POST /v1/images/edits`（图生图 / 局部重绘）。

先在门户 **「设置 → 图片生成」** 填入 Azure 的 **Endpoint** 与 **API Key**（保存在本地
`db/` 的 SQLite，不写入代码 / 环境变量；endpoint 填完整的
`.../openai/v1/images/generations` 或裸的 `.../openai/v1` 均可），然后：

```bash
curl http://127.0.0.1:8088/v1/images/generations \
  -H "Authorization: Bearer <门户生成的 key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-image-2","prompt":"a tiny red cube on white","size":"1024x1024"}'
```

- **模型**：`gpt-image-2`
- **常用参数**：`prompt`（必填）、`n`、`size`（宽高均需被 16 整除，如 `1024x1024` /
  `1536x1024` / `1024x1536` / `auto`）、`quality`（`low` / `medium` / `high` / `auto`）、
  `output_format`（`png` / `jpeg`）、`background`（`transparent` / `opaque` / `auto`）、
  `output_compression`（jpeg，0–100）
- **返回**：始终是 base64（`data[].b64_json`，无 `url` 模式）；自带 `usage`
  （`input_tokens` / `output_tokens` / `total_tokens`），用量与费用照常计入门户统计
- **图生图 / 局部重绘**：`/v1/images/edits` 用 `multipart/form-data` 上传
  `image`（及可选 `mask`）+ `prompt` 等字段

> **限速**：Azure S0 套餐对 `gpt-image` 限速很严（约每 50 秒 1 次），超限会原样返回
> `429`，请自行控制调用频率。

### curl 自测

```bash
curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4.1","messages":[{"role":"user","content":"say ok"}]}'
```

---

## 📊 用量与费用

门户「用量统计」展示请求数、输入/输出 token、按模型与按天的明细，并给出**预估费用**。

- 费用按「设置 → 模型单价」里的单价表（美元 / 百万 token）换算，默认值取自 Anthropic
  官方公开定价，可自行编辑。
- 流式请求若后端未返回 `prompt_tokens`，网关会用本地估算兜底，并在记录上标注「估算」。
- 由于走的是 Copilot 包月订阅，费用栏是「等价 API 成本」参考，**并非真实扣费**。

---

## ☁️ 部署到 Azure（可选）

`infra/` 提供一套 Terraform，**一条 `terraform apply` 即可**完成「创建专属 ACR →
在云端构建镜像（`az acr build`，无需本地 docker）→ 部署到 Azure Container Apps」：

- 自动新建一个 Basic SKU 的 ACR（随机后缀命名，规避全球唯一名冲突）
- 镜像 tag 由源码（`hub/` + `Dockerfile` + `requirements.txt`）内容哈希自动生成：
  **改了代码再 `apply` 就会自动重建并滚动出新 revision**，不动代码则 apply 幂等、不重复构建
- 用 user-assigned identity 从该 ACR 拉镜像（无需 admin 凭据）
- 自动新建一个 Standard LRS 存储账户 + file share（随机后缀命名），Azure Files 挂载到
  `/data` 持久化 SQLite；挂载用 `nobrl` 选项关闭 SMB 字节范围锁（否则全新 share 上首次建库
  会报 `database is locked`）
- 单副本（SQLite 为 WAL 单写者，**切勿多副本**）
- 资源组在创建时即打上 `SecurityControl = Ignore` 标签

### 前置条件

**无需任何前置 Azure 资源** —— 资源组、ACR、镜像、存储账户、file share 全部由 Terraform
自动创建。SQLite 数据库与表结构由应用启动时（`store.py` 的 `init_db()`）自动建立。

只需具备：Azure CLI 已 `az login`，且对目标订阅有创建资源的权限（建资源组 + 角色分配，
需 Owner 或 Contributor+User Access Administrator）。

### 部署

```bash
cd infra
terraform init
terraform apply
```

`terraform.tfvars` 里**没有必填变量** —— 所有变量都有默认值（资源组名、区域、ACR/存储名、
镜像名/tag…）。如需自定义，复制 `terraform.tfvars.example` 后覆盖即可。

> **存储说明**：存储账户与 file share 都在应用所在资源组内由 Terraform 创建（不再依赖外部
> 存储账户 / connection string）。`*.tfstate` 含敏感信息，已被 `.gitignore` 排除，**切勿提交**。

### 可配置变量

所有变量定义在 `infra/variables.tf`（`default` 即默认值）。**全部变量都有默认值**，
按需在 `terraform.tfvars` 覆盖，或用 `terraform apply -var="名=值"` 临时覆盖。
**优先级**：`tfvars` / `-var` > `variables.tf` 的 `default`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `resource_group_name` | `gitmodel-rg` | 新建的资源组名 |
| `location` | `centralus` | Azure 区域 |
| `prefix` | `gitmodel` | 资源名前缀（身份 / 环境 / App / ACR 名都基于它）|
| `image_name` | `gitmodel` | 镜像仓库名（tag 由源码哈希自动生成）|
| `file_share_name` | `gitmodel-data` | 挂载的 Azure Files share 名 |
| `container_port` | `8088` | 容器内监听端口 |
| `require_auth` | `true` | `HUB_REQUIRE_AUTH`，公网暴露时务必保持 |
| `login_max_fails` | `5` | 登录失败锁定阈值（`0` 关闭限流）|
| `login_lock_seconds` | `900` | 锁定时长（秒）|
| `cpu` | `0.5` | 每副本 vCPU |
| `memory` | `1.0Gi` | 每副本内存（需与 `cpu` 匹配）|

部分资源名在 `main.tf` 中由 `prefix` 拼接而成：

| 资源 | 命名规则 | 示例 |
| --- | --- | --- |
| 资源组 | `var.resource_group_name` | `gitmodel-rg` |
| ACR | `${prefix}` + 随机 8 位后缀 | `gitmodel8df1h6i9` |
| 托管身份 | `${prefix}-id` | `gitmodel-id` |
| Container 环境 | `${prefix}-env` | `gitmodel-env` |
| Container App | `${prefix}-hub` | `gitmodel-hub` |

> ⚠️ **改名会触发重建**：`resource_group_name`、`prefix` 等是资源身份的一部分，
> 部署后再修改会让 Terraform **销毁旧资源并重建**。这类名字请在**首次部署前**定好。

> **公网暴露注意**：`ingress.external_enabled = true` 会把服务挂到公网。务必
> ① 立即修改后台默认密码；② 保持 `HUB_REQUIRE_AUTH=true`；③ 视情况调低
> `login_max_fails`。`terraform.tfvars` 与 `*.tfstate` 含敏感信息，已被 `.gitignore` 排除，**切勿提交**。

---

## 📁 项目结构

```
hub/
  __main__.py           # python -m hub 入口
  config.py             # 环境变量配置
  store.py              # SQLite：oauth token / api keys / usage / 价格表
  copilot_client.py     # Copilot 异步客户端 + Device Flow 鉴权
  anthropic_adapter.py  # Anthropic <-> OpenAI 翻译（流式 / 工具 / 图片 / 模型别名）
  server.py             # FastAPI 应用与所有端点（含登录限流）
  static/index.html     # Web 门户（单文件）
infra/                  # Terraform：Azure Container Apps 部署
Dockerfile
docker-compose.yml
```

---

## 📜 许可

仅供个人学习研究，使用风险自负。
