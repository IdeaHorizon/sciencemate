"""本地受管作业的能力记账必须来自隔离层，不能是硬编码常量（#920 回归）。

原实现在收据里写死 ``"enforcement": "docker_cgroup_and_pid1_supervisor"``。两个
问题：Docker 已在 PR C 删干净（`core/isolation` 下只剩原生后端），这句在说假话；
而且它是常量 —— 同一个字符串在守得住和守不住的宿主上一模一样。2026-09-07 实测
（issue #849）：UI 侧 worker 够不到 systemd 用户会话，`mem_cap`/`pids_cap` 双双
落不下去，收据却照旧宣称有 cgroup 监护。

节点规则：「对 enforcement、隔离、资源、只读边界和清理能力的声明，必须来自实际
生效配置、运行事实或代表性行为探测；无法兑现时记录 missing/unknown、影响和 owner」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nodes.experiment.tools import resource_manager as rm  # noqa: E402


def test_account_reports_what_the_isolation_layer_actually_enforces():
    acct = rm._local_enforcement_account()

    assert acct["enforcement"] == "native_managed_job"
    assert "docker" not in str(acct).lower(), "不得再声称 Docker/容器边界"
    # 两份清单都要在，且必须与隔离层同源 —— 节点不另答一遍。
    from core import isolation

    snap = isolation.enforcement_snapshot()
    assert acct["enforced_invariants"] == sorted(str(x) for x in snap["enforced"])
    assert acct["missing_for_unattended"] == sorted(
        str(x) for x in snap["missing_for_unattended"])
    assert acct["enforcement_backend"] == str(snap.get("backend") or "unknown")


def test_account_says_unknown_instead_of_guessing(monkeypatch):
    """答不上来时如实记 unknown 并说明原因 —— 不猜、不沉默。"""
    import core.isolation as iso

    def _boom():
        raise RuntimeError("no isolation backend here")

    monkeypatch.setattr(iso, "enforcement_snapshot", _boom)
    acct = rm._local_enforcement_account()

    assert acct["enforcement"] == "unknown"
    assert "no isolation backend here" in acct["enforcement_unknown_reason"]
    assert "enforced_invariants" not in acct, (
        "答不上来时不得给出一份看起来像真的空清单")


def test_the_account_is_not_a_constant(monkeypatch):
    """判别力：隔离层的回答变了，记账必须跟着变。

    这条是本文件的要害 —— 旧实现之所以有害，正是因为它无论宿主如何都返回同一个
    字符串。若有人把它改回常量，这里必须转红。
    """
    import core.isolation as iso

    monkeypatch.setattr(iso, "enforcement_snapshot", lambda: {
        "backend": "linux", "enforced": ["walltime"],
        "missing_for_unattended": ["mem_cap", "pids_cap"]})
    weak = rm._local_enforcement_account()

    monkeypatch.setattr(iso, "enforcement_snapshot", lambda: {
        "backend": "linux", "enforced": ["mem_cap", "pids_cap", "walltime"],
        "missing_for_unattended": []})
    strong = rm._local_enforcement_account()

    assert weak != strong
    assert weak["missing_for_unattended"] == ["mem_cap", "pids_cap"]
    assert strong["missing_for_unattended"] == []
