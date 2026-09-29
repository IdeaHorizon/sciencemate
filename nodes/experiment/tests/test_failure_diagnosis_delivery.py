"""The runtime failure detector must deliver configured, actionable guidance."""
from __future__ import annotations

import logging

from core.loop_hooks import HookContext
from nodes.experiment import hooks
from nodes.experiment.tools import diagnose


class _State:
    def __init__(self, root):
        self.root = root


def _diagnose(tmp_path, text: str) -> str:
    (tmp_path / "build.err").write_text(text, encoding="utf-8")
    messages = hooks.generic_failure_detector_on_turn_start(
        HookContext(harness=None, state=_State(tmp_path), messages=[], turn=1)
    )
    assert messages is not None
    return messages[0]["content"]


def test_yaml_only_rule_reaches_the_runtime_hook_with_fix_and_context(tmp_path):
    message = _diagnose(
        tmp_path,
        "mpiifort: ifort: command not found\n",
    )

    assert "[compilation]" in message
    assert "重定向 wrapper 后端到已安装的编译器" in message
    assert "MPI wrapper 后端选择错误" in message
    assert "添加工具 bin 目录到 PATH" not in message
    assert "[environment]" not in message


def test_fms_stack_overflow_prefers_domains_stack_size_guidance(tmp_path):
    message = _diagnose(
        tmp_path,
        "FATAL from PE 0: mpp_domains_stack overflow; increase domains_stack_size\n",
    )

    assert "domains_stack_size" in message
    assert "ulimit -s unlimited" not in message
    assert "先识别报错的库或程序" not in message


def test_plain_stack_overflow_uses_stack_guidance_not_numeric_guidance(tmp_path):
    message = _diagnose(tmp_path, "fatal: stack overflow\n")

    assert "先识别报错的库或程序" in message
    assert "检查除零、溢出、无效数值操作" not in message


def test_overlapping_builtin_and_yaml_rule_keeps_configured_guidance(tmp_path):
    message = _diagnose(tmp_path, "Segmentation fault\n")

    assert "检查数组越界、空指针、栈溢出" in message
    assert message.count("Segmentation fault") == 1


def test_equal_span_actionable_rules_are_both_preserved():
    first = diagnose.ErrorPattern(
        r"same failure",
        diagnose.Category.RUNTIME,
        diagnose.Severity.ERROR,
        generic_fix="first repair",
        configured=True,
    )
    second = diagnose.ErrorPattern(
        r"same failure",
        diagnose.Category.RUNTIME,
        diagnose.Severity.ERROR,
        generic_fix="second repair",
        configured=True,
    )
    engine = diagnose.DiagnoseEngine()
    engine.patterns = [first, second]

    report = engine.analyze_output("same failure")

    assert len(report["findings"]) == 2
    assert {finding["generic_fix"] for finding in report["findings"]} == {
        "first repair", "second repair",
    }


def test_nonoverlapping_failures_on_one_line_are_both_preserved(tmp_path):
    message = _diagnose(tmp_path, "Segmentation fault; no space left\n")

    assert "检查数组越界、空指针、栈溢出" in message
    assert "清理磁盘空间或更换输出目录" in message


def test_generic_match_on_another_line_is_preserved(tmp_path):
    message = _diagnose(
        tmp_path,
        "mpiifort: ifort: command not found\n"
        "unrelated-tool: command not found\n",
    )

    assert "重定向 wrapper 后端到已安装的编译器" in message
    assert "添加工具 bin 目录到 PATH" in message
    assert "[environment]" in message


def test_dominance_preserves_the_highest_severity_and_injection(tmp_path, monkeypatch):
    generic = diagnose.ErrorPattern(
        r"inner failure",
        diagnose.Category.RUNTIME,
        diagnose.Severity.CRITICAL,
        confidence=0.8,
    )
    specific = diagnose.ErrorPattern(
        r"outer inner failure context",
        diagnose.Category.COMPILATION,
        diagnose.Severity.ERROR,
        confidence=0.9,
        generic_fix="use the specific repair",
        configured=True,
    )
    engine = diagnose.DiagnoseEngine()
    engine.patterns = [generic, specific]
    report = engine.analyze_output("outer inner failure context")

    assert report["statistics"]["critical_count"] == 1
    assert report["statistics"]["error_count"] == 0
    assert len(report["findings"]) == 1
    assert report["findings"][0]["severity"] == "critical"

    monkeypatch.setattr(hooks, "_analyze_with_engine", lambda _path: report)
    message = _diagnose(tmp_path, "placeholder failure\n")
    assert "检测到 1 个严重问题" in message
    assert "use the specific repair" in message


def test_yaml_loader_failure_warns_and_falls_back_to_builtin(
    tmp_path, monkeypatch, caplog,
):
    log_path = tmp_path / "runtime.err"
    log_path.write_text("Segmentation fault\n", encoding="utf-8")

    def _broken_loader():
        raise ValueError("malformed diagnostic rules")

    monkeypatch.setattr(hooks.DiagnoseEngine, "from_yaml_patterns", _broken_loader)
    with caplog.at_level(logging.WARNING):
        report = hooks._analyze_with_engine(log_path)

    assert report["statistics"]["critical_count"] == 1
    assert "回退到内置规则" in caplog.text


def test_malformed_yaml_warns_and_returns_no_partial_rules(monkeypatch, caplog):
    def _malformed(_stream):
        raise ValueError("invalid yaml")

    monkeypatch.setattr(diagnose.yaml, "safe_load", _malformed)
    with caplog.at_level(logging.WARNING):
        patterns = diagnose.load_yaml_patterns()

    assert patterns == []
    assert "回退到内置规则" in caplog.text


def test_invalid_yaml_regex_warns_and_builtin_diagnosis_still_reaches_hook(
    tmp_path, monkeypatch, caplog,
):
    monkeypatch.setattr(
        diagnose.yaml,
        "safe_load",
        lambda _stream: {
            "patterns": [{
                "regex": "(",
                "category": "runtime",
                "severity": "critical",
            }],
        },
    )

    with caplog.at_level(logging.WARNING):
        message = _diagnose(tmp_path, "Segmentation fault\n")

    assert "[runtime]" in message
    assert "Segmentation fault" in message
    assert "回退到内置规则" in caplog.text


def test_runtime_injection_bounds_yaml_advice(tmp_path, monkeypatch):
    oversized = "x" * 2_000
    monkeypatch.setattr(
        hooks,
        "_analyze_with_engine",
        lambda _path: {
            "findings": [{
                "severity": "error",
                "category": "runtime",
                "message": oversized,
                "generic_fix": oversized,
                "context_hint": oversized,
            }],
        },
    )

    message = _diagnose(tmp_path, "failure\n")

    assert oversized not in message
    assert len(message) < 1_000
