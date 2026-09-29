"""模型面的写产物工具，不得自带 typed-only 闸的豁免。

## 为什么要有这条扫盘

2026-08-22 一天里同一个形撞了两次 —— **模型被一道闸机械拒了，就换个工具再来一次**：

- #617：`freeze_artifact` 拒了 finalize（那道闸是对的，专防把 reviewer 的 REVISE
  洗成已冻结的论文）→ 模型改用 `save_artifact` 重写产物、metadata 带 `frozen: true`。
- #618：`save_artifact` 铸不出 typed-only 凭据 → 但 `import_artifact` 同样是模型
  可调的、而且**无条件**传 `imported` provenance，而豁免判据当时写作
  `provenance is not None` → `import_artifact(artifact_type="research_state", ...)`
  一句话绕开整道闸。

两次都不是「没想到要防」，是**防线边界划错、漏了一条到达路径**
（`feedback_mechanism_exists_but_unwired` 那个族）。#618 是我查别的事时**顺手撞见**
的，不是被任何测试拦下的 —— 所以要有这条。

## 判据

`validate_artifact_write` 只有两个豁免口子：

1. `capability is _WRITE_CAPABILITY`（生命周期工具的可信令牌）
2. `_is_transfer(provenance)` —— 只认 `kind == forwarded`

两个都是**框架内部**才该构造的。于是不变量可以机械表述：

> 凡是「模型能自己填 artifact_type」的注册工具，其实现都不得自带这两个豁免。

带了就等于给模型开了一扇绕过属主检查的正门 —— 不管它本意是什么。

扫的是**注册表**，不是名单：以后新加这样的工具，这条自动把它算进来。

## 这两条各自管什么（别把功劳记混）

- `test_a_model_facing_writer_carries_no_exemption` 管**新工具自带豁免**。
- `test_the_two_exemptions_are_still_the_only_two` 管**豁免口子被放宽**。

⚠️ 说清楚：**前者当年抓不到 #618**。#618 那次 `import_artifact` 传的是
`imported` 而不是 `forwarded`，它本身不算"自带豁免" —— 出事的是判据那侧写得太
宽（`provenance is not None` 把 `imported` 也算进了转发）。抓那个形的是后者：
把豁免退回原样，后者当场红。

两条合起来才盖住这一类。单看任一条都会高估自己。
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _model_facing_artifact_writers() -> dict[str, object]:
    """扫出所有「模型能自己填 artifact_type」的注册工具 → 它的实现函数。

    排除两类：
    - schema 给了 `enum` 的（取值被框架限死，模型选不到 typed-only 之外的东西）
    - 只读的（名字以 list_/read_/find_ 打头，不落盘）
    """
    from core.bootstrap import bootstrap
    from core.tool_registry import _REGISTRY, all_tool_names, get_tool

    bootstrap()
    out = {}
    for tool_name in all_tool_names():
        definition = get_tool(tool_name)
        properties = ((getattr(definition, "parameters_schema", None) or {})
                      .get("properties") or {})
        schema = properties.get("artifact_type")
        if not isinstance(schema, dict) or schema.get("enum"):
            continue
        if tool_name.startswith(("list_", "read_", "find_", "search_")):
            continue
        executor = _REGISTRY.executors.get(tool_name)
        if executor is not None:
            out[tool_name] = executor
    return out


def test_the_scan_finds_the_known_population() -> None:
    """扫盘器不能悄悄扫成空 —— 空集合会让下面那条永远为真。"""
    found = _model_facing_artifact_writers()
    assert {"save_artifact", "import_artifact"} <= set(found), (
        f"两个已知的模型面写产物工具没被扫到，说明扫法坏了；扫到的是 {sorted(found)}"
    )


def _carried_exemptions(func) -> list[str]:
    """这个实现有没有**自带**豁免：传可信令牌，或自己造 forwarded provenance。

    查 AST 不查字符串：查文本的话，`_is_transfer` 那类名字出现在注释或 import 里
    就会命中，撤掉真正的调用也不转红（#617 的扫盘第一版就是这么假绿的）。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    carried = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "_write_capability":
            carried.append("_write_capability=（可信令牌）")
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
            if name == "forwarded":
                carried.append("provenance=forwarded(...)（转发豁免）")
    return carried


@pytest.mark.parametrize("tool_name", sorted(_model_facing_artifact_writers()))
def test_a_model_facing_writer_carries_no_exemption(tool_name: str) -> None:
    """模型能自选类型的写产物工具，一律不得自带豁免。"""
    carried = _carried_exemptions(_model_facing_artifact_writers()[tool_name])
    assert not carried, (
        f"`{tool_name}` 是模型能自己填 artifact_type 的工具，却自带了 typed-only "
        f"闸的豁免：{carried}。\n"
        f"这等于给模型开了一扇绕过属主检查的正门 —— #618 就是这么被绕开的"
        f"（`import_artifact(artifact_type=\"research_state\", ...)`）。\n"
        f"要么给 artifact_type 加 enum 限死取值，要么别在这个工具里自带豁免。"
    )


def test_the_two_exemptions_are_still_the_only_two() -> None:
    """上面那条扫的是「这两个豁免」。真加了第三个，这条先红，提醒去扩扫盘。

    不然新豁免一进来，扫盘会继续绿着 —— 而它已经不覆盖全部口子了
    （护栏自己得名单病，今天已经栽过一次）。
    """
    from core import artifact_capabilities as caps

    source = inspect.getsource(caps.validate_artifact_write)
    tree = ast.parse(textwrap.dedent(source))
    early_returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
    assert len(early_returns) == 1, (
        f"`validate_artifact_write` 现在有 {len(early_returns)} 个提前返回，"
        f"而扫盘只认得 capability 和 forwarded 两个豁免。"
        f"新加了口子就去 `_carried_exemptions` 里一并扫上。"
    )
    assert "_is_transfer(provenance)" in source, (
        "转发豁免不再走 `_is_transfer` 了 —— 扫盘的假设失效，去对一下"
    )
