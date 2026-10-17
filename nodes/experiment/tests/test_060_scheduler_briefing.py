"""Startup guidance consumes the public submission projection, not client presence."""
from nodes.experiment.tools import resource_manager, run_bootstrap


def _facts(monkeypatch, *, local, remote):
    monkeypatch.setattr(resource_manager, "_local_resources", lambda: {
        "scheduler": "local", "available": local,
    })
    for scheduler in ("slurm", "pbs"):
        monkeypatch.setattr(resource_manager, "_detect_" + scheduler,
                            lambda s=scheduler: {"scheduler": s, "available": remote})
    monkeypatch.setattr(resource_manager, "_detect_k8s", lambda _n: {
        "scheduler": "kubernetes", "available": False,
    })
    monkeypatch.setattr(resource_manager, "_core_platform_capabilities", lambda: {})


def test_installed_slurm_is_not_presented_as_a_working_submission_path(monkeypatch):
    _facts(monkeypatch, local=True, remote=True)
    text = run_bootstrap._scheduler_facts(None)
    assert "客户端" in text and "slurm" in text
    assert "候选：local" in text
    assert "不等于" in text
    assert "受管作业由框架生成脚本并提交" not in text


def test_no_candidate_briefing_does_not_promise_local_fallback(monkeypatch):
    _facts(monkeypatch, local=False, remote=True)
    text = run_bootstrap._scheduler_facts(None)
    assert "没有可用的 submit_job 候选" in text
    assert "受管作业走 local" not in text


def test_local_briefing_keeps_authorization_and_resource_checks_separate(monkeypatch):
    _facts(monkeypatch, local=True, remote=False)
    text = run_bootstrap._scheduler_facts(None)
    assert "候选：local" in text
    assert "授权" in text and "资源" in text
