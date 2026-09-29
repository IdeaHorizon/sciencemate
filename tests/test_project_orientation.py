"""定向层：全局可读 != 全局了解（v2.1）。

实测反复证明"只要还需要模型主动去查，它就有一半概率不查"。本 hook 把项目
现状（各节点交付面 / research_state 摘要 / MEMORY.md 头）开局机械注入给每个
绑定 worktree 的节点 —— 不是让它自己去翻。
"""
from __future__ import annotations

import os
import subprocess

from core.ledger import write_record
from core.loop_hooks_builtin import _project_orientation_on_turn_start
from core.state import State


class _Ctx:
    def __init__(self, state, turn=1):
        self.state = state
        self.turn = turn
        self.harness = None


def _worktree(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "i"], cwd=root,
                   check=True, env=env)
    return root


def _state(tmp_path, root, node_type="experiment"):
    from core.project_workspace import bind_project_workspace

    run_root = tmp_path / f"run-{node_type}"
    run_root.mkdir(parents=True, exist_ok=True)
    state = State(run_id=f"r-{node_type}", node_type=node_type, root=run_root)
    bind_project_workspace(state, root)
    return state


def test_orientation_shows_deliveries_state_and_memory(tmp_path):
    root = _worktree(tmp_path)
    hypo = _state(tmp_path, root, "hypothesis")
    prereg = hypo.save_artifact("pre_registration", "P", "协议", metadata={})
    hypo.mark_frozen(prereg["id"])
    # research_state 是 typed-only 类型：夹具不经 State，直接落工作区账本。
    write_record(root, artifact_type="research_state", name="research_state",
                 content="# research state v2", directory="plan",
                 metadata={"version": 2, "verdict": "continue",
                           "hypotheses": [{"id": "H1", "status": "active"},
                                          {"id": "H2", "status": "refuted",
                                           "evidence": ["e"]}]},
                 produced_by_node_type="hypothesis", produced_by_run_id=hypo.run_id)
    (root / "MEMORY.md").write_text("# Project Memory\n\n## Decisions\n- [D-001] 用 LJ 体系\n",
                                    encoding="utf-8")

    out = _project_orientation_on_turn_start(_Ctx(_state(tmp_path, root, "experiment")))
    assert out is not None
    body = out[0].content
    assert "plan/" in body and "pre_registration__P" in body      # 交付面
    assert "research_state v2" in body and "H1" in body and "H2" not in body.split("未裁决")[1][:20]  # 只列未裁决
    assert "D-001" in body                                              # MEMORY 头


def test_injected_once_per_state(tmp_path):
    root = _worktree(tmp_path)
    hypo = _state(tmp_path, root, "hypothesis")
    hypo.save_artifact("pre_registration", "P", "x", metadata={})
    state = _state(tmp_path, root, "writing")
    assert _project_orientation_on_turn_start(_Ctx(state)) is not None
    assert _project_orientation_on_turn_start(_Ctx(state)) is None


def test_silent_without_worktree(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    state = State(run_id="r", node_type="experiment", root=run_root)
    assert _project_orientation_on_turn_start(_Ctx(state)) is None


def test_empty_project_injects_nothing(tmp_path):
    """画图/调研一次性任务的开局：没内容的定向是噪音，整个不注入。"""
    root = _worktree(tmp_path)
    assert _project_orientation_on_turn_start(_Ctx(_state(tmp_path, root, "hypothesis"))) is None


def test_node_readme_first_line_is_amplified(tmp_path):
    """节点自愿维护的 README —— 写了就会被带给所有后续节点（可见性激励）。"""
    root = _worktree(tmp_path)
    lit = _state(tmp_path, root, "literature")
    lit.save_artifact("survey_report", "s", "body", {})
    (root / "literature" / "README.md").write_text(
        "# literature\n\n定向查证 MLIP 外推可靠性：5 篇已核验，2 篇全文未取到（见 index）。\n",
        encoding="utf-8")
    out = _project_orientation_on_turn_start(_Ctx(_state(tmp_path, root, "hypothesis")))
    assert out is not None
    assert "自述：定向查证 MLIP 外推可靠性" in out[0].content


def test_every_node_harness_enables_it():
    """扫盘不变量：新节点接入时默认获得定向层，漏了就红。

    唯一的例外是**机械算出来的**，不是名单：自己换掉框架 loop 的节点。
    `project_orientation` 靠 `on_turn_start` 往对话里注入消息，而 loop hook 只由
    `core.agent_loop.run_loop` 触发 —— executor 对 custom loop 是直接 dispatch
    的（`core/executor.py` 的 custom 分支），那条路上一个 hook 都不会跑。在那种
    节点上写 `loop_hooks: [project_orientation]` 得到的不是定向层，是一行永不
    生效的配置（而配置一旦从不生效，它就不是事实）。

    判据用 `runs_the_framework_loop()` 现算：谁哪天把 loop 换回框架的，这条当天就重新
    对它生效，不需要有人记得回来改名单。另一半在
    `test_custom_loop_declares_no_inert_hooks.py`：换掉 loop 的节点不许再声明
    hook —— 否则这里的豁免就成了一个可以偷偷躺着假配置的洞。
    """
    from core.custom_loop import runs_the_framework_loop
    from core.loader import list_harnesses, load_harness

    missing = [n for n in list_harnesses()
               if runs_the_framework_loop(n)
               and "project_orientation" not in (load_harness(n).loop_hooks or [])]
    assert not missing, f"这些节点没接定向层：{missing}"
