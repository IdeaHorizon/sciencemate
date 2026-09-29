"""任务合同 —— 一条**只追加、不可改写**的任务意图记录（#1080 / #1097）。

## 为什么它不能长在 TaskList 上

`core/tasks.py` 是一本**工作流账**：`Txx`、status、description，存储是 append +
last-write-wins per identity。那套结构服务的是"这件事做到哪了"，改写是它的正常
工作方式。

而「这个 run 被安排去做什么、绑哪份预注册」是一条**授权**。授权被 last-write-wins
静默覆盖，等于执行节点拿到的合同随时可能在它背后变成另一份 —— #1052 的翼型会话
里，新任务落回了旧 Experiment child 的上下文，正是这个形状。所以合同另起一本账：
只追加，不可改写，不可删除。

## 三条不打算让步的

**一、没有「取最新」。** 并发从同一个 parent 写出两条 revision 时，两条**都留着**，
调用方只能按 `(task_instance_uuid, digest)` 精确取。给一个 `latest()` 等于在分叉
面前替调用方抽签 —— 而那正是 LWW 的病，换个地方再犯一遍。

**二、工作流账与合同互不改写。** Task status 变化不产生、也不修改任何 revision；
追加 revision 不改 Task status。两边各自回答各自的问题
（`tests/test_task_contract_authority.py` 扫盘钉着）。

**三、缺席不等于「明确没有」。** prereg 绑定有三种状态，不是两种：

    exact_bound(artifact_id, version, content_hash)   父明确绑了这一份
    explicit_none(reason)                             父明确说这一趟不绑
    （字段缺席）                                        pending_assignment —— 还没安排

项目里"恰好只有一份 prereg"只能当候选提示，**不构成授权**。把缺席读成
`explicit_none`，就是让执行节点替上游做了那个决定
（[[feedback_absent_check_looks_like_passed_check]]）。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 合同账本的文件名（与 tasks.jsonl 同目录，但**是两本账**）。
FILENAME = "task_contract_revisions.jsonl"

#: 一条 revision 与它前驱的关系。
RELATIONS = ("supersedes", "extends")

#: prereg 绑定的三种状态。`pending_assignment` 是**缺席**的名字，不是一个可写的值。
ASSIGNMENT_EXACT = "exact_bound"
ASSIGNMENT_NONE = "explicit_none"
ASSIGNMENT_PENDING = "pending_assignment"


class TaskContractError(ValueError):
    """合同账本上**按设计**的拒绝（分叉冲突、字段非法、试图改写）。"""


@dataclass(frozen=True)
class PreregAssignment:
    """父给这一趟安排的预注册绑定。两种合法形态互斥。"""

    kind: str                       # ASSIGNMENT_EXACT | ASSIGNMENT_NONE
    artifact_id: str = ""
    version: str = ""
    content_hash: str = ""
    reason: str = ""

    @classmethod
    def exact(cls, artifact_id: str, *, version: str = "", content_hash: str = "") -> PreregAssignment:
        if not str(artifact_id or "").strip():
            raise TaskContractError("exact_bound 必须给 artifact_id")
        return cls(kind=ASSIGNMENT_EXACT, artifact_id=str(artifact_id).strip(),
                   version=str(version or ""), content_hash=str(content_hash or ""))

    @classmethod
    def none(cls, reason: str) -> PreregAssignment:
        if not str(reason or "").strip():
            # 「明确不绑」是一个**决定**，决定要有理由 —— 没有理由的 explicit_none
            # 和忘了填长得一模一样，而那两件事的后果完全不同。
            raise TaskContractError("explicit_none 必须给 reason（这是一个决定，不是缺省）")
        return cls(kind=ASSIGNMENT_NONE, reason=str(reason).strip())

    def as_dict(self) -> dict[str, Any]:
        out = {"kind": self.kind}
        if self.kind == ASSIGNMENT_EXACT:
            out.update({"artifact_id": self.artifact_id,
                        "version": self.version,
                        "content_hash": self.content_hash})
        else:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_dict(cls, data: Any) -> PreregAssignment | None:
        if not isinstance(data, dict) or not data.get("kind"):
            return None
        kind = str(data["kind"])
        if kind == ASSIGNMENT_EXACT:
            return cls(kind=kind, artifact_id=str(data.get("artifact_id") or ""),
                       version=str(data.get("version") or ""),
                       content_hash=str(data.get("content_hash") or ""))
        if kind == ASSIGNMENT_NONE:
            return cls(kind=kind, reason=str(data.get("reason") or ""))
        raise TaskContractError(f"未知的 prereg 绑定形态：{kind!r}")


@dataclass(frozen=True)
class TaskContractRevision:
    """一条不可改写的合同记录。`digest` 由内容算出，是它的身份。"""

    task_instance_uuid: str
    revision: int
    digest: str
    parent_revision_digest: str = ""
    objective_digest: str = ""
    intended_use: str = ""
    target_node: str = ""
    prereg_assignment: dict[str, Any] | None = None
    actor: str = ""
    reason: str = ""
    relation: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def assignment(self) -> PreregAssignment | None:
        """这一趟的 prereg 绑定；**缺席返回 None = pending_assignment**。"""
        return PreregAssignment.from_dict(self.prereg_assignment)

    @property
    def assignment_kind(self) -> str:
        a = self.assignment
        return a.kind if a is not None else ASSIGNMENT_PENDING


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_digest(payload: dict[str, Any]) -> str:
    """合同内容的身份。**不含 digest / at 自身** —— 否则它算的是别的东西。"""
    body = {k: v for k, v in payload.items() if k not in {"digest", "at"}}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


class TaskContractLog:
    """任务合同账本。只追加，不改写，不删除。"""

    def __init__(self, tasks_dir: Path):
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.tasks_dir / FILENAME

    # ── 读 ───────────────────────────────────────────────────────────────

    def all_revisions(self) -> list[TaskContractRevision]:
        if not self.path.exists():
            return []
        out: list[TaskContractRevision] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(TaskContractRevision(**{
                k: v for k, v in data.items()
                if k in TaskContractRevision.__dataclass_fields__
            }))
        return out

    def revisions_for(self, task_instance_uuid: str) -> list[TaskContractRevision]:
        needle = str(task_instance_uuid or "")
        return [r for r in self.all_revisions() if r.task_instance_uuid == needle]

    def get(self, task_instance_uuid: str, digest: str) -> TaskContractRevision | None:
        """**精确取一条**。这是读这本账唯一的方式。

        没有 `latest()`：分叉时"最新"不是一个有定义的东西，而替调用方在分叉面前
        抽签，正是这本账要消灭的病。
        """
        needle, want = str(task_instance_uuid or ""), str(digest or "")
        if not needle or not want:
            return None
        for r in self.revisions_for(needle):
            if r.digest == want:
                return r
        return None

    def children_of(self, task_instance_uuid: str, parent_digest: str) -> list[TaskContractRevision]:
        """从同一个 parent 长出来的所有 revision —— 分叉在这里看得见。"""
        return [r for r in self.revisions_for(task_instance_uuid)
                if r.parent_revision_digest == str(parent_digest or "")]

    # ── 写 ───────────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _lock(self):
        """读-改-写围成临界区：CAS 要先看"这个 parent 下面有没有兄弟"再落行。

        走 `shared.lib.filelock`（跨进程文件锁的唯一入口）。
        """
        from shared.lib import filelock

        try:
            with filelock.exclusive(self.tasks_dir / ".contract.lock"):
                yield
        except OSError as exc:
            log.debug("task contract lock unavailable: %s", exc)
            yield

    def append(
        self,
        *,
        task_instance_uuid: str,
        objective: str = "",
        intended_use: str = "",
        target_node: str = "",
        prereg_assignment: PreregAssignment | None = None,
        actor: str = "",
        reason: str = "",
        parent_revision_digest: str = "",
        relation: str = "",
        allow_fork: bool = False,
    ) -> TaskContractRevision:
        """追加一条合同 revision。返回它（`digest` 是它的身份）。

        `parent_revision_digest` 为空 = 这是第一条。非空时默认走 **CAS**：那个
        parent 下面已经有 revision 了就拒绝（`allow_fork=True` 时改为两条都留下，
        之后只能按 digest 精确取 —— 绝不悄悄覆盖）。
        """
        uuid = str(task_instance_uuid or "").strip()
        if not uuid:
            raise TaskContractError("合同必须挂在一个任务身份上（task_instance_uuid）")
        if relation and relation not in RELATIONS:
            raise TaskContractError(
                f"relation={relation!r} 不是合法值。合法值：{', '.join(RELATIONS)}")
        with self._lock():
            existing = self.revisions_for(uuid)
            parent = str(parent_revision_digest or "")
            if parent:
                if not any(r.digest == parent for r in existing):
                    raise TaskContractError(
                        f"parent_revision_digest={parent[:12]}… 不在这个任务的合同链上")
                siblings = [r for r in existing if r.parent_revision_digest == parent]
                if siblings and not allow_fork:
                    raise TaskContractError(
                        f"这个 parent 下面已经有 revision "
                        f"{siblings[0].digest[:12]}… 了 —— 并发分叉必须显式声明"
                        "（allow_fork=True），否则会静默覆盖别人的决定")
            elif existing:
                raise TaskContractError(
                    "这个任务已经有合同了：追加新版要带 parent_revision_digest，"
                    "否则读的人分不清两条哪个在前")
            payload = {
                "task_instance_uuid": uuid,
                "revision": len(existing) + 1,
                "parent_revision_digest": parent,
                "objective_digest": (
                    hashlib.sha256(str(objective or "").encode("utf-8")).hexdigest()
                    if objective else ""),
                "intended_use": str(intended_use or ""),
                "target_node": str(target_node or ""),
                "prereg_assignment": (
                    prereg_assignment.as_dict() if prereg_assignment is not None else None),
                "actor": str(actor or ""),
                "reason": str(reason or ""),
                "relation": str(relation or ("supersedes" if parent else "")),
            }
            payload["digest"] = compute_digest(payload)
            payload["at"] = _now_iso()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(_canonical(payload) + "\n")
        return TaskContractRevision(**payload)


def assignment_for_state(state: Any) -> tuple[TaskContractRevision | None, str]:
    """子 run 读**自己这一趟**的合同与 prereg 绑定。

    返回 `(revision, assignment_kind)`；拿不到合同时是 `(None, "pending_assignment")`
    —— 没有合同和"父明确说不绑"是两件事，但对执行者的约束相同：**不许自行认领**。

    这是给节点用的一行入口：派发时身份已经冻在 `state` 上了，节点不必再自己拼
    项目路径、也不该再去扫"此刻项目里有几份 prereg"。
    """
    uuid = str(getattr(state, "task_instance_uuid", "") or "")
    digest = str(getattr(state, "task_contract_digest", "") or "")
    project_root = getattr(state, "project_root", None)
    if not uuid or not digest or project_root is None:
        return None, ASSIGNMENT_PENDING
    revision = TaskContractLog(Path(project_root) / "tasks").get(uuid, digest)
    if revision is None:
        return None, ASSIGNMENT_PENDING
    return revision, revision.assignment_kind
