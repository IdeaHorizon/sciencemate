"""049-3a: the configured diagnose rules reach the model where the failure is seen.

Codex's 049/01 probe (P0): ``generic_failure_detector`` globs ``state.root``
non-recursively while real logs live under ``runtime/logs`` and the managed
job's stdout/stderr, so a YAML rule that passes ``test_failure_diagnosis_delivery``
never fires in a live run.  This file pins the two authoritative delivery
points instead:

* a failed ``safe_run_bash`` result carries ``diagnosis`` (fix + context of the
  configured rules that matched its stderr/stdout);
* a managed job's health snapshot carries ``diagnosis`` computed over the same
  log tails it already reads for error markers.

Both are informational: no status, gate, or ``error_evidence`` reads them.
Also pinned (Codex P1): ``category: linking`` keeps its category instead of
being silently relabelled ``runtime``, and an unknown category warns.

Everything here is red on d95753ba.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
import yaml

from core import sandbox
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tests.test_035a_health_and_roc import _local_record, _probe
from nodes.experiment.tools import diagnose
from nodes.experiment.tools import resource_manager as manager
from nodes.experiment.tools import safe_bash
from nodes.experiment.tools.run_contract import _classify_experiment_scope

YAML_ONLY_LINE = "mpiifort: ifort: command not found"          # rule mpi_wrapper_backend_missing
YAML_ONLY_FIX = "重定向 wrapper 后端到已安装的编译器"
FMS_LINE = "FATAL from PE 0: mpp_domains_stack overflow; increase domains_stack_size"


# ── Codex P1：category 不再静默降级 ─────────────────────────────────────────

def test_linking_rules_keep_their_category():
    report = diagnose.DiagnoseEngine.from_yaml_patterns().analyze_output(
        "ld: error: cannot find -lnetcdff\n")
    categories = {finding["category"] for finding in report["findings"]}
    assert "linking" in categories, report


def test_shipped_rules_use_only_known_categories_and_severities():
    rules = yaml.safe_load(
        (Path(diagnose.__file__).parent / "diagnose_patterns" / "diagnose_patterns.yaml")
        .read_text(encoding="utf-8"))["patterns"]
    known_categories = {item.value for item in diagnose.Category}
    known_severities = {item.value for item in diagnose.Severity}
    bad = [(rule["id"], rule.get("category"), rule.get("severity")) for rule in rules
           if rule.get("category", "runtime") not in known_categories
           or rule.get("severity", "error") not in known_severities]
    assert bad == [], bad


def test_unknown_category_warns_instead_of_silently_relabelling(tmp_path, monkeypatch, caplog):
    fake_module_dir = tmp_path / "pkg"
    (fake_module_dir / "diagnose_patterns").mkdir(parents=True)
    (fake_module_dir / "diagnose_patterns" / "diagnose_patterns.yaml").write_text(
        yaml.safe_dump({"patterns": [{
            "id": "typo_rule", "regex": "xyzzy failed", "category": "linkng",
            "severity": "error", "generic_fix": "fix xyzzy"}]}),
        encoding="utf-8")
    monkeypatch.setattr(diagnose, "__file__", str(fake_module_dir / "diagnose.py"))
    with caplog.at_level(logging.WARNING, logger=diagnose.__name__):
        patterns = diagnose.load_yaml_patterns()
    assert [p.pattern for p in patterns] == ["xyzzy failed"]       # the rule survives
    assert patterns[0].category is diagnose.Category.RUNTIME       # documented fallback
    assert any("typo_rule" in record.getMessage() and "linkng" in record.getMessage()
               for record in caplog.records), caplog.text


# ── 投影本身 ─────────────────────────────────────────────────────────────────

def test_configured_guidance_keeps_one_entry_per_rule_and_drops_builtin_only_hits():
    text = "\n".join([YAML_ONLY_LINE, YAML_ONLY_LINE, "random failure text with no rule"])
    guidance = diagnose.configured_guidance(text)
    assert len(guidance) == 1, guidance
    assert guidance[0]["fix"].startswith(YAML_ONLY_FIX[:6])
    assert guidance[0]["category"] == "compilation"
    assert diagnose.configured_guidance("random failure text with no rule\n") == []
    # 只有内置规则命中（它们带 context_hint 但没有修法）：不值一条。
    assert diagnose.configured_guidance("warning: foo is deprecated\n") == []
    assert diagnose.configured_guidance("") == []


def test_configured_guidance_shows_the_most_specific_rule_when_two_cover_the_same_line():
    """`_analyze` keeps equal-span configured interpretations side by side (041 policy);
    the projection picks one per line — the higher-confidence, i.e. more specific, rule."""
    engine = diagnose.DiagnoseEngine(extra_patterns=[
        diagnose.ErrorPattern(r"(?i)error:\s*(.+)", diagnose.Category.COMPILATION,
                              diagnose.Severity.ERROR, 0.8, generic_fix="generic fix", configured=True),
        diagnose.ErrorPattern(r"^ERROR: Unknown command: .+", diagnose.Category.INPUT,
                              diagnose.Severity.ERROR, 0.95, generic_fix="specific fix", configured=True),
    ])
    guidance = diagnose.configured_guidance(
        "ERROR: Unknown command: fx (src/input.cpp:1)\n", engine=engine)
    assert [item["fix"] for item in guidance] == ["specific fix"], guidance


# ── 送达点 1：失败的 safe_run_bash 结果 ─────────────────────────────────────

def test_failed_command_result_carries_the_configured_fix():
    result = {"status": "error", "returncode": 1, "stderr_tail": YAML_ONLY_LINE}
    safe_bash._attach_diagnosis(result, stdout="", stderr=YAML_ONLY_LINE + "\n")
    assert result["diagnosis"][0]["fix"].startswith(YAML_ONLY_FIX[:6]), result
    assert result["status"] == "error" and result["returncode"] == 1     # untouched


def test_successful_or_unmatched_results_get_no_diagnosis_key():
    ok = {"status": "success", "returncode": 0}
    safe_bash._attach_diagnosis(ok, stdout="", stderr=YAML_ONLY_LINE + "\n")
    assert "diagnosis" not in ok
    unmatched = {"status": "error", "returncode": 2}
    safe_bash._attach_diagnosis(unmatched, stdout="", stderr="make: *** No rule to make target 'x'\n")
    assert "diagnosis" not in unmatched


def test_bounded_process_builder_also_carries_the_configured_fix(tmp_path, monkeypatch):
    """The second result builder (bounded stream supervisor, kept for the build-guard
    callers) attaches the same diagnosis; a real child process, no sandbox needed."""
    from nodes.experiment.tests.test_build_resource_guard import _State
    from nodes.experiment.tools import build_resource_guard as guard
    state = _State(tmp_path)
    monkeypatch.setattr(safe_bash, "experiment_output_dir", lambda *_a, **_k: tmp_path)
    limits = guard.BuildLimits(
        parallelism=1, tasks_max=64, memory_high_bytes=10**9, memory_max_bytes=2 * 10**9,
        memory_swap_max_bytes=10**8, timeout_s=10,
        disk_warning_free_bytes=1, disk_stop_free_bytes=0)

    async def run():
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", f"printf '%s\\n' '{YAML_ONLY_LINE}' >&2; exit 1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        return await safe_bash._bounded_process_wait(
            proc, state=state, cmd="mpiifort -c a.f90", timeout=10,
            limits=limits, unit=None, strong_guard=False)

    result = asyncio.run(run())
    assert result["returncode"] == 1, result
    assert result["diagnosis"][0]["fix"].startswith(YAML_ONLY_FIX[:6]), result


def test_repeated_error_focus_block_carries_the_tool_diagnosis(tmp_path):
    state = State.new("experiment", tmp_path)
    record = {"name": "safe_run_bash", "args": {"cmd": "mpiifort -c a.f90"},
              "result": {"status": "error", "returncode": 127, "stderr_tail": YAML_ONLY_LINE,
                         "diagnosis": [{"category": "compilation", "severity": "error", "line": 1,
                                        "matched": YAML_ONLY_LINE, "fix": "fix A", "context": "ctx A"}]}}
    diag = hooks._diagnose_failure(record, state)
    assert diag is not None
    assert diag["guidance"] == record["result"]["diagnosis"]
    block = hooks._focus_block(diag)
    assert "修法: fix A" in block and "上下文: ctx A" in block


requires_sandbox = pytest.mark.skipif(
    not sandbox.availability()[0], reason="mandatory Docker sandbox is unavailable")


@requires_sandbox
def test_real_failed_command_end_to_end_carries_the_configured_fix(tmp_path, monkeypatch):
    """The whole path: real shell → real result dict → diagnosis (needs the Docker sandbox)."""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    base = tmp_path / "home" / "projects" / "proj-diag" / "runs"
    base.mkdir(parents=True, exist_ok=True)
    state = State.new(node_type="experiment", base_dir=base, project_id="proj-diag")
    run_root = safe_bash.experiment_output_dir(state, "runtime", create=True)
    state.hook_state["path_roles"] = {"experiment_root": str(state.root), "run_root": str(run_root)}
    state.hook_state["node_inputs"] = {"fixture": "049-3", "requested_work": "验证失败命令的诊断送达。"}
    classified = asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="environment_probe",
        reason="诊断送达回归的执行尝试必须绑定稳定的上游测试输入。"))
    assert classified["status"] == "success", classified
    # 只读调查形状（`ls -l <路径>` 免路线）；文件名让 ls 的报错行长成 wrapper 规则
    # 能命中的样子：`ls: cannot access 'mpiifort: ifort: command not found': No such file…`
    result = asyncio.run(safe_bash._safe_run_bash(
        state, f"ls -l '{YAML_ONLY_LINE}'", cwd=str(run_root)))
    assert result["status"] != "success" and result.get("returncode") not in (0, None), result
    assert result["diagnosis"][0]["fix"].startswith(YAML_ONLY_FIX[:6]), result


# ── 送达点 2：受管作业 health ────────────────────────────────────────────────

def test_health_reports_configured_guidance_from_the_job_log_without_changing_evidence(
        tmp_path, monkeypatch):
    work = tmp_path / "work"
    record = _local_record(work, completion_paths=[work / "logs" / "out.log"])
    Path(record["stderr_path"]).write_text(FMS_LINE + "\n", encoding="utf-8")
    _state, health = _probe(tmp_path, monkeypatch, record)
    assert health["diagnosis"], health
    assert health["diagnosis"][0]["path"] == str(Path(record["stderr_path"]).resolve())
    assert "domains_stack_size" in health["diagnosis"][0]["fix"]
    # 纯信息：默认 markers 不含这行，error_evidence 与之前一样为空。
    assert health["error_evidence"] == []


def test_health_diagnosis_is_empty_for_a_clean_log(tmp_path, monkeypatch):
    work = tmp_path / "work"
    record = _local_record(work, completion_paths=[work / "logs" / "out.log"])
    Path(record["stdout_path"]).write_text("step 1 done\nstep 2 done\n", encoding="utf-8")
    _state, health = _probe(tmp_path, monkeypatch, record)
    assert health["diagnosis"] == []
