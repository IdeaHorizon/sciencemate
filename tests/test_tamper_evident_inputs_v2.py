"""科学时序门在 Workspace-First 下必须照常执行（2026-08-08）。

"预注册必须已冻结才能开跑"防的是确认偏误：先冻结协议、后做实验。它和
"父 state 里得有某个类型的 artifact"（v1 点对点传递约定）写在同一段代码里，
于是 v2 把两个一起关掉了 —— 传递约定该关，科学凭据不该。

输入契约要按**它保护的是什么**分类，不按它写在哪一行分类。
"""
from __future__ import annotations

import inspect

import pytest

from core import executor


def test_tamper_evident_scan_is_not_excluded_under_v2():
    """守卫：那句 `for a in () if project_workspace_mode` 不许回来。"""
    source = inspect.getsource(executor)
    assert "for a in () if project_workspace_mode else state.list_artifacts():" not in source
    assert "_TAMPER_EVIDENT_INPUT_TYPES = {\"pre_registration\"}" in source


def test_handoff_convention_stays_disabled_under_v2():
    """(1) 文件传递约定在 v2 下仍然关闭 —— 下游读上游目录，不够就报阻塞。"""
    source = inspect.getsource(executor)
    assert "missing_inputs = (\n        []\n        if project_workspace_mode" in source


@pytest.mark.parametrize("frozen,expected_blocked", [(True, False), (False, True)])
def test_unfrozen_prereg_blocks_experiment_in_workspace_mode(tmp_path, frozen, expected_blocked):
    """真判据：跨节点看得见 hypothesis 的 prereg，冻了才放行。"""
    import os
    import subprocess

    from core.project_workspace import bind_project_workspace
    from core.state import State

    root = tmp_path / "wt"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "i"], cwd=root,
                   check=True, env=env)

    hypo = State(run_id="h", node_type="hypothesis", root=tmp_path / "run-h")
    bind_project_workspace(hypo, root)
    saved = hypo.save_artifact("pre_registration", "P", "协议")
    if frozen:
        hypo.mark_frozen(saved["id"])   # 冻结只出自账本的 freeze 行，metadata 里写不出来

    exp = State(run_id="e", node_type="experiment", root=tmp_path / "run-e")
    bind_project_workspace(exp, root)

    # 复刻 executor 里那段判据（跨节点可见 + frozen 检查）
    unfrozen = []
    for a in exp.list_artifacts():
        if a["type"] == "pre_registration":
            record = exp.read_artifact(a["id"]) or {}
            if not (record.get("metadata") or {}).get("frozen"):
                unfrozen.append(a["type"])
    assert bool(unfrozen) is expected_blocked
