"""`_submit_sync` 的 stage_in / state 必须由调用方**显式**给出（issue #791）。

## 现场（2026-09-04 node20 部署门，release 18803f2）

97 条沙箱门测试 6 红，同一 reason `local_submission_state_missing`。`_submit_sync`
新加的本地提交闸读第 24 个位置参数 `state`（默认 None），而唯一的生产调用点
`_submit_job` 给了 22 个位置实参 + 7 个关键字，没有一个是 state → 闸恒真 →
**每一次真实的本地作业提交都被拒**。同一处还漏了第 23 个 `stage_in`：`_submit_job`
拿 `validated_stage_in` 做了沙箱挂载与 payload 预检，提交时却没传下去 —— 预检
按"它们会就位"放行，作业跑起来 stage-in 的文件不在。这一个此前被前一个缺陷
挡着没显形。

CI 只跑 PR 分支的单测，直接调 `_submit_sync(state=state)` 的测试自己是绿的，
缺陷带着绿 CI 进了 main，部署时才被门拦下。

## 判据

默认值就是"漏传能沉默"的原因。两个参数改成 keyword-only 且必填：漏传在调用
那一刻就是 TypeError（任何一条走到这里的测试都会红），不是运行期一个理由指错
的 error dict。这条测试钉三件事：签名、生产调用点、老写法确实会响。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from nodes.experiment.tools import resource_manager as rm

SOURCE = Path(rm.__file__).read_text(encoding="utf-8")


def test_stage_in_and_state_are_keyword_only_and_required():
    params = inspect.signature(rm._submit_sync).parameters
    for name in ("stage_in", "state"):
        param = params[name]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} 必须是 keyword-only"
        assert param.default is inspect.Parameter.empty, (
            f"{name} 不许有默认值 —— 默认值就是 09-04 那次漏传能沉默的原因"
        )


def _production_call_sites() -> list[ast.Call]:
    tree = ast.parse(SOURCE)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if name == "_submit_sync":
                calls.append(node)
    return calls


def test_the_production_call_site_passes_both_by_keyword():
    calls = _production_call_sites()
    assert len(calls) == 1, "resource_manager 里 _submit_sync 应只有 _submit_job 这一个调用点"
    keywords = {kw.arg for kw in calls[0].keywords}
    assert {"stage_in", "state"} <= keywords, (
        f"生产调用点没有把两者按关键字传下去：{sorted(keywords)}"
    )
    stage_in_value = next(kw.value for kw in calls[0].keywords if kw.arg == "stage_in")
    assert isinstance(stage_in_value, ast.Name) and stage_in_value.id == "validated_stage_in", (
        "提交必须带预检用过的那份 validated_stage_in，不是别的什么"
    )


def test_the_old_call_shape_is_a_type_error_not_a_silent_rejection(tmp_path):
    """09-04 那种写法（位置实参到 health_check，然后只给后面的关键字）现在必须
    在调用那一刻就炸，而不是返回一个 reason 指错的 error dict。"""
    with pytest.raises(TypeError):
        rm._submit_sync(
            tmp_path, "local", "echo hi", "old-shape",
            1, 1, 0, 1.0, 8.0, 1, None, None, None, str(tmp_path), True, None,
            None, None, None, None, None, None,
            guard_process_tree=False,
        )
