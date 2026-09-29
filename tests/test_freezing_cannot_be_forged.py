"""冻结不能靠手写 metadata 伪造 —— 那会绕过冻结背后的整道闸。

## 病例（2026-08-22，E2E v27 真实现场）

orchestrator 要 finalize 论文，`freeze_artifact` 把它机械拒了。**那道闸是对的**：
它专防「把 reviewer 的 REVISE 洗成一份已冻结的论文」（`artifacts_extra` 里
`pending_post_node_flow` 那段）。

模型于是绕过去了：用 `save_artifact` 把同一份产物重写一遍，metadata 里直接带
`frozen: true`。盘上那份 manuscript 因此长这样：

    frozen:      True
    frozen_at:   None          ← 真冻结会写时间戳
    promotion:   （没有）       ← 真冻结会把 B 类产物晋升进 deliverables/

它自己在 `freeze_executor` 里写得很清楚：

    "executed per freeze_instruction via save_artifact re-save with frozen=true
     metadata; orchestrator freeze_artifact was mechanically rejected"

**后果不是少两个字段**：下游只要检查 `metadata.frozen` 就会把它当成已冻结，
而它绕过的正是那道防 REVISE 洗白的闸。一次普通写入让不可逆的冻结形同虚设。

同一天的现场佐证：项目级 `deliverables/` 里 prereg 和 results 都在（它们走了
真 freeze），**唯独论文不在** —— 伪造的那次没有触发晋升。（`deliverables/`
的晋升副本已随 2026-09-12「原生文件 + 账本」退场；今天真假冻结的分水岭是
账本上有没有 freeze 行，见下。）

## 判据放哪：工具面驳回，账本只认 freeze 行

第一版我放在 `State.save_artifact`（与 typed-only 同一处），理由写的是「内部
调用方和 fixture 也绕不过去」—— **那是照抄位置，不是推出来的判断**。代价当场
显形：全仓 157 条测试转红，其中 107 条在别人的节点目录里。

两者的对手不同。typed-only 挡的是「凭据被随手铸出来」，内部调用方本来就该走
生命周期工具。冻结不一样：**「先造一份已冻结的产物、再测下游怎么对待它」是
夹具的正常需求**，157 处都是。

伪造只有一个对手：**模型**。而模型碰得到这件事的地方只有注册的工具面。所以
**驳回**放在那儿（报错指名字段、指名 freeze_artifact、把「拒绝就是结论」说死）
—— 并且靠下面那条**扫盘**测试保证以后新开的写产物工具也逃不掉。

2026-09-12 记录改成「原生文件 + 账本」之后，冻结的事实只剩**一个出处**：账本
上的 freeze 行（`core/ledger.py`）。`RecordStore.save` 把 `frozen / frozen_at /
frozen_version / freeze_reason` 从每一条 save 行里剥掉 —— 不是驳回（那是工具面
的事），是让 `metadata.frozen` 不再有第二个真相源。夹具造冻结记录走
`state.mark_frozen(id)`（或 `write_record(..., frozen=True)`）；下面
`test_fixtures_can_still_build_frozen_artifacts` 钉的就是这两件事：手写
`frozen` 是空操作，`mark_frozen` 才是冻结。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest

from core.state import State


@pytest.fixture()
def writing_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_RUNS_ROOT", raising=False)
    from core.bootstrap import bootstrap

    bootstrap()
    return State.new(node_type="writing", base_dir=tmp_path / "runs",
                     project_id="forge-test")


# ── 一、原始病例：模型面的 save_artifact 不能自称已冻结 ────────────────────

@pytest.mark.parametrize("field", ["frozen", "frozen_at", "freeze_reason"])
def test_the_model_cannot_hand_write_freeze_fields(writing_state, field: str) -> None:
    """三个冻结字段都不许出现在模型写入的 metadata 里。

    只挡 `frozen` 不够：挡了它，下一次就会有人写 `frozen_at` 去骗某个只查时间戳
    的下游 —— 名单式护栏漏一个就等于没有，所以三个一起挡。

    **走真工具（execute），不直接调守卫函数**：第一版我调的是守卫本身，撤掉
    `_save_artifact` 里那行调用之后测试照样全绿 —— 守卫在，只是没接到路径上，
    正是今天反复栽的那个形。
    """
    result = asyncio.run(_call_save(writing_state, name=f"forged_{field}",
                                    metadata={field: True}))

    assert result.get("status") == "error", f"伪造冻结必须被驳回，实际拿到 {result}"
    assert writing_state.read_artifact(f"manuscript__forged_{field}") is None, (
        "被拒之后不该还留下一份产物"
    )
    message = result.get("error") or ""
    assert field in message, "报错要指名是哪个字段被拒"
    assert "freeze_artifact" in message, "必须给出真正的下一步，不能只说不行"
    assert "不要绕开" in message, (
        "这个 bug 的成因就是模型把一次机械拒绝当成了「换个工具再试」的信号；"
        "报错必须把「拒绝就是结论」说死，否则它还会去找下一条缝"
    )


def test_a_falsy_freeze_field_is_not_a_claim() -> None:
    """`frozen: false` 不是在声称冻结。

    按「键在不在」判会把 sediment 那两处炸掉 —— 它们正是先 `if not frozen`
    才写，metadata 里带着一个假值的 `frozen`。
    """
    from core.artifact_capabilities import reject_freeze_forgery

    reject_freeze_forgery({"frozen": False, "frozen_at": None})


def test_an_ordinary_save_is_untouched(writing_state) -> None:
    """收紧不能变成拦死：不带冻结字段的普通写入照常。"""
    asyncio.run(_call_save(writing_state, name="normal",
                           metadata={"format": "latex", "pdf_path": "x.pdf"}))
    record = writing_state.read_artifact("manuscript__normal")
    assert record is not None
    assert (record.get("metadata") or {}).get("format") == "latex"


def test_fixtures_can_still_build_frozen_artifacts(writing_state) -> None:
    """夹具/框架内部照旧能造一份已冻结的产物 —— 但只有一条路：`mark_frozen`。

    冻结的事实只有账本 freeze 行一个出处。进程内的调用方在 save 时手写
    `metadata.frozen` **不是驳回、也不是冻结**：键被剥掉，head 照旧未冻结 ——
    否则 `metadata.frozen` 就有了第二个真相源，读者按它判「已冻结」而账本从
    没钉死过它（正是原始病例的形状，只是换了个入口）。
    """
    saved = writing_state.save_artifact(
        artifact_type="pre_registration", name="frozen_fixture",
        content="H1: ...", metadata={"frozen": True, "frozen_at": "2026-08-22T00:00:00Z"},
    )
    head = writing_state.artifact_head(saved["id"])
    assert head is not None and head.frozen is False, (
        "save 时手写 frozen 必须是空操作 —— head 不能因此变成已冻结")
    metadata = (writing_state.read_artifact(saved["id"]) or {}).get("metadata") or {}
    assert not metadata.get("frozen") and not metadata.get("frozen_at"), (
        "剥掉的键不能从 record 里漏出来")

    # 夹具造冻结记录的那条路：账本追一行 freeze，文件一个字节不动。
    writing_state.mark_frozen(saved["id"])
    head = writing_state.artifact_head(saved["id"])
    assert head is not None and head.frozen is True
    assert head.frozen_version == head.version
    metadata = (writing_state.read_artifact(saved["id"]) or {}).get("metadata") or {}
    assert metadata.get("frozen") is True
    assert metadata.get("frozen_at"), "真冻结折进 record 时带时间戳"


def test_the_real_freeze_tool_still_works(writing_state) -> None:
    """挡住伪造之后，真冻结必须照常 —— 否则就是把病人和病一起治死了。"""
    from core.tool_registry import execute

    asyncio.run(execute(
        "save_artifact",
        writing_state,
        artifact_type="accepted_paper",
        name="real",
        content="# 正文",
        metadata={},
    ))
    result = asyncio.run(
        execute("freeze_artifact", writing_state, artifact_id="accepted_paper__real"))

    assert result["status"] == "success"
    metadata = (
        writing_state.read_artifact("accepted_paper__real") or {}
    ).get("metadata") or {}
    assert metadata.get("frozen") is True
    assert metadata.get("frozen_at"), "真冻结必须写时间戳 —— 它正是伪造货缺的那一半"
    # 冻结落在账本上：head 被钉死（frozen 且钉的就是当前版本）。伪造的那次
    # 只改了 metadata，账本从没钉过它 —— 这一条就是真假冻结的分水岭。
    # （从前这里还看 `promotion`：B 类产物冻结时抄进 deliverables/。记录改成
    # 原生文件 + 账本后没有副本这回事，冻结 = 账本钉 path@sha256。）
    head = writing_state.artifact_head("accepted_paper__real")
    assert head is not None and head.frozen is True
    assert head.frozen_version == head.version


async def _call_save(state: State, *, name: str, metadata: dict):
    from core.tool_registry import execute

    return await execute("save_artifact", state, artifact_type="manuscript",
                         name=name, content="# 正文", metadata=metadata)


# ── 二、扫盘：任何**能让模型自带 metadata 落产物**的工具都得挡 ──────────────

def _model_facing_tools_taking_metadata() -> list[str]:
    """扫出所有「模型能自带 metadata、且会落产物」的注册工具。

    扫盘不写名单：新加一个这样的工具，这里自动把它算进来，漏挡就红。
    （名单式护栏的毛病我今天已经栽过一次 —— 扫盘闸自己得了名单病。）
    """
    from core.bootstrap import bootstrap
    from core.tool_registry import all_tool_names, get_tool

    bootstrap()
    found = []
    for tool_name in all_tool_names():
        definition = get_tool(tool_name)
        schema = getattr(definition, "parameters_schema", None) or {}
        properties = schema.get("properties") or {}
        if "metadata" not in properties:
            continue
        # 只关心真会落产物的：schema 里认 artifact_type，或实现签名里认
        try:
            params = set(inspect.signature(_executor_of(tool_name)).parameters)
        except (TypeError, ValueError):
            params = set()
        if "artifact_type" in properties or "artifact_type" in params:
            found.append(tool_name)
    return found


def test_the_scan_actually_finds_something() -> None:
    """扫盘器自己不能悄悄扫成空 —— 空集合会让下面那条测试永远为真。"""
    tools = _model_facing_tools_taking_metadata()
    assert "save_artifact" in tools, (
        f"扫盘没扫到原始病例那个工具，说明扫法坏了；扫到的是 {tools}"
    )


def test_every_metadata_taking_artifact_tool_rejects_forged_freezes() -> None:
    """扫到的每一个都必须挡住伪造冻结。

    挡法可以不同（`save_artifact` 抛 PermissionError、`import_artifact` 直接把
    字段剥掉），只要**结果**上模型无法凭这次调用让产物自称已冻结就行 —— 判的是
    行为，不是实现，所以换实现不会假绿。
    """
    from core.artifact_capabilities import _FREEZE_OWNED_METADATA
    import core.artifact_capabilities as caps
    import shared.tools.library.artifact_intake as intake

    guarded, unguarded = [], []
    for tool_name in _model_facing_tools_taking_metadata():
        called = _names_actually_called(_executor_of(tool_name))
        rejects = "reject_freeze_forgery" in called
        strips = any(n.endswith("FORBIDDEN_METADATA") for n in _names_referenced(
            _executor_of(tool_name)))
        (guarded if (rejects or strips) else unguarded).append(tool_name)

    assert not unguarded, (
        f"这些工具能让模型自带 metadata 落产物，却没挡冻结字段：{unguarded}。\n"
        f"在入口调一次 `core.artifact_capabilities.reject_freeze_forgery(metadata)`，"
        f"或像 artifact_intake 那样把 {list(_FREEZE_OWNED_METADATA)} 剥掉。\n"
        f"已挡住的：{guarded}"
    )
    assert set(_FREEZE_OWNED_METADATA) <= set(intake._FORBIDDEN_METADATA), (
        "artifact_intake 自己那份剥离名单必须覆盖全部冻结字段，"
        "否则 import_artifact 就是第二个正门"
    )
    assert caps  # 引用一下，免得 lint 把 import 当没用的


def _names_actually_called(func) -> set[str]:
    """函数体里**真被调用**的名字。

    早先这里查的是 `"reject_freeze_forgery" in inspect.getsource(...)` —— 撤掉
    那行调用、留下 import，字符串照样命中，测试假绿。查 Call 节点才是查接线。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def _names_referenced(func) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def _executor_of(tool_name: str):
    """拿工具的**实现函数** —— executor 不挂在 ToolDefinition 上，在 registry 里另存。"""
    from core.tool_registry import _REGISTRY

    executor = _REGISTRY.executors.get(tool_name)
    assert executor is not None, f"{tool_name} 注册了定义却没有 executor"
    return executor
