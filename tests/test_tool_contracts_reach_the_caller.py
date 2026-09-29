"""工具会拒绝你的字段，必须在你调用之前就说得出来 —— 而且只说一遍。

## 现场（2026-08-18，英国饮食文化课题）

一条 experiment run 把全部裁决做完后连撞四次墙，每次一个白烧的来回。逐条查
下来，病根是同一个：**工具契约有两份真相源**。

一份是给模型看的 `description`（手写），一份是校验器的实现
（`errors.append("x must be …")`）。两份各自演化，**分叉时不报错**：

    freeze_raw_results 的说明写着 "…retention entries"
    校验器要的却是      raw_results.files

模型照着说明写，被自己的说明坑掉一整个来回。这是"送达了契约，但送的是错的
那一份" —— 比没送更贵，因为它看起来可信。

## 修法：一份声明，两个消费者

`ToolDefinition.content_contract` 是唯一声明：
  - `register_tool` 把它渲染进模型看到的 description（`_render_content_contract`）
  - 校验器用 `contract_requirement()` 从同一个 dict 取拒绝措辞

想让两边分叉，得先把这个 dict 拆成两个 —— 结构上做不到。

## 这个文件守的是"没有工具能绕过声明"

判据**扫盘，不写名单**（写名单 = 新工具默认漏过，正是要消灭的那种护栏）：
遍历所有节点的 tools 模块，把"按字段名拒绝"的字段抽出来，要求每一个都能在
模型调用前看到 —— 在工具的 schema / description（含渲染出的契约）里，或在
该节点的 harness.yaml 里。

"按字段名拒绝"的判据也要准：`"compilation must not run inside a source tree"`
里的 compilation 是英文句首的名词，不是字段名。所以只认**同一模块里确实以
dict key 读过**的标识符（`.get("x")` / `["x"]`）—— 否则护栏自己就在造噪音。
"""
from __future__ import annotations

import importlib
import pathlib
import re

import pytest

from core.tool_registry import (
    _REGISTRY,
    ToolDefinition,
    _render_content_contract,
    contract_requirement,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
NODES = REPO / "nodes"

#: `"<field> must …"` —— 校验器按名字拒绝的通用写法（尚未迁移到声明的那些）。
_REJECTS_RE = re.compile(r'"([a-z_][a-z_0-9]*)(?:\.[a-z_0-9.{}\[\]]+)?\s+must\s')

#: `contract_requirement(SOME_CONTRACT, "field")` —— 已迁移的那些。
#:
#: 这一条是补的：迁移之后字段名藏进了调用里，不再以 `"x must"` 的形式出现在
#: 源码中，只扫前一种正则就**看不见已迁移的工具**了 —— 护栏会退化成"只防没
#: 迁移的"。突变验证时抓到的：把 analysis_eligible 从声明里删掉，测试照样全绿。
_REQUIREMENT_CALL_RE = re.compile(
    r'contract_requirement\(\s*([A-Z_][A-Z_0-9]*)\s*,\s*["\']([a-z_][a-z_0-9]*)["\']'
)


def _reads_as_dict_key(source: str, name: str) -> bool:
    """这个标识符在本模块里真的当过 dict key 吗 —— 用来滤掉散文里的英文单词。"""
    return bool(re.search(rf'\.get\(\s*["\']{name}["\']|\[["\']{name}["\']\]', source))


def _rejected_fields(source: str) -> set[str]:
    return {
        m.group(1)
        for m in _REJECTS_RE.finditer(source)
        if _reads_as_dict_key(source, m.group(1))
    }


def _import_all_node_tools() -> None:
    for module_path in sorted(NODES.glob("*/tools/*.py")):
        if module_path.name.startswith("_"):
            continue
        node = module_path.parent.parent.name
        try:
            importlib.import_module(f"nodes.{node}.tools.{module_path.stem}")
        except Exception:  # 有些模块需要运行时上下文；扫不到就跳过，不假绿
            continue


def _executing_tools(module_path: pathlib.Path, source: str) -> list[str]:
    """模型要调**哪个**工具，才会撞上本模块的拒绝。

    通常是本模块自己注册的工具（按源文件归属判定）。但拒绝逻辑不一定长在工具上：
    冻结门（2026-08-17 起）是**类型**的性质，用 `register_freeze_gate` 声明，
    由唯一的 `freeze_artifact` 执行 —— 模型调的是 freeze_artifact，契约就得在
    freeze_artifact 的说明里能读到。判据跟着执行者走，不跟着文件走。
    """
    tools = [
        name
        for name, src in _REGISTRY.tool_source_files.items()
        if src and pathlib.Path(src).name == module_path.name
        and f"/{module_path.parent.parent.name}/" in src
    ]
    if "register_freeze_gate" in source:
        tools.append("freeze_artifact")
    return tools


def _model_visible_surface(module_path: pathlib.Path, source: str = "") -> str:
    """模型在调用**之前**能看到的全部文字：执行者工具的契约 + 该节点的 harness.yaml。"""
    surface = " ".join(
        (_REGISTRY.tools[name].description or "") + str(_REGISTRY.tools[name].parameters_schema)
        for name in _executing_tools(module_path, source)
        if name in _REGISTRY.tools
    )
    harness = module_path.parent.parent / "harness.yaml"
    if harness.exists():
        surface += harness.read_text(encoding="utf-8", errors="replace")
    return surface


class TestTheDeclarationIsTheOnlySource:
    """机制本身：声明 → 两个消费者。"""

    def test_the_contract_is_rendered_into_what_the_model_sees(self):
        rendered = _render_content_contract({"files": "a non-empty list"})
        assert "`files`" in rendered and "a non-empty list" in rendered

    def test_the_validator_wording_comes_from_the_same_declaration(self):
        contract = {"status": "a non-empty string"}
        assert contract_requirement(contract, "status") == "status must be a non-empty string"

    def test_rejecting_an_undeclared_field_is_loud_not_silent(self):
        """契约没覆盖到的字段，措辞要难看到有人来修 —— 不许静默退回老样子。"""
        message = contract_requirement({"a": "x"}, "b")
        assert "not declared" in message

    def test_registration_appends_the_contract_once_not_twice(self):
        async def _noop(*, state=None, **_):  # pragma: no cover - 只验注册行为
            return {"status": "success"}

        definition = ToolDefinition(
            name="contract_render_probe",
            description="Probe.",
            parameters_schema={"type": "object", "properties": {}},
            content_contract={"foo": "a bar"},
        )
        from core.tool_registry import register_tool

        register_tool(definition, _noop)
        rendered = _REGISTRY.tools["contract_render_probe"].description
        assert rendered.count("Content contract") == 1
        assert "`foo`" in rendered


class TestNoToolRejectsOnSomethingItNeverDeclared:
    """扫盘：全部节点工具模块，一个都不放过。"""

    def test_every_field_a_validator_rejects_is_visible_before_the_call(self):
        _import_all_node_tools()
        debts: dict[str, list[str]] = {}
        for module_path in sorted(NODES.glob("*/tools/*.py")):
            source = module_path.read_text(encoding="utf-8", errors="replace")
            rejected = _rejected_fields(source)
            if not rejected:
                continue
            surface = _model_visible_surface(module_path, source)
            missing = sorted(field for field in rejected if field not in surface)
            if missing:
                debts[f"{module_path.parent.parent.name}/{module_path.name}"] = missing
        assert not debts, (
            "这些字段会让调用被拒，但模型在调用前看不到它们 —— "
            "把它们放进对应工具的 content_contract（或该节点的 harness.yaml）：\n"
            + "\n".join(f"  {mod}: {fields}" for mod, fields in sorted(debts.items()))
        )

    def test_every_migrated_call_names_a_field_its_declaration_actually_has(self):
        """已迁移的校验器：`contract_requirement(X, "f")` 里的 f 必须真在 X 里。

        否则 `contract_requirement` 会返回 "f is rejected but not declared…" ——
        一句给用户看的、毫无帮助的报错，而且悄悄绕过了上面那条扫盘（字段名藏
        在调用里，不以 `"f must"` 的形式出现在源码中）。
        """
        _import_all_node_tools()
        broken: list[str] = []
        for module_path in sorted(NODES.glob("*/tools/*.py")):
            source = module_path.read_text(encoding="utf-8", errors="replace")
            module = None
            for contract_name, field in _REQUIREMENT_CALL_RE.findall(source):
                if module is None:
                    node = module_path.parent.parent.name
                    try:
                        module = importlib.import_module(
                            f"nodes.{node}.tools.{module_path.stem}")
                    except Exception:
                        break
                declared = getattr(module, contract_name, None)
                if not isinstance(declared, dict) or field not in declared:
                    broken.append(f"{module_path.name}: {contract_name} 里没有 {field!r}")
        assert not broken, "校验器引用了声明里不存在的字段：\n  " + "\n  ".join(broken)

    def test_the_scan_ignores_prose_that_merely_starts_with_a_word(self):
        """护栏不能自己造噪音。

        `"compilation must not run inside a source tree"` 里的 compilation 是
        句首名词，不是字段名 —— 误报会让人学会忽略这条测试。
        """
        prose = 'errors.append("compilation must not run inside a source tree")'
        assert _rejected_fields(prose) == set()
        real = 'x = payload.get("status")\nerrors.append("status must be a non-empty string")'
        assert _rejected_fields(real) == {"status"}
