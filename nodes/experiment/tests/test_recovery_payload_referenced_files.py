"""恢复守卫的载荷摘要要看命令引用的脚本内容（收敛任务书 K9 改法 1、缺陷 #2；
识别规则按第三会话复审 0914c 第六节第 3 条）。

活体 partial_validation_failure：修好 wrapper 脚本后逐字重跑同一命令，被
route_recovery_payload_unchanged 拒——摘要只算命令文本；模型改了命令文本绕过守卫，
没修的处理程序又跑了一次，违反「只运行一次」。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from nodes.experiment.tools import resource_manager as manager
from nodes.experiment.tools import safe_bash as sb
from nodes.experiment.tools.execution_route import (
    _declare_execution_route, begin_route_step_attempt, finish_route_step_attempt,
    resolve_execution_context,
)
from test_execution_route import _state


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "x.py").write_text("print('v1')\n", encoding="utf-8")
    (work / "scripts" / "run.sh").write_text("exit 1\n", encoding="utf-8")
    (work / "cfg.yaml").write_text("a: 1\n", encoding="utf-8")
    return work


@pytest.mark.parametrize("cmd, expected", [
    ("python x.py", ["x.py"]),
    ("python -X dev x.py", ["x.py"]),
    ("env python x.py", ["x.py"]),
    ("mpirun -np 4 python x.py", ["x.py"]),
    ("bash -c 'python x.py'", ["x.py"]),
    ("bash scripts/run.sh && echo done", ["scripts/run.sh"]),
    ("python x.py;", ["x.py"]),
    # 配置 / 数据文件不纳入（第三会话复审 0915 P1：失败运行会自己改写这类文件）。
    ("./scripts/run.sh --config=cfg.yaml", ["scripts/run.sh"]),
    ("CONFIG=cfg.yaml python x.py", ["x.py"]),
    ("python -m runner --entry=x.py", ["x.py"]),
    ("echo nothing referenced", []),
], ids=["plain", "option_with_value", "env", "mpirun", "bash_c", "and_list",
        "semicolon", "config_file_left_out", "env_config_left_out", "long_option_script", "none"])
def test_every_word_that_resolves_to_a_file_is_fingerprinted(tmp_path, cmd, expected):
    work = _work(tmp_path)

    fingerprints = sb._referenced_file_fingerprints(cmd, work, None)

    assert sorted(fingerprints) == expected, fingerprints


def test_files_the_run_cannot_change_are_left_out(tmp_path):
    work = _work(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "tool.py").write_text("print('host')\n", encoding="utf-8")
    os.symlink(outside / "tool.py", work / "linked.py")

    fingerprints = sb._referenced_file_fingerprints(
        f"python {outside / 'tool.py'} linked.py scripts missing.py x.py", work, None)

    assert sorted(fingerprints) == ["x.py"], fingerprints


def test_the_fingerprint_follows_content_and_large_files_fall_back_to_size_and_mtime(
    tmp_path, monkeypatch,
):
    work = _work(tmp_path)
    first = sb._referenced_file_fingerprints("python x.py", work, None)
    assert sb._referenced_file_fingerprints("python x.py", work, None) == first
    (work / "x.py").write_text("print('v2')\n", encoding="utf-8")
    assert sb._referenced_file_fingerprints("python x.py", work, None) != first

    monkeypatch.setattr(sb, "_REFERENCED_FILE_HASH_LIMIT", 4)
    large = sb._referenced_file_fingerprints("python x.py", work, None)
    assert large["x.py"][0] == "size_mtime_ns", large


def test_a_command_without_referenced_files_keeps_the_old_digest(tmp_path):
    work = _work(tmp_path)
    old_formula = hashlib.sha256(json.dumps(
        {"command": "echo hi", "cwd": str(work.resolve()), "timeout": 60,
         "execution_params": None},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()

    assert sb._bash_payload_digest(
        "echo hi", work, timeout=60, execution_params=None, state=None) == old_formula
    assert manager._referenced_files_payload("echo hi", work, None) == {}
    assert set(manager._referenced_files_payload("python x.py", work, None)) == {
        "referenced_files"}


def _bash_decision(state, work: Path) -> dict:
    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash", "program": "bash", "route_step_id": "validate",
        "read_only": False, "observed_effects": ["workspace_write"],
        "workdir_roles": ["run_root"],
    })
    decision.update({"workdir_role_observed": True, "workdir_resolution_status": "resolved",
                     "resolved_workdir": str(work)})
    return decision


def _digest(work: Path) -> dict:
    return {"payload_digest": sb._bash_payload_digest(
        "bash scripts/run.sh", work, timeout=600, execution_params=None, state=None)}


def test_fixing_the_wrapper_lets_the_same_command_retry_and_an_unfixed_one_stays_refused(
    tmp_path,
):
    """partial_validation_failure 的序列：wrapper 失败 → 带 recovery_basis 重开 → 逐字重跑。"""
    state = _state(tmp_path)
    work = _work(tmp_path)
    route = {
        "schema_version": 2, "goal": "校验产物",
        "evidence_refs": ["https://example.invalid/validation-guide"],
        "steps": [{
            "id": "validate", "goal": "运行校验 wrapper", "after": [],
            "action": {"tool": "safe_run_bash", "program": "bash"},
            "effects": ["workspace_write"], "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }
    assert asyncio.run(_declare_execution_route(state, route=route))["status"] == "success"
    binding = begin_route_step_attempt(
        state, _bash_decision(state, work), tool="safe_run_bash", action=_digest(work))
    finish_route_step_attempt(
        state, binding, result={"status": "error", "returncode": 1})
    evidence_ref = "artifact:" + state.save_artifact(
        "diagnostic_evidence", "wrapper_failure", "wrapper 里校验路径写错。")["id"]
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["goal"] = "修好 wrapper 后重跑校验"
    revised["evidence_refs"].append(evidence_ref)
    amended = asyncio.run(_declare_execution_route(
        state, route=revised, amendment_reason="已定位 wrapper 里的路径错误",
        recovery_basis={"attempt_id": binding["attempt_id"], "failure_class": "execution",
                        "diagnosis": "wrapper 校验路径写错", "evidence_refs": [evidence_ref]}))
    assert amended["status"] == "success", amended

    unchanged = begin_route_step_attempt(
        state, _bash_decision(state, work), tool="safe_run_bash", action=_digest(work))
    assert unchanged["binding_error"] == "route_recovery_payload_unchanged", unchanged

    (work / "scripts" / "run.sh").write_text("exit 0\n", encoding="utf-8")
    fixed = begin_route_step_attempt(
        state, _bash_decision(state, work), tool="safe_run_bash", action=_digest(work))
    assert not fixed.get("binding_error"), fixed
    assert fixed["attempt_id"] != binding["attempt_id"]


# ── 第三会话复审 0915 P1：失败运行自己写出的输出不算载荷变化 ─────────────────────
# 探针 review-0914-third/probe_batch2 前 4 格：失败那次运行把日志/输出写到命令点名的路径上，
# 2062467c 把这些文件算进摘要，没修的重跑被放行。


def _retry_after_failure(tmp_path: Path, program: str, command: str, *,
                         written: str | None = None, fix_script: bool = False,
                         written_bytes: bytes | None = None,
                         written_mode: int | None = None) -> dict:
    state = _state(tmp_path)
    work = _work(tmp_path)
    (work / "input.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    route = {
        "schema_version": 2, "goal": "校验产物",
        "evidence_refs": ["https://example.invalid/validation-guide"],
        "steps": [{
            "id": "validate", "goal": "运行校验", "after": [],
            "action": {"tool": "safe_run_bash", "program": program},
            "effects": ["workspace_write"], "workdir_role": "run_root", "expected_outputs": [],
        }],
    }
    assert asyncio.run(_declare_execution_route(state, route=route))["status"] == "success"

    def decision() -> dict:
        resolved = resolve_execution_context(state, {
            "tool": "safe_run_bash", "program": program, "route_step_id": "validate",
            "read_only": False, "observed_effects": ["workspace_write"],
            "workdir_roles": ["run_root"],
        })
        resolved.update({"workdir_role_observed": True, "workdir_resolution_status": "resolved",
                         "resolved_workdir": str(work)})
        return resolved

    def action() -> dict:
        return {"payload_digest": sb._bash_payload_digest(
            command, work, timeout=600, execution_params=None, state=None)}

    binding = begin_route_step_attempt(state, decision(), tool="safe_run_bash", action=action())
    if written:
        target = work / written
        if written_bytes is None:
            target.write_text("partial output from the failed run\n", encoding="utf-8")
        else:
            target.write_bytes(written_bytes)
        if written_mode is not None:
            target.chmod(written_mode)
    finish_route_step_attempt(state, binding, result={"status": "error", "returncode": 1})
    evidence_ref = "artifact:" + state.save_artifact(
        "diagnostic_evidence", "failure_note", "失败诊断")["id"]
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["goal"] = "诊断后重跑"
    revised["evidence_refs"].append(evidence_ref)
    amended = asyncio.run(_declare_execution_route(
        state, route=revised, amendment_reason="诊断后重开",
        recovery_basis={"attempt_id": binding["attempt_id"], "failure_class": "execution",
                        "diagnosis": "诊断", "evidence_refs": [evidence_ref]}))
    assert amended["status"] == "success", amended
    if fix_script:
        (work / "scripts" / "run.sh").write_text("exit 0\n", encoding="utf-8")
    return begin_route_step_attempt(state, decision(), tool="safe_run_bash", action=action())


@pytest.mark.parametrize("program, command, written", [
    ("bash", "bash scripts/run.sh > run.log", "run.log"),
    ("bash", "bash scripts/run.sh | tee run.log", "run.log"),
    ("python", "python x.py --out result.json", "result.json"),
    ("python", "python x.py input.csv summary.json", "summary.json"),
], ids=["redirect_log", "tee_log", "out_option", "positional_output"])
def test_output_written_by_the_failed_run_does_not_unlock_an_unfixed_retry(
    tmp_path, program, command, written,
):
    retry = _retry_after_failure(tmp_path, program, command, written=written)

    assert retry["binding_error"] == "route_recovery_payload_unchanged", retry


def test_fixing_the_script_still_unlocks_a_retry_that_also_writes_a_log(tmp_path):
    retry = _retry_after_failure(tmp_path, "bash", "bash scripts/run.sh > run.log",
                                 written="run.log", fix_script=True)

    assert not retry.get("binding_error"), retry


@pytest.mark.parametrize("name, content, mode, counted", [
    ("tool", "#!/bin/sh\nexit 0\n", 0o644, True),
    ("runner", "compiled", 0o755, False),      # 可执行位本身不算（构建出的二进制也带）
    ("wrapper", "#!/bin/sh\nexit 0\n", 0o755, True),
    ("model.R", "x <- 1\n", 0o644, True),
    ("result.json", "{}", 0o644, False),
    ("run.log", "log line\n", 0o644, False),
], ids=["shebang", "executable_binary", "shebang_executable", "r_script", "json_output", "log"])
def test_only_script_like_files_are_fingerprinted(tmp_path, name, content, mode, counted):
    work = tmp_path / "w"
    work.mkdir()
    target = work / name
    target.write_text(content, encoding="utf-8")
    target.chmod(mode)

    assert (name in sb._referenced_file_fingerprints(f"sh {name}", work, None)) is counted


def test_a_binary_built_by_the_failed_run_does_not_unlock_an_unfixed_retry(tmp_path):
    """第三会话复审 0915b P3（探针 probe_0915_fixes 的 built_binary_unfixed）：失败那次运行构建出带
    可执行位的二进制，可执行位原先也算「像脚本」，没修的重跑被放行。"""
    retry = _retry_after_failure(
        tmp_path, "bash", "bash scripts/run.sh && ./app", written="app",
        written_bytes=b"\x7fELF\x02\x01\x01binary", written_mode=0o755)

    assert retry["binding_error"] == "route_recovery_payload_unchanged", retry
