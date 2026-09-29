"""网格参数的拒绝理由，模型在**调用前**就得读得到。

`mesh_generator._build_cylinder_gmsh_geo()` 会因为 `farfield_radius` 不合法而
抛错，但模型调的不是它 —— 模型调的是 `prepare_scientific_mesh(parameters=…)`，
参数一路传到几百行之外才被拒。中间没有任何一处告诉过模型有这条约束。

框架层的扫盘（tests/test_tool_contracts_reach_the_caller.py）只能判到"本模块
有没有留下未声明的拒绝字符串"；它按**文件名**找执行者，而 mesh_generator.py
自己不注册工具，所以它看不见"声明有没有真的挂到模型看得见的那个入口上"。
这一条把那半截补上：断言落在**渲染后的 description** 上，不是落在声明存在。
"""
from __future__ import annotations


def _mesh_tool_description() -> str:
    from core.bootstrap import bootstrap
    from core.tool_registry import _REGISTRY

    bootstrap()
    return _REGISTRY.tools["prepare_scientific_mesh"].description or ""


def test_the_rejected_field_is_visible_before_the_call() -> None:
    assert "farfield_radius" in _mesh_tool_description(), (
        "prepare_scientific_mesh 的说明里没有 farfield_radius —— "
        "模型只能先写错一版被拒才知道有这条约束"
    )


def test_the_wording_the_model_reads_is_the_wording_the_validator_uses() -> None:
    """一份声明两个消费者：说明和拒绝理由必须逐字同源，不许各写一句。"""
    from core.tool_registry import contract_requirement
    from nodes.data.tools.mesh_generator import MESH_PARAMETER_CONTRACT

    requirement = MESH_PARAMETER_CONTRACT["farfield_radius"]
    assert requirement in _mesh_tool_description()
    assert requirement in contract_requirement(MESH_PARAMETER_CONTRACT, "farfield_radius")


def test_an_undeclared_field_would_be_loud() -> None:
    """反向锚：声明里没有的字段，措辞难看到有人会来修（不静默退回老样子）。"""
    from core.tool_registry import contract_requirement
    from nodes.data.tools.mesh_generator import MESH_PARAMETER_CONTRACT

    assert "not declared" in contract_requirement(MESH_PARAMETER_CONTRACT, "nope_not_a_field")
