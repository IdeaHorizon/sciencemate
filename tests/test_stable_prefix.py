"""system_prompt 的稳定前缀。

缓存在**第一个变化的字节**处断掉。所以这一层的判据只有一条：
**连续两轮构建，边界之前逐字节相同** —— 否则前缀白设。
"""
from __future__ import annotations

import pytest

from core.context_engine import (
    PREFIX_BOUNDARY,
    _build_system_prompt,
    _frozen,
    stable_prefix_enabled,
)


class _Harness:
    node_type = "writing"
    system_prompt = "你是 writing 节点。"
    rules = ["规则一"]
    guidelines = ["建议一"]
    skills: list = []
    expected_outputs: dict = {}


class _State:
    """够 _build_system_prompt 走完的最小 state。"""

    def __init__(self, tmp_path):
        self.project_root = tmp_path
        self.project_worktree = None
        self.workspace_root = tmp_path / "ws"
        self.hook_state: dict = {}
        self.run_id = "r1"


def _prefix_of(prompt: str) -> str:
    return prompt.split(PREFIX_BOUNDARY, 1)[0]


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("HARNESS_STABLE_PREFIX", raising=False)
    assert stable_prefix_enabled() is True
    monkeypatch.setenv("HARNESS_STABLE_PREFIX", "off")
    assert stable_prefix_enabled() is False
    monkeypatch.setenv("HARNESS_STABLE_PREFIX", "on")
    assert stable_prefix_enabled() is True


def test_frozen_computes_once_per_run(tmp_path):
    """状态文件 run 开始时算一次就冻住 —— 中途重算就等于前缀每轮变。"""
    st = _State(tmp_path)
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return f"value-{calls['n']}"

    assert _frozen(st, "k", compute) == "value-1"
    assert _frozen(st, "k", compute) == "value-1"     # 第二轮不重算
    assert _frozen(st, "k", compute) == "value-1"
    assert calls["n"] == 1

    # 不同 key 互不影响
    assert _frozen(st, "other", compute) == "value-2"


def test_frozen_without_state_still_works():
    """state=None（调试 / 单测路径）不能炸，只是不冻。"""
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    assert _frozen(None, "k", compute) == 1
    assert _frozen(None, "k", compute) == 2           # 没地方存，每次都算


def test_frozen_survives_state_without_hook_state():
    class Bare:
        pass

    assert _frozen(Bare(), "k", lambda: "ok") == "ok"


def test_prefix_is_byte_identical_across_turns(tmp_path):
    """核心不变量。"""
    st = _State(tmp_path)
    h = _Harness()

    p1 = _build_system_prompt(h, st)
    p2 = _build_system_prompt(h, st)
    p3 = _build_system_prompt(h, st)

    assert _prefix_of(p1) == _prefix_of(p2) == _prefix_of(p3)


def test_memory_written_midrun_does_not_move_the_prefix(tmp_path):
    """run 中途改 MEMORY.md：本 run 的 prompt 不变（下个 run 才拿到新快照）。

    这是拿"本 run 内的新鲜度"换"整段前缀可复用" —— 工具读到的仍是最新值。
    """
    from core import memory as M

    st = _State(tmp_path)
    st.project_worktree = tmp_path
    M.ensure_skeleton(st)
    M.append_law(st, text="第一版铁律：修复要改产生问题的那一层", derived_from=["x"])

    h = _Harness()
    p1 = _build_system_prompt(h, st)
    assert "第一版铁律" in p1

    M.append_law(st, text="第二版铁律 —— 中途写入", derived_from=["y"])
    p2 = _build_system_prompt(h, st)

    assert "第二版铁律" not in p2          # 本 run 不刷新
    assert _prefix_of(p1) == _prefix_of(p2)

    # 新 run（新 state）拿到新快照
    st2 = _State(tmp_path)
    st2.project_worktree = tmp_path
    assert "第二版铁律" in _build_system_prompt(h, st2)


def test_boundary_absent_when_nothing_is_volatile(tmp_path):
    """没有可变段就不该凭空多出一行标记。"""
    st = _State(tmp_path)
    st.project_root = None
    out = _build_system_prompt(_Harness(), st)
    assert PREFIX_BOUNDARY not in out


def test_kill_switch_removes_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STABLE_PREFIX", "off")
    st = _State(tmp_path)
    st.project_worktree = tmp_path
    (tmp_path / "MEMORY.md").write_text("m", encoding="utf-8")
    out = _build_system_prompt(_Harness(), st)
    assert PREFIX_BOUNDARY not in out


def test_user_intake_still_precedes_directives(tmp_path, monkeypatch):
    """既有权威顺序不能被重排打破：用户原文在 PROFILE/PROJECT 之前。

    三轮实测漂移的主信道是转述链；这条顺序是拿事故换来的，重排时必须守住。
    """
    import core.context_engine as ce

    monkeypatch.setattr(ce, "render_intake_section", None, raising=False)

    st = _State(tmp_path)
    h = _Harness()

    def fake_intake(_root):
        return "## 用户原始输入\nINTAKE_MARKER"

    def fake_directives(_state, _node):
        # 替身的形状要和真的一样：organization 层和 snapshot_sha256 随
        # RFC X3 一起删了，留着它们等于用一个不存在的契约测被测实现。
        return {"profile": "PROFILE_MARKER", "project": "",
                "digests": {"personal": None, "project": None}}

    import core.research_intake as ri
    import core.directives_loader as dl
    monkeypatch.setattr(ri, "render_intake_section", fake_intake)
    monkeypatch.setattr(dl, "load_directives_for_node", fake_directives)

    out = _build_system_prompt(h, st)
    if "INTAKE_MARKER" in out and "PROFILE_MARKER" in out:
        assert out.index("INTAKE_MARKER") < out.index("PROFILE_MARKER")


def test_directives_are_not_frozen(tmp_path, monkeypatch):
    """directives 不能被冻结。

    它们每轮从同一批文件读出来 —— 没改就字节相同（不破前缀），改了就该下一轮
    生效。把它冻进缓存只会端出陈旧值。
    （test_profile_project::test_changing_the_file_changes_the_next_turn 钉的是
    同一件事的另一半：改了文件，下一轮真的变。）
    """
    import core.directives_loader as dl

    seen = {"n": 0}

    def fake(_state, _node):
        seen["n"] += 1
        return {"profile": f"P{seen['n']}", "project": "",
                "digests": {"personal": None, "project": None}}

    monkeypatch.setattr(dl, "load_directives_for_node", fake)

    st = _State(tmp_path)
    h = _Harness()
    out1 = _build_system_prompt(h, st)
    out2 = _build_system_prompt(h, st)

    assert "P1" in out1
    assert "P2" in out2, "directives 被冻住了，快照重绑将端出陈旧值"
