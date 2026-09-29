"""收敛任务书第一批（brief_experiment_convergence_0914.md K1–K7）。

每条复现缺陷清单里对应的那段空转；K5 在 test_expected_output_unchanged_message.py。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.state import State
from core.tool_registry import get_tool
from nodes.experiment import hooks
from nodes.experiment.tools import safe_bash

_NODE = Path(__file__).resolve().parents[1]


def _rules() -> str:
    config = yaml.safe_load((_NODE / "harness.yaml").read_text(encoding="utf-8"))
    return "\n".join(config["rules"])


# ── K1 例行复盘不在第 1 轮触发 ────────────────────────────────────────────────


def test_the_cadence_review_waits_forty_turns_instead_of_firing_on_turn_one(tmp_path):
    state = State.new("experiment", tmp_path)

    first = hooks.strategic_review_on_turn_start(
        SimpleNamespace(state=state, turn=1, messages=[]))
    fortieth = hooks.strategic_review_on_turn_start(
        SimpleNamespace(state=state, turn=40, messages=[]))

    assert first is None
    assert fortieth and "例行路线复盘" in fortieth[0].content


# ── K2 开局告知 prereg 绑定失败 ───────────────────────────────────────────────


def _briefing(state, turn=1):
    return hooks._prereg_binding_briefing_on_turn_start(SimpleNamespace(state=state, turn=turn))


def _frozen_prereg(state: State, name: str) -> str:
    saved = state.save_artifact("pre_registration", name, f"# {name}", metadata={
        "run_role": "primary", "execution_mode": "scientific",
        "expected_params": {"case": name},
    })
    state.mark_frozen(saved["id"])
    return saved["id"]


def test_a_declared_prereg_that_does_not_exist_is_told_on_turn_one(tmp_path):
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"prereg_artifact_id": "pre_registration__missing"}

    messages = _briefing(state)

    text = messages[0].content
    assert "declared_prereg_not_found" in text
    assert "pre_registration__missing" in text
    assert "report_blocker" in text
    assert "不要为了绕开这道门把 scope 改判为 operation" in text
    assert any("prereg_dispatch" in str(item.get("blocker_id"))
               for item in state.hook_state.get("blockers") or [])
    assert _briefing(state, turn=2) is None


def test_pending_assignment_reports_non_authorizing_candidates_without_blocker(tmp_path):
    """Catalog 候选只是观察：child 不猜选权威，也不自行登记 blocker。"""
    state = State.new("experiment", tmp_path)
    _frozen_prereg(state, "plan_a")
    _frozen_prereg(state, "plan_b")

    messages = _briefing(state)

    assert messages and "prereg_assignment_pending" in messages[0].content
    assert "pre_registration__plan_a@v1" in messages[0].content
    assert "pre_registration__plan_b@v1" in messages[0].content
    assert "不构成本 run 的绑定或执行授权" in messages[0].content
    assert "不会自动选择候选" in messages[0].content
    assert "typed prereg_assignment=bound" in messages[0].content
    assert "ambiguous_preregs" not in messages[0].content
    assert "预注册绑定失败" not in messages[0].content   # 对不消费 prereg 的任务不说「失败」
    assert not (state.hook_state.get("blockers") or [])


def test_pending_assignment_reports_latest_frozen_candidate_and_visible_draft(tmp_path):
    """未指名时，旧冻结版是非授权候选；修订草稿只作为可见事实，且不登记 blocker。"""
    state = State.new("experiment", tmp_path)
    _frozen_prereg(state, "plan_a")
    state.save_artifact("pre_registration", "plan_a", "# plan_a v2 draft", metadata={
        "run_role": "primary", "execution_mode": "scientific",
        "expected_params": {"case": "plan_a"},
    }, amendment_reason="修订草稿，尚未冻结")

    messages = _briefing(state)

    assert messages and "prereg_assignment_pending" in messages[0].content
    assert "pre_registration__plan_a@v1" in messages[0].content
    assert "unfrozen draft v2 also visible" in messages[0].content
    assert "不构成本 run 的绑定或执行授权" in messages[0].content
    assert not (state.hook_state.get("blockers") or [])


def test_a_run_without_prereg_trouble_gets_no_briefing(tmp_path):
    assert _briefing(State.new("experiment", tmp_path)) is None


def test_the_prereg_briefing_hook_is_wired_into_the_harness():
    # K11 起经 turn_one_briefing 合并注入（与其他开局提示拼成一条）。
    config = yaml.safe_load((_NODE / "harness.yaml").read_text(encoding="utf-8"))
    assert "turn_one_briefing" in config["loop_hooks"]
    assert ("prereg_binding_briefing", hooks._prereg_binding_briefing_on_turn_start) in (
        hooks._TURN_ONE_BRIEFING_PARTS)


# ── K3 不再要求模型手动声明 envelope；不改判 scope；探索性科学写法 ───────────


def test_the_rules_no_longer_ask_for_a_manual_envelope_declaration():
    rules = _rules()
    assert "declare_execution_envelope(assurance_class" not in rules
    assert "改判为 operation" in rules
    assert "探索性、无 prereg" in rules


def test_the_scientific_results_skill_describes_an_exploratory_run():
    skill = (_NODE / "skills" / "scientific-results" / "SKILL.md").read_text(encoding="utf-8")
    assert "exploratory, no prereg" in skill


# ── K4 safe_execute_python 描述与现状一致 ─────────────────────────────────────


def test_safe_execute_python_description_matches_the_isolation_backend_and_submit_job():
    description = get_tool("safe_execute_python").description
    assert "Docker" not in description
    assert "submit_job" in description
    assert "构建和正式计算使用 safe_run_bash" not in description


# ── K6 只读命令识别小幅补全 ───────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "tar -tf a.tar", "tar -tvzf a.tar.gz", "tar --list --file=a.tar", "tar tvf a.tar",
    "od -c data.bin", "hexdump -C data.bin", "cut -d, -f1 table.csv", "type ls",
])
def test_newly_recognised_inspection_commands_are_read_only(cmd):
    assert safe_bash._is_read_only_shell_command(cmd, None) is True, cmd


@pytest.mark.parametrize("cmd", [
    "tar -xf a.tar", "tar -tf a.tar --to-command=sh", "tar -tf a.tar -I zstd",
    "tar -C /tmp -tf a.tar", "tar -tf host:/remote/a.tar",
    "pip install numpy", "pip list --index-url http://mirror.invalid/simple",
    "sort -o out.txt in.txt", "uniq in.txt out.txt", "xxd -r dump.hex out.bin",
    # 第三会话复审 P1（review_commits_0914c_third.md 第一节）：c7cc7a8c 把这三条判成只读，
    # 真实 bash / GNU tar 1.35 下都会执行任意命令。
    "compgen -C ./x.sh w",
    "compgen -F myfunc x",
    "TAR_OPTIONS='--checkpoint=1 --checkpoint-action=exec=./hook.sh' tar -tf a.tar",
    "compgen -c",
    "PIP_CONFIG_FILE=pip.conf pip list",
    "env TAR_OPTIONS=--to-command=sh tar -tf a.tar",
])
def test_commands_that_can_write_or_execute_stay_not_read_only(cmd):
    assert safe_bash._is_read_only_shell_command(cmd, None) is False, cmd


@pytest.mark.parametrize("args, expected", [
    (["list"], True), (["show", "numpy"], True), (["list", "--format=json"], True),
    (["install", "x"], False), (["list", "--outdated"], False), ([], False),
])
def test_pip_counts_as_read_only_only_for_plain_list_and_show(args, expected):
    assert safe_bash._pip_command_is_read_only(args) is expected


# ── K7 行为规则 ───────────────────────────────────────────────────────────────


def test_the_rules_cover_the_three_behaviours_seen_in_live_runs():
    """并进意思最近的现有规则，不新增规则行：每条规则每个 run 都要付一遍 prompt 成本
    （test_data_source_reachability 钉住 16 条）。"""
    rules = _rules()
    assert "被拒的调用不原样重发" in rules
    assert "先冻结三件套、再修记账" in rules
    assert "只写工具返回的实测值" in rules
