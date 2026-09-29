"""本节点开局提示合并成一条（收敛任务书 K11 第 2 条）。

第 1 轮本节点四个 hook（路径角色契约、任务 KB briefing、prereg 绑定失败、数据源可达性预检）
各注入一条 system 消息。合并后只注入一条，判定与副作用不变。共享层 hook 不动。
K11 第 1 条（确认卡批准后的「逐字重调」提示）在 core/loop_hooks_builtin.py，节点侧不改。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from nodes.experiment import hooks
from test_workdir_convention import _ctx

_NODE = Path(__file__).resolve().parents[1]
_PARTS = ("path_role_convention", "task_briefing", "prereg_binding_briefing",
          "data_source_reachability_preflight")


def _with_missing_prereg(project_id: str):
    ctx = _ctx(project_id=project_id)
    ctx.state.hook_state["node_inputs"] = {"prereg_artifact_id": "pre_registration__missing"}
    return ctx


def test_two_briefings_on_turn_one_arrive_as_one_message_with_the_same_effects(tmp_path):
    separate_ctx = _with_missing_prereg("proj-k11-separate")
    separate = [message.content for _name, part in hooks._TURN_ONE_BRIEFING_PARTS
                for message in (part(separate_ctx) or [])]
    merged_ctx = _with_missing_prereg("proj-k11-merged")

    merged = hooks._turn_one_briefing_on_turn_start(merged_ctx)

    assert len(separate) == 2, separate
    assert merged is not None and len(merged) == 1
    assert merged[0].role == "system"
    assert "路径角色契约" in merged[0].content and "预注册绑定失败" in merged[0].content
    assert merged[0].content.index("路径角色契约") < merged[0].content.index("预注册绑定失败")
    assert any("prereg_dispatch" in str(item.get("blocker_id"))
               for item in merged_ctx.state.hook_state.get("blockers") or [])
    assert merged_ctx.state.hook_state.get("path_roles", {}).get("run_root")


def test_later_turns_still_run_the_path_role_part_without_injecting_again(tmp_path):
    ctx = _with_missing_prereg("proj-k11-turn2")
    hooks._turn_one_briefing_on_turn_start(ctx)
    ctx.state.hook_state["path_roles"].pop("run_root")
    ctx.turn = 2

    assert hooks._turn_one_briefing_on_turn_start(ctx) is None
    assert ctx.state.hook_state["path_roles"].get("run_root")   # 每轮补写照旧


def test_a_failing_part_does_not_swallow_the_others(tmp_path, monkeypatch):
    def broken(_ctx):
        raise RuntimeError("boom")

    parts = tuple((name, broken if name == "path_role_convention" else part)
                  for name, part in hooks._TURN_ONE_BRIEFING_PARTS)
    monkeypatch.setattr(hooks, "_TURN_ONE_BRIEFING_PARTS", parts)
    ctx = _with_missing_prereg("proj-k11-broken")

    merged = hooks._turn_one_briefing_on_turn_start(ctx)

    assert merged and "预注册绑定失败" in merged[0].content
    assert "turn_one_briefing_part_failed" in ctx.state.transcript_path.read_text(encoding="utf-8")


def test_the_harness_lists_the_merged_hook_in_place_of_the_four(tmp_path):
    config = yaml.safe_load((_NODE / "harness.yaml").read_text(encoding="utf-8"))
    loop_hooks = config["loop_hooks"]

    assert [name for name, _part in hooks._TURN_ONE_BRIEFING_PARTS] == list(_PARTS)
    assert not set(_PARTS).intersection(loop_hooks)
    # 仍排在 blocker 提醒之前：prereg 派发阻塞在同一轮先登记，提醒才看得到。
    assert loop_hooks.index("turn_one_briefing") < loop_hooks.index("foreign_owner_blocker_ask_nudge")
    assert set(hooks.turn_one_briefing.emits) >= {
        "path_role_convention_injected", "task_briefing_injected",
        "prereg_binding_briefing_injected", "data_source_reachability_preflight",
        "turn_one_briefing_part_failed", "experiment_task_prose_input_receipt"}
