"""用户说了「不要修改文件」：派发时声明、写入时禁止、收尾时照实数（#973）。

## 现场（2026-09-12）

用户明说「只做检查和报告…不要修改文件」。这次会话实际新增了 **24 个** Project
文件，而调度器的最终回复和独立审稿**双双称「未修改文件」**，审稿还给了 0.92 通过。

那 24 个文件全是 experiment / reviewer 自己目录里的证据（运行日志、三件套、审稿
意见）—— 用户的源码一个字没动。「没改源码」被简写成「没改文件」；而当时的判据
只认 `.research/`，节点证据反而会被算成「改了用户文件」（lujy 09-18 实测）。

## 裁决（wangd 2026-10-21）

1. 「别改文件」由**调度器派发时**传结构化 `read_only` 标记，框架机械执行，
   不靠猜意图。子 run 继承父的承诺，不能自己放宽。
2. 文件分三类（`core.project_workspace.classify_project_path`，唯一判据）：
   用户源文件 / 节点证据（按 workspace_prefix）/ `.research` 记账。
   只读约束下用户源文件这一类**禁写**；终稿与 Reviewer 都按三类分别陈述，
   不再说「未修改文件」。

## 这一层做什么

* :func:`declare_read_only` —— 记下承诺（落 run 目录 + transcript 事件），
  同时记下 transcript 当时的长度：**改动从这一刻起算**。
* :func:`refuse_user_source_write` —— 写入咽喉调它；只读约束下写用户源文件就拒。
* :func:`reconcile` / :func:`reconcile_run` —— 读 transcript 里带路径的改动事件
  （本节点的 + 子 run 汇总上来的），按三类返回**事实**。
* :func:`end_read_only` —— 一次请求结束（下一条真实用户消息），承诺随之结束。

**返回事实，不下判决。**怎么跟用户说、Reviewer 能不能放行，由各自的调用方决定；
这里只保证「说的和做的」放在一起，而且不可能悄悄对不上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.project_workspace import (
    BOOKKEEPING,
    NODE_EVIDENCE,
    USER_SOURCE,
    ProjectWorkspaceError,
    classify_project_path,
    node_prefix_of,
)

#: state 上挂承诺的属性名（活的那份）；盘上那份在 run 目录的
#: `side_effect_contract.json` —— 续跑、换进程之后还在。
_ATTR = "_side_effect_promise"

#: 哪些 transcript 事件带着 Project 改动的路径，以及路径在哪个字段。
#: 漏一种就少数一类改动 —— 所以这里是全集，不是举例：
_CHANGE_EVENTS: dict[str, str] = {
    # 本节点作用域里的每一次改动（project_workspace.observe_after_tool）
    "workspace_changed": "paths",
    # 作用域外的写：交付写权写的项目根文件、越界见证（enforce_after_tool）
    "workspace_out_of_scope_writes": "paths",
    # save_artifact 的交付投影（用户点名的文件，由框架写）
    "deliverable_projected": "path",
    # 子 run 收尾时汇总上来的改动（run_node._finish_child）
    "child_workspace_changed": "paths",
}

#: 汇总事件里路径的上限。一个 run 写上千个文件很少见，但真遇上也不能把事件撑爆；
#: 超了照实标 truncated。
ROLLUP_PATH_LIMIT = 5000


@dataclass(frozen=True)
class ReadOnlyPromise:
    """派发方声明的「不要修改文件」。"""

    said_by: str = "user"
    quote: str = ""
    declared_by_run_id: str | None = None
    #: 声明那一刻本 run transcript 的字节长度 —— 改动从这里起算。复用一个长会话的
    #: transcript 却不带起点，就会把用户还没提要求之前的正常改动也算成违约。
    since_offset: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "said_by": self.said_by,
            "quote": self.quote,
            "declared_by_run_id": self.declared_by_run_id,
            "since_offset": self.since_offset,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReadOnlyPromise:
        return cls(
            said_by=str(raw.get("said_by") or "user"),
            quote=str(raw.get("quote") or ""),
            declared_by_run_id=raw.get("declared_by_run_id") or None,
            since_offset=max(0, int(raw.get("since_offset") or 0)),
        )


@dataclass
class Reconciliation:
    """说的 vs 做的，按三类。**事实，不是判决。**"""

    promised_read_only: bool
    promise: dict[str, Any] | None
    user_source: list[str] = field(default_factory=list)
    node_evidence: list[str] = field(default_factory=list)
    bookkeeping: list[str] = field(default_factory=list)

    @property
    def contradiction(self) -> bool:
        """只读约束下改了用户源文件。节点证据与记账不构成矛盾 —— 它们照数。"""
        return self.promised_read_only and bool(self.user_source)

    @property
    def total(self) -> int:
        return len(self.user_source) + len(self.node_evidence) + len(self.bookkeeping)

    def evidence_by_prefix(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for path in self.node_evidence:
            prefix = node_prefix_of(path) or "?"
            out[prefix] = out.get(prefix, 0) + 1
        return out

    def counts_text(self, source_label: str = "用户源文件") -> str:
        """三类各几个 —— 「用户源文件 0 个 · 节点证据 22 个（experiments/ 21、reviews/ 1）· …」

        `source_label`：说给用户听时是「你的源文件」，说给模型听时是「用户源文件」。
        """
        evidence = f"节点证据 {len(self.node_evidence)} 个"
        if self.node_evidence:
            evidence += "（" + "、".join(
                f"{prefix}/ {n}" if not prefix.endswith(".md") else f"{prefix} {n}"
                for prefix, n in self.evidence_by_prefix().items()) + "）"
        return " · ".join([
            f"{source_label} {len(self.user_source)} 个",
            evidence,
            f".research 记账 {len(self.bookkeeping)} 个",
        ])

    def statement(self) -> str:
        """给人读的一句：用户要求只读时，这一次实际改了什么。没有承诺就是空串。

        恒按三类说，零就说零 —— 「没改源文件」和「没改文件」不能再被写成一句话。
        """
        if not self.promised_read_only:
            return ""
        head = "**文件改动核对**（你要求不修改文件）："
        if not self.total:
            return head + "本次没有新增或修改任何 Project 文件。"
        if self.contradiction:
            shown = "、".join(self.user_source[:5])
            more = "" if len(self.user_source) <= 5 else f" 等 {len(self.user_source)} 个"
            return (f"⚠️ {head}{self.counts_text('你的源文件')}。"
                    f"改动了你的源文件：{shown}{more}。")
        return (f"{head}{self.counts_text('你的源文件')}。你的源文件没有改动；"
                "其余是执行留下的证据和记账，都在 Project 里可见。")

    def as_dict(self, *, limit: int = 50) -> dict[str, Any]:
        return {
            "promised_read_only": self.promised_read_only,
            "promise": self.promise,
            "counts": {
                USER_SOURCE: len(self.user_source),
                NODE_EVIDENCE: len(self.node_evidence),
                BOOKKEEPING: len(self.bookkeeping),
            },
            "node_evidence_by_prefix": self.evidence_by_prefix(),
            USER_SOURCE: list(self.user_source[:limit]),
            NODE_EVIDENCE: list(self.node_evidence[:limit]),
            BOOKKEEPING: list(self.bookkeeping[:limit]),
            "contradiction": self.contradiction,
        }


# ── 承诺：声明 / 读回 / 结束 ─────────────────────────────────────────────────


def _contract_file(root: Any) -> Path | None:
    """承诺落在 run 根下这一个文件里（与 stop_authorization.json 同类：本 run 自己
    的一份固定产物，文件名写成字面量）。"""
    if not isinstance(root, (str, Path)):
        return None          # 没有 run 目录（测试替身 / 未落盘的 state）：只有活的那份
    return Path(root) / "side_effect_contract.json"


def _transcript_file(state: Any) -> Path | None:
    path = getattr(state, "transcript_path", None)
    return Path(path) if isinstance(path, (str, Path)) else None


def _load(root: Any) -> ReadOnlyPromise | None:
    path = _contract_file(root)
    if path is None or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return ReadOnlyPromise.from_dict(raw) if raw.get("read_only") else None


def promised_read_only(state: Any) -> ReadOnlyPromise | None:
    """本 run 此刻是否在只读约束下。活的那份优先，没有就读盘（续跑 / 换进程）。"""
    if state is None:
        return None
    value = getattr(state, _ATTR, None)
    if isinstance(value, ReadOnlyPromise):
        return value
    if value is False:
        return None          # 已经确认过没有（或已结束）—— 不必每次写入都 stat 一遍
    loaded = _load(getattr(state, "root", None))
    try:
        setattr(state, _ATTR, loaded if loaded is not None else False)
    except Exception:
        pass
    return loaded


def declare_read_only(
    state: Any,
    quote: str = "",
    said_by: str = "user",
    *,
    declared_by_run_id: str | None = None,
) -> ReadOnlyPromise:
    """记下「不要修改文件」。已经有了就**不覆盖**：承诺不能中途挪起点，也不能变宽。"""
    existing = promised_read_only(state)
    if existing is not None:
        return existing
    transcript = _transcript_file(state)
    try:
        offset = transcript.stat().st_size if transcript is not None and transcript.exists() else 0
    except OSError:
        offset = 0
    promise = ReadOnlyPromise(
        said_by=str(said_by or "user"),
        quote=str(quote or "")[:500],
        declared_by_run_id=declared_by_run_id,
        since_offset=offset,
    )
    try:
        setattr(state, _ATTR, promise)
    except Exception:
        pass
    path = _contract_file(getattr(state, "root", None))
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"read_only": True, **promise.as_dict()},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass         # 活的那份还在；盘上那份只服务续跑
    append = getattr(state, "append_transcript", None)
    if callable(append):
        try:
            append("side_effect_contract_declared", read_only=True, **promise.as_dict())
        except Exception:
            pass
    return promise


def end_read_only(state: Any, *, reason: str) -> bool:
    """这次请求结束了，承诺随之结束。没有承诺就什么都不做（返回 False）。"""
    if promised_read_only(state) is None:
        return False
    try:
        setattr(state, _ATTR, False)
    except Exception:
        pass
    path = _contract_file(getattr(state, "root", None))
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    append = getattr(state, "append_transcript", None)
    if callable(append):
        try:
            append("side_effect_contract_ended", reason=str(reason)[:200])
        except Exception:
            pass
    return True


def dispatch_promise(parent_state: Any, read_only: bool) -> ReadOnlyPromise | None:
    """派给子 run 的那份承诺：父身上有就原样往下传（子 run 不能自己放宽）；
    父没有，就看这一次派发的 `read_only` 标记。"""
    inherited = promised_read_only(parent_state)
    if inherited is not None:
        return inherited
    if read_only:
        return ReadOnlyPromise(
            said_by="dispatcher",
            declared_by_run_id=getattr(parent_state, "run_id", None),
        )
    return None


def refuse_user_source_write(promise: ReadOnlyPromise | None, rel_path: str) -> None:
    """只读约束下写用户源文件 → 拒（ProjectWorkspaceError，派发口记成 rejected）。

    节点证据与 `.research` 不拦：experiment 的诚实终态必须留下运行记录和三件套，
    拦掉它们就是把「说错话」变成「做不成事」（09-15 那条理由仍然成立）。
    """
    if promise is None or classify_project_path(rel_path) != USER_SOURCE:
        return
    raise ProjectWorkspaceError(
        f"只读约束：用户要求不修改文件，{rel_path!r} 是用户源文件"
        "（不在任何节点的作用域里，也不是 .research 记账），这一次写入已拒绝。"
        "约束由派发方声明，本 run 不能自行解除。"
        "要交给用户的内容写进你自己的作用域，或直接写在回复里。"
    )


# ── 对账：改动事件 → 三类 ─────────────────────────────────────────────────────


def _events_since(path: Path | None, offset: int) -> list[dict[str, Any]]:
    """从 transcript 的 `offset` 字节处读到末尾，只解析带改动路径的那几种事件。"""
    if path is None or not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(offset if 0 <= offset <= size else 0)
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not any(name in line for name in _CHANGE_EVENTS):
            continue          # 一个 experiment run 的 transcript 能有几十 MB，先按名字筛
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def changed_paths(events: list[dict[str, Any]] | None) -> list[str]:
    """事件里所有被改过的 Project 路径，按首次出现去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        key = _CHANGE_EVENTS.get(str(event.get("event") or event.get("type") or ""))
        if key is None:
            continue
        value = event.get(key)
        for path in ([value] if isinstance(value, str) else (value or [])):
            text = str(path or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def _reconcile(promise: ReadOnlyPromise | None, events: list[dict[str, Any]]) -> Reconciliation:
    result = Reconciliation(
        promised_read_only=promise is not None,
        promise=promise.as_dict() if promise else None,
    )
    buckets = {USER_SOURCE: result.user_source, NODE_EVIDENCE: result.node_evidence,
               BOOKKEEPING: result.bookkeeping}
    for path in changed_paths(events):
        buckets[classify_project_path(path)].append(path)
    return result


def reconcile(state: Any, events: list[dict[str, Any]] | None = None) -> Reconciliation:
    """一个活的 run：承诺 + 自承诺起的全部改动（含子 run 汇总上来的）。

    ``events`` 不给就从本 run 的 transcript 读，起点是承诺的 ``since_offset``；
    没有承诺时从头读。**读不到事件不等于没改过** —— 那种情况如实返回空集。
    """
    promise = promised_read_only(state)
    if events is None:
        events = _events_since(_transcript_file(state),
                               promise.since_offset if promise else 0)
    return _reconcile(promise, events)


def reconcile_run(run_root: Path | str) -> Reconciliation:
    """一个**不在本进程里活着**的 run（Reviewer 审的 producer、刚收尾的子 run）：
    从它的 run 目录读承诺与 transcript。"""
    root = Path(run_root)
    promise = _load(root)
    return _reconcile(promise, _events_since(root / "transcript.jsonl",
                                             promise.since_offset if promise else 0))


def append_statement(state: Any, reply: str) -> str:
    """只读约束在场时，在回复末尾机械附上三类改动 —— 模型写什么都改不掉这几行。

    幂等：同一刻同一份事实只附一次。回复有两个出口会经过这里（共享脊柱
    `session_driver.run_turn`，和平台每种操作共用的 `_operation_end` —— 答复
    暂停后续跑出来的回复只走后者），两处都调，谁先到谁附。
    """
    if promised_read_only(state) is None or not (reply or "").strip():
        return reply
    line = reconcile(state).statement()
    if not line or line in reply:
        return reply
    return f"{reply.rstrip()}\n\n{line}"


# ── Reviewer 的副作用检查：被审的那个 run 说了什么、做了什么 ─────────────────

#: 框架算好的检查结果挂在 reviewer 的 hook_state 上：context 组装时算一次，
#: compose_review_critique 读同一份（set_verdict 拒放行、finalize 盖章）。
PRODUCER_CHECK_KEY = "_producer_side_effect_check"


def producer_side_effect_check(state: Any, node_inputs: dict | None = None) -> dict[str, Any] | None:
    """Reviewer 审的 producer run：用户的副作用约束 + 它实际改了什么。

    2026-09-12 那次审稿逐条核对了产物和日志，却没把改动清单和用户那句「不要修改
    文件」放在一起比，于是照着 producer 的说法写了「全程只读」并给 0.92 通过。
    这两样都是**框架知道的事实**，所以由框架算好交到审稿手上、盖进 critique，
    不让模型申报。producer 不在（没给 producer_run_id、run 目录不在）就是 None。
    """
    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        return None
    cached = hook_state.get(PRODUCER_CHECK_KEY)
    if isinstance(cached, dict):
        return cached
    inputs = node_inputs if node_inputs is not None else (hook_state.get("node_inputs") or {})
    run_id = str((inputs or {}).get("producer_run_id") or "").strip()
    root = getattr(state, "root", None)
    # producer 与 reviewer 是同一个 base_dir 下的兄弟 run（run_node 的 state_dir）。
    # run_id 只许是一段名字 —— 它是派发参数，不能拿来走出 runs 目录。
    if not run_id or root is None or Path(run_id).name != run_id or run_id in {".", ".."}:
        return None
    producer_root = Path(root).parent / run_id
    if not producer_root.is_dir():
        return None
    rec = reconcile_run(producer_root)
    check = {
        **rec.as_dict(limit=20),
        "producer_run_id": run_id,
        "counts_text": rec.counts_text(),
        "stamped_by": "framework",
    }
    hook_state[PRODUCER_CHECK_KEY] = check
    return check
