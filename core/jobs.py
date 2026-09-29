"""后台计算作业的**项目级登记表**。

## 为什么需要它

框架一直有 `ActiveRunInfo`（core/pause.py）—— 但它登记的是**子 run**：「哪个节点
在跑」。而节点手底下真正吃算力的那些东西，框架完全不知道：

    nohup bash serve_235b_bf16.sh &      # 一个吃 4×A100 的 vLLM 服务
    nohup bash h3_rerun.sh &             # 一个跑几小时的推理作业

E2E-7 实测（2026-08-04）：experiment 节点连着三次做不出 `experiment_log`。查下来
**它一个错都没报** —— 服务健康、作业在跑、进度在动，它只是在

    sleep 1500; tail -4 h3_run2.log      # 睡 25 分钟看一眼
    sleep 3000; tail -6 h3_rerun.log     # 睡 50 分钟看一眼

一觉一觉地等。而每次醒来**消耗一个 turn**，`max_turns=200` 的寿命全喂给了 sleep，
撞线时 run 结束、产出没落盘、10 小时 30M token 全废。

调度器这边看到的只有"experiment 这个 run 在跑」。它想知道更多得自己想起来去调
`runtime_control(action="progress")` 翻 transcript 猜 —— **拉，不是推**，而且
"什么时候该查「全靠模型自觉。

## 这个登记表补的是哪一层

    已有：子 run（谁在跑）        本模块：子作业（跑的是什么、占什么、还要多久）

一条登记就能同时解掉三件事：

  1. **调度器不用猜** —— 查登记表直接看到 `h3_rerun / 4×A100 / 预计 6h /
     已跑 2h / 进度 41%`，不用翻 transcript 文本。
  2. **"该不该干预「有了机械判据** —— 现在完全靠模型或人肉眼看。有了
     `expected_duration` 对照实际，就变成可判的：**预计 1 小时跑了 6 小时才该
     报警；预计 6 小时跑了 5 小时就该安心睡**。见 `JobRecord.overrun_ratio`。
  3. **"实验用了什么算力「变成框架记的事实** —— 现在论文里那句「用了 4×A100 跑了
     35.6 GPU 小时「是模型自己记的自述。

## 为什么持久化到项目级而不是 state.hook_state

hook_state 随 run 消亡。而这个场景的**核心事实**就是「作业活得比 run 长"——
run 撞线结束了，那个 vLLM 还在吃着 4 张卡。登记表必须跨 run 存活，下一个 run
接手时才知道「上一轮起的作业还在跑，别重复起」。

存 `<project_root>/jobs.jsonl`，与 kb_*.jsonl / memory.jsonl 同一套约定。
append-only：状态变更追加新记录，读取时按 job_id 取最后一条 —— 与 transcript
同构，历史可审计。
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_JOBS_FILE = "jobs.jsonl"

# 起后台进程的 shell 形态 —— 用来机械识别「起了作业却没登记」。
# 覆盖 nohup / setsid / 行尾 & / disown；不覆盖 `sleep N; cmd` 这类前台等待
# （那是轮询，不是起作业）。
_BACKGROUND_LAUNCH = re.compile(
    r"(^|[;&|]\s*)(nohup|setsid)\s|&\s*$|&\s*(echo|disown)", re.MULTILINE)

OPEN_STATUSES = frozenset({"running", "unknown"})


@dataclass(frozen=True)
class JobRecord:
    """一个后台计算作业。字段就是「点开卡片该看到什么」。"""

    job_id: str
    purpose: str = ""
    """这个作业干什么用的（人话，给调度器/用户看）。"""
    resources: str = ""
    """占什么算力（"4×A100 / GPU4,5" 这种）。"""
    expected_duration_s: float = 0.0
    """预计跑多久。**可以粗，但必须有** —— 没有它就没有「该不该干预「的判据。"""
    progress_probe: str = ""
    """怎么查进度（一条能跑的命令 / 一个会长的文件路径）。"""
    pid: int | None = None
    scheduler_job_id: str | None = None
    """HPC 调度器的作业号（走 submit_job 那条路时有）。"""
    node_type: str = ""
    run_id: str = ""
    """哪个 run 起的 —— 它可能早就结束了，作业还活着。"""
    by_user_id: str = ""
    """谁的会话起的（`core.identity`，平台按人发）。项目层一个项目一份、成员共用
    （`docs/RFC_PROJECT_HOME_20260924.md`），「谁在跑」不能再靠账本在谁的 home 里推。"""
    started_at: float = 0.0
    status: str = "running"
    """running | done | failed | cancelled | unknown"""
    last_progress: str = ""
    last_checked_at: float = 0.0
    note: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    # ── 派生量：调度器要的就是这些 ──────────────────────────────────────────
    @property
    def elapsed_s(self) -> float:
        return max(0.0, time.time() - self.started_at) if self.started_at else 0.0

    @property
    def eta_s(self) -> float | None:
        """还要多久。没声明预计时长就返回 None（不猜）。"""
        if self.expected_duration_s <= 0:
            return None
        return max(0.0, self.expected_duration_s - self.elapsed_s)

    @property
    def overrun_ratio(self) -> float | None:
        """实际 / 预计。**这才是「该不该干预「的机械判据。**

        1.0 以内 = 按计划走，该安心睡；远大于 1 = 真的不对劲了。
        没声明预计时长 → None：**不可判，而不是「没问题"**
        （"查不出来不是跑成了「这条今天已经栽过好几次）。
        """
        if self.expected_duration_s <= 0:
            return None
        return self.elapsed_s / self.expected_duration_s

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def alive(self) -> bool | None:
        """进程还在不在。查不到（没 pid / 跨机器）返回 None —— 不是 False。"""
        if self.pid is None:
            return None
        from shared.lib import process_control

        return process_control.alive(self.pid)

    def as_dict(self) -> dict:
        d = {
            "job_id": self.job_id, "purpose": self.purpose,
            "resources": self.resources, "status": self.status,
            "node_type": self.node_type, "run_id": self.run_id,
            "by_user_id": self.by_user_id,
            "pid": self.pid, "scheduler_job_id": self.scheduler_job_id,
            "started_at": self.started_at,
            "expected_duration_s": self.expected_duration_s,
            "progress_probe": self.progress_probe,
            "last_progress": self.last_progress,
            "last_checked_at": self.last_checked_at,
            "note": self.note,
        }
        d["elapsed_s"] = round(self.elapsed_s)
        d["eta_s"] = None if self.eta_s is None else round(self.eta_s)
        d["overrun_ratio"] = (None if self.overrun_ratio is None
                              else round(self.overrun_ratio, 2))
        d["alive"] = self.alive()
        return d


def _jobs_path(state: Any) -> Path | None:
    root = getattr(state, "project_root", None)
    return Path(root) / _JOBS_FILE if root else None


def _append(state: Any, payload: dict) -> bool:
    p = _jobs_path(state)
    if p is None:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except OSError:
        log.warning("jobs.jsonl 写入失败", exc_info=True)
        return False


def declare(state: Any, *, purpose: str, resources: str = "",
            expected_duration_s: float = 0.0, progress_probe: str = "",
            pid: int | None = None, scheduler_job_id: str | None = None,
            note: str = "") -> JobRecord:
    """登记一个刚起的后台作业。"""
    ts = time.time()
    job_id = f"job_{int(ts)}_{abs(hash((purpose, pid, ts))) % 100000:05d}"
    rec = JobRecord(
        job_id=job_id, purpose=purpose, resources=resources,
        expected_duration_s=float(expected_duration_s or 0),
        progress_probe=progress_probe, pid=pid,
        scheduler_job_id=scheduler_job_id,
        node_type=str(getattr(state, "node_type", "") or ""),
        run_id=str(getattr(state, "run_id", "") or ""),
        by_user_id=_who(),
        started_at=ts, status="running", note=note,
    )
    # 存下命令行签名：守护进程重启/孤儿化之后 pid 会变，签名是唯一还认得出
    # "还是同一个服务"的东西（见 resolve_live_pids 的根因说明）。
    payload = {"_op": "declare", **rec.as_dict()}
    if pid is not None:
        sig = process_signature(pid)
        if sig:
            payload["process_signature"] = sig
    _append(state, payload)
    return rec


def _who() -> str:
    try:
        from core.identity import current_user_id

        return current_user_id()
    except Exception:
        return ""


def update(state: Any, job_id: str, *, status: str | None = None,
           last_progress: str | None = None, note: str | None = None) -> bool:
    """追加一条状态变更（append-only，不改历史）。"""
    payload: dict = {"_op": "update", "job_id": job_id,
                     "last_checked_at": time.time()}
    if status is not None:
        payload["status"] = status
    if last_progress is not None:
        payload["last_progress"] = last_progress
    if note is not None:
        payload["note"] = note
    return _append(state, payload)


def load(state: Any, *, only_open: bool = False) -> list[JobRecord]:
    """读登记表：按 job_id 取最后一条（append-only 的自然语义）。

    行怎么合成记录只有一处（`_records_from_lines`）—— 这里从前另抄了一份，加一个字段
    就得记得改两处。
    """
    p = _jobs_path(state)
    if p is None or not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        log.warning("jobs.jsonl 读取失败", exc_info=True)
        return []
    return _records_from_lines(lines, only_open=only_open)


class JobsLedgerUnreadable(RuntimeError):
    """登记表在，但读不出来。

    **和「没有作业」必须分开**（#941）：观测面把读失败降级成空清单，
    看起来和「这个项目一个作业都没跑过」一模一样 —— 调用方会据此判 PASS。
    观测不到不许当成没有。
    """


def observe(project_root: Path | str, *, only_open: bool = False) -> list[JobRecord]:
    """按项目根读作业登记表 —— 给**观测面**用的入口（#941）。

    与 :func:`load` 的区别只有一个：读不出来时**抛**而不是返回空清单。
    `load` 是给 agent 循环内部用的，那里读不到就当没有是可接受的降级；
    观测面不行 —— 它的答案会被拿去判「这个 run 到底怎么了」。

    文件**不存在**仍然返回 `[]`：那是"确实还没有作业"，不是"读不出来"。
    这两件事分开，正是这个函数存在的理由。
    """
    root = Path(project_root)
    path = root / _JOBS_FILE
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JobsLedgerUnreadable(f"{path}: {exc}") from exc
    return _records_from_lines(text.splitlines(), only_open=only_open)


def _records_from_lines(lines: list[str], *, only_open: bool = False) -> list[JobRecord]:
    """把 jsonl 的行合成记录：按 job_id 取最后一条（append-only 的自然语义）。"""
    merged: dict[str, dict] = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        jid = d.get("job_id")
        if not jid:
            continue
        merged.setdefault(jid, {}).update(
            {k: v for k, v in d.items() if not k.startswith("_")})
    out = []
    for d in merged.values():
        out.append(JobRecord(
            job_id=d["job_id"], purpose=d.get("purpose", ""),
            resources=d.get("resources", ""),
            expected_duration_s=float(d.get("expected_duration_s") or 0),
            progress_probe=d.get("progress_probe", ""),
            pid=d.get("pid"), scheduler_job_id=d.get("scheduler_job_id"),
            node_type=d.get("node_type", ""), run_id=d.get("run_id", ""),
            by_user_id=str(d.get("by_user_id") or ""),
            started_at=float(d.get("started_at") or 0),
            status=d.get("status", "running"),
            last_progress=d.get("last_progress", ""),
            last_checked_at=float(d.get("last_checked_at") or 0),
            note=d.get("note", ""), raw=d))
    out.sort(key=lambda r: r.started_at, reverse=True)
    return [r for r in out if r.is_open] if only_open else out


def looks_like_background_launch(cmd: str) -> bool:
    """这条 shell 命令是不是在起后台进程。

    机械识别，用于「起了作业却没登记「的提醒 —— 否则这个登记表就会变成又一个
    "机制存在但没人用「（今天已经数出七次了）。
    """
    return bool(_BACKGROUND_LAUNCH.search(str(cmd or "")))


def undeclared_launch_hint(state: Any, cmd: str) -> str | None:
    """起了后台进程但登记表里没有对应的 open 作业 → 返回一句提醒，否则 None。

    **只提醒不拦截**：合法用途很多（起个临时 http server、跑个 tail -f），
    一刀切拦死会把正常工作也挡住。但提醒进的是**工具返回值**，模型下一轮
    一定看得到 —— 这是「路径」，不是躺在 harness 里没人读的一句话。
    """
    if not looks_like_background_launch(cmd):
        return None
    if load(state, only_open=True):
        return None
    return (
        "⚠️ 你刚起了一个后台进程，但作业登记表里没有对应条目。\n"
        "长作业请调 `declare_job(purpose=..., resources=..., "
        "expected_duration_s=..., progress_probe=..., pid=...)` 登记：\n"
        "  · 调度器就能直接看到「这是什么、占什么算力、还要多久」，不用去翻你的日志\n"
        "  · 有了预计时长，「跑超时了该不该干预「才有机械判据\n"
        "  · **作业活得比 run 长**：本 run 撞 max_turns 结束后，下一个 run 靠这张表"
        "才知道它还在跑、不用重复起\n"
        "（临时的小命令不用登记。）"
    )


def observed_gpus() -> dict[int, list[int]] | None:
    """实测：每个 pid 现在占着哪几张物理卡。查不到返回 **None**，不是 {}。

    2026-08-05 e2e9 实测：登记表写着「2×A100-80GB (GPU 4,5)」，`nvidia-smi`
    显示 **GPU 0** 被占 80.5GB、1–7 全空。也就是说它以为自己在 4、5 号卡上，
    实际跑在 0 号卡上，而且一张卡就吃满了（vLLM 默认预占 90% 显存）。

    根子是登记表只存了**先验**（我打算占哪几张），从没跟观测对过账。调度器
    照着它做资源决策会撞车 —— 以为 0 号空着，其实满的。

    `None` 和 `{}` 必须分开：没有 nvidia-smi（CPU 机器 / 容器里看不见）是
    「查不出来」，不是「没有作业在占卡」。查不出来不许当成对得上。
    """
    import shutil
    import subprocess
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-compute-apps=pid,gpu_bus_id",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        bus = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,gpu_bus_id",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        if bus.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None

    bus2idx: dict[str, int] = {}
    for line in bus.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit():
            bus2idx[parts[1].lower()] = int(parts[0])

    by_pid: dict[int, list[int]] = {}
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        idx = bus2idx.get(parts[1].lower())
        if idx is None:
            continue
        by_pid.setdefault(int(parts[0]), []).append(idx)
    return by_pid


def _ps_table() -> list[tuple[int, int, str]] | None:
    """(pid, ppid, cmdline) 全表。查不到返回 None。"""
    from shared.lib import process_control

    return process_control.process_table()


def _descendant_pids(pid: int) -> set[int]:
    """pid 及其所有后代。

    ⚠️ 只在进程树**没断**时管用。`nohup ... &` 起的守护进程，父 shell 一退出
    就被 init 收养（ppid=1），链条当场断掉 —— 而登记表存在的意义恰恰就是记录
    这种活得比 run 长的守护进程。所以这个函数是**尽力而为**，不是权威判据；
    真正的兜底是 `resolve_live_pids()` 的签名匹配。
    """
    rows = _ps_table()
    if rows is None:
        return {pid}
    children: dict[int, list[int]] = {}
    for p, pp, _ in rows:
        children.setdefault(pp, []).append(p)
    seen = {pid}
    stack = [pid]
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def process_signature(pid: int) -> str:
    """取 pid 的命令行签名 —— 用来在它换了 pid 之后还认得出同一个作业。"""
    rows = _ps_table()
    if rows is None:
        return ""
    for p, _, cmd in rows:
        if p == pid:
            return cmd
    return ""


#: 签名里这些片段足以锚定一个服务实例（端口/模型路径/脚本名）
_SIG_ANCHOR = re.compile(r"(--port[= ]\s*\d+|--model[= ]\s*\S+|/[\w./-]+\.(?:py|sh)\b|serve\s+\S+)")


def _signature_keys(sig: str) -> list[str]:
    return sorted(set(m.group(0) for m in _SIG_ANCHOR.finditer(sig or "")))


def resolve_live_pids(job: "JobRecord") -> tuple[list[int], str]:
    """这个作业**现在**跑在哪些 pid 上，以及是怎么认出来的。

    2026-08-05 e2e9 实测的两种失配，都不是"作业没了"：

      ① **重启换了 pid**：turn 15 起了一次 vLLM，turn 28 换个 conda 环境同端口
         又起了一次 —— 新 pid 从没登记，登记表停在第一次那个死 pid 上。
      ② **孤儿化**：`nohup ... &` 的守护进程父 shell 一退出就被 init 收养
         （实测 117273 ← 117117 ← 1），顺着登记 pid 往下走永远找不到它。

    所以先看登记 pid 还在不在；不在就拿登记时存下的**命令行签名**去全表找
    继任者。找到了说"pid 已变"，找不到才说"查不到" —— 而"查不到"永远不等于
    "作业没了"，更不等于"没占卡"。

    返回 (pids, 认法)；`认法` ∈ {"declared", "signature", "unknown"}。
    """
    if job.pid is None:
        return [], "unknown"
    rows = _ps_table()
    if rows is None:
        return [], "unknown"
    live = {p for p, _, _ in rows}
    if job.pid in live:
        return sorted(_descendant_pids(job.pid)), "declared"

    keys = _signature_keys(job.raw.get("process_signature", ""))
    if not keys:
        return [], "unknown"
    hits = [p for p, _, cmd in rows if all(k in cmd for k in keys)]
    if not hits:
        return [], "unknown"
    out: set[int] = set()
    for h in hits:
        out.update(_descendant_pids(h))
    return sorted(out), "signature"


def observed_gpus_for(job: "JobRecord",
                      by_pid: dict[int, list[int]] | None) -> list[int] | None:
    """这个作业**实际**占着哪几张卡。查不到返回 None（不是空列表）。

    "认不出它现在跑在哪" 和 "它一张卡都没占" 是两件事：前者返回 None
    （不可判），后者返回 []（有证据的否定）。把前者渲染成后者，就是把
    证据缺失当成了否定判决 —— 这条今天已经栽过好几次。
    """
    if by_pid is None or job.pid is None:
        return None
    pids, how = resolve_live_pids(job)
    if how == "unknown":
        return None
    got: set[int] = set()
    for p, idxs in by_pid.items():
        if p in pids:
            got.update(idxs)
    return sorted(got)


_GPU_TOKEN = re.compile(r"(?:gpu|卡)\s*[:#]?\s*([0-9]+(?:\s*[,，、]\s*[0-9]+)*)", re.I)


def declared_gpu_indices(resources: str) -> list[int]:
    """从人写的 resources 串里抠出声明的卡号。抠不出来返回 []（不猜）。"""
    out: set[int] = set()
    for m in _GPU_TOKEN.finditer(resources or ""):
        for tok in re.split(r"[,，、]", m.group(1)):
            tok = tok.strip()
            if tok.isdigit():
                out.add(int(tok))
    return sorted(out)


def gpu_reconciliation(job: "JobRecord",
                       by_pid: dict[int, list[int]] | None) -> str:
    """声明 vs 实测的一句话。对不上就说出来，查不到就说查不到。"""
    obs = observed_gpus_for(job, by_pid)
    if obs is None:
        return ""          # 查不出来 —— 不许渲染成"对得上"
    decl = declared_gpu_indices(job.resources)
    if not obs:
        # 有证据的否定：确实认出了它跑在哪些 pid 上，那些 pid 一张卡没占。
        # （认不出跑在哪的情况上面已经返回 None 了，不会走到这。）
        return "  ⚠️ 实测未占任何 GPU（声明了算力，但它的进程一张卡都没占）" if decl else ""
    obs_s = ",".join(str(i) for i in obs)
    if not decl:
        return f"  📌 实测占用 GPU {obs_s}（声明里没写卡号）"
    if set(decl) == set(obs):
        return f"  ✅ 实测占用 GPU {obs_s}（与声明一致）"
    return (f"  ⚠️ **声明 GPU {','.join(str(i) for i in decl)}，"
            f"实测在 GPU {obs_s}** —— 按声明做资源决策会撞车")


def render_for_orchestrator(state: Any) -> str:
    """给调度器看的一屏摘要 —— 它不该为了知道这些去翻 transcript。"""
    jobs = load(state, only_open=True)
    if not jobs:
        return ""
    lines = ["🖥️ **在跑的计算作业**"]
    by_pid = observed_gpus()          # 一次探测，所有作业共用
    for j in jobs:
        eta = ("?" if j.eta_s is None
               else f"{j.eta_s/3600:.1f}h" if j.eta_s >= 3600
               else f"{j.eta_s/60:.0f}min")
        over = ("" if j.overrun_ratio is None
                else f" ⚠️ 已超预计 {j.overrun_ratio:.1f}×" if j.overrun_ratio > 1.5
                else "")
        # "登记的 pid 没了" ≠ "作业没了"：守护进程重启换 pid、或被 init 收养
        # 都会让原 pid 消失。先按签名找继任者，找到才说它还活着，找不到才说没了。
        _pids, _how = resolve_live_pids(j)
        if _how == "declared":
            alive_s = ""
        elif _how == "signature":
            alive_s = f" 🔄 登记 pid {j.pid} 已不在，按签名找到继任 pid {_pids[0]}"
        elif j.pid is None:
            alive_s = ""
        else:
            alive_s = f" ❓ 登记 pid {j.pid} 已不在，也没找到继任进程（不等于作业已结束）"
        lines.append(
            f"  · {j.purpose or j.job_id} —— {j.resources or '资源未声明'}；"
            f"已跑 {j.elapsed_s/3600:.1f}h / 预计还要 {eta}{over}{alive_s}")
        recon = gpu_reconciliation(j, by_pid)
        if recon:
            lines.append(f"    {recon}")
        if j.last_progress:
            lines.append(f"      进度：{j.last_progress[:120]}")
    return "\n".join(lines)
