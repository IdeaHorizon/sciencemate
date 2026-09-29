"""成本 / 缓存记账。

核心不变量：**unknown ≠ 0**。provider 不报缓存字段、或模型没配价格时，
必须能与"缓存一次没命中"、"这次调用免费"区分开 —— 否则判据归零而没人知道。
"""
from __future__ import annotations

import json

import pytest

from core.cost_ledger import (
    Price,
    compute_cost,
    format_rollup,
    lookup_price,
    normalize_usage,
    read_rollup,
    record,
)


# ── usage 归一 ─────────────────────────────────────────────────────────────

def test_openai_dialect_cached_tokens():
    u = normalize_usage({
        "prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
        "prompt_tokens_details": {"cached_tokens": 800},
    })
    assert (u.prompt_tokens, u.completion_tokens) == (1000, 200)
    assert u.cache_read == 800
    assert u.cache_hit_ratio() == pytest.approx(0.8)


def test_anthropic_dialect():
    u = normalize_usage({
        "input_tokens": 500, "output_tokens": 100,
        "cache_read_input_tokens": 400, "cache_creation_input_tokens": 50,
    })
    assert u.prompt_tokens == 500 and u.completion_tokens == 100
    assert u.cache_read == 400 and u.cache_write == 50
    assert u.total_tokens == 600          # 缺 total 时由 in+out 补


def test_deepseek_dialect():
    u = normalize_usage({
        "prompt_tokens": 900, "completion_tokens": 50,
        "prompt_cache_hit_tokens": 600,
    })
    assert u.cache_read == 600


def test_missing_cache_fields_stay_unknown_not_zero():
    """最关键的一条：provider 没报缓存 → None，不是 0。"""
    u = normalize_usage({"prompt_tokens": 100, "completion_tokens": 10})
    assert u.cache_read is None
    assert u.cache_hit_ratio() is None

    # 报了但确实是 0 —— 与上面必须不同
    u0 = normalize_usage({
        "prompt_tokens": 100, "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 0},
    })
    assert u0.cache_read == 0
    assert u0.cache_hit_ratio() == 0.0


def test_empty_usage_does_not_crash():
    for bad in (None, {}, {"garbage": 1}):
        u = normalize_usage(bad)
        assert u.prompt_tokens == 0 and u.cache_read is None


# ── 价格 ───────────────────────────────────────────────────────────────────

def test_selfhosted_is_zero_not_unknown():
    """自建推理没有外部账单：明确 0，且属于 known —— 与未配价格是两件事。"""
    p = lookup_price("gpustack", "whatever")
    assert p.is_known() and p.input_per_m == 0.0
    u = normalize_usage({"prompt_tokens": 10_000, "completion_tokens": 1_000})
    assert compute_cost(u, p) == 0.0


def test_unpriced_model_returns_none():
    p = lookup_price("mystery-provider", "mystery-model")
    assert not p.is_known()
    u = normalize_usage({"prompt_tokens": 10_000, "completion_tokens": 1_000})
    assert compute_cost(u, p) is None


def test_price_override_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_PRICES",
                       json.dumps({"acme:m1": {"in": 1.0, "out": 2.0}}))
    p = lookup_price("acme", "m1")
    assert p.input_per_m == 1.0 and p.output_per_m == 2.0
    u = normalize_usage({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
    assert compute_cost(u, p) == pytest.approx(3.0)


def test_override_beats_default(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_PRICES",
                       json.dumps({"deepseek-v4-pro": {"in": 0, "out": 0}}))
    assert compute_cost(
        normalize_usage({"prompt_tokens": 1_000_000, "completion_tokens": 0}),
        lookup_price(None, "deepseek-v4-pro"),
    ) == 0.0


def test_bad_override_json_is_ignored(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_PRICES", "{not json")
    assert lookup_price("gpustack", "x").input_per_m == 0.0


def test_cached_tokens_are_cheaper():
    p = Price(input_per_m=10.0, output_per_m=0.0, cache_read_per_m=1.0)
    fresh = compute_cost(normalize_usage(
        {"prompt_tokens": 1_000_000, "completion_tokens": 0,
         "prompt_tokens_details": {"cached_tokens": 0}}), p)
    cached = compute_cost(normalize_usage(
        {"prompt_tokens": 1_000_000, "completion_tokens": 0,
         "prompt_tokens_details": {"cached_tokens": 1_000_000}}), p)
    assert fresh == pytest.approx(10.0)
    assert cached == pytest.approx(1.0)


# ── 落盘与汇总 ─────────────────────────────────────────────────────────────

def test_record_and_rollup(tmp_path):
    for i in range(3):
        row = record(
            project_root=tmp_path, run_id="r1", node_type="writing",
            provider="gpustack", model="m", turn=i,
            usage={"prompt_tokens": 100, "completion_tokens": 10,
                   "prompt_tokens_details": {"cached_tokens": 60}},
        )
        assert row is not None and row["cost_usd"] == 0.0

    r = read_rollup(tmp_path, run_id="r1")
    assert r.calls == 3
    assert r.prompt_tokens == 300 and r.completion_tokens == 30
    assert r.cache_read == 180
    assert r.cache_hit_ratio == pytest.approx(0.6)
    assert r.cost_is_partial is False
    assert r.by_node["writing"] == 0.0


def test_rollup_filters_by_run(tmp_path):
    record(project_root=tmp_path, run_id="a", node_type="n", provider="gpustack",
           model="m", usage={"prompt_tokens": 10, "completion_tokens": 1})
    record(project_root=tmp_path, run_id="b", node_type="n", provider="gpustack",
           model="m", usage={"prompt_tokens": 99, "completion_tokens": 1})
    assert read_rollup(tmp_path, run_id="a").prompt_tokens == 10
    assert read_rollup(tmp_path).calls == 2          # 不过滤 = 全量


def test_rollup_marks_partial_cost(tmp_path):
    record(project_root=tmp_path, run_id="r", node_type="n", provider="gpustack",
           model="m", usage={"prompt_tokens": 10, "completion_tokens": 1})
    record(project_root=tmp_path, run_id="r", node_type="n",
           provider="mystery", model="unpriced",
           usage={"prompt_tokens": 10, "completion_tokens": 1})
    r = read_rollup(tmp_path, run_id="r")
    assert r.calls == 2 and r.cost_known_calls == 1
    assert r.cost_is_partial is True
    assert "下界" in format_rollup(r)


def test_rollup_cache_unknown_is_reported_as_unknown(tmp_path):
    record(project_root=tmp_path, run_id="r", node_type="n", provider="gpustack",
           model="m", usage={"prompt_tokens": 100, "completion_tokens": 1})
    r = read_rollup(tmp_path, run_id="r")
    assert r.cache_hit_ratio is None
    assert "unknown" in format_rollup(r)


def test_record_never_raises_on_bad_input():
    """观测是旁路：路径不可写、usage 畸形，都不能打断主流程。"""
    assert record(project_root="/nonexistent/nope", run_id=None, node_type=None,
                  provider=None, model=None, usage={"prompt_tokens": "x"}) is None
    assert record(project_root=None, run_id=None, node_type=None,
                  provider=None, model=None, usage=None) is not None


def test_rollup_survives_corrupt_lines(tmp_path):
    record(project_root=tmp_path, run_id="r", node_type="n", provider="gpustack",
           model="m", usage={"prompt_tokens": 10, "completion_tokens": 1})
    from core.cost_ledger import ledger_path
    p = ledger_path(tmp_path)
    with p.open("a", encoding="utf-8") as fh:
        fh.write("{broken json\n\n")
    assert read_rollup(tmp_path, run_id="r").calls == 1


# ── 自建 vs 商业 API ────────────────────────────────────────────────────────

def test_private_hosts_are_self_hosted():
    from core.cost_ledger import is_self_hosted

    for host in ("10.49.1.20:18080", "192.168.1.9", "172.16.0.3:8000",
                 "127.0.0.1:1234", "localhost:8080",
                 "desktop-9el2944.taile9f15e.ts.net", "box.local"):
        assert is_self_hosted(host), host
    for host in ("api.deepseek.com", "open.bigmodel.cn", "172.32.0.1", None, ""):
        assert not is_self_hosted(host), host


def test_selfhosted_open_weights_model_is_not_billed_at_vendor_price():
    """自建 GPUStack 上跑 deepseek-v4-pro：不能按 DeepSeek 官方报价计费。

    那个金额是编出来的 —— 比 unknown 更糟，因为它看起来像真的。
    """
    u = normalize_usage({"prompt_tokens": 1_000_000, "completion_tokens": 100_000})

    vendor = compute_cost(u, lookup_price("api.deepseek.com", "deepseek-v4-pro"))
    selfhosted = compute_cost(u, lookup_price("10.49.1.20:18080", "deepseek-v4-pro"))

    assert vendor is not None and vendor > 0
    assert selfhosted == 0.0


def test_override_still_wins_over_selfhosted_detection(monkeypatch):
    """运维想给自建机器记内部成本 —— override 优先级最高。"""
    monkeypatch.setenv("HARNESS_LLM_PRICES",
                       json.dumps({"10.49.1.20:18080": {"in": 5.0, "out": 5.0}}))
    p = lookup_price("10.49.1.20:18080", "m")
    assert p.input_per_m == 5.0


def test_unknown_host_does_not_inherit_vendor_price():
    """真机抓到的：自建 HPC 端点 zju.hpc.pub 跑 deepseek-v4-pro，
    既非私有 IP 也非内网域名，被按 DeepSeek 官方报价算出 $0.61 假账单。

    模型名说明「跑的是什么权重」，不说明「谁在收钱」。
    """
    from core.cost_ledger import is_vendor_host

    assert not is_vendor_host("zju.hpc.pub:17882")
    assert is_vendor_host("api.deepseek.com")

    u = normalize_usage({"prompt_tokens": 1_620_897, "completion_tokens": 19_553})
    assert compute_cost(u, lookup_price("zju.hpc.pub:17882", "deepseek-v4-pro")) is None
    assert compute_cost(u, lookup_price("api.deepseek.com", "deepseek-v4-pro")) > 0


def test_vendor_subdomain_recognised():
    from core.cost_ledger import is_vendor_host
    assert is_vendor_host("eu.api.openai.com")
    assert not is_vendor_host("notapi.openai.com.evil.tld")


def test_selfhosted_ip_still_zero():
    assert compute_cost(
        normalize_usage({"prompt_tokens": 1000, "completion_tokens": 10}),
        lookup_price("10.49.1.20:18080", "deepseek-v4-pro")) == 0.0
