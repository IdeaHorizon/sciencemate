"""基于 JSON 文件的 artifact + memory 存储。

它替代完整平台里用 Postgres 做的事情。单次 run 的 state 放在
output/{run_id}/ 下。owner 可以直接 `cat` JSON 文件来检查。

目录结构：
  output/{run_id}/
    artifacts/{type}__{slug}.json     —— 每个保存的 artifact 一个文件
    memory.jsonl                       —— 仅追加的 memory 日志
    transcript.jsonl                   —— 仅追加的 LLM / 工具调用日志
    summary.json                       —— 最终 run 结果（status、outputs、…）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core import artifact_provenance as _provenance
from core.tool_errors import ToolRejection
from shared.lib import filelock


from core.paths import (
    org_root as _paths_org_root,
    project_dir as _paths_project_dir,
)


def _project_root(project_id: str | None) -> Path | None:
    """thin wrapper over `core.paths.project_dir` 保持旧调用点兼容。"""
    p = _paths_project_dir(project_id)
    if p is not None:
        p.mkdir(parents=True, exist_ok=True)
        # v3.8：项目级工作区**机械创建**。E2E-3 之前它只是 experiment
        # harness.yaml 提示词里的一句约定 —— 目录从来没被建过，于是实验节点
        # 只能去共享的 <平台>/workspace/tau-bench/ 干活，并在那里读到了 32
        # 小时前另一个项目留下的 trajectory 日志。约定不该是提示词。
        (p / "workspace").mkdir(exist_ok=True)
    return p


def _org_root() -> Path:
    """thin wrapper over `core.paths.org_root`。"""
    p = _paths_org_root()
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── v2.1 entity scope 表 ────────────────────────────────────────────────────
# 哪些 entity 默认存在 org 层（跨项目共享）；其它默认项目层
# 注：skill 不在 KB 里（独立系统，folder + SKILL.md），不放这里

# Phase B (v0.3.2+)：vector index 懒初始化 flag —— 进程内单次 rebuild_if_needed
# 用 dict 而不是 bool 以支持每 scope (org / project) 独立初始化
_SEMANTIC_INDEX_INITIALIZED: dict[str, bool] = {}


def _slug(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    return (s or "x")[:max_len]


def apply_env_tokens_limit(state: "State") -> None:
    """给顶层 state 设 `HARNESS_TOKENS_LIMIT`（>0 时激活 agent_loop 的 v3.1
    熔断：1× 警告、1.5× 硬停）。默认 0=无限。

    v3.2 修复：`State.new()` 一直会读这个 env var，但顶层 orchestrator state
    在 chat.py/run_e2e_dogfood.py 里是**直接调 `State(...)` 构造函数**（走
    `_make_or_load_orchestrator_state`），完全绕开 `State.new()` —— 导致
    orchestrator 自己的 `tokens_limit` 永远是 dataclass 默认值 0（无限），
    子节点又从父节点继承这个 0，整条链路的熔断从未真正激活过
    （v8/v9 dogfood 实测：experiment 烧到 2.36M tokens 都没触发 1.5×900k 硬停）。
    两处构造路径必须都调用本函数，不能只在 `State.new()` 里读一次。
    """
    tl = int(os.environ.get("HARNESS_TOKENS_LIMIT", "0") or 0)
    if tl > 0:
        state.tokens_limit = tl


def apply_env_runtime_capabilities(state: "State") -> None:
    """Compatibility wrapper for direct ``State(...)`` construction paths."""
    from core.runtime_capabilities import apply_env_runtime_capabilities as _apply

    _apply(state)


class StateContractError(ValueError, ToolRejection):
    """State 层**故意**的拒绝：非法取值、冻结产物、状态机不允许的转换。

    继承 ValueError 是为了不动任何 `except ValueError` 的调用方；继承
    ToolRejection 是为了让 dispatch 认得出"这是按设计说不"，而不是把它记成
    `tool_exception`（我们的代码崩了）。

    差别在界面上很大：本机库里 19 条"artifact 已冻结，请带 amendment_reason
    重发"被展示成 `ValueError: …`，读起来像平台出故障 —— 而它其实是预注册
    防篡改语义在正常工作，且那段话已经把下一步说得很清楚了。
    """


@dataclass
class State:
    """每次 run 的 state 目录。把它传给工具 + context_engine。

    持久化分层：
      - run-local（每次 run 一份）：artifacts/ / transcript.jsonl / summary.json
      - project-scoped（如果传 project_id）：memory.jsonl + kb_*.jsonl
        → 跨 run 共享，让 KB 跟 memory 在项目尺度上 **复利**
      - 没传 project_id 时，memory 和 KB 都退回 run-local（教学版默认）
    """
    run_id: str
    node_type: str
    root: Path
    project_id: str | None = None              # 项目 id（None = 不跨 run 持久化）
    project_root: Path | None = None           # 项目持久化目录（derived 自 project_id）
    tenant_id: str | None = None             # 平台运行身份；CLI/local 默认 None
    session_id: str | None = None            # 平台 Session；run-local 状态隔离键
    # App Server freezes these before dispatch. Containers are disposable
    # materializations; child states inherit this capability unchanged.
    platform_attempt_id: str | None = None
    sandbox_manifest: dict[str, Any] | None = None
    sandbox_manifest_hash: str | None = None
    # Platform-owned Git worktree. Nodes only see a normal filesystem cwd;
    # observation/checkpoint authority stays in the framework and App Server.
    project_worktree: Path | None = None
    workspace_root: Path | None = None
    workspace_relative_path: str | None = None
    # 节点在 Git 里的记录目录（= 它自己的节点目录）；由 bind_project_workspace 唯一决定。
    # None = 该节点没有 Git 内记录目录（作用域是单个文件，或没绑 worktree）→ 一切落 run 本地。
    workspace_records_dir: Path | None = None
    # ── 这一趟是**被谁、按哪份合同**派来的（#1080 第 4 条）─────────────────
    #
    # 从前 child State 里一个字段都没有：节点要判"我这趟在做哪件事"，只能去扫
    # "此刻项目里有几份 prereg"。续跑、接管、上游重派这三类跨 run 场景因此都
    # 没有可比较的身份 —— #1052 的翼型会话里，新任务落回了旧 child 的上下文。
    #
    # 四个字段一起进 `run_start` 事件，节点和审计读的是同一份。
    task_instance_uuid: str | None = None
    task_contract_revision: int | None = None
    task_contract_digest: str | None = None
    #: 父 run 里**那一次** run_node 调用的身份（每次派发一个，不复用）。
    parent_dispatch_id: str | None = None
    # Budget tracking（query_budget 工具读这些）
    tokens_used: int = 0                  # 累计 LLM token 消耗
    tokens_limit: int = 0                 # 0 = 无限制（开发阶段默认）；>0 = 软上限，query_budget 提醒
    tool_calls_made: int = 0              # 累计 tool 调用次数
    # read-before-edit 安全约束（read_file / write_file / edit_file 用）
    files_read: set[str] = field(default_factory=set)   # 本次 run 已经 read 过的绝对路径
    # Hook 之间共享 / 跨 turn 保持的小状态（memory_delta / scratchpad 等都用它）
    hook_state: dict[str, Any] = field(default_factory=dict)
    # Process-owner capabilities.  Node inputs and LLM tool arguments cannot
    # mutate this trust boundary.
    runtime_capabilities: frozenset[str] = field(default_factory=frozenset)
    # cancel_node 的即时抢占信号（2026-07-14）：与 hook_state["kill_signal"] 并存——
    # 后者供 agent_loop turn_start 边界检查；这个 Event 供正在阻塞等待子进程的
    # run_bash 类工具立刻响应，不用等 timeout 参数到期。同一 State 对象引用贯穿
    # pause/resume 全程（core/pause.py 注册表按引用存取），故跨暂停恢复依然有效。
    kill_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
    # 白板：run 内跨轮工作状态。**覆盖语义** —— 一块板子，不是一本日志。
    # 语义、容量、渲染全在 core/whiteboard.py（唯一真相源）。
    # 类型从 list[str] 改成 str（2026-08-13）：旧的追加日志涨到 639 条 / 50KB，
    # 每轮全量注入把上下文挤爆，模型连着 591 轮只写笔记不干活。
    scratchpad: str = ""
    scratchpad_revision: int = 0        # 改写次数（进展熔断读它）
    scratchpad_revised_turn: int = 0    # 上次改写发生在第几轮（注入时算年龄）
    # subagent / 递归调度（depth=0 表示 top-level；>0 表示被 run_node 工具调起的子节点）
    depth: int = 0
    sub_run_id: str | None = None          # 父分配的子 run 标签（人类可读）
    parent_run_id: str | None = None       # 父 run_id（用于 transcript 链）

    def __post_init__(self) -> None:
        # root 必须**生而绝对**（issue #426 nidy 现场，2026-08-13）。
        #
        # 相对的 root 不会在这里报错，而是毒化下游每一处路径计算，且毒发时
        # 报错指向假原因：CLI 用相对 `output/` 起 run → experiment 的
        # run-local 默认 run_root 算出来也是相对的 → `_normalize_path` 只认
        # `/`、`~` 开头，返回 None → 默认角色被**静默丢弃** → 这个 run 没有
        # 任何可写角色 → agent 给的（完全正确的）绝对 workdir 匹配不到东西 →
        # "must be inside a declared writable role" → submit_job 全拒 →
        # 与 safe_run_bash 长任务门互锁 → 6 连败 → 熔断锁死节点。
        # 第一环和最后一环隔着五层，中间没有一层报出真名。
        #
        # 修在 dataclass 自己身上而不是 State.new：chat.py 等处有绕过工厂的
        # 直接构造，堵工厂堵不住全部入口。
        self.root = Path(self.root).resolve(strict=False)

    @property
    def transcript_path(self) -> Path:
        return self.root / "transcript.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.json"

    @classmethod
    def new(cls, node_type: str, base_dir: Path,
            project_id: str | None = None, *,
            tenant_id: str | None = None,
            session_id: str | None = None,
            project_worktree: Path | None = None,
            task_instance_uuid: str | None = None,
            task_contract_revision: int | None = None,
            task_contract_digest: str | None = None,
            parent_dispatch_id: str | None = None) -> State:
        run_id = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        root = base_dir / run_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "artifacts").mkdir(exist_ok=True)
        project_root = _project_root(project_id)
        inst = cls(
            run_id=run_id, node_type=node_type, root=root,
            tenant_id=tenant_id, project_id=project_id,
            session_id=session_id, project_root=project_root,
            task_instance_uuid=task_instance_uuid,
            task_contract_revision=task_contract_revision,
            task_contract_digest=task_contract_digest,
            parent_dispatch_id=parent_dispatch_id,
        )
        apply_env_tokens_limit(inst)
        apply_env_runtime_capabilities(inst)
        if project_worktree is not None:
            from core.project_workspace import bind_project_workspace

            bind_project_workspace(inst, project_worktree)
        return inst

    @classmethod
    def reopen(cls, node_type: str, base_dir: Path, run_id: str,
               project_id: str | None = None, *,
               tenant_id: str | None = None,
               session_id: str | None = None,
               project_worktree: Path | None = None,
               task_instance_uuid: str | None = None,
               task_contract_revision: int | None = None,
               task_contract_digest: str | None = None,
               parent_dispatch_id: str | None = None) -> State:
        """重新打开一个**已存在**的 run 目录 —— 死亡续跑用。

        与 `new` 的唯一区别是 run_id 不新生成：transcript 继续追加、产物目录
        原地复用、checkpoint 就在旁边。一次被打断的 run 续跑之后仍然是**同一
        个 run**，而不是"又开了一个"（wangd 2026-08-18：「上一个 curator 被
        打断，然后就新开一个 curator，这非常离谱」）。
        """
        root = base_dir / run_id
        if not root.is_dir():
            raise FileNotFoundError(f"run 目录不存在，无法续跑：{root}")
        (root / "artifacts").mkdir(exist_ok=True)
        project_root = _project_root(project_id)
        inst = cls(
            run_id=run_id, node_type=node_type, root=root,
            tenant_id=tenant_id, project_id=project_id,
            session_id=session_id, project_root=project_root,
            task_instance_uuid=task_instance_uuid,
            task_contract_revision=task_contract_revision,
            task_contract_digest=task_contract_digest,
            parent_dispatch_id=parent_dispatch_id,
        )
        apply_env_tokens_limit(inst)
        apply_env_runtime_capabilities(inst)
        if project_worktree is not None:
            from core.project_workspace import bind_project_workspace

            bind_project_workspace(inst, project_worktree)
        return inst

    # ── Artifacts ────────────────────────────────────────────────────────────
    # ── 研究记录：原生文件 + 账本（core/ledger，RFC 2026-09-12 §6）────────────
    #
    # 2026-09-12 之前每份产物是一个 JSON 信封落在 `<节点>/artifacts/`，冻结原地改写
    # 文件，旧版进 `.versions/`。现在正文就是文件（`plan/pre_registration__Q1.md`），
    # 框架事实进账本；冻结是账本上的一行，版本历史归 git。消费方拿到的 record
    # 形状不变（type / name / content / metadata / created_at / provenance /
    # produced_by_* / version / content_hash），只是没有人再需要拆信封。

    @property
    def memory_path(self) -> Path:
        """有 project_id 时项目级持久化，否则 run-local。"""
        if self.project_root:
            return self.project_root / "memory.jsonl"
        return self.root / "memory.jsonl"

    @property
    def records_dir(self) -> Path:
        """本节点的记录落在哪个目录：绑了 worktree 是它自己的节点目录，否则 run 本地。"""
        target = getattr(self, "workspace_records_dir", None)
        if target is not None:
            target = Path(target)
            target.mkdir(parents=True, exist_ok=True)
            return target
        target = self.root / "artifacts"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _run_store(self) -> "RecordStore":
        """run 本地账本：随 run 生灭。装运行时记录（run_local 类型）和没绑
        worktree 时的一切记录。"""
        from core.ledger import RecordStore

        return RecordStore(self.root / "artifacts", self.root / "records.jsonl",
                           snapshot_dir=self.root / "versions")

    def _worktree_store(self) -> "RecordStore | None":
        worktree = getattr(self, "project_worktree", None)
        if not worktree or getattr(self, "workspace_records_dir", None) is None:
            return None
        from core.ledger import workspace_store

        return workspace_store(worktree, snapshot_dir=self.root / "versions")

    def _stores(self) -> "list[RecordStore]":
        """读的顺序：工作区账本在前（协作总线），run 本地在后。"""
        out = []
        worktree = self._worktree_store()
        if worktree is not None:
            out.append(worktree)
        out.append(self._run_store())
        return out

    def _store_for(self, artifact_type: str) -> "tuple[RecordStore, Path]":
        """这一类记录该进哪本账、落哪个目录。

        判据只问策略表一位（`artifact_policy.run_local`）：运行时记录（压缩摘要、
        资源画像…）进 run 本地账；其余进工作区账、落本节点目录。没绑 worktree
        （CLI / fixture）或文件作用域的节点（curator 只拥有 MEMORY.md）一律 run 本地。
        """
        from shared.lib.artifact_policy import is_run_local

        worktree = self._worktree_store()
        if worktree is None or is_run_local(artifact_type):
            return self._run_store(), self.root / "artifacts"
        return worktree, Path(self.workspace_records_dir)  # type: ignore[arg-type]

    def artifact_head(self, artifact_id: str) -> "Head | None":
        """账本上的当前状态（路径、版本、冻结…）；不读正文。"""
        for store in self._stores():
            head = store.head(artifact_id)
            if head is not None:
                return head
        return None

    def save_artifact(self, artifact_type: str, name: str, content: str,
                      metadata: dict | None = None,
                      provenance: dict | None = None,
                      amendment_reason: str | None = None,
                      _write_capability: object | None = None) -> dict:
        """provenance=None → 本 run 自己产的（默认，行为同以前）。

        转发/回填/导入的调用方**必须**传 —— 否则接收方会被记成产出方，
        外部导入的材料转发一次就被洗成平台自产。见 core/artifact_provenance。
        """
        if metadata is None:
            metadata = {}
        elif not isinstance(metadata, dict):
            raise TypeError("artifact metadata must be a dict or None")

        # Approval- and provenance-bearing artifacts are credentials, not
        # arbitrary documents.  Enforce lifecycle-tool ownership at the final
        # persistence boundary so internal callers cannot bypass it.
        from core.artifact_capabilities import validate_artifact_write

        validate_artifact_write(
            node_type=self.node_type,
            artifact_type=artifact_type,
            capability=_write_capability,
            provenance=provenance,
        )

        from core import ledger as _ledger

        artifact_id = f"{artifact_type}__{_slug(name)}"
        store, directory = self._store_for(artifact_type)
        existing = store.head(artifact_id)
        amendment: dict | None = None
        preserved_provenance: dict | None = None
        if existing is not None and existing.frozen and existing.frozen_version == existing.version:
            reason = (amendment_reason or "").strip()
            if not reason:
                # 冻结版的修订必须公开、带理由 —— 但必须**有路可走**。
                # 旧文案同时禁止覆盖和换名，模型被逼换名，身份就碎了。
                raise StateContractError(
                    f"artifact {artifact_id!r} 的 v{existing.version} 已冻结。"
                    f"要修订它：**带 amendment_reason 重发同一个调用**"
                    f"（同 type 同 name）。框架会保留 v{existing.version}（git 历史里）、"
                    f"记录逐字段差异，并把修订稿存为 v{existing.version + 1}（未冻结）；"
                    f"新版本要治理新的实验，必须重新 freeze_artifact。不要换名另存 —— "
                    f"那会制造一个平行身份，下游将无法判断哪份有效。"
                )
            old = store.record(artifact_id) or {}
            diff = _ledger.compute_amendment_diff(old, content, metadata)
            # 修订**不是**产出：本 run 改了别人的证据，不等于本 run 产出了这份证据
            # （2026-09-01 实拍：一趟只修订上游账本的 run 把 experiment_log 的
            # produced_by_run_id 改写成自己，真做实验那趟名下归零）。修订者记在
            # amendment 里，产出方保留。
            preserved_provenance = existing.provenance or None
            amendment = {
                "from_version": existing.version,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
                "by_node_type": self.node_type,
                "by_run_id": self.run_id,
                "diff": diff,
            }
            try:
                self.append_transcript(
                    "artifact_amended", artifact_id=artifact_id,
                    from_version=existing.version, to_version=existing.version + 1,
                    reason=reason[:300],
                    changed_metadata_keys=diff.get("changed_metadata_keys"),
                    content_changed=diff.get("content_changed"),
                )
            except OSError:
                pass
        elif existing is not None:
            # 写不跨节点：别的节点目录里的记录，本节点不能当成自己的覆盖掉
            # （issue #621 的形状：协调者代笔一份同名 manuscript，此前落在它自己的
            # 目录里成为平行身份；一个工作区一本账之后同 id 就是同一份记录，
            # 覆盖等于改写 writing 的文件）。修订冻结版走上面的 amendment 通道，
            # 那是公开、带理由、记修订者的动作；这里拒的是静默的普通覆盖。
            owner_dir = store.abs_path(existing).parent.resolve()
            if owner_dir != Path(directory).resolve():
                raise StateContractError(
                    f"artifact {artifact_id!r} 是别的节点的记录"
                    f"（{existing.produced_by_node_type or '?'} 写在 {existing.path}），"
                    f"本节点（{self.node_type}）不能覆盖别的节点目录里的记录。"
                    f"要在它基础上改：由产出方修订，或对冻结版带 amendment_reason；"
                    f"要另起一份自己的：换一个 name，它会落在本节点自己的目录里、"
                    f"盖本节点的章。"
                )
            # 普通覆盖：账本记版本，run 内快照留给 /undo（随 run 生灭）。
            self.hook_state["_last_artifact_overwrite"] = {
                "artifact_id": artifact_id,
                "version": existing.version,
                "at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"),
            }
        # 产出方身份（issue #202）：调用方显式传的 > 修订时保留的原产出方 > 本 run。
        prov = (provenance if isinstance(provenance, dict)
                else preserved_provenance if preserved_provenance is not None
                else _provenance.produced(self.node_type, self.run_id))
        _by_node, _by_run = _provenance.true_producer({"provenance": prov})
        record = store.save(
            artifact_id=artifact_id, artifact_type=artifact_type, name=name,
            content=content, metadata=metadata, directory=directory,
            created_at=self._next_artifact_timestamp(), provenance=prov,
            produced_by_node_type=_by_node, produced_by_run_id=_by_run,
            by_node=self.node_type, by_run=self.run_id, amendment=amendment,
        )
        head = store.head(artifact_id)
        path = store.abs_path(head) if head is not None else directory / artifact_id
        return {"id": artifact_id, "path": self._artifact_display_path(path),
                "version": int(record.get("version") or 1)}

    def mark_frozen(self, artifact_id: str, metadata_patch: dict | None = None,
                    *, frozen_at: str | None = None) -> dict:
        """冻结 head：账本一行，文件一个字节不动。返回冻结后的 record。

        文件哈希 = 正文哈希 = 账本钉死的那个哈希；此前冻结要原地改写文件，
        账本钉的是改写后的文件而 content_hash 只描述正文，两个哈希天然分叉。
        `frozen_at` 只给转发场景用（沿用上游冻结的时刻），默认取现在。
        """
        for store in self._stores():
            if store.head(artifact_id) is not None:
                return store.freeze(artifact_id, metadata_patch=dict(metadata_patch or {}),
                                    by_node=self.node_type, by_run=self.run_id,
                                    frozen_at=frozen_at)
        raise KeyError(artifact_id)

    def retire_artifact(self, artifact_id: str, reason: str) -> bool:
        """撤下一个未冻结的身份（文件删掉、账本留行）。冻结的拒绝。"""
        for store in self._stores():
            if store.head(artifact_id) is not None:
                return store.retire(artifact_id, reason=reason,
                                    by_node=self.node_type, by_run=self.run_id)
        return False

    def undo_last_overwrite(self) -> dict | None:
        """撤销最近一次覆盖：把 run 内快照的正文作为**新一版**写回（留痕，不删账）。"""
        rec = self.hook_state.get("_last_artifact_overwrite")
        if not rec:
            return None
        artifact_id = str(rec.get("artifact_id") or "")
        head = self.artifact_head(artifact_id)
        if head is None:
            return {"status": "error", "error": f"找不到 {artifact_id}"}
        if head.frozen and head.frozen_version == head.version:
            return {"status": "error", "error": f"{artifact_id} 当前已冻结 —— 冻结产物不可回滚。"}
        versions = self.artifact_versions(artifact_id)
        wanted = int(rec.get("version") or 0)
        previous = next((v for v in versions if int(v.get("version") or 0) == wanted), None)
        if previous is None or previous.get("content") is None:
            return {"status": "error", "error": f"v{wanted} 的快照已不存在"}
        restored = self.save_artifact(head.artifact_type, head.name, str(previous.get("content") or ""),
                                      metadata=dict(previous.get("metadata") or {}))
        self.hook_state.pop("_last_artifact_overwrite", None)
        self.append_transcript("artifact_undo", artifact_id=artifact_id,
                               restored_version=wanted, as_version=restored.get("version"))
        return {"status": "success", "artifact_id": artifact_id,
                "restored_version": wanted, "version": restored.get("version")}

    def artifact_versions(self, artifact_id: str) -> list[dict]:
        """一个身份的全部已知版本，版本号升序（末位 = head）。旧版正文来自
        run 内快照或 git 历史；找不到时 content 为空但 content_hash 永远在。"""
        for store in self._stores():
            if store.head(artifact_id) is not None:
                return store.versions(artifact_id)
        return []

    def latest_frozen_artifact(self, artifact_id: str) -> dict | None:
        """这个身份最新的**冻结**版本。head 若冻结就是它自己；从未冻结 → None。

        修订期间（head 是未冻结草稿）这里返回的仍是上一个冻结版 —— 但**治理
        新 run 必须 fail-loud 而不是静默用它**（见 experiment 的
        load_run_contract）。
        """
        for store in self._stores():
            if store.head(artifact_id) is not None:
                return store.latest_frozen(artifact_id)
        return None

    def _artifact_display_path(self, path: Path) -> str:
        """产物的可读相对路径 —— 按它真正的锚点算，不按固定层数猜。"""
        worktree = getattr(self, "project_worktree", None)
        if worktree:
            try:
                return str(Path(path).resolve().relative_to(Path(worktree).resolve()))
            except ValueError:
                pass
        try:
            return str(Path(path).relative_to(self.root.parent.parent))
        except ValueError:
            return str(path)

    def read_artifact(self, artifact_id: str) -> dict | None:
        for store in self._stores():
            record = store.record(artifact_id)
            if record is not None:
                return record
        return None

    def find_artifact_path(self, artifact_id: str) -> Path | None:
        """artifact 正文文件的落盘路径。

        大产物不该整份进上下文 —— 调用方需要能把**文件**交给 execute_python 去
        算。搜索顺序与 `read_artifact` 完全一致："读到的是哪一份"和"路径指向
        哪一份"必须是同一个答案。
        """
        for store in self._stores():
            head = store.head(artifact_id)
            if head is not None:
                return store.abs_path(head)
        return None

    def list_artifacts(self, artifact_type: str | None = None,
                       own_only: bool = False) -> list[dict]:
        """own_only=True 只看本节点自己的产出（按账本上的产出方，不按目录）。

        跨节点读是给**协作**用的（下游取料、orchestrator 看全局）。但"本 run
        产出了什么"是**归属**问题 —— executor 的 produced_types、summary、
        交付判定都必须用 own_only，否则上游节点的产物会被算成本 run 的产出。

        **返回顺序是契约的一部分：按 created_at 升序，末位 = 最新。**
        全仓 7 处调用方无一例外地写 `artifacts[-1]` 表示"最新的那份"。

        运行时记录（run_local 类型）不是"产出"：只有指名要它的调用方才看得到。
        不滤的话它们会混进"可用的上游 artifact"提示、produced_types、引用完整性
        的新 id 清单。
        """
        from shared.lib.artifact_policy import is_run_local

        seen: set[str] = set()
        rows: list[tuple[tuple[str, str], dict]] = []
        for store in self._stores():
            for head in store.heads().values():
                if head.artifact_id in seen:
                    continue
                if artifact_type and head.artifact_type != artifact_type:
                    continue
                if not artifact_type and is_run_local(head.artifact_type):
                    continue
                if own_only and head.produced_by_node_type != self.node_type:
                    continue
                seen.add(head.artifact_id)
                entry = {
                    "id": head.artifact_id,
                    "type": head.artifact_type or "(unknown)",
                    "name": head.name or head.artifact_id,
                    "owner_node": head.produced_by_node_type,
                }
                rows.append(((head.created_at or "", head.artifact_id), entry))
        rows.sort(key=lambda item: item[0])
        return [entry for _, entry in rows]

    def _next_artifact_timestamp(self) -> str:
        """产物的登记时刻 —— 在本 State 内**严格单调**，不指望时钟粒度。

        为什么不能直接用 `datetime.now()`（2026-08-22 CI 实测）：
        `test_auto_resolve_picks_latest_on_ambiguity` 用 `time.sleep(0.01)` 制造
        "一先一后"，容器里时钟粒度一粗，两次 now() 就返回同一个字符串。而时间戳
        并列时"哪个更新"这个信息**根本不存在** —— `_artifact_order_key` 的第二元
        （文件名）只让顺序**确定**，不让它**正确**：字典序里 `h_new` < `h_old`，
        后写入的反而被判成更旧。表现是 CI 偶发红，真实语义是"选最新"会选错。

        时序是**写入方的事实**，不是时钟的事实。同一个 State 连续登记两份产物，
        先后顺序它自己完全知道，没有理由去问一个精度不确定的时钟。这与
        「序号计数器必须由库发」是同一条：能由写入方保证的单调，别指望外部来源。

        跨 State 实例（跨进程、跨 run）退化为普通 now() —— 那种场景下两次写入
        之间隔着调度和 LLM 调用，并列的概率可以忽略。
        """
        now = datetime.now(timezone.utc)
        last = getattr(self, "_last_artifact_ts", None)
        if last is not None and now <= last:
            now = last + timedelta(microseconds=1)
        self._last_artifact_ts = now
        return now.isoformat()

    # ── Memory v2.1（wet ledger）──────────────────────────────────────────
    #
    # 重新设计：4 kinds + lifecycle + 内容寻址 id
    #
    # kind:
    #   directive   - 运行时 user/agent 给的临时指令（可能升 PROFILE/PROJECT）
    #   observation - agent 注意到但未确认的现象（可能升 KB claim）
    #   decision    - agent 自己的方法学选择（可能升 KB decision）
    #   todo        - 给未来 self/其它节点的 reminder
    #
    # lifecycle status: active | promoted | archived | superseded
    #
    # 物理：内容寻址 id（防重复），append-only + rewrite-on-update。

    def save_memory(self, kind: str, text: str,
                     tags: list[str] | None = None,
                     applies_to_node: str | None = None,
                     expires_at: str | None = None) -> dict:
        """写一条 memory。返回 (record, created)。

        kind ∈ {directive, observation, decision, todo}
        """
        import hashlib
        from shared.lib.kb_schema import normalize

        if kind not in ("directive", "observation", "decision", "todo"):
            raise StateContractError(f"kind 必须 ∈ directive/observation/decision/todo，得到 {kind!r}")
        if not text or not text.strip():
            # 判决拆除（verdicts_core state:725）：空 memory 不写=无损，如实返回。
            return {"id": None, "created": False, "noop": "empty_text"}

        # 内容寻址 id：sha8(kind + normalize(text))
        sig = f"{kind}|{normalize(text)}"
        mem_id = "mem_" + hashlib.sha256(sig.encode("utf-8")).hexdigest()[:8]
        now = datetime.now(timezone.utc).isoformat()

        # 读 + 检查重复（同 id 不重复写）
        existing = self.list_memory()
        for r in existing:
            if r.get("id") == mem_id:
                # 同内容已存在：更新 last_referenced_at（lightweight refresh）
                return r

        record = {
            "id": mem_id,
            "kind": kind,
            "text": text,
            "tags": tags or [],
            "applies_to_node": applies_to_node,
            "created_at": now,
            "created_by_run_id": self.run_id,
            "created_by_node_type": self.node_type,
            "status": "active",
            "expires_at": expires_at,
            "last_referenced_at": now,
        }
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        with self.memory_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def list_memory(self) -> list[dict]:
        """读全部 memory 记录（含已 archived / promoted）。"""
        if not self.memory_path.exists():
            return []
        out: list[dict] = []
        for line in self.memory_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def search_memory(self, query: str = "",
                      kind: str | None = None,
                      status: str = "active",
                      tags: list[str] | None = None,
                      applies_to_node: str | None = None,
                      limit: int = 20) -> list[dict]:
        """检索 memory。默认只返 active（promoted/archived 默认隐藏）。

        kind: 过滤 kind（directive/observation/decision/todo）
        status: 'active' / 'promoted' / 'archived' / 'all'
        """
        recs = self.list_memory()

        if status != "all":
            recs = [r for r in recs if r.get("status", "active") == status]
        if kind:
            recs = [r for r in recs if r.get("kind") == kind]
        if applies_to_node:
            # 匹配 applies_to_node = None（适用所有节点）或精确等于
            recs = [r for r in recs
                     if r.get("applies_to_node") in (None, applies_to_node)]

        q = (query or "").lower()
        if q:
            recs = [r for r in recs if q in (r.get("text") or "").lower()]

        want_tags = set(tags or [])
        if want_tags:
            recs = [r for r in recs
                     if want_tags & set(r.get("tags") or [])]

        return recs[:limit]

    def get_memory(self, mem_id: str) -> dict | None:
        for r in self.list_memory():
            if r.get("id") == mem_id:
                return r
        return None

    # ── Transcript ──────────────────────────────────────────────────────────
    #: 这一次提交（用户的一句话 / 一次插话）的身份。**每条 transcript 事件都盖**
    #: —— 见 `append_transcript`。
    #:
    #: RFC 异步运行时 P1-5（借鉴 Codex SQ/EQ，附录 S1）：「事件端到端带
    #: submission id，回答锚回提问」。
    #:
    #: 为什么要它：UI 要回答「这条输出是在回应我哪句话」。此前每种事件各自手工
    #: 带一个锚字段（`replies_to_message_id` / `message_id` / …），谁忘了带谁就
    #: 渲染错位 —— 实测修过两次同款（决策呈递把回答渲染在提问上面、右栏箭头跳到
    #: 最后一张同类卡）。逐个补是名单式护栏，新事件默认漏。
    #:
    #: 盖在 `append_transcript` 这个**唯一出口**上，一处盖全。
    submission_id: str = ""

    @property
    def planned_stop_policy_id(self) -> str | None:
        """本 run 是否被**派发方**授权中途停止作业；没授权就是 None（#1084 第二节）。

        授权是 run 级的（「这个任务允许中途停下作业」），由派发方在子 run 开工前
        给出、Core 冻住，子 run 只能引用。执行者不能自己给自己发授权 —— 那正是
        「引文逐字出自任务正文」这条判据挡不住的东西。真相源在
        `core/stop_authorization.py`，这里只是子 run 读得到的那个入口。
        """
        from .stop_authorization import planned_stop_policy_id

        return planned_stop_policy_id(self)

    def append_transcript(self, event_type: str, **payload: Any) -> None:
        record = {
            "event": event_type,
            "at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        if self.tenant_id:
            record["tenant_id"] = self.tenant_id
        if self.session_id:
            record["session_id"] = self.session_id
        # 不覆盖调用方显式给的值：子节点 run 可能在转述父级的提交。
        if self.submission_id and "submission_id" not in record:
            record["submission_id"] = self.submission_id
        # run 目录此前由 artifacts_dir 属性顺手建出来；现在没有那个副作用，
        # 记录事件的一方自己保证目录在。
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # ── Summary ─────────────────────────────────────────────────────────────
    def write_summary(self, summary: dict) -> None:
        if self.tenant_id:
            summary.setdefault("tenant_id", self.tenant_id)
        if self.session_id:
            summary.setdefault("session_id", self.session_id)
        self.summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    # ── KB JSONL ───────────────────────────────────────────────────────────
    #
    # 设计原则：
    #   1. 内容寻址 ID：id = "<entity>_<sha8(normalized_signature)>"
    #      → 同样的输入永远产生同一 id；并发写不会双插。
    #   2. Upsert 语义：write_kb(entity, record) 若 id 已存在则按 schema 合并
    #   3. 文件锁：跨进程并发安全（shared.lib.filelock，POSIX 与 Windows 都真锁）。
    #   4. 重写文件：upsert 触发 read-merge-write，原子 rename。
    #      O(N) 每次写，几千条以下 fine。要更快换真 DB。
    #
    # 物理布局（v3，唯一存活版本）：
    #   ~/.harness-framework/org/kb_{concepts,claims,experiments,chunks}.jsonl
    #   <project_root>/kb_{concepts,claims,experiments,chunks}.jsonl
    #
    # 每条 record 带 `scope` 字段（org/project），写入路径由 scope 决定。
    # 4 个 entity：concepts/claims/experiments/chunks。
    # 旧 synthesis/hypothesis/question/opportunity/decision/failure 全合 claim
    # 的 claim_type 字段（10 个 claim_type）。

    def _read_kb_records(self, path: Path) -> list[dict]:
        """KB 读取的唯一咽喉 —— 旧 claim_type 在这里归一。

        类型从 10 收敛到 5（2026-08-21）。按仓库惯例不写迁移器，但旧盘上的
        `theoretical` / `conjecture` / `replication` 等值必须读得进来，否则
        它们对晋升管线**静默隐身**：`KIND_BY_CLAIM_TYPE` 查不到这些键，
        老 claim 就再也不会出现在候选清单里 —— 不报错，只是消失。

        归一只在读取端；写入端仍然拒收旧名（见 test_legacy_types_are_refused_on_write）。
        接在这一层而不是各调用方：读者有十几个，逐个接就是十几份会各自演化的抄件。
        """
        if not path.exists():
            return []
        from shared.lib.kb_schema import normalize_claim_type

        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("claim_type"):
                legacy = str(rec["claim_type"])
                current = normalize_claim_type(legacy)
                if current != legacy:
                    rec["claim_type"] = current
                    # 留痕：归一是解读，不是事实。原值保留，免得日后想追
                    # 「这条当年是按什么类型写的」时无从查起。
                    rec.setdefault("legacy_claim_type", legacy)
            out.append(rec)
        return out

    def _write_kb_records(self, path: Path, records: list[dict]) -> None:
        """原子写：先写 .tmp，再 rename。锁应该在调用前持有。"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(path)

    # ────────────────────────────────────────────────────────────────────────
    # KB v3 API：按 record.scope 路由
    # ────────────────────────────────────────────────────────────────────────
    #
    # v3 物理布局：
    #   ~/.harness-framework/org/kb_{concepts,claims,experiments,chunks}.jsonl
    #   <project_root>/kb_{concepts,claims,experiments,chunks}.jsonl
    #
    # 每条 record 带 `scope` 字段（org/project），写入路径由 scope 决定（不再按
    # entity_type 一刀切）。v3 只有 4 个 entity；旧 synthesis/hypothesis/... 都
    # 合并进 claim（按 claim_type 字段区分）。

    _KB_ENTITIES = ("concepts", "claims", "experiments", "chunks")

    def _kb_path(self, entity: str, scope: str) -> Path:
        if entity not in self._KB_ENTITIES:
            raise StateContractError(f"v3 only supports {self._KB_ENTITIES}, got {entity!r}")
        if scope == "org":
            return _org_root() / f"kb_{entity}.jsonl"
        if scope == "project":
            base = self.project_root if self.project_root else self.root
            return base / f"kb_{entity}.jsonl"
        raise StateContractError(f"scope must be 'org' or 'project', got {scope!r}")

    def _kb_read_paths(self, entity: str) -> list[Path]:
        """v3 读时扫两层（project 优先，org 兜底）。"""
        return [
            self._kb_path(entity, "project"),
            self._kb_path(entity, "org"),
        ]

    def _find_path_for_kb_id(self, entity: str, kb_id: str) -> Path | None:
        for path in self._kb_read_paths(entity):
            if not path.exists():
                continue
            for r in self._read_kb_records(path):
                if r.get("id") == kb_id:
                    return path
        return None

    def write_kb(self, entity: str, record: dict,
                     *, curator_run_id: str | None = None,
                     skip_semantic_dedup: bool = False) -> tuple[dict, bool]:
        """v3 KB 写入：按 record.scope 路由，自动校验 + 填默认 + merge upsert。

        record 必须含 scope（或调用前用 smart_default_scope 填）。
        校验失败 raise kb_schema.SchemaValidationError。
        返回 (final_record, created)。

        Phase B (v0.3.2+)：写前对 claim / concept 做 semantic dedup check：
          - cosine ≥ 0.95 + sources 真独立 → 自动 merge sources 到已有 entity（不写新）
          - 0.80 ≤ cosine < 0.95 → 写新，但 record.derived.dedup_info 含 similar_candidates 警告
          - cosine < 0.80 → 当成真新

        `skip_semantic_dedup=True` 跳过（curator revert / 内部 helper 用）。
        """
        from shared.lib.kb_schema import (
            compute_kb_id, fill_defaults, merge_kb_record, validate_record,
            smart_default_scope,
        )

        now = datetime.now(timezone.utc).isoformat()

        # ★ v3：自动填 created_by_user_id（防忘 + 团队共享必备）
        if "created_by_user_id" not in record:
            try:
                from core.identity import current_user_id
                record["created_by_user_id"] = current_user_id()
            except Exception:
                record["created_by_user_id"] = "anonymous"

        # scope 智能默认
        if "scope" not in record:
            record["scope"] = smart_default_scope(entity, record)

        # 算 id（内容寻址）
        kb_id = compute_kb_id(entity, record)
        record = {**record, "id": kb_id}

        # fill defaults
        record = fill_defaults(entity, record, now=now)

        # ── Phase B: semantic dedup（写之前查近邻）──────────────────────────
        dedup_info: dict | None = None
        # RFC 2026-08-18：结构化身份的 hypothesis claim **不进语义去重**。
        # 它的身份是 (预注册身份, 问题 id)，机械精确 —— 语义近邻在这里只有两种
        # 可能：同 id（上面 upsert 已处理成修订）或真的不同问题（不该 merge）。
        # 旧行为在 0.95 命中时静默 merge、只并 sources，把修订的全部实质
        # （falsification_criteria 等）丢掉（#395-2 的直接凶手）。
        structural_claim = (
            entity == "claims"
            and record.get("claim_type") == "hypothesis"
            and (record.get("hypothesis_id") or "").strip()
            and (record.get("prereg_artifact_id") or "").strip()
        )
        if not skip_semantic_dedup and entity in ("claims", "concepts") \
                and not structural_claim:
            dedup_info = self._semantic_dedup_check(entity, record)
            if dedup_info and dedup_info.get("action") == "merged":
                # 命中高 sim + 真独立 → 不写新，merge 到已有
                target_id = dedup_info["merged_into_id"]
                merged = self._semantic_merge_into(
                    entity, record, target_id, dedup_info, now=now,
                    curator_run_id=curator_run_id,
                )
                return merged, False
            # 0.80-0.95 / 无命中：继续正常写入；dedup_info 进 record.derived
            if dedup_info:
                record.setdefault("derived", {})
                record["derived"]["dedup_info"] = dedup_info

        # 校验（hypothesis 强校验 + Phase C 高阶门槛等）
        validate_record(entity, record)

        if curator_run_id:
            record["created_by_curator_run_id"] = curator_run_id

        # 找已存在记录（可能跨 scope；以已存在的为准）
        target_path = self._find_path_for_kb_id(entity, kb_id)
        if target_path is None:
            target_path = self._kb_path(entity, record["scope"])
        target_path.parent.mkdir(parents=True, exist_ok=True)

        created = False
        with _kb_file_lock(target_path):
            existing = self._read_kb_records(target_path)
            existing_by_id = {r.get("id"): (idx, r) for idx, r in enumerate(existing)}

            if kb_id in existing_by_id:
                idx, old = existing_by_id[kb_id]
                merged = merge_kb_record(entity, old, record, now=now)
                existing[idx] = merged
                self._write_kb_records(target_path, existing)
                final = merged
            else:
                record["updated_at"] = now
                existing.append(record)
                self._write_kb_records(target_path, existing)
                final = record
                created = True

        # 写入成功后追加 embedding 到 vector index（best-effort，失败仅 log）
        if entity in ("claims", "concepts") and not skip_semantic_dedup:
            try:
                self._append_embedding_to_index(entity, final)
            except Exception as e:
                import logging as _l
                _l.getLogger("state.kb").warning(
                    "append embedding for %s failed: %s（写 jsonl 成功，索引落后）",
                    final.get("id"), e,
                )

        # Phase F: dreaming scheduler hook（仅 created/merged 的 claim & concept）
        try:
            from core.dreaming_scheduler import on_kb_write
            on_kb_write(self.project_id, entity=entity, record=final)
        except Exception as e:
            import logging as _l
            _l.getLogger("state.kb").debug(
                "dreaming_scheduler.on_kb_write failed: %s", e,
            )

        return final, created

    def supersede_older_chunks(self, origin_artifact_id: str, *,
                               new_chunk_id: str, new_version: int) -> list[str]:
        """同一 artifact 身份的旧版 chunk 全部打 superseded_by（RFC 2026-08-18）。

        谱系由框架闭合，模型零参与。行永不删；已 superseded 的不重打。
        """
        now = datetime.now(timezone.utc).isoformat()
        superseded: list[str] = []
        for rec in self.list_kb("chunks"):
            if rec.get("id") == new_chunk_id:
                continue
            if str(rec.get("origin_artifact_id") or "") != str(origin_artifact_id):
                continue
            if rec.get("superseded_by_chunk_id"):
                continue
            try:
                old_version = int(rec.get("origin_artifact_version") or 1)
            except (TypeError, ValueError):
                old_version = 1
            if old_version >= int(new_version):
                continue
            target_path = self._find_path_for_kb_id("chunks", rec["id"])
            if target_path is None:
                continue
            with _kb_file_lock(target_path):
                records = self._read_kb_records(target_path)
                for i, r in enumerate(records):
                    if r.get("id") != rec["id"]:
                        continue
                    r["superseded_by_chunk_id"] = new_chunk_id
                    r["superseded_at"] = now
                    r["updated_at"] = now
                    records[i] = r
                    superseded.append(rec["id"])
                    break
                self._write_kb_records(target_path, records)
        return superseded

    # ── Phase B helpers: semantic dedup + 索引同步 ──────────────────────────

    def _semantic_dedup_check(self, entity: str, record: dict) -> dict | None:
        """查 top-K 语义相似的同 entity_type record。

        返 dict 描述结果：
          {"action": "merged", "merged_into_id": "...", "cosine": 0.97,
            "new_sources": [...]}  —— sim ≥ 0.95 + sources 独立
          {"action": "warn_similar", "candidates": [(id, cosine), ...]}  —— 0.80-0.95
          None —— 无足够近邻 / 索引未初始化 / embedding 失败 / 显式禁用

        env: HARNESS_DISABLE_SEMANTIC_DEDUP=1 → 测试 / CI 跳过 embed 模型加载。
        """
        if os.getenv("HARNESS_DISABLE_SEMANTIC_DEDUP") == "1":
            return None
        try:
            from core.embeddings import get_default_embedding_client
            from core.kb_embedding_text import (
                claim_to_embed_text, concept_to_embed_text,
            )
            from core.kb_vector_index import (
                query_across_scopes, rebuild_if_needed,
            )
            client = get_default_embedding_client()
        except Exception as e:
            import logging as _l
            _l.getLogger("state.kb").debug(
                "semantic dedup unavailable: %s", e,
            )
            return None

        # 懒初始化：首次调时确保索引跟当前 model/template signature 一致
        try:
            scope_for_init = "project" if self.project_id else "org"
            if not _SEMANTIC_INDEX_INITIALIZED.get(scope_for_init):
                rebuild_if_needed("org", client=client)
                if self.project_id:
                    rebuild_if_needed("project", self.project_id, client=client)
                _SEMANTIC_INDEX_INITIALIZED[scope_for_init] = True
        except Exception as e:
            import logging as _l
            _l.getLogger("state.kb").warning(
                "vector index init failed: %s", e,
            )
            return None

        # 拼 embed text
        try:
            if entity == "claims":
                text = claim_to_embed_text(
                    record,
                    concept_lookup=self._concept_lookup_for_embed,
                    claim_lookup=self._claim_lookup_for_embed,
                )
            else:
                text = concept_to_embed_text(record)
            vec = client.embed([text])[0]
        except Exception as e:
            import logging as _l
            _l.getLogger("state.kb").warning(
                "embed failed for record %s: %s", record.get("id"), e,
            )
            return None

        # 查 top-K
        try:
            hits = query_across_scopes(
                entity, vec, top_k=5,
                project_id=self.project_id,
                exclude_ids=[record.get("id")],
                min_cosine=0.80,
            )
        except Exception as e:
            import logging as _l
            _l.getLogger("state.kb").warning("query_across_scopes failed: %s", e)
            return None

        if not hits:
            return None

        # ── 真模型 cosine 调优（v0.3.2.1 dogfood）─────────────────────────
        # multilingual-e5-small 实测：
        #   同义不同字面（同 scope）cosine = 0.92-0.94（不够 0.95 但已显著高）
        #   同字面不同 scope cosine = 0.987（高 cosine 不等于 same claim）
        #   完全无关主题 cosine = 0.85+（model base 整体偏高）
        # 所以纯 cosine 阈值不够 —— **必须配 scope / claim_type 严格 match check**
        # 防字面同 scope 不同误 merge。
        MERGE_COSINE = 0.90      # 调低（0.95 太严，真同义 cos<0.95）
        WARN_COSINE = 0.85       # 走 warn 让 LLM 看

        top_id, top_cos, _scope = hits[0]
        if top_cos >= MERGE_COSINE:
            target = self.get_kb_record(entity, top_id)
            if target is not None and self._safe_to_auto_merge(
                entity, target, record,
            ):
                new_sources = self._compute_new_independent_sources(
                    target.get("sources") or [],
                    record.get("sources") or [],
                )
                return {
                    "action": "merged",
                    "merged_into_id": top_id,
                    "cosine": top_cos,
                    "new_sources": new_sources,
                    "all_top_candidates": [
                        {"id": h[0], "cosine": h[1], "scope": h[2]}
                        for h in hits[:3]
                    ],
                }
        # cosine ≥ WARN_COSINE 或 ≥ MERGE 但 scope/type 不 match → warn
        if top_cos >= WARN_COSINE:
            return {
                "action": "warn_similar",
                "candidates": [
                    {"id": h[0], "cosine": h[1], "scope": h[2]}
                    for h in hits[:5]
                ],
            }
        return None

    def _safe_to_auto_merge(
        self, entity: str, target: dict, new_record: dict,
    ) -> bool:
        """是否可以 auto-merge 两个 KB record。**严格 scope / type match**。

        防 multilingual-e5 在短文本上的"字面同 scope 不同 cosine ≈ 0.99"误判。
        """
        if entity == "concepts":
            # concept_type 必须同（method vs dataset 即使名字像也不该 merge）
            if target.get("concept_type") != new_record.get("concept_type"):
                return False
            # v3.2（2026-07 KB 审计 Bug#1）：**身份类实体禁止按主题相似度自动合并**。
            # person / group 的"描述相似"= 主题相似（同一篇论文的 3 位共同作者
            # 描述都提"LJ cutoff in alchemical FE"，cosine 0.92+），不是同一实体。
            # 实测事故：Whitmore/Ramezani/Sharma 三位作者被并成一个 concept 的 aliases。
            # 原则：概率性判据（embedding）只能 propose，不能对身份实体直接执行 mutation。
            # → 返 False 让上层落到 warn_similar（写新 record + dedup 警告，不合并）。
            if new_record.get("concept_type") in ("person", "group"):
                return False
            return True

        if entity == "claims":
            # claim_type 必须同
            if target.get("claim_type") != new_record.get("claim_type"):
                return False
            # v3.3（#395-Issue2）：**绑在不同冻结预注册上的两条 claim 是修订，
            # 不是重复** —— 哪怕措辞几乎一样。
            #
            # 与上面 person/group 那条同源：高 cosine 说明"讲的是同一个话题"，
            # 不说明"是同一个承诺"。hypothesis 是有锚点的承诺，锚点就是它绑的
            # 那份 frozen prereg。
            #
            # 实测事故（jicq E2E 流体力学，Study3 假设4/5）：hypothesis 冻了
            # prereg v5、带**新的** falsification_criteria_structured 重建 claim，
            # 语义命中上一版 → _semantic_merge_into 只合并 sources，把新判据和新
            # prereg_chunk_id **整个丢掉**，返回旧记录；validate_hypothesis_outputs
            # 于是照样失败，而改措辞还是命中（同义 cosine 依旧 ≥0.90）——
            # 节点没有任何确定性的修复路径，连着 5 次 incomplete 到 blocked。
            #
            # 这里只放开"新建"，不改写旧记录：冻结的承诺不可被覆盖（架构审计
            # 高危项"prereg 冻结可绕"），修订走新记录 + supersede 语义。
            t_prereg = (target.get("prereg_chunk_id") or "").strip()
            n_prereg = (new_record.get("prereg_chunk_id") or "").strip()
            if t_prereg != n_prereg:
                return False
            # scope_dimensions 关键字段（dataset / regime / task）必须**严格同**
            # —— 都有则比较；只一方有则不匹配（信息不对称难判等价）
            tgt_dims = target.get("scope_dimensions") or {}
            new_dims = new_record.get("scope_dimensions") or {}
            for key in ("dataset", "regime", "task", "split"):
                t_val = tgt_dims.get(key)
                n_val = new_dims.get(key)
                # 都有 → 必须严格相等
                if t_val and n_val and t_val != n_val:
                    return False
                # 一方有一方无 → 信息不对称，保守 not merge
                if (t_val is None) != (n_val is None):
                    if t_val is None and n_val is None:
                        continue
                    return False
            # scope itself (org vs project) 不影响 merge 判断（scope routing 由 jsonl 决定）
            return True

        # 其它 entity 默认允许
        return True

    def _concept_lookup_for_embed(self, cid: str) -> dict | None:
        """给 embedding template 用：concept_id → record。跨 scope 查。"""
        rec = self.get_kb_record("concepts", cid)
        return rec

    def _claim_lookup_for_embed(self, cid: str) -> dict | None:
        rec = self.get_kb_record("claims", cid)
        return rec

    @staticmethod
    def _compute_new_independent_sources(
        existing_sources: list[str], new_sources: list[str],
    ) -> list[str]:
        """简单 source 独立性：去掉 existing 中已有的 source string。

        v1：纯 string 比较。Future: 解析 doi: / arxiv: prefix，同 paper 不同 chunk
        不视作独立。
        """
        existing_set = set(existing_sources)
        return [s for s in new_sources if s and s not in existing_set]

    def _semantic_merge_into(
        self, entity: str, new_record: dict, target_id: str,
        dedup_info: dict, *, now: str, curator_run_id: str | None,
    ) -> dict:
        """sim ≥ 0.95 命中：把新 record 合并到 target；不写新 record。

        claims：merge sources + bump independent_source_count，跨项目可加 replication_count
        concepts：merge canonical_name (旧的) 进 aliases，merge aliases / description
        """
        target_path = self._find_path_for_kb_id(entity, target_id)
        if target_path is None:
            return new_record
        with _kb_file_lock(target_path):
            existing = self._read_kb_records(target_path)
            for i, r in enumerate(existing):
                if r.get("id") != target_id:
                    continue
                if entity == "claims":
                    new_sources = dedup_info.get("new_sources") or []
                    src_old = list(r.get("sources") or [])
                    for s in new_sources:
                        if s not in src_old:
                            src_old.append(s)
                    r["sources"] = src_old
                    r["independent_source_count"] = len(src_old)
                    if (new_record.get("scope") == "project"
                            and self.project_id
                            and new_sources):
                        r["replication_count"] = int(r.get("replication_count") or 0) + 1
                    sig_preview = (new_record.get("claim_text") or "")[:200]
                else:   # concepts
                    aliases = list(r.get("aliases") or [])
                    new_name = new_record.get("canonical_name", "")
                    if new_name and new_name != r.get("canonical_name") and new_name not in aliases:
                        aliases.append(new_name)
                    for a in (new_record.get("aliases") or []):
                        if a not in aliases and a != r.get("canonical_name"):
                            aliases.append(a)
                    r["aliases"] = aliases
                    # description 补充：若 target 空且 new 非空
                    if not r.get("description") and new_record.get("description"):
                        r["description"] = new_record["description"]
                    sig_preview = new_name[:200]

                r.setdefault("derived", {})
                r["derived"]["last_semantic_merge_at"] = now
                merge_log = r["derived"].setdefault("semantic_merge_log", [])
                merge_log.append({
                    "at": now,
                    "from_record_signature": sig_preview,
                    "cosine": dedup_info.get("cosine"),
                    "new_sources_added": dedup_info.get("new_sources", []),
                    "by_run_id": self.run_id,
                    "by_curator_run_id": curator_run_id,
                })
                r["updated_at"] = now
                existing[i] = r
                self._write_kb_records(target_path, existing)
                return r
        return new_record

    def _append_embedding_to_index(self, entity: str, record: dict) -> None:
        """写 jsonl 成功后，把 embedding 加进 vector index。

        懒：用单例 client + 同步追加。索引未初始化时静默跳过（caller 之后 rebuild）。

        env: HARNESS_DISABLE_SEMANTIC_DEDUP=1 → 跳过（测试/CI）。
        """
        if os.getenv("HARNESS_DISABLE_SEMANTIC_DEDUP") == "1":
            return
        from core.embeddings import get_default_embedding_client
        from core.kb_embedding_text import (
            claim_to_embed_text, concept_to_embed_text,
        )
        from core.kb_vector_index import append_one, should_rebuild

        client = get_default_embedding_client()
        scope = "project" if record.get("scope") == "project" else "org"
        proj = self.project_id if scope == "project" else None
        if should_rebuild(scope, proj, client=client):
            # 索引还没初始化或 signature 过期 —— 跳过，等下次 rebuild_if_needed
            return
        if entity == "claims":
            text = claim_to_embed_text(
                record,
                concept_lookup=self._concept_lookup_for_embed,
                claim_lookup=self._claim_lookup_for_embed,
            )
        else:
            text = concept_to_embed_text(record)
        vec = client.embed([text])[0]
        append_one(entity, record["id"], vec, scope, proj)

    def list_kb(self, entity: str,
                    *, scope_filter: str | None = None) -> list[dict]:
        """v3 列 entity 所有 record。

        scope_filter=None 跨 project + org；'project' 仅项目；'org' 仅 org。
        同 id 去重，project 层优先（让项目可 shadow org）。
        """
        paths: list[Path]
        if scope_filter == "project":
            paths = [self._kb_path(entity, "project")]
        elif scope_filter == "org":
            paths = [self._kb_path(entity, "org")]
        else:
            paths = self._kb_read_paths(entity)

        seen: set[str] = set()
        out: list[dict] = []
        for path in paths:
            for r in self._read_kb_records(path):
                rid = r.get("id")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                out.append(r)
        return out

    def get_kb_record(self, entity: str, kb_id: str) -> dict | None:
        for r in self.list_kb(entity):
            if r.get("id") == kb_id:
                return r
        return None

    def patch_derived(self, entity: str, kb_id: str, derived_patch: dict,
                         *, curator_run_id: str | None = None) -> dict | None:
        """更新 derived 段（不动 canonical / lifecycle）。同 v2 patch_derived 但走 v3 路径。"""
        from shared.lib.kb_schema import classify_field
        now = datetime.now(timezone.utc).isoformat()

        target_path = self._find_path_for_kb_id(entity, kb_id)
        if target_path is None:
            return None

        clean: dict[str, Any] = {}
        for k, v in derived_patch.items():
            seg = classify_field(entity, k)
            if seg in ("canonical", "lifecycle"):
                continue
            clean[k] = v
        clean["derived_at"] = now
        if curator_run_id:
            clean["derived_by_curator_run_id"] = curator_run_id

        with _kb_file_lock(target_path):
            records = self._read_kb_records(target_path)
            updated = None
            for i, r in enumerate(records):
                if r.get("id") == kb_id:
                    records[i] = {**r, **clean}
                    updated = records[i]
                    break
            if updated is None:
                return None
            self._write_kb_records(target_path, records)
            return updated

    def set_lifecycle_fields(self, entity: str, kb_id: str, fields: dict) -> dict | None:
        """直接写 lifecycle 段里的字段（不走 status 状态机）。

        与 `patch_derived` 对称：只收 lifecycle 段的字段，别的段一个都不碰 ——
        canonical 是内容本身，derived 是可重算的。`status` / `review_history` 不许走
        这里：它们有 `update_lifecycle` 那条带转换记账的路。
        """
        from shared.lib.kb_schema import classify_field

        clean = {k: v for k, v in fields.items()
                 if classify_field(entity, k) == "lifecycle"
                 and k not in ("status", "review_history")}
        unknown = sorted(set(fields) - set(clean))
        if unknown:
            raise ValueError(f"{entity} 的 lifecycle 段没有这些字段（或它们有自己的路）：{unknown}")
        target_path = self._find_path_for_kb_id(entity, kb_id)
        if target_path is None:
            return None
        with _kb_file_lock(target_path):
            records = self._read_kb_records(target_path)
            updated = None
            for i, r in enumerate(records):
                if r.get("id") == kb_id:
                    records[i] = {**r, **clean,
                                  "updated_at": datetime.now(timezone.utc).isoformat()}
                    updated = records[i]
                    break
            if updated is None:
                return None
            self._write_kb_records(target_path, records)
            return updated

    def update_lifecycle(self, entity: str, kb_id: str,
                            *, status_change: dict | None = None,
                            reasoning: str = "",
                            curator_run_id: str | None = None) -> dict | None:
        """更新 lifecycle（status / review_history 等）。"""
        from shared.lib.kb_schema import can_transition_claim_status, derive_status
        now = datetime.now(timezone.utc).isoformat()

        target_path = self._find_path_for_kb_id(entity, kb_id)
        if target_path is None:
            return None

        with _kb_file_lock(target_path):
            records = self._read_kb_records(target_path)
            updated = None
            for i, r in enumerate(records):
                if r.get("id") != kb_id:
                    continue

                # v3.1 fix：confidence-only 更新（status_change 无 to_status）
                # 以前也会走转换校验，can_transition(from, None) 恒 False → 100% 抛错。
                to_s = (status_change or {}).get("to_status")
                if to_s is not None:
                    from_s = r.get("status", "open")
                    # 判决拆除（state:1584）：claim 状态机的「合法转换表」不再是
                    # 一道拒绝。转换本身连同 reasoning / evidence_ids / by_user_id /
                    # by_run_id 全部进 review_history，账不会假；「新证据能不能翻一条
                    # refuted / superseded」是科学判断，不是流程能拍的板（真实期刊
                    # 有 retraction-of-retraction）。表外的转换照放行、如实标
                    # `unusual_transition: true`，由 referee 终审读 review_history
                    # 判是非。工具层另有 validate_status_flip（空 reasoning /
                    # 缺证据）与 prereg 闸，这里删了不会裸奔。
                    unusual = (entity == "claims"
                               and not can_transition_claim_status(from_s, to_s))

                    try:
                        from core.identity import current_user_id
                        by_user = current_user_id()
                    except Exception:
                        by_user = "anonymous"
                    r["status"] = to_s
                    r["last_reviewed_at"] = now
                    entry = {
                        "from_status": from_s,
                        "to_status": to_s,
                        "reasoning": reasoning,
                        "by_run_id": self.run_id,
                        "by_user_id": by_user,
                        "by_curator_run_id": curator_run_id,
                        "at": now,
                    }
                    if unusual:
                        entry["unusual_transition"] = True
                    # v3.1：verdict 翻转的证据链接进 audit（validate_status_flip 强制）
                    ev = (status_change or {}).get("evidence_ids")
                    if ev:
                        entry["evidence_ids"] = list(ev)
                    # RFC 2026-08-18：取代关系是 lifecycle 事实，随翻转一起落
                    successor = (status_change or {}).get("superseded_by_claim_id")
                    if successor:
                        r["superseded_by_claim_id"] = str(successor)
                        entry["superseded_by_claim_id"] = str(successor)
                    r.setdefault("review_history", []).append(entry)

                # confidence override（如果传了）
                if "confidence" in (status_change or {}):
                    r["confidence"] = float(status_change["confidence"])
                    # re-derive status
                    if entity == "claims":
                        r["status"] = derive_status(r)

                r["updated_at"] = now
                records[i] = r
                updated = r
                break

            if updated is None:
                return None
            self._write_kb_records(target_path, records)
            return updated


# ── 文件锁 helper（模块级）──────────────────────────────────────────────────

import contextlib                          # noqa: E402


@contextlib.contextmanager
def _kb_file_lock(path: Path):
    """跨进程文件锁：两个平台都真锁（`shared.lib.filelock`）。个人版里后端与
    worker 是两个进程、都写同一份 KB —— 「Windows 退化成 no-op」曾是静默数据竞争。"""
    with filelock.exclusive(path.with_suffix(path.suffix + ".lock")):
        yield
