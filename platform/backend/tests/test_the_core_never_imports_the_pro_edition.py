"""核心永远不 import 专业版 —— 两种发行只差「有没有 `app/pro`」。

## 这条线为什么必须是机械的

个人版开源、专业版不开源，而源码是同一份。做法是：专业版的代码住在几个固定目录里
（后端 `app/pro/`），公开仓库是内部仓库删掉这些目录之后的快照。这只在一个前提下成立：
**核心一个 import 都不指向专业版**。谁都能顺手写一句 `from app.services import organisation`
——写了不报错、测试全绿、打包也过，直到导出的那棵树起不来。所以这条线由 import 图守，
不由人记。判据是 AST，不是 grep 名字：`core/org_*.py` 那样名字像专业版、实际是核心的
东西，按名字扫会误伤。

## 三样东西

- **专业版集合** = `app/pro/` 底下的一切 + `STILL_OUTSIDE_THE_PRO_PACKAGE`（还没搬进去的，
  按边界清单登记；搬一个删一个，清单清空这条就退役）。
- **唯一的接缝** = `app/assembly.py:wire_the_edition`：它按名字找 `app.pro`
  （`importlib.util.find_spec`），找得到才 import。别处一律不许。
- **棘轮** `BASELINE`：今天还允许的核心→专业版 import，逐条写着为什么和谁来清。
  只许减不许增；一条不再成立时必须从这里删掉 —— 表上留着一条已经不存在的例外，
  下次有人再犯它就漏过去了。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

PRO_PACKAGE = "app.pro"

#: 边界清单 §2.1 里整文件归专业版、但还没搬进 `app/pro/` 的模块。PR② 搬完后这张表清空。
STILL_OUTSIDE_THE_PRO_PACKAGE: frozenset[str] = frozenset()
# （PR② 把 20 个模块搬进了 app/pro；JWT 原语和 `core/authentication` 是账号的基础设施，
#   9 个核心测试文件靠它们认证，留在核心。）

#: 核心找专业版的唯一一处。
THE_SEAM = "app.assembly"

#: 棘轮。键 = 核心模块，值 = 它今天还 import 着的专业版模块 → 谁来清。
BASELINE: dict[str, set[str]] = {}
# 棘轮已清零（PR②）。从现在起核心 → 专业版的任何一个 import 都是红。


def _module_name(path: Path) -> str:
    rel = path.relative_to(BACKEND).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _modules() -> dict[str, Path]:
    return {_module_name(p): p for p in APP.rglob("*.py") if "__pycache__" not in p.parts}


def _is_pro(name: str) -> bool:
    return name == PRO_PACKAGE or name.startswith(PRO_PACKAGE + ".") or name in STILL_OUTSIDE_THE_PRO_PACKAGE


def _imports_of(path: Path, this: str) -> set[str]:
    """这个文件 import 了哪些 `app.*` 模块（`from app.services import x` 算 `app.services.x`）。"""
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = this.split(".")
                if path.name != "__init__.py":
                    anchor = anchor[:-1]
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join(anchor + ([base] if base else []))
            if not base.startswith("app"):
                continue
            found.add(base)
            for alias in node.names:
                found.add(f"{base}.{alias.name}")
    return found


def _core_to_pro_imports() -> dict[str, set[str]]:
    modules = _modules()
    breaches: dict[str, set[str]] = {}
    for name, path in modules.items():
        if _is_pro(name) or name == THE_SEAM:
            continue
        hit = {m for m in _imports_of(path, name) if _is_pro(m) and (m in modules or m == PRO_PACKAGE)}
        if hit:
            breaches[name] = hit
    return breaches


def test_the_registry_only_names_modules_that_exist() -> None:
    """登记里不许有不存在的模块：搬走一个就从这里删一个，表才是真的。"""
    modules = _modules()
    stale = sorted(m for m in STILL_OUTSIDE_THE_PRO_PACKAGE if m not in modules)
    assert not stale, f"这些登记的模块已经不在原处了（搬进 app/pro 了？）：{stale} —— 从 STILL_OUTSIDE_THE_PRO_PACKAGE 删掉"


def test_the_core_never_imports_the_pro_edition() -> None:
    breaches = _core_to_pro_imports()
    new = {src: dst - BASELINE.get(src, set()) for src, dst in breaches.items()}
    new = {src: dst for src, dst in new.items() if dst}
    assert not new, (
        f"核心 import 了专业版：{new}。\n"
        "核心不能知道专业版的存在 —— 公开仓库没有那些模块。要用它的功能，在核心开一个接缝"
        "（登记表 / 钩子，见 app/services/other_homes.py、app/policies.py STEWARDSHIP_EXTENSIONS），"
        "由 app/pro/__init__.py:wire 在装配时填进去。"
    )


def test_the_baseline_only_lists_what_is_still_true() -> None:
    """棘轮只许往下走：表上的例外一旦清掉，就把它从表上删掉。"""
    breaches = _core_to_pro_imports()
    stale = {src: dst - breaches.get(src, set()) for src, dst in BASELINE.items()}
    stale = {src: dst for src, dst in stale.items() if dst}
    assert not stale, f"这些例外已经不成立了，从 BASELINE 删掉：{stale}"


def test_only_the_assembly_looks_for_the_pro_package() -> None:
    """接缝只有一处，且它是**按名字找**，不是 import。"""
    modules = _modules()
    seam = modules[THE_SEAM]
    tree = ast.parse(seam.read_text(encoding="utf-8"))
    finds_by_name = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "find_spec"
        and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == PRO_PACKAGE
        for n in ast.walk(tree)
    )
    assert finds_by_name, f"{THE_SEAM} 必须用 importlib.util.find_spec({PRO_PACKAGE!r}) 找专业版，找不到就是个人版"
    others = sorted(
        name for name, path in modules.items()
        if name != THE_SEAM and not _is_pro(name)
        and any(m == PRO_PACKAGE or m.startswith(PRO_PACKAGE + ".") for m in _imports_of(path, name))
    )
    assert not others, f"除了 {THE_SEAM}，这些核心模块也碰了 {PRO_PACKAGE}：{others}"
