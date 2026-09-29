"""组织登记的机器：组织服务器探、写进登记表；agent 读登记表，不自己 ssh。

`RFC_ORGANISATION_PAGE_20260923` §4.4（D 批）。判据：
- 登记表里不许有密码 / 私钥（agent 读得到这份文件）；
- 引用登记机器的授权，现状照登记表说、带「探于」，一条命令都不跑；
- 按人写的授权对得上**平台说的那个人**（`HARNESS_FRAMEWORK_USER_ID` / identity.json），
  而不是这台机器 git 配置里的名字。
"""
from __future__ import annotations

import pytest

from core import capabilities, machines

GPU_BOX = {
    "id": "m_a1b2c3d4", "name": "gpu-01", "kind": "gpu_node",
    "host": "10.0.0.5", "port": 22, "username": "lab",
    "facts": {"gpus": [{"name": "NVIDIA A100", "memory_total": "80 GB", "utilization": "0%"},
                       {"name": "NVIDIA A100", "memory_total": "80 GB", "utilization": "35%"}],
              "os": "Linux 6.8"},
    "status": "online", "probed_at": "2026-09-24T10:00:00+00:00",
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    capabilities.forget_the_last_look()
    monkeypatch.delenv("HARNESS_GRANTS_FILE", raising=False)
    yield
    capabilities.forget_the_last_look()


def _grant(mapping: dict) -> None:
    capabilities.write_grants_file(mapping)


def test_the_register_refuses_secrets():
    with pytest.raises(ValueError, match="密码或私钥"):
        machines.write_machines([{**GPU_BOX, "password": "hunter2"}])
    assert machines.read_machines() == ([], None)


def test_the_register_refuses_a_kind_nobody_probed():
    with pytest.raises(ValueError, match="类型"):
        machines.write_machines([{**GPU_BOX, "kind": "supercomputer"}])


def test_a_granted_machine_is_described_from_the_register_without_running_anything(monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "u-liming")
    machines.write_machines([GPU_BOX])
    _grant({"u-liming": [{"kind": "gpu_node", "machine": GPU_BOX["id"], "devices": "0"}]})

    def ran(*_a, **_k):
        raise AssertionError("worker 进不去组织的机器 —— 不该在这里 ssh / 跑命令")

    monkeypatch.setattr(capabilities, "_run", ran)
    caps, err = capabilities.snapshot(force=True)

    assert err is None
    [cap] = caps
    assert cap.status == "verified"
    assert "gpu-01（lab@10.0.0.5）" in cap.detail
    assert "2 张卡（2×NVIDIA A100 80 GB），1 张在忙" in cap.detail
    assert "授权卡位：0" in cap.detail and "探于 2026-09-24 10:00 UTC" in cap.detail
    assert capabilities.allows_local_deployment(caps), "一台登记的卡机该算「能本地部署」"
    section = "\n".join(capabilities.render_compute_section())
    assert "gpu-01" in section and GPU_BOX["id"] not in section, "给 agent 看的是名字，不是 id"


def test_an_unreachable_machine_says_when_it_was_last_seen(monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "u-liming")
    machines.write_machines([{**GPU_BOX, "status": "unreachable", "problem": "连不上 10.0.0.5:22"}])
    _grant({"default": [{"kind": "gpu_node", "machine": GPU_BOX["id"]}]})

    [cap], _ = capabilities.snapshot(force=True)

    assert cap.status == "probe_failed"
    assert "上次探不到（连不上 10.0.0.5:22）" in cap.detail


def test_a_removed_machine_is_said_out_loud(monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "u-liming")
    _grant({"default": [{"kind": "gpu_node", "machine": "m_gone000"}]})

    [cap], _ = capabilities.snapshot(force=True)

    assert cap.status == "probe_failed" and "登记里没有这台机器" in cap.detail


def test_grants_follow_the_person_the_platform_names(monkeypatch):
    """按人写的那一节，对的是平台说的那个人。"""
    machines.write_machines([GPU_BOX])
    _grant({"u-liming": [{"kind": "gpu_node", "machine": GPU_BOX["id"]}],
            "u-zhang": [{"kind": "gpu_node", "machine": GPU_BOX["id"], "devices": "1"}]})

    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "u-liming")
    mine, _ = capabilities.snapshot(force=True)
    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "u-someone-else")
    theirs, _ = capabilities.snapshot(force=True)

    assert len(mine) == 1 and "授权卡位" not in mine[0].detail
    assert theirs == []


def test_writing_the_register_is_seen_at_once(monkeypatch):
    """登记表一改，下一次注入就该看到 —— 不等 60 秒的缓存过期。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "u-liming")
    machines.write_machines([GPU_BOX])
    _grant({"default": [{"kind": "gpu_node", "machine": GPU_BOX["id"]}]})
    before, _ = capabilities.snapshot()
    machines.write_machines([{**GPU_BOX, "status": "unreachable", "problem": "关机了"}])

    after, _ = capabilities.snapshot()

    assert before[0].status == "verified" and after[0].status == "probe_failed"
