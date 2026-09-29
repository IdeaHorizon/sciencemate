"""v2.0：first-class TaskList 系统。

把"agent 长任务该做啥"从 memory.jsonl 的 kind='todo' 剥出来作独立 system。
学 Claude Code TodoWrite —— 显式 task 状态机，每轮 LLM call 看到完整 list，
**强约束**：同 owner_node 同一时刻最多 1 个 in_progress。

跟 memory / scratchpad 的区别：
  task      —— 跨 run 项目级，结构化 plan，驱动行动
  scratchpad —— 单 run，agent 自由速记下一步思路
  memory    —— 跨 run，soft recall（pitfalls / workflows / prefs）

存储：
  ~/.harness-framework/projects/<id>/tasks/
    ├── tasks.jsonl     ← truth，append + last-state-wins per id
    ├── active.md       ← 自动 render 视图，agent 每轮注入
    └── completed.md    ← 自动 render audit view（on-demand 看）

设计原则：
  - jsonl 是 truth，markdown 是视图（context_engine 注入 active.md 内容）
  - 同 owner_node 每时刻 ≤ 1 个 in_progress（framework hard enforce）
  - task id 递增整数（T01 / T02 / ...），人类好读，jsonl 写时检查最大值
  - completed task 不删，保 audit trail
"""
from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)


_VALID_STATUSES = ("pending", "in_progress", "completed", "blocked")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    """一条 task 记录。"""
    id: str                          # T01 / T02 / ... 递增
    title: str
    description: str = ""
    status: str = "pending"          # pending | in_progress | completed | blocked
    owner_node: str = ""             # 创建时的 state.node_type
    parent_id: str | None = None     # 挂在哪个父 task 下（树形）
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    blocked_reason: str | None = None
    created_by_run_id: str = ""
    #: 这条待办**出生在哪个会话**（#952）。任务清单是项目级的，而会话是人
    #: 交代一件事的边界：少了这个字段，「本会话自己的开放作业」和「项目里
    #: 别人两天前留下的历史待办」在模型眼里长得一模一样，都写着"你 own 的
    #: pending"，于是每个新 run 开局先去替别人打扫。老记录没有这个字段 → ""，
    #: 注入时按「来源不明」处理，不冒充本会话的。
    created_in_session_id: str = ""
    #: **任务的真身份**（#1080 第 1 条）。`Txx` 只是人类可读的别名。
    #:
    #: `Txx` 当不了身份：它由 `_next_id()` 读全表取 max+1 算出来，而 `_persist`
    #: 是裸 append。实测（4 个进程 × 各 create 25 次，跑六轮）：jsonl 每次都是
    #: 100 行，`list_all()` 只剩 53–57 条 —— 多个调用方拿到同一个 Txx，后写的
    #: 静默覆盖先写的。本地部署下同一用户在同一项目开两个会话就有两个进程写
    #: 同一个 tasks.jsonl，这不是理论局面。
    #:
    #: 老记录没有这个字段 → 读回来时按 `Txx` 当身份（它们本来就是那么存的），
    #: 不凭空补一个 uuid 假装它一直有。
    task_instance_uuid: str = ""
    notes: list[str] = field(default_factory=list)   # complete/update 时 append

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(**{k: v for k, v in d.items()
                       if k in cls.__dataclass_fields__})


class TaskListError(Exception):
    """task 操作违反约束（如 hard limit）。"""


class TaskList:
    """项目级 task 管理。

    truth = tasks.jsonl（append + last-write-wins per id）
    view  = active.md / completed.md（auto-rendered，agent 通过 context_engine 看）

    实例化：TaskList(tasks_dir=projects/<id>/tasks/)
    每次操作后自动 rewrite jsonl + 两个 markdown 视图。
    """

    def __init__(self, tasks_dir: Path):
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.tasks_dir / "tasks.jsonl"
        self.active_md_path = self.tasks_dir / "active.md"
        self.completed_md_path = self.tasks_dir / "completed.md"

    # ── 读 ─────────────────────────────────────────────────────────────────

    def list_all(self) -> list[Task]:
        """读 jsonl 还原所有 task（last-state-wins per id）。"""
        if not self.jsonl_path.exists():
            return []
        by_id: dict[str, Task] = {}
        for line in self.jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = Task.from_dict(d)
            # **按身份收敛，不按别名**（#1080 第 1 条）。
            #
            # 从前这里是 `by_id[t.id] = t` —— 两个进程各自算出同一个 `Txx` 时，
            # 后写的把先写的整条抹掉，而两条记的是**两件不同的事**。
            # 老记录没有 uuid：它们当初就是按 Txx 存的，仍按 Txx 收敛。
            by_id[t.task_instance_uuid or t.id] = t      # last-write-wins per identity
        # 按 id 数字部分排序
        def _idx(t: Task) -> int:
            try:
                return int(t.id.lstrip("Tt"))
            except (ValueError, AttributeError):
                return 0
        return sorted(by_id.values(), key=_idx)

    def get(self, task_id: str) -> Task | None:
        """按**身份**或别名取一条任务。

        uuid 优先：别名可能因为历史上的并发覆盖而重复，身份不会。
        """
        needle = str(task_id or "")
        if not needle:
            return None
        tasks = self.list_all()
        for t in tasks:
            if t.task_instance_uuid and t.task_instance_uuid == needle:
                return t
        for t in tasks:
            if t.id == needle:
                return t
        return None

    def filter(self, status: str | None = None,
               owner_node: str | None = None) -> list[Task]:
        tasks = self.list_all()
        if status:
            tasks = [t for t in tasks if t.status == status]
        if owner_node:
            tasks = [t for t in tasks if t.owner_node == owner_node]
        return tasks

    # ── 写 ─────────────────────────────────────────────────────────────────

    def _next_id(self) -> str:
        """生成下一个 task id (T01 / T02 / ...)。**只在持有创建锁时调用。**"""
        existing = self.list_all()
        max_n = 0
        for t in existing:
            try:
                n = int(t.id.lstrip("Tt"))
                max_n = max(max_n, n)
            except (ValueError, AttributeError):
                continue
        return f"T{max_n + 1:02d}"

    @contextlib.contextmanager
    def _creation_lock(self):
        """把「算别名 + 落行」围成一个临界区（#1080 第 1 条）。

        没有它，`_next_id()` 读全表取 max+1、`_persist` 裸 append，两个进程之间
        就是个教科书式的 read-modify-write 竞态：实测 4 进程各 create 25 次，
        100 行里只剩 53–57 条身份。

        锁走 `shared.lib.filelock`（仓库里跨进程文件锁的唯一入口，两平台一份实现）
        —— 这里要的是**跨进程**：写同一个 tasks 目录的是不同进程（同一用户在同一
        项目开两个会话 = 两个常驻 harness 子进程）。

        拿不到锁不致命：uuid 已经保证身份唯一，退化的只是别名可能重号，所以锁
        失败照常往下走。
        """
        from shared.lib import filelock

        try:
            with filelock.exclusive(self.tasks_dir / ".create.lock"):
                yield
        except OSError as exc:
            log.debug("task creation lock unavailable: %s", exc)
            yield

    def _persist(self, t: Task) -> None:
        """append 到 jsonl（last-state-wins per identity）+ rewrite markdown 视图。"""
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
        self._rewrite_views()

    def create(self, title: str, description: str,
               owner_node: str, run_id: str,
               parent_id: str | None = None,
               session_id: str = "") -> Task:
        """创建新 task（status='pending'）。"""
        # 判决拆除（verdicts_core tasks:154）：空 title 不再拒绝——如实记「(未命名)」。
        title = title.strip() if title and title.strip() else "(未命名)"
        if parent_id:
            parent = self.get(parent_id)
            if parent is None:
                raise TaskListError(f"parent_id={parent_id!r} 不存在")
            if parent.status == "completed":
                # 判决拆除（tasks:160）：发现还有子活=父其实没完。如实重开并留痕，
                # 见证不禁令（账本状态机如实迁移组）。
                parent.status = "in_progress"
                parent.notes.append(f"[{_now_iso()}] 因新增子 task 重开")
                self._persist(parent)
        with self._creation_lock():
            t = Task(
                id=self._next_id(),
                title=title,
                description=description.strip(),
                status="pending",
                owner_node=owner_node,
                parent_id=parent_id,
                created_by_run_id=run_id,
                created_in_session_id=str(session_id or ""),
                # 身份在**创建时**定死，之后不可变。别名（Txx）只给人读。
                task_instance_uuid=str(uuid4()),
            )
            self._persist(t)
        return t

    def start(self, task_id: str, owner_node: str) -> Task:
        """开始 task。**hard limit**：同 owner_node ≤ 1 个 in_progress。"""
        t = self.get(task_id)
        if t is None:
            raise TaskListError(f"task_id={task_id!r} 不存在")
        if t.status == "in_progress":
            return t        # 幂等
        if t.status == "completed":
            # 判决拆除（tasks:183）：「发现还有活要干」是正常科学修正，记 reopen。
            t.notes.append(f"[{_now_iso()}] reopen（曾 completed）")
        if t.status == "blocked":
            # 判决拆除（tasks:185）：start 一个 blocked task = 显然想解锁，隐式
            # 解锁并留痕，别逼两次调用换一次（纯仪式）。
            t.notes.append(f"[{_now_iso()}] 隐式 unblock（原因曾是：{t.blocked_reason!r}）")
            t.blocked_reason = None
        # 判决拆除（tasks:193）：「同 owner ≤1 个 in_progress」是任意阈值——并行
        # 推进两条 task 账仍真、可逆；要不要专注是模型能判的事。
        t.status = "in_progress"
        t.started_at = _now_iso()
        self._persist(t)
        return t

    def complete(self, task_id: str, notes: str | None = None) -> Task:
        t = self.get(task_id)
        if t is None:
            raise TaskListError(f"task_id={task_id!r} 不存在")
        if t.status == "completed":
            return t       # 幂等
        t.status = "completed"
        t.completed_at = _now_iso()
        if notes:
            t.notes.append(f"[{_now_iso()}] {notes.strip()}")
        self._persist(t)
        return t

    def block(self, task_id: str, reason: str) -> Task:
        t = self.get(task_id)
        if t is None:
            raise TaskListError(f"task_id={task_id!r} 不存在")
        if not reason or not reason.strip():
            raise TaskListError("block 需要 reason（为什么被卡）——语义必需，不验长度")
        if t.status == "completed":
            # 判决拆除（tasks:223）：矛盾迁移记为 reopen 比拒绝更真。
            t.notes.append(f"[{_now_iso()}] reopen 为 blocked（曾 completed）")
        t.status = "blocked"
        t.blocked_reason = reason.strip()
        self._persist(t)
        return t

    def unblock(self, task_id: str) -> Task:
        """blocked → pending（重新可 start）。"""
        t = self.get(task_id)
        if t is None:
            raise TaskListError(f"task_id={task_id!r} 不存在")
        if t.status != "blocked":
            return t        # 幂等
        t.status = "pending"
        t.blocked_reason = None
        self._persist(t)
        return t

    # ── 视图 rewrite ───────────────────────────────────────────────────────

    def _rewrite_views(self) -> None:
        tasks = self.list_all()
        active = self._render_active(tasks)
        completed = self._render_completed(tasks)
        self.active_md_path.write_text(active, encoding="utf-8")
        self.completed_md_path.write_text(completed, encoding="utf-8")

    def _render_active(self, tasks: list[Task]) -> str:
        """active.md：in_progress + pending + blocked + 最近 5 条 completed（context 用）"""
        in_prog = [t for t in tasks if t.status == "in_progress"]
        pending = [t for t in tasks if t.status == "pending"]
        blocked = [t for t in tasks if t.status == "blocked"]
        recent_done = [t for t in tasks if t.status == "completed"]
        # 最近 5 条 completed 按 completed_at 倒序
        recent_done.sort(key=lambda t: t.completed_at or "", reverse=True)
        recent_done = recent_done[:5]

        lines: list[str] = ["# Tasks"]
        lines.append("")

        if in_prog:
            lines.append(f"## ⏵ In progress ({len(in_prog)})")
            for t in in_prog:
                lines.append(self._render_task_line(t))
            lines.append("")

        if pending:
            lines.append(f"## ◯ Pending ({len(pending)})")
            for t in pending:
                lines.append(self._render_task_line(t))
            lines.append("")

        if blocked:
            lines.append(f"## ⏸ Blocked ({len(blocked)})")
            for t in blocked:
                lines.append(self._render_task_line(t))
            lines.append("")

        if recent_done:
            lines.append(f"## ✓ Recently completed ({len(recent_done)})")
            for t in recent_done:
                lines.append(self._render_task_line(t))

        if len(lines) == 2:    # 只有 "# Tasks" + 空行
            lines.append("（无 task）")
        return "\n".join(lines)

    def _render_completed(self, tasks: list[Task]) -> str:
        """completed.md：所有 completed task 全量列表（audit）。"""
        done = [t for t in tasks if t.status == "completed"]
        done.sort(key=lambda t: t.completed_at or "", reverse=True)
        lines: list[str] = ["# Completed tasks"]
        lines.append("")
        if not done:
            lines.append("（无）")
            return "\n".join(lines)
        for t in done:
            lines.append(self._render_task_line(t, full=True))
        return "\n".join(lines)

    def _render_task_line(self, t: Task, full: bool = False) -> str:
        marker = {
            "in_progress": "⏵",
            "pending": "◯",
            "completed": "✓",
            "blocked": "⏸",
        }.get(t.status, "?")
        parent = f" ↳ parent={t.parent_id}" if t.parent_id else ""
        line = f"- {marker} ({t.id}) [{t.owner_node}] {t.title}{parent}"
        if t.status == "blocked":
            line += f"\n    blocked: {t.blocked_reason}"
        if t.status == "in_progress" and t.started_at:
            line += f"\n    started: {t.started_at}"
        if full and t.description:
            line += f"\n    description: {t.description}"
        if full and t.notes:
            for n in t.notes:
                line += f"\n    note: {n}"
        return line
