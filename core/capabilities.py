"""算力/硬件资源登记 —— 文件只放钥匙，现状全靠探。

## 分工（设计讨论定稿 2026-07-30）

探得到的事实**永远不写进文件**：写进去就会腐坏（GPU 被占了、换卡了，文件还是
旧的），而且和探针结果构成两个事实来源。文件（grants.yaml）只放探不出来的东西：

  - 入口：HPC 集群的 endpoint / 账号 / partition —— 不知道门在哪就没法去探
  - 政策：这个用户/课题组允许用哪几张卡、配额多少 —— 这是决策不是事实
  - 多用户：按 user id 分节，`default` 节对所有人生效

现状由框架在消费时**顺着钥匙去探**：每种资源就是一条 bash 命令 + 解析
（local_gpu → nvidia-smi；slurm → sinfo；任意 grant 可自带 probe_cmd）。
新资源类型 = 加一条 grant + 一条探针命令，不改框架代码。

## 为什么探针由框架跑而不是 agent 跑

freeze 门禁要有东西可校验。agent 自己 bash 探出来的清单门禁看不见，承诺出界
就拦不住。所以命令还是那几条 bash，只是框架跑一遍、结果同时喂给两处：

    grants.yaml + 探针 → snapshot() ──→ system prompt 注入（agent 读到）
                                    └─→ prereg freeze 门禁（承诺出界机械拒）

**同一份观测，两处消费** —— 别让注入和门禁各自推导。

## 失败方向（机制接缝五问过一遍）

  - grants.yaml 解析失败 → **吵**：注入段里显式一行 ⚠️，门禁按无 grants 处理
    （保守但可见；静默返空 = 用户授了权而平台装聋）
  - 探针失败/超时 → 标 "已授权、本次探测失败" 照样注入 —— 门禁管的是
    "有没有钥匙"，不是"房间现在空不空"；现状好坏让预注册自己权衡
  - 无 grants 文件 → 一切行为与本模块存在之前完全一致（爆炸半径 = 0）
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 12
_CACHE_TTL_S = 60.0        # 一个 run 起始附近的多次消费共享一次探测

# 开源权重模型族：算力登记允许本地部署时，点名它们不再是"承诺不存在的资源"。
# API-only 模型（gpt/claude/gemini…）不在此列 —— 有 GPU 也变不出闭源权重。
OPEN_WEIGHT_FAMILIES = ("qwen", "llama", "mistral", "mixtral")

# 允许本地部署/训练的 grant kind（登记的机器：一台卡机或一个调度器，都算）
_DEPLOY_KINDS = ("local_gpu", "slurm_cluster", "gpu_node", "pbs_cluster", "kubernetes")


@dataclass(frozen=True)
class Capability:
    """一条授权 + 它此刻的探测结果。"""

    kind: str
    grant: dict = field(default_factory=dict)
    status: str = "declared"      # verified / probe_failed / declared
    detail: str = ""              # 探到的现状，或失败原因

    @property
    def allows_local_deployment(self) -> bool:
        return self.kind in _DEPLOY_KINDS


# ── grants 文件 ──────────────────────────────────────────────────────────────

def grants_path() -> Path:
    override = os.getenv("HARNESS_GRANTS_FILE")
    if override:
        return Path(override)
    from core.paths import org_root
    return org_root() / "grants.yaml"


def _load_grants() -> tuple[list[dict], str | None]:
    """(生效的 grant 列表, 解析错误文案或 None)。

    user 节 + default 节合并；文件不存在 → 空列表（行为与无此模块一致）。
    解析失败不吞：返回错误文案，由注入段展示。
    """
    p = grants_path()
    if not p.exists():
        return [], None
    try:
        import yaml
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return [], f"grants.yaml 解析失败（{e.__class__.__name__}: {e}）—— 本次按无授权处理"
    if not isinstance(raw, dict):
        return [], "grants.yaml 顶层必须是 {user_id: [grants]} 映射 —— 本次按无授权处理"
    from core.identity import current_user_id
    uid = current_user_id()
    out: list[dict] = []
    for key in ("default", uid):
        entries = raw.get(key)
        if isinstance(entries, list):
            out.extend(e for e in entries if isinstance(e, dict) and e.get("kind"))
    return out, None


def read_grants_file() -> tuple[dict, str | None]:
    """原样读出整份 grants（**不合并 default、不按用户筛**）。

    `_load_grants()` 答的是"我现在有哪些钥匙"，这个答的是"这份文件里写着什么" ——
    两个问题。管理界面要的是后者：它得看见每个人各自那一节，才谈得上改。
    """
    p = grants_path()
    if not p.exists():
        return {}, None
    try:
        import yaml
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 —— 解析失败的方式很多，对调用方是同一件事
        return {}, f"grants.yaml 解析失败（{e.__class__.__name__}: {e}）"
    if not isinstance(raw, dict):
        return {}, "grants.yaml 顶层必须是 {user_id: [grants]} 映射"
    return raw, None


def write_grants_file(mapping: dict) -> None:
    """把整份 grants 写回去。

    ## 为什么写在这里

    在此之前 `grants.yaml` **全仓没有任何写入方** —— 读它的有两处（agent 的提示
    注入、预注册冻结门禁），写它的只有"人手编辑那个文件"。于是"算力管理"在产品
    里不存在：界面上能看到探针结果，却没有一处能授权一台机器。

    写的那一半必须和读的那一半住在一起：格式的规则（顶层是 {user_id: [grants]}、
    每条 grant 至少要有 kind）只有这个模块知道。让 App Server 自己拼 YAML，就是
    第二份会各自演化的格式知识。

    先写临时文件再 rename：探针和注入随时可能在读它，半份 YAML 会被读成"解析
    失败 → 本次按无授权处理"，也就是**所有人的算力突然全没了**。
    """
    if not isinstance(mapping, dict):
        raise ValueError("grants 顶层必须是 {user_id: [grants]} 映射")
    for who, entries in mapping.items():
        if not isinstance(entries, list):
            raise ValueError(f"{who} 那一节必须是一个列表")
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("kind", "")).strip():
                raise ValueError(f"{who} 底下有一条没写 kind —— 那条钥匙开不了任何门")

    import yaml

    target = grants_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".yaml.writing")
    scratch.write_text(
        yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False), encoding="utf-8")
    scratch.replace(target)
    # 缓存里那份是旧的：改完授权，下一次注入/门禁就该看到新的。
    global _cache
    _cache = None


# ── 探针：每种资源一条命令；grant 自带 probe_cmd 可覆盖 ─────────────────────

def _run(cmd: list[str] | str, *, shell: bool = False) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                           timeout=_PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"探测超时（>{_PROBE_TIMEOUT_S}s）"
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or f"exit={r.returncode}").strip()[:300]
    return True, (r.stdout or "").strip()[:600]


def _probe_local_gpu(grant: dict) -> tuple[bool, str]:
    ok, out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                    "--format=csv,noheader"])
    if not ok:
        return False, out
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    allowed = str(grant.get("devices", "")).strip()
    note = f"（授权卡位：{allowed}）" if allowed else ""
    return True, f"{len(lines)} 张 GPU{note}：" + "; ".join(lines[:8])


def _probe_slurm(grant: dict) -> tuple[bool, str]:
    ep = str(grant.get("endpoint", "")).strip()
    if not ep:
        return False, "grant 缺 endpoint，无从探测"
    return _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 ep, "sinfo", "-h", "-o", "%P %a %D %T"])


_PROBES = {
    "local_gpu": _probe_local_gpu,
    "slurm_cluster": _probe_slurm,
}


def _from_the_register(grant: dict) -> Capability:
    """引用登记机器的授权：现状读登记表（组织服务器探的），不在这里 ssh。

    组织的钥匙只在组织服务器的后端手里（`core.machines` 的说明）；worker 进不去那台机器，
    也就不该装作探过。照实说「探于 X」。
    """
    from core import machines as registry

    kind = str(grant.get("kind") or "")
    machine = registry.find(str(grant["machine"]))
    if machine is None:
        return Capability(kind=kind, grant=grant, status="probe_failed",
                          detail="登记里没有这台机器了（可能已被管理员移除）")
    scope = "，".join(f"{label}：{grant[key]}" for key, label in
                     (("devices", "授权卡位"), ("partition", "授权分区")) if grant.get(key))
    facts = registry.describe(machine)
    # 登记表里是 UTC（组织服务器写的）；照写成 UTC —— 不带时区的一个钟点，agent 会当成本地时间。
    stamp = str(machine.get("probed_at") or "")
    when = stamp[:16].replace("T", " ") + (" UTC" if stamp.endswith(("+00:00", "Z")) else "")
    online = machine.get("status") == registry.ONLINE
    detail = "；".join(filter(None, [
        f"{machine.get('name')}（{registry.entry(machine)}）",
        facts,
        scope,
        (f"探于 {when}" if online else
         f"上次探不到（{machine.get('problem') or '连不上'}），探于 {when}"),
    ]))
    return Capability(kind=kind, grant=grant, status="verified" if online else "probe_failed",
                      detail=detail)


def _probe(grant: dict) -> Capability:
    kind = str(grant["kind"])
    if grant.get("machine"):
        return _from_the_register(grant)
    custom = str(grant.get("probe_cmd", "")).strip()
    if custom:
        ok, out = _run(custom, shell=True)
    elif kind in _PROBES:
        ok, out = _PROBES[kind](grant)
    else:
        return Capability(kind=kind, grant=grant, status="declared",
                          detail="（无探针，仅登记）")
    return Capability(kind=kind, grant=grant,
                      status="verified" if ok else "probe_failed", detail=out)


# ── snapshot：唯一对外出口，注入和门禁都吃它 ────────────────────────────────

_cache: tuple[float, list[Capability], str | None] | None = None


def forget_the_last_look() -> None:
    """登记表或授权改了：下一次注入 / 门禁就该看到新的。"""
    global _cache
    _cache = None


def snapshot(*, force: bool = False) -> tuple[list[Capability], str | None]:
    """(能力清单, grants 解析错误或 None)。带 TTL 缓存，探针不逐 turn 打。"""
    global _cache
    now = time.time()
    if not force and _cache is not None and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1], _cache[2]
    grants, err = _load_grants()
    caps = [_probe(g) for g in grants]
    _cache = (now, caps, err)
    return caps, err


def allows_local_deployment(caps: list[Capability] | None = None) -> bool:
    """有没有任何一把"能本地部署/训练模型"的钥匙。门禁按此放行开源权重模型。"""
    if caps is None:
        caps, _ = snapshot()
    return any(c.allows_local_deployment for c in caps)


def render_compute_section() -> list[str]:
    """注入 system prompt 的算力段落（空 grants → 空列表，注入侧零变化）。"""
    caps, err = snapshot()
    lines: list[str] = []
    if err:
        lines.append(f"⚠️ {err}")
    if not caps:
        return lines
    lines.append("算力/硬件授权（框架探的，不是声明值：本机的当场探；组织登记的机器由组织服务器探，"
                 "写着探于何时）：")
    mark = {"verified": "✅", "probe_failed": "⚠️", "declared": "▫️"}
    for c in caps:
        # 登记机器的授权：名字、入口、卡位都已在 detail 里说了，这里不再抄一遍 id。
        extra = {k: v for k, v in c.grant.items()
                 if k not in ("kind", "probe_cmd", "machine", "devices", "partition") and v}
        if not c.grant.get("machine"):
            extra.update({k: c.grant[k] for k in ("devices", "partition") if c.grant.get(k)})
        meta = "，".join(f"{k}={v}" for k, v in extra.items())
        status = {"verified": "", "declared": "",
                  "probe_failed": "【已授权，本次探测失败】"}[c.status]
        lines.append(f"- {mark[c.status]} {c.kind}"
                     + (f"（{meta}）" if meta else "") + f"{status}：{c.detail}")
    if allows_local_deployment(caps):
        lines.append(
            "已授权本地部署/训练：可下载、部署、微调开源权重模型"
            f"（{'/'.join(OPEN_WEIGHT_FAMILIES)} 等），或从头训练小模型；"
            "预注册须写明部署与测量方案。闭源 API 模型不因此可用。")
    return lines
