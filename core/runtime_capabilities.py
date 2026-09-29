"""运行时能力 —— 这个进程**能干什么**，由进程属主的配置决定。

能力是进程属主的配置，不是节点输入：模型可以随便写 ``node_inputs`` 和工具参数，
但它不能给自己授权。工具声明 ``required_runtime_capability`` 之后，能力不在场时
那个工具**压根不出现在工具面上** —— 不是出现了、被调用了、然后失败。

两个来源，同一个概念：

  ``writing_fixture_delivery``   测试运行时档位（HARNESS_RUNTIME_PROFILE=test）
  ``model_role:<role>``          平台给这个角色指派了可用后端（见 core.model_roles）

第二个来源是 2026-08-23 加的，起因是一个实测事故：postprocess 把三张图全渲染完，
在 finalize 处被审图工具驳回（平台没配 vision 角色）→ run 判死 → 一张反复回来的
审批卡片。当时的形状是"能力缺失 → 工具出现了、被调用了、然后把整条流水线判死"。
现在缺能力的工具不出现，节点走它本来就会走的那条降级路（图有了先用上）。

判据是同一条：**能力是进程属主的配置**。模型给不了自己一个角色绑定，正如它给
不了自己一个测试档位。所以按能力收缩工具面是安全的，不是给模型开后门。
"""
from __future__ import annotations

import os
from collections.abc import Iterable


RUNTIME_PROFILE_ENV = "HARNESS_RUNTIME_PROFILE"
RUNTIME_CAPABILITIES_ENV = "HARNESS_TEST_CAPABILITIES"
TEST_RUNTIME_PROFILE = "test"

WRITING_FIXTURE_DELIVERY_CAPABILITY = "writing_fixture_delivery"

KNOWN_TEST_CAPABILITIES = frozenset({WRITING_FIXTURE_DELIVERY_CAPABILITY})

#: `model_role:<role>` —— 平台是否给这个角色指派了可用后端。
MODEL_ROLE_PREFIX = "model_role:"


def model_role_capability(role_id: str) -> str:
    return f"{MODEL_ROLE_PREFIX}{role_id}"


def trusted_runtime_capabilities() -> frozenset[str]:
    """Return capabilities granted by the process owner.

    The separate ``HARNESS_RUNTIME_PROFILE=test`` switch prevents a stray
    capability variable in a production shell from silently enabling fixture
    behavior.  Unknown names are ignored so typos fail closed.
    """
    if os.environ.get(RUNTIME_PROFILE_ENV, "").strip().lower() != TEST_RUNTIME_PROFILE:
        return frozenset()
    requested = {
        item.strip()
        for item in os.environ.get(RUNTIME_CAPABILITIES_ENV, "").split(",")
        if item.strip()
    }
    return frozenset(requested & KNOWN_TEST_CAPABILITIES)


def apply_env_runtime_capabilities(state: object) -> None:
    """Attach the trusted process capabilities to a newly created state."""
    setattr(state, "runtime_capabilities", trusted_runtime_capabilities())


def has_runtime_capability(state: object, capability: str | None) -> bool:
    if not capability:
        return True
    if capability.startswith(MODEL_ROLE_PREFIX):
        # **现算**，不进 state 的快照集合：绑定活在 core.model_roles 那一份里，
        # 抄一份到 state 上就会有两个采样时刻（state 在 run 开始时造，角色配置
        # 可能在那之后才装上），而分叉时两边都不报错。
        from core import model_roles

        return model_roles.available(capability[len(MODEL_ROLE_PREFIX):])
    granted: Iterable[str] = getattr(state, "runtime_capabilities", ()) or ()
    return capability in granted


def capability_absence_reason(capability: str) -> str:
    """能力不在场时，对**能解决它的那个人**说人话。"""
    if capability.startswith(MODEL_ROLE_PREFIX):
        from core import model_roles

        role_id = capability[len(MODEL_ROLE_PREFIX):]
        note = getattr(model_roles.spec(role_id), "absence_note", "") or ""
        return (
            f"平台没有给模型角色 {role_id!r} 指派可用后端。"
            + (f" {note}" if note else "")
        )
    return f"当前 run 没有受信任运行时能力 {capability!r}。"


def validate_node_runtime_inputs(
    node_type: str,
    node_inputs: dict | None,
    *,
    capabilities: Iterable[str] = (),
) -> str | None:
    """Reject node-input switches that are reserved for isolated test runs."""
    if node_type != "writing" or not isinstance(node_inputs, dict):
        return None
    delivery_test_mode = str(node_inputs.get("delivery_test_mode") or "").strip()
    if not delivery_test_mode:
        return None
    if WRITING_FIXTURE_DELIVERY_CAPABILITY in set(capabilities):
        return None
    return (
        "writing node_inputs.delivery_test_mode is reserved for an isolated "
        "test runtime. Production research runs cannot enable deterministic "
        "delivery fixtures through LLM-authored node inputs."
    )
