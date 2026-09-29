"""失败必须带着"是谁的锅"离开工具层。

## 现场（2026-08-21）

wangd 在界面上看到六条并排的失败，每一条都写着同一句
"The tool stopped before producing a usable result."。

往回查：正文一直都在，是被显示层按**长相**判成"技术细节"整句换掉的（含
snake_case 标识符 / 花括号 / 超 180 字符）。拿本机 1038 条真实失败跑那段代码：
87% 被换掉。而那些正文恰恰是最有用的一批 —— 我们的报错按设计就要点名字段。

但把正文放行只解决一半。剩下一半是 wangd 当场问的那句：

  「这些根本没必要显示成错误吧。就是一个正常的 react 循环。」

对。框架按设计驳回一次调用（参数不对、时机不对、护栏拦下），模型下一轮改对
—— 这是 ReAct 的正常一步，人对它做不了任何事。真正该被看到的是**我们的代码
崩了**那一类：模型改多少次参数都没用，它只能绕，绕不过就永久丢掉这个能力。

## 判据必须在失败发生的那一层定

分类不能留给下游猜。dispatch（`core/tool_registry.execute`）是**唯一**的工具
派发口，它天然知道这次失败是怎么来的：

  - 工具**选择**返回 `{"status": "error"}` → `rejected`
  - 工具**抛**了未捕获异常          → `tool_exception`

例外只有一个：有些护栏埋在深层函数里，用 raise 实现驳回（路径边界、冻结产物）。
它们继承 `ToolRejection`，dispatch 据此仍记 `rejected` —— 实测 39 条"真异常"
里 19 条是这类故意的拒绝，按 raise/return 分会全判错。

这个文件守的就是：**每一条 error envelope 都盖了章，且章盖对了。**
"""
from __future__ import annotations

import pytest

from core import tool_errors
from core.project_workspace import ProjectWorkspaceError
from core.state import State
from core.tool_registry import ToolDefinition, execute, register_tool

_SCHEMA = {"type": "object", "properties": {}}


async def _returns_error(*, state, **_):
    return {"status": "error", "error": "claim_type='methodological' 需要 ≥2 个独立来源。"}


async def _raises_bug(*, state, **_):
    return {"turns": 1}["nope"]


async def _raises_rejection(*, state, **_):
    raise ProjectWorkspaceError("Path escaped the Project boundary: '/etc/passwd'")


async def _needs_an_argument(*, state, path: str, **_):
    return {"status": "success", "path": path}


for name, executor in (
    ("probe_returns_error", _returns_error),
    ("probe_raises_bug", _raises_bug),
    ("probe_raises_rejection", _raises_rejection),
    ("probe_needs_argument", _needs_an_argument),
):
    register_tool(ToolDefinition(name=name, description=name, parameters_schema=_SCHEMA), executor)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "expected_code"),
    [
        # 工具自己选择返回 error = 框架按设计说不。
        ("probe_returns_error", tool_errors.REJECTED),
        # 未捕获异常 = 我们的代码崩了，必须给人看。
        ("probe_raises_bug", tool_errors.TOOL_EXCEPTION),
        # 用 raise 实现的**故意**驳回不算 bug（ToolRejection 子类）。
        ("probe_raises_rejection", tool_errors.REJECTED),
        # 调用方式不对：模型改调用就能过，不是缺陷。
        ("probe_needs_argument", tool_errors.MISSING_PARAMETERS),
    ],
)
async def test_every_failure_is_stamped_with_who_is_at_fault(tmp_path, tool, expected_code):
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute(tool, state)
    assert result["status"] == "error"
    assert result["error_code"] == expected_code
    assert result["error"], "错误正文不能是空的 —— 它是模型自纠和人排查的唯一依据"


@pytest.mark.asyncio
async def test_deliberate_rejection_does_not_read_like_a_crash(tmp_path):
    """故意的驳回不许带 `ProjectWorkspaceError:` 这种前缀。

    带了就会被读成"平台出故障了"，而它其实是"这条路不许走"。
    """
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute("probe_raises_rejection", state)
    assert "Error:" not in result["error"]
    assert result["error"].startswith("Path escaped")
    assert "traceback_tail" not in result


@pytest.mark.asyncio
async def test_a_crash_keeps_the_exception_type_and_a_traceback_tail(tmp_path):
    """真 bug 相反：类型和 traceback 尾巴都要留 —— 那是修它的线索。"""
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute("probe_raises_bug", state)
    assert result["error"].startswith("KeyError")
    assert result["traceback_tail"]


@pytest.mark.asyncio
async def test_rejected_is_the_default_so_new_tools_are_never_unstamped(tmp_path):
    """没盖章的 error 一律补 `rejected`。

    护栏要扫盘不要写名单：新工具、MCP 来的工具、同事写的工具都不用改一行，
    盖章发生在唯一的派发口上。
    """
    async def _unstamped(*, state, **_):
        return {"status": "error", "error": "no code here"}

    register_tool(
        ToolDefinition(name="probe_unstamped", description="x", parameters_schema=_SCHEMA),
        _unstamped,
    )
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute("probe_unstamped", state)
    assert result["error_code"] == tool_errors.REJECTED


def test_transcript_truncation_never_eats_the_envelope():
    """截 body，永远不截 envelope。

    上一版 `_brief` 超过 500 字节就把整个 dict dumps 成截断**字符串**，于是
    `{"status": "error", …}` 变成一个字符串；平台 ingest 判错的写法是
    `isinstance(result, dict) and result["status"]`，字符串一律判不出错 ——
    这些失败全部落成 `tool.completed`，在界面上**显示为成功**。本机库里查到
    659 条，占真实失败的 39%。

    最该被保住的恰恰是出错那一批：错误正文是模型自纠和人排查的唯一依据。
    """
    from core.agent_loop import _brief

    big = {
        "status": "error",
        "error_code": tool_errors.REJECTED,
        "error": "⛔ 探查专用 shell：本节点的 run_bash 只用于只读探查。",
        "stdout_tail": "x" * 4000,
        "stderr_tail": "y" * 4000,
    }
    brief = _brief(big)
    assert isinstance(brief, dict), "错误结果被压成字符串 = 下游判不出失败"
    assert brief["status"] == "error"
    assert brief["error_code"] == tool_errors.REJECTED
    assert brief["error"] == big["error"]
    assert len(str(brief)) < 2000, "body 还是要截，不然 transcript 会被撑爆"


def test_successful_results_keep_their_old_truncated_shape():
    """只对**失败**改写形状 —— 成功结果照旧压成截断字符串。

    已经有消费方依赖这一点，而且都在别人的节点目录里（不该为了这次改动去动
    它们，也不该让它们静默变行为）：

      - `nodes/experiment/tools/contract_audit.py` 拿"只剩截断字符串"当"这次
        调用不算成功"的保守判据 —— 换成 envelope dict 会**放宽一道审计**。
      - `nodes/hypothesis/artifact_recovery.py` 直接在字符串里找 `"passed"`
        —— envelope 只留 status/error，`passed` 会消失，恢复逻辑静默失效。
    """
    from core.agent_loop import _brief

    big_success = {"status": "success", "passed": True, "body": "x" * 4000}
    brief = _brief(big_success)
    assert isinstance(brief, str)
    assert '"passed": true' in brief


@pytest.mark.asyncio
async def test_a_nonzero_exit_says_why_even_when_the_tool_never_wrote_error(tmp_path):
    """子进程类工具把失败写在 returncode/stderr_tail 里，envelope 也要有原因。

    实测：`safe_run_bash` 的 `returncode: 127`（命令不存在）在库里 325 条，
    envelope 没有 `error`，下游只剩一句 "Tool execution failed" —— 到底哪条
    命令、为什么挂的，全没了。

    补在唯一的派发口上，不逐个工具改：MCP 工具和同事节点自己的工具（experiment
    的 safe_run_bash 就在别人的目录里）一行不改也都覆盖到。
    """
    async def _exits_nonzero(*, state, **_):
        return {
            "status": "error",
            "cmd": "python3 -c 'import numpy'",
            "returncode": 127,
            "stderr_tail": "bash: python3: command not found",
        }

    register_tool(
        ToolDefinition(name="probe_nonzero_exit", description="x", parameters_schema=_SCHEMA),
        _exits_nonzero,
    )
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute("probe_nonzero_exit", state)
    assert result["error_code"] == tool_errors.COMMAND_FAILED
    assert result["error"] == "命令退出码 127：bash: python3: command not found"
    # 工具本来的结构化字段一个都不动 —— 模型读的是它们。
    assert result["returncode"] == 127 and result["stderr_tail"]


@pytest.mark.asyncio
async def test_structured_artifact_content_never_crashes_a_text_scan(tmp_path):
    """产物正文是对象时，两端都不许炸。

    `content` 声明是字符串，但工具参数来自 LLM 的 JSON，模型给对象是常事。

      写入面：以前一路走到 `sha256_text(content)` 才炸
      `AttributeError: 'dict' object has no attribute 'encode'` —— 一句指不出
      病因的 traceback。现在在工具边界规范化成 JSON 文本。

      读取面：`scan_artifact_disagreements` 对着 dict 调 `finditer`，
      `TypeError: expected string or bytes-like object, got 'dict'`，**整个
      curator 扫描当场结束**，连同已经扫到的发现一起丢（实测 2026-08-11）。
      `read_artifact` 早就写对了，但那是内联抄件，另外五处都没有。

    读取面用 dumps 而不是跳过：结构化产物里一样可能写着 "I disagree with
    claim_x"，跳过等于让这些检查对它们静默失效 —— 比报错更糟，没人会发现。
    """
    from shared.lib.artifact_text import artifact_text
    from shared.tools.library.disagreement_scan import _scan_text_for_disagreements

    state = State.new(node_type="_curator", base_dir=tmp_path)
    note = "I disagree with claim_a1b2c3d4 because the fit is unstable"

    # 写入面：模型给了对象，工具照样写得出来（不再抛 AttributeError）
    saved = await execute(
        "save_artifact", state,
        artifact_type="cluster_report", name="Candidate_Clustering",
        content={"sections": [{"note": note}]},
    )
    assert saved["status"] == "success", saved

    # 读取面：拿到的是可扫的文本，异议照样扫得到
    record = state.read_artifact(saved["id"])
    text = artifact_text(record)
    assert isinstance(text, str) and note in text
    assert [f["claim_id"] for f in _scan_text_for_disagreements(text)] == ["claim_a1b2c3d4"]

    # 历史记录里 content 已经是对象的，读取面也要扛住（不能只靠写入面）
    assert artifact_text({"content": {"note": note}}).startswith('{"note"')

    # 「记录」和「裸正文」是两个问题，别用一个参数同时表示。
    # 第一版 artifact_text 两样都收，而 content 本身可以是 dict —— 传正文进来
    # 被当成记录去取它的 `content` 键，**静默返回空串**，产物存成了空的。
    from shared.lib.artifact_text import as_text
    assert as_text({"content": "x"}) == '{"content": "x"}'
    assert artifact_text({"content": "x"}) == "x"


# ── 实参形状：模型调错了，别记成我们崩了 ──────────────────────────────────

_ARRAY_OF_OBJECT = {
    "type": "object",
    "properties": {
        "assessments": {"type": "array", "items": {"type": "object"}},
        "label": {"type": "string"},
    },
}


async def _needs_dicts(*, state, assessments, label: str = "", **_):
    return {"status": "success", "n": sum(a.get("G", 0) for a in assessments)}


register_tool(
    ToolDefinition(name="probe_needs_dicts", description="x", parameters_schema=_ARRAY_OF_OBJECT),
    _needs_dicts,
)


@pytest.mark.asyncio
async def test_wrong_argument_shape_is_a_rejection_not_a_crash(tmp_path):
    """`list[str]` 传给声明 `list[dict]` 的参数 → rejected + 说清形状。

    实测两个工具因此崩：`score_hypothesis_innovation` /
    `cluster_hypothesis_candidates` 报 `AttributeError: 'str' object has no
    attribute 'get'`。对模型三重无用：不知道是哪个参数、该是什么形状、更不知道
    是它自己调错了 —— 于是它只能换个方式再试，实测绕不过去就把这个能力丢了。

    hif_scorer 其实写了逐条 try/except，捕了 KeyError/TypeError/ValueError 三种、
    漏了 AttributeError。每个节点各补一遍、补漏一种就崩一次 —— 这种护栏该在唯一
    的派发口上扫盘，不该是 178 个工具各写一遍的名单。
    """
    state = State.new(node_type="hypothesis", base_dir=tmp_path)
    result = await execute("probe_needs_dicts", state, assessments=["H1", "H2"])

    assert result["error_code"] == tool_errors.REJECTED, "模型调错了，不是我们崩了"
    assert "第 0 个元素是 string" in result["error"]
    assert "assessments" in result["error"]
    # 合法取值要送到调用方，不能只说"错了"
    assert "本工具接受的参数" in result["error"]
    # 判决变了，证据不能少：工具原本报的错一并带回
    assert "AttributeError" in result["error"]
    assert result["argument_shape_mismatches"]


@pytest.mark.asyncio
async def test_scalar_where_object_declared_also_gets_named(tmp_path):
    """顶层类型不符同样点名（run_node 的 `'dict' 没有 .replace` 就是这一类）。"""
    async def _needs_string(*, state, path: str, **_):
        return {"status": "success", "path": path.replace("a", "b")}

    register_tool(
        ToolDefinition(name="probe_needs_string", description="x", parameters_schema={
            "type": "object", "properties": {"path": {"type": "string"}}}),
        _needs_string,
    )
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute("probe_needs_string", state, path={"file": "a.py"})
    assert result["error_code"] == tool_errors.REJECTED
    assert "path 声明是 string，实际收到 object" in result["error"]


@pytest.mark.asyncio
async def test_the_gate_only_runs_on_the_exception_path(tmp_path):
    """调用成功就当无事发生 —— 刻意宽松的工具不许被这道门改掉行为。

    `save_artifact.metadata` 声明 `type: object`，工具却故意也接受 JSON 字符串
    并解析（tests/test_artifact_metadata_contract.py 守着这个行为）。拦在调用
    **前**的类型闸会把它拒掉；这道门只走异常路径，所以它照常工作。

    这是这道门敢覆盖全部 178 个工具的原因：它不改变任何**能跑通**的调用。
    """
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute(
        "save_artifact", state,
        artifact_type="manuscript", name="loose", content="body",
        metadata='{"preflight_status": "blocked"}',      # 声明 object，给的是 string
    )
    assert result["status"] == "success", result
    assert state.read_artifact(result["id"])["metadata"] == {"preflight_status": "blocked"}


@pytest.mark.asyncio
async def test_a_real_bug_with_correct_arguments_stays_a_bug(tmp_path):
    """形状没问题却崩了 —— 那就是我们的 bug，不许被这道门洗成 rejected。"""
    async def _crashes_with_good_args(*, state, label: str = "", **_):
        return {"turns": 1}["nope"]

    register_tool(
        ToolDefinition(name="probe_good_args_crash", description="x", parameters_schema={
            "type": "object", "properties": {"label": {"type": "string"}}}),
        _crashes_with_good_args,
    )
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute("probe_good_args_crash", state, label="fine")
    assert result["error_code"] == tool_errors.TOOL_EXCEPTION
    assert result["traceback_tail"]


@pytest.mark.asyncio
async def test_provider_failure_is_not_our_code_crashing(tmp_path):
    """模型服务挂了 / 上下文超限 —— 要人看，但不该混进 bug 清单。

    实测 `RuntimeError: LLM API HTTP 400: {"error":{"message":"This model's
    maximum context length is 262144 tokens…"}}` 被记成 tool_exception，读起来
    像"平台的代码崩了"，而它说的是"这次请求太大"。判据用 core.llm 那份唯一真相源
    （重试策略共用同一份名单），不在工具层另写一份长得像的
    （[[项目-模型服务故障的归属]]）。
    """
    from core.llm import LLMHTTPError

    async def _provider_down(*, state, **_):
        raise LLMHTTPError(
            400, "LLM API HTTP 400: maximum context length is 262144 tokens",
            body='{"error":{"message":"This model\'s maximum context length is 262144 tokens"}}')

    register_tool(
        ToolDefinition(name="probe_provider_down", description="x", parameters_schema=_SCHEMA),
        _provider_down,
    )
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await execute("probe_provider_down", state)
    assert result["error_code"] == tool_errors.PROVIDER_ERROR
    assert "context length" in result["error"] or "LLMHTTPError" in result["error"]
    assert result["traceback_tail"]
