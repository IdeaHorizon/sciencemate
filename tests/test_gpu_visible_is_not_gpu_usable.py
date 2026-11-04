"""看得见几张卡，和能不能用上，是两个问题（#893）。

node20 真机 E2E：资源画像报 local `available=true` 且可见 4×A100，
而 `submit_job(gpus=1)` 在创建 job identity **之前**稳定拒绝。模型读到"有 4 张卡"
就一路规划到提交才撞墙 —— 而那时人已经批过一次不可逆的审批。

根子是一个 `available` 字段同时回答了两个问题。这组判据钉住三件事：

1. 两个问题各有各的字段，**不许合并**；
2. 查不出来（没有 nvidia-smi）必须是 `None`，不是 0 —— 查不出来不等于没有；
3. 拒绝理由必须带上**和可见性同一份事实**，而且这个答案在提交前就能拿到。
"""
from __future__ import annotations

import pytest

from core import sandbox
from core.sandbox import SandboxContractError, gpu_capability


def test_visible_and_schedulable_are_two_fields(tmp_path):
    cap = gpu_capability()
    # 合成一个 available 就只能二选一地撒谎，这里要求它们分开存在。
    assert hasattr(cap, "visible") and hasattr(cap, "schedulable")
    assert not hasattr(cap, "available"), (
        "又出现了一个 available —— 它会把「看得见」和「用得上」重新压成一个答案")
    assert isinstance(cap.schedulable, bool)
    assert cap.visible is None or isinstance(cap.visible, int)


def test_no_probe_means_unknown_not_zero(monkeypatch):
    """没有 nvidia-smi 是「查不出来」，不是「没有卡」。"""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    cap = gpu_capability()
    assert cap.visible is None, "查不出来被记成了 0 —— 那会让人以为机器上没有卡"
    assert "看不出来" in cap.sentence()


def _four_cards(monkeypatch, *, backend: str | None):
    """机器上看得见 4 张卡；后端能不能给 CUDA 开口由 ``backend`` 定。

    只替换这两个事实，不替换 ``subprocess.run``：gpu_capability 还要问隔离后端，全局
    换掉 subprocess.run 会让后端探针读到假结果并被缓存，污染同进程后面所有测试。
    """
    from core import gpu

    monkeypatch.setattr(gpu, "visible_indices", lambda: [0, 1, 2, 3])
    monkeypatch.setattr(sandbox, "_gpu_backend",
                        lambda: (backend, "" if backend else "测试：这个后端不给 CUDA 开口"))


def test_the_refusal_carries_the_same_facts_as_discovery(tmp_path, monkeypatch):
    """拒绝时说的，必须和资源画像看到的是同一份事实。"""
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    _four_cards(monkeypatch, backend=None)

    cap = gpu_capability()
    assert cap.visible == 4 and cap.schedulable is False

    with pytest.raises(SandboxContractError) as e:
        sandbox.prepare_launch(
            ["true"], cwd=tmp_path, writable_roots=[tmp_path], gpus=1)

    text = str(e.value)
    assert "4 张" in text, (
        f"拒绝理由没提它自己也看得见 4 张卡 —— 模型无从和资源画像对账：{text}")
    assert "提交之前" in text, (
        f"没说清这个答案在提交前就能确定，而这正是 #893 的代价：{text}")


def test_schedulable_is_not_a_grant(tmp_path, monkeypatch):
    """#893 阶段 1：卡可分配 ≠ 这个 run 可以用。没经人批准的作业照样拒，并指出怎么申请。"""
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(tmp_path / "jobs"))
    _four_cards(monkeypatch, backend="linux")

    cap = gpu_capability()
    assert cap.visible == 4 and cap.schedulable is True and cap.isolated is False
    assert "submit_job" in cap.reason, "可分配时要说怎么拿到：提交 GPU 作业时框架替你问人"
    with pytest.raises(SandboxContractError, match="gpu_runtime"):
        sandbox.prepare_launch(["true"], cwd=tmp_path, writable_roots=[tmp_path], gpus=1)


def test_asking_costs_nothing_irreversible(tmp_path, monkeypatch):
    """这个答案必须在**问人之前**就拿得到 —— #893 的代价是它被推到了审批之后。

    回答它只许读：数卡（nvidia-smi）、问后端（和开局说明同一份快照）、读锁目录。
    不分卡、不起作业、不写锁文件。
    """
    jobs = tmp_path / "jobs"
    monkeypatch.setenv("HARNESS_JOBS_ROOT", str(jobs))
    _four_cards(monkeypatch, backend="linux")
    from core import gpu

    def _forbidden(*_a, **_k):
        pytest.fail("回答「能不能用」时分了卡 / 起了作业")

    for name in ("acquire", "attach_job"):
        monkeypatch.setattr(gpu, name, _forbidden)
    gpu_capability()
    written = [p for p in jobs.rglob("*") if p.is_file() and p.name != ".mutex"]
    assert written == [], f"回答这个问题时写了东西：{written}"


def test_zero_gpus_still_goes_through(tmp_path):
    """收窄不能收过头：不要 GPU 的命令不受这条闸影响。"""
    launch = sandbox.prepare_launch(
        ["true"], cwd=tmp_path, writable_roots=[tmp_path], gpus=0)
    assert launch is not None
