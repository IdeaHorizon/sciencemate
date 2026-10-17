"""worker 的启动不是闸（docs/RFC_WORKER_START_IS_NOT_A_GATE_20261016.md，窄单 A / F）。

三道闸：

1. **`start()` 只有一种写法。** 函数体里直接出现的调用只能是登记过的「纯代码」调用
   （设环境变量、import 出来的构造器、装回调），其余一律经 `self._probe(...)`；裸 `raise`
   一个都不许有。在 `start()` 里加一个 raise、或直接调一个会碰磁盘的函数 → 红。
2. **硬失败按类名登记。** 允许把这一轮杀掉的只有 `_HARD_START_FAILURES` 里那几种，
   子类的名字写死在这里：表外再造一种 → 红。空表 = 全违规，不是全放行。
3. **逐个破坏。** 每一项持久化前提单独弄坏，注入假模型，断言：模型照样被调用、
   `ready` 里对应的位是 False、有一条对应 code 的 environment_warning。
   真冲突（marker 写着别人）与模型不可达仍然硬失败，而且 code 各说各的。

破坏手法尽量不用 chmod：CI 容器里可能是 root，chmod 000 挡不住它。「把文件换成目录」
对 root 也成立（open 一个目录是 IsADirectoryError）。chmod 的用例另标，非 root 时多跑一遍。
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
from pathlib import Path

import pytest

import platform_runtime as pr
from core.llm import LLMResponse
from platform_runtime import serve_jsonl

RUNTIME = Path(pr.__file__)

# ─────────────────────────────────────────────────────────────────────────────
# 闸 1 / 2：AST
# ─────────────────────────────────────────────────────────────────────────────

#: `start()` 里允许**直接**调用的名字（点号路径）。这些是纯代码：设环境变量、构造器、
#: 装回调、发事件。任何会碰磁盘 / 锁 / 旧状态的步骤都不在这里 —— 它们只能经 `self._probe`。
ALLOWED_DIRECT_CALLS = frozenset({
    "_require_provider_environment",      # 唯一的 require（RFC §G1）
    "StartReport",
    "self._stack.enter_context",
    "_temporary_home",                    # enter_context 的参数：只设环境变量
    "_hide_provider_key_from_children",
    "_temporary_runs_root",
    "self._probe",
    "self.config.get",
    "session_dir_name",
    "bootstrap",
    "load_harness",
    "LLMClient",
    "TranscriptTailer",
    "asyncio.create_task",
    "self._tailer.run",
    "agent_loop.set_progress_sink",
    "set_child_event_sink",
    "self.emit",
    "self._tailer.stop",
    "self._stack.close",
    "dict",
    "list",
    "str",
    "self.state.transcript_path.resolve",
    "self.state.root.resolve",
    "self.base_dir.resolve",
})

#: 允许把这一轮杀掉的失败类型。加一种 → 这里也得加 → review 看得见。
REGISTERED_HARD_FAILURES = frozenset({"HardStartFailure", "ProjectBusyError"})
REGISTERED_HARD_SUBCLASSES = frozenset({"ProviderNotConfigured", "IdentityConflict"})


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _start_function(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PlatformSession":
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == "start":
                    return item
    raise AssertionError("PlatformSession.start not found")


def _walk_own_body(node: ast.AST):
    """遍历函数体，**不进**嵌套的 def / lambda（它们是回调，不在启动路径上跑）。"""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        stack.extend(ast.iter_child_nodes(child))


def _module_tree() -> ast.Module:
    return ast.parse(RUNTIME.read_text(encoding="utf-8"))


def test_start_has_no_raise_of_its_own():
    """`start()` 自己一个 raise 都不许有；唯一的例外是末尾 `except BaseException` 里的裸 re-raise。"""
    start = _start_function(_module_tree())
    offenders = []
    for node in _walk_own_body(start):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                bare_reraise = (
                    _dotted(handler.type) == "BaseException"
                    and any(isinstance(s, ast.Raise) and s.exc is None for s in handler.body)
                )
                if not bare_reraise:
                    continue
                for s in handler.body:
                    if isinstance(s, ast.Raise):
                        s._allowed = True  # type: ignore[attr-defined]
        if isinstance(node, ast.Raise) and not getattr(node, "_allowed", False):
            offenders.append(node.lineno)
    assert not offenders, f"start() 里有自己的 raise（行 {offenders}）：硬失败只能从 _probe 穿过去"


def test_start_calls_only_registered_code_or_probes():
    """函数体里每一个直接调用都在登记表里。会碰磁盘的步骤只能经 `self._probe`。"""
    start = _start_function(_module_tree())
    offenders = []
    for node in _walk_own_body(start):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name not in ALLOWED_DIRECT_CALLS:
                offenders.append((node.lineno, name or ast.dump(node.func)[:60]))
    assert not offenders, (
        "start() 里有未登记的直接调用（会碰磁盘的步骤只能经 self._probe）：" + repr(offenders)
    )


def test_start_has_exactly_one_require_and_it_is_the_provider():
    start = _start_function(_module_tree())
    requires = [
        node.lineno for node in _walk_own_body(start)
        if isinstance(node, ast.Call) and _dotted(node.func) == "_require_provider_environment"
    ]
    assert len(requires) == 1, "模型可达是唯一的 require"
    probes = [
        node for node in _walk_own_body(start)
        if isinstance(node, ast.Call) and _dotted(node.func) == "self._probe"
    ]
    assert len(probes) >= 8, "持久化前提都该经 probe，少于 8 个说明有的被绕回了直接调用"


def test_hard_failures_are_registered_by_name():
    names = {klass.__name__ for klass in pr._HARD_START_FAILURES}
    assert names == REGISTERED_HARD_FAILURES, f"硬失败表变了：{names}；要改先改这里的登记"
    subclasses = {
        obj.__name__ for obj in vars(pr).values()
        if isinstance(obj, type) and issubclass(obj, pr.HardStartFailure) and obj is not pr.HardStartFailure
    }
    assert subclasses == REGISTERED_HARD_SUBCLASSES, f"HardStartFailure 的子类变了：{subclasses}"
    assert REGISTERED_HARD_FAILURES and REGISTERED_HARD_SUBCLASSES, "空表 = 全违规，不是全放行"


def test_probe_lets_only_hard_failures_through():
    """`_probe` 的源码里：先 `except _HARD_START_FAILURES: raise`，再 `except Exception` 收成降级。"""
    tree = _module_tree()
    probe = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_probe":
            probe = node
    assert probe is not None
    handlers = [h for n in ast.walk(probe) if isinstance(n, ast.Try) for h in n.handlers]
    kinds = [_dotted(h.type) for h in handlers]
    assert kinds == ["_HARD_START_FAILURES", "Exception"], kinds
    assert any(isinstance(s, ast.Raise) and s.exc is None for s in handlers[0].body)
    assert not any(isinstance(s, ast.Raise) for s in handlers[1].body)


def test_the_gate_itself_is_not_vacuous():
    """登记表清空时闸必须红：至少 start() 真的在调东西。"""
    start = _start_function(_module_tree())
    calls = [n for n in _walk_own_body(start) if isinstance(n, ast.Call)]
    assert len(calls) > 10


# ─────────────────────────────────────────────────────────────────────────────
# 闸 3：逐个破坏
# ─────────────────────────────────────────────────────────────────────────────


class _FakeLLM:
    def __init__(self) -> None:
        self.seen: list = []
        self.stream_display = None

    async def chat(self, messages, **kwargs):
        self.seen.append(list(messages))
        return LLMResponse(content="跑起来了。", tool_calls=[], finish_reason="stop",
                           usage={"total_tokens": 3})


def _home(tmp_path: Path, project_id: str = "project-gate") -> Path:
    home = tmp_path / "home"
    (home / "user").mkdir(parents=True, exist_ok=True)
    (home / "user" / "PROFILE.md").write_text("# PROFILE.md\n", encoding="utf-8")
    (home / "user" / "RESEARCH_SETTINGS.md").write_text("settings\n", encoding="utf-8")
    project = home / "projects" / project_id
    project.mkdir(parents=True, exist_ok=True)
    (project / "PROJECT.md").write_text("# PROJECT.md\n", encoding="utf-8")
    return home


async def _run(tmp_path: Path, *, home: Path, state_dir: Path | None = None,
               single_tenant: bool = True, tenant: str = "tenant-gate",
               session: str = "session-gate", llm=None) -> tuple[list[dict], _FakeLLM]:
    llm = llm or _FakeLLM()
    init = {
        "op": "init", "request_id": "init-1", "tenant_id": tenant,
        "project_id": "project-gate", "session_id": session, "home_dir": str(home),
    }
    if state_dir is not None:
        init["state_dir"] = str(state_dir)
    init["home_is_single_tenant"] = single_tenant
    requests = [
        init,
        {"op": "turn", "request_id": "turn-1", "message": "开始"},
        {"op": "terminate", "request_id": "terminate-1"},
    ]
    stream = io.StringIO("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests))
    events: list[dict] = []

    def emit(event_type: str, **payload):
        events.append({"type": event_type, **payload})

    await serve_jsonl(stream, emit, llm=llm)
    return events, llm


def _ready(events: list[dict]) -> dict:
    ready = [e for e in events if e["type"] == "ready"]
    assert ready, f"没有 ready：{[(e['type'], e.get('code'), e.get('message')) for e in events][:4]}"
    return ready[0]


def _model_was_called(events: list[dict], llm: _FakeLLM) -> None:
    assert llm.seen, "模型没被调用"
    assert any(e["type"] == "result" for e in events), "这一轮没有 result"


def _warning(ready: dict, code: str) -> dict:
    hits = [w for w in ready["warnings"] if w["code"] == code]
    assert hits, f"没有 {code} 的 environment_warning：{[w['code'] for w in ready['warnings']]}"
    return hits[0]


@pytest.mark.asyncio
async def test_baseline_every_capability_reports_true(tmp_path):
    events, llm = await _run(tmp_path, home=_home(tmp_path))
    ready = _ready(events)
    _model_was_called(events, llm)
    caps = ready["capabilities"]
    for key in ("home_bound", "state_persistent", "state_bound", "lock_held",
                "activity_file", "state_loaded", "conversation_loaded", "transcript_writable"):
        assert caps.get(key) is True, (key, caps)
    # 进程内的 emit 没有落盘 sink：这是合法的，但必须**可见**，不是缺席。
    assert "durable_sink" in caps
    assert ready["warnings"] == [] or all(w["code"] == "durable_sink_unavailable" for w in ready["warnings"])


@pytest.mark.asyncio
async def test_a_single_tenant_home_rebuilds_an_unreadable_marker_with_a_backup(tmp_path):
    """个人版只有一个租户：坏掉的 marker 改名留底、重建（RFC §3.5）。事实照记：一条 degraded
    说它坏了、一条 repaired 说修好了；能力位回到 True；模型照样被调用。"""
    home = _home(tmp_path)
    (home / ".platform-tenant-identity.json").mkdir()      # 文件换成目录：root 也读不了
    events, llm = await _run(tmp_path, home=home)
    ready = _ready(events)
    _model_was_called(events, llm)
    assert ready["capabilities"]["home_bound"] is True
    broken = _warning(ready, "home_marker_unreadable")
    assert broken["severity"] == "degraded" and broken["path"].endswith(".platform-tenant-identity.json")
    fixed = _warning(ready, "home_marker_rebuilt")
    assert fixed["severity"] == "repaired"
    backups = sorted(home.glob(".platform-tenant-identity.json.bak.*"))
    assert backups and Path(fixed["backup"]) == backups[0] and backups[0].is_dir()
    assert json.loads((home / ".platform-tenant-identity.json").read_text(encoding="utf-8"))["tenant_id"] == "tenant-gate"
    # 留在这个家 —— state 仍落在这个家的 projects/ 下
    assert Path(ready["runtime_root"]).is_relative_to(home.resolve())


@pytest.mark.asyncio
async def test_home_marker_garbage_is_unreadable_not_a_conflict(tmp_path):
    home = _home(tmp_path)
    (home / ".platform-tenant-identity.json").write_text("not json", encoding="utf-8")
    events, llm = await _run(tmp_path, home=home)
    ready = _ready(events)
    _model_was_called(events, llm)
    assert "JSONDecodeError" in _warning(ready, "home_marker_unreadable")["error"]
    _warning(ready, "home_marker_rebuilt")
    backup = next(home.glob(".platform-tenant-identity.json.bak.*"))
    assert backup.read_text(encoding="utf-8") == "not json", "留底不删，能撤"


@pytest.mark.asyncio
async def test_a_multi_tenant_home_moves_to_a_private_home_when_unreadable(tmp_path):
    home = _home(tmp_path)
    (home / ".platform-tenant-identity.json").mkdir()
    events, llm = await _run(tmp_path, home=home, single_tenant=False)
    ready = _ready(events)
    _model_was_called(events, llm)
    assert ready["capabilities"]["home_bound"] is False
    assert not Path(ready["runtime_root"]).is_relative_to(home.resolve())
    assert not list(home.glob(".platform-tenant-identity.json.bak.*")), "多租户的家不动可能是别人的 marker"
    assert not [w for w in ready["warnings"] if w["severity"] == "repaired"]


@pytest.mark.asyncio
async def test_a_real_tenant_conflict_is_still_hard_and_says_so(tmp_path):
    home = _home(tmp_path)
    (home / ".platform-tenant-identity.json").write_text(
        json.dumps({"schema_version": 1, "tenant_id": "someone-else"}) + "\n", encoding="utf-8"
    )
    events, llm = await _run(tmp_path, home=home)
    assert not [e for e in events if e["type"] == "ready"]
    error = [e for e in events if e["type"] == "error"][0]
    assert error["code"] == "tenant_home_conflict"
    assert error["error_type"] == "IdentityConflict"
    assert not llm.seen


@pytest.mark.asyncio
async def test_state_dir_unavailable_falls_back_to_a_private_dir(tmp_path):
    home = _home(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    events, llm = await _run(tmp_path, home=home, state_dir=blocker / "runs")
    ready = _ready(events)
    _model_was_called(events, llm)
    assert ready["capabilities"]["state_persistent"] is False
    warning = _warning(ready, "state_dir_unavailable")
    assert warning["path"] == str(blocker / "runs")
    assert not Path(ready["runtime_root"]).is_relative_to(tmp_path.resolve())


@pytest.mark.asyncio
async def test_state_marker_unreadable_degrades(tmp_path):
    home = _home(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / ".platform-runtime-identity.json").mkdir()
    events, llm = await _run(tmp_path, home=home, state_dir=state)
    ready = _ready(events)
    _model_was_called(events, llm)
    # state 目录是会话私有的：坏 marker 不分档位，改名留底、重建
    assert ready["capabilities"]["state_bound"] is True
    _warning(ready, "state_marker_unreadable")
    fixed = _warning(ready, "state_marker_rebuilt")
    assert Path(fixed["backup"]).is_dir()
    assert json.loads((state / ".platform-runtime-identity.json").read_text(encoding="utf-8"))["session_id"] == "session-gate"
    assert Path(ready["runtime_root"]) == state.resolve()


@pytest.mark.asyncio
async def test_a_real_runtime_identity_conflict_is_still_hard(tmp_path):
    home = _home(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / ".platform-runtime-identity.json").write_text(
        json.dumps({"schema_version": 2, "tenant_id": "tenant-gate",
                    "project_id": "project-gate", "session_id": "someone-elses-session",
                    "home_dir": str(home)}) + "\n", encoding="utf-8")
    events, llm = await _run(tmp_path, home=home, state_dir=state)
    assert not [e for e in events if e["type"] == "ready"]
    error = [e for e in events if e["type"] == "error"][0]
    assert error["code"] == "runtime_identity_conflict"
    assert not llm.seen


@pytest.mark.asyncio
async def test_a_corrupt_conversation_does_not_stop_the_start(tmp_path):
    """上次的对话读不回来：core 自己已经会退到 checkpoint / 空对话（不抛），probe 在它之外
    再兜一层。两层哪一层接住都行，判据只有一条：启动不死、模型被调用、这一轮有结果。"""
    home = _home(tmp_path)
    state = tmp_path / "state"
    root = state / "orchestrator__project-gate__session__session-gate"
    root.mkdir(parents=True)
    (root / "conversation.json").write_text("{not json", encoding="utf-8")
    events, llm = await _run(tmp_path, home=home, state_dir=state)
    ready = _ready(events)
    _model_was_called(events, llm)
    assert "conversation_loaded" in ready["capabilities"]


@pytest.mark.asyncio
async def test_provider_not_configured_is_still_hard(tmp_path, monkeypatch):
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    home = _home(tmp_path)
    init = {"op": "init", "request_id": "init-1", "tenant_id": "t", "project_id": "project-gate",
            "session_id": "s", "home_dir": str(home)}
    stream = io.StringIO(json.dumps(init) + "\n")
    events: list[dict] = []
    await serve_jsonl(stream, lambda t, **p: events.append({"type": t, **p}), llm=None)
    assert events and events[0]["type"] == "error"
    assert events[0]["code"] == "provider_not_configured"
    assert events[0]["error_type"] == "ProviderNotConfigured"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="chmod 对 root / Windows 不生效；目录替换那条用例覆盖它们")
@pytest.mark.asyncio
async def test_permission_denied_on_the_marker_is_unreadable_not_a_conflict(tmp_path):
    home = _home(tmp_path)
    marker = home / ".platform-tenant-identity.json"
    marker.write_text(json.dumps({"schema_version": 1, "tenant_id": "tenant-gate"}) + "\n",
                      encoding="utf-8")
    marker.chmod(0)
    try:
        events, llm = await _run(tmp_path, home=home)
        ready = _ready(events)
        _model_was_called(events, llm)
        assert "PermissionError" in _warning(ready, "home_marker_unreadable")["error"]
        # 改名不需要文件本身可读，所以个人版照样修得好
        _warning(ready, "home_marker_rebuilt")
    finally:
        for path in home.glob(".platform-tenant-identity.json*"):
            path.chmod(0o600)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="chmod 对 root / Windows 不生效")
@pytest.mark.asyncio
async def test_when_the_home_itself_is_unwritable_the_turn_moves_to_a_private_home(tmp_path):
    """家本身写不进（连临时文件都建不出）：不分档位，只能换私有临时家 —— 修不了，但这一轮照跑，
    事实（PermissionError + 路径）在 warning 里（RFC §3.1 / §3.5）。"""
    home = _home(tmp_path)
    (home / ".platform-tenant-identity.json").write_text("not json", encoding="utf-8")
    home.chmod(0o500)
    try:
        events, llm = await _run(tmp_path, home=home)
        ready = _ready(events)
        assert llm.seen, "模型没被调用"
        assert ready["capabilities"]["home_bound"] is False
        broken = _warning(ready, "home_marker_unreadable")
        assert "PermissionError" in broken["error"]
        assert not [w for w in ready["warnings"] if w["code"] == "home_marker_rebuilt"]
        assert not Path(ready["runtime_root"]).is_relative_to(home.resolve())
    finally:
        home.chmod(0o700)


@pytest.mark.asyncio
async def test_the_agent_is_briefed_once_about_what_will_not_persist(tmp_path):
    """降级那一轮，模型收到的消息里有一行平台环境提示（RFC §3.3 第 2 条）；第二轮不再重复。"""
    home = _home(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    llm = _FakeLLM()
    init = {"op": "init", "request_id": "init-1", "tenant_id": "tenant-gate", "project_id": "project-gate",
            "session_id": "session-gate", "home_dir": str(home), "state_dir": str(blocker / "runs"),
            "home_is_single_tenant": True}
    requests = [init, {"op": "turn", "request_id": "turn-1", "message": "第一轮"},
                {"op": "turn", "request_id": "turn-2", "message": "第二轮"},
                {"op": "terminate", "request_id": "terminate-1"}]
    stream = io.StringIO("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests))
    events: list[dict] = []
    await serve_jsonl(stream, lambda t, **p: events.append({"type": t, **p}), llm=llm)
    _ready(events)
    assert len(llm.seen) == 2

    def briefs(messages):
        return [m for m in messages
                if "[平台环境提示]" in str(getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else ""))]

    first = briefs(llm.seen[0])
    assert len(first) == 1
    text = str(getattr(first[0], "content", None) or first[0].get("content"))
    assert "state_dir_unavailable" in text and str(blocker / "runs") in text
    assert "不要向用户承诺" in text
    # 第二轮：同一组 code，不再唠叨（对话里仍然只有第一轮那一条）
    assert len(briefs(llm.seen[1])) == 1
