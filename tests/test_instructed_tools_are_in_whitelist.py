"""我们递给模型的文字里以「调用形」点名的工具，本节点的工具面得给得出。

## 两个病例，同一个形状

**2026-08-23（E2E v29）**：#635/#637 把 manuscript 冻结责任交给 writing，writing
的 harness 正文写着「调 `freeze_artifact(artifact_id=<manuscript_id>)` 冻结」，
但没人把 `freeze_artifact` 加进 writing 的白名单。三条路全断（自己冻不了 /
orchestrator 代冻被拒 / 伪造 frozen 被堵），会话死锁。

**2026-09-07（真机第四轮 Ising）**：experiment 在一个 run 里误建了第二份
`experiment_log`，完整性闸拒绝冻结，并且**在拒绝理由里指了一条出路**：

    supersede_experiment_log(artifact_id=<误建的草稿 id>, reason=...)

模型照着找，然后报了 blocker：这个工具不在本节点的工具面上。三个已经跑完的
simulation 作业 finalize 不了，路线锁死，连修订重跑都做不了（issue #832）。

第一例的文字在 **prompt** 里，第二例的文字在**闸的拒绝理由**里。上一版这条闸只
扫 prompt，所以第二例整整从它眼皮底下走过去。分界线画错了地方：真正的边界不是
「prompt」，而是**这个节点会递给模型的所有文字** —— 工具描述、错误、hint、
拒绝理由，模型读它们的时候不会去分辨这句话是从 yaml 来的还是从 py 来的。

## 判据

对每个节点，取它**递给模型的文字**：

1. `harness.yaml` 的 `system_prompt` 与 `rules`；
2. `nodes/<node>/**/*.py` 里**除文档字符串以外**的字符串字面量 —— 工具描述、
   返回的 error/hint/reasons 都在这里。文档字符串（模块/类/函数的第一条语句）
   是写给人看的，结构上可判，排除；`#` 注释根本不是字面量，天然不在内。
   节点自己的 `tests/` 不发给模型，排除。

在这些文字里找 `<name>(<args>)`，`<name>` 是**注册工具**的，`<name>` 必须在该
节点的 `tools` 白名单里。

### 为什么括号里要「像参数」

上一版只要求 `name(`。真跑一遍发现英文复数会撞上工具名：hooks 里的
`"Follow-up task(s): "` 命中了注册工具 `task`。分辨「叫你调它」和「英文散文」
的，不是名字，是**括号里像不像一个参数表**：空括号、`key=`、`<占位>` 都是在
示范怎么调；`(s)` 不是。这条比「名字长度 ≥ N」那种阈值实在 —— 它盯的是这段
文字在教模型什么。

### 不管什么

- 只管**本节点自己的代码**。共享工具（`shared/tools/`）的描述不知道自己此刻
  挂在哪个节点上，同一句话对 A 节点合法对 B 节点不合法，那是另一个问题。
- 跨节点提及（"postprocess 会调 stage_writing_assets(...)"）由 callable_nodes
  那套管；这里只在名字属于注册工具集时开火，天然把 `np.array(` 之类挡在外面。

## 两种改法都对

闸红了，说明「文字许诺的」和「工具面给的」对不上。哪边错要看事实：

- 工具就该给（本例 `supersede_experiment_log`）→ 加进白名单；
- 那条路已经关了、文字是陈的（本例 hypothesis 的 `create_claim`：承诺在
  641c8dbf7 收归预注册，工具面同时收敛）→ **改文字**，别把工具加回来。

改文字和加工具都能让闸变绿，但只有一个是真的。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.loader import load_harness
from core.tool_registry import all_tool_names

#: 仓库根（tests/ 的上一级）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 所有 producing + 系统节点。
_NODES = [
    "hypothesis", "literature", "experiment", "observation", "writing",
    "postprocess", "data", "derivation", "_orchestrator", "_reviewer", "_curator",
]

#: `name(args)` —— 名字 + 一层括号（括号里不再嵌套括号，够用且不会吃掉整段）。
_CALL = re.compile(r"\b([a-z_][a-z0-9_]{2,})\s*\(([^()]{0,200}?)\)")


def _looks_like_an_argument_list(inner: str) -> bool:
    """括号里像不像在示范怎么调它。见上文「为什么括号里要像参数」。"""
    text = inner.strip()
    return text == "" or text == "..." or "=" in text or "<" in text


def _node_directory(node: str) -> Path:
    """节点代码在哪 —— 系统节点带前导下划线，目录名不带。"""
    for candidate in (REPO_ROOT / "nodes" / node, REPO_ROOT / "nodes" / node.lstrip("_")):
        if candidate.is_dir():
            return candidate
    return REPO_ROOT / "nodes" / node


def _is_a_test_file(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_") or path.name.endswith("_test.py")


def _docstring_literals(tree: ast.AST) -> set[int]:
    """模块/类/函数的文档字符串 —— 写给人的，不是递给模型的。"""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def model_facing_text(node: str) -> list[tuple[str, str]]:
    """这个节点会递给模型的文字，每条带一个出处。"""
    pieces: list[tuple[str, str]] = []

    harness = load_harness(node)
    pieces.append((f"nodes/{node}/harness.yaml:system_prompt",
                   str(getattr(harness, "system_prompt", "") or "")))
    rules = getattr(harness, "rules", None) or []
    rules = rules if isinstance(rules, list) else [rules]
    for index, rule in enumerate(rules):
        pieces.append((f"nodes/{node}/harness.yaml:rules[{index}]", str(rule)))

    for path in sorted(_node_directory(node).rglob("*.py")):
        if _is_a_test_file(path):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - 语法错另有闸管
            continue
        docstrings = _docstring_literals(tree)
        where = path.relative_to(REPO_ROOT)
        for literal in ast.walk(tree):
            if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
                continue
            if id(literal) in docstrings:
                continue
            pieces.append((f"{where}:{literal.lineno}", literal.value))
    return pieces


def registered_tools() -> set[str]:
    """注册表里的全部工具名。

    ⚠️ 必须先 `bootstrap()` —— 不 bootstrap 时注册表是**空的**，于是"文字点名了
    注册工具"永远不成立，整条闸静默变成空判据、一路全绿。2026-09-07 写这一版时
    我就这么栽了一次：参数化那 11 条全过，只有直指病例那条红。
    """
    from core.bootstrap import bootstrap

    bootstrap()
    return set(all_tool_names())


def tools_the_text_tells_the_model_to_call(node: str) -> dict[str, str]:
    """本节点文字里以调用形点名的**注册工具** → 第一处出处。"""
    registered = registered_tools()
    named: dict[str, str] = {}
    for where, text in model_facing_text(node):
        for name, inner in _CALL.findall(text):
            if name in registered and _looks_like_an_argument_list(inner):
                named.setdefault(name, where)
    return named


@pytest.mark.parametrize("node", _NODES)
def test_instructed_tools_are_available_to_the_node(node: str) -> None:
    """文字许诺的能力，本节点的工具面得给得出。"""
    try:
        harness = load_harness(node)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"load_harness({node}) 失败：{exc}")
        return
    whitelist = set(getattr(harness, "tools", []) or [])

    named = tools_the_text_tells_the_model_to_call(node)
    missing = {name: where for name, where in sorted(named.items()) if name not in whitelist}

    assert not missing, (
        f"节点 `{node}` 递给模型的文字以调用形点名了这些**注册工具**，"
        f"却不在它的 tools 白名单里：\n"
        + "\n".join(f"  - {name}  ←  {where}" for name, where in missing.items())
        + "\n模型读到这句话就会去调，调不到就只能报 blocker —— 一条路被堵死时，"
        f"这就是死锁（E2E v29 的 writing 缺 freeze_artifact；#832 的 experiment "
        f"缺 supersede_experiment_log）。\n"
        f"要么把它加进 `nodes/{node.lstrip('_')}/harness.yaml` 的 tools，"
        f"要么改这段文字 —— 取决于那条路到底该不该有。"
    )


def test_the_scan_is_not_vacuous() -> None:
    """注册表非空，且这条闸确实在某个节点上认出了工具。

    没有这条，注册表一空（忘了 bootstrap、bootstrap 出错被吞）就等于把闸关了，
    而且是**全绿**着关的 —— 那正是"没执行 ≈ 执行了没效果"。
    """
    registered = registered_tools()
    assert len(registered) > 50, f"注册表只有 {len(registered)} 个工具 —— 判据在空跑"

    recognised = tools_the_text_tells_the_model_to_call("experiment")
    assert recognised, "experiment 的文字里一个注册工具的调用形都没认出来 —— 判据在空跑"


def test_the_scan_covers_both_kinds_of_text() -> None:
    """两个病例是两种**出处**，这条闸必须两种都还在扫。

    * 第一例（#635/#637 writing 缺 freeze_artifact）的文字在 **harness.yaml** 里；
    * 第二例（#832 experiment 缺那条 supersede 出路）的文字在 **`.py` 的拒绝理由**里。
      上一版这条闸只扫 prompt，第二例整整从它眼皮底下走过去。

    这里**不点名任何工具**：点名的是"这两种出处都得有产出"。

    为什么不再逐个钉死工具名（#923）：那样等于把**节点的工具名**写死在**框架的测试**
    里。后果是节点每次改名、增删工具，都要动一个自己没权限的文件 —— 不动就带着红测试
    交付，动了就被 scope guard 整条拒掉（lujy 的 fix/879-minimal 把
    ``supersede_experiment_log`` 改名成 ``supersede_closure_draft`` 时正是这样卡住的）。
    而且那两条用例本来就是多余的：通用闸按**调用形**认工具，与名字无关，改完名它照样
    认得出来 —— 完整性由框架验，内容由节点 owner 维护
    （[[feedback_guardrails_must_scan_not_list]]）。
    """
    from_yaml: list[str] = []
    from_python: list[str] = []
    for node in _NODES:
        for name, where in tools_the_text_tells_the_model_to_call(node).items():
            if where.endswith(".yaml") or ":rules[" in where or ":system_prompt" in where:
                from_yaml.append(f"{node}:{name}")
            elif ".py:" in where:
                from_python.append(f"{node}:{name}")
    assert from_yaml, (
        "没有任何一条来自 harness.yaml —— prompt 那一半的扫描停了（第一个病例的形状）"
    )
    assert from_python, (
        "没有任何一条来自 .py 的字符串字面量 —— 拒绝理由/错误/hint 那一半的扫描停了。"
        "第二个病例（#832）正是从这一半走过去的：闸在拒绝理由里指了一条出路，"
        "而那个工具不在节点的工具面上，三个跑完的作业 finalize 不了、路线锁死。"
    )


def test_the_scan_reads_more_than_the_prompt() -> None:
    """判据的覆盖面本身要可证：语料必须包含节点 .py 里的非文档字符串。

    上一版只扫 prompt，#832 那条就是从这个缺口走掉的。这条锁住"扫的东西比
    prompt 多"，否则语料悄悄缩回去、闸照样全绿，谁也不知道。
    """
    sources = {where for where, _ in model_facing_text("experiment")}
    from_python = {w for w in sources if w.endswith(".py") or ".py:" in w}
    assert from_python, "语料里没有任何 .py 出处 —— 这条闸又缩回只看 prompt 了"

    audit_lines = {w for w in from_python if "contract_audit.py" in w}
    assert audit_lines, (
        "experiment 的闸文案（contract_audit.py）不在语料里 —— #832 那条正是从这里出的"
    )


def test_english_plurals_do_not_count_as_calls() -> None:
    """`task(s)` 不是在叫模型调 `task` 工具。

    这是真踩到的假阳性：`nodes/experiment/hooks.py` 的收尾 footer 里写着
    "Follow-up task(s): "，而 `task` 确实是注册工具名。
    """
    assert not _looks_like_an_argument_list("s")
    assert not _looks_like_an_argument_list("es")
    assert _looks_like_an_argument_list("")
    assert _looks_like_an_argument_list("artifact_id=<x>, reason=...")
    assert _looks_like_an_argument_list("claim_type='hypothesis'")
    assert _looks_like_an_argument_list("<manuscript_id>")
