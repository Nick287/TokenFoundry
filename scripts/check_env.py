#!/usr/bin/env python3
"""Token Foundry — environment health check: what this deployment has vs what
the current code expects.

WHY THIS EXISTS
---------------
An environment's configuration is written at FIVE unrelated moments, and
"change the code, redeploy the image" covers exactly one of them:

    terraform apply          -> Azure resources, roles, service-level diagnostic,
                                control-plane env
    deploy.sh / update-app   -> images
    portal "push deploy cfg" -> repo variables HUB_IMAGE_REF / HUB_EVENTHUB_* ...
    deploy-hub workflow      -> each account's hub container
    portal "resync models"   -> APIM API operations, policies, API diagnostics

The last three are neither terraform resources nor image content. Three separate
incidents in a single day (2026-08-26) all traced to one of them:

  * a new account's hub came up on a twelve-day-old image, because HUB_IMAGE_REF
    had not been re-published since 08-14;
  * a customer's billing page had no data, because HUB_EVENTHUB_* was published
    empty and the hub then reports nothing at all, silently and by design;
  * a customer saw a streaming bug whose fix had shipped weeks earlier, because
    their APIM policy was still the pre-fix one.

`count_tokens` is the next of the same kind: the `/v1/messages/count_tokens`
operation was added to `llm-anthropic` on 2026-08-25, and no existing environment
has it until somebody presses "resync models". Clients get a 404 and quietly
degrade to estimating context size — nothing errors, so nobody finds out.

WHAT MAKES THIS DIFFERENT FROM A CHECKLIST
------------------------------------------
The EXPECTATIONS ARE IMPORTED, not transcribed. `PROVIDER_APIS` says which
operations each API must have; `_repo_variables()` says which variables must be
published and what they should hold. A list copied into this file would drift the
first time someone adds an operation and forgets to update the checker — and a
checker that is quietly out of date is worse than none, because it reports green.

SCOPE
-----
Read-only by default. `--fix` performs four things, all of which preserve
existing data: rebuild+push both images, roll the control plane, re-publish the
repo variables, and resync one account's catalog (which is what lands
`count_tokens`). It NEVER runs terraform, never redeploys a hub, never touches
the deployment SP or Key Vault.

Terraform is planned but never applied — `--fix` prints the plan's verdict and
stops if it would destroy or replace anything. See `check_terraform`.

USAGE
-----
    az login && az account set --subscription <id>
    python scripts/check_env.py -g tokenfoundry-rg-dev-19
    python scripts/check_env.py -g <rg> --fix

Control-plane admin credentials are needed only for `--fix` (and for the model
route listing). Pass --admin-user/--admin-pass, or set TF_ADMIN_USERNAME and
TF_ADMIN_PASSWORD in the environment. They are never written to disk.

Requires: `az` on PATH and logged in, plus this repo's dependencies (the script
imports from app/ to read the expectations).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402

# The expectations. Imported rather than restated — see the module docstring.
from app.services.acr import newest_tag  # noqa: E402
from app.services.apim_provisioner import PROVIDER_APIS  # noqa: E402

# terraform colours its errors even with a pipe; the codes make the report
# unreadable when it is pasted into an issue.
_ANSI = re.compile("\x1b\[[0-9;]*m")

OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"
_MARK = {OK: "  OK  ", WARN: " WARN ", BAD: " FAIL ", INFO: "  ..  "}

# Control-plane env vars whose absence breaks a feature SILENTLY. Each is listed
# with what stops working, because "TF_USAGE_CAPTURE_STORAGE_ACCOUNT is unset" is
# not actionable on its own — the importer disables itself with one log line and
# the billing page simply stays empty.
REQUIRED_APP_ENV = {
    "TF_USAGE_CAPTURE_STORAGE_ACCOUNT":
        "usage importer disables itself; Cosmos never receives any usage document",
    "TF_EVENTHUB_FQDN":
        "published to hubs as HUB_EVENTHUB_FQDN; empty means hubs emit nothing",
    "TF_EVENTHUB_NAME":
        "same — the hub's producer has nowhere to send",
    "TF_EVENTHUB_NAMESPACE_ID":
        "the hub terraform scopes its Event Hubs Data Sender role to this",
    "TF_HUB_IMAGE_TAG":
        "fallback image tag for new hubs; empty makes HUB_IMAGE_REF unpublishable",
    "TF_ACR_NAME":
        "the control plane cannot ask the registry for the newest hub image",
    "TF_COSMOS_ENDPOINT":
        "usage store unreachable; the billing page has no source",
}

# Repo variables that are legitimately allowed to be empty vs. those that are not.
# `_repo_variables()` publishes the Event Hub coordinates without complaint when
# they are blank ("Empty is a valid state (no Event Hub deployed)") — true in
# general, and the exact way a customer ended up with a hub that never reported.
MUST_NOT_BE_EMPTY = (
    "HUB_IMAGE_REF", "HUB_ACR_NAME", "HUB_ACR_RG", "HUB_KEYVAULT_NAME",
    "TFSTATE_STORAGE_ACCOUNT", "TFSTATE_CONTAINER",
    "HUB_EVENTHUB_FQDN", "HUB_EVENTHUB_NAME", "HUB_EVENTHUB_NAMESPACE_ID",
)


@dataclass
class Finding:
    layer: str
    name: str
    state: str
    detail: str
    remedy: str = ""


@dataclass
class Env:
    """Everything discovered from the resource group, so no local .env is read.

    Deliberate: this has to run against SOMEBODY ELSE'S environment, where a
    local .env either does not exist or — worse — describes a different one.
    """
    rg: str
    subscription: str = ""
    apim: str = ""
    acr: str = ""
    aca: str = ""
    aca_fqdn: str = ""
    cosmos: str = ""
    eventhub_ns: str = ""
    capture_sa: str = ""
    app_image_tag: str = ""
    app_env: dict[str, str] = field(default_factory=dict)
    hub_rgs: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# az plumbing                                                                 #
# --------------------------------------------------------------------------- #
def _az_binary() -> str:
    """Resolve the az executable once, and fail loudly if it is not there.

    shutil.which is what makes this work on Windows: the CLI installs as `az.cmd`,
    and subprocess with an argument list bypasses PATHEXT resolution, so a bare
    "az" raises FileNotFoundError there.

    Exiting rather than degrading is deliberate. Every check below reads Azure
    through this, so an unlaunchable az would make the whole report read
    "everything is missing" — a checker confidently describing an environment it
    never actually looked at, which is worse than one that refuses to run.
    """
    found = shutil.which("az")
    if not found:
        raise SystemExit(
            "找不到 az 命令。请先安装 Azure CLI 并 `az login`。\n"
            "（本脚本的每一项检查都要通过 az 读 Azure，缺了它只会输出一份"
            "「什么都没有」的假报告，所以这里直接退出。）"
        )
    return found


_AZ: str | None = None


def az(*args: str, default: Any = None) -> Any:
    """Run an `az` command and parse its JSON, or return `default` on failure.

    Failure is normal here: a resource that does not exist IS the finding, so a
    non-zero exit must not abort the run. Errors surface as an absent value that
    the caller reports in its own words. The one failure that is NOT normal —
    az being unavailable at all — is caught in `_az_binary` before any check runs.
    """
    global _AZ
    if _AZ is None:
        _AZ = _az_binary()
    try:
        p = subprocess.run(                                    # noqa: S603
            [_AZ, *args, "-o", "json"],
            capture_output=True, text=True, timeout=180, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return default
    if p.returncode != 0 or not p.stdout.strip():
        return default
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return default


_ARM_TOKEN: str | None = None


def arm_get(path: str, **params: str) -> tuple[Any | None, str]:
    """GET an ARM resource as JSON. Returns (payload, error) — never raises.

    Not `az rest`, because two of these responses defeat it: the policy endpoint
    answers XML rather than JSON ("Not a json response"), and printing it then
    dies on the Windows console codepage. That produced an empty policy string,
    which the caller read as "the policy has no attribution headers" — the
    checker reporting its OWN failure as a fact about the environment, on a
    gateway that was attributing calls correctly the whole time.

    Hence the error is returned alongside the payload instead of being folded
    into a falsy value: a check that cannot read something must say so, not
    convict.
    """
    global _ARM_TOKEN
    try:
        if _ARM_TOKEN is None:
            from azure.identity import DefaultAzureCredential  # noqa: PLC0415
            _ARM_TOKEN = DefaultAzureCredential().get_token(
                "https://management.azure.com/.default").token
        r = httpx.get(
            f"https://management.azure.com{path}",
            params={"api-version": "2022-08-01", **params},
            headers={"Authorization": f"Bearer {_ARM_TOKEN}",
                     "Accept": "application/json"},
            timeout=90,
        )
    except Exception as exc:  # noqa: BLE001 — every failure must be reportable
        return None, f"{type(exc).__name__}: {exc}"
    if r.status_code == 404:
        return None, "404"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:120]}"
    try:
        return r.json(), ""
    except ValueError:
        return None, "响应不是 JSON"


def discover(rg: str) -> Env:
    """Find the environment's resources by TYPE, not by name.

    Names carry a random suffix (tokenfoundry-apim-f95e578716ce3), and older
    environments used different prefixes, so matching on names would make this
    work on the environment it was written against and no other.
    """
    env = Env(rg=rg)
    acct = az("account", "show") or {}
    env.subscription = acct.get("id", "")

    for res in az("resource", "list", "-g", rg) or []:
        kind, name = res.get("type", ""), res.get("name", "")
        if kind == "Microsoft.ApiManagement/service":
            env.apim = name
        elif kind == "Microsoft.ContainerRegistry/registries":
            env.acr = name
        elif kind == "Microsoft.DocumentDB/databaseAccounts":
            env.cosmos = name
        elif kind == "Microsoft.EventHub/namespaces":
            env.eventhub_ns = name
        elif kind == "Microsoft.App/containerApps":
            env.aca = name

    if env.aca:
        app = az("containerapp", "show", "-g", rg, "-n", env.aca) or {}
        props = app.get("properties", {})
        env.aca_fqdn = props.get("configuration", {}).get("ingress", {}).get("fqdn", "")
        containers = props.get("template", {}).get("containers", [])
        if containers:
            env.app_env = {
                e["name"]: e.get("value", "") for e in (containers[0].get("env") or [])
                if "value" in e            # secret refs carry no inline value
            }
        env.capture_sa = env.app_env.get("TF_USAGE_CAPTURE_STORAGE_ACCOUNT", "")
        if containers:
            env.app_image_tag = containers[0].get("image", "").split(":")[-1]

    # Hub resource groups are SEPARATE from the environment's own RG (one per
    # account, created by the GitHub Action), so they need a subscription-wide
    # listing rather than a lookup inside `rg`.
    env.hub_rgs = [
        g["name"] for g in (az("group", "list") or [])
        if g.get("name", "").startswith("tokenfoundry-hub-")
    ]
    return env


# --------------------------------------------------------------------------- #
# Layer 1-2 — Azure resources and the role assignments code actually uses      #
# --------------------------------------------------------------------------- #
def check_resources(env: Env) -> list[Finding]:
    out: list[Finding] = []
    for label, value, why in (
        ("APIM", env.apim, "no gateway"),
        ("ACR", env.acr, "no image registry"),
        ("Container App (control plane)", env.aca, "no control plane"),
        ("Cosmos", env.cosmos, "no usage store — the billing page has no source"),
        ("Event Hub namespace", env.eventhub_ns,
         "hubs have nowhere to send usage; billing data never leaves the hub"),
    ):
        out.append(Finding(
            "1 资源", label, OK if value else BAD, value or f"缺失 — {why}",
            "" if value else "terraform apply（这套环境的 terraform 版本太老或没跑过）",
        ))

    if env.eventhub_ns:
        # Capture is what turns a stream into blobs the importer can read. An
        # Event Hub without it means events arrive and expire unread.
        hubs = az("eventhubs", "eventhub", "list", "-g", env.rg,
                  "--namespace-name", env.eventhub_ns) or []
        cap = next((h for h in hubs if (h.get("captureDescription") or {}).get("enabled")), None)
        out.append(Finding(
            "1 资源", "Event Hub Capture", OK if cap else BAD,
            f"已启用（{cap['name']}）" if cap else "未启用 — 事件流不会落成 blob，导入器无从读取",
            "" if cap else "terraform apply",
        ))
    return out


def check_roles(env: Env) -> list[Finding]:
    """The grants that application code depends on — not the ones the platform uses.

    These two are easy to confuse and the confusion is expensive. The app carries
    TWO identities: a user-assigned one the Container Apps PLATFORM authenticates
    with to pull the image, and the SYSTEM one that DefaultAzureCredential
    resolves to in code. On 2026-08-26 the registry grant existed on the first and
    not the second, so a newly shipped image-resolution feature failed every call
    and fell back — looking deployed while doing nothing.
    """
    out: list[Finding] = []
    if not env.aca:
        return out
    app = az("containerapp", "show", "-g", env.rg, "-n", env.aca) or {}
    mi = (app.get("identity") or {}).get("principalId", "")
    if not mi:
        return [Finding("2 角色", "系统标识", BAD, "控制平面没有系统分配标识",
                        "terraform apply")]

    def has_role(scope: str, wanted: str) -> bool:
        rows = az("role", "assignment", "list", "--assignee", mi, "--scope", scope) or []
        return any(r.get("roleDefinitionName") == wanted for r in rows)

    if env.acr:
        acr_id = (az("acr", "show", "-g", env.rg, "-n", env.acr) or {}).get("id", "")
        ok = bool(acr_id) and has_role(acr_id, "AcrPull")
        out.append(Finding(
            "2 角色", "系统标识 → ACR AcrPull", OK if ok else BAD,
            "已授予" if ok else
            "缺失 — 新建 hub 时解析最新镜像会失败并静默回退到 TF_HUB_IMAGE_TAG",
            "" if ok else "terraform apply（modules/containerapps 的 app_acr_pull）",
        ))

    if env.cosmos:
        # Cosmos data-plane RBAC is a separate system from ARM roles: a
        # subscription Owner still reads nothing without one of these.
        rows = az("cosmosdb", "sql", "role", "assignment", "list",
                  "-g", env.rg, "-a", env.cosmos) or []
        ok = any(r.get("principalId") == mi for r in rows)
        out.append(Finding(
            "2 角色", "系统标识 → Cosmos 数据平面", OK if ok else BAD,
            "已授予" if ok else "缺失 — 控制平面读写不了用量文档（控制平面角色不顶用）",
            "" if ok else "terraform apply",
        ))
    return out


# --------------------------------------------------------------------------- #
# Layer 3 — control-plane env                                                  #
# --------------------------------------------------------------------------- #
def check_app_env(env: Env) -> list[Finding]:
    out = []
    for name, breaks in REQUIRED_APP_ENV.items():
        val = env.app_env.get(name, "")
        out.append(Finding(
            "3 控制平面env", name, OK if val else BAD,
            (val[:60] if val else f"未设置 — {breaks}"),
            "" if val else "terraform apply（这些由 terraform 注入）",
        ))
    return out


# --------------------------------------------------------------------------- #
# Layer 4 — repo variables                                                     #
# --------------------------------------------------------------------------- #
def check_repo_vars(env: Env, owner: str, repo: str, pat: str,
                    newest_tag: str | None) -> list[Finding]:
    """What a NEW hub will be built from.

    Only ever written when an operator presses "push deploy configuration", so
    this is the layer that goes stale without any signal at all.
    """
    if not pat:
        return [Finding("4 仓库变量", "读取", INFO,
                        "跳过 — 未提供 GitHub PAT（需要能读 Actions variables 的令牌）",
                        "设置 GITHUB_BOOTSTRAP_PAT 后重跑")]
    try:
        with httpx.Client(timeout=60) as c:
            r = c.get(
                f"https://api.github.com/repos/{owner}/{repo}/actions/variables",
                params={"per_page": 100},
                headers={"Authorization": f"Bearer {pat}",
                         "Accept": "application/vnd.github+json"},
            )
        if r.status_code != 200:
            return [Finding("4 仓库变量", "读取", WARN,
                            f"GitHub 返回 {r.status_code}：{r.text[:120]}",
                            "换一个有 administration:read 权限的 PAT")]
        have = {v["name"]: v.get("value", "") for v in r.json().get("variables", [])}
    except httpx.HTTPError as exc:
        return [Finding("4 仓库变量", "读取", WARN, f"读取失败：{exc}", "")]

    out = []
    for name in MUST_NOT_BE_EMPTY:
        val = have.get(name, "")
        out.append(Finding(
            "4 仓库变量", name, OK if val else BAD,
            val[:70] if val else
            "空 — hub 会以「不上报」的方式正常运行，任何地方都不会报错",
            "" if val else "门户「推送 GitHub 部署配置」（先确认控制平面 env 非空）",
        ))

    # Staleness is separate from emptiness: a valid-looking tag that is simply old
    # deploys, pulls, and runs. Only the registry can tell them apart.
    ref = have.get("HUB_IMAGE_REF", "")
    if ref and newest_tag:
        cur = ref.split(":")[-1]
        fresh = cur == newest_tag
        out.append(Finding(
            "4 仓库变量", "HUB_IMAGE_REF 是否最新", OK if fresh else WARN,
            f"{cur} vs ACR 最新 {newest_tag}" +
            ("" if fresh else " — 新建账号的 hub 会起在旧镜像上"),
            "" if fresh else "门户「推送 GitHub 部署配置」，或加一个账号时自动刷新",
        ))
    return out


# --------------------------------------------------------------------------- #
# Layer 5 — APIM runtime config (the layer terraform and images cannot reach)   #
# --------------------------------------------------------------------------- #
def check_apim(env: Env) -> list[Finding]:
    """Operations, policy and per-API diagnostic — all written only at
    account-add / resync time.

    The expected operation list comes from PROVIDER_APIS, so adding an operation
    to the provisioner automatically makes this check demand it. That is the
    whole point: `count_tokens` was added on 2026-08-25 and no existing
    environment has it until a resync runs.
    """
    out: list[Finding] = []
    if not env.apim:
        return out
    base = (f"/subscriptions/{env.subscription}/resourceGroups/{env.rg}"
            f"/providers/Microsoft.ApiManagement/service/{env.apim}")

    for cfg in PROVIDER_APIS.values():
        api_id = cfg["api_id"]
        api, err = arm_get(f"{base}/apis/{api_id}")
        if api is None:
            # 404 is not necessarily wrong: an API only exists once a hub has
            # served that provider's models. Anything else is US failing to look,
            # which must not masquerade as a verdict on the environment.
            out.append(Finding(
                "5 APIM", api_id, INFO if err == "404" else WARN,
                "不存在（该 provider 尚未有模型注册过）" if err == "404"
                else f"读取失败：{err}", ""))
            continue

        ops, err = arm_get(f"{base}/apis/{api_id}/operations")
        if ops is None:
            out.append(Finding("5 APIM", f"{api_id} 操作", WARN, f"读取失败：{err}", ""))
        else:
            have = {o["name"] for o in ops.get("value", [])}
            want = {op_id for op_id, _ in cfg["ops"]}
            missing = sorted(want - have)
            out.append(Finding(
                "5 APIM", f"{api_id} 操作", OK if not missing else BAD,
                f"{len(have & want)}/{len(want)}" +
                (f" — 缺 {', '.join(missing)}（客户端会收到 404 并静默降级）"
                 if missing else ""),
                "" if not missing else "门户「重新同步模型」",
            ))

        pol, err = arm_get(f"{base}/apis/{api_id}/policies/policy", format="xml")
        if pol is None:
            out.append(Finding("5 APIM", f"{api_id} 策略", WARN, f"读取失败：{err}", ""))
        else:
            xml = ((pol.get("properties") or {}).get("value") or "")
            # x-tf-subscription is what attributes a call to a paying tenant.
            # Without it the hub cannot tell who to bill.
            stamped = "x-tf-subscription" in xml and "x-tf-api" in xml
            out.append(Finding(
                "5 APIM", f"{api_id} 策略", OK if stamped else BAD,
                f"含 x-tf-* 归属头（{len(xml)} 字节）" if stamped else
                "缺 x-tf-* 归属头 — 调用无法归属到租户（策略是旧版本）",
                "" if stamped else "门户「重新同步模型」",
            ))

        diag, err = arm_get(f"{base}/apis/{api_id}/diagnostics/applicationinsights")
        if diag is None and err != "404":
            out.append(Finding("5 APIM", f"{api_id} 诊断", WARN, f"读取失败：{err}", ""))
            continue
        props = (diag or {}).get("properties") or {}
        # An API-level diagnostic OVERRIDES the service-level one and an omitted
        # field does NOT inherit — it takes APIM's own default. So a missing
        # `metrics` silently switches customMetrics off for this API alone.
        metrics_on = props.get("metrics") is True
        out.append(Finding(
            "5 APIM", f"{api_id} 诊断", OK if metrics_on else BAD,
            (f"metrics={props.get('metrics')} "
             f"sampling={(props.get('sampling') or {}).get('percentage')} "
             f"alwaysLog={props.get('alwaysLog')}") if diag else
            "无 API 级诊断 — 该 API 的 customMetrics 会静默为空",
            "" if metrics_on else "门户「重新同步模型」",
        ))
    return out


# --------------------------------------------------------------------------- #
# Layer 6 — the hub fleet                                                      #
# --------------------------------------------------------------------------- #
def check_hubs(env: Env, newest_tag: str | None) -> list[Finding]:
    out: list[Finding] = []
    if not env.hub_rgs:
        return [Finding("6 hub", "舰队", INFO, "没有已部署的 hub", "")]
    for rg in sorted(env.hub_rgs):
        apps = az("containerapp", "list", "-g", rg) or []
        if not apps:
            out.append(Finding("6 hub", rg, WARN, "资源组存在但没有 Container App（可能是残留）",
                               "确认是否为销毁失败留下的孤儿"))
            continue
        app = apps[0]
        name = app["name"]
        img = app["properties"]["template"]["containers"][0]["image"]
        tag = img.split(":")[-1]
        fresh = (tag == newest_tag) if newest_tag else None
        out.append(Finding(
            "6 hub", f"{name} 镜像", OK if fresh else (WARN if newest_tag else INFO),
            tag + ("" if fresh else f" — 落后 ACR 最新 {newest_tag}"),
            "" if fresh else "重新部署该账号的 hub（见脚本结尾说明）",
        ))

        fqdn = app["properties"]["configuration"]["ingress"]["fqdn"]
        try:
            with httpx.Client(timeout=45) as c:
                st = c.get(f"https://{fqdn}/api/status").json()
        except (httpx.HTTPError, ValueError) as exc:
            out.append(Finding("6 hub", f"{name} 状态", BAD, f"探测失败：{exc}", ""))
            continue
        ev = st.get("usage_events") or {}
        state = ev.get("state")
        # "disabled" is the single clearest signal that a hub was deployed
        # without Event Hub coordinates: it reports nothing, counts nothing, and
        # logs nothing — by design, which is why it is invisible from anywhere
        # else. This one line is the fastest answer to "why is billing empty".
        out.append(Finding(
            "6 hub", f"{name} 上报", OK if state == "ok" else BAD,
            f"state={state} lost={ev.get('lost')} dropped={ev.get('dropped')}" +
            ("" if state != "disabled" else
             " — 未配置 Event Hub，这个 hub 从不上报任何用量"),
            "" if state != "disabled" else
            "先补齐 HUB_EVENTHUB_*（第 4 层），再重新部署该账号的 hub",
        ))
        out.append(Finding(
            "6 hub", f"{name} Copilot 登录", OK if st.get("logged_in") else BAD,
            "已登录" if st.get("logged_in") else "未登录 — 该 hub 无法服务请求",
            "" if st.get("logged_in") else "门户对该账号「重新登录」",
        ))
    return out


# --------------------------------------------------------------------------- #
# terraform — planned, translated, never applied                               #
# --------------------------------------------------------------------------- #
def check_terraform(run: bool, env: Env) -> list[Finding]:
    """Plan (never apply) and refuse to bless anything that destroys state.

    Three things stop the run cold, because each destroys something this script
    cannot put back:

      * any destroy or replace;
      * a change to the db_url / jwt / admin_pwd Key Vault secrets — their values
        come from tfvars variables that have NO defaults, and *.tfvars is
        gitignored, so upgrading an environment whose original tfvars was lost
        rewrites tf-jwt-secret (invalidating every live session) and
        tf-database-url (desynchronising it from the actual server password);
      * removal of the Container Apps workload profile — a known drift: the
        environment declares none while Azure attached `Consumption`, and the
        control plane is running on it.

    Opt-in, so the rest of the check needs neither terraform nor a tfvars file.
    """
    if not run:
        return [Finding(
            "TF", "terraform plan", INFO,
            "未运行（加 --terraform-plan 开启）。全量 apply 前务必先 plan",
            "",
        )]

    tf_dir = _REPO_ROOT / "terraform"
    tf = shutil.which("terraform")
    if not tf:
        return [Finding("TF", "terraform plan", WARN, "找不到 terraform 命令", "")]

    with tempfile.TemporaryDirectory() as tmp:
        planfile = str(Path(tmp) / "tfplan")
        # Both tags are REQUIRED variables with no defaults, and both must be the
        # values currently running. Passing anything else makes the plan propose
        # rolling the images as a side effect of whatever you were actually
        # checking — noise that hides the destroy/replace lines that matter.
        # We already discovered them, so there is no reason to make a human type
        # them (and get them wrong).
        p = subprocess.run(                                    # noqa: S603
            [tf, "plan", "-lock=false", "-input=false", f"-out={planfile}",
             "-var", f"image_tag={env.app_image_tag}",
             "-var", f"hub_image_tag={env.app_env.get('TF_HUB_IMAGE_TAG', '')}"],
            cwd=tf_dir, capture_output=True, text=True, timeout=1800, check=False,
        )
        if p.returncode != 0:
            # Missing required variables lands here. That is worth saying plainly:
            # it is exactly the situation where somebody would be tempted to type
            # fresh values and silently rewrite the three secrets.
            msg = _ANSI.sub("", (p.stderr or p.stdout)).strip()
            return [Finding(
                "TF", "terraform plan", WARN,
                f"plan 失败：{msg[-300:]}",
                "若因缺少变量而失败，请用这套环境**原始的** tfvars —— "
                "临时填新值会改写 jwt/db_url/admin_pwd 三个 KV secret",
            )]
        shown = subprocess.run(                                # noqa: S603
            [tf, "show", "-json", planfile],
            cwd=tf_dir, capture_output=True, text=True, timeout=600, check=False,
        )
    try:
        changes = json.loads(shown.stdout).get("resource_changes", [])
    except (ValueError, AttributeError):
        return [Finding("TF", "terraform plan", WARN, "无法解析 plan 输出", "")]

    out: list[Finding] = []
    destructive = [c for c in changes
                   if "delete" in (c.get("change") or {}).get("actions", [])]
    out.append(Finding(
        "TF", "destroy/replace", OK if not destructive else BAD,
        "无" if not destructive else
        "；".join(f"{c['address']}({'/'.join(c['change']['actions'])})"
                  for c in destructive[:6]),
        "" if not destructive else "**不要 apply**。先逐条确认这些资源为何会被删/重建",
    ))

    secrets_touched = [
        c["address"] for c in changes
        if c.get("type") == "azurerm_key_vault_secret"
        and (c.get("change") or {}).get("actions", []) != ["no-op"]
    ]
    out.append(Finding(
        "TF", "KV secret 变更", OK if not secrets_touched else BAD,
        "无" if not secrets_touched else "、".join(secrets_touched),
        "" if not secrets_touched else
        "**不要 apply**。改写 tf-jwt-secret 会让所有已登录会话失效，"
        "改写 tf-database-url 会与实际库密码脱节",
    ))

    changed = [c for c in changes
               if (c.get("change") or {}).get("actions", []) not in ([], ["no-op"])]

    # The workload-profile drift needs its own check, because it does NOT show up
    # as a destroy or a replace: Azure attached a `Consumption` profile that the
    # config never declared, so the plan proposes an in-place UPDATE that removes
    # it — from under the control plane, which is running on it. A generic
    # destroy/replace guard reports this environment as safe to apply.
    profiles_lost: list[str] = []
    for c in changes:
        if c.get("type") != "azurerm_container_app_environment":
            continue
        ch = c.get("change") or {}
        before = {p.get("name") for p in ((ch.get("before") or {}).get("workload_profile") or [])}
        after = {p.get("name") for p in ((ch.get("after") or {}).get("workload_profile") or [])}
        profiles_lost += sorted(before - after)
    out.append(Finding(
        "TF", "workload profile", OK if not profiles_lost else BAD,
        "无移除" if not profiles_lost else
        f"会移除 {', '.join(profiles_lost)} — 控制平面正跑在上面",
        "" if not profiles_lost else
        "**不要 apply**。要么用 -target 只应用你真正要改的资源，"
        "要么先在 modules/containerapps 里显式声明该 profile 让配置与实际一致",
    ))

    out.append(Finding(
        "TF", "总变更数", OK if not changed else INFO,
        f"{len(changed)} 项" + ("" if not changed else
                                f"：{', '.join(c['address'] for c in changed[:6])}"),
        "",
    ))
    return out


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #
def report(findings: list[Finding]) -> int:
    layer = None
    for f in findings:
        if f.layer != layer:
            layer, _ = f.layer, print(f"\n--- {f.layer} ---")
        print(f"[{_MARK[f.state]}] {f.name:<34} {f.detail}")
        if f.remedy and f.state in (BAD, WARN):
            print(f"{'':>9}└─ 怎么修: {f.remedy}")

    bad = sum(1 for f in findings if f.state == BAD)
    warn = sum(1 for f in findings if f.state == WARN)
    print(f"\n{'=' * 70}\n共 {len(findings)} 项：{bad} 项失败，{warn} 项告警\n")
    if bad or warn:
        print("注意：第 4/5/6 层 terraform 和镜像更新都碰不到——只有门户的两个按钮"
              "（推送部署配置 / 重新同步模型）和 deploy-hub workflow 会写它们。")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# --fix                                                                        #
# --------------------------------------------------------------------------- #
def login(cp_url: str, user: str, pwd: str) -> str:
    r = httpx.post(f"{cp_url}/api/login", json={"username": user, "password": pwd},
                   timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"控制平面登录失败 ({r.status_code}): {r.text[:200]}")
    return str(r.json()["access_token"])


def do_fix(env: Env, token: str) -> None:
    """The four data-preserving repairs. Deliberately narrow — see the docstring.

    Not here on purpose: terraform apply, hub redeploy, and anything that writes
    the deployment SP or Key Vault.
    """
    cp = f"https://{env.aca_fqdn}"
    hdr = {"Authorization": f"Bearer {token}"}

    print("\n>>> 重新发布仓库变量（HUB_IMAGE_REF / HUB_EVENTHUB_* …）")
    r = httpx.post(f"{cp}/api/deploy-config/push-sp", headers=hdr, timeout=180)
    print("    " + ("成功" if r.status_code < 300 else f"失败 {r.status_code}: {r.text[:200]}"))

    print("\n>>> 重新同步模型目录（这一步才会写 APIM 操作/策略/诊断）")
    accounts = httpx.get(f"{cp}/api/github-accounts", headers=hdr, timeout=60).json()
    ready = [a for a in accounts if a.get("status") == "READY"]
    if not ready:
        print("    跳过 — 没有 READY 状态的账号")
        return
    # `resync-catalog` prunes PLATFORM routes whose model is gone from the hub
    # catalog. That is a DB write, and the only one this script performs, so it
    # is announced rather than done quietly. TENANT/BYO routes are never touched.
    print(f"    将对账号 {ready[0]['id']} 执行同步；它会删除 hub 目录里已不存在的"
          " PLATFORM 模型路由（BYO 路由不受影响）")
    if input("    继续？[y/N] ").strip().lower() != "y":
        print("    已跳过")
        return
    r = httpx.post(f"{cp}/api/github-accounts/{ready[0]['id']}/resync-catalog",
                   headers=hdr, timeout=600)
    print("    " + (f"成功：{r.json()}" if r.status_code < 300
                    else f"失败 {r.status_code}: {r.text[:200]}"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Token Foundry 环境体检")
    ap.add_argument("-g", "--resource-group", required=True)
    ap.add_argument("--fix", action="store_true",
                    help="重新发布仓库变量 + 重新同步模型目录（不跑 terraform、不重部署 hub）")
    ap.add_argument("--terraform-plan", action="store_true",
                    help="额外跑一次 terraform plan（只读）检查 destroy/replace 与 KV secret 变更")
    ap.add_argument("--owner", default="Nick287")
    ap.add_argument("--repo", default="TokenFoundry")
    ap.add_argument("--admin-user", default=os.environ.get("TF_ADMIN_USERNAME", ""))
    ap.add_argument("--admin-pass", default=os.environ.get("TF_ADMIN_PASSWORD", ""))
    args = ap.parse_args()

    print(f"体检环境: {args.resource_group}")
    env = discover(args.resource_group)
    if not env.aca:
        raise SystemExit(f"在 {args.resource_group} 里没找到控制平面 Container App")
    print(f"  APIM={env.apim}  ACR={env.acr}  控制平面={env.aca}")
    print(f"  Cosmos={env.cosmos}  EventHub={env.eventhub_ns}  hub 数={len(env.hub_rgs)}")

    newest = None
    if env.acr:
        newest = newest_tag(env.acr, "gitmodel")

    pat = os.environ.get("GITHUB_BOOTSTRAP_PAT", "")
    findings = [
        *check_resources(env),
        *check_roles(env),
        *check_app_env(env),
        *check_repo_vars(env, args.owner, args.repo, pat, newest),
        *check_apim(env),
        *check_hubs(env, newest),
        *check_terraform(args.terraform_plan, env),
    ]
    code = report(findings)

    if args.fix:
        if not (args.admin_user and args.admin_pass):
            raise SystemExit("--fix 需要控制平面管理员凭据"
                             "（--admin-user/--admin-pass 或 TF_ADMIN_USERNAME/PASSWORD）")
        do_fix(env, login(f"https://{env.aca_fqdn}", args.admin_user, args.admin_pass))
        print("\n重新跑一次体检确认结果。镜像重建请用 ./scripts/deploy.sh -g <rg>。")
    sys.exit(code)


if __name__ == "__main__":
    main()
