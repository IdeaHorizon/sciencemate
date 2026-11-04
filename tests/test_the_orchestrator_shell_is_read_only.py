"""调度器的 shell 是只读探查：写边界为空，有副作用的命令做不成，返回值指路派节点（#1187）。

wangd 10-21 裁决：做成机制而不是 prompt —— ``_orchestrator`` 的 ``run_bash`` /
``execute_python`` 沙箱收成**只读探测**（写边界为空、不给作业提交），任何有副作用的命令
被拒并提示派 Experiment / Data；「这一步是科学执行」的机械判据就是「有副作用」。

此前：写根照旧是调度器自己的作用域 ``notes/`` + run 目录，``probe_only_shell_violation``
只按命令文本记一笔账，命令照跑、写照落 —— 调度器用 heredoc / ``python -c`` 替
experiment 写代码、跑计算、落结果，全都真的落了地。

判据落在**效果**上：对这台机器上真能起来的原生后端真起一次进程、真写一次，断言宿主上
那个文件到底变没变；不落在 argv 或 seatbelt 规则长什么样上。没有能守住写边界的后端
（没有 bwrap / Landlock 的 Linux、Windows CI）→ 真跑的几条跳过，接线的几条照跑。

变异判据（手动验过会转红）：

* ``core.sandbox.model_tool_wall`` 不看 ``shell_is_read_only``（调度器拿回作用域 + run
  目录）→ ``…cannot_create_or_change…`` 与 ``…tool_wall_follows_the_flag`` 红。
* ``write_layers`` 无视 ``persistent_cache``（缓存照旧放行）→
  ``test_the_persistent_cache_is_not_a_drawer`` 红。
* ``execute_python`` 在只读墙下仍认 ``_sandbox_writable_roots`` →
  ``test_a_smuggled_writable_root_does_not_widen_the_wall`` 红。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import isolation
from core import tool_errors as errs
from core.bootstrap import bootstrap
from core.isolation import CommandSpec, IsolationContractError, Invariant, select_backend
from core.loader import load_harness
from core.sandbox import model_tool_roots, model_tool_wall
from core.state import State
from core.tool_registry import execute as execute_tool

bootstrap()

NATIVE = "darwin" if sys.platform == "darwin" else "linux"


@pytest.fixture(autouse=True)
def _native(monkeypatch):
    monkeypatch.setenv(isolation.EXECUTOR_ENV, NATIVE)
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


def _backend_ready() -> bool:
    if sys.platform not in ("darwin",) and not sys.platform.startswith("linux"):
        return False
    try:
        backend = select_backend(NATIVE)
    except IsolationContractError:
        return False
    return Invariant.WRITE_BOUNDARY in backend.capabilities()


live = pytest.mark.skipif(
    not _backend_ready(),
    reason=f"native backend {NATIVE} cannot enforce the write boundary on this host",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """一个真 Project：调度器的作用域 notes/ 里已经有一份笔记，别的节点各有自己的目录。"""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    for directory in ("notes", "literature", "experiments", "data", "figures"):
        (root / directory).mkdir()
        (root / directory / "README.md").write_text(f"# {directory}\n", encoding="utf-8")
    (root / "notes" / "findings.md").write_text("ORIGINAL\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-q", "-m", "Initialize Project")
    return root


def _orchestrator(tmp_path: Path, project: Path) -> State:
    state = State.new("_orchestrator", tmp_path / "runtime", project_id="p1",
                      project_worktree=project)
    # run_loop 每轮开头按 harness 写进来的那个值（test_shell_probe_only 钉着装配链）。
    state.hook_state["_shell_probe_only"] = bool(load_harness("_orchestrator").shell_probe_only)
    return state


def _events(state: State, name: str) -> list[dict]:
    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("event") == name]


def _bash(state: State, cmd: str, **kw) -> dict:
    return asyncio.run(execute_tool("run_bash", state, cmd=cmd, timeout=60, **kw))


def _python(state: State, code: str, **kw) -> dict:
    return asyncio.run(execute_tool("execute_python", state, code=code, timeout=60, **kw))


def _assert_pointed_at_the_nodes(res: dict) -> None:
    assert res["status"] == "error", res
    assert res["error_code"] == errs.REJECTED, res
    assert res.get("read_only_shell") is True and res.get("write_boundary") == "none", res
    assert res.get("recommended_nodes") == ["experiment", "data"], res
    assert "experiment" in res["error"] and "data" in res["error"], res["error"]


# ══ 1. 墙怎么定（不起进程）══════════════════════════════════════════════════════


def test_the_tool_wall_follows_the_flag(tmp_path: Path, project: Path) -> None:
    """同一个 state：旗子开 → 一个可写根都没有、ephemeral；旗子关 → 原样是作用域 + run 目录。"""
    state = _orchestrator(tmp_path, project)
    wall = model_tool_wall(state)
    assert wall.ephemeral is True and wall.writable_roots == []
    assert project.resolve() in wall.readonly_roots

    state.hook_state["_shell_probe_only"] = False
    wall = model_tool_wall(state)
    writable, readonly = model_tool_roots(state)
    assert wall.ephemeral is False
    assert (wall.writable_roots, wall.readonly_roots) == (writable, readonly)


def test_only_the_orchestrator_harness_turns_it_on() -> None:
    assert load_harness("_orchestrator").shell_probe_only is True
    for node in ("experiment", "data", "literature", "postprocess", "writing", "_reviewer"):
        assert load_harness(node).shell_probe_only is False, node


def test_the_orchestrator_is_handed_no_job_submission() -> None:
    """「不给作业提交」：工具面上没有任何起作业 / 登记作业的口子。shell 里起的后台进程
    随命令结束整组收掉（下面真跑那条），网络关着 —— sbatch 也够不着调度器。"""
    job_tools = {"submit_job", "declare_job", "job_progress", "job_status", "cancel_job",
                 "wait_for_external_job", "finalize_external_job", "check_external_job_health",
                 "safe_run_bash", "safe_execute_python", "fetch_resource"}
    assert not job_tools & set(load_harness("_orchestrator").tools)


def test_an_ephemeral_command_declares_no_writable_root(tmp_path: Path) -> None:
    CommandSpec(argv=("true",), cwd=str(tmp_path), writable_roots=(), ephemeral=True)
    with pytest.raises(IsolationContractError, match="ephemeral"):
        CommandSpec(argv=("true",), cwd=str(tmp_path), writable_roots=(tmp_path,),
                    ephemeral=True)
    with pytest.raises(IsolationContractError, match="writable root"):
        CommandSpec(argv=("true",), cwd=str(tmp_path), writable_roots=())


def test_the_wsl_handoff_carries_ephemeral(tmp_path: Path) -> None:
    """WSL 目标在发行版里重建 CommandSpec：这一位不跟着走，发行版那头就会拒（没有可写根）
    或者 —— 更糟 —— 被谁顺手补一个根。"""
    from core.isolation.wsl import WslBackend

    backend = WslBackend("Ubuntu-24.04")
    backend._probed = True
    backend._mount_prefix = "/mnt/"
    backend._entry_linux = "/mnt/c/afs/core/isolation/_wsl_entry.py"
    backend._report = {"caps": ["write_boundary", "git_unwritable", "net_deny", "walltime",
                                "group_kill"], "net_scope": "all"}
    backend.to_linux = lambda p: f"/mnt/x{p}" if str(p).startswith("/") else f"/mnt/c/{p}"
    limits = SimpleNamespace(memory_bytes=0, pids=0, cpus=0, walltime_seconds=30)
    spec = CommandSpec(argv=("ls",), cwd=str(tmp_path), writable_roots=(),
                       readonly_roots=(tmp_path,), limits=limits, ephemeral=True)
    launch = backend.prepare(spec, state=None)
    try:
        handed = json.loads((launch.scratch_dir / "spec.json").read_text(encoding="utf-8"))
        assert handed["ephemeral"] is True and handed["writable"] == []
    finally:
        launch.cleanup()


@pytest.mark.parametrize("err", [
    "/bin/sh: notes/x.md: Operation not permitted",                       # darwin seatbelt
    "touch: cannot touch 'x': Read-only file system",                     # linux bwrap
    "PermissionError: [Errno 13] Permission denied: 'notes/x.md'",        # linux Landlock
    "Out-File : Access is denied.",                                       # Windows 原生
])
def test_each_backend_s_refusal_is_recognised(err: str) -> None:
    from shared.lib.dangerous_commands import read_only_shell_refusal

    got = read_only_shell_refusal("", err, 1)
    assert got and got["recommended_nodes"] == ["experiment", "data"], err


def test_data_that_merely_mentions_a_refusal_is_not_a_refusal() -> None:
    """成功的命令只看 stderr：``cat`` 一份写着 Permission denied 的日志不是撞墙。"""
    from shared.lib.dangerous_commands import read_only_shell_refusal

    log = "2026-11-03 ssh: Permission denied (publickey)\n"
    assert read_only_shell_refusal(log, "", 0) is None
    assert read_only_shell_refusal(log, "", 1) is not None, "失败的命令 stdout 也看（2>&1）"
    assert read_only_shell_refusal("done\n", "sh: f: Operation not permitted", 0) is not None


# bwrap 不「拒」没声明的 /tmp 路径，而是把它**藏**起来（空 tmpfs 铺在 /tmp 上，只挂回声明
# 过的根）：写撞上的是「父目录不存在」。node20 真机 11-04，run 目录住在 /tmp 下时：
#   /bin/sh: 1: cannot create /tmp/hf-1187-probe/runtime/smuggled.json: Directory nonexistent
_CREATE_FORMS = [
    ("/bin/sh: 1: cannot create {p}: Directory nonexistent", 2),              # dash 重定向
    ("touch: cannot touch ‘{p}’: No such file or directory", 1),              # coreutils（UTF-8 引号）
    ("mkdir: cannot create directory '{p}': No such file or directory", 1),
    ("cp: cannot create regular file '{p}': No such file or directory", 1),
    ("tee: {p}: No such file or directory", 1),
    ("/bin/sh: line 1: {p}: No such file or directory", 1),                   # bash 作 /bin/sh
    ("FileNotFoundError: [Errno 2] No such file or directory: '{p}'", 1),     # Python open(…,'w')
]


@pytest.mark.parametrize("form,rc", _CREATE_FORMS)
def test_a_write_into_a_directory_the_wall_hid_is_a_refusal(tmp_path: Path, form: str,
                                                             rc: int) -> None:
    """父目录在宿主上**存在**、墙里却「不存在」→ 是墙藏的，算撞墙。"""
    from shared.lib.dangerous_commands import read_only_shell_refusal

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "smuggled.json"
    got = read_only_shell_refusal("", form.format(p=target), rc)
    assert got, form
    assert got["hidden_path"] == str(target) and "hidden by the wall" in got["os_refusal"]
    assert got["recommended_nodes"] == ["experiment", "data"]


@pytest.mark.parametrize("form,rc", _CREATE_FORMS)
def test_a_genuinely_missing_directory_is_the_command_s_own_mistake(tmp_path: Path, form: str,
                                                                     rc: int) -> None:
    """父目录在宿主上也没有 = 路径写错了，普通失败，不挂指路。"""
    from shared.lib.dangerous_commands import read_only_shell_refusal

    target = tmp_path / "no-such-dir" / "smuggled.json"
    assert read_only_shell_refusal("", form.format(p=target), rc) is None, form


def test_a_missing_file_where_the_wall_shows_the_project_is_just_missing(tmp_path: Path) -> None:
    """项目 worktree 是墙挂回来给它看的只读根：在那里「不存在」就是真不存在（读一份缺失的
    CSV），不是墙藏的。"""
    from shared.lib.dangerous_commands import read_only_shell_refusal

    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    err = f"FileNotFoundError: [Errno 2] No such file or directory: '{project}/data/missing.csv'"
    assert read_only_shell_refusal("", err, 1, shown=[project]) is None
    assert read_only_shell_refusal("", err, 1) is not None, "没有 shown 时这条就会被误认 —— 守卫有用"


def test_probing_for_a_path_is_not_a_write(tmp_path: Path) -> None:
    """「X 在不在」是探查本身：ls / cat 的不存在、执行一个不存在的程序、相对路径都不算撞墙。"""
    from shared.lib.dangerous_commands import read_only_shell_refusal

    present = tmp_path / "opt" / "bin"
    present.mkdir(parents=True)
    for err, rc in [
        (f"ls: cannot access '{present}/nvcc': No such file or directory", 2),
        (f"cat: {present}/README: No such file or directory", 1),
        (f"/bin/sh: line 1: {present}/lmp: No such file or directory", 127),
        ("/bin/sh: 1: cannot create out.txt: Directory nonexistent", 2),
    ]:
        assert read_only_shell_refusal("", err, rc) is None, err


@pytest.mark.parametrize("where,expected", [("run", errs.REJECTED), ("project", errs.COMMAND_FAILED)])
def test_run_bash_tells_hidden_from_missing(tmp_path: Path, project: Path, monkeypatch,
                                            where: str, expected: str) -> None:
    """接线：run_bash 把墙给它看的根（项目）交给识别器。bwrap 藏起来的 run 目录 → 撞墙；
    项目里（墙挂回来了）的「不存在」→ 普通失败。不需要 bwrap：输出按 node20 的原样替身。"""
    state = _orchestrator(tmp_path, project)
    target = (Path(state.root) / "smuggled.json" if where == "run"
              else project / "notes" / "missing.md")

    async def _bwrap_said(cmd, **kw):
        assert kw.get("sandbox_ephemeral") is True
        msg = f"/bin/sh: 1: cannot create {target}: Directory nonexistent\n"
        return "done", 2, b"", msg.encode()

    monkeypatch.setattr("shared.tools.builtin.spawn_and_wait", _bwrap_said)
    res = _bash(state, f"echo x > '{target}'")
    assert res["error_code"] == expected, res
    assert ("read_only_shell" in res) is (where == "run"), res


def test_requirements_are_refused_before_anything_is_installed(tmp_path: Path,
                                                               project: Path) -> None:
    """装依赖 = 开网下载 + 往 run 目录写包。装包器是框架拼的命令、带着自己的可写根，
    不在只读墙后面 —— 所以必须在它起之前说不（不需要后端，这条哪儿都跑）。"""
    state = _orchestrator(tmp_path, project)
    res = _python(state, "import numpy; print(numpy.__version__)", requirements=["numpy"])
    _assert_pointed_at_the_nodes(res)
    assert not (Path(state.root) / ".harness" / "python-packages").exists()
    assert _events(state, "read_only_shell_refused")


# ══ 2. 真起进程：调度器读得到、写不进 ═══════════════════════════════════════════


@live
def test_a_probe_still_reads_the_project_and_the_machine(tmp_path: Path, project: Path) -> None:
    state = _orchestrator(tmp_path, project)
    res = _bash(state, f"cat '{project}/notes/findings.md'; ls '{project}'; "
                       "command -v python3 >/dev/null && echo has-python; "
                       "python3 -c 'print(6*7)'")
    assert res["status"] == "success", res
    out = res["stdout_tail"]
    assert "ORIGINAL" in out and "literature" in out and "has-python" in out and "42" in out
    assert "read_only_shell" not in res, "纯读不该挂撞墙说明"

    res = _python(state, f"print(open({str(project / 'notes' / 'findings.md')!r}).read())")
    assert res["status"] == "success" and "ORIGINAL" in res["stdout_tail"], res


@live
def test_it_cannot_create_or_change_a_file_in_the_project_or_its_scope(
        tmp_path: Path, project: Path) -> None:
    state = _orchestrator(tmp_path, project)
    notes = project / "notes"
    attempts = {
        "在自己的作用域里新建": (f"echo result > '{notes}/new.md'", notes / "new.md"),
        "改自己作用域里已有的笔记": (f"echo TAMPERED >> '{notes}/findings.md'", None),
        "在项目根新建": (f"echo x > '{project}/result.csv'", project / "result.csv"),
        "替 experiment 落结果": (f"mkdir -p '{project}/experiments/run1'",
                              project / "experiments" / "run1"),
        "相对路径落进 run 目录（默认 cwd）": ("echo x > out.txt",
                                     Path(state.root) / "scratch" / "out.txt"),
        "直接写 run 目录": (f"echo x > '{state.root}/smuggled.json'",
                       Path(state.root) / "smuggled.json"),
    }
    for why, (cmd, must_not_exist) in attempts.items():
        res = _bash(state, cmd)
        _assert_pointed_at_the_nodes(res)
        if must_not_exist is not None:
            assert not must_not_exist.exists(), f"{why}：写落地了"
    assert (notes / "findings.md").read_text(encoding="utf-8") == "ORIGINAL\n"
    refused = _events(state, "read_only_shell_refused")
    assert len(refused) == len(attempts) and all(e["tool"] == "run_bash" for e in refused)


@live
def test_a_write_that_fails_inside_a_succeeding_command_is_still_named(
        tmp_path: Path, project: Path) -> None:
    """``echo x > f; echo done``：写被拒了，命令仍 0 退出 —— 不翻成失败，但说明照挂。"""
    state = _orchestrator(tmp_path, project)
    target = project / "notes" / "partial.md"
    res = _bash(state, f"echo x > '{target}'; echo done")
    assert res["status"] == "success" and "done" in res["stdout_tail"], res
    assert not target.exists()
    assert res.get("read_only_shell") is True and "experiment" in res["read_only_shell_note"]
    assert "error" not in res, res


@live
def test_execute_python_hits_the_same_wall(tmp_path: Path, project: Path) -> None:
    state = _orchestrator(tmp_path, project)
    target = project / "notes" / "fit.json"
    res = _python(state, f"open({str(target)!r}, 'w').write('{{}}')")
    _assert_pointed_at_the_nodes(res)
    assert not target.exists()

    res = _python(state, "print(sum(range(10)))")
    assert res["status"] == "success" and "45" in res["stdout_tail"], res


@live
def test_a_smuggled_writable_root_does_not_widen_the_wall(tmp_path: Path, project: Path) -> None:
    """``_sandbox_writable_roots`` 是模型实参里也能出现的名字：只读墙下一个都不认。"""
    state = _orchestrator(tmp_path, project)
    target = project / "notes" / "smuggled.txt"
    res = _python(state, f"open({str(target)!r}, 'w').write('x')",
                  _sandbox_writable_roots=[str(project / "notes")])
    _assert_pointed_at_the_nodes(res)
    assert not target.exists()


@live
def test_the_persistent_cache_is_not_a_drawer(tmp_path: Path, project: Path,
                                              monkeypatch) -> None:
    """持久缓存是声明根之外唯一跨命令留得下东西的地方 —— 调度器写进去的结果下一轮还在，
    「只读」就成了「只读，除了这一块」。缓存根改到 tmp：不碰本机真缓存。"""
    from core.isolation import _native

    monkeypatch.setattr(_native, "cache_root", lambda: tmp_path / "host-caches")
    cache = _native.cache_scratch()
    assert cache is not None
    drawer = cache / f"drawer-{uuid.uuid4().hex[:8]}.json"

    state = _orchestrator(tmp_path, project)
    res = _bash(state, f"echo result > '{drawer}'")
    _assert_pointed_at_the_nodes(res)
    assert not drawer.exists()

    # 程序照样有缓存可写（不刷 "cache unwritable"）—— 只是它在这条命令自己的 scratch 里。
    res = _bash(state, 'printf x > "$XDG_CACHE_HOME/probe" && echo "$XDG_CACHE_HOME"')
    assert res["status"] == "success", res
    assert Path(res["stdout_tail"].strip()).resolve() != cache.resolve()


@live
def test_tmpdir_is_writable_and_gone_with_the_command(tmp_path: Path, project: Path) -> None:
    """唯一可写的是这条命令自己的 $TMPDIR —— 写得进（探查要临时文件），命令一结束就没了。"""
    state = _orchestrator(tmp_path, project)
    res = _bash(state, 'echo hi > "$TMPDIR/x" && cat "$TMPDIR/x" && echo "$TMPDIR"')
    assert res["status"] == "success", res
    lines = res["stdout_tail"].strip().splitlines()
    assert lines[0] == "hi"
    assert not Path(lines[-1]).exists(), "私有 scratch 活过了命令"


@live
def test_a_background_job_does_not_outlive_the_probe(tmp_path: Path, project: Path) -> None:
    """不起作业：后台进程随这条命令结束整组收掉，返回值照实说它已经没了。"""
    state = _orchestrator(tmp_path, project)
    res = _bash(state, "nohup sleep 37 >/dev/null 2>&1 & echo $!")
    pid = int(res["stdout_tail"].strip().splitlines()[-1])
    from shared.lib import process_control

    # 用框架自己的判活：僵尸算死。`os.kill(pid, 0)` 对僵尸也返回成功，而 CI 容器的 PID 1
    # 不收割孤儿 —— 进程早被收掉了，它的表项还在（PR #1307 第一次 CI 就是这样红的）。
    deadline = time.monotonic() + 10
    while process_control.alive(pid):
        assert time.monotonic() < deadline, f"后台进程 {pid} 活过了调度器的命令"
        time.sleep(0.1)
    assert "收掉" in res.get("job_registry_reminder", ""), res
    assert "declare_job" not in res["job_registry_reminder"], "指它去登记一个它没有的工具"
    assert res.get("recommended_nodes") == ["experiment", "data"], res


# ══ 3. 别的节点不受影响 ═══════════════════════════════════════════════════════


@live
def test_another_node_still_writes_its_own_scope(tmp_path: Path, project: Path) -> None:
    state = State.new("literature", tmp_path / "runtime-lit", project_id="p1",
                      project_worktree=project)
    target = project / "literature" / "out.txt"
    res = _bash(state, f"echo data > '{target}'")
    assert res["status"] == "success", res
    assert target.read_text(encoding="utf-8").strip() == "data"
    assert "read_only_shell" not in res
    # 它的墙也没被放宽：别人的目录照旧写不进，也没有调度器那段说明。
    res = _bash(state, f"echo x > '{project}/notes/new.md'")
    assert res["status"] == "error" and res["error_code"] == errs.COMMAND_FAILED, res
    assert "read_only_shell" not in res
    assert not (project / "notes" / "new.md").exists()

    plot = State.new("postprocess", tmp_path / "runtime-fig", project_id="p1",
                     project_worktree=project)
    fig = project / "figures" / "fig.txt"
    res = _python(plot, f"open({str(fig)!r}, 'w').write('ok')")
    assert res["status"] == "success", res
    assert fig.read_text(encoding="utf-8") == "ok"
