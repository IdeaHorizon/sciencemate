"""049-1: scheduler terminal-state facts are real assets, machine-checkable, routed, and diagnosable.

Four things are pinned here, all red on 48af4fa0 (nothing below existed):

1. the two references files follow the §2 hard format (facts only, every fact
   with an official source + version/date + a "when it lies" line, no steps,
   no site-specific names) and cover every 046 probe case and every terminal
   state the node code knows;
2. they are reachable through the skill asset channel (`load_skill(asset=…)`),
   not through the project read boundary;
3. `experiment_scheduler_facts_router` names the right file once per
   (scheduler, job_id) when a managed job reaches ``terminal``;
4. the scheduler kill lines that Slurm / PBS / Torque write into a job's
   stderr are diagnosed by the runtime failure detector with the configured
   guidance, and the generic ``error:`` interpretation does not survive next
   to them.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.bootstrap import bootstrap
from core.loop_hooks import HookContext
from core.skill_registry import get_skill
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools import resource_manager as manager
from shared.tools.library.skill_tools import _load_skill

SKILL_DIR = Path(hooks.__file__).resolve().parent / "skills" / "scheduler-longrun"
REFERENCES = {
    "slurm": SKILL_DIR / "references" / "slurm-terminal-facts.md",
    "pbs": SKILL_DIR / "references" / "pbs-torque-terminal-facts.md",
}
FACT_HEADING = re.compile(r"^### ([SPT]\d{2}) ", re.M)
# 步骤与站点名：references 只能有事实与判据（049 提案 §2 第 1、4 条）。
# 围栏只允许 ```text（原样贴的调度器输出样本）；```bash / ```sh / 裸围栏都是"步骤"的形状。
FORBIDDEN_LINE = re.compile(
    r"^\s*(?:```(?!text\s*$)|\$ |运行|执行|先运行|请运行|Run |Execute )", re.M)
FORBIDDEN_TOKEN = re.compile(
    r"/mnt/|/home/|beegfs|spr-cu|module load|--partition|-p debug|-p main\b")


def _facts(text: str) -> dict[str, str]:
    heads = list(FACT_HEADING.finditer(text))
    out: dict[str, str] = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        out[head.group(1)] = text[head.start():end]
    return out


# ── 1. 硬格式 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("scheduler", sorted(REFERENCES))
def test_reference_follows_the_facts_only_hard_format(scheduler: str):
    text = REFERENCES[scheduler].read_text(encoding="utf-8")
    assert "<!-- facts-format: v1 -->" in text
    # 谁叫模型读它：必须点名真实存在的 hook，不是泛泛的"运行时会提示"。
    assert hooks.experiment_scheduler_facts_router.name in text
    assert "未在真实集群上验证" in text          # 真集群跑过之前不许写"已验"

    facts = _facts(text)
    assert len(facts) >= 15, sorted(facts)
    for fact_id, block in facts.items():
        assert re.search(r"^- 事实：\S", block, re.M), fact_id
        source = re.search(r"^- 来源：(.+)$", block, re.M)
        assert source is not None, fact_id
        assert re.search(r"https?://", source.group(1)), fact_id
        # 版本/日期：Slurm/Torque 记抓取日，PBS Pro 记文档 Updated 日期
        assert re.search(r"抓取 \d{4}-\d{2}-\d{2}|Updated \d", source.group(1)), fact_id
        assert re.search(r"^- 会说谎：\S", block, re.M), fact_id
    # ```text 样本块整块摘掉后再查：剩下的任何围栏都是步骤的形状。
    prose = re.sub(r"```text\n.*?\n```", "", text, flags=re.S)
    assert FORBIDDEN_LINE.search(prose) is None, FORBIDDEN_LINE.search(prose)
    assert FORBIDDEN_TOKEN.search(text) is None, FORBIDDEN_TOKEN.search(text)


def test_every_table_reference_points_at_an_existing_fact():
    for path in REFERENCES.values():
        text = path.read_text(encoding="utf-8")
        facts = _facts(text)
        table_refs = set(re.findall(r"\b([SPT]\d{2})\b", text.split("## 2.", 1)[1]))
        missing = sorted(ref for ref in table_refs if ref not in facts)
        assert not missing, (path.name, missing)


# 046 的探针用例（tests/test_cluster_job_state_truth.py）：每个 State 都要在
# Slurm references 的用例表里对上至少一条事实。
_046_STATES = [
    "TIMEOUT", "CANCELLED by 1000", "NODE_FAIL", "PREEMPTED", "DEADLINE",
    "BOOT_FAIL", "REVOKED", "SPECIAL_EXIT", "OUT_OF_MEMORY", "COMPLETED", "FAILED",
]


def test_slurm_reference_covers_every_046_probe_case_with_a_fact():
    text = REFERENCES["slurm"].read_text(encoding="utf-8")
    facts = _facts(text)
    table = text.split("## 3.", 1)[1].split("## 4.", 1)[0]
    rows = [line for line in table.splitlines() if line.startswith("| ") and "|---" not in line]
    covered: dict[str, list[str]] = {}
    for row in rows[1:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) < 3:
            continue
        covered[cells[0]] = re.findall(r"S\d{2}", cells[2])
    for state in _046_STATES:
        assert state in covered, (state, sorted(covered))
        assert covered[state] and all(ref in facts for ref in covered[state]), (state, covered[state])


def test_slurm_reference_documents_every_terminal_state_the_node_code_knows():
    text = REFERENCES["slurm"].read_text(encoding="utf-8")
    for state in sorted(manager._SLURM_TERMINAL_STATES):
        assert state in text, state
    # 节点侧对照表必须点名产生端的符号，不许只抄文档。
    for symbol in ("_SLURM_TERMINAL_STATES", "_slurm_state_name", "_slurm_exit_status"):
        assert symbol in text, symbol


# ── 2. 通过 skill asset 通道可达 ─────────────────────────────────────────────

def test_references_are_skill_assets_loadable_by_load_skill(tmp_path):
    bootstrap(force=True)
    skill = get_skill("scheduler-longrun")
    assert skill is not None
    state = State.new("experiment", tmp_path)
    for asset in hooks._SCHEDULER_FACTS_ASSETS.values():
        assert asset in skill.assets, (asset, skill.assets)
        loaded = asyncio.run(_load_skill(state, "scheduler-longrun", asset=asset))
        assert loaded["status"] == "success", loaded
        assert "facts-format: v1" in loaded["content"]
        assert "会说谎" in loaded["content"]


# ── 3. 终态时点名一次 ────────────────────────────────────────────────────────

def _ctx(state, turn: int, records: list[dict]) -> HookContext:
    return SimpleNamespace(state=state, turn=turn, tool_call_records=records, messages=[])


def _health_record(scheduler: str, job_id: str, phase: str, *, nested: bool = False) -> dict:
    health = {"status": "success", "scheduler": scheduler, "job_id": job_id,
              "scheduler_phase": phase, "health_state": "terminal_needs_analysis"}
    if nested:
        return {"name": "wait_for_external_job",
                "args": {"scheduler": scheduler, "job_id": job_id},
                "result": {"status": "success", "wait_outcome": "terminal", "health": health}}
    return {"name": "check_external_job_health",
            "args": {"scheduler": scheduler, "job_id": job_id}, "result": health}


def test_the_facts_router_is_enabled_for_the_experiment_node():
    """hook 注册了还不够：harness.yaml 的 loop_hooks 不列它就永远不跑。"""
    from core.loader import load_harness
    bootstrap(force=True)
    harness = load_harness("experiment")
    enabled = [hook.name if hasattr(hook, "name") else str(hook) for hook in harness.loop_hooks]
    assert hooks.experiment_scheduler_facts_router.name in enabled, enabled
    assert "experiment_skill_routed" in hooks.experiment_scheduler_facts_router.emits


def test_terminal_slurm_job_is_pointed_at_the_slurm_facts_once(tmp_path):
    state = State.new("experiment", tmp_path)
    record = _health_record("slurm", "31415", "terminal")

    messages = hooks._experiment_scheduler_facts_router_on_turn_end(_ctx(state, 3, [record]))

    assert messages is not None
    content = messages[0].content
    assert "load_skill(name='scheduler-longrun', asset='references/slurm-terminal-facts.md')" in content
    assert "31415" in content
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert '"event": "experiment_skill_routed"' in transcript
    assert '"scope": "scheduler_terminal"' in transcript
    assert '"asset": "references/slurm-terminal-facts.md"' in transcript
    # 同一作业再次到终态（重复 health / finalize）不再点名；另一个作业照点。
    assert hooks._experiment_scheduler_facts_router_on_turn_end(_ctx(state, 4, [record])) is None
    other = hooks._experiment_scheduler_facts_router_on_turn_end(
        _ctx(state, 5, [_health_record("slurm", "27182", "terminal")]))
    assert other is not None and "27182" in other[0].content


def test_nested_health_of_a_pbs_job_points_at_the_pbs_torque_facts(tmp_path):
    state = State.new("experiment", tmp_path)
    messages = hooks._experiment_scheduler_facts_router_on_turn_end(
        _ctx(state, 2, [_health_record("pbs", "41.server", "terminal", nested=True)]))
    assert messages is not None
    assert "asset='references/pbs-torque-terminal-facts.md'" in messages[0].content


@pytest.mark.parametrize(
    ("scheduler", "phase"),
    [("slurm", "running"), ("slurm", "unknown"), ("local", "terminal"), ("kubernetes", "terminal")],
)
def test_running_jobs_and_schedulers_without_a_reference_get_no_pointer(tmp_path, scheduler, phase):
    state = State.new("experiment", tmp_path)
    assert hooks._experiment_scheduler_facts_router_on_turn_end(
        _ctx(state, 2, [_health_record(scheduler, "7", phase)])) is None
    # 什么都没写：transcript 还不存在，或者存在但没有这条事件。
    assert (not state.transcript_path.exists()
            or "experiment_skill_routed" not in state.transcript_path.read_text(encoding="utf-8"))


# ── 4. 调度器写进 stderr 的 kill 行走运行时诊断 ─────────────────────────────

class _State:
    def __init__(self, root):
        self.root = root


def _diagnose(tmp_path, text: str) -> str:
    (tmp_path / "slurm-7.err").write_text(text, encoding="utf-8")
    messages = hooks.generic_failure_detector_on_turn_start(
        HookContext(harness=None, state=_State(tmp_path), messages=[], turn=1))
    assert messages is not None, text
    return messages[0]["content"]


# 逐字形状来自源码（src/slurmd/slurmstepd/req.c、task_cgroup_memory.c、
# jobacct_gather.c；OpenPBS/Torque src/resmom/mom_main.c），前缀按版本不同：
# ≤24.11 "slurmstepd: error: "，≥25.05 "[2026-09-21T10:00:00.000] error: "。
_KILL_LINES = [
    ("slurmstepd: error: *** JOB 7 ON node1 CANCELLED AT 2026-09-21T10:00:00 DUE TO TIME LIMIT ***",
     "[resource]", "墙钟到点被杀"),
    ("[2026-09-21T10:00:00.123] error: *** STEP 7.0 ON node1 CANCELLED AT 2026-09-21T10:00:00 DUE TO TIME LIMIT ***",
     "[resource]", "墙钟到点被杀"),
    ("slurmstepd: error: *** JOB 7 ON node1 CANCELLED AT 2026-09-21T10:00:00 DUE TO PREEMPTION ***",
     "[resource]", "sacct -D"),
    ("slurmstepd: error: *** JOB 7 ON node1 CANCELLED AT 2026-09-21T10:00:00 DUE TO NODE FAILURE, SEE SLURMCTLD LOG FOR DETAILS ***",
     "[resource]", "没有返回过退出码"),
    ("slurmstepd: error: *** JOB 7 ON node1 CANCELLED AT 2026-09-21T10:00:00 ***",
     "[runtime]", "CANCELLED by <uid>"),
    ("[2026-09-21T10:00:00.123] error: *** JOB 7 ON node1 CANCELLED AT 2026-09-21T10:00:00 DUE to SIGNAL Terminated ***",
     "[runtime]", "CANCELLED by <uid>"),
    ("slurmstepd: error: *** JOB 7 ON node1 FAILED (non-zero exit code or other failure mode) ***",
     "[runtime]", "不是根因"),
    ("slurmstepd: error: Detected 1 oom-kill event(s) in StepId=7.batch. Some of your processes may have been killed by the cgroup out-of-memory handler.",
     "[memory]", "MaxRSS"),
    ("[2026-09-21T10:00:00.123] error: Detected 2 oom_kill events in StepId=7.0. Some of the step tasks have been OOM Killed.",
     "[memory]", "MaxRSS"),
    ("slurmstepd: error: StepId=7.batch exceeded memory limit (734003200 > 52428800), being killed",
     "[memory]", "MaxRSS"),
    # 下面三行是 Docker 单机 Slurm 23.11.4（MULTIPLE_SLURMD 构建，前缀 slurmstepd-<node>:）
    # 真实写进 *.err 的原文（references §4）。
    ("slurmstepd-lulu: error: *** JOB 1 ON lulu CANCELLED AT 2026-09-21T08:03:22 DUE TO TIME LIMIT ***",
     "[resource]", "墙钟到点被杀"),
    ("slurmstepd-lulu: error: *** JOB 9 ON lulu CANCELLED AT 2026-09-21T08:12:01 DUE TO NODE FAILURE, SEE SLURMCTLD LOG FOR DETAILS ***",
     "[resource]", "没有返回过退出码"),
    ("slurmstepd-lulu: error: Exceeded job memory limit", "[memory]", "MaxRSS"),
    ("=>> PBS: job killed: walltime 3650 exceeded limit 3600", "[resource]", "时间/CPU 限制到点被杀"),
    ("=>> PBS: job killed: mem 2097152kb exceeded limit 1048576kb", "[memory]", "重新申请 mem/vmem"),
    ("=>> PBS: job killed: cput job total 7300 secs exceeded limit 7200", "[resource]", "时间/CPU 限制到点被杀"),
]


@pytest.mark.parametrize(("line", "category", "fix_fragment"), _KILL_LINES)
def test_scheduler_kill_line_reaches_the_runtime_hook_with_its_guidance(tmp_path, line, category, fix_fragment):
    message = _diagnose(tmp_path, line + "\n")
    assert category in message, message
    assert fix_fragment in message, message
    # 同一行上的通用 "error:" 解释被机械覆盖：模型不会先看到「通用编译错误」。
    assert "[compilation]" not in message, message
    assert "检查编译器输出" not in message, message


def test_time_limit_banner_is_not_read_as_a_plain_cancel():
    from nodes.experiment.tools.diagnose import DiagnoseEngine
    engine = DiagnoseEngine.from_yaml_patterns()
    report = engine.analyze_output(
        "slurmstepd: error: *** JOB 7 ON node1 CANCELLED AT 2026-09-21T10:00:00 DUE TO TIME LIMIT ***\n")
    fixes = {finding["generic_fix"] for finding in report["findings"]}
    assert any("墙钟到点被杀" in fix for fix in fixes), report
    assert not any("CANCELLED by <uid>" in fix for fix in fixes), report


def test_a_normal_program_line_mentioning_cancelled_is_not_a_scheduler_kill(tmp_path):
    from nodes.experiment.tools.diagnose import DiagnoseEngine
    engine = DiagnoseEngine.from_yaml_patterns()
    report = engine.analyze_output("run cancelled at step 3 due to time limit of solver\n")
    assert not any(
        "墙钟到点被杀" in finding["generic_fix"] for finding in report["findings"]), report
