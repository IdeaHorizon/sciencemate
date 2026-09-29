"""owner_config.yaml —— 节点 owner 的控制面板。

每个节点可以在自己 folder 放一份 `owner_config.yaml`，集中调以下 4 类参数：

  - llm          : LLM call 相关（timeout, temperature override...）
  - budget       : 节点 budget（max_turns, max_context_tokens, max_output_tokens...）
  - summarizer   : context 压缩触发参数
  - subagent     : 子节点调度参数（max_depth, max_parallel, child_timeout）

加载顺序（后面的 override 前面的）：

  1. core defaults                  （NodeHarness dataclass 默认值）
  2. env vars                       （LLM_TIMEOUT, LLM_CONTEXT_WINDOW 等）
  3. nodes/<n>/harness.yaml         （owner 写的传统配置）
  4. nodes/<n>/owner_config.yaml    ← **本文件，优先级最高**

设计哲学：

  - owner 看一个文件就能改所有可调旋钮，不用翻 agent_loop / llm.py
  - 全 opt-in：留空 / 注释 → 走上面层级的值
  - schema 校验严：错的 key / 类型 → loader 抛 ValueError，不静默吞
  - 字段是**已经被 framework 真消费**的；列出但未消费的字段会在加载时 warn
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# ── 已支持字段白名单 ─────────────────────────────────────────────────────────
# 这些是 framework 真消费的字段。新增字段时**必须**同时改本表 + 在 framework
# 代码里真消费它，否则 owner 配了不生效 = 误导。
SUPPORTED_FIELDS: dict[str, dict[str, type]] = {
    "llm": {
        "timeout_s": (int, float),
        "temperature_override": (int, float),
        "max_retries": int,
    },
    "budget": {
        "max_turns": int,
        "max_context_tokens": int,
        "max_output_tokens": int,
    },
    "summarizer": {
        "trigger_threshold": (int, float),
        "keep_last_n_turns": int,
        "target_tokens": int,
    },
    "subagent": {
        "max_depth": int,
        "max_parallel": int,
        "child_timeout_s": (int, float),
    },
}


@dataclass
class OwnerConfig:
    """从 owner_config.yaml 解析的结构化覆盖配置。

    所有字段都是 Optional —— None 表示 "不覆盖，走上层默认"。
    """
    # llm
    llm_timeout_s: float | None = None
    llm_max_retries: int | None = None
    temperature_override: float | None = None

    # budget
    max_turns: int | None = None
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None

    # summarizer
    summarizer_trigger_threshold: float | None = None
    summarizer_keep_last_n_turns: int | None = None
    summarizer_target_tokens: int | None = None

    # subagent
    subagent_max_depth: int | None = None
    subagent_max_parallel: int | None = None
    subagent_child_timeout_s: float | None = None

    # raw 原始 dict（debug / 反射用；不用于运行时逻辑）
    raw: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """没任何字段被设 → 等价于"owner 没写"，loader 跳过 merge。"""
        return all(
            getattr(self, k) is None
            for k in (
                "llm_timeout_s", "llm_max_retries", "temperature_override",
                "max_turns", "max_context_tokens", "max_output_tokens",
                "summarizer_trigger_threshold", "summarizer_keep_last_n_turns",
                "summarizer_target_tokens",
                "subagent_max_depth", "subagent_max_parallel",
                "subagent_child_timeout_s",
            )
        )


def _validate_section(
    section_name: str, section: dict[str, Any], allowed: dict[str, type],
) -> None:
    """校验一个 section 的所有 key 和类型；不认识的 key warn，类型错 raise。"""
    for k, v in section.items():
        if k not in allowed:
            log.warning(
                "owner_config: 未知字段 %s.%s（拼写错？或框架还没消费这个字段？）"
                "—— 已忽略", section_name, k,
            )
            continue
        expected = allowed[k]
        if not isinstance(v, expected):
            raise ValueError(
                f"owner_config: {section_name}.{k} 类型错 —— "
                f"expected {expected}, got {type(v).__name__} (value={v!r})"
            )


def parse_owner_config(raw: dict[str, Any]) -> OwnerConfig:
    """把 yaml 解析后的 dict 转 OwnerConfig；校验 key / 类型。"""
    if not raw:
        return OwnerConfig()

    if not isinstance(raw, dict):
        raise ValueError(
            f"owner_config yaml 顶层必须是 dict，got {type(raw).__name__}"
        )

    # 校验每 section
    for sec_name in raw.keys():
        if sec_name not in SUPPORTED_FIELDS:
            log.warning(
                "owner_config: 未知顶层 section '%s'（应为 llm/budget/"
                "summarizer/subagent 之一）—— 已忽略", sec_name,
            )
            continue
        sec_dict = raw[sec_name] or {}
        if not isinstance(sec_dict, dict):
            raise ValueError(
                f"owner_config: '{sec_name}' section 必须是 dict, "
                f"got {type(sec_dict).__name__}"
            )
        _validate_section(sec_name, sec_dict, SUPPORTED_FIELDS[sec_name])

    llm = raw.get("llm") or {}
    budget = raw.get("budget") or {}
    summ = raw.get("summarizer") or {}
    sub = raw.get("subagent") or {}

    return OwnerConfig(
        llm_timeout_s=llm.get("timeout_s"),
        llm_max_retries=llm.get("max_retries"),
        temperature_override=llm.get("temperature_override"),
        max_turns=budget.get("max_turns"),
        max_context_tokens=budget.get("max_context_tokens"),
        max_output_tokens=budget.get("max_output_tokens"),
        summarizer_trigger_threshold=summ.get("trigger_threshold"),
        summarizer_keep_last_n_turns=summ.get("keep_last_n_turns"),
        summarizer_target_tokens=summ.get("target_tokens"),
        subagent_max_depth=sub.get("max_depth"),
        subagent_max_parallel=sub.get("max_parallel"),
        subagent_child_timeout_s=sub.get("child_timeout_s"),
        raw=raw,
    )


def load_owner_config(node_dir: Path) -> OwnerConfig:
    """加载 nodes/<n>/owner_config.yaml。文件不存在 → 返空 OwnerConfig。

    Args:
        node_dir: nodes/<node_type>/ 的绝对或相对路径
    """
    path = node_dir / "owner_config.yaml"
    if not path.exists():
        return OwnerConfig()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(
            f"owner_config: 解析 {path} 失败 —— {e}"
        ) from e

    return parse_owner_config(raw)


def apply_owner_config(harness: Any, cfg: OwnerConfig) -> None:
    """把 OwnerConfig 的非空字段 override 到 NodeHarness 实例上。

    in-place 修改 harness。loader 在 harness yaml 加载完 + 默认值兜底后调
    本函数，让 owner_config 拿到最终决定权。
    """
    if cfg.is_empty():
        return

    # llm
    if cfg.llm_timeout_s is not None:
        # NodeHarness 加新字段 llm_timeout_s；llm.chat 调用方读
        harness.llm_timeout_s = float(cfg.llm_timeout_s)
    if cfg.llm_max_retries is not None:
        # v2.x: transient 错的重试次数 override（agent_loop 调 llm.chat 时透传）
        harness.llm_max_retries = int(cfg.llm_max_retries)
    if cfg.temperature_override is not None:
        harness.temperature = float(cfg.temperature_override)

    # budget
    if cfg.max_turns is not None:
        harness.max_turns = int(cfg.max_turns)
    if cfg.max_context_tokens is not None:
        harness.max_context_tokens = int(cfg.max_context_tokens)
    if cfg.max_output_tokens is not None:
        harness.max_output_tokens = int(cfg.max_output_tokens)

    # summarizer
    if cfg.summarizer_trigger_threshold is not None:
        harness.summarizer.trigger_threshold = float(cfg.summarizer_trigger_threshold)
    if cfg.summarizer_keep_last_n_turns is not None:
        harness.summarizer.keep_last_n_turns = int(cfg.summarizer_keep_last_n_turns)
    if cfg.summarizer_target_tokens is not None:
        harness.summarizer.target_tokens = int(cfg.summarizer_target_tokens)

    # subagent
    if cfg.subagent_max_depth is not None:
        harness.subagent_max_depth = int(cfg.subagent_max_depth)
    if cfg.subagent_max_parallel is not None:
        harness.subagent_max_parallel = int(cfg.subagent_max_parallel)
    if cfg.subagent_child_timeout_s is not None:
        harness.subagent_child_timeout_s = float(cfg.subagent_child_timeout_s)
