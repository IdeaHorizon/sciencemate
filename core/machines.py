"""组织登记的机器 —— 入口 + 组织服务器探到的现状。组织服务器写，agent 读。

`RFC_ORGANISATION_PAGE_20260923` §4.4（D 批）。

## 和 grants.yaml 的分工

    machines.yaml   有什么机器：名字、类型、ssh 入口、探到的现状（卡 / 分区）、探于何时
    grants.yaml     谁能用什么：按人（或 default = 所有人）× 机器 × 卡位 / 分区

授权引用机器 id（`machine: m_…`），入口只在这里写一次 —— 机器换了地址改一处。

## 为什么现状写进了文件

`core/capabilities` 开头那条规矩是「探得到的事实永远不写进文件」—— 前提是**消费方自己
探得到**。组织的机器要用组织的钥匙才进得去，而那把钥匙只在组织服务器的后端手里（不在
agent 读得到的 org 层：一个项目的 agent 若拿得到它，授权就只是建议，谁都能 ssh 上去）。
worker 探不了，所以探针挪到后端，结果连同**探测时刻**写在这里；注入时照实说「探于 X」。
腐坏由时间戳暴露，不由装作是现场探的来掩盖。

格式的规矩只有这个模块知道；App Server 经桥调 `write_machines`，不自己拼 YAML。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: 机器的类型 —— 由组织服务器探出来，不由人填（人会填错，探针不会）。
#: `gpu_node` / `cpu_node` 是一台直接用的机器；三种调度器是登录节点。
MACHINE_KINDS = ("gpu_node", "cpu_node", "slurm_cluster", "pbs_cluster", "kubernetes")

ONLINE = "online"
UNREACHABLE = "unreachable"

_AN_ID = re.compile(r"m_[a-z0-9]{6,32}")


def machines_path() -> Path:
    from core.paths import org_root

    return org_root() / "machines.yaml"


def read_machines() -> tuple[list[dict], str | None]:
    """(登记的机器, 解析错误或 None)。没有文件 = 没有机器。"""
    p = machines_path()
    if not p.exists():
        return [], None
    try:
        import yaml

        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 —— 解析失败的方式很多，对调用方是同一件事
        return [], f"machines.yaml 解析失败（{e.__class__.__name__}: {e}）"
    machines = raw.get("machines") if isinstance(raw, dict) else None
    if not isinstance(machines, list):
        return [], "machines.yaml 顶层必须是 {machines: [...]}"
    return [m for m in machines if isinstance(m, dict) and m.get("id")], None


def find(machine_id: str, machines: list[dict] | None = None) -> dict | None:
    if machines is None:
        machines, _ = read_machines()
    return next((m for m in machines if m.get("id") == machine_id), None)


def write_machines(machines: list[dict]) -> list[dict]:
    """整份写回（先写临时文件再换名 —— 注入随时可能在读它）。"""
    if not isinstance(machines, list):
        raise ValueError("machines 必须是一个列表")
    seen: set[str] = set()
    for m in machines:
        if not isinstance(m, dict):
            raise ValueError("每台机器是一个映射")
        mid = str(m.get("id") or "")
        if not _AN_ID.fullmatch(mid):
            raise ValueError(f"机器 id {mid!r} 不合格式（m_ 加小写字母数字）")
        if mid in seen:
            raise ValueError(f"机器 id {mid} 出现了两次")
        seen.add(mid)
        if str(m.get("kind") or "") not in MACHINE_KINDS:
            raise ValueError(f"{mid} 的类型 {m.get('kind')!r} 不在 {MACHINE_KINDS} 里")
        if not str(m.get("host") or "").strip() or not str(m.get("name") or "").strip():
            raise ValueError(f"{mid} 缺名字或地址")
        if any(k in m for k in ("password", "private_key", "key")):
            raise ValueError(f"{mid} 带着密码或私钥 —— 这份文件 agent 读得到，它们不许进来")

    import yaml

    target = machines_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".yaml.writing")
    scratch.write_text(yaml.safe_dump({"machines": machines}, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
    scratch.replace(target)
    from core import capabilities

    capabilities.forget_the_last_look()
    return machines


def entry(machine: dict) -> str:
    """ssh 入口，给人看的那种写法：user@host[:port]。"""
    host = str(machine.get("host") or "")
    user = str(machine.get("username") or "")
    port = int(machine.get("port") or 22)
    return f"{user + '@' if user else ''}{host}{'' if port == 22 else f':{port}'}"


def describe(machine: dict) -> str:
    """一行现状：几张什么卡、多少在用；或者分区、节点、状态。说不出就空。"""
    facts: dict[str, Any] = machine.get("facts") or {}
    parts: list[str] = []
    gpus = facts.get("gpus") or []
    if gpus:
        names: dict[str, int] = {}
        for g in gpus:
            label = str(g.get("name") or "GPU")
            if g.get("memory_total"):
                label += f" {g['memory_total']}"
            names[label] = names.get(label, 0) + 1
        kinds = "，".join(f"{n}×{label}" for label, n in names.items())
        busy = sum(1 for g in gpus if str(g.get("utilization") or "0").rstrip(" %") not in ("", "0"))
        parts.append(f"{len(gpus)} 张卡（{kinds}）" + (f"，{busy} 张在忙" if busy else ""))
    partitions = facts.get("partitions") or []
    if partitions:
        parts.append("分区 " + "；".join(
            f"{p.get('name')}（{p.get('availability', '?')}，{p.get('nodes', '?')} 节点，{p.get('state', '?')}）"
            for p in partitions[:6]))
    if facts.get("cpus"):
        parts.append(f"{facts['cpus']} 核" + (f"，{facts['memory_gb']} GB 内存" if facts.get("memory_gb") else ""))
    if facts.get("queues"):
        parts.append("队列 " + "、".join(str(q) for q in facts["queues"][:6]))
    if facts.get("k8s_nodes"):
        parts.append(f"{facts['k8s_nodes']} 个 Kubernetes 节点")
    if facts.get("os"):
        parts.append(str(facts["os"]))
    return "；".join(parts)
