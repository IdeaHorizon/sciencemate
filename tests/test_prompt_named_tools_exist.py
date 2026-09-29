"""模型看得见的文案里点名的工具，必须真的存在。

## 为什么（2026-08-18 起，多次实测）

`nodes/hypothesis/harness.yaml` 写着「save pre_registration 后、freeze 前必须
跑齐 `audit_inquiry_contract` / …」—— 那个工具**不存在**。模型照着文案去找，
找不到，于是跳过了这道检查，直接 freeze；冻结闸这才告诉它闭合条件解析不出来。
一个格式要求花了三轮才学会，其中一轮跑在模型自己编的假线索上。

2026-08-21 那一晚把代价摆得更清楚：`create_experiment` 的**参数描述**写着
「先 freeze_artifact + kb_register_artifact_as_chunk 拿 id」，而后者在工具面
精简时已经下架。那条链在注册面上没有任何合法产生路径，experiment 连挂两次，
模型在两个 run 里提这个幽灵名 147 次和 48 次，最后只能报 blocker 收尾。

文案和 registry 是关于「有哪些工具」的两个真相源，能静默分叉。

## 上一版为什么没抓到

它只扫 `nodes/*/harness.yaml`，并且靠一张动词前缀白名单（audit/assess/…）
认工具形态。于是：

  · `kb_*` / `find_*` / `cancel_*` / `inject_*` 开头的名字，**判据里压根没有**；
  · SKILL.md、review_spec.md、hooks 的 nudge 字符串、**已注册工具自己的
    description**、框架发给模型的运行时消息 —— 整片在扫描面之外。

它的 docstring 当时就写着「⚠️ 如果哪天这条测试开始需要一张豁免名单，那说明
判据选错了」。判据确实选错了，而且错的正是它警告的那种错：**名单化**。
`_VERB_PREFIXES` 就是那张名单，只不过长在护栏自己身上。

## 现在的判据

**扫「模型看得见的文本」，不扫代码。** 这是本测试唯一的边界：

  · yaml / md —— 全文
  · py —— 只取**字符串字面量**（ast 取常量）。工具 description、参数
    description、hook 注入的提醒、返回给模型的报错，全都是字符串；而
    `state.read_artifact(...)` 那种真实代码调用不是字符串，自然不进扫描面。
    这一条把噪声挡在外面，不需要豁免名单。

**在这些文本里，形如调用的 snake_case 名字就算点名**：`name(` 或
「调/跑/用 name」。不再问它像不像工具名 —— 那是名单。

**排除**只有一条，而且是机械的：仓库里有 `def <name>` 的，是内部函数被文档
引用，不是在指使模型调工具。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from core.bootstrap import bootstrap
from core.tool_registry import all_tool_names

REPO_ROOT = Path(__file__).resolve().parents[1]

#: snake_case、至少两段。不限定开头动词 —— 限定开头就是名单。
_IDENT = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+"
#: 调用形状 `name(`；前面不能挨着 `.` 或标识符字符（排除 `obj.method(`）。
#: 名字和括号之间**不许跨行** —— 多条参数描述拼在一起时，上一条以
#: `concept_id` 结尾、下一条以 `(claims only)` 开头，`\s*` 会把它们粘成一次
#: 调用。实测这一条就贡献了大半噪声。
#: 括号里紧跟中文的不算调用 —— `reputation_note(≤80字符)`、
#: `layer_filter('kb'=项目+org)` 是在给字段加注解，不是在教模型调工具。
#: 这条会漏掉 `create_claim(text="中文…")` 这类真调用站点，但**不影响判定**：
#: 两遍扫只要求一个名字在全仓有**任意一处**干净的调用形状，就认定它是工具
#: 形态，之后它的裸提及照样被抓。漏站点便宜，漏名字才贵。
_CALL_SHAPE = re.compile(
    r"(?<![\w.])(%s)[ \t]*\((?![^)\n]{0,14}[一-鿿])" % _IDENT)
#: 中文调用动词后面紧跟的名字。ASCII 动词要求前面是词边界 —— 否则 `use` 会
#: 匹配进 `user_prompt` 的内部，把 `r_prompt` 当成工具名（实测 287 条噪声里
#: 一大半是这么来的）。
_CALL_VERB = re.compile(
    r"(?:调用|调起|调|跑齐|跑|使用|改用|(?<![\w])(?:call|Call|use|Use)\b)"
    r"\s*[`'\"]?\s*(%s)\b" % _IDENT)

#: 模型看得见的文本：整文件就是给模型的。
_TEXT_GLOBS = (
    "nodes/*/harness.yaml",
    "nodes/*/review_spec.md",
    "nodes/*/skills/*/SKILL.md",
    "shared/skills/*/SKILL.md",
)
#: 这些 .py 里有大量发给模型的字符串（工具/参数 description、nudge、报错）。
_PY_GLOBS = (
    "shared/tools/*.py",
    "shared/tools/library/*.py",
    "shared/lib/*.py",
    "core/*.py",
    "nodes/*/hooks.py",
    "nodes/*/tools/*.py",
)


#: py 里**结构上**发给模型的字符串位置。
#:
#: 为什么不是"所有字符串常量"：源码里的字符串还包括日志、事件名、代码示例、
#: JSON schema 片段。把它们一起扫会捞出满屏 `train_test_split(`、
#: `user_prompt(` 这种噪声（实测），而判据一吵就会被加豁免名单，名单化又正是
#: 这条测试要防的东西。所以按**位置**取，不按内容猜。
_MODEL_FACING_KEYS = ("description", "error", "note", "hint", "guidance")


def _model_facing_strings(source: str) -> str:
    """工具/参数的 description、返回给模型的 error/note/hint。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    out: list[str] = []

    def _collect(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):          # f-string
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    out.append(part.value)
        elif isinstance(node, ast.BinOp):              # "a" "b" 隐式拼接的变体
            _collect(node.left)
            _collect(node.right)

    for node in ast.walk(tree):
        # description=... / error=... 关键字参数
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in _MODEL_FACING_KEYS:
                    _collect(kw.value)
        # {"description": ...} / {"error": ...} 字典项
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant)
                        and key.value in _MODEL_FACING_KEYS):
                    _collect(value)
    return "\n".join(out)


def _named_tools(text: str) -> set[str]:
    return ({m.group(1) for m in _CALL_SHAPE.finditer(text)}
            | {m.group(1) for m in _CALL_VERB.finditer(text)})


def _defined_functions() -> set[str]:
    """仓库里定义过的**公开**函数名 —— 文档引用内部函数不算指使模型调工具。

    ⚠️ 名字必须**逐字**相等，不许把 `_kb_register_artifact_as_chunk` 去掉下划线
    当成 `kb_register_artifact_as_chunk` 的实现而放行 —— 那正是 2026-08-21 的
    病例形态：工具下架了，私有实现还在（`create_claim` 内部还在调它），文案继续
    点公开名。去掉下划线就等于让这条测试对**唯一真实的病例**失明。
    模型看不见私有函数，`_` 开头的一律不算。
    """
    names: set[str] = set()
    for path in REPO_ROOT.rglob("*.py"):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not node.name.startswith("_")):
                names.add(node.name)
    return names


def _model_facing_sources() -> list[tuple[str, str]]:
    seen: set[Path] = set()
    out: list[tuple[str, str]] = []
    for pattern in _TEXT_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            out.append((str(path.relative_to(REPO_ROOT)),
                        path.read_text(encoding="utf-8")))
    for pattern in _PY_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path in seen or "test" in path.name:
                continue
            seen.add(path)
            out.append((str(path.relative_to(REPO_ROOT)),
                        _model_facing_strings(path.read_text(encoding="utf-8"))))
    return out


def test_every_tool_named_in_model_facing_text_resolves() -> None:
    """两遍扫：先让语料自己说哪些名字是工具形态，再抓它们的全部提及。

    为什么要两遍：真实病例是**裸提及** —— 「先 freeze_artifact +
    kb_register_artifact_as_chunk 拿 id」，一个括号都没有。想靠「先/再/然后」
    这类顺序词去抓，会把满仓的字段名一起捞进来（实测 287 条噪声）。

    但语料里的信息够用：同一个名字只要**在任何一处**被写成 `name(`，它就是
    工具形态；那么它在别处的裸提及也是在指使模型调它。于是不需要顺序词，也
    不需要任何名单 —— 判据从语料现算。
    """
    bootstrap()
    known = set(all_tool_names())
    internal = _defined_functions()
    sources = _model_facing_sources()

    phantom = _phantom_mentions(sources, known, internal)

    assert not phantom, (
        "模型看得见的文案点名了不存在的工具 —— 模型会照着去找、找不到，然后"
        f"跳过那一步或者绕路：\n{phantom}\n"
        "修法二选一：把工具接上，或者把文案改成真实存在的那个名字。"
        "别加豁免名单 —— 名单化正是这条测试要防的东西。")


def _phantom_mentions(
    sources: list[tuple[str, str]], known: set[str], internal: set[str],
) -> dict[str, list[str]]:
    """两遍扫的实现体。抽出来是为了让判据自己也能被测。"""
    # 第一遍：**只认带括号的调用形状**。
    #
    # 为什么不把「使用 X」那一路也算进来：它会把枚举值和模板名一起捞进来
    # （「使用 technical_report 模板」「use toolchain_build」「重跑 source_node
    # 本身」），而这些不是工具。带括号是强得多的信号，且**够用** —— 一个真
    # 工具在全仓总会有至少一处被写成 `name(...)`，认出来之后，它在别处的裸
    # 提及由第二遍兜住。宁可第一遍严一点，也不要为了多认几个名字去养一张
    # 豁免名单。
    callable_like: set[str] = set()
    for _, text in sources:
        callable_like |= {m.group(1) for m in _CALL_SHAPE.finditer(text)}
    ghosts = {n for n in callable_like if n not in known and n not in internal}

    # 第二遍：这些幽灵名的**任何**提及都算点名，裸提及也算。
    phantom: dict[str, list[str]] = {}
    for label, text in sources:
        hit = sorted(g for g in ghosts if re.search(r"(?<![\w.])%s\b" % g, text))
        if hit:
            phantom[label] = hit
    return phantom


def _public_functions_in(node: str) -> set[str]:
    """`nodes/<node>/tools/` 里定义的公开函数名。

    这些名字是**候选工具**：它们长在节点自己的工具包里，随时可能是（或曾经是）
    一个注册工具。模型调不到函数，只调得到工具 —— 所以节点指令里用祈使句点它们
    的名字，就必须有一个同名工具在本节点的白名单上。
    """
    names: set[str] = set()
    for path in sorted((REPO_ROOT / "nodes" / node / "tools").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node_ast in ast.walk(tree):
            if (isinstance(node_ast, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not node_ast.name.startswith("_")):
                names.add(node_ast.name)
    return names


def test_an_instruction_to_call_a_node_tool_names_one_the_node_actually_has() -> None:
    """节点指令里祈使句点名的、长在自己 tools/ 里的名字，必须在自己的白名单上。

    ## 这条补的是上面那条扫盘的一个洞（2026-08-31 抓到）

    `test_every_tool_named_in_model_facing_text_resolves` 会放过"仓库里有
    `def <name>`"的名字 —— 理由写在 `_defined_functions()` 上：文档引用内部
    函数不算指使模型调工具。多数时候对，但它对**这一种形状**失明：

      · `nodes/data/tools/input_inspection.py` 里 `async def inspect_input_path`
        一直在（planning loop 内部还在调它）；
      · 同时 `nodes/data/tools/__init__.py` 把它的注册 import 删了 —— 工具没了；
      · 而 harness.yaml 还写着「任务给出文件或目录路径时，**必须先调用**
        inspect_input_path」。

    于是模型收到一条硬要求，指向一个它调不到的名字。上面那条扫盘因为
    `def inspect_input_path` 存在而放行，`test_instructed_tools_are_in_whitelist`
    因为中文「必须先调用 X」不带括号而放行 —— 两道闸各自有理，中间恰好漏出
    这个形状。PR #715 就是这么带着幽灵工具通过全绿 CI 的。

    ## 判据

    只看两件机械可判的事，不猜名字像不像工具：
      1. 名字是 `nodes/<node>/tools/*.py` 里的公开函数（候选工具，噪声极低 ——
         `technical_report` 这种散文名词不是函数，进不来）；
      2. 它在本节点 model-facing 文本里以**调用形**或**调用动词**出现。
    两条都满足 → 必须在本节点 `harness.tools` 上。

    修法二选一：把工具注册回来并加进白名单，或者把文案改成不再指使模型调它
    （内部自动完成的事就别写成对模型的要求）。
    """
    from core.loader import list_harnesses, load_harness

    bootstrap()
    debts: dict[str, list[str]] = {}
    for node in list_harnesses():
        try:
            h = load_harness(node)
        except Exception:                                    # noqa: BLE001
            continue
        candidates = _public_functions_in(node)
        if not candidates:
            continue
        text = "\n".join([
            getattr(h, "system_prompt", "") or "",
            *[str(r) for r in (getattr(h, "rules", None) or [])],
            *[str(g) for g in (getattr(h, "guidelines", None) or [])],
        ])
        named = _named_tools(text)
        whitelist = set(getattr(h, "tools", []) or [])
        missing = sorted(n for n in named & candidates if n not in whitelist)
        if missing:
            debts[node] = missing
    assert not debts, (
        "节点指令祈使句点名了自己 tools/ 里的函数，但那个名字不在本节点工具"
        "白名单上 —— 模型照着去调，调不到，然后跳过整道要求：\n"
        + "\n".join(f"  {node}: {names}" for node, names in sorted(debts.items()))
        + "\n修法：把工具注册并加进白名单，或者把文案改成不再指使模型调它。")


def test_the_scanner_reads_strings_not_code() -> None:
    """判据的边界：代码不进扫描面，发给模型的字符串要进。"""
    source = (
        "async def handler(state):\n"
        "    rec = state.read_artifact(artifact_id)\n"       # 真实代码调用
        "    helper_function(rec)\n"                          # 真实代码调用
        "    log.info('calling verify_everything(x)')\n"      # 日志，不是给模型的
        "    return {'error': '先 freeze_artifact 再 create_claim(...)'}\n"
    )
    named = _named_tools(_model_facing_strings(source))
    assert "read_artifact" not in named, "真实代码调用不该进扫描面"
    assert "helper_function" not in named, "真实代码调用不该进扫描面"
    assert "verify_everything" not in named, "日志字符串不是发给模型的"
    assert "create_claim" in named, "error 里发给模型的调用必须抓得住"


def test_bare_mentions_are_caught_once_the_name_is_known_callable() -> None:
    """真实病例是裸提及：「先 freeze_artifact + kb_register_… 拿 id」，没有括号。

    第一遍在别处见过 `kb_register_artifact_as_chunk(`，就认定它是工具形态；
    第二遍据此把**没有括号**的那句也判成点名。
    """
    sources = [
        ("with_parens", "拿不到就 kb_register_artifact_as_chunk(artifact_id=...)"),
        ("bare_only", "先 freeze_artifact + kb_register_artifact_as_chunk 拿 id"),
    ]
    phantom = _phantom_mentions(sources, known={"freeze_artifact"}, internal=set())
    assert "bare_only" in phantom, "裸提及必须被第二遍抓住"
    assert phantom["bare_only"] == ["kb_register_artifact_as_chunk"]
    assert "freeze_artifact" not in phantom.get("bare_only", []), "真工具不许误报"


def test_the_scanner_is_not_a_verb_allowlist() -> None:
    """上一版漏掉的那几族必须认得 —— 它们不以 audit/create/save 这类动词开头。

    这条是上一版失明的直接回归测试：`_VERB_PREFIXES` 里没有 kb_/find_/cancel_/
    inject_，于是这四族一个都扫不到，全仓漏了 60+ 处。
    """
    corpus = [(f"src{i}", text) for i, text in enumerate((
        "先 kb_register_artifact_as_chunk(artifact_id=x) 拿到真实 chunk_id",
        "用 cancel_node(child_run_id=..., reason=...) 停掉它",
        "跑 find_org_promotion_candidates(auto_propose=True)",
        "处理：用 inject_into_node(child_run_id=..., content=<答案>) 作答",
    ))]
    found = {g for hits in _phantom_mentions(corpus, set(), set()).values()
             for g in hits}
    assert found == {
        "kb_register_artifact_as_chunk", "cancel_node",
        "find_org_promotion_candidates", "inject_into_node",
    }, f"判据漏掉了某一族：{found}"


def test_the_scanner_tells_call_sites_from_field_names() -> None:
    """噪声不许进。"""
    for text in (
        "`run_role=primary` + `stage=simulation`",                    # 字段赋值
        "必须通过 `tools.build_contract.validate_contract`",          # 带点路径
        "统一替代 v2 的 update_claim_status / update_question_status",  # 解释历史名字
    ):
        assert not _named_tools(text), f"噪声被当成点名：{text!r} → {_named_tools(text)}"


def test_the_legal_shape_is_described_in_exactly_one_place() -> None:
    """合法形状是一件事，不许有第二种说法。

    冻结闸两个分支 + freeze 前审计此前各写各的，只有一处提到 ```yaml``` 块 ——
    模型第一次撞到的偏偏是没提的那条。现在三处都调 `closure_shape_hint()`。
    """
    from core.prereg_commitments import closure_shape_hint

    hint = closure_shape_hint()
    assert "```yaml```" in hint, "最关键的那条要求必须在文案里"
    assert "- metric:" in hint and "- statement:" in hint

    sources = [
        REPO_ROOT / "shared" / "tools" / "library" / "artifacts_extra.py",
        REPO_ROOT / "nodes" / "hypothesis" / "tools" / "research_questions.py",
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "closure_shape_hint()" in text, f"{path.name} 没用共用文案"
        assert "- statement: \\\"成分" not in text, (
            f"{path.name} 里又抄了一份闭合条件写法 —— 抄件会各自演化")
