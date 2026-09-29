"""执行器接口（core.isolation）本身的契约 + 咽喉真的走它。

三类断言，各自对应一种变异会照出的缺陷：

1. **选择**：``HARNESS_EXECUTOR`` 非法值必须把合法值念给调用方（契约送到调用方），
   ``auto`` 就是本平台的原生后端，没有 Docker 兜底。
2. **记账**：一条 run 只记一次 ``isolation_enforcement``，字段说清守到了什么、
   离无人值守 / 人在场最低集合各差什么。
3. **接线**：``spawn_and_wait`` 拿后端只经 ``select_backend``，起的是后端交回的
   argv。把咽喉里那两行改回直接调 ``core.sandbox``，这里立刻红 —— 这是
   [[feedback_grep_for_a_name_is_not_wiring]] 的判据：走真入口，看后端有没有被调。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from core import isolation
from core.isolation import (
    ATTENDED_MINIMUM,
    UNATTENDED_MINIMUM,
    UNRECOVERABLE,
    CommandSpec,
    Invariant,
    IsolationContractError,
    enforcement_record,
    record_enforcement_once,
    select_backend,
)
from shared.lib.cancellable_subprocess import spawn_and_wait


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv(isolation.EXECUTOR_ENV, raising=False)
    monkeypatch.delenv(isolation.POLICY_ENV, raising=False)
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


class _State:
    """够 spawn_and_wait 和记账用的最小 state：有 transcript、没有 kill_event。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


class _StubLaunch:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.terminated = False
        self.cleaned = False

    def terminate(self, *, remove: bool = False) -> None:
        del remove
        self.terminated = True

    def cleanup(self) -> None:
        self.cleaned = True


class _StubBackend:
    name = "stub"

    def __init__(self, caps: frozenset[Invariant]) -> None:
        self._caps = caps
        self.specs: list[CommandSpec] = []
        self.launch: _StubLaunch | None = None

    def capabilities(self) -> frozenset[Invariant]:
        return self._caps

    def prepare(self, spec: CommandSpec, *, state) -> _StubLaunch:
        del state
        self.specs.append(spec)
        # 起一个真进程，把模型 argv 原样回显，证明咽喉起的是**后端交回的** argv。
        self.launch = _StubLaunch([sys.executable, "-c",
                                   "import sys, json; print(json.dumps(sys.argv[1:]))",
                                   *spec.argv])
        return self.launch


# ── 1. 选择 ───────────────────────────────────────────────────────────────────


def test_auto_is_the_native_backend_and_nothing_else() -> None:
    """auto = 本平台原生后端。没有别的后端了（Docker 随 PR C 删除）。"""
    native = isolation.native_backend_name()
    if native is None:
        pytest.skip("no native backend on this platform")
    chosen = select_backend()
    assert chosen.name == native
    assert select_backend("auto") is chosen
    assert select_backend(native) is chosen, "同名后端是同一个实例"
    with pytest.raises(IsolationContractError, match="valid values"):
        select_backend("image")


class _DeadNative:
    unavailable_reason = "probe says no"

    def __init__(self, name: str) -> None:
        self.name = name

    def capabilities(self):
        return frozenset()

    def prepare(self, spec, *, state):
        raise AssertionError("must not be used")


def test_auto_never_falls_back_to_docker(monkeypatch) -> None:
    """原生后端守不住写边界 → 拒绝并说修法，**不去问 Docker**（wangd 09-04 定的）。"""
    native = isolation.native_backend_name()
    if native is None:
        pytest.skip("no native backend on this platform")
    from core import sandbox

    monkeypatch.setitem(isolation._cache, native, _DeadNative(native))

    def _asked_docker(*_a, **_k):
        raise AssertionError("auto 去问了 Docker")

    monkeypatch.setattr(sandbox, "availability", _asked_docker)
    with pytest.raises(IsolationContractError) as exc:
        select_backend("auto")
    message = str(exc.value)
    assert "probe says no" in message, "为什么不行要念出来"
    assert "image" not in message, "Docker 没有了，报错里不该再指向它"
    assert any(marker in message for marker in ("bubblewrap", "sandbox-exec", "Low-integrity")), \
        "修法要念出来（按本平台的原生后端）"


def test_auto_refuses_on_platforms_without_a_native_backend(monkeypatch) -> None:
    monkeypatch.setattr(isolation, "native_backend_name", lambda: None)
    with pytest.raises(IsolationContractError, match="no native isolation backend"):
        select_backend("auto")


def test_unknown_backend_name_lists_the_valid_values(monkeypatch) -> None:
    monkeypatch.setenv(isolation.EXECUTOR_ENV, "gvisor")
    with pytest.raises(IsolationContractError) as exc:
        select_backend()
    message = str(exc.value)
    assert "gvisor" in message
    for valid in ("auto", "darwin", "linux", "win32"):
        assert valid in message, "合法取值必须念给调用方"


@pytest.mark.parametrize(("raw", "expected"), [("", "personal"), ("strict", "strict"),
                                               ("Personal", "personal")])
def test_policy_defaults_to_personal(monkeypatch, raw: str, expected: str) -> None:
    if raw:
        monkeypatch.setenv(isolation.POLICY_ENV, raw)
    assert isolation.enforcement_policy() == expected


def test_unknown_policy_lists_the_valid_values(monkeypatch) -> None:
    monkeypatch.setenv(isolation.POLICY_ENV, "paranoid")
    with pytest.raises(IsolationContractError, match="strict"):
        isolation.enforcement_policy()


def test_command_spec_rejects_empty_argv_and_missing_roots(tmp_path: Path) -> None:
    with pytest.raises(IsolationContractError, match="argv"):
        CommandSpec(argv=(), cwd=str(tmp_path), writable_roots=(tmp_path,))
    with pytest.raises(IsolationContractError, match="argv"):
        CommandSpec(argv=("a\x00b",), cwd=str(tmp_path), writable_roots=(tmp_path,))
    with pytest.raises(IsolationContractError, match="writable root"):
        CommandSpec(argv=("true",), cwd=str(tmp_path), writable_roots=())


def test_minimum_sets_are_nested_the_way_the_rfc_says() -> None:
    assert UNRECOVERABLE < ATTENDED_MINIMUM < UNATTENDED_MINIMUM
    # 可恢复类只在无人值守档才是硬要求
    assert {Invariant.MEM_CAP, Invariant.PIDS_CAP} <= (
        UNATTENDED_MINIMUM - ATTENDED_MINIMUM
    )


# ── 2. 记账 ───────────────────────────────────────────────────────────────────



def test_a_weak_backend_reports_exactly_what_it_lacks(monkeypatch) -> None:
    monkeypatch.setenv(isolation.POLICY_ENV, "strict")
    weak = _StubBackend(ATTENDED_MINIMUM)  # 像 Mac 上的 seatbelt：没有内存硬墙
    record = enforcement_record(weak)
    assert record.policy == "strict"
    assert record.missing_for_attended == ()
    assert set(record.missing_for_unattended) == {"mem_cap", "pids_cap"}


def test_enforcement_is_recorded_once_per_state() -> None:
    state = _State()
    backend = _StubBackend(UNATTENDED_MINIMUM)
    first = record_enforcement_once(state, backend)
    second = record_enforcement_once(state, backend)
    assert first is not None and second is None
    events = [e for e in state.events if e["event"] == "isolation_enforcement"]
    assert len(events) == 1
    assert events[0]["backend"] == "stub"
    assert events[0]["missing_for_unattended"] == []
    assert json.dumps(events[0])  # 可序列化，进 transcript 不会炸


def test_record_works_for_the_real_unhashable_state_and_for_rigid_doubles(tmp_path) -> None:
    """core.state.State 是 eq=True 的 dataclass → 不可 hash；WeakSet 版记账在这里炸过。"""
    from core.state import State

    backend = _StubBackend(UNATTENDED_MINIMUM)
    real = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs", project_id="p")
    assert record_enforcement_once(real, backend) is not None
    assert record_enforcement_once(real, backend) is None
    assert any(json.loads(l)["event"] == "isolation_enforcement"
               for l in real.transcript_path.read_text(encoding="utf-8").splitlines() if l.strip())

    class _Rigid(tuple):  # 不可 weakref 的对象也不能让记账炸
        __slots__ = ()

    rigid = _Rigid()
    assert record_enforcement_once(rigid, backend) is not None
    assert record_enforcement_once(rigid, backend) is None


# ── 3. 接线 ───────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def test_spawn_and_wait_reaches_the_os_through_the_selected_backend(monkeypatch, tmp_path):
    backend = _StubBackend(UNATTENDED_MINIMUM)
    monkeypatch.setattr(isolation, "select_backend", lambda name=None: backend)
    state = _State()

    status, rc, out, err = _run(spawn_and_wait(
        "echo", "hello-from-model", state=state, timeout=20,
        writable_roots=[tmp_path],
    ))

    assert (status, rc) == ("done", 0), err
    assert json.loads(out.decode()) == ["echo", "hello-from-model"], (
        "起的不是后端交回的 argv —— 咽喉绕开了后端")
    assert len(backend.specs) == 1
    spec = backend.specs[0]
    assert spec.writable_roots == (tmp_path,)
    assert spec.cwd == str(tmp_path.resolve())
    assert spec.network_access is False
    assert spec.limits.walltime_seconds == 20, "timeout 要落到 limits.walltime_seconds"
    assert backend.launch is not None and backend.launch.cleaned, "launch.cleanup 没被调"
    assert [e["event"] for e in state.events] == ["isolation_enforcement"]


def test_shell_commands_are_wrapped_before_they_reach_the_backend(monkeypatch, tmp_path):
    backend = _StubBackend(UNATTENDED_MINIMUM)
    monkeypatch.setattr(isolation, "select_backend", lambda name=None: backend)

    status, _rc, out, _err = _run(spawn_and_wait(
        "echo a && echo b", state=_State(), timeout=20, shell=True,
        writable_roots=[tmp_path],
    ))

    assert status == "done"
    # shell=True 的包法要走 the_shell（POSIX /bin/sh；Windows 是随包 MSYS bash），
    # 别把 "/bin/sh" 写死 —— 那是 P0-6 之前的 POSIX 假设，Windows 上会红。
    from shared.lib.shell import posix_shell
    assert json.loads(out.decode()) == [posix_shell(), "-c", "echo a && echo b"]


def test_network_access_is_carried_to_the_backend_as_a_fact(monkeypatch, tmp_path):
    backend = _StubBackend(UNATTENDED_MINIMUM)
    monkeypatch.setattr(isolation, "select_backend", lambda name=None: backend)

    _run(spawn_and_wait("true", state=_State(), timeout=5,
                        writable_roots=[tmp_path], network_access=True))

    assert backend.specs[0].network_access is True


def test_a_bad_executor_name_becomes_spawn_failed_with_the_valid_values(monkeypatch, tmp_path):
    monkeypatch.setenv(isolation.EXECUTOR_ENV, "nope")
    status, rc, _out, err = _run(spawn_and_wait(
        "true", state=_State(), timeout=5, writable_roots=[tmp_path]))
    assert (status, rc) == ("spawn_failed", None)
    assert b"nope" in err and b"darwin" in err and b"linux" in err and b"win32" in err


def test_framework_internal_commands_do_not_touch_the_backend(monkeypatch, tmp_path):
    def _boom(name=None):
        raise AssertionError("writable_roots=None 的框架内部命令不该问后端")

    monkeypatch.setattr(isolation, "select_backend", _boom)
    status, rc, out, _err = _run(spawn_and_wait(
        sys.executable, "-c", "print('internal')", state=_State(), timeout=10))
    assert (status, rc) == ("done", 0)
    assert out.strip() == b"internal"
