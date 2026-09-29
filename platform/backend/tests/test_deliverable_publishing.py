"""交付链自动 publish 的机械判据。

命门语义：referee approve 即自动 publish，但**证据不足绝不伪造 approval**。
锚在效果上（哪些 deliverable 会/不会被交付、缺什么证据、cleanliness 放不放行），
变异 `plan_receipts`/`find_writing_approval`/`read_frozen_deliverables`/cleanliness
任一处都应转红。

夹具写的是**真记录**（RFC 2026-09-12 §6）：正文落节点目录的原生文件
（`paper/writing_validation_report__v.json`、`reviews/review_critique__c.json`），
事实进 `.research/ledger/records.jsonl`。写法走 `core.ledger.write_record`，
经 `harness_contract.ledger_module` 加载 —— 后端读记录走的就是这条桥。
"""
import json
import subprocess
from pathlib import Path

from app.services.deliverable_publishing import (
    find_writing_approval,
    plan_receipts,
    read_frozen_deliverables,
)
from app.services.harness_contract import ledger_module
from app.services.project_repository import run_in_repository_thread


def _record(root: Path, atype: str, name: str, content: str = "body", *, directory: str,
            produced_by: str = "framework", run_id: str = "fixture",
            metadata: dict | None = None, frozen: bool = False) -> dict:
    """在工作区 `root` 里落一份记录：原生文件 + 账本行（`frozen=True` 再追一行 freeze）。"""
    return ledger_module().write_record(
        root, artifact_type=atype, name=name, content=content, directory=directory,
        metadata=dict(metadata or {}), produced_by_node_type=produced_by,
        produced_by_run_id=run_id, frozen=frozen,
    )


def _critique(root: Path, source_node: str, verdict: str, name: str = "writing_critique_x") -> dict:
    """referee 的一份 critique —— 形状照真产物：出处 `_reviewer`，裁决在 metadata 与正文里各一份。"""
    return _record(
        root, "review_critique", name,
        json.dumps({"source_node_type": source_node, "verdict": verdict}),
        directory="reviews", produced_by="_reviewer", run_id="run_x",
        metadata={"verdict": verdict},
    )


def test_read_frozen_deliverables_reads_frozen_permanent_records(tmp_path):
    """交付物 = 账本上**冻结且永久**的记录，正文来自那个原生文件；没冻的草稿与
    框架内务（冻了也）不算。没有第二份拷贝可读。"""
    wt = tmp_path / "wt"
    _record(wt, "manuscript", "m", "\\documentclass{article}", directory="paper",
            produced_by="writing", frozen=True)
    _record(wt, "clean_results", "r", "{}", directory="experiments",
            produced_by="experiment", frozen=True)
    _record(wt, "pre_registration", "p", "# prereg", directory="plan",
            produced_by="hypothesis", frozen=True)
    # 没冻 → 草稿，不是交付物
    _record(wt, "manuscript", "draft", "\\documentclass{article}", directory="paper",
            produced_by="writing", frozen=False)
    # 框架内务：冻了也不是交付物（策略表说了算，不在后端另写名单）
    _record(wt, "run_manifest", "rm", "{}", directory="experiments",
            produced_by="experiment", frozen=True)

    found = read_frozen_deliverables(wt)
    assert {(d["type"], d["name"]) for d in found} == {
        ("manuscript", "m"), ("clean_results", "r"), ("pre_registration", "p"),
    }, found
    manuscript = next(d for d in found if d["type"] == "manuscript")
    assert manuscript["content"] == "\\documentclass{article}"
    assert manuscript["path"] == wt / "paper" / "manuscript__m.tex"
    assert manuscript["produced_by_node_type"] == "writing"
    assert manuscript["version"] == 1


def test_manuscript_without_approval_is_not_delivered(tmp_path):
    """铁律：没有 approve critique → review_approval 进 missing → 不该交付。"""
    wt = tmp_path / "wt"
    _record(wt, "writing_validation_report", "v", directory="paper",
            produced_by="writing", frozen=True)
    _critique(wt, "writing", "reject")

    deliverable = {"type": "manuscript", "name": "m", "content": "x"}
    receipts, missing = plan_receipts(deliverable, wt)
    assert any("review_approval" in m for m in missing), missing
    assert not any(r[0] == "review_approval" for r in receipts), receipts


def test_manuscript_with_approval_and_validation_gets_both_receipts(tmp_path):
    wt = tmp_path / "wt"
    _record(wt, "writing_validation_report", "v", directory="paper",
            produced_by="writing", frozen=True)
    _critique(wt, "writing", "approve")

    deliverable = {"type": "manuscript", "name": "m", "content": "x"}
    receipts, missing = plan_receipts(deliverable, wt)
    assert missing == [], missing
    assert {r[0] for r in receipts} == {"writing_validation", "review_approval"}
    ra = next(p for k, p in receipts if k == "review_approval")
    assert ra["decision"] == "approve"


def test_non_manuscript_gets_producer_validation(tmp_path):
    receipts, missing = plan_receipts(
        {"type": "clean_results", "name": "r", "content": "x"}, tmp_path / "wt"
    )
    assert missing == []
    assert [r[0] for r in receipts] == ["producer_validation"]


def test_find_writing_approval_ignores_non_approve_and_non_writing(tmp_path):
    wt = tmp_path
    _critique(wt, "experiment", "approve", "exp_critique")
    assert find_writing_approval(wt) is None
    _critique(wt, "writing", "approve", "writing_critique")
    got = find_writing_approval(wt)
    assert got is not None and got.get("name") == "writing_critique"


def test_cleanliness_tolerates_lock_but_blocks_real_changes(tmp_path):
    from app.services.project_repository import get_project_repository
    repo = get_project_repository()

    root = tmp_path / "wt"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "tracked.txt").write_text("v1")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    assert repo._worktree_has_blocking_changes(root) is False

    (root / "MEMORY.md.lock").write_text("")
    assert repo._worktree_has_blocking_changes(root) is False, "stray .lock must not block"

    (root / "junk.txt").write_text("x")
    assert repo._worktree_has_blocking_changes(root) is True
    (root / "junk.txt").unlink()

    (root / "tracked.txt").write_text("v2")
    assert repo._worktree_has_blocking_changes(root) is True


# ── ③ 触发对账 + ④ curator MEMORY.md flush（2026-08-24，E2E v33 根因）──────────


def test_memory_md_dirty_detects_untracked_and_tracked_changes(tmp_path):
    """④ 的探测：MEMORY.md 未跟踪新建 / 已跟踪被改 都算脏；committed 后干净。

    v33 现场就是「已跟踪被改」——orchestrator 收尾写叙事段，改了已提交的
    MEMORY.md。变异（让它恒返 False）→ 最后一条 tracked-modified 断言转红。
    """
    from app.services.deliverable_publishing import _memory_md_dirty

    root = tmp_path / "wt"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)

    assert _memory_md_dirty(root) is False  # 没有 MEMORY.md
    (root / "MEMORY.md").write_text("v1")
    assert _memory_md_dirty(root) is True  # 未跟踪新建
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    assert _memory_md_dirty(root) is False  # 已提交 → 干净
    (root / "MEMORY.md").write_text("v2")
    assert _memory_md_dirty(root) is True  # 已跟踪被改（v33 病例）


async def test_flush_curator_memory_commits_dirty_memory_md():
    """④：脏 MEMORY.md 以 **curator 所有制**提交（过 access/nodes.yaml 校验），
    worktree 变干净、authority 记录推进。

    非 curator 节点写 MEMORY.md 却无其 checkpoint 权限 → 留脏挡 staging；这条
    flush 是解法。变异（跳过 checkpoint / _memory_md_dirty 恒 False）→ 断言转红。
    """
    from types import SimpleNamespace

    from app.services.deliverable_publishing import (
        _flush_curator_memory_if_dirty,
        _memory_md_dirty,
    )
    from app.services.project_repository import get_project_repository

    repo = get_project_repository()
    (await run_in_repository_thread(repo.initialize_project, 
        project_id="proj-mem", name="Mem", description=None,
        research_domain=None, owner_id="owner",
    ))
    (await run_in_repository_thread(repo.ensure_session_workspace, 
        project_id="proj-mem", session_id="sess-mem", base_commit=None,
        title="Mem", created_by="owner",
    ))
    root = Path((await run_in_repository_thread(repo.session_status, "proj-mem", "sess-mem")).path)
    (root / "MEMORY.md").write_text(
        "# memory\n\n叙事段：最后一次 curator checkpoint 之后写的\n", encoding="utf-8"
    )
    assert _memory_md_dirty(root) is True

    head_before = (await run_in_repository_thread(repo.session_status, "proj-mem", "sess-mem")).head_commit
    session = SimpleNamespace(session_id="sess-mem", git_head_commit_sha=head_before)
    project = SimpleNamespace(id="proj-mem")
    flushed = await _flush_curator_memory_if_dirty(
        repo, project=project, session=session, run_id="run-x", worktree_root=root
    )
    assert flushed is True
    assert _memory_md_dirty(root) is False, "MEMORY.md 应已被 curator 提交，worktree 干净"
    assert session.git_head_commit_sha and session.git_head_commit_sha != head_before


async def test_flush_tolerates_checkpoint_authority_mismatch():
    """④ 健壮性：worktree git head 与 DB 记录分歧时，checkpoint 权威 fail-closed
    抛 ProjectRepositoryError —— flush 必须**吞掉、返 False**，不带塌整条对账。

    首次部署真事故：一个 session 的 head 是平台旧提交、DB 记录没跟上，flush 的
    checkpoint 抛权威分歧 → 未捕获 → 冒泡到对账循环 → except 里取 run.id 又二次崩
    （ORM 过期 lazy-load）。这里钉住 flush 自己不抛。变异（去掉 try/except）→ 转红。
    """
    from types import SimpleNamespace

    from app.services.deliverable_publishing import (
        _flush_curator_memory_if_dirty,
        _memory_md_dirty,
    )
    from app.services.project_repository import get_project_repository

    repo = get_project_repository()
    (await run_in_repository_thread(repo.initialize_project, 
        project_id="proj-auth", name="Auth", description=None,
        research_domain=None, owner_id="owner",
    ))
    (await run_in_repository_thread(repo.ensure_session_workspace, 
        project_id="proj-auth", session_id="sess-auth", base_commit=None,
        title="Auth", created_by="owner",
    ))
    root = Path((await run_in_repository_thread(repo.session_status, "proj-auth", "sess-auth")).path)
    (root / "MEMORY.md").write_text("# memory\n\n脏\n", encoding="utf-8")
    assert _memory_md_dirty(root) is True

    # 故意给一个**错的** expected head → checkpoint_session_workspace 判权威分歧抛错
    session = SimpleNamespace(session_id="sess-auth", git_head_commit_sha="0" * 40)
    project = SimpleNamespace(id="proj-auth")
    flushed = await _flush_curator_memory_if_dirty(
        repo, project=project, session=session, run_id="run-x", worktree_root=root
    )
    assert flushed is False, "权威分歧应被吞掉、返 False，而不是抛出"
    assert _memory_md_dirty(root) is True, "没 flush 成 → MEMORY.md 仍脏（如实）"


async def test_deliver_completed_run_is_idempotent_from_git(db_session, monkeypatch):
    """③ 幂等：git 上已经有带 `Delivered-Run` trailer 的提交 → 不重复发。

    判据从**表**搬到了 **git**（RFC X1 第一步）：manifest 里写着 authority: git，
    `project_revisions` 是投影。判据建在投影上，分叉时不报错 —— 只会悄悄重复
    交付，或者永不交付。

    检查在最前、早于任何 worktree/state_root 访问，所以给个不存在的 state_root
    也不该被碰。变异（去掉这一步）→ 往下走会碰不存在的 session → 断言转红。
    """
    from types import SimpleNamespace

    from app.models.project import Project
    from app.services import deliverable_publishing, project_repository

    pid = "proj-idem"
    db_session.add(Project(id=pid, name="Idem", owner_id="owner"))
    await db_session.commit()

    asked: list[tuple[str, str, str]] = []

    class _Repo:
        def commit_with_trailer(self, project_id, trailer, value):
            asked.append((project_id, trailer, value))
            return "cafebabe" if value == "run-done" else None

    monkeypatch.setattr(project_repository, "get_project_repository", lambda: _Repo())

    project = SimpleNamespace(id=pid)
    session = SimpleNamespace(session_id="sess-idem", git_head_commit_sha="")
    user = SimpleNamespace(id="user-idem")
    outcome = await deliverable_publishing.deliver_completed_run(
        db_session, run_id="run-done", project=project, session=session,
        user=user, state_root=Path("/nonexistent-state-root"),
    )
    assert outcome is None
    assert asked == [(pid, deliverable_publishing.DELIVERY_TRAILER, "run-done")], asked
