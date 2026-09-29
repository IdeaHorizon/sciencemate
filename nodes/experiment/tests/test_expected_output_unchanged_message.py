"""「预期产物存在但本次执行没有更新它」与「缺失」分开报（收敛任务书 K5，缺陷清单 #15）。

活体里模型看到「缺少预期产物」，就去检查文件存不存在——文件明明在，于是反复怀疑路径。实际是
重跑前后指纹没变。判定不变（指纹没变也可能是作业确实没写），只把报错说准。
"""
from __future__ import annotations

from nodes.experiment.tools import execution_route as er


def _binding_with_baseline(workdir, specs):
    binding = {"expected_outputs": list(specs), "resolved_workdir": str(workdir)}
    matches = er._safe_expected_output_matches(binding)
    binding["expected_output_baseline"] = {
        spec: {"fingerprint": er._output_set_fingerprint(workdir, matches.get(spec) or [])}
        for spec in specs if matches.get(spec)
    }
    return binding


def test_an_existing_output_the_run_did_not_touch_is_told_apart_from_a_missing_one(tmp_path):
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")
    binding = _binding_with_baseline(tmp_path, ["summary.json", "result.csv"])

    _verified, _paths, missing = er._verify_expected_outputs(binding)
    unchanged = er._unchanged_expected_outputs(binding, missing)

    assert sorted(missing) == ["result.csv", "summary.json"]
    assert unchanged == ["summary.json"]

    block = er.route_outcome_block({
        "failure_class": "expected_outputs_missing",
        "missing_expected_outputs": missing,
        "unchanged_expected_outputs": unchanged,
        "attempt_id": "a", "route_step_id": "s",
    })

    assert block["reason"] == "route_expected_outputs_missing"
    assert "预期产物存在但本次执行没有更新它 ['summary.json']" in block["error"]
    assert "缺失 ['result.csv']" in block["error"]
    assert block["unchanged_expected_outputs"] == ["summary.json"]


def test_a_plainly_missing_output_keeps_the_original_message(tmp_path):
    block = er.route_outcome_block({
        "failure_class": "expected_outputs_missing",
        "missing_expected_outputs": ["result.csv"],
        "attempt_id": "a", "route_step_id": "s",
    })

    assert block["error"] == "命令返回成功，但路线步骤缺少预期产物：['result.csv']；已停止路线并等待诊断。"
    assert "unchanged_expected_outputs" not in block
