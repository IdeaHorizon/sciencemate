"""审过的论文没冻结 = 没交付 —— 这条账框架自己记，不靠模型记性。

## 病例（E2E v27 + v28，同一格两种坏法）

- **v27**：reviewer approve 后 orchestrator 想 freeze，被"作者签字不能代签"机械拒
  （artifacts_extra owner!=here），模型转而**伪造** `frozen` 字段绕过（#617 已堵）。
- **v28**：reviewer 两审 approve，但没有任何一步真的 freeze，writing run 却 completed。
  manuscript 至今 `frozen=None`、没进 deliverables/ —— 论文没交付。

两次同因：completion 门只检查「审查 flow 闭合」，而 flow 一闭合就从
`pending_post_node_flow` 出列，**没有任何机制检查「被批准的论文真的冻结了」**。
这条 obligation 补上这道判据：完成态、审过、却没冻结的 manuscript = 阻塞义务。

## 三重触发（防两类误伤）
1. `preflight_status=passed` 且未冻结 —— blocked 的材料不足报告不欠冻结。
2. 最新 writing_critique 是 approve/proceed —— 草稿期 / 被打回都不催冻。
3. 没有仍 open 且点名它的 flow —— 决策结算中别插队。

了结路径可达（防死锁）：writing 自己 freeze（作者签字），discharge_hint 指向 writing。

夹具照现行记录模型：manuscript / review_critique 是节点目录下的原生文件
（`paper/manuscript__paper.tex`、`reviews/review_critique__….json`），出处、
登记时刻、冻结在工作区账本（`core/ledger`）里 —— 冻结是账本上的一行。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core import obligations
from core.ledger import workspace_store, write_record
from core.project_workspace import _NODE_WORKSPACES as _DIRS


class _State:
    def __init__(self, worktree: Path, flows: list | None = None) -> None:
        self.project_worktree = worktree
        self.node_type = "_orchestrator"
        self.hook_state = {"pending_post_node_flow": flows or []}

    def append_transcript(self, event: str, **fields) -> None:
        pass


def _manuscript(worktree: Path, *, preflight="passed", frozen=False,
                created="2026-08-22T10:00:00+00:00", name="paper") -> str:
    d = worktree / _DIRS["writing"]
    d.mkdir(parents=True, exist_ok=True)
    review_pdf = d / f"{name}_review.pdf"
    clean_pdf = d / f"{name}_clean.pdf"
    review_pdf.write_bytes(b"%PDF-1.4\nreview")
    clean_pdf.write_bytes(b"%PDF-1.4\nclean")
    meta = {
        "preflight_status": preflight,
        "format": "latex",
        "pdf_path": str(review_pdf),
        "pdf_variants": {
            "review": {"pdf_path": str(review_pdf)},
            "clean": {"pdf_path": str(clean_pdf)},
        },
    }
    rec = write_record(
        worktree, artifact_type="manuscript", name=name, content="\\documentclass...",
        directory=_DIRS["writing"], metadata=meta,
        produced_by_node_type="writing", produced_by_run_id="r-writing", created_at=created,
    )
    if frozen:
        workspace_store(worktree).freeze(rec["id"], metadata_patch={}, by_node="writing",
                                         by_run="r-writing",
                                         frozen_at="2026-08-22T11:00:00+00:00")
    return rec["id"]


def _critique(worktree: Path, *, action="proceed",
              created="2026-08-22T10:30:00+00:00", name="writing_critique_v1",
              content=None, metadata=None) -> str:
    if content is None:
        content = json.dumps({"verdict": "approve", "decision": {"action": action}},
                             ensure_ascii=False)
    if metadata is None:
        # 真实 critique 都在 metadata 写结构化契约字段（review_spec 强制）
        metadata = {"verdict": "approve", "recommended_action": action}
    store = workspace_store(worktree)
    # 审的是最新那份 manuscript：与产物层同一判据（created_at 最大）。
    subject_head = max(
        (h for h in store.heads().values() if h.artifact_type == "manuscript"),
        key=lambda h: h.created_at,
    )
    subject_record = store.record(subject_head.artifact_id)
    subject = {
        "artifact_id": subject_head.artifact_id,
        "version": subject_record["version"],
        "content_hash": hashlib.sha256(subject_record["content"].encode()).hexdigest(),
        "pdf_variant_sha256": {
            variant: hashlib.sha256(
                Path(info["pdf_path"]).read_bytes()
            ).hexdigest()
            for variant, info in subject_record["metadata"]["pdf_variants"].items()
        },
    }
    metadata = {**metadata, "review_subject": subject}
    payload = json.loads(content)
    payload["review_subject"] = subject
    payload["verdict"] = metadata["verdict"]
    payload["recommended_action"] = {"action": metadata["recommended_action"]}
    payload["_composed_by"] = "compose_review_critique"
    content = json.dumps(payload, ensure_ascii=False)
    return write_record(
        worktree, artifact_type="review_critique", name=name, content=content,
        directory=_DIRS["_reviewer"], metadata=metadata,
        produced_by_node_type="_reviewer", produced_by_run_id="r-review", created_at=created,
    )["id"]


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    return tmp_path / "wt"


def _owed(state) -> list:
    return obligations._collect_unfrozen_manuscript(state, [])


# ── 主病例：审过、passed、没冻 → 欠一笔冻结 ─────────────────────────────────

def test_approved_unfrozen_manuscript_is_owed(worktree):
    _manuscript(worktree)
    _critique(worktree, action="proceed")
    owed = _owed(_State(worktree))
    assert len(owed) == 1
    o = owed[0]
    assert o.kind == obligations.KIND_UNFROZEN_MANUSCRIPT
    assert o.owed_by == "writing"          # 作者签字，不是 orchestrator
    assert o.blocking is True
    assert "freeze_artifact" in o.discharge_hint
    assert "manuscript__paper" in o.discharge_hint
    assert "orchestrator" in o.discharge_hint  # 明确不能代签


# ── 了结：冻了就没账 ─────────────────────────────────────────────────────────

def test_a_frozen_manuscript_owes_nothing(worktree):
    _manuscript(worktree, frozen=True)
    _critique(worktree, action="proceed")
    assert _owed(_State(worktree)) == []


# ── 三类不该触发 ─────────────────────────────────────────────────────────────

def test_blocked_material_gap_report_does_not_owe_freeze(worktree):
    """preflight=blocked 的是材料不足报告，本就不该冻。"""
    _manuscript(worktree, preflight="blocked")
    _critique(worktree, action="proceed")
    assert _owed(_State(worktree)) == []


def test_a_manuscript_not_yet_approved_is_not_pushed_to_freeze(worktree):
    """草稿期（还没审 / 被打回）不催冻结 —— 否则会催冻一份 reviewer 拒了的稿。"""
    _manuscript(worktree)
    # 没有 critique
    assert _owed(_State(worktree)) == []
    # 被打回
    _critique(worktree, action="revise")
    assert _owed(_State(worktree)) == []


def test_open_flow_defers_the_freeze_obligation(worktree):
    """决策还在结算（flow 仍 open 且点名它）时不插队。"""
    _manuscript(worktree)
    _critique(worktree, action="proceed")
    flows = [{"artifact_ids": ["manuscript__paper"], "review_state": "pending",
              "decision_state": "pending"}]
    assert _owed(_State(worktree, flows=flows)) == []


# ── 取最新那一份（redirect 后 writing 出了新稿，账跟着新稿走）─────────────────

def test_obligation_tracks_the_latest_manuscript(worktree):
    """老稿被打回、writing 出了更新的一稿并通过 —— 账认最新那份。"""
    _manuscript(worktree, name="old", created="2026-08-22T09:00:00+00:00")
    _manuscript(worktree, name="new", created="2026-08-22T12:00:00+00:00")
    _critique(worktree, action="proceed", created="2026-08-22T12:30:00+00:00")
    owed = _owed(_State(worktree))
    assert len(owed) == 1
    assert "manuscript__new" in owed[0].discharge_hint


# ── 接线：必须走真 collect() 入口，不能只直调 collector ──────────────────────

def test_collector_is_actually_registered(tmp_path):
    """撤掉 _COLLECTORS 里那一行、这条就红 —— 上面几条直调 collector 抓不到「没接线」。

    2026-08-22 变异验证实测：只直调 `_collect_unfrozen_manuscript` 时，把它从
    `_COLLECTORS` 摘掉测试照样全绿（机制在场、没接到路径）。所以这条走真 `collect()`。
    """
    import subprocess

    from core.bootstrap import bootstrap
    from core.state import State

    bootstrap()
    # 先落产物（顺带 mkdir 出 wt/），再绑 —— bind 要求 worktree 存在且是 git 仓
    _manuscript(tmp_path / "wt")
    _critique(tmp_path / "wt", action="proceed")
    wt = tmp_path / "wt"
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "init"], check=True, env=env)
    st = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs",
                   project_id="p_freeze", project_worktree=wt)

    kinds = {o.kind for o in obligations.collect(st)}
    assert obligations.KIND_UNFROZEN_MANUSCRIPT in kinds, (
        "collect() 没产出冻结义务 —— collector 没接进 _COLLECTORS"
    )


# ── 裁决从 metadata 读，不从脆弱的 content 猜（2026-08-23 E2E v29 真实病例）──────

def test_verdict_read_from_metadata_when_content_is_a_repr_blob(worktree):
    """content 把 decision 塞成 repr 字符串时，仍要从 metadata 认出 approve。

    v29 现场：reviewer 的 content 里 `decision` 是 `"{'action': 'proceed', ...}"`
    （单引号 repr），json 解出来是 str 不是 dict，旧 _latest_writing_verdict 取不到
    action、fallback 也拿到整坨字符串 → 冻结义务在一份真 approve 的稿子上静默不触发。
    权威源是 metadata 的结构化契约字段。
    """
    _manuscript(worktree)
    # 复刻 v29 的脆弱 content：decision 是 repr 字符串；但 metadata 干净
    fragile = json.dumps({
        "summary": "approved",
        "decision": "{'action': 'proceed', 'target_node': None, 'feedback': '...'}",
    }, ensure_ascii=False)
    _critique(worktree, content=fragile,
              metadata={"verdict": "approve", "recommended_action": "proceed"})
    owed = _owed(_State(worktree))
    assert len(owed) == 1, f"metadata 明明 approve/proceed，义务却没触发：{owed}"
    assert owed[0].kind == obligations.KIND_UNFROZEN_MANUSCRIPT


def test_metadata_revise_still_defers(worktree):
    """metadata 说 revise（被打回）→ 不催冻，哪怕 content 里有 'proceed' 字样。"""
    _manuscript(worktree)
    _critique(worktree, content=json.dumps({"note": "please proceed to fix then revise"}),
              metadata={"verdict": "major_concerns", "recommended_action": "revise"})
    assert _owed(_State(worktree)) == []
