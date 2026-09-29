"""项目级待办不能让一个全新会话开局先去替别人打扫（#952）。

真实运行实测：一次全新会话的 experiment run，在完成用户交给它的任务之余，顺手收尾了
**另外两个会话**留下的未收尾外部作业（一个两天前、一个昨天）。节点的处理是诚实的
（一个按机械证据关成失败，一个关成受阻，都没伪造成功）——**它不是记错了账，是被
要求去做这件事**：开局上下文里写着"你 own 的 pending（2）"，逐条列着那两个作业。

任务清单是**项目级**的，而会话是人交代一件事的边界。两件事压在同一节里，模型眼里
就只有一份待办清单。这里不改框架行为（不删、不拦、不过期），只把它们在文本上分开
并写明归属 —— 判据是**出生会话**，不是标题或年龄猜测。
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.context_engine import _render_task_injection
from core.state import State
from core.tasks import TaskList


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "tasks").mkdir(parents=True)
    return root


def _state(project_root: Path, session_id: str) -> State:
    bootstrap()
    st = State.new(node_type="experiment", base_dir=project_root / "runs", project_id="p")
    st.project_root = project_root
    st.session_id = session_id
    return st


def _seed(project_root: Path, *, session_id: str, title: str, days_old: int = 0):
    tl = TaskList(project_root / "tasks")
    task = tl.create(title=title, description="作业身份 hf-job-…",
                     owner_node="experiment", run_id="run_x", session_id=session_id)
    if days_old:
        task.created_at = (
            datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
        tl._persist(task)
    return task


def test_another_sessions_chore_is_not_listed_as_yours(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _seed(root, session_id="session-A", title="Finalize external job", days_old=2)
    _seed(root, session_id="session-B", title="跑本轮的收敛检查")

    text = _render_task_injection(_state(root, "session-B"), "experiment")

    assert "你 own 的 pending（1）" in text, (
        f"本会话自己的待办数不对 —— 历史待办被算进来了：\n{text}")
    assert "历史待办" in text and "不是这一轮的活" in text
    assert "2 天前写下的" in text, "年龄看不见：挂了两天的和刚写下的读起来一样"
    assert "另一个会话" in text
    assert "不要顺手去做" in text


def test_your_own_sessions_chores_stay_where_they_were(tmp_path: Path) -> None:
    """对照：本会话自己的开放作业照旧要办 —— 这条修复不能把它们也推开。"""
    root = _project(tmp_path)
    _seed(root, session_id="session-B", title="Finalize external job")

    text = _render_task_injection(_state(root, "session-B"), "experiment")
    assert "你 own 的 pending（1）" in text
    assert "历史待办" not in text


def test_without_a_session_identity_nothing_is_split(tmp_path: Path) -> None:
    """CLI / 教学版没有会话身份 —— 分不出来就别假装分得出。"""
    root = _project(tmp_path)
    _seed(root, session_id="", title="Finalize external job")

    st = _state(root, "")
    text = _render_task_injection(st, "experiment")
    assert "你 own 的 pending（1）" in text
    assert "历史待办" not in text


def test_a_legacy_task_without_a_session_is_not_claimed_as_yours(tmp_path: Path) -> None:
    """老记录没有 session id → 按来源不明处理，不冒充本会话的。"""
    root = _project(tmp_path)
    _seed(root, session_id="", title="Finalize external job", days_old=5)

    text = _render_task_injection(_state(root, "session-B"), "experiment")
    assert "历史待办" in text
    assert "5 天前写下的" in text


def test_the_birth_session_is_recorded_at_creation(tmp_path: Path) -> None:
    """判据得有来源 —— 待办出生时就记下它属于哪个会话。"""
    root = _project(tmp_path)
    task = _seed(root, session_id="session-A", title="Finalize external job")
    assert task.created_in_session_id == "session-A"
    reloaded = TaskList(root / "tasks").get(task.id)
    assert reloaded.created_in_session_id == "session-A", "落盘之后丢了"
