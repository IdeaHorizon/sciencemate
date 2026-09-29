"""停止授权必须有身份，不能靠「引文逐字出自任务正文」（#1084 第二节）。

`expected_termination.planned_stop` 现在只核对引文在不在任务正文里。而
`"完整跑完大约 10 分钟"` 也逐字出自任务正文 —— 引它一样通过。于是子 run 的模型
可以给任意本地作业声明 planned_stop，把一次取消记成「按计划停止」、收尾写
success；没有这个声明时同样的局面要求收尾写 blocked。

节点**刻意不**去用关键词或语义正则猜「这句话是不是在授权停止」——那等于让执行者
自己给自己发授权。所以授权只能由派发方显式给出，Core 在子 run 开工前冻住。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.stop_authorization import (
    authorizes_planned_stop,
    freeze,
    mint_policy_id,
    planned_stop_policy_id,
)


def _state(tmp_path: Path, name: str = "r") -> State:
    bootstrap()
    return State.new(node_type="experiment", base_dir=tmp_path / name, project_id=None)


def test_planned_stop_requires_typed_policy_identity(tmp_path: Path) -> None:
    """#1084 验收 2：没有带类型的停止授权身份时，计划内停止不成立。"""
    # 没授权 —— 引什么都不成立
    state = _state(tmp_path, "unauthorized")
    assert state.planned_stop_policy_id is None
    assert authorizes_planned_stop(state, None) is False
    assert authorizes_planned_stop(state, "") is False
    assert authorizes_planned_stop(state, mint_policy_id(state.run_id, "")) is False, (
        "自己算一个 id 就能通过 —— 那等于执行者给自己发授权"
    )

    # 派发方授权之后才成立，而且只对**那一个** id 成立
    authorized = _state(tmp_path, "authorized")
    record = freeze(authorized, authorized=True, note="让它跑大约 1 分钟就停下",
                    authorized_by_run_id="run_parent")
    policy_id = record["planned_stop_policy_id"]
    assert authorized.planned_stop_policy_id == policy_id
    assert authorizes_planned_stop(authorized, policy_id) is True
    assert authorizes_planned_stop(authorized, "psp_deadbeefdeadbeef") is False
    # 另一个 run 的授权在这个 run 上不成立：授权是 run 级的，不是通行证
    assert authorizes_planned_stop(state, policy_id) is False


def test_an_authorization_is_never_widened_mid_run(tmp_path: Path) -> None:
    """开工后再喊一次「授权」改不了已经冻住的那一份。"""
    state = _state(tmp_path)
    first = freeze(state, authorized=True, note="第一次")
    again = freeze(state, authorized=True, note="换个说法再来一次")
    assert again == first


def test_refusing_to_authorize_writes_nothing(tmp_path: Path) -> None:
    """没授权就是没有记录 —— 一份 `authorized: false` 只会让「没授权」和
    「授权被撤了」混成一件事。"""
    state = _state(tmp_path)
    assert freeze(state, authorized=False, note="用户没说可以停") is None
    assert not (state.root / "stop_authorization.json").exists()
    assert state.planned_stop_policy_id is None


def test_the_dispatcher_can_actually_grant_it() -> None:
    """契约要送到调用方手里：run_node 的 schema 里得有这个参数。

    只在 Core 里实现、派发方看不见，等于这条授权没有任何合法产生路径。
    """
    from core.bootstrap import bootstrap as _bootstrap
    from core.tool_registry import get_tool

    _bootstrap()
    schema = get_tool("run_node").parameters_schema["properties"]
    assert "planned_stop_authorized" in schema, (
        "派发方在工具面上看不到这个参数 —— 授权就永远不会被给出")
    assert "planned_stop_note" in schema
