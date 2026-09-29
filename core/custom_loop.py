"""自定 agent_loop override 机制。

如果 `nodes/<node_type>/agent_loop.py` 存在并暴露 `run_loop` async 函数，
framework 在该节点 run 时**用 owner 的版本**而非 `core.agent_loop.run_loop`。

设计原则：
  - 完全 opt-in：没文件 = 默认行为
  - 同事不被迫学
  - Framework 默认 loop 仍可被 owner reuse（推荐"包装模式"）
  - 用 override 等于 owner 接受**部分 framework 保证减弱**（详见 templates/agent_loop.py.template 警告）

Discovery：
  bootstrap 时 import `nodes.<x>.agent_loop`（如果文件存在）；本模块在 executor 调
  前解析它，找 `run_loop` 函数返回。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("custom_loop")

# Type 注释（避免循环 import）
CustomLoopFn = Callable[..., Awaitable]   # async def run_loop(harness, state, messages, llm) -> LoopResult

_NODES_DIR = Path(__file__).resolve().parent.parent / "nodes"


def _nodes_dir(base_dir: Path | None = None) -> Path:
    return base_dir if base_dir is not None else _NODES_DIR


def has_custom_loop(node_type: str, base_dir: Path | None = None) -> bool:
    """看节点是否有 <base>/<node_type>/agent_loop.py 文件。"""
    return (_nodes_dir(base_dir) / node_type / "agent_loop.py").exists()


def resolve_custom_loop(node_type: str,
                         base_dir: Path | None = None) -> CustomLoopFn | None:
    """返回 owner 的 run_loop 函数；没文件 / 没函数 / import 失败返 None。

    v3.1 正门政策（消除"框架说可以、CI 说不行"的制度矛盾，2026-06 审计）：
    节点 MAY ship agent_loop.py，条件 = 在 framework_exemptions.yaml 的
    `custom_agent_loops` 登记（rationale + framework owner 批准）。
    未登记的文件存在也**不生效**（warn + 走默认 loop）——机制与治理从此一致。

    用 spec_from_file_location 直接 load 文件，跟 Python package 系统解耦
    （让 sandbox 测试可行 + 不依赖 nodes/ 一定是 namespace package）。
    """
    if not has_custom_loop(node_type, base_dir):
        return None

    # base_dir 非 None 是测试注入的假 nodes 目录 —— 登记检查只对生产 nodes/ 生效
    if base_dir is None:
        from shared.lib.exemptions import allowed_custom_loops
        if node_type not in allowed_custom_loops():
            log.warning(
                "nodes/%s/agent_loop.py 存在但未在 framework_exemptions.yaml "
                "的 custom_agent_loops 登记 —— 不生效，走框架默认 loop。"
                "申请：提 issue 给 framework owner（rationale + 影响面）。",
                node_type,
            )
            return None
    path = _nodes_dir(base_dir) / node_type / "agent_loop.py"
    mod_name = f"_custom_loop_{node_type}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        log.warning("Custom agent_loop for %s import 失败：%s", node_type, e)
        return None
    fn = getattr(mod, "run_loop", None)
    if fn is None:
        log.warning("nodes/%s/agent_loop.py 存在但没暴露 run_loop 函数", node_type)
        return None
    return fn


def runs_the_framework_loop(node_type: str, base_dir: Path | None = None) -> bool:
    """这个节点的 run 会不会走框架自己的 agent loop（`core.agent_loop.run_loop`）。

    问的是**这趟真正会跑哪个 loop**，不是"目录里有没有 agent_loop.py"。两者会
    分叉：文件在、但没在 `framework_exemptions.yaml` 的 `custom_agent_loops`
    登记（或 import 失败），`resolve_custom_loop()` 返 None，executor 照样走框架
    默认 loop。拿 `has_custom_loop()` 当判据的话，那种节点会被当成"自定义 loop"
    而误放行。

    谁需要这个答案：所有"框架 loop 才提供的装备"的扫盘闸 —— loop_hooks（只由
    `core.agent_loop.run_loop` 的 run_on_turn_start / run_on_end 触发）、工具面
    （custom loop 按函数名直接 import 调用，`harness.tools` 没人读）、轮循环
    （没有轮，跨轮笔记无处落地）。判据集中在这一处，三道闸各自复制一份就是
    三个会各自演化的答案。
    """
    return resolve_custom_loop(node_type, base_dir) is None


def list_nodes_with_custom_loop(base_dir: Path | None = None) -> list[str]:
    """所有用 custom loop 的节点名 —— `hf doctor` / startup log 用。"""
    d = _nodes_dir(base_dir)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_dir() and has_custom_loop(p.name, base_dir):
            out.append(p.name)
    return out
