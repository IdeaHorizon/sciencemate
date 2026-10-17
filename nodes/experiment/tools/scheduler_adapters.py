"""调度器 adapter 契约与声明的能力（governing document §3 第 2 层、§11）。

一个 adapter 只承诺四件事：提交、查状态、取消、终态事实。除此之外它必须**如实
声明自己有什么能力**，特别是两件框架不能替它假设的：

* ``reconnect_by_correlation_key``：ACK 丢了之后，能不能按我们自己的 correlation
  key 反查"这次提交到底有没有进队列"。能查才做 reconcile；不能查就把状态留成
  unknown 等接管，**绝不盲目重提**。
* ``remote_write_isolation``：计算节点上有没有物理写边界。Slurm / PBS 作业跑在
  用户身份下，没有沙箱——所以这里是 ``missing``，框架只能在提交前检查脚本与参数
  里的路径、事后从快照看出越界并披露，不得声称有物理只读墙。

新增一个调度器 = 加一个 adapter + 一份 references，不复制状态机。
"""

from __future__ import annotations

import getpass
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

#: 远端写隔离的三种如实说法。绝不把 unknown 或 missing 讲成 enforced。
ISOLATION_ENFORCED = "enforced"
ISOLATION_MISSING = "missing"
ISOLATION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class AdapterCapabilities:
    """一个 adapter 对自己能力的如实声明。"""

    scheduler: str
    submit: bool
    query_status: bool
    cancel: bool
    terminal_facts: bool
    reconnect_by_correlation_key: bool
    remote_write_isolation: str
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "scheduler": self.scheduler, "submit": self.submit, "query_status": self.query_status,
            "cancel": self.cancel, "terminal_facts": self.terminal_facts,
            "reconnect_by_correlation_key": self.reconnect_by_correlation_key,
            "remote_write_isolation": self.remote_write_isolation, "notes": list(self.notes),
        }


class SchedulerAdapter(Protocol):
    name: str

    def capabilities(self) -> AdapterCapabilities: ...

    def reconnect(self, correlation_key: str, *, since: str | None = None) -> dict[str, Any]: ...


@dataclass
class SlurmAdapter:
    """Slurm 23.11+ / 25.x。correlation key 走 ``--comment=ai4s:<nonce>``。

    提交脚本本来就写了那条 comment（``resource_manager._script_for``），所以 ACK
    丢失时可以按它反查而不必猜。**两条查询路径，实测（Docker Slurm 23.11.4，
    2026-09-23）不等价**：

    * ``squeue -h -o '%k'`` 对还在队列里（PENDING / RUNNING）的作业直接返回
      comment，**不依赖任何站点配置**。ACK 刚丢的时候作业正处于这个阶段，所以
      这是主路径。
    * ``sacct -o Comment`` 只有站点设了 ``AccountingStoreFlags=job_comment``
      才有内容；默认配置下那一列是空的。实测里没设这个 flag 时，明明带了
      ``--comment`` 的作业查回来 Comment 为空——只按 sacct 判会得出"没这个作业"
      的错误结论，进而重复提交。所以它只作已终态作业的补充路径。
    """

    name: str = "slurm"
    user: str = field(default_factory=lambda: _current_user())

    def capabilities(self) -> AdapterCapabilities:
        has = {cmd: bool(shutil.which(cmd)) for cmd in ("sbatch", "squeue", "sacct", "scancel")}
        notes = [
            "计算节点上作业以用户身份运行，没有沙箱；越界写只能事后从快照看出并披露。",
        ]
        if has["squeue"]:
            notes.append("按 correlation key 反查走 squeue 的 %k（队列内作业，不依赖站点 accounting 配置）。")
        if has["sacct"]:
            notes.append("已终态作业的补充反查走 sacct Comment；站点未设 "
                         "AccountingStoreFlags=job_comment 时该列为空，所以 sacct 查不到"
                         "不能当作『作业不存在』的证据。")
        return AdapterCapabilities(
            scheduler="slurm",
            submit=has["sbatch"],
            query_status=has["squeue"] or has["sacct"],
            cancel=has["scancel"],
            terminal_facts=has["sacct"],
            reconnect_by_correlation_key=has["squeue"],
            remote_write_isolation=ISOLATION_MISSING,
            notes=tuple(notes),
        )

    def reconnect(self, correlation_key: str, *, since: str | None = None) -> dict[str, Any]:
        """按 correlation key 反查这次提交进没进队列。

        返回 ``{"status": "found"|"absent"|"cannot_query", "jobs": [...]}``。
        **cannot_query 不等于 absent**：查不了就留 unknown，不重提。
        只有两条路径都能查、且都没查到，才敢说 absent。
        """
        caps = self.capabilities()
        if not caps.reconnect_by_correlation_key:
            return {"status": "cannot_query", "scheduler": "slurm",
                    "reason": "squeue 不可用，无法按 correlation key 反查队列内的作业"}
        needle = f"ai4s:{correlation_key}"
        jobs: list[dict[str, str]] = []
        probes: list[dict[str, Any]] = []

        queue_argv = ["squeue", "-h", "-u", self.user, "-o", "%i|%j|%k|%T"]
        queued = _run(queue_argv, timeout=20)
        probes.append({"argv": queue_argv, "ok": bool(queued.get("ok"))})
        if not queued.get("ok"):
            return {"status": "cannot_query", "scheduler": "slurm", "probes": probes,
                    "reason": f"squeue 调用失败：{str(queued.get('stderr') or '')[:200]}"}
        for line in str(queued.get("stdout") or "").splitlines():
            parts = [p.strip() for p in line.split("|")]
            parts.extend([""] * (4 - len(parts)))
            job_id, job_name, comment, state = parts[:4]
            if job_id and needle in comment:
                jobs.append({"job_id": job_id, "job_name": job_name, "state": state, "source": "squeue"})

        accounting_conclusive = False
        if caps.terminal_facts:
            acct_argv = ["sacct", "-n", "-P", "-u", self.user, "-S", since or _default_since(),
                         "-o", "JobIDRaw,JobName,Comment,State,ExitCode"]
            acct = _run(acct_argv, timeout=20)
            probes.append({"argv": acct_argv, "ok": bool(acct.get("ok"))})
            if acct.get("ok"):
                rows = [l for l in str(acct.get("stdout") or "").splitlines() if l.strip()]
                # 站点没开 job_comment 时整列为空——那样的 sacct 结果不能当"查过了"。
                accounting_conclusive = any(
                    len(l.split("|")) > 2 and l.split("|")[2].strip() for l in rows)
                for line in rows:
                    parts = [p.strip() for p in line.split("|")]
                    parts.extend([""] * (5 - len(parts)))
                    job_id, job_name, comment, state, exit_code = parts[:5]
                    if "." in job_id or needle not in comment:
                        continue
                    if any(j["job_id"] == job_id for j in jobs):
                        continue
                    jobs.append({"job_id": job_id, "job_name": job_name, "state": state,
                                 "exit_code": exit_code, "source": "sacct"})
        if jobs:
            return {"status": "found", "scheduler": "slurm", "jobs": jobs,
                    "correlation_key": correlation_key, "probes": probes}
        if accounting_conclusive:
            return {"status": "absent", "scheduler": "slurm", "jobs": [],
                    "correlation_key": correlation_key, "probes": probes,
                    "basis": "队列里没有，accounting 里 Comment 列有内容且不含这个 key"}
        return {"status": "cannot_query", "scheduler": "slurm", "jobs": [], "probes": probes,
                "reason": ("队列里没查到，而 accounting 的 Comment 列为空——站点很可能没开 "
                           "AccountingStoreFlags=job_comment。这种情况下查不到不等于没有，"
                           "状态保持 unknown 等接管。")}


@dataclass
class PbsAdapter:
    """PBS Pro / Torque。

    PBS 的指令里没有通用 comment 字段，作业名也有长度上限，所以 correlation key
    反查这一条**声明为假**：ACK 丢了只能留 unknown 等接管。这不是实现偷懒，是
    平台确实没给可靠的按键反查；假装能查会直接导致重复提交。
    """

    name: str = "pbs"

    def capabilities(self) -> AdapterCapabilities:
        has = {cmd: bool(shutil.which(cmd)) for cmd in ("qsub", "qstat", "qdel")}
        return AdapterCapabilities(
            scheduler="pbs",
            submit=has["qsub"], query_status=has["qstat"], cancel=has["qdel"],
            terminal_facts=has["qstat"],
            reconnect_by_correlation_key=False,
            remote_write_isolation=ISOLATION_MISSING,
            notes=(
                "计算节点上作业以用户身份运行，没有沙箱；越界写只能事后从快照看出并披露。",
                "PBS 指令没有通用 comment 字段，作业名有长度上限：没有可靠的按 correlation key 反查。",
                "ACK 丢失时状态保持 unknown 等接管，绝不重提。",
            ),
        )

    def reconnect(self, correlation_key: str, *, since: str | None = None) -> dict[str, Any]:
        return {"status": "cannot_query", "scheduler": "pbs",
                "reason": "PBS 没有可靠的按 correlation key 反查；不要据此判断作业不存在"}


@dataclass
class LocalAdapter:
    """本机受管作业（Core 沙箱容器）。

    写边界由沙箱原生后端物理保证，所以 ``remote_write_isolation`` 是 enforced。
    反查按不可变 container id 做，不是按 nonce 猜。
    """

    name: str = "local"

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            scheduler="local", submit=True, query_status=True, cancel=True, terminal_facts=True,
            reconnect_by_correlation_key=False,
            remote_write_isolation=ISOLATION_ENFORCED,
            notes=("本机作业跑在 Core 沙箱里，可写根之外物理写不进去。",
                   "没有按 correlation key 的反查：容器身份由提交时冻结的 immutable id 承担。"),
        )

    def reconnect(self, correlation_key: str, *, since: str | None = None) -> dict[str, Any]:
        return {"status": "cannot_query", "scheduler": "local",
                "reason": "本机作业按不可变容器 id 对账，不按 correlation key 反查"}


_ADAPTERS: dict[str, Any] = {}


def adapter_for(scheduler: str) -> Any:
    key = str(scheduler or "").strip().lower()
    if key not in _ADAPTERS:
        _ADAPTERS[key] = {"slurm": SlurmAdapter, "pbs": PbsAdapter, "torque": PbsAdapter,
                          "local": LocalAdapter}.get(key, LocalAdapter)()
    return _ADAPTERS[key]


def capabilities_for(scheduler: str) -> AdapterCapabilities:
    return adapter_for(scheduler).capabilities()


def resolve_lost_ack(state: Any, *, scheduler: str, correlation_key: str) -> dict[str, Any]:
    """ACK 丢失后的唯一合法处置（governing §3 第 2 层）。

    能按 correlation key 查的 adapter 去查：查到就把外部 id 绑上（``job_reconciled``）；
    确认没进队列才算 absent。查不了就记 ``job_unknown`` 等接管——**绝不重提**。
    """
    try:
        from . import ledger as _ledger
    except ImportError:  # pragma: no cover
        from tools import ledger as _ledger  # type: ignore
    adapter = adapter_for(scheduler)
    probe = adapter.reconnect(correlation_key)
    status = str(probe.get("status") or "cannot_query")
    if status == "found":
        jobs = probe.get("jobs") or []
        external_id = str(jobs[0].get("job_id")) if jobs else None
        _ledger.try_record(state, "job_reconciled", nonce=correlation_key, scheduler=scheduler,
                           external_id=external_id, state="submitted" if external_id else "unknown",
                           found=len(jobs), raw=probe.get("raw"))
        return {"decision": "adopted", "external_id": external_id, "jobs": jobs, "probe": probe,
                "resubmit_allowed": False}
    if status == "absent":
        _ledger.try_record(state, "job_absent", nonce=correlation_key, scheduler=scheduler,
                           argv=probe.get("argv"), reason="adapter 按 correlation key 确认这次提交没有进队列")
        return {"decision": "absent", "probe": probe, "resubmit_allowed": True,
                "note": "adapter 明确查到没有这个作业，重提是安全的"}
    _ledger.try_record(state, "job_unknown", nonce=correlation_key, scheduler=scheduler,
                       reason=str(probe.get("reason") or "adapter 无法按 correlation key 反查"))
    return {"decision": "unknown", "probe": probe, "resubmit_allowed": False,
            "note": "查不了不等于没有：状态保持 unknown 等接管，不重提"}


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def _default_since() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 7 * 24 * 3600))


def _run(argv: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        from .resource_manager import _run as _rm_run
    except ImportError:  # pragma: no cover
        from tools.resource_manager import _run as _rm_run  # type: ignore
    return _rm_run(argv, timeout=timeout)
