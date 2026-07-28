# 单个 GitHub 账号（Copilot 订阅）的吞吐容量

> 2026-07-20 在 dev-a12（新加坡，单 hub，1 vCPU）上实测。
> 一句话：**≈41.6K TPM / ≈5 RPS，24 并发是硬边界；超过就撞 Copilot 429，
> 触发 60 秒熔断后实际吞吐反而暴跌。**

---

## 1. 容量基线

| 指标 | 值 | 条件 |
| --- | --- | --- |
| **最大稳定 TPM** | **≈ 40–43k tokens/min** | 24 并发，`max_tokens=1200`，零 429；四个模型都落在这个区间 |
| **最大 RPS** | **≈ 2.7–4.6 req/s** | 取决于模型延迟：gpt-4o-mini ≈4.6，Opus ≈2.7–3.0 |
| **并发硬边界** | **24** | 48 并发所有模型都撞 429 |
| 单请求延迟 | gpt-4o-mini p50 ≈3.8s；Opus p50 ≈5.8–7.5s | 其中约 3.5s 是与模型无关的固定开销 |

**安全运行区**：≤24 并发。超了不是"慢一点"，是**吞吐崩塌**（见 §3.3）。
TPM 天花板与模型无关（配额是账号级），但**同样的 TPM，不同模型的 RPS 差近一倍**。

---

## 2. 实测数据

### 2.1 并发 × TPM（gpt-4o-mini，1 vCPU）

| 并发 | max_tokens | ok | 429 | 503 | RPS | TPM | 判断 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 24 | 400 | 48 | 0 | 0 | 4.43 | 39,434 | ✅ |
| **24** | **1200** | **48** | **0** | **0** | **4.63** | **41,602** | ✅ **最优** |
| 32 | 800 | 35 | 2 | 11 | 2.62 | 23,425 | ❌ 熔断拖累 |
| 40 | 800 | 47 | 1 | 0 | 1.06 | 9,528 | ❌ 崩塌 |
| 48 | 60 | 30–41 | 6–17 | 0–48 | 3.9–8.9 | 17K–40K | ❌ 不稳定 |

### 2.2 CPU 对比（同参数 24/48 并发）

| vCPU | 表现 | 结论 |
| --- | --- | --- |
| 0.5 | 4 并发即排队，p95 **38s**，**零 429** | ❌ 容器是瓶颈 |
| **1.0** | 峰值 8.86 RPS / 39.8K TPM，撞 429 | ✅ **够用** |
| 2.0 | 与 1 vCPU 无差异（波动主导） | 加钱无收益 |

→ **≥1 vCPU 后瓶颈就在上游配额，不在硬件。** hub 默认值因此设为 1 vCPU / 2Gi
（`vendored/gitmodel-hub/infra/variables.tf`）。注意 hub 锁死单副本（内存态
session 表不能跨副本），所以只能垂直扩，而 Consumption 层上限是 2 vCPU / 4Gi。

### 2.3 跨模型对比（24 并发 · `max_tokens=1200` · per-level 72）

| 模型 | TPM | RPS | p50 | p95 | 特征 |
| --- | --- | --- | --- | --- | --- |
| claude-opus-5 | **42,658** | 2.67 | 7.51s | 9.73s | 最慢，但单次产出最多 |
| gpt-4o-mini | **41,602** | 4.63 | 3.82s | 5.61s | 最快且均衡 |
| claude-opus-4.8 | **40,463** | 3.04 | 5.76s | 8.05s | 居中 |

**四个模型的 TPM 都落在 40k 附近** —— 再次说明瓶颈是**账号级配额**，与模型无关。
差异只体现在「怎么用掉这些 token」：Opus 延迟高、RPS 低，但单次生成的 token 多，
TPM 反而追平甚至略超 gpt-4o-mini。

触顶行为同样一致 —— 三个模型（gpt-4o-mini / claude-haiku / claude-opus-5）都在
**24 并发零 429、48 并发撞 429**：

| 模型 | 48 并发 | ok | 429 | 503 | 结果 TPM |
| --- | --- | --- | --- | --- | --- |
| gpt-4o-mini | 撞顶 | 30–41 | 6–17 | 0–48 | 17k–40k（不稳） |
| claude-opus-5 | 撞顶 | 37 | 11 | 45 | 19,345（低于 24 并发的 42.7k） |

→ Opus 超限后同样因熔断导致吞吐**倒退**，和 §3.3 的结论一致。

### 2.4 ⚠️ 样本量陷阱：慢模型尤其容易被低估

同样是 24 并发、同样的 `max_tokens`，只把 `--per-level` 从 48 提到 72：

| 模型 | per-level=48 | per-level=72 | 偏差 |
| --- | --- | --- | --- |
| claude-opus-4.8 | 17,798 | **40,463** | **+127%** |
| claude-opus-5 | 31,174 | **42,658** | +37% |

原因：Opus 的 p50 有 6–8 秒，48 个请求在 24 并发下只够跑两轮，wall-clock 里
线程池启动/收尾的空转占比过高，TPM 被严重稀释。

**纪律：`--per-level` 至少是并发数的 2–3 倍，慢模型取上限。** 对 24 并发，
gpt-4o-mini 用 48 尚可，Opus 必须 72 以上。

---

## 3. 三个反直觉的发现

### 3.1 限流按「请求数/并发」，不按 token

把 `max_tokens` 从 60 降到 10（TPM 只剩 8.5K，远低于 40K），**48 并发照样撞 429**。
如果限的是 TPM，这时该畅通无阻。→ Copilot 限的是并发请求数。

### 3.2 `max_tokens` 是上限，不是目标

| max_tokens | TPM | 增幅 |
| --- | --- | --- |
| 10 | 7,756 | — |
| 60 | 22,203 | +186% |
| 400 | 39,434 | +78% |
| 1200 | 41,602 | **+5%**（饱和）|

400 → 1200 只涨 5%，因为模型**自然停止**（一段关于海洋的话用不了 1200 token）。
想真正压满 TPM，得用强制长输出的 prompt，而不是单纯调大 `max_tokens`。

### 3.3 超限比不满载更糟

24 并发能跑 41.6K TPM；32 并发只有 23.4K，40 并发只剩 9.5K。
原因见下节 —— 一次 429 换来 60 秒全站不可用。

---

## 4. 熔断：为什么超限代价这么高

`ApimProvisioner._breaker_rules()` 给每个 backend 配了一条熔断规则：

```
count=1（一次就跳闸）· statusCodeRanges=[429, 500–599] · tripDuration=60s
```

链路：

```
请求 → hub → Copilot 返回 429
  → APIM 熔断器看到 429，立刻把该 hub 从池中摘除 60 秒
  → 单 hub 池此时无可用后端 → 之后 60 秒的请求 APIM 直接返回 503
```

**三种状态码要分清：**

| 码 | 来源 | 含义 |
| --- | --- | --- |
| 429 | Copilot 上游（经 hub 透传） | 账号配额打满 ← **真正的天花板** |
| 503 | **APIM 自己生成** | 熔断已开、池中无可用后端 |
| 429 | 我们的 `llm-token-limit` policy | 该 key 的 TPM 用完（inbound 拦截，**不触发熔断**）|

**运维含义：单账号 + 单 hub 是单点。** 熔断的设计初衷是 failover 到另一个账号的
hub（见 docs/APIM-LLM-Gateway.md §5.3），单 hub 时退化成"全挂 60 秒"。
→ **想提高可用性和容量，唯一有效手段是加第二个 GitHub 账号**（池自动分摊 + 熔断时
可 failover），本地调参/加 CPU 都突破不了。

---

## 5. 怎么复现这些测试

脚本：`tests/manual/load_test_ramp.py`（纯 stdlib，递增并发压测）。

```bash
# 默认档（探路）：1,2,4,8,16 并发，每级 12 请求
python tests/manual/load_test_ramp.py

# 找 TPM 上限（本文的最优点）
python tests/manual/load_test_ramp.py --levels 24 --per-level 72 \
  --max-tokens 1200 --timeout 180

# 找并发天花板（会撞 429，cooldown 要跨过 60s 熔断）
python tests/manual/load_test_ramp.py --levels 24,48 --per-level 48 --cooldown 70

# 换 provider（anthropic 原生 Messages API）
TF_PATH=/llm-anthropic/v1/messages TF_AUTH_HEADER=x-api-key \
TF_MODEL=claude-opus-5 python tests/manual/load_test_ramp.py \
  --levels 24 --per-level 72 --max-tokens 1200 --timeout 180
```

配置从仓库根 `.env` 读（`TF_GATEWAY_URL` / `TF_VIRTUAL_KEY`），也可命令行覆盖。
默认参数是脚本顶部的常量（`LEVELS` / `PER_LEVEL` / `MAX_TOKENS` / `TIMEOUT` /
`COOLDOWN` / `STOP_ERROR_RATE`），每个都有注释说明取值理由。

**两个测试纪律：**

1. **用无限额的 virtual key**（不设 `tokens_per_minute`），否则先撞我们自己的
   `llm-token-limit`，测到的是自家策略而非 Copilot 配额。
2. **`--per-level` 至少是并发数的 2–3 倍，慢模型取上限**。样本太小会严重低估慢模型：
   Opus 4.8 在 per-level=48 时只测出 17.8k TPM，提到 72 后是 40.5k（见 §2.4）。

---

## 6. 结论与建议

- **单个 GitHub 账号 ≈ 40–43k TPM / 24 并发**，这是 Copilot 订阅的配额，
  改硬件、调 `max_tokens`、加并发都突破不了。
- **TPM 上限与模型无关，RPS 与模型强相关**：gpt-4o-mini ≈4.6 RPS，Opus ≈2.7–3.0 RPS
  （Opus 延迟高但单次产出多，TPM 追平）。选模型时按「要吞吐还是要响应速度」权衡。
- **hub 用 1 vCPU / 2Gi 即可**（0.5 会排队，2 无额外收益）。
- **扩容唯一有效路径：加 GitHub 账号**。多 hub 既分摊负载，又让熔断有地方 failover，
  消除"一次超限 → 60 秒全挂"的单点。
- 生产容量规划时按 **≤24 并发/账号** 估算，并留出余量避免触发熔断。
- 压测时 `--per-level` 要够大（≥并发数×3，慢模型更甚），否则会严重低估（见 §2.4）。
