"""可写面的三条边界：只读 carve-out、共享 scratch、宿主 unix socket。

每条都对**这台机器上真能起来的原生后端**真起一次进程、真写一次/真连一次，断言落在
效果上（宿主上那个文件到底变没变），不落在 argv 长什么样上。

对应 issue：

* **#899**（A 绑定次序 + C ``_minimal_roots`` 提前删 carve-out）：调用方声明
  ``writable=[W]``、``readonly=[..., P]`` 且 ``P`` 严格位于 ``W`` 内部 —— "在可写根里
  挖一个只读洞"。绑定 Project 的 run 里 ``W`` 又必然住在只读的 worktree 之下，这是常态。
* **#872**：整棵 ``/tmp`` 与整棵用户缓存曾被挂成可写，于是一条只声明了自己临时目录的
  沙箱命令能改写**别的 run** 的账本，以及 ``/tmp/hf-jobs/<job>/record.json`` 里的
  ``exit_code`` —— 而 ``enforcement_record()`` 仍自报 ``write_boundary`` 已兑现。
* **#845**：path-based AF_UNIX ``connect(2)`` 既不归 Landlock 的 FS 规则族管、也不归
  network namespace 管。docker.sock 可达 ≈ root 等价逃逸。

**判据不只落在"拒了没有"上**，还落在"账面说的和现场一致"上：
:meth:`WriteLayers.grants_write` 是对外公开的判定（#899-D 请求的那个），它对每条路径
的回答必须与真跑出来的结果**逐条相同**。这就是 #903 里那份"节点自带一份绑定次序镜像"
不必再建的理由 —— 判定和发射读同一份数据。

变异判据（每条都手动验过会转红）：

* 把 ``_bwrap_argv`` / ``seatbelt_profile`` 的发射次序改回 broad → readonly → priority
  → ``test_a_readonly_hole_inside_a_writable_root_is_honoured`` 红。
* 把 ``write_layers`` 的 scratch 改回 ``[gettempdir(), "/tmp", cache_root()]``
  → ``test_a_shared_tmp_is_not_part_of_the_write_face`` 与
  ``test_another_apps_cache_is_not_writable`` 红。
* 把 ``core.sandbox._minimal_roots`` 的 ``carve_outs`` 参数忽略掉
  → ``test_minimal_roots_keeps_a_hole_that_another_mode_separates`` 红。
* 去掉 darwin 的 ``(deny network*)`` → ``test_a_host_unix_socket_is_out_of_reach`` 红。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from core import isolation, sandbox
from core.isolation import Invariant, select_backend
from core.isolation._native import cache_root, cache_scratch, write_layers
from shared.lib.cancellable_subprocess import spawn_and_wait

NATIVE = "darwin" if sys.platform == "darwin" else "linux"


class _State:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kill_event = None

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


@pytest.fixture(autouse=True)
def _native(monkeypatch):
    monkeypatch.setenv(isolation.EXECUTOR_ENV, NATIVE)
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


def _backend_ready() -> bool:
    try:
        backend = select_backend(NATIVE)
    except isolation.IsolationContractError:
        return False
    return Invariant.WRITE_BOUNDARY in backend.capabilities()


live = pytest.mark.skipif(
    not _backend_ready(),
    reason=f"native backend {NATIVE} cannot enforce the write boundary on this host",
)


# ── 真起进程的那几条 ──────────────────────────────────────────────────────────


@pytest.fixture()
def nest(tmp_path: Path) -> dict[str, Path]:
    """#899 的复现布局：可写根住在只读 worktree 里，可写根**内部**再挖一个只读洞。

        <wt>                       ro   ← 整个 worktree
        └── experiment/runtime     rw   ← 节点自己的根（住在只读根里 = priority）
            └── deps               ro   ← 声明的只读洞
    """
    worktree = tmp_path / "wt"
    runtime = worktree / "experiment" / "runtime"
    deps = runtime / "deps"
    deps.mkdir(parents=True)
    (deps / "lib.txt").write_text("ORIGINAL", encoding="utf-8")
    (worktree / "outside.txt").write_text("ORIGINAL", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    return {"worktree": worktree, "runtime": runtime, "deps": deps}


def _run(cmd: str, nest: dict[str, Path], *, extra_readonly=(), timeout: float = 60):
    state = _State()
    result = asyncio.run(spawn_and_wait(
        cmd, state=state, timeout=timeout, shell=True, cwd=str(nest["runtime"]),
        writable_roots=[nest["runtime"]],
        readonly_roots=[nest["worktree"], nest["deps"], *extra_readonly],
    ))
    return state, result


def _layers(nest: dict[str, Path]):
    return write_layers(
        [nest["runtime"]], [nest["worktree"], nest["deps"]],
        scratch_dir=Path(tempfile.gettempdir()),
    )


@live
def test_a_readonly_hole_inside_a_writable_root_is_honoured(nest):
    """#899 的正题：声明为只读的 ``deps`` 交给后端之后，真的写不进去。

    此前 ``W`` 的可写绑定排在 ``P`` 的只读绑定**之后**（bwrap 后挂的赢 / seatbelt 后写
    的赢），于是把 ``P`` 又盖成了可写 —— 实测 ``rc=0``、宿主文件 ORIGINAL → TAMPERED。
    """
    target = nest["deps"] / "lib.txt"
    _state, (status, _rc, _out, _err) = _run(f"echo TAMPERED > '{target}'", nest)
    assert status == "done"
    assert target.read_text(encoding="utf-8").strip() == "ORIGINAL", (
        "声明为只读的 dependency_root 被写穿了 —— 绑定/规则的发射次序没有让"
        "「最具体的声明」赢（#899-A），或者这条 readonly 根在 core.sandbox 里"
        "就已经被 _minimal_roots 丢掉了（#899-C）"
    )


@live
def test_an_existing_file_outside_the_hole_stays_writable(nest):
    """收窄不能收过头：洞**外面**已经存在的文件必须照样写得进去。

    没有这一条，上一条测试可以靠"把一切都拒掉"作弊通过。

    注意这里写的是**已存在**的文件。"在可写根里新建"是另一回事 —— 见下一条。
    """
    target = nest["runtime"] / "existing.txt"
    target.write_text("ORIGINAL", encoding="utf-8")
    _state, (status, _rc, _out, _err) = _run(f"echo WROTE > '{target}'", nest)
    assert status == "done"
    assert target.read_text(encoding="utf-8").strip() == "WROTE"


@live
def test_the_price_of_the_hole_is_on_the_record(nest):
    """挖了洞就不能在根里**新建** —— 这是纯放行清单的真实代价，必须记在账上。

    Landlock 没有 deny 原语，表达洞的唯一办法是只放行"到洞这条链上的兄弟条目"，
    于是链上的目录自己进不了清单。后果：`runtime/` 里原有的文件照样能写，
    但**新建**一个会被拒。

    为什么不反过来（放行整个根、把洞报成没兑现）：那等于让声明为只读的数据可被
    改写，削弱的正是这套机制要建的写边界。**宁可少一项能力，不可少一道墙。**

    这一条钉的是"代价被说出来了"—— 不记账的话，节点只会看到一个没有理由的
    FileNotFoundError，而那正是 2026-09-15 在 CI 上花了几轮才认出来的形状。
    """
    layers = _layers(nest)
    chains = layers.chain_dirs()
    assert nest["runtime"] in chains, (
        f"挖了洞的可写根没被登记成「不能直接新建」：{chains}")

    if NATIVE != "linux":
        pytest.skip("这条代价属于纯放行清单后端（Landlock）；seatbelt 有 deny 原语")

    _state, (status, _rc, _out, _err) = _run(
        f"echo NEW > '{nest['runtime'] / 'brand_new.txt'}'", nest)
    assert status == "done"
    assert not (nest["runtime"] / "brand_new.txt").exists(), (
        "在挖了洞的可写根里新建成功了 —— 那说明洞其实没守住")
    # 记账那一半：这条代价必须是**可判定的**，不是只能靠读注释知道。
    # `chain_dirs` 上面已经断言过了；这里再钉一次它说的和现场一致：
    # 登记为「不能新建」的根，新建确实失败；没登记的根不受影响。
    assert nest["worktree"] not in chains, (
        "只读根被误登记成「挖了洞的可写根」—— 那会把一条不存在的代价报给节点")


@live
def test_the_public_predicate_agrees_with_the_backend(nest):
    """``grants_write`` 与真跑出来的结果逐条相同 —— #899-D 要的那个公开判定。

    这条测试的意义是把**判定**和**发射**钉在一起：哪天有人改了绑定次序却忘了改判定
    （或反过来），这里立刻红。节点因此不必自己镜像一份后端行为去猜（#903）。
    """
    layers = _layers(nest)
    # 用**已存在**的文件比对：新建是另一回事（见
    # test_the_price_of_the_hole_is_on_the_record），把两件事混在一条里，
    # 红的时候分不清是判定错了还是后端表达不了。
    cases = [
        (nest["runtime"] / "ok.txt", True),
        (nest["deps"] / "lib.txt", False),
        (nest["worktree"] / "outside.txt", False),
    ]
    for target, predicted in cases:
        assert layers.grants_write(target) is predicted, f"判定给错了：{target}"
    for target, predicted in cases:
        target.write_text("ORIGINAL", encoding="utf-8")
        _state, (status, _rc, _out, _err) = _run(f"echo TAMPERED > '{target}'", nest)
        assert status == "done"
        observed = target.read_text(encoding="utf-8").strip() == "TAMPERED"
        assert observed is predicted, (
            f"{target}: 判定说 {'可写' if predicted else '只读'}，现场是 "
            f"{'可写' if observed else '只读'} —— 账面与现实脱节"
        )


@live
def test_a_shared_tmp_is_not_part_of_the_write_face(nest):
    """#872：别的 run 住在共享 tmp 下的账本，不该被一条沙箱命令改写。

    这正是实测里 ``/tmp/hf-jobs/<job>/record.json`` 的 ``exit_code`` 被从 138（被资源
    守卫杀死）改成 0 的那条路 —— A 类墙的执法结果被 B 类墙本该保护的账本抹掉。
    """
    victim_dir = Path(tempfile.mkdtemp(prefix="hf-victim-"))
    try:
        victim = victim_dir / "record.json"
        victim.write_text('{"exit_code": 138, "status": "killed"}', encoding="utf-8")
        _state, (status, _rc, _out, _err) = _run(
            f"""echo '{{"exit_code": 0, "status": "exited"}}' > '{victim}'""", nest)
        assert status == "done"
        assert "138" in victim.read_text(encoding="utf-8"), (
            "共享 tmp 仍在可写面里 —— 一条只声明了自己根的命令改写了别的 run 的事实账"
        )
    finally:
        shutil.rmtree(victim_dir, ignore_errors=True)


@live
def test_the_command_still_has_a_tmp_of_its_own(nest):
    """收窄共享 tmp 的代价必须由**私有** tmp 补上：``$TMPDIR`` 得真能写。

    不给的话命令没地方放临时文件，"cache/tmp unwritable" 会变成噪音 —— 而噪音会训练
    模型忽略真正的拒写。
    """
    _state, (status, rc, out, err) = _run(
        'printf x > "$TMPDIR/probe" && cat "$TMPDIR/probe"', nest)
    assert status == "done", err[:400]
    assert rc == 0, err[:400]
    assert out.strip() == b"x"


@live
def test_another_apps_cache_is_not_writable(nest):
    """#872 的另一半：整棵用户缓存曾是可写的 —— 别人的缓存不该在我们的可写面里。

    我们自己的那块（``XDG_CACHE_HOME`` 指过去的子目录）仍要可写，否则 uv/pip 会反复
    报 cache unwritable。两条一起验，避免"把一切都拒掉"式的假通过。
    """
    root = cache_root()
    neighbour = root / "some-other-app"
    neighbour.mkdir(parents=True, exist_ok=True)
    victim = neighbour / "hf-carveout-probe.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    try:
        _state, (status, _rc, _out, _err) = _run(f"echo TAMPERED > '{victim}'", nest)
        assert status == "done"
        assert victim.read_text(encoding="utf-8").strip() == "ORIGINAL", (
            "整棵用户缓存仍在可写面里（#872）"
        )
        _state, (status, rc, _out, err) = _run(
            'printf x > "$XDG_CACHE_HOME/probe" && rm "$XDG_CACHE_HOME/probe"', nest)
        assert status == "done" and rc == 0, (
            f"harness 自己的缓存子目录写不进去 —— 收窄收过头了：{err[:300]}")
    finally:
        victim.unlink(missing_ok=True)


@live
def test_a_host_unix_socket_is_out_of_reach(nest):
    """#845：宿主上 path-based 的 AF_UNIX socket，墙内不该连得上。

    docker.sock / systemd 私有 socket / 本地 MCP server 都是这个形状；docker.sock 可达
    ≈ root 等价逃逸。后端记账里的 ``unix_socket_reachable`` 说自己守到了（``False``），
    这条就必须真的连不上；说守不到（``True`` / ``"partial"``）时**不**在这里断言 ——
    那是如实记账，不是缺陷（[[feedback_absent_check_looks_like_passed_check]]）。
    """
    backend = select_backend(NATIVE)
    reachable = getattr(backend, "unix_socket_reachable", True)
    sock_dir = Path(tempfile.mkdtemp(prefix="hf-sock-"))
    sock_path = sock_dir / "control.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    stop = threading.Event()

    def _accept() -> None:
        server.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            conn.close()

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    probe = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(3)\n"
        "try:\n"
        f"    s.connect({str(sock_path)!r})\n"
        "except OSError:\n"
        "    sys.exit(0)\n"
        "sys.exit(91)\n"
    )
    try:
        script = nest["runtime"] / "probe.py"
        script.write_text(probe, encoding="utf-8")
        _state, (status, rc, _out, err) = _run(f"python3 '{script}'", nest)
        assert status == "done", err[:400]
        if reachable is False:
            assert rc == 0, (
                "后端记账说宿主 unix socket 够不到，实测却连上了 —— 记账在说谎（#845）"
            )
        else:
            assert rc in (0, 91), err[:400]
    finally:
        stop.set()
        thread.join(timeout=2)
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


@live
def test_the_record_says_whether_unix_sockets_are_reachable(nest):
    """记账里必须**有这个词**。没有它，缺口在结构上就报告不出来（#845 的请求）。"""
    backend = select_backend(NATIVE)
    record = isolation.enforcement_record(backend).as_event()
    assert "unix_socket_reachable" in record
    assert record["unix_socket_reachable"] in (True, False, "partial")


# ── 纯函数：跨平台都能跑 ──────────────────────────────────────────────────────


def test_minimal_roots_keeps_a_hole_that_another_mode_separates(tmp_path: Path):
    """#899-C：``A ⊃ C ⊃ B`` 且 C 是另一种模式时，B 不是冗余，是一处 carve-out。

    实测症状：``_minimal_roots(readonly)`` 把 ``deps`` 整条丢掉，于是即便绑定次序修好
    了，``submit_job(local)`` 这条路上后端也永远收不到那条只读根。
    """
    outer = tmp_path / "wt"
    middle = outer / "experiment" / "runtime"
    inner = middle / "deps"
    inner.mkdir(parents=True)
    kept = sandbox._minimal_roots([outer, inner], carve_outs=[middle])
    assert inner in kept, "被一条可写根隔开的只读根被当成冗余删掉了"
    collapsed = sandbox._minimal_roots([outer, inner], carve_outs=[])
    assert inner not in collapsed, "没有隔开时仍应折叠 —— 否则这条测试没有判别力"


def test_the_frozen_manifest_stops_calling_a_hole_rw(tmp_path: Path):
    """冻结的 attempt manifest 此前对 carve-out **保持沉默**，``_effective_mode`` 随后
    把那条路径报成 ``"rw"``。任何人都不该拿那份 manifest 当"这条边界不存在"的旁证。"""
    outer = tmp_path / "wt"
    middle = outer / "experiment" / "runtime"
    inner = middle / "deps"
    inner.mkdir(parents=True)

    class _S:
        project_worktree = str(outer)
        root = str(middle)
        run_id = "r1"

    manifest = sandbox._local_manifest(_S(), [middle], [inner])
    roots = [(Path(path), mode) for path, mode in manifest.mounts]
    assert sandbox._effective_mode(roots, inner / "lib.txt") == "ro"
    assert sandbox._effective_mode(roots, middle / "ok.txt") == "rw"


def test_allow_list_carves_the_hole_out_by_complement(tmp_path: Path):
    """纯放行清单（Landlock）没有 deny 原语，只能靠补集覆盖表达洞。"""
    root = tmp_path / "runtime"
    hole = root / "deps"
    hole.mkdir(parents=True)
    (root / "keep").mkdir()
    (root / "also").mkdir()
    layers = write_layers([root], [hole], scratch_dir=None)
    allowed, honored, why = layers.allow_list()
    assert honored and not why
    assert hole not in allowed
    assert root not in allowed, "放行了整个根 = 洞根本没挖"
    assert root / "keep" in allowed and root / "also" in allowed


def test_allow_list_is_unchanged_when_there_is_no_hole(tmp_path: Path):
    """没有洞时补集不该生效 —— 否则每条命令都要付枚举的代价，还带上快照语义。"""
    root = tmp_path / "runtime"
    (root / "keep").mkdir(parents=True)
    layers = write_layers([root], [tmp_path], scratch_dir=None)
    allowed, honored, _why = layers.allow_list()
    assert honored
    assert allowed == layers.writable


def test_allow_list_reports_the_gap_instead_of_pretending(tmp_path: Path):
    """宽目录超出规则预算时**不**悄悄放行整个根还说守住了 —— 如实报告没兑现。

    这就是 #900 里那个"要你判断的代价"的落点：代价不消失，但它变成一条读得到的账。
    """
    root = tmp_path / "runtime"
    hole = root / "deps"
    hole.mkdir(parents=True)
    for i in range(40):
        (root / f"d{i:03d}").mkdir()
    layers = write_layers([root], [hole], scratch_dir=None)
    allowed, honored, why = layers.allow_list(budget=5)
    assert honored is False
    assert "allow-rules" in why
    assert allowed == layers.writable, "没兑现时应放行整个根（可用性优先），但要说出来"


def test_the_write_face_never_contains_the_shared_tmp(tmp_path: Path):
    """结构判据：共享 tmp / 整棵用户缓存永远不进可写面（#872 的根因）。"""
    root = tmp_path / "runtime"
    root.mkdir()
    scratch = Path(tempfile.mkdtemp(prefix="hf-scratch-probe-"))
    try:
        layers = write_layers([root], [tmp_path], scratch_dir=scratch)
        shared = {Path(tempfile.gettempdir()).resolve(), Path("/tmp")}
        for path in layers.writable:
            assert path not in shared, f"共享 tmp 又回到可写面了：{path}"
            assert path != cache_root(), f"整棵用户缓存又回到可写面了：{path}"
        # canonical() 跟符号链接走：macOS 上 /var → /private/var，比对前先解一次。
        assert scratch.resolve() in layers.writable
        cache = cache_scratch()
        if cache is not None:
            assert cache in layers.writable, "harness 自己那块缓存该留着"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_grants_write_is_a_total_answer(tmp_path: Path):
    """判定对**任意**路径都给得出答案 —— 这是它能替掉节点自带镜像的前提（#903）。"""
    root = tmp_path / "runtime"
    hole = root / "deps"
    hole.mkdir(parents=True)
    layers = write_layers([root], [tmp_path, hole], scratch_dir=None)
    assert layers.grants_write(root / "a.txt") is True
    assert layers.grants_write(hole / "a.txt") is False
    assert layers.grants_write(tmp_path / "a.txt") is False
    assert layers.grants_write("/etc/passwd") is False
