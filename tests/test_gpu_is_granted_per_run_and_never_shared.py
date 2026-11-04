"""本机 GPU 按 run 批、按卡记账、run 之间不撞（#893 阶段 1 / #1174，wangd 10-21 裁决）。

#893 的原始病症：资源画像说「看得见 4×A100」，提交 GPU 作业却在人批准**之后**才被拒 ——
那时人的批准已经花掉了。阶段 0 把拒绝挪到了批准之前；阶段 1 让它真能用：

1. 人批了（``submit_job(gpus=N)`` 时框架替模型弹的那张卡），这个 run 拿到 N 张卡的锁，别的 run 分不到；
2. 它的命令只看得见那几张卡（``CUDA_VISIBLE_DEVICES`` + ``CUDA_DEVICE_ORDER=PCI_BUS_ID``），
   没批的 run 一张都看不见；
3. Linux 的墙为批到 GPU 的命令放开 ``/proc`` 的写（lujy 在 #1174 实证：CUDA 驱动给线程写
   ``/proc/self/task/<tid>/comm``，被 Landlock 拒就 ``cuInit=304``），没批的照旧拒；
4. run 结束放卡；占卡的进程没了，锁被下一个来分卡的人回收；后台作业还在跑，卡继续算它的。

不是隔离：``isolated`` 恒为 False，同机不经本平台的程序仍看得见这些卡。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from core import capability_grants as cg
from core import gpu, sandbox
from core.bootstrap import bootstrap
from core.state import State


@pytest.fixture(autouse=True)
def _jobs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(gpu, "visible_indices", lambda: [0, 1, 2, 3])
    yield


def _state() -> State:
    bootstrap()
    return State.new(node_type="experiment", base_dir=Path(tempfile.mkdtemp()), project_id=None)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ── 1. 账本：独占、全有或全无、回收 ─────────────────────────────────────────


def test_two_runs_never_get_the_same_card() -> None:
    a = gpu.acquire("run-a", 2)
    b = gpu.acquire("run-b", 2)
    assert a is not None and b is not None
    assert set(a.indices).isdisjoint(b.indices)
    assert gpu.acquire("run-c", 1) is None, "4 张卡都分出去了，第三个 run 还分到了"


def test_asking_for_more_than_is_free_gets_nothing() -> None:
    assert gpu.acquire("run-a", 3) is not None
    assert gpu.acquire("run-b", 2) is None
    assert gpu.free_indices() == [3], "要 2 张只剩 1 张时，那 1 张不能被半拿走"


def test_release_gives_the_cards_back() -> None:
    lease = gpu.acquire("run-a", 4)
    assert lease is not None and gpu.free_indices() == []
    assert gpu.release_run(lease) == [0, 1, 2, 3]
    assert gpu.free_indices() == [0, 1, 2, 3]


def test_a_dead_holder_is_reclaimed_by_the_next_caller() -> None:
    """worker 崩了没走到 finalize_run：锁文件还在，但占卡的进程没了 —— 下一个人顺手收回。"""
    locks = gpu._locks_dir()
    (locks / "0.json").write_text(json.dumps({
        "index": 0, "run_id": "crashed",
        "holders": [{"role": "run", "pid": _dead_pid(), "birth": None, "run_id": "crashed"}],
    }), encoding="utf-8")
    assert 0 in gpu.free_indices()
    lease = gpu.acquire("run-b", 4)
    assert lease is not None and 0 in lease.indices


def test_a_running_job_keeps_the_card_after_its_run_ends(tmp_path) -> None:
    """run 起了后台作业就结束了：卡继续算作业的，作业结束才空出来。"""
    from shared.lib import process_control

    lease = gpu.acquire("run-a", 1)
    assert lease is not None
    control = tmp_path / "job"
    control.mkdir()
    me = os.getpid()
    (control / "record.json").write_text(json.dumps({
        "status": "running", "pid": me, "birth_identity": process_control.birth_identity(me),
    }), encoding="utf-8")
    gpu.attach_job(lease, control)

    assert gpu.release_run(lease) == [], "作业还在跑，卡不能空出来"
    assert lease.indices[0] not in gpu.free_indices()

    (control / "record.json").write_text(json.dumps({"status": "exited"}), encoding="utf-8")
    assert lease.indices[0] in gpu.free_indices()


def test_hidden_means_no_device_on_every_platform() -> None:
    """空串在 Windows 上等于没设（11-04 9800x3d：没批的 run 照样 cuInit=0）；-1 两边都是「没有卡」。"""
    assert gpu.HIDDEN["CUDA_VISIBLE_DEVICES"] == "-1"


def test_the_lease_names_cards_in_nvidia_smi_order() -> None:
    lease = gpu.acquire("run-a", 2)
    assert lease is not None
    env = lease.env()
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID", (
        "CUDA 默认按「最快的在前」排，和 nvidia-smi 的编号对不上 —— 会用到别人那张卡")


# ── 2. 授权：人批了才分卡；拒绝、读不出来都不分 ──────────────────────────────


def test_an_approval_takes_cards_and_a_refusal_takes_none() -> None:
    state = _state()
    assert cg.grant_gpu_from_answer(state, 1, "拒绝") is False
    assert cg.gpu_lease(state) is None
    assert cg.grant_gpu_from_answer(state, 2, "允许", reason="训练") is True
    lease = cg.gpu_lease(state)
    assert lease is not None and len(lease.indices) == 2
    assert cg.grant_gpu_from_answer(state, 1, "允许") is True
    assert cg.gpu_lease(state) is lease, "已经够用的 run 再批一次不该多占卡"
    assert cg.release_gpu(state) == list(lease.indices)
    assert cg.gpu_lease(state) is None


def test_approved_but_all_cards_taken_is_said_not_hidden() -> None:
    assert gpu.acquire("someone-else", 4) is not None
    state = _state()
    assert cg.grant_gpu_from_answer(state, 1, "允许") is True
    grant = cg.gpu_grant(state)
    assert grant is not None and grant.busy and grant.lease is None


def test_an_unwritable_ledger_is_an_answer_not_a_crash(monkeypatch) -> None:
    """11-04 node20：/tmp/hf-jobs 是另一个用户建的，建锁目录 PermissionError。能不能分卡
    要照实回答，批了也要照实记下为什么没分到 —— 不能把开局说明和授权结算一起炸掉。"""
    def _denied():
        raise PermissionError(13, "Permission denied", "/tmp/hf-jobs/gpu-locks")

    monkeypatch.setattr(sandbox, "_gpu_backend", lambda: ("linux", ""))
    monkeypatch.setattr(gpu, "_locks_dir", _denied)
    cap = sandbox.gpu_capability()
    assert cap.schedulable is False and "账本" in cap.reason
    state = _state()
    assert cg.grant_gpu_from_answer(state, 1, "允许") is True
    grant = cg.gpu_grant(state)
    assert grant.lease is None and "Permission denied" in grant.error and not grant.busy


def test_the_default_jobs_root_is_per_user(monkeypatch) -> None:
    """多用户 Linux 上 /tmp 共用：谁先建了 hf-jobs，别人就建不了自己的作业目录与 GPU 账本。"""
    monkeypatch.delenv("HARNESS_JOBS_ROOT", raising=False)
    root = sandbox._jobs_root()
    if hasattr(os, "getuid"):
        assert root.name == f"hf-jobs-{os.getuid()}"


def test_the_card_names_the_capability_the_resume_path_settles() -> None:
    state = _state()
    pause = cg.request_gpu_grant(state, 2, "微调 ResNet")
    meta = pause["pause_event"]["metadata"]
    assert pause["status"] == "pause"
    assert meta["type"] == cg.GRANT_PAUSE_TYPE and meta["capability"] == cg.GPU_CAPABILITY
    assert meta["gpus"] == 2
    assert "不是隔离" in pause["pause_event"]["context"]


def test_the_resume_path_dispatches_gpu_answers_to_the_gpu_grant() -> None:
    import inspect

    from core import pause_driver

    source = inspect.getsource(pause_driver.drive_pause_chain)
    assert "GPU_CAPABILITY" in source and "grant_gpu_from_answer" in source, (
        "GPU 授权卡的答复没有结算分支：人点了「允许」，卡一张也分不到")


def test_finalize_run_gives_the_cards_back() -> None:
    import inspect

    from core import executor

    source = inspect.getsource(executor.finalize_run)
    assert "release_gpu(state)" in source


# ── 3. 框架替模型问：批了放行、没批出卡、开不了口报事实 ────────────────────────


def test_require_asks_once_and_then_lets_it_through(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_gpu_backend", lambda: ("linux", ""))
    state = _state()
    asked = cg.require(state, cg.GPU_CAPABILITY, 1, reason="x")
    assert asked is not None and asked["status"] == "pause"
    assert asked["pause_event"]["metadata"]["capability"] == cg.GPU_CAPABILITY
    assert cg.grant_gpu_from_answer(state, 1, "允许") is True
    assert cg.require(state, cg.GPU_CAPABILITY, 1) is None
    assert cg.require(state, cg.GPU_CAPABILITY, 2) is not None, "批了 1 张不等于批了 2 张"


def test_require_does_not_raise_a_card_that_cannot_be_honoured(monkeypatch) -> None:
    """§3.3：后端开不了这个口 → 带事实的 error，没有卡片（#893 的原始病症是批了才说不行）。"""
    monkeypatch.setattr(sandbox, "_gpu_backend", lambda: (None, "测试：没有后端"))
    result = cg.require(_state(), cg.GPU_CAPABILITY, 1)
    assert result["status"] == "error" and result["error_code"] == "capability_not_enforceable"


def test_the_model_is_not_handed_a_gpu_tool() -> None:
    """RFC_CAPABILITY_GRANTS_LAYERING §6：要不要 GPU 从作业声明机械可得，框架替它问。"""
    import shared.tools.builtin  # noqa: F401  （注册副作用）
    from core.tool_registry import get_tool

    assert get_tool("request_gpu_access") is None


def test_an_unknown_capability_never_reaches_a_wall() -> None:
    from core.isolation import CommandSpec, IsolationContractError

    with pytest.raises(IsolationContractError, match="unknown capabilities"):
        CommandSpec(argv=("true",), cwd="/", writable_roots=(Path("/tmp"),),
                    capabilities=frozenset({"gpu"}))


# ── 4. 咽喉：命令看得见的卡 = 本 run 批到的卡 ─────────────────────────────────


class _ThroatState:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.kill_event = None

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append({"event": event_type, **payload})


def _backend_ready() -> bool:
    from core import isolation

    try:
        isolation.select_backend()
    except Exception:
        return False
    return True


_PRINT_ENV = ("import os; print(repr(os.environ.get('CUDA_VISIBLE_DEVICES')), "
              "os.environ.get('CUDA_DEVICE_ORDER'))")


def _run_in_wall(state, tmp_path: Path, *argv: str) -> tuple[str, int | None, str, str]:
    from shared.lib.cancellable_subprocess import spawn_and_wait

    status, rc, out, err = asyncio.run(spawn_and_wait(
        *argv, state=state, timeout=60, cwd=str(tmp_path), writable_roots=[tmp_path]))
    return status, rc, out.decode(errors="replace"), err.decode(errors="replace")


@pytest.mark.skipif(not _backend_ready(), reason="这台机器没有可用的原生隔离后端")
def test_without_a_grant_the_command_sees_no_card(tmp_path) -> None:
    status, rc, out, err = _run_in_wall(_ThroatState(), tmp_path, sys.executable, "-c", _PRINT_ENV)
    assert (status, rc) == ("done", 0), err
    assert out.split()[0] == "'-1'", (
        f"没批的 run 拿到的不是 -1：{out}（空串在 Windows 上等于没设，照样看得见卡）")


@pytest.mark.skipif(not _backend_ready(), reason="这台机器没有可用的原生隔离后端")
def test_with_a_grant_the_command_sees_exactly_its_cards(tmp_path) -> None:
    state = _ThroatState()
    assert gpu.acquire("someone-else", 1) is not None           # 0 号被别人占着
    assert cg.grant_gpu_from_answer(state, 2, "允许") is True
    status, rc, out, err = _run_in_wall(state, tmp_path, sys.executable, "-c", _PRINT_ENV)
    assert (status, rc) == ("done", 0), err
    assert out.split() == ["'1,2'", "PCI_BUS_ID"], out
    cg.release_gpu(state)


def _linux_wall() -> bool:
    if not sys.platform.startswith("linux") or not _backend_ready():
        return False
    from core import isolation

    return isolation.select_backend().name == "linux"


_WRITE_COMM = ("import threading\n"
               "p = f'/proc/self/task/{threading.get_native_id()}/comm'\n"
               "try:\n"
               "    open(p, 'w').write('cuda-probe')\n"
               "    print('wrote')\n"
               "except OSError as e:\n"
               "    print('denied', e.errno)\n")


@pytest.mark.skipif(not _linux_wall(), reason="只在 Linux 原生后端上有 /proc 这堵墙")
def test_the_linux_wall_opens_proc_only_for_a_granted_run(tmp_path) -> None:
    """#1174 的机制本身：CUDA 驱动写线程名。没批的 run 写不了（cuInit=304 的来源），批了能写。"""
    status, rc, out, err = _run_in_wall(_ThroatState(), tmp_path, sys.executable, "-c", _WRITE_COMM)
    assert status == "done", err
    assert out.startswith("denied"), f"没批 GPU 的命令写得动 /proc：{out}"

    state = _ThroatState()
    assert cg.grant_gpu_from_answer(state, 1, "允许") is True
    status, rc, out, err = _run_in_wall(state, tmp_path, sys.executable, "-c", _WRITE_COMM)
    assert (status, rc) == ("done", 0), err
    assert out.strip() == "wrote", f"批了 GPU，CUDA 运行时要写的 /proc 还是写不了：{out}"
    cg.release_gpu(state)


def test_the_spec_carries_the_grant_to_every_backend() -> None:
    """wsl 目标把 CommandSpec 送进发行版：capabilities 也得跟着过去，否则那边的墙不开口。"""
    import inspect

    from core.isolation import _wsl_entry, wsl

    assert '"capabilities": sorted(spec.capabilities)' in inspect.getsource(wsl)
    assert 'capabilities=frozenset(spec.get("capabilities") or ())' in inspect.getsource(_wsl_entry)


# ── 5. 开局说明里那一行 ─────────────────────────────────────────────────────


def test_the_opening_facts_say_how_to_get_a_card() -> None:
    from core import execution_context_facts as ecf

    facts = ecf.gather(snapshot={"backend": "linux", "enforced": []}, platform="linux",
                       shell="/bin/bash", python="python3", os_text="Linux",
                       gpu="GPU：可见 4 张，受管入口可分配。")
    assert "GPU：可见 4 张" in ecf.render_section(facts)
    facts = ecf.gather(snapshot={"backend": "darwin", "enforced": []}, platform="darwin",
                       shell="/bin/bash", python="python3", os_text="macOS")
    assert "GPU" not in ecf.render_section(facts), "没卡的机器每个 run 都付一行 GPU 的字"
