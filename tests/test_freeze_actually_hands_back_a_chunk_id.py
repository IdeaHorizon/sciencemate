"""freeze 必须真的交出一个能用的 chunk_id —— 不只是"走到了那个分支"。

## 为什么单独写这条

`create_experiment(frozen_log_chunk_id=…)` 是必填的，而它此前在注册面上没有
任何合法产生路径（指路的工具已下架）。补法是让 freeze 按产物策略自动登记。

第一版**是坏的**，而既有测试全绿：我只测了 `_should_auto_chunk()` 的类型路由
（experiment_log 该登记、manuscript 不该），从没真调过登记函数本身。而登记里
传的 `created_by_role="freeze_auto"` 根本不在 `CREATED_BY_ROLES` 里，schema
逐字校验 → 自动登记**每次都失败**，chunk_id 一次都没交出去过。

是 2026-08-22 的真 E2E 抓到的：返回值里带着
`chunk_registration_error: created_by_role='freeze_auto' 不合法`。

教训是老的那条：**测了分支，不等于测了那一步真的成立**。所以这条测试打的是
出口而不是路径 —— 冻一份真的 experiment_log，然后要求 `chunk_id` 在返回值里、
且它在 KB 里真的能查到。
"""
from __future__ import annotations

import asyncio

import pytest

from core.state import State


def _freeze(state: State, artifact_id: str) -> dict:
    from core.tool_registry import execute

    return asyncio.run(execute("freeze_artifact", state, artifact_id=artifact_id))


@pytest.fixture()
def experiment_state(tmp_path):
    from core.bootstrap import bootstrap

    bootstrap()
    return State.new(node_type="experiment", base_dir=tmp_path, project_id="freeze1")


def test_freezing_an_experiment_log_returns_a_usable_chunk_id(experiment_state) -> None:
    state = experiment_state
    state.save_artifact(
        artifact_type="experiment_log",
        name="closure_ledger",
        content="## Verdict\n- Q1 discharged\n\n## Results\n- 观测到的量: 3.2",
        metadata={"verdict": {"assessment": "Q1 成立", "status": "provisional"}},
    )
    result = _freeze(state, "experiment_log__closure_ledger")

    assert result["status"] == "success"
    assert "chunk_registration_error" not in result, (
        f"自动登记失败了：{result.get('chunk_registration_error')} —— "
        "freeze 的返回值是 frozen_log_chunk_id 的唯一来源，它失败就等于那条链还断着")
    chunk_id = result.get("chunk_id")
    assert chunk_id, "freeze 必须把 chunk_id 交出来"

    # 出口要真的能用：KB 里查得到这一条。
    record = state.get_kb_record("chunks", chunk_id)
    assert record is not None, f"chunk_id={chunk_id!r} 在 KB 里查不到 —— 交了个空头支票"
    assert "Verdict" in str(record.get("text") or ""), "登记的该是这份产物的正文"


def test_a_permanent_paper_is_not_chunked(experiment_state) -> None:
    """只登记下游真的按 chunk_id 引用的那几类，别把长篇成稿灌进 KB。"""
    state = experiment_state
    state.save_artifact(
        artifact_type="accepted_paper", name="paper", content="# 论文正文" * 50,
        metadata={},
    )
    result = _freeze(state, "accepted_paper__paper")

    assert result["status"] == "success"
    assert "chunk_id" not in result, "永久论文不该被自动灌进 KB"


def test_a_failing_registration_never_fails_the_freeze(experiment_state, monkeypatch) -> None:
    """freeze 已经落盘且不可逆 —— 附加动作没有资格把它判失败。

    这条曾经真的会穿透：第一版把 except 只套在登记调用上，而 except 里那句
    `append_transcript` 自己会抛（run 目录不存在时），异常照样飞出去，把整次
    工具调用变成崩溃。**错误处理路径本身也是路径。** 现在整段（含落日志）都在
    try 里，落日志再包一层 suppress。

    这里只让登记抛：把 `append_transcript` 也打瘸会让 freeze 在更早的地方就
    失败（它别处也用），那样测的就不是这件事了。
    """
    state = experiment_state
    state.save_artifact(
        artifact_type="experiment_log", name="boom", content="## Verdict\n- x",
        metadata={},
    )

    async def _explode(*_args, **_kwargs):
        raise RuntimeError("KB 挂了")

    monkeypatch.setattr(
        "shared.tools.library.kb._kb_register_artifact_as_chunk", _explode)

    result = _freeze(state, "experiment_log__boom")
    assert result["status"] == "success", "登记炸了不许把 freeze 判失败"
    assert "KB 挂了" in str(result.get("chunk_registration_error")), "但错误要摆出来，不许吞"
