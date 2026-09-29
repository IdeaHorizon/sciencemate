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


def test_the_refusal_carries_the_same_facts_as_discovery(tmp_path, monkeypatch):
    """拒绝时说的，必须和资源画像看到的是同一份事实。"""
    import shutil
    import subprocess

    class _Out:
        returncode = 0
        stdout = "0\n1\n2\n3\n"

    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out())

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


def test_asking_costs_nothing_irreversible(tmp_path, monkeypatch):
    """这个答案必须在**问人之前**就拿得到 —— #893 的代价是它被推到了审批之后。"""
    calls: list[str] = []
    import subprocess
    real = subprocess.run

    def _spy(argv, *a, **k):
        calls.append(argv[0] if isinstance(argv, (list, tuple)) else str(argv))
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", _spy)
    gpu_capability()

    assert all(c.endswith("nvidia-smi") or "nvidia-smi" in c for c in calls), (
        f"回答这个问题时跑了别的东西：{calls} —— 它必须是一次只读探测")


def test_zero_gpus_still_goes_through(tmp_path):
    """收窄不能收过头：不要 GPU 的命令不受这条闸影响。"""
    launch = sandbox.prepare_launch(
        ["true"], cwd=tmp_path, writable_roots=[tmp_path], gpus=0)
    assert launch is not None
