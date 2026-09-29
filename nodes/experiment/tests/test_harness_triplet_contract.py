"""harness 里三件套契约的自洽性（#919 回归）。

`harness.yaml` 曾同时说三件事，互相矛盾：

- rules 写「冻结顺序 raw→clean→log」；
- `required_output_artifact_types` 却按 `[experiment_log, clean_results, raw_results]`
  倒序列出；
- `expected_outputs.experiment_log` 把它称作「所有 Experiment run 的**唯一**冻结交付」。

模型读的就是这三段。倒序清单会诱导它先建 log 再补证据；「唯一冻结交付」与三件套
并列存在，则让它以为另外两件可以不冻。本文件把「集合不变、顺序与实际冻结一致、
措辞不自相矛盾」钉住 —— 但**不锁死任意措辞或规则条数**，只验语义。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

_HARNESS = Path(__file__).resolve().parents[1] / "harness.yaml"


def _raw() -> dict:
    return yaml.safe_load(_HARNESS.read_text(encoding="utf-8"))


def test_required_outputs_keep_the_triplet_and_follow_freeze_order():
    got = _raw()["required_output_artifact_types"]

    assert set(got) == {"raw_results", "clean_results", "experiment_log"}, (
        "三件套成员不得增删")
    assert got == ["raw_results", "clean_results", "experiment_log"], (
        "展示顺序必须与实际冻结顺序 raw→clean→log 一致；"
        "倒着写会诱导模型先建 experiment_log 再补证据")


def test_no_artifact_is_advertised_as_the_only_frozen_deliverable():
    outputs = _raw().get("expected_outputs") or {}
    log_desc = str(outputs.get("experiment_log") or "")

    assert "唯一冻结交付" not in log_desc, (
        "experiment_log 是三件套之一，不是唯一冻结交付 —— 这句会让模型以为"
        "raw_results/clean_results 可以不冻")
    # 三件套的其余两件必须仍然在 expected_outputs 里各自有描述。
    assert outputs.get("raw_results") and outputs.get("clean_results")


def test_no_empty_config_blocks_left_behind():
    raw = _raw()
    empty = [k for k, v in raw.items() if v is None]

    assert not empty, f"空配置块会被 loader 静默忽略，应删除而不是留着：{empty}"


def test_the_loader_still_reads_the_same_required_set():
    """改顺序不得改变 Core 解析出的必交产物集合。"""
    from core.loader import load_harness

    h = load_harness("experiment")
    assert set(h.required_output_artifact_types) == {
        "raw_results", "clean_results", "experiment_log"}
    # required_outputs 是别名，两条路径必须一致（core/harness.py 有明确注释）。
    assert set(h.required_outputs) == set(h.required_output_artifact_types)
