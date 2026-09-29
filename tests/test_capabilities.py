"""算力资源登记：文件只放钥匙，现状全靠探。

设计定稿（2026-07-30 与 wangd 讨论）：探得到的事实永远不写进文件（写进去就
腐坏）；grants.yaml 只放入口/政策/多用户授权；框架在消费时顺钥匙探测，同一份
snapshot 喂 system prompt 注入和 prereg freeze 门禁两处。
"""
from __future__ import annotations

import pytest

from core import capabilities as cap
from core.bootstrap import bootstrap

bootstrap()


@pytest.fixture()
def grants(tmp_path, monkeypatch):
    """写一份 grants.yaml 并清缓存。返回写文件的函数。"""
    p = tmp_path / "grants.yaml"
    monkeypatch.setenv("HARNESS_GRANTS_FILE", str(p))
    monkeypatch.setattr(cap, "_cache", None)

    def _write(text: str):
        p.write_text(text, encoding="utf-8")
        cap._cache = None
        return p
    return _write


# ── 钥匙层 ──────────────────────────────────────────────────────────────────

def test_no_grants_file_means_zero_blast_radius(grants):
    """没有 grants 文件 → 一切与本模块存在之前一致。"""
    caps, err = cap.snapshot(force=True)
    assert caps == [] and err is None
    assert cap.render_compute_section() == []
    assert cap.allows_local_deployment() is False


def test_default_and_user_sections_merge(grants, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "wangd")
    grants("""
default:
  - kind: shared_scratch
    probe_cmd: "echo 500G free"
wangd:
  - kind: local_gpu
    devices: "1,2"
someone_else:
  - kind: slurm_cluster
    endpoint: nope.example
""")
    caps, err = cap.snapshot(force=True)
    assert err is None
    kinds = {c.kind for c in caps}
    assert kinds == {"shared_scratch", "local_gpu"}, "default+本人生效，别人的不掺和"


def test_malformed_grants_is_loud_not_silent(grants):
    """解析失败必须吵（注入段一行 ⚠️），门禁按无授权保守处理。

    静默返空 = 用户授了权而平台装聋 —— 又是"在最要紧的用例上静默降级"。
    """
    grants("just a string, not a mapping")
    caps, err = cap.snapshot(force=True)
    assert caps == [] and err is not None
    lines = cap.render_compute_section()
    assert lines and "⚠️" in lines[0]
    assert cap.allows_local_deployment() is False


# ── 探针层 ──────────────────────────────────────────────────────────────────

def test_custom_probe_cmd_success_and_failure(grants):
    grants("""
default:
  - kind: shared_scratch
    probe_cmd: "echo 500G free"
  - kind: broken_thing
    probe_cmd: "false"
""")
    caps, _ = cap.snapshot(force=True)
    by = {c.kind: c for c in caps}
    assert by["shared_scratch"].status == "verified"
    assert "500G free" in by["shared_scratch"].detail
    assert by["broken_thing"].status == "probe_failed"


def test_probe_failure_keeps_the_grant_visible(grants):
    """探测失败 ≠ 授权消失：标"已授权、本次探测失败"照样注入。

    门禁管"有没有钥匙"，不管"房间现在空不空"—— 否则 GPU 恰好被占的那一刻
    冻结的预注册会被误拒。
    """
    grants("""
default:
  - kind: local_gpu
    devices: "1,2"
    probe_cmd: "false"
""")
    lines = cap.render_compute_section()
    assert any("已授权，本次探测失败" in ln for ln in lines)
    assert cap.allows_local_deployment() is True


def test_unknown_kind_without_probe_is_declared_only(grants):
    grants("""
default:
  - kind: quantum_annealer
    endpoint: somewhere
""")
    caps, _ = cap.snapshot(force=True)
    assert caps[0].status == "declared"


# ── 消费点 1：system prompt 注入（走真正的 section 构建）────────────────────

def test_wired_into_platform_capability_section(grants):
    """走 context_engine 的真入口 —— 只测 render_compute_section 的话，
    把 context_engine 那两行接线摘掉测试照样全绿。"""
    grants("""
default:
  - kind: local_gpu
    devices: "1,2"
    probe_cmd: "echo 2xA100-80G idle"
""")
    from core.context_engine import _platform_capability_section
    section = _platform_capability_section()
    assert section is not None
    assert "local_gpu" in section and "2xA100-80G idle" in section
    assert "开源权重模型" in section, "有部署钥匙时必须告诉 agent 这个可能性"
    assert "闭源 API 模型不因此可用" in section


# ── 消费点 2：freeze 门禁（同一份 snapshot）─────────────────────────────────

PREREG_QWEN = {"content": "Treatment arm: deploy Qwen-1.5B locally via vLLM "
                          "and replay routed steps.", "metadata": {}}
PREREG_GPT = {"content": "We will call gpt-4o for judging.", "metadata": {}}


def test_gate_admits_open_weight_when_deploy_granted(grants):
    from shared.tools.library.artifacts_extra import _prereg_feasibility_violations

    # 没钥匙：qwen 被拒（原行为）
    grants("default: []")
    assert _prereg_feasibility_violations(PREREG_QWEN), "无授权时点名 qwen 必须拦"

    # 有本地部署钥匙：qwen 放行，闭源 gpt-4o 照拦
    grants("""
default:
  - kind: local_gpu
    devices: "1,2"
    probe_cmd: "echo ok"
""")
    assert _prereg_feasibility_violations(PREREG_QWEN) == []
    assert _prereg_feasibility_violations(PREREG_GPT), "有 GPU 也变不出闭源权重"


def test_gate_and_injection_read_the_same_snapshot(grants):
    """两处消费必须同源：改一次 grants，两处同时改变。"""
    from core.context_engine import _platform_capability_section
    from shared.tools.library.artifacts_extra import _prereg_feasibility_violations

    grants("default: []")
    assert "算力/硬件授权" not in (_platform_capability_section() or "")
    assert _prereg_feasibility_violations(PREREG_QWEN)

    grants("""
default:
  - kind: local_gpu
    probe_cmd: "echo ok"
""")
    assert "算力/硬件授权" in (_platform_capability_section() or "")
    assert _prereg_feasibility_violations(PREREG_QWEN) == []
