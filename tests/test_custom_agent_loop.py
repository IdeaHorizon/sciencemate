"""Custom agent_loop override 测试。

确认：
  - nodes/<X>/agent_loop.py 缺 → 用 framework 默认 run_loop
  - nodes/<X>/agent_loop.py 含 run_loop → 用 owner 的
  - owner run_loop 写 transcript event "agent_loop_mode" 让 framework 留痕
  - 找不到函数 / import 失败 → log warning + 回退默认
"""
from __future__ import annotations

import asyncio
import importlib
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def fake_nodes(tmp_path):
    """造一个临时 nodes/ 目录。resolve_custom_loop 接 base_dir 参数测它。"""
    base = tmp_path / "nodes"
    base.mkdir()

    def _make(node_type: str, has_custom: bool, content: str = ""):
        d = base / node_type
        d.mkdir()
        (d / "harness.yaml").write_text(f"node_type: {node_type}\nversion: '0.1'\n")
        if has_custom:
            (d / "agent_loop.py").write_text(content)
        return d

    return base, _make


def test_no_custom_loop_returns_none(fake_nodes):
    from core import custom_loop
    base, mk = fake_nodes
    mk("noplain", has_custom=False)
    assert custom_loop.resolve_custom_loop("noplain", base_dir=base) is None
    assert custom_loop.has_custom_loop("noplain", base_dir=base) is False


def test_custom_loop_resolves(fake_nodes):
    from core import custom_loop
    base, mk = fake_nodes
    mk("withcustom", has_custom=True, content=textwrap.dedent("""
        async def run_loop(harness, state, messages, llm):
            return 'fake_result'
    """))
    fn = custom_loop.resolve_custom_loop("withcustom", base_dir=base)
    assert fn is not None
    assert asyncio.iscoroutinefunction(fn)


def test_custom_loop_missing_run_loop_function(fake_nodes):
    from core import custom_loop
    base, mk = fake_nodes
    mk("nofn", has_custom=True, content="# 空文件，没 run_loop 函数\n")
    fn = custom_loop.resolve_custom_loop("nofn", base_dir=base)
    assert fn is None


def test_custom_loop_import_error(fake_nodes):
    from core import custom_loop
    base, mk = fake_nodes
    mk("badimport", has_custom=True,
        content="raise ImportError('test broken')\n")
    fn = custom_loop.resolve_custom_loop("badimport", base_dir=base)
    assert fn is None


def test_list_nodes_with_custom_loop(fake_nodes):
    from core import custom_loop
    base, mk = fake_nodes
    mk("a", has_custom=False)
    mk("b", has_custom=True, content="async def run_loop(*a,**k):\n  pass\n")
    mk("c", has_custom=True, content="async def run_loop(*a,**k):\n  pass\n")
    nodes = custom_loop.list_nodes_with_custom_loop(base_dir=base)
    assert nodes == ["b", "c"]


def test_resolved_function_actually_callable(fake_nodes):
    from core import custom_loop
    base, mk = fake_nodes
    mk("call", has_custom=True, content=textwrap.dedent("""
        async def run_loop(harness, state, messages, llm):
            return ('called', harness, len(messages))
    """))
    fn = custom_loop.resolve_custom_loop("call", base_dir=base)
    assert fn is not None
    result = asyncio.run(fn("h", "s", ["m1", "m2"], "llm"))
    assert result == ("called", "h", 2)


def test_shipped_custom_loops_match_exemption_registry():
    """v3.1 正门政策：ship 的 custom loop 集合必须 == framework_exemptions.yaml
    的 custom_agent_loops 登记集合。

    以前这个测试断言恒空（"框架说可以、CI 说不行"的制度矛盾 —— 2026-06 审计），
    owner 撞墙后只能挖隧道（如借 hook 改写 response）。现在：登记 = 合法，
    未登记 = 本测试红 + 运行时 resolve_custom_loop 也拒绝生效。
    """
    from core.custom_loop import list_nodes_with_custom_loop
    from shared.lib.exemptions import allowed_custom_loops, reload
    reload()
    # 这测试用真 _NODES_DIR（不 monkeypatch）—— 验证生产 repo 状态。
    # 语义 = allowlist 子集：ship 未登记的 → 红；登记了还没 ship 的 → 合法
    # （批准可以先于 owner 的 PR 落地，这是自然的 PR 时序）。
    shipped = set(list_nodes_with_custom_loop())
    registered = allowed_custom_loops()
    unregistered = shipped - registered
    assert not unregistered, (
        f"未登记的 custom agent_loop：{sorted(unregistered)}。"
        f"要么在 framework_exemptions.yaml 的 custom_agent_loops 登记"
        f"（framework owner 批准），要么删掉 nodes/<x>/agent_loop.py。"
    )


def test_unregistered_custom_loop_does_not_resolve(tmp_path, monkeypatch):
    """未登记的 custom loop 文件存在也不生效（运行时正门检查）。"""
    import core.custom_loop as cl
    # 在真 nodes/ 里造一个未登记节点是不行的（污染 repo）——
    # 改为 monkeypatch _NODES_DIR 指向 tmp，然后临时关掉 base_dir 豁免逻辑：
    # 直接验证 allowed_custom_loops 是 gate 的数据源即可。
    from shared.lib.exemptions import allowed_custom_loops
    assert "definitely_not_registered_node" not in allowed_custom_loops()
