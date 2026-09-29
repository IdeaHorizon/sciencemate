"""pids 守法属于**提供它的那个后端**，不属于所有本地作业。

## 这条测试为什么存在

`scheduler=local` 的作业脚本里，原来无条件注入一段采样
`/sys/fs/cgroup/pids.events` 的 prelude —— 那段代码的 docstring 自己写着它是给
「detached **Docker** cgroup」用的，而 Docker 已在 PR C 删干净。

原生后端（macOS seatbelt、Linux Landlock+bwrap）下根本没有那个 cgroup 文件，
于是**每一个本地作业在跑 payload 之前就 `exit 127`**：

    HARNESS_SANDBOX_ERROR pids_events_unavailable phase=baseline

2026-09-06 真机实测：Mac 安装包上一个真课题走到 experiment 节点，三条执行通道
全废，9 条闭合条件 0 条兑现，一行科学计算都没跑成。

## 判据钉的是两个方向

只钉「macOS 上别注入」是不够的 —— 那样把 cgroup 后端上的证据一起丢了也没人
知道。所以两条对称的判据：**cgroup 后端上必须注入，非 cgroup 后端上必须不注入**，
而"是不是 cgroup 后端"由隔离层回答，不由这里判平台。
"""
from __future__ import annotations

import pytest

# 本目录 conftest 只把 repo 根进 sys.path —— 用正典导入，别依赖先跑的测试
# 恰好把 nodes/experiment 塞进 path（standalone 风格单跑本文件会 ModuleNotFound）。
from nodes.experiment.tools import resource_manager as rm


@pytest.fixture()
def a_backend_that(monkeypatch):
    """让隔离层报告一组指定的 enforced 能力。"""

    def _install(*enforced: str):
        import core.isolation as isolation

        monkeypatch.setattr(
            isolation, "enforcement_snapshot",
            lambda: {"backend": "fake", "enforced": list(enforced)},
        )

    return _install


def test_a_cgroup_backend_still_gets_its_evidence(a_backend_that) -> None:
    """cgroup 记 pids 的后端上，采样照旧注入 —— 否则被拒的 fork 会静默截断。"""
    a_backend_that("pids_cap", "mem_cap", "write_boundary")
    assert rm.the_backend_accounts_pids_in_a_cgroup() is True


def test_a_native_backend_is_not_asked_for_a_file_it_does_not_have(a_backend_that) -> None:
    """seatbelt / Landlock 这类后端不记 pids，就不该去采样一个不存在的文件。"""
    a_backend_that("write_boundary", "net_deny", "walltime", "group_kill")
    assert rm.the_backend_accounts_pids_in_a_cgroup() is False


def test_not_knowing_counts_as_not_having(monkeypatch) -> None:
    """问不出来时按"没有"算。

    不注入不会放宽任何上限（上限要么 cgroup 真守着、要么这个后端本来就不守，
    后者由 `missing_for_unattended` 如实报告）；而反过来注入，是让整台机器一个
    作业都跑不了。两种错的代价不对称。
    """
    import core.isolation as isolation

    def _boom():
        raise RuntimeError("隔离层这会儿答不上来")

    monkeypatch.setattr(isolation, "enforcement_snapshot", _boom)
    assert rm.the_backend_accounts_pids_in_a_cgroup() is False


def _local_script(monkeypatch, *enforced: str) -> str:
    import core.isolation as isolation

    monkeypatch.setattr(
        isolation, "enforcement_snapshot",
        lambda: {"backend": "fake", "enforced": list(enforced)},
    )
    return rm._script_for(
        scheduler="local",
        command="python3 run_scan.py",
        job_name="ising-scan",
        mpi_ranks=1,
        cpus_per_rank=2,
        gpus=0,
        memory_gb=2.0,
        walltime_minutes=10,
        queue=None,
        nodelist=None,
        image=None,
        workdir="/tmp/run",
    )


def test_the_script_samples_the_cgroup_only_where_there_is_one(monkeypatch) -> None:
    """作业脚本本身：cgroup 后端上出现那段采样，原生后端上不出现。

    判据落在**生成出来的脚本**上，不落在"那个函数返回什么" —— 后者可以答对而
    脚本照旧注入（两处各写一份条件就会这样）。
    """
    with_cgroup = _local_script(monkeypatch, "pids_cap")
    assert "pids.events" in with_cgroup
    assert "pids_events_unavailable" in with_cgroup

    native = _local_script(monkeypatch, "write_boundary", "net_deny")
    assert "pids.events" not in native, (
        "原生后端上仍在采样 cgroup —— 每个本地作业都会在 payload 之前 exit 127"
    )
    assert "pids_events_unavailable" not in native
    # 作业本身该有的东西一件不少
    assert "python3 run_scan.py" in native
    assert "cd /tmp/run" in native
