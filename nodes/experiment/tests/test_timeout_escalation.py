"""超时升级阶梯（nodes/experiment/tools/timeout_escalation.py）。

回归目标是 2026-07-31 e2e-jicq221-poiseuille 的实证失败：同一份源码被
6 次同步下载尝试砍断，每次拿到的返回值完全一样、不带任何出路，agent
最终误判"环境不可行"。见模块 docstring。
"""
from __future__ import annotations

import asyncio
import time
import tempfile
from pathlib import Path

import pytest

from core import sandbox
from core.sandbox import availability
from nodes.experiment.tools import timeout_escalation as te


requires_sandbox = pytest.mark.skipif(
    not availability()[0], reason="mandatory Docker sandbox is unavailable")


class _FakeState:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hf-timeout-test-"))
        # RunAttempt identity must not be reused across fixtures whose frozen
        # mount capabilities differ.
        self.run_id = self.root.name
        self.node_type = "experiment"
        self.project_worktree = None
        self.workspace_root = None
        self.execution_root = self.root / "payload"
        self.execution_root.mkdir()
        self.hook_state: dict = {
            "path_roles": {
                "approved_write_root": {
                    "path": str(self.execution_root), "writable": True,
                },
            },
        }
        self.transcript: list = []
        self.kill_event = None

    def append_transcript(self, event: str, **kw) -> None:
        self.transcript.append((event, kw))


@pytest.fixture
def runattempt_state():
    """Give one production test one RunAttempt lease and always release it."""
    from nodes.experiment.tools.subprocess_policy import (
        register_approved_subprocess_write_root,
    )

    state = _FakeState()
    register_approved_subprocess_write_root(state, str(state.execution_root))
    try:
        yield state
    finally:
        manifest = getattr(state, "sandbox_manifest", None)
        if isinstance(manifest, dict) and manifest.get("attempt_id"):
            attempt_id = str(manifest["attempt_id"])
            evicted = sandbox.evict_state_attempt(state)
            leaked = [
                row for row in sandbox.list_attempt_instances()
                if row.get("attempt_id") == attempt_id
            ]
            assert evicted or not leaked, (
                "production sandbox RunAttempt still exists after teardown: "
                + attempt_id
            )


# ── 目标签名：不同写法的同一件事必须归并 ──────────────────────────────

def test_same_host_different_tools_share_signature():
    """git clone / curl / wget 拉同一个 host = 同一个目标。

    按整条命令文本签名会让每种写法都算"第一次超时"，升级阶梯永远升不上去——
    这正是实证里 6 次尝试没触发任何升级的原因。
    """
    sigs = {
        te.target_signature("git clone --depth 1 https://github.com/A/B.git"),
        te.target_signature("curl -L -o b.zip https://github.com/A/B/archive/master.zip"),
        te.target_signature("wget https://github.com/A/B/archive/master.zip"),
    }
    assert len(sigs) == 1
    assert sigs.pop() == "host:github.com"


def test_different_hosts_do_not_share_signature():
    assert te.target_signature("curl https://github.com/x") != \
           te.target_signature("curl https://archive.ubuntu.com/x")


def test_non_url_commands_fall_back_to_normalized_text():
    a = te.target_signature("make   -j8    all")
    b = te.target_signature("make -j8 all")
    assert a == b == "cmd:make -j8 all"


# ── 后台化识别：硬拒的逃生口必须可靠 ─────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "nohup ./run.sh &",
    "setsid nohup curl -o x https://github.com/a/b &",
    "./solver > log 2>&1 &",
    "cmd & disown",
    "sbatch job.slurm",
    "salloc -N 1",
])
def test_backgrounded_commands_recognised(cmd):
    assert te.looks_backgrounded(cmd) is True


@pytest.mark.parametrize("cmd", [
    "curl -L -o x.zip https://github.com/a/b",
    "git clone https://github.com/a/b.git",
    "echo 'run in background' # 只是提到而已",
])
def test_foreground_commands_not_mistaken_for_background(cmd):
    assert te.looks_backgrounded(cmd) is False


# ── 返回值：残留输出不再被丢弃 ───────────────────────────────────────

def test_payload_carries_partial_output():
    """执行器停止进程后收回的输出不能在调用点被丢掉。"""
    st = _FakeState()
    payload = te.build_timeout_payload(
        st, tool="run_bash", cmd="curl https://github.com/a/b",
        timeout_s=300, stdout=b"Receiving objects:  68%", stderr=b"")
    assert payload["status"] == "timeout"
    assert "68%" in payload["partial_stdout"]
    assert payload["made_output_before_kill"] is True
    assert payload["timeout_count_for_target"] == 1


def test_payload_marks_absence_of_output():
    st = _FakeState()
    payload = te.build_timeout_payload(
        st, tool="run_bash", cmd="sleep 999", timeout_s=10)
    assert payload["made_output_before_kill"] is False


def test_first_timeout_forbids_a_larger_foreground_timeout():
    st = _FakeState()
    payload = te.build_timeout_payload(
        st, tool="run_bash", cmd="curl https://github.com/a/b", timeout_s=300)
    nxt = payload["next_steps"]
    assert "timeout=" not in nxt
    assert "submit_job" in nxt
    assert "request_human_input" in nxt
    assert "不允许以更大的 timeout" in nxt


def test_first_timeout_offers_no_foreground_timeout_escalation():
    """After timeout, durable submit_job replaces a longer foreground wait."""
    st = _FakeState()
    payload = te.build_timeout_payload(
        st, tool="run_bash", cmd="curl https://github.com/a/b", timeout_s=300)
    assert "timeout=1800" not in payload["next_steps"]
    assert "submit_job" in payload["next_steps"]


def test_ladder_narrows_options_as_count_grows():
    st = _FakeState()
    cmd = "curl https://github.com/a/b"
    first = te.build_timeout_payload(st, tool="run_bash", cmd=cmd, timeout_s=300)
    second = te.build_timeout_payload(st, tool="run_bash", cmd=cmd, timeout_s=300)
    assert first["timeout_count_for_target"] == 1
    assert second["timeout_count_for_target"] == 2
    assert "timeout=" not in first["next_steps"]
    assert "timeout=" not in second["next_steps"]
    assert "submit_job" in second["next_steps"]
    assert "request_human_input" in second["next_steps"]
    assert second["warning"]                     # 预告下一次会被硬拒


def test_timeout_is_recorded_in_transcript():
    st = _FakeState()
    te.build_timeout_payload(st, tool="run_bash", cmd="curl https://x.io/a",
                             timeout_s=60)
    assert [e for e, _ in st.transcript] == ["bash_timeout"]


# ── 硬熔断：首次超时后不再让它同步重跑 ────────────────────────────────

def test_sync_execution_blocked_after_first_timeout():
    st = _FakeState()
    cmd = "curl -L https://github.com/a/b"
    te.build_timeout_payload(st, tool="run_bash", cmd=cmd, timeout_s=300)
    blocked = te.check_sync_block(st, tool="run_bash", cmd=cmd)
    assert blocked is not None
    assert blocked["status"] == "error"
    assert blocked["prior_timeouts"] == 1


def test_block_applies_across_different_command_spellings():
    """换个工具重下同一个 host 不能绕过熔断 —— 实证里正是靠换写法绕了 6 次。"""
    st = _FakeState()
    for cmd in ("git clone https://github.com/a/b.git",
                "curl -L https://github.com/a/b/archive/master.zip",
                "wget https://github.com/a/b/archive/master.zip"):
        te.build_timeout_payload(st, tool="run_bash", cmd=cmd, timeout_s=300)
    assert te.check_sync_block(
        st, tool="run_bash",
        cmd="curl --retry 3 https://github.com/a/b/archive/master.zip") is not None


def test_backgrounded_retry_is_not_exempt_from_sync_block():
    """手工后台化不是 experiment 的恢复路径，不能绕开熔断。"""
    st = _FakeState()
    cmd = "curl -L https://github.com/a/b"
    for _ in range(5):
        te.build_timeout_payload(st, tool="run_bash", cmd=cmd, timeout_s=300)
    assert te.check_sync_block(st, tool="run_bash", cmd=cmd) is not None
    assert te.check_sync_block(
        st, tool="run_bash",
        cmd="setsid nohup curl -L https://github.com/a/b > /tmp/d.log 2>&1 &") is not None


def test_block_does_not_leak_to_unrelated_targets():
    st = _FakeState()
    for _ in range(5):
        te.build_timeout_payload(st, tool="run_bash",
                                 cmd="curl https://github.com/a/b", timeout_s=300)
    assert te.check_sync_block(
        st, tool="run_bash", cmd="curl https://archive.ubuntu.com/x") is None
    assert te.check_sync_block(st, tool="run_bash", cmd="make -j8") is None


def test_block_message_forbids_jumping_to_infeasible():
    """实证里的最终误判：把'同步跑不动'写成了'环境不可行'。"""
    st = _FakeState()
    cmd = "curl https://github.com/a/b"
    for _ in range(3):
        te.build_timeout_payload(st, tool="run_bash", cmd=cmd, timeout_s=300)
    blocked = te.check_sync_block(st, tool="run_bash", cmd=cmd)
    assert "infeasible" in blocked["error"]
    assert "request_human_input" in blocked["error"]


def test_declared_long_foreground_runs_and_is_witnessed(tmp_path, monkeypatch):
    """判决拆除·第三波（te:247 降格）：expected_duration_s>=600 不再拒绝——照跑，
    结果与 transcript 带 managed_submission_recommended；短任务不带。墙加回去
    （status=error / blocker）本测试即转红。"""
    import json as _json
    from core.state import State
    from nodes.experiment.tools import safe_bash as sb

    state = State.new("experiment", tmp_path)
    # 受管生命周期下，任何真实执行都要先有 scope；上游那版跑在裸 state 上。
    state.hook_state.setdefault("node_inputs", {
        "fixture": "timeout_escalation",
        "requested_work": "验证长任务见证取代长任务墙。",
    })
    from nodes.experiment.tools.run_contract import _classify_experiment_scope
    assert asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="environment_probe",
        reason="verify managed_submission_recommended witness"))["status"] == "success"
    calls: list[str] = []

    async def fake_exec(st, cmd, timeout=600, cwd=None, **kw):
        calls.append(cmd)
        return {"status": "success", "stdout_tail": "ok", "stderr_tail": ""}

    monkeypatch.setattr(sb, "_exec_and_log", fake_exec)
    # 非 major build 命令：不会被 build_env source 包装，calls 可逐字比对。
    long_run = asyncio.run(sb._safe_run_bash(state, "sleep 0", expected_duration_s=600))
    short = asyncio.run(sb._safe_run_bash(state, "sleep 0", expected_duration_s=30))

    assert calls == ["sleep 0", "sleep 0"]
    assert long_run["status"] == "success"
    witness = long_run["execution_witness"]["managed_submission_recommended"]
    assert witness["expected_duration_s"] == 600
    assert witness["threshold_s"] == te._SYNC_LONG_THRESHOLD_S
    assert "managed_submission_recommended" not in short.get("execution_witness", {})
    events = [_json.loads(line) for line in state.transcript_path.read_text().splitlines() if line.strip()]
    assert any(e.get("event") == "managed_submission_recommended" for e in events)
    assert not any(e.get("event") == "managed_submission_required" for e in events)


def test_missing_hook_state_degrades_without_raising():
    class _Bare:
        pass
    payload = te.build_timeout_payload(
        _Bare(), tool="run_bash", cmd="curl https://x.io/a", timeout_s=30)
    assert payload["status"] == "timeout"


# ── _exec_and_log 端到端：超时返回值带出路，且不留孤儿 ────────────────

@requires_sandbox
def test_exec_timeout_returns_ladder_not_bare_status(runattempt_state):
    """以前这里 return 的是 {"status","cmd","timeout_s"} 三个字段，没有别的。"""
    from nodes.experiment.tools.safe_bash import _exec_and_log

    async def run():
        return await _exec_and_log(
            runattempt_state, "sleep 30", timeout=1,
            cwd=str(runattempt_state.execution_root),
        )

    res = asyncio.run(run())
    assert res["status"] == "timeout"
    assert "next_steps" in res and "request_human_input" in res["next_steps"]
    assert res["timeout_count_for_target"] == 1


@requires_sandbox
def test_exec_timeout_surfaces_partial_output(runattempt_state):
    """超时前已经打印的进度必须回传 —— 这是判断"要不要接着等"的唯一依据。"""
    from nodes.experiment.tools.safe_bash import _exec_and_log

    async def run():
        return await _exec_and_log(
            runattempt_state,
            "echo 'Receiving objects:  68%'; sleep 30",
            timeout=1,
            cwd=str(runattempt_state.execution_root),
        )

    res = asyncio.run(run())
    assert res["status"] == "timeout"
    assert "68%" in res["partial_stdout"]
    assert res["made_output_before_kill"] is True


@requires_sandbox
def test_exec_timeout_kills_whole_process_tree(runattempt_state):
    """超时只 kill shell 会留下孤儿子进程继续跑。

    实证（2026-07-31）：框架版 run_bash 以 timeout=300 起 git clone，工具在
    300s 返回后 git 进程又活了 4 分钟、下满 14MB，然后被收掉时 git 自己删掉了
    半成品目录 —— 上层看到的是"超时且零进展"，白烧 9 分钟。当前实现由 RunAttempt 容器监督整棵 payload 进程树；timeout 必须 stop 容器，
    不能只终止入口 shell。
    """
    from nodes.experiment.tools.safe_bash import _exec_and_log

    state = runattempt_state
    escaped = state.root / "child-survived"
    cmd = ("python3 -c \"import time;from pathlib import Path;time.sleep(3);"
           f"Path(r'{escaped}').write_text('escaped')\" | cat")

    async def run():
        return await _exec_and_log(
            state, cmd, timeout=1, cwd=str(state.execution_root),
        )

    res = asyncio.run(run())
    assert res["status"] == "timeout"

    time.sleep(3)
    assert not escaped.exists()


@requires_sandbox
def test_direct_shell_completion_kills_background_child(runattempt_state):
    """直接 payload 退出即完成；后台后代不能持有管道或逃出容器。"""
    from nodes.experiment.tools.safe_bash import _exec_and_log

    state = runattempt_state
    escaped = state.root / "background-survived"
    cmd = ("python3 -c \"import time;from pathlib import Path;time.sleep(2);"
           f"Path(r'{escaped}').write_text('escaped')\" & echo started")
    # First materialization includes Docker provisioning, which is not part of
    # the shell-completion property under test. Reuse the same RunAttempt so
    # the wall-clock assertion measures payload completion only.
    warmup = asyncio.run(_exec_and_log(
        state, "true", timeout=5, cwd=str(state.execution_root),
    ))
    assert warmup["status"] == "success"
    started = time.monotonic()
    res = asyncio.run(_exec_and_log(
        state, cmd, timeout=5, cwd=str(state.execution_root),
    ))
    assert res["status"] == "success"
    assert time.monotonic() - started < 5
    time.sleep(2.5)
    assert not escaped.exists()



def test_scheduler_probe_mentions_do_not_trigger_managed_job_guards():
    from nodes.experiment.tools import timeout_escalation as escalation

    probes = (
        "command -v sbatch",
        "which srun",
        "grep -iE \"sbatch|srun\" /etc/slurm/slurm.conf",
        "srun --version",
        "sbatch --help",
    )
    for command in probes:
        assert not escalation.looks_backgrounded(command), command


def test_scheduler_command_heads_and_shell_wrappers_are_detected():
    from nodes.experiment.tools import timeout_escalation as escalation

    assert escalation.looks_backgrounded("env SLURM_HINT=nomultithread sbatch job.sh")
    assert escalation.looks_backgrounded("command sbatch job.sh")
    assert escalation.looks_backgrounded("bash -c \"sbatch job.sh\"")
    # Command substitutions can execute scheduler words outside a simple
    # command head, so they deliberately use conservative fallback.
    assert escalation.looks_backgrounded("echo " + chr(36) + "(sbatch job.sh)")
@pytest.mark.parametrize(("command", "blocker_kind"), (
    ("$runner --version", "unverifiable_dynamic_execution"),
    ("if then", "bash_parse_error"),
))
def test_safe_run_bash_reports_unknown_execution_without_mislabeling_it(
    command, blocker_kind,
):
    from nodes.experiment.tools.safe_bash import _safe_run_bash

    result = asyncio.run(_safe_run_bash(_FakeState(), command))

    assert result["status"] == "error"
    assert result["blocker"]["kind"] == blocker_kind
    assert "这不表示检测到了后台任务或 srun" in result["error"]


def test_and_list_is_not_mistaken_for_a_background_launch():
    assert te.looks_backgrounded("cd /tmp && touch marker && echo OK") is False



def test_tree_sitter_keeps_scheduler_words_as_loop_data_and_parses_nested_execution():
    from nodes.experiment.tools import timeout_escalation as escalation

    probe = "for c in gcc gfortran srun sbatch; do command -v $c; done"
    assert not escalation.looks_backgrounded(probe)
    assert not escalation.classify_bash_execution(probe).known_srun_launch
    assert escalation.classify_bash_execution("bash -c \"srun -n 2 hostname\"").known_srun_launch


def test_tree_sitter_redirects_and_dynamic_shell_are_not_keyword_fallbacks():
    from nodes.experiment.tools import timeout_escalation as escalation

    assert not escalation.looks_backgrounded("echo probe > /dev/null 2>&1")
    assert not escalation.classify_bash_execution("echo probe > /dev/null 2>&1").known_srun_launch
    dynamic_shell = "bash -c " + chr(34) + chr(36) + "runner" + chr(34)
    decision = escalation.classify_bash_execution(dynamic_shell)
    assert not decision.known_srun_launch
    assert decision.unverifiable_execution
    assert not escalation.classify_bash_execution(dynamic_shell).known_srun_launch
    assert escalation.looks_backgrounded("nohup sleep 1")


@pytest.mark.parametrize("command", (
    "find /tmp -name sbatch",
    "find . -type f -name '*qsub*' -print",
    "printf '%s\\n' a b | xargs -n1 echo",
    "eval 'echo harmless'",
    "submit() { sbatch job.sh; }",
    "function launch { qsub job.pbs; }",
    "f() { srun hostname; }",
))
def test_static_non_execution_contexts_do_not_trigger_scheduler_guards(command):
    """Only executable command positions count, not inert definitions/data."""
    assert not te.looks_backgrounded(command), command
    assert not te.classify_bash_execution(command).known_srun_launch, command


@pytest.mark.parametrize("command", (
    "bash -c 'sbatch job.sh'",
    "bash -lc 'qsub job.pbs'",
    "sh -c 'bsub < job.lsf'",
    "command bash -c 'sbatch job.sh'",
    "env bash -lc 'qsub job.pbs'",
))
def test_single_quoted_static_shell_payloads_are_analyzed(command):
    assert te.looks_backgrounded(command), command


@pytest.mark.parametrize("command", (
    "bash -c 'srun hostname'",
    "command sh -c 'srun -n 2 hostname'",
))
def test_single_quoted_static_shell_payloads_detect_srun(command):
    assert te.classify_bash_execution(command).known_srun_launch, command


@pytest.mark.parametrize("command", (
    "bash job.sh",
    "sh ./job.sh",
    "printf 'sbatch job.sh\\n' | bash",
    "bash < job.sh",
    "sh -s < job.sh",
    "bash <<'EOF'\nsbatch job.sh\nEOF",
    "zsh ./submit.zsh",
))
def test_unread_shell_sources_fail_closed_as_dynamic_execution(command):
    from nodes.experiment.tools import bash_semantics as semantics

    analysis = semantics.analyze_bash(command)
    decision = te.classify_bash_execution(command)
    assert analysis.dynamic_execution, command
    assert decision.unverifiable_execution, command
    assert decision.uncertainty_kind == "dynamic_execution", command
    assert not te.looks_backgrounded(command), command
    assert not te.classify_bash_execution(command).known_srun_launch, command


@pytest.mark.parametrize("command", (
    "bash --version",
    "bash --help",
    "bash -n job.sh",
    "sh -n ./job.sh",
))
def test_non_executing_shell_modes_remain_safe(command):
    assert not te.looks_backgrounded(command), command
    assert not te.classify_bash_execution(command).known_srun_launch, command


def test_function_body_is_analyzed_only_when_the_function_is_called():
    assert not te.looks_backgrounded("submit() { sbatch job.sh; }")
    assert te.looks_backgrounded("submit() { sbatch job.sh; }; submit")
    assert not te.classify_bash_execution("launch() { srun hostname; }").known_srun_launch
    assert te.classify_bash_execution("launch() { srun hostname; }; launch").known_srun_launch
    assert te.looks_backgrounded("eval 'later() { sbatch job.sh; }'; later")


def test_command_specific_dynamic_execution_is_precise():
    assert not te.looks_backgrounded("find . -name '*.sh' -print")
    assert te.looks_backgrounded("find . -name '*.sh' -exec sbatch {} ';'")
    assert not te.looks_backgrounded("printf x | xargs echo")
    assert not te.looks_backgrounded("printf x | xargs -I{} echo {}")
    decision = te.classify_bash_execution("printf sbatch | xargs -I{} {}")
    assert not decision.known_srun_launch
    assert decision.unverifiable_execution
    assert not decision.known_background_launch
    assert not decision.known_srun_launch
    assert te.classify_bash_execution("printf x | xargs srun hostname").known_srun_launch
    assert not te.looks_backgrounded("eval 'echo ok'")
    assert te.looks_backgrounded("eval 'sbatch job.sh'")
    assert te.looks_backgrounded("f() { sbatch job.sh; }; eval 'f'")


def test_coproc_is_a_detached_launch():
    assert te.looks_backgrounded("coproc sleep 1")
    assert te.looks_backgrounded("coproc WORKER { sleep 1; }")



def test_missing_tree_sitter_is_reported_as_analyzer_unavailable(monkeypatch):
    from nodes.experiment.tools import bash_semantics as semantics

    monkeypatch.setattr(semantics, "_PARSER", None)
    monkeypatch.setattr(semantics, "_ANALYZER_UNAVAILABLE", "ImportError: tree_sitter")
    analysis = semantics.analyze_bash("echo harmless")

    assert analysis.analyzer_unavailable == "ImportError: tree_sitter"
    assert analysis.unsafe_dynamic


@pytest.mark.parametrize("command", (
    "make -i",
    "gmake -ki",
    "env MAKEFLAGS=--ignore-errors make",
))
def test_ignore_errors_build_is_rejected_by_both_static_validity_gates(command):
    from nodes.experiment.tools.resource_manager import (
        _submission_static_validity_block,
    )
    from nodes.experiment.tools.safe_bash import _bash_static_validity_block

    safe_state = _FakeState()
    safe_block = _bash_static_validity_block(
        safe_state,
        command,
        expected_duration_s=None,
        check_after_seconds=None,
    )
    submit_state = _FakeState()
    submit_block = _submission_static_validity_block(submit_state, command)

    assert safe_block["status"] == "error"
    assert safe_block["blocker"]["uncertainty_kind"] == "ignore_errors_build"
    assert submit_block["status"] == "error"
    assert submit_block["blocker"]["uncertainty_kind"] == "ignore_errors_build"


@pytest.mark.parametrize("command", (
    "./compile em_real -j 2",
    "make -k all",
    "echo 'make -i'",
))
def test_normal_build_inputs_pass_ignore_errors_validity_check(command):
    from nodes.experiment.tools.resource_manager import (
        _submission_static_validity_block,
    )
    from nodes.experiment.tools.safe_bash import _bash_static_validity_block

    state = _FakeState()
    assert _bash_static_validity_block(
        state,
        command,
        expected_duration_s=None,
        check_after_seconds=None,
    ) is None
    assert _submission_static_validity_block(state, command) is None


@pytest.mark.parametrize("command", (
    "ssh login-node hostname",
    "ssh -f login-node ./solver",
    "pdsh -w node01,node02 hostname",
    "kubectl exec pod/solver -- ./solver",
    "kubectl proxy",
))
def test_external_control_public_entry_blocks_before_spawn(command, monkeypatch):
    from nodes.experiment.tools.safe_bash import _safe_run_bash

    spawned = []

    async def no_spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        raise AssertionError("external control must be rejected before spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    result = asyncio.run(_safe_run_bash(_FakeState(), command))

    assert result["status"] == "error"
    assert result["blocker"]["kind"] == "unmanaged_background_launch"
    assert spawned == []


@pytest.mark.parametrize("command", (
    "ssh login-node hostname",
    "ssh -f login-node ./solver",
    "pdsh -w node01,node02 hostname",
))
def test_remote_shell_is_rejected_inside_submit_payload_too(command):
    from nodes.experiment.tools.resource_manager import (
        _submission_static_validity_block,
    )

    block = _submission_static_validity_block(_FakeState(), command)

    assert block["status"] == "error"
    assert block["blocker"]["kind"] == "unmanaged_background_launch"
