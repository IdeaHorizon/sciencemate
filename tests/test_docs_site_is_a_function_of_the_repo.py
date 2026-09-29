"""文档站的内容必须由**仓库**决定，不能由 build 那台机器决定。

2026-09-15 实测：同一个 commit，三台机器建出三个不同的站 ——

    写 origin/main 那台（有 lean + flint）→ 331 页
    我的笔记本（有 lean、没 flint）        → 330 页（少 interval_check）
    CI（两个都没有）                        → 329 页（两个都少）

根因是两个不同的问题共用了一份答案：「这个框架里有没有这件东西」和「这台
机器现在能不能跑它」都问 `_REGISTRY`。能力闸控的工具（check_lean 要 Lean
工具链、interval_check 要 python-flint）探不到就不注册 —— 那是**运行时该有的
样子**，但文档站照着它出页，站就成了 build 机器的函数。

后果不是"文档少一页"。每个工具页还列同侪工具，少一个工具会改掉上百页，于是
**没有任何一台机器能重建出提交的那份**，"重建结果 == 提交的产物"这道闸永远
红 —— 只能被关掉，而那正是文档站上次烂掉三个月的形状。

分法见 `core.tool_registry.register_capability_gated_tool`：定义永远登记，
可用性单独记一笔。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from core.bootstrap import bootstrap
from core.tool_registry import _REGISTRY

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 机器探测的形状 —— 问的是"这台机器上有什么"，而不是"这个仓库里有什么"。
_MACHINE_PROBES = ("which", "expanduser", "find_spec", "import_module")


@pytest.fixture(scope="module", autouse=True)
def _booted():
    bootstrap()


def test_capability_gated_tools_stay_out_of_the_live_registry_when_absent():
    """运行时那条规矩不许被这次改动放松：能力缺席就不给工具。

    这条是[[能力缺席就不给工具]]的回归闸 —— 把定义登记进目录是为了文档站，
    绝不能顺带让 agent 看见一个跑不了的工具（模型会照着调，然后整条路走不通）。
    """
    gated = _REGISTRY.capability_gated
    assert gated, "一个能力闸控的工具都没有？那多半是登记通道断了，不是真没有"

    for name, entry in gated.items():
        assert entry["definition"].name == name
        if entry["available"]:
            assert name in _REGISTRY.executors, f"{name} 探到了能力却没进注册表"
        else:
            assert name not in _REGISTRY.executors, (
                f"{name} 的能力探不到，却出现在 agent 能调的注册表里 —— "
                f"「能力缺席就不给工具」被破坏了"
            )


def test_every_gated_tool_is_documented_regardless_of_this_machine():
    """文档站取的那份目录，必须无条件包含每个能力闸控的工具。

    判据落在 build 真正读的那个东西上（introspect 出来的 tools_index），
    不是"catalog 这个 dict 非空" —— 后者能在接线断掉时照样绿。
    """
    import sys

    # 生成器顶层 import markdown。CI 装 `.[docs]`（见 .gitea/ci-deps.sh），
    # 所以这条在**门禁上真的跑**；只有没装文档依赖的开发机才跳过。
    pytest.importorskip(
        "markdown", reason="文档站生成器的依赖；装 `pip install -e '.[docs]'` 后这条会跑"
    )
    sys.path.insert(0, str(ROOT / "scripts"))
    from build_docs_html import introspect_tools_and_nodes

    data = introspect_tools_and_nodes()
    index = data["tools_index"]

    for name, entry in _REGISTRY.capability_gated.items():
        assert name in index, (
            f"{name} 不在文档站的工具目录里 —— 这台机器探不到它的能力，"
            f"于是站上就没有这一页。站的内容不能是机器的函数。"
        )
        assert index[name]["requires_capability"] == entry["capability"], (
            f"{name} 的页上没写清要什么能力；读的人会以为它随时可用"
        )


def test_no_tool_registration_hides_behind_a_machine_probe():
    """扫盘：不许再出现 `if <探测本机>: register_tool(...)`。

    不写名单写判据（[[护栏要扫盘不要写名单]]）：名单的形状是"新加的默认漏过"，
    而这里要防的正是**下一个**被加进来的能力闸控工具 —— 它会以完全一样的方式
    把站重新变成机器的函数，而且照样三道闸全绿。
    """
    offenders: list[str] = []

    for path in sorted(ROOT.glob("shared/**/*.py")) + sorted(ROOT.glob("nodes/**/*.py")):
        if any(part.startswith("test") for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        # 模块里每个函数的 body，按名字备查 —— 条件常写成 `_lean_binary()`，
        # 探测藏在那个函数里面，只看条件本身看不出来。
        funcs = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            registers = any(
                isinstance(c, ast.Call)
                and (getattr(c.func, "id", None) or getattr(c.func, "attr", None))
                == "register_tool"
                for c in ast.walk(node)
            )
            if not registers:
                continue

            # 条件里直接探测，或条件调用的函数体里探测 —— 两种都算。
            probe_sources = [node.test]
            for called in ast.walk(node.test):
                if isinstance(called, ast.Call):
                    fname = getattr(called.func, "id", None)
                    if fname in funcs:
                        probe_sources.append(funcs[fname])

            for src in probe_sources:
                for sub in ast.walk(src):
                    attr = getattr(sub, "attr", None) or getattr(sub, "id", None)
                    if attr in _MACHINE_PROBES:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} "
                            f"（条件 `{ast.unparse(node.test)[:50]}` 探测本机 {attr!r}）"
                        )
                        break
                else:
                    continue
                break

    assert not offenders, (
        "这些地方把 register_tool 藏在了一个**本机探测**后面，于是文档站的内容\n"
        "会取决于谁来 build（实测同一 commit 三台机器三个站）：\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n\n改用 core.tool_registry.register_capability_gated_tool："
        "\n定义无条件登记（文档站读它），能力探测只决定 available=（运行时读它）。"
    )
