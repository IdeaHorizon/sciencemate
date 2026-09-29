"""v2.0：TaskList 单元 + dispatcher 集成测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.tasks import Task, TaskList, TaskListError


# ─────────────────────────────────────────────────────────────────────────────
# Task dataclass
# ─────────────────────────────────────────────────────────────────────────────


def test_task_to_from_dict_roundtrip():
    t = Task(id="T01", title="x", description="y", owner_node="experiment",
              created_by_run_id="r1")
    d = t.to_dict()
    t2 = Task.from_dict(d)
    assert t2 == t


def test_task_from_dict_ignores_unknown_fields():
    d = {"id": "T01", "title": "x", "owner_node": "literature",
          "unknown_field": "x"}
    t = Task.from_dict(d)
    assert t.id == "T01"


# ─────────────────────────────────────────────────────────────────────────────
# TaskList CRUD
# ─────────────────────────────────────────────────────────────────────────────


def _make_tl(tmp_path: Path) -> TaskList:
    return TaskList(tmp_path / "tasks")


def test_taskList_create_assigns_t01(tmp_path):
    tl = _make_tl(tmp_path)
    t = tl.create(title="first", description="d",
                   owner_node="literature", run_id="r1")
    assert t.id == "T01"
    assert t.status == "pending"


def test_taskList_create_increments_id(tmp_path):
    tl = _make_tl(tmp_path)
    a = tl.create(title="a", description="", owner_node="literature", run_id="r1")
    b = tl.create(title="b", description="", owner_node="literature", run_id="r1")
    c = tl.create(title="c", description="", owner_node="literature", run_id="r1")
    assert (a.id, b.id, c.id) == ("T01", "T02", "T03")


def test_taskList_persist_across_instances(tmp_path):
    tl1 = _make_tl(tmp_path)
    tl1.create(title="x", description="", owner_node="literature", run_id="r1")
    # 新 instance 读同一目录
    tl2 = _make_tl(tmp_path)
    all_tasks = tl2.list_all()
    assert len(all_tasks) == 1
    assert all_tasks[0].title == "x"


def test_taskList_create_empty_title_records_placeholder(tmp_path):
    """判决拆除：空 title 不再拒绝——如实落「(未命名)」（空值仪式删）。"""
    tl = _make_tl(tmp_path)
    t = tl.create(title="  ", description="", owner_node="x", run_id="r1")
    assert t.title == "(未命名)"


def test_taskList_create_with_unknown_parent_raises(tmp_path):
    tl = _make_tl(tmp_path)
    with pytest.raises(TaskListError, match="parent_id"):
        tl.create(title="x", description="", owner_node="x", run_id="r1",
                    parent_id="T99")


def test_taskList_create_under_completed_parent_reopens_it(tmp_path):
    """判决拆除：发现还有子活=父其实没完——如实重开并留痕，见证不禁令。"""
    tl = _make_tl(tmp_path)
    p = tl.create(title="parent", description="", owner_node="x", run_id="r1")
    tl.start(p.id, owner_node="x")
    tl.complete(p.id)
    tl.create(title="child", description="", owner_node="x", run_id="r1",
              parent_id=p.id)
    parent = tl.get(p.id)
    assert parent.status == "in_progress"
    assert any("重开" in n for n in parent.notes)


# ─────────────────────────────────────────────────────────────────────────────
# Hard limit: max 1 in_progress per owner_node
# ─────────────────────────────────────────────────────────────────────────────


def test_same_owner_may_run_two_tasks(tmp_path):
    """判决拆除：≤1 in_progress 是任意阈值——并行推进账仍真，专注与否归模型判。"""
    tl = _make_tl(tmp_path)
    a = tl.create(title="a", description="", owner_node="literature", run_id="r1")
    b = tl.create(title="b", description="", owner_node="literature", run_id="r1")
    tl.start(a.id, owner_node="literature")
    tl.start(b.id, owner_node="literature")
    assert len(tl.filter(status="in_progress", owner_node="literature")) == 2


def test_hard_limit_different_owner_ok(tmp_path):
    """不同 owner_node 各 1 in_progress 互不冲突。"""
    tl = _make_tl(tmp_path)
    a = tl.create(title="a", description="", owner_node="literature", run_id="r1")
    b = tl.create(title="b", description="", owner_node="experiment", run_id="r1")
    tl.start(a.id, owner_node="literature")
    tl.start(b.id, owner_node="experiment")    # 不抛
    in_progress = tl.filter(status="in_progress")
    assert len(in_progress) == 2


def test_start_after_complete_allows_new_start(tmp_path):
    tl = _make_tl(tmp_path)
    a = tl.create(title="a", description="", owner_node="x", run_id="r1")
    b = tl.create(title="b", description="", owner_node="x", run_id="r1")
    tl.start(a.id, owner_node="x")
    tl.complete(a.id)
    tl.start(b.id, owner_node="x")           # 不抛
    assert tl.get(b.id).status == "in_progress"


def test_start_after_block_allows_new_start(tmp_path):
    tl = _make_tl(tmp_path)
    a = tl.create(title="a", description="", owner_node="x", run_id="r1")
    b = tl.create(title="b", description="", owner_node="x", run_id="r1")
    tl.start(a.id, owner_node="x")
    tl.block(a.id, reason="GPU 配额满了无法继续")
    tl.start(b.id, owner_node="x")           # 不抛（a 已 blocked，不算 in_progress）
    assert tl.get(b.id).status == "in_progress"


# ─────────────────────────────────────────────────────────────────────────────
# 状态转换
# ─────────────────────────────────────────────────────────────────────────────


def test_complete_idempotent(tmp_path):
    tl = _make_tl(tmp_path)
    t = tl.create(title="x", description="", owner_node="x", run_id="r1")
    tl.start(t.id, owner_node="x")
    tl.complete(t.id)
    tl.complete(t.id)    # 不抛
    assert tl.get(t.id).status == "completed"


def test_start_completed_reopens_with_trail(tmp_path):
    """判决拆除：「发现还有活要干」是正常科学修正——reopen 留痕。"""
    tl = _make_tl(tmp_path)
    t = tl.create(title="x", description="", owner_node="x", run_id="r1")
    tl.start(t.id, owner_node="x")
    tl.complete(t.id)
    tl.start(t.id, owner_node="x")
    got = tl.get(t.id)
    assert got.status == "in_progress"
    assert any("reopen" in n for n in got.notes)


def test_block_reason_is_semantic_not_length(tmp_path):
    """判决拆除：字数闸删——短理由放行（原样记录），空理由仍拒（语义必需）。"""
    tl = _make_tl(tmp_path)
    t = tl.create(title="x", description="", owner_node="x", run_id="r1")
    tl.start(t.id, owner_node="x")
    tl.block(t.id, reason="short")
    assert tl.get(t.id).blocked_reason == "short"
    t2 = tl.create(title="y", description="", owner_node="x", run_id="r1")
    tl.start(t2.id, owner_node="x")
    with pytest.raises(TaskListError, match="reason"):
        tl.block(t2.id, reason="   ")


def test_block_completed_reopens_with_trail(tmp_path):
    """判决拆除：矛盾迁移记为 reopen 比拒绝更真。"""
    tl = _make_tl(tmp_path)
    t = tl.create(title="x", description="", owner_node="x", run_id="r1")
    tl.start(t.id, owner_node="x")
    tl.complete(t.id)
    tl.block(t.id, reason="this is a valid block reason")
    got = tl.get(t.id)
    assert got.status == "blocked"
    assert any("reopen" in n for n in got.notes)


def test_unblock_restores_pending(tmp_path):
    tl = _make_tl(tmp_path)
    t = tl.create(title="x", description="", owner_node="x", run_id="r1")
    tl.start(t.id, owner_node="x")
    tl.block(t.id, reason="GPU quota exhausted")
    tl.unblock(t.id)
    assert tl.get(t.id).status == "pending"
    assert tl.get(t.id).blocked_reason is None


def test_start_blocked_implicitly_unblocks_with_trail(tmp_path):
    """判决拆除：start 一个 blocked task=显然想解锁——隐式解锁留痕，免两次调用仪式。"""
    tl = _make_tl(tmp_path)
    t = tl.create(title="x", description="", owner_node="x", run_id="r1")
    tl.start(t.id, owner_node="x")
    tl.block(t.id, reason="some reason here")
    tl.start(t.id, owner_node="x")
    got = tl.get(t.id)
    assert got.status == "in_progress"
    assert got.blocked_reason is None
    assert any("unblock" in n for n in got.notes)


# ─────────────────────────────────────────────────────────────────────────────
# Markdown 视图
# ─────────────────────────────────────────────────────────────────────────────


def test_active_md_rendered(tmp_path):
    tl = _make_tl(tmp_path)
    tl.create(title="任务 A", description="", owner_node="experiment", run_id="r1")
    tl.create(title="任务 B", description="", owner_node="experiment", run_id="r1")
    md = tl.active_md_path.read_text(encoding="utf-8")
    assert "Pending" in md
    assert "任务 A" in md
    assert "任务 B" in md


def test_active_md_shows_in_progress_section(tmp_path):
    tl = _make_tl(tmp_path)
    t = tl.create(title="跑 ablation", description="",
                    owner_node="experiment", run_id="r1")
    tl.start(t.id, owner_node="experiment")
    md = tl.active_md_path.read_text(encoding="utf-8")
    assert "In progress" in md
    assert "跑 ablation" in md
    assert "experiment" in md


def test_completed_md_audit_trail(tmp_path):
    tl = _make_tl(tmp_path)
    t = tl.create(title="done thing", description="d",
                    owner_node="x", run_id="r1")
    tl.start(t.id, owner_node="x")
    tl.complete(t.id, notes="finished nicely")
    md = tl.completed_md_path.read_text(encoding="utf-8")
    assert "done thing" in md
    assert "finished nicely" in md


# ─────────────────────────────────────────────────────────────────────────────
# task dispatcher 工具
# ─────────────────────────────────────────────────────────────────────────────


def _make_state_with_project(tmp_path: Path):
    from core.state import State
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    # State.new 期望 base_dir + 可选 project_id；但 project_root 是 derived from project_id
    # 这里手工构造一个含 project_root 的 State
    s = State.new(node_type="literature", base_dir=tmp_path / "runs",
                   project_id="test_proj")
    # State.new 走 _project_root('test_proj') 路径，会自动建 ~/.harness-framework/projects/test_proj/
    return s


@pytest.mark.asyncio
async def test_task_tool_create_then_list(tmp_path, monkeypatch):
    """通过 tool registry 调用：create + list。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf_home"))
    state = _make_state_with_project(tmp_path)

    r = await execute_tool(
        "task", state, action="create",
        title="先做 X", description="先调研一下",
    )
    assert r["status"] == "success"
    assert r["task"]["status"] == "pending"
    tid = r["task"]["id"]

    r2 = await execute_tool("task", state, action="list", filter="pending")
    assert r2["status"] == "success"
    assert r2["count"] == 1
    assert r2["tasks"][0]["id"] == tid


@pytest.mark.asyncio
async def test_task_tool_hard_limit_enforced(tmp_path, monkeypatch):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf_home"))
    state = _make_state_with_project(tmp_path)

    a = await execute_tool("task", state, action="create", title="A")
    b = await execute_tool("task", state, action="create", title="B")
    await execute_tool("task", state, action="start", task_id=a["task"]["id"])
    res = await execute_tool("task", state, action="start", task_id=b["task"]["id"])
    assert res["status"] == "success"      # 判决拆除：并行 in_progress 合法
    assert res["task"]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_task_tool_unknown_action(tmp_path, monkeypatch):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf_home"))
    state = _make_state_with_project(tmp_path)
    res = await execute_tool("task", state, action="bogus")
    # 契约归 schema：action 的 enum 在 parameters_schema，派发口核一次并把
    # 合法值写进报错；工具体内不再手写 available_actions。
    assert res["status"] == "error"
    assert res["parameter_violations"]
    assert "create" in res["error"] and "complete" in res["error"]


@pytest.mark.asyncio
async def test_task_tool_empty_title_lands_as_unnamed(tmp_path, monkeypatch):
    """判决拆除：工具层不再重立「空 title 拒绝」——交 core/tasks 如实记「(未命名)」。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf_home"))
    state = _make_state_with_project(tmp_path)
    res = await execute_tool("task", state, action="create", title="   ")
    assert res["status"] == "success", res
    assert res["task"]["title"] == "(未命名)"


@pytest.mark.asyncio
async def test_task_tool_requires_task_id_once_for_every_targeted_action(tmp_path, monkeypatch):
    """条件必填只在一处查：create / list 之外的 action 缺 task_id 都报同一句。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf_home"))
    state = _make_state_with_project(tmp_path)
    for action in ("start", "complete", "block", "unblock", "get"):
        res = await execute_tool("task", state, action=action)
        assert res["status"] == "error" and "task_id" in res["error"], (action, res)


@pytest.mark.asyncio
async def test_task_tool_no_project_root_errors(tmp_path):
    """无 project_id 起 State → task 工具拒绝。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    from core.state import State
    state = State.new(node_type="literature", base_dir=tmp_path)
    # 注意：不传 project_id → project_root=None
    assert state.project_root is None
    res = await execute_tool("task", state, action="create", title="x")
    assert res["status"] == "error"
    assert "project_id" in res["error"]
