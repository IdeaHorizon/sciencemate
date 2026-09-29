"""owner_config.yaml 加载 + merge 测试。"""
from __future__ import annotations

import pytest
import yaml

from core.owner_config import (
    OwnerConfig, apply_owner_config, load_owner_config, parse_owner_config,
)
from core.harness import NodeHarness, SummarizerConfig


# ── parse_owner_config ───────────────────────────────────────────────────────

def test_parse_empty_returns_empty_config():
    cfg = parse_owner_config({})
    assert cfg.is_empty()


def test_parse_all_sections():
    raw = {
        "llm": {"timeout_s": 600, "temperature_override": 0.3},
        "budget": {"max_turns": 50, "max_context_tokens": 200000,
                    "max_output_tokens": 32768},
        "summarizer": {"trigger_threshold": 0.6,
                        "keep_last_n_turns": 5, "target_tokens": 3000},
        "subagent": {"max_depth": 6, "max_parallel": 8,
                      "child_timeout_s": 180},
    }
    cfg = parse_owner_config(raw)
    assert cfg.llm_timeout_s == 600
    assert cfg.temperature_override == 0.3
    assert cfg.max_turns == 50
    assert cfg.max_context_tokens == 200000
    assert cfg.max_output_tokens == 32768
    assert cfg.summarizer_trigger_threshold == 0.6
    assert cfg.summarizer_keep_last_n_turns == 5
    assert cfg.summarizer_target_tokens == 3000
    assert cfg.subagent_max_depth == 6
    assert cfg.subagent_max_parallel == 8
    assert cfg.subagent_child_timeout_s == 180


def test_parse_partial_only_some_fields():
    raw = {"llm": {"timeout_s": 600}, "budget": {"max_turns": 50}}
    cfg = parse_owner_config(raw)
    assert cfg.llm_timeout_s == 600
    assert cfg.max_turns == 50
    # 其它字段应为 None
    assert cfg.temperature_override is None
    assert cfg.max_context_tokens is None
    assert cfg.summarizer_trigger_threshold is None


def test_parse_unknown_section_warns_not_raises(caplog):
    raw = {"weird_section": {"x": 1}}
    cfg = parse_owner_config(raw)
    assert cfg.is_empty()
    assert any("weird_section" in r.message for r in caplog.records)


def test_parse_unknown_field_warns_not_raises(caplog):
    raw = {"llm": {"timeout_s": 600, "unknown_knob": 42}}
    cfg = parse_owner_config(raw)
    assert cfg.llm_timeout_s == 600  # 已知字段正常解析
    assert any("unknown_knob" in r.message for r in caplog.records)


def test_parse_wrong_type_raises():
    raw = {"llm": {"timeout_s": "not a number"}}
    with pytest.raises(ValueError, match="timeout_s.*类型错"):
        parse_owner_config(raw)


def test_parse_section_not_dict_raises():
    raw = {"llm": "not a dict"}
    with pytest.raises(ValueError, match="必须是 dict"):
        parse_owner_config(raw)


def test_parse_top_level_not_dict_raises():
    with pytest.raises(ValueError, match="顶层必须是 dict"):
        parse_owner_config("not a dict")    # type: ignore[arg-type]


# ── load_owner_config（文件 IO）─────────────────────────────────────────────

def test_load_missing_file_returns_empty(tmp_path):
    cfg = load_owner_config(tmp_path)
    assert cfg.is_empty()


def test_load_yaml_file(tmp_path):
    (tmp_path / "owner_config.yaml").write_text(yaml.dump({
        "llm": {"timeout_s": 450},
        "budget": {"max_turns": 30},
    }))
    cfg = load_owner_config(tmp_path)
    assert cfg.llm_timeout_s == 450
    assert cfg.max_turns == 30


def test_load_empty_yaml_returns_empty(tmp_path):
    (tmp_path / "owner_config.yaml").write_text("# nothing\n")
    cfg = load_owner_config(tmp_path)
    assert cfg.is_empty()


def test_load_malformed_yaml_raises(tmp_path):
    (tmp_path / "owner_config.yaml").write_text("llm:\n  timeout_s: [unclosed")
    with pytest.raises(ValueError, match="解析.*失败"):
        load_owner_config(tmp_path)


# ── apply_owner_config（merge 到 NodeHarness）──────────────────────────────

def _new_harness() -> NodeHarness:
    return NodeHarness(
        node_type="test",
        max_context_tokens=120000,
        max_output_tokens=16384,
        temperature=0.7,
        max_turns=12,
        summarizer=SummarizerConfig(),
    )


def test_apply_empty_config_no_change():
    h = _new_harness()
    before = (h.max_turns, h.max_context_tokens, h.temperature,
              h.summarizer.trigger_threshold)
    apply_owner_config(h, OwnerConfig())
    after = (h.max_turns, h.max_context_tokens, h.temperature,
             h.summarizer.trigger_threshold)
    assert before == after


def test_apply_overrides_llm():
    h = _new_harness()
    apply_owner_config(h, OwnerConfig(
        llm_timeout_s=600, temperature_override=0.3,
    ))
    assert h.llm_timeout_s == 600.0
    assert h.temperature == 0.3


def test_apply_overrides_budget():
    h = _new_harness()
    apply_owner_config(h, OwnerConfig(
        max_turns=50, max_context_tokens=200000, max_output_tokens=32768,
    ))
    assert h.max_turns == 50
    assert h.max_context_tokens == 200000
    assert h.max_output_tokens == 32768


def test_apply_overrides_summarizer():
    h = _new_harness()
    apply_owner_config(h, OwnerConfig(
        summarizer_trigger_threshold=0.6,
        summarizer_keep_last_n_turns=5,
        summarizer_target_tokens=3000,
    ))
    assert h.summarizer.trigger_threshold == 0.6
    assert h.summarizer.keep_last_n_turns == 5
    assert h.summarizer.target_tokens == 3000


def test_apply_overrides_subagent():
    h = _new_harness()
    apply_owner_config(h, OwnerConfig(
        subagent_max_depth=6,
        subagent_max_parallel=8,
        subagent_child_timeout_s=180,
    ))
    assert h.subagent_max_depth == 6
    assert h.subagent_max_parallel == 8
    assert h.subagent_child_timeout_s == 180.0


def test_apply_partial_only_set_fields():
    h = _new_harness()
    h.max_context_tokens = 50000  # 模拟 yaml 设过
    h.max_output_tokens = 8192
    apply_owner_config(h, OwnerConfig(max_turns=99))
    # max_turns 被 override，其它保留
    assert h.max_turns == 99
    assert h.max_context_tokens == 50000
    assert h.max_output_tokens == 8192


# ── 端到端：loader 调 load_harness，owner_config 真生效 ────────────────────

def test_load_harness_applies_owner_config(tmp_path):
    """模拟一个完整 nodes/<n>/ 目录，验证 load_harness 调 owner_config 后
    NodeHarness 字段被 override。"""
    from core.loader import load_harness

    # 造 nodes/<n>/
    node_dir = tmp_path / "nodes" / "fake_node"
    node_dir.mkdir(parents=True)

    (node_dir / "harness.yaml").write_text(yaml.dump({
        "node_type": "fake_node",
        "system_prompt": "test",
        "context_config": {
            "max_context_tokens": 60000,    # owner_config 应 override
            "max_output_tokens": 4096,       # owner_config 应 override
            "temperature": 0.7,              # owner_config 应 override
        },
        "max_turns": 12,                     # owner_config 应 override
    }))

    (node_dir / "owner_config.yaml").write_text(yaml.dump({
        "llm": {"timeout_s": 600, "temperature_override": 0.3},
        "budget": {"max_turns": 50, "max_context_tokens": 200000,
                    "max_output_tokens": 32768},
    }))

    h = load_harness("fake_node", nodes_dir=tmp_path / "nodes")
    assert h.llm_timeout_s == 600.0
    assert h.temperature == 0.3
    assert h.max_turns == 50
    assert h.max_context_tokens == 200000
    assert h.max_output_tokens == 32768


def test_load_harness_no_owner_config_keeps_yaml_values(tmp_path):
    """没有 owner_config.yaml 时 harness.yaml 的值应保留。"""
    from core.loader import load_harness

    node_dir = tmp_path / "nodes" / "fake_node"
    node_dir.mkdir(parents=True)
    (node_dir / "harness.yaml").write_text(yaml.dump({
        "node_type": "fake_node",
        "context_config": {"max_context_tokens": 60000},
        "max_turns": 12,
    }))

    h = load_harness("fake_node", nodes_dir=tmp_path / "nodes")
    assert h.max_context_tokens == 60000
    assert h.max_turns == 12
    assert h.llm_timeout_s is None       # 没 override = None = 用 LLMClient default


# ── 7 个 producing 节点 stub 都能加载 ─────────────────────────────────────

@pytest.mark.parametrize("node_name", [
    "literature", "hypothesis", "data", "experiment",
    "postprocess", "writing",
])
def test_producing_node_has_owner_config_stub(node_name):
    """所有 producing 节点都有 owner_config.yaml stub（默认全注释，加载应得空 config）。"""
    from pathlib import Path
    cfg = load_owner_config(Path("nodes") / node_name)
    # stub 默认全注释 → is_empty
    assert cfg.is_empty(), (
        f"nodes/{node_name}/owner_config.yaml 应该默认全注释（is_empty），"
        "如果你 enable 了字段记得把测试期望调整"
    )
