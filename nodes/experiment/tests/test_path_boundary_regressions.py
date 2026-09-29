"""路径边界的跨模块回归 —— 2026-08-18 沙箱/路径角色整改锁定的不变量。

单开一个文件是因为这些断言横跨 hooks（角色声明）、safe_bash（写分类与沙箱）、
resource_manager（调度器脚本与本机边界）三处：挂进其中任何一个已有测试文件都
会让"改了 A 忘了 B"这类分叉继续躲在别人的文件里。每条测试对应一个**实测发生过**
的失效形态，注释里写清那是什么，别只留断言。
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from core import sandbox
from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools import resource_manager as rm
from nodes.experiment.tools import safe_bash as sb
from nodes.experiment.tools import subprocess_policy as policy
from nodes.experiment.tools.path_roles import collect_path_roles, validate_path_roles
from nodes.experiment.tools.resource_manager import _submit_job
from nodes.experiment.tools.run_contract import _classify_experiment_scope


requires_sandbox = pytest.mark.skipif(
    not sandbox.availability()[0],
    reason="mandatory Docker sandbox is unavailable",
)


@pytest.fixture
def runattempt_state(tmp_path: Path):
    """Own one production-test RunAttempt and always release its reservation."""
    state = State.new("experiment", tmp_path / "runattempt-state")
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
                "production RunAttempt still exists after teardown: " + attempt_id
            )


def _attempt_instance(state: State) -> dict:
    manifest = sandbox.parse_manifest(state.sandbox_manifest)
    matches = [
        row for row in sandbox.list_attempt_instances()
        if row.get("attempt_id") == manifest.attempt_id
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _manifest_mode(manifest: sandbox.SandboxManifest, path: Path) -> str | None:
    target = path.resolve()
    matches = [
        (len(Path(root).parts), mode)
        for root, mode in manifest.mounts
        if target == Path(root) or target.is_relative_to(Path(root))
    ]
    return max(matches)[1] if matches else None


def _planned_state(tmp_path: Path) -> State:
    state = State.new("experiment", tmp_path)
    saved = state.save_artifact("pre_registration", "plan", "# prereg", metadata={
        "run_role": "primary", "analysis_eligible": True,
        "expected_params": {"case": "test"},
    })
    state.mark_frozen(saved["id"])   # 冻结只出自账本的 freeze 行
    return state


def _transcript_events(state: State) -> list[str]:
    """已落盘的 transcript 事件名 —— 拦截必须留痕，靠自动审计的人才看得见。"""
    path = state.transcript_path
    if not path.exists():
        return []
    events: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line).get("type") or json.loads(line).get("event"))
        except json.JSONDecodeError:
            continue
    return [e for e in events if e]


def _bind_worktree(state: State, worktree: Path) -> None:
    """把 state 绑到一个真 Git worktree 上（兄弟节点目录一并建出来）。"""
    for node_dir in ("experiments", "figures"):
        (worktree / node_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(worktree)], check=False)
    state.project_worktree = worktree
    state.workspace_root = worktree / "experiments"
    state.workspace_relative_path = "experiments"


def _bind_operation_inputs(state: State) -> None:
    """Bind public execution tests to a stable non-scientific v1 request."""
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the declared path-boundary regression.",
        "stage": "diagnostic",
    }


# ── 1. 调度器日志：脚本写的文件名必须等于对外宣告的路径 ─────────────────────
# 实测事故：pattern 宣告 `<job_name>-<job_id>.out`，脚本 `exec >` 写的是
# `<job_id>.out`。两者永不相等 → job_health 拿 stdout_path 做进度/停滞判定时
# 永远 exists:False，一个正常跑着的作业被判成"没有任何输出"。

@pytest.mark.parametrize("scheduler", ["slurm", "pbs"])
def test_script_redirect_filename_equals_advertised_log_path(tmp_path, scheduler):
    job_name = "lmp"
    out_dir = tmp_path / "logs"
    patterns = rm._output_path_patterns(scheduler, out_dir, job_name)
    resolved = rm._resolve_output_paths(patterns, job_name, "12345")

    names = rm._script_log_basenames(scheduler, job_name)
    assert names is not None
    # 脚本里 %j 是 shell 变量，替换成同一个 job_id 后必须与解析结果同名。
    rendered = [n.replace("$HARNESS_SCHEDULER_LOG_ID", "12345") for n in names]

    assert rendered[0] == os.path.basename(resolved["stdout_path"])
    assert rendered[1] == os.path.basename(resolved["stderr_path"])


def test_submitted_slurm_script_redirects_to_the_path_it_advertises(tmp_path):
    """端到端形态：真渲染一次脚本，从脚本文本里把重定向目标抠出来比对。"""
    state = _planned_state(tmp_path)
    run_root = Path(state.root) / "case"
    run_root.mkdir(parents=True)
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}

    result = asyncio.run(_submit_job(
        state=state, command="srun ./solver", scheduler="slurm",
        job_name="lmp", workdir=str(run_root), output_dir=str(run_root / "logs"),
        dry_run=True, walltime_minutes=10))

    assert result["status"] == "success", result.get("error")
    # 读落盘的脚本本体，不读 script_preview —— preview 是中间截断的摘要
    # （见 _script_preview），重定向那行正好落在被省略的中段。
    script = Path(result["script_path"]).read_text(encoding="utf-8")
    exec_line = next(l for l in script.splitlines() if l.startswith("exec >"))
    # dry_run 没有 job_id，所以比对模板层：脚本用 job_name-$ID，pattern 用 %x-%j
    assert "lmp-$HARNESS_SCHEDULER_LOG_ID.out" in exec_line
    assert result["stdout_path_pattern"].endswith("/%x-%j.out")


def test_bootstrap_output_goes_to_a_directory_that_exists_at_submit_time(tmp_path):
    """`exec` 重定向之前那段（mkdir / stage-in / preflight）必须有归属。

    删掉 `#SBATCH --output` 的代价是它落进 SLURM 默认的 `slurm-<id>.out`，
    写在 sbatch 继承的 cwd（平台进程工作目录）里 —— 没人读，还堆垃圾。
    """
    state = _planned_state(tmp_path)
    run_root = Path(state.root) / "case"
    run_root.mkdir(parents=True)
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}

    result = asyncio.run(_submit_job(
        state=state, command="srun ./solver", scheduler="slurm",
        job_name="lmp", workdir=str(run_root), dry_run=True, walltime_minutes=10))

    assert result["status"] == "success", result.get("error")
    boot = result["bootstrap_stdout_path_pattern"]
    assert Path(boot).parent.is_dir(), "bootstrap 日志目录必须在提交时已存在"
    assert f"#SBATCH --output={boot}" in result["script_preview"]


# ── 2. local 调度器：路径角色不能自行铸造宿主写能力 ───────────────────────
# 外部 workdir 即使声明成 run_root，也必须已有 Core capability 或消费精确人工批准；
# 拒绝发生在脚本、目录和容器物化之前。

def test_local_job_requires_capability_for_declared_external_workdir(tmp_path):
    state = _planned_state(tmp_path)
    outside = tmp_path / "shared_scratch"
    outside.mkdir()
    state.hook_state["path_roles"] = {"run_root": {"path": str(outside), "writable": True}}

    result = asyncio.run(_submit_job(
        state=state, command="./solver", scheduler="local",
        workdir=str(outside), dry_run=True))

    assert result["status"] == "error"
    assert result["reason"] == "path_capability_required"
    assert result["blocker"]["kind"] == "path_capability_required"
    assert "宿主写能力" in result["error"]
    assert "命令未执行" in result["error"]


def test_local_job_does_not_create_external_directories_on_the_submit_host(tmp_path):
    state = _planned_state(tmp_path)
    outside = tmp_path / "shared_scratch"
    outside.mkdir()
    state.hook_state["path_roles"] = {"run_root": {"path": str(outside), "writable": True}}

    asyncio.run(_submit_job(
        state=state, command="./solver", scheduler="local",
        workdir=str(outside), output_dir=str(outside / "logs"), dry_run=True))

    assert not (outside / "logs").exists(), "被拒的提交不得留下任何外部目录"


# ── 3. Kubernetes：不得假定宿主机绝对路径在 Pod 内存在 ─────────────────────
# 形态：payload 里 `mkdir -p /beegfs/...` 会在容器内建一个空目录，identity
# preflight 于是通过、作业却读不到任何输入 —— 比直接失败难查得多。

def test_kubernetes_without_volume_contract_stops_before_materialization(
    tmp_path,
    monkeypatch,
):
    state = _planned_state(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Kubernetes blocker must precede route and submit")

    monkeypatch.setattr(rm, "_resolve_submission_route", forbidden)
    monkeypatch.setattr(rm, "_submit_sync", forbidden)
    run_root = Path(state.root) / "case"
    run_root.mkdir(parents=True)
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }
    runtime_root = Path(state.root) / "outputs" / "experiment" / "runtime"

    result = asyncio.run(_submit_job(
        state=state, command="./solver", scheduler="kubernetes",
        job_name="k", workdir=str(run_root), dry_run=True, hard_deadline_s=600))

    assert result["status"] == "error"
    assert result["reason"] == "kubernetes_volume_contract_required"
    assert result["blocker"]["suggested_owner"] == "framework_or_platform"
    assert "script_path" not in result
    assert not runtime_root.exists()
    assert state.list_artifacts("external_submission_intent") == []
    events = _transcript_events(state)
    assert "route_step_bound" not in events
    assert "kubernetes_volume_contract_required" in events


def test_kubernetes_rejects_a_submit_host_output_dir(tmp_path):
    """宿主机路径在 Pod 内不可见：接受它只会在提交端建出一个没人写的空目录，
    而作业把日志写进容器里同名的另一个地方。"""
    state = _planned_state(tmp_path)
    outside = tmp_path / "host_logs"

    result = asyncio.run(_submit_job(
        state=state, command="./solver", scheduler="kubernetes",
        output_dir=str(outside), dry_run=True))

    assert result["status"] == "error"
    assert "volume" in result["error"].lower()
    assert not outside.exists(), "被拒的提交不得在宿主机上留下目录"


def test_kubernetes_rejects_stage_in_without_a_volume_contract(tmp_path):
    state = _planned_state(tmp_path)
    src = Path(state.root) / "in.dat"
    src.write_text("x", encoding="utf-8")

    result = asyncio.run(_submit_job(
        state=state, command="./solver", scheduler="kubernetes",
        stage_in=[{"src": str(src), "dst": "in.dat"}], dry_run=True))

    assert result["status"] == "error"
    assert "volume" in result["error"].lower()


# ── 4. stage_in：改了源文件就让已有的提交确认失效 ──────────────────────────

def test_stage_in_records_content_digest_and_executable_bit(tmp_path):
    state = _planned_state(tmp_path)
    script = Path(state.root) / "solver.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    plain = Path(state.root) / "in.dat"
    plain.write_text("x", encoding="utf-8")

    validated = rm._validate_stage_in(
        state, [{"src": str(script), "dst": "solver.sh"},
                {"src": str(plain), "dst": "in.dat"}])

    assert validated[0]["mode"] == "0755", "可执行位必须保留，否则作业 Permission denied"
    assert validated[1]["mode"] == "0644"
    assert all(len(item["sha256"]) == 64 for item in validated)


def test_changing_a_stage_in_source_invalidates_the_existing_approval(tmp_path):
    """人批准的是**当时那些文件的内容**；路径没变而内容变了不算同一件事。"""
    state = _planned_state(tmp_path)
    run_root = Path(state.root) / "case"
    run_root.mkdir(parents=True)
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}
    src = Path(state.root) / "in.dat"
    src.write_text("original", encoding="utf-8")
    stage_in = [{"src": str(src), "dst": "in.dat"}]

    before = rm._validate_stage_in(state, stage_in)
    src.write_text("swapped after approval", encoding="utf-8")
    after = rm._validate_stage_in(state, stage_in)

    assert before != after, "内容变化必须体现在 payload 里，否则批准可被悄悄复用"
    assert before[0]["src"] == after[0]["src"], "变的只是内容，路径不变"


def test_stage_in_accepts_run_local_sources_on_an_unbound_run(tmp_path):
    """未绑 Project 的 run 里，模型自己写出的输入文件就落在 state.root 下。

    以前只认 worktree 与写角色表，于是 unbound run 想暂存自己刚写好的输入
    会被判 "outside this run's readable roots" —— 读的范围本来就比写宽。
    """
    state = _planned_state(tmp_path)
    src = Path(state.root) / "inputs" / "in.dat"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x", encoding="utf-8")

    validated = rm._validate_stage_in(state, [{"src": str(src), "dst": "in.dat"}])
    assert validated[0]["dst"] == "in.dat"


@pytest.mark.parametrize("dst", ["/abs/path", "../escape", "a/../../escape", ""])
def test_stage_in_rejects_escaping_destinations(tmp_path, dst):
    state = _planned_state(tmp_path)
    src = Path(state.root) / "in.dat"
    src.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError):
        rm._validate_stage_in(state, [{"src": str(src), "dst": dst}])


# ── 4b. bootstrap 日志接进健康判定 ────────────────────────────────────────
# `HARNESS_IDENTITY_PREFLIGHT status=failed` 一直在 _DEFAULT_HEALTH_ERROR_PATTERNS
# 里，但它只会出现在 exec 重定向之前那段输出中 —— 那段以前落在 sbatch cwd 的野
# 文件里，现在有了 bootstrap.out 却没人读。这条错误模式此前一直是死的。

class _RecordState:
    """只提供 probe_external_job_health 需要的表面。"""

    run_id = "run-health"
    node_type = "experiment"

    def __init__(self, tmp_path: Path, record: dict):
        self.project_root = tmp_path / "project"
        self.hook_state: dict = {}
        self.events: list[dict] = []
        self._records = {"job-1": {"content": json.dumps(record)}}

    def list_artifacts(self, artifact_type=None):
        if artifact_type != "job_submission":
            return []
        return [{"id": "job-1", "type": "job_submission", "name": "job-1"}]

    def read_artifact(self, artifact_id):
        return self._records[artifact_id]

    def append_transcript(self, event, **fields):
        self.events.append({"event": event, **fields})


def _health_record(tmp_path: Path) -> tuple[dict, Path]:
    job_dir = tmp_path / "jobs" / "1_lmp"
    job_dir.mkdir(parents=True)
    logs = tmp_path / "case" / "logs"
    logs.mkdir(parents=True)
    record = {
        # _submission_payloads 只收已真实提交的成功记录。
        "status": "success", "dry_run": False,
        "scheduler": "slurm", "job_id": "12345", "job_name": "lmp",
        "submitted_at": "2026-08-18T00:00:00+00:00",
        "output_roots": [str(tmp_path / "case")],
        "scheduler_output_dir": str(logs),
        "bootstrap_log_dir": str(job_dir),
        "stdout_path": str(logs / "lmp-12345.out"),
        "stderr_path": str(logs / "lmp-12345.err"),
        "bootstrap_stdout_path": str(job_dir / "bootstrap.out"),
        "bootstrap_stderr_path": str(job_dir / "bootstrap.err"),
        "command": "srun ./solver",
    }
    return record, job_dir


def test_bootstrap_failure_becomes_a_failure_signal_not_silence(tmp_path):
    """作业死在 identity preflight 时 payload 日志根本不会出现。

    以前健康快照只报"没有进度证据"，真正的原因（workdir 不可写）躺在没人读的
    文件里；要等满 stall_after_s 才会升级成 stalled，而且依旧不说原因。
    """
    record, job_dir = _health_record(tmp_path)
    (job_dir / "bootstrap.out").write_text(
        "HARNESS_IDENTITY_PREFLIGHT status=failed reason=workdir_access uid=1000\n",
        encoding="utf-8")
    state = _RecordState(tmp_path, record)

    health = rm.probe_external_job_health(state, "slurm", "12345")

    assert health["health_state"] == "failure_signal"
    assert any("bootstrap" in e["path"] for e in health["error_evidence"])


def test_bootstrap_log_is_visible_but_does_not_count_as_progress(tmp_path):
    """bootstrap 只在启动时写一次。把它算进进度，等于让一个刚启动就挂死的作业
    显示成 healthy，真失败要拖满 stall_after_s 才暴露。"""
    record, job_dir = _health_record(tmp_path)
    (job_dir / "bootstrap.out").write_text("HARNESS_IDENTITY_PREFLIGHT status=pass\n",
                                           encoding="utf-8")
    state = _RecordState(tmp_path, record)

    health = rm.probe_external_job_health(state, "slurm", "12345")

    observed = {Path(o["path"]).name: o for o in health["progress_paths"]}
    assert observed["bootstrap.out"]["exists"] is True
    assert observed["bootstrap.out"]["role"] == "bootstrap"
    # payload 日志尚未出现 → 没有任何进度证据。断言 progress_age_s 而不是
    # health_state：后者还受调度器可达性影响（本机没有 squeue 时是 unknown），
    # 而这条测试要锁的恰恰是 "bootstrap 不进 newest" 这一件事。
    assert health["progress_age_s"] is None
    assert health["health_state"] != "healthy"


# ── 5. workspace_root：可写，但不是 run_root ───────────────────────────────
# 防的是一个具体的错误方案：把节点自有 Git 工作区直接声明成 run_root。那会
# 连带继承 cleanup="root"（rm -rf 自有工作区变成免询问的合规清理）和
# scheduler workdir 资格。

def test_owned_workspace_is_writable_but_is_not_a_disposable_run_root(tmp_path):
    state = _planned_state(tmp_path)
    _bind_worktree(state, tmp_path / "worktree")

    roles = {r.role: r for r in collect_path_roles(state)}
    assert "workspace_root" in roles
    ws = roles["workspace_root"]
    assert ws.writable and not ws.container_only
    assert ws.cleanup == "none"
    # 删掉自有工作区永远要问人：它是 Git 跟踪的产物目录，不是一次性运行状态。
    assert not ws.allows_cleanup(delete_root=True)
    assert not ws.allows_cleanup(delete_root=False)
    assert sb._contained_cleanup(
        state, f"rm -rf {state.workspace_root}", str(state.workspace_root)) is None


def test_owned_workspace_is_writable_for_ordinary_files(tmp_path):
    """A 之前的形态：experiment_root=container_only 让自有目录里除 runtime/
    build 外全是不可批准、不可 bypass 的硬停 —— 连 experiment 自己代码在写的
    repro/ 也算。"""
    state = _planned_state(tmp_path)
    _bind_worktree(state, tmp_path / "worktree")

    for relative in ("case1/in.lammps", "README.md", "repro/run_manifest.json"):
        scope, reason = sb._classify_write_path(
            str(state.workspace_root / relative), state)
        assert scope == "safe", f"{relative} -> {scope}: {reason}"


def test_owned_workspace_cannot_be_a_scheduler_workdir(tmp_path):
    state = _planned_state(tmp_path)
    _bind_worktree(state, tmp_path / "worktree")

    block = rm._scheduler_role_guard(
        state, "slurm", "./solver", str(state.workspace_root), None, [], stage="toolchain_build")

    assert block is not None
    assert block["blocker"]["kind"] == "scheduler_path_not_declared"


def test_node_inputs_cannot_declare_the_owned_workspace_role(tmp_path):
    """workspace_root 只能由 State 注入：它表达的是框架分配的事实，
    不是编排方可以主张的权限。"""
    state = _planned_state(tmp_path)
    forged = tmp_path / "forged"
    forged.mkdir()
    state.hook_state["node_inputs"] = {
        "path_roles": {"workspace_root": {"path": str(forged), "writable": True}}}

    assert not any(r.role == "workspace_root" and r.path == str(forged)
                   for r in collect_path_roles(state))


# ── 6. /tmp 兜底不得盖过 worktree 归属 ────────────────────────────────────
# 形态：worktree 住在 /tmp 下时，兄弟节点目录被 `_under(p, "/tmp")` 无条件
# 判 safe。已实测能写进 postprocess/。框架侧为此专门加过一层 deny，节点侧
# 之前没有对应防护。

def test_tmp_fallback_does_not_cover_a_worktree_that_lives_under_tmp(tmp_path):
    import tempfile

    tmp_root = Path(tempfile.mkdtemp(prefix="hf-worktree-in-tmp-"))
    try:
        state = _planned_state(tmp_path)
        _bind_worktree(state, tmp_root / "worktree")
        assert str(state.project_worktree).startswith(tempfile.gettempdir())

        sibling = state.project_worktree / "figures" / "fig.png"
        scope, _ = sb._classify_write_path(str(sibling), state)
        assert scope != "safe", "兄弟节点目录不得因为住在 /tmp 下而被放行"

        # worktree 之外的真 scratch 仍然可用 —— 收窄的是归属，不是 /tmp 本身。
        outside_scope, _ = sb._classify_write_path(
            str(Path(tempfile.gettempdir()) / "unrelated_scratch" / "x.log"), state)
        assert outside_scope == "safe"
    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)


# ── 7. remote 模式：不得成为任意远端写入口 ────────────────────────────────

def test_remote_mode_rejects_undeclared_absolute_paths_without_offering_approval(tmp_path):
    """remote 下不发可登记的 pause：人工批准会登记 approved_write_root，
    那等于给作业体开一个任意远端写入口。"""
    state = _planned_state(tmp_path)
    run_root = tmp_path / "cluster"
    run_root.mkdir()
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}

    # 目标刻意选在 /tmp 之外：/tmp 有一条独立的 scratch 兜底（见
    # _classify_write_path），它在 remote 下同样生效，不是本条要锁的语义。
    undeclared = Path.home() / ".cache" / "hf-remote-undeclared" / "x"
    block = sb._bash_path_effects_guard(
        state,
        f"touch {undeclared}",
        cwd=str(run_root),
        remote=True,
        allow_authorization=False,
    )

    assert block["status"] == "error"
    assert block["blocker"]["scope"] == "remote_path_not_declared"


@pytest.mark.parametrize("target", [
    "$TMPDIR/scratch.log",
    "${TMPDIR}/scratch.log",
    "$SLURM_TMPDIR/big/dump.nc",
    "$PBS_JOBFS/x",
])
def test_remote_mode_accepts_scheduler_defined_node_local_scratch(tmp_path, target):
    """字面 /tmp 一直放行，而 `$TMPDIR` 曾被判 unresolved 拒掉。

    净效果是护栏在教模型把 /tmp 写死 —— 而很多集群的 /tmp 是小容量 tmpfs，
    `$TMPDIR` 才指向节点本地 NVMe。两种写法必须同判。
    """
    state = _planned_state(tmp_path)
    run_root = tmp_path / "cluster"
    run_root.mkdir()
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}

    assert sb._bash_path_effects_guard(
        state,
        f"srun ./a.out > {target}",
        cwd=str(run_root),
        remote=True,
        allow_authorization=False,
    ) is None


@pytest.mark.parametrize("target", [
    "$TMPDIR/$CASE/x",      # 首段之后还有别的变量 → 整体仍不可证明
    "$SCRATCH/x",           # 不在调度器定义的名单里
    "$HOME/x",
])
def test_scheduler_scratch_allowlist_does_not_become_variable_expansion(tmp_path, target):
    state = _planned_state(tmp_path)
    run_root = tmp_path / "cluster"
    run_root.mkdir()
    state.hook_state["path_roles"] = {"run_root": {"path": str(run_root), "writable": True}}

    block = sb._bash_path_effects_guard(
        state,
        f"srun ./a.out > {target}",
        cwd=str(run_root),
        remote=True,
        allow_authorization=False,
    )
    assert block["status"] == "error"
    assert block["blocker"]["scope"] == "unresolved_target"



@pytest.mark.parametrize("command", [
    "solver --output $TMPDIR/out.bin",
    "cp input.dat $TMPDIR/out.bin",
])
def test_scheduler_scratch_cli_operands_use_the_same_safe_marker(
    tmp_path, command,
):
    state = _planned_state(tmp_path)
    run_root = tmp_path / "cluster"
    run_root.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }

    assert sb._bash_path_effects_guard(
        state, command, cwd=str(run_root), remote=True,
        allow_authorization=False,
    ) is None


@pytest.mark.parametrize("command", [
    "solver --output $TMPDIR/../escape.bin",
    "cp input.dat $TMPDIR/../escape.bin",
    "solver --output $TMPDIR//etc/escape.bin",
    "solver --output $TMPDIR/$CASE/escape.bin",
])
def test_scheduler_scratch_cli_escape_is_unresolved_before_submit(
    tmp_path, command,
):
    state = _planned_state(tmp_path)
    run_root = tmp_path / "cluster"
    run_root.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }

    block = sb._bash_path_effects_guard(
        state, command, cwd=str(run_root), remote=True,
        allow_authorization=False,
    )

    assert block["status"] == "error"
    assert block["blocker"]["scope"] == "unresolved_target"


def test_scheduler_scratch_is_still_unknown_on_the_submit_host(tmp_path):
    """本机执行时 `$TMPDIR` 指向哪里同样不可证明 —— 放行只对远端作业体成立。"""
    state = _planned_state(tmp_path)
    scope, _ = sb._classify_write_path(sb.REMOTE_SCRATCH, state, remote=False)
    assert scope == "unresolved_target"
    assert sb._classify_write_path(sb.REMOTE_SCRATCH, state, remote=True)[0] == "safe"


def test_scratch_marker_never_leaks_into_a_source_write_confirmation(tmp_path):
    """标记不是路径：它会被按相对路径解析到 cwd，而 cwd 可能正好在
    source_worktree_root 里 —— 那样会为一个并不存在的文件弹源码写入确认。"""
    state = _planned_state(tmp_path)
    worktree = tmp_path / "src"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=False)
    run_root = tmp_path / "run"
    run_root.mkdir()
    state.hook_state["path_roles"] = {
        "source_worktree_root": {"path": str(worktree), "writable": True},
        "run_root": {"path": str(run_root), "writable": True},
    }

    result = sb._bash_path_effects_guard(
        state,
        "srun ./a.out > $TMPDIR/x",
        cwd=str(worktree),
        remote=True,
        allow_authorization=False,
    )

    assert result is None


def test_remote_mode_does_not_honour_human_approved_write_roots(tmp_path):
    state = _planned_state(tmp_path)
    approved = tmp_path / "approved"
    approved.mkdir()
    state.hook_state["path_roles"] = {
        "approved_write_root": [{"path": str(approved), "writable": True,
                                 "cleanup": "contents", "authority": "human"}]}

    scope, _ = sb._classify_write_path(str(approved / "x"), state, remote=True)
    assert scope == "remote_path_not_declared"

# ── 11. Bash AST 路径事件：入口必须在 spawn/脚本前阻断 ───────────────────

def test_safe_run_blocks_pipeline_nested_shell_function_and_redirect_before_spawn(
    tmp_path,
    monkeypatch,
):
    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="验证 shell-visible 越界写在 spawn 前统一阻断。",
    ))
    assert classified["status"] == "success"
    run_root = Path(state.root) / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True)
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
        "source_baseline_root": {
            "path": str(baseline),
            "writable": False,
        },
    }
    pipeline_target = baseline / "pipeline-poison"
    shell_target = baseline / "shell-poison"
    subshell_target = baseline / "subshell-poison"
    function_target = baseline / "function-poison"
    redirect_target = baseline / "redirect-poison"
    commands = [
        (f"printf x | tee {pipeline_target}", pipeline_target),
        (
            f"bash -c {chr(34)}touch {shell_target}{chr(34)}",
            shell_target,
        ),
        (
            f"(cd {baseline} && touch {subshell_target.name})",
            subshell_target,
        ),
        (
            f"f() {{ cd {baseline}; touch {function_target.name}; }}; f",
            function_target,
        ),
        (f"cd {run_root} > {redirect_target}", redirect_target),
    ]
    calls = []

    async def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("protected path must stop before executor")

    monkeypatch.setattr(sb, "_exec_and_log", forbidden)

    for command, target in commands:
        result = asyncio.run(sb._safe_run_bash(
            state,
            command,
            cwd=str(run_root),
        ))
        assert result["status"] == "error", (command, result)
        assert result["blocker"]["scope"] == "protected_source_tree"
        assert not target.exists()

    assert calls == []


def test_static_nested_run_local_writes_remain_low_risk_and_flow(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="验证静态嵌套 shell 的 run-local 小写入无需完整路线。",
    ))
    assert classified["status"] == "success"
    run_root = Path(state.root) / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True)
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }
    quote = chr(34)
    shell_target = run_root / "shell-local"
    commands = (
        f"bash -c {quote}touch {shell_target}{quote}",
        f"(cd {run_root} && touch subshell-local)",
        f"f() {{ cd {run_root}; touch function-local; }}; f",
    )
    calls = []

    async def executed(_state, cmd, **_kwargs):
        calls.append(cmd)
        return {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(sb, "_exec_and_log", executed)

    for command in commands:
        result = asyncio.run(sb._safe_run_bash(
            state, command, cwd=str(run_root)))
        assert result["status"] == "success", (command, result)
        assert result.get("reason") != "execution_route_required"

    assert calls == list(commands)


def test_submit_blocks_pipeline_before_script_intent_or_scheduler_call(
    tmp_path,
    monkeypatch,
):
    for scheduler in ("local", "slurm", "pbs"):
        state = State.new("experiment", tmp_path / scheduler)
        _bind_operation_inputs(state)
        classified = asyncio.run(_classify_experiment_scope(
            state,
            scope="operation",
            operation_category="other",
            reason="验证外部作业 payload 路径门先于脚本和 intent。",
        ))
        assert classified["status"] == "success"
        run_root = Path(state.root) / "outputs" / "experiment" / "runtime"
        run_root.mkdir(parents=True)
        baseline = tmp_path / scheduler / "baseline"
        baseline.mkdir(parents=True)
        target = baseline / "pipeline-poison"
        state.hook_state["path_roles"] = {
            "run_root": {"path": str(run_root), "writable": True},
            "source_baseline_root": {
                "path": str(baseline),
                "writable": False,
            },
        }
        calls = []

        def forbidden(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("path gate must stop before _submit_sync")

        monkeypatch.setattr(rm, "_submit_sync", forbidden)
        result = asyncio.run(_submit_job(
            state=state,
            command=f"printf x | tee {target}",
            scheduler=scheduler,
            workdir=str(run_root),
            dry_run=True,
        ))

        assert result["status"] == "error", (scheduler, result)
        assert result["blocker"]["scope"] == "protected_source_tree"
        assert calls == []
        assert not target.exists()
        assert state.list_artifacts("external_submission_intent") == []
        assert not list(run_root.rglob("*.sh"))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("curl https://example.invalid/a -o /beegfs/other/a", ("curl", "/beegfs/other/a")),
        ("wget -O /beegfs/other/a https://example.invalid/a", ("wget", "/beegfs/other/a")),
        ("scp -P 22 host:/src /beegfs/other/a", ("scp", "/beegfs/other/a")),
        ("scp local host:/dst", ("scp", sb.UNRESOLVED)),
        ("scp local scp://host/dst", ("scp", sb.UNRESOLVED)),
        ("scp local [2001:db8::1]:/dst", ("scp", sb.UNRESOLVED)),
        ("scp host:/src /run/local", ("scp", "/run/local")),
        ("rsync local host:/dst", ("rsync", sb.UNRESOLVED)),
        (
            "rsync src /beegfs/other/out --exclude pattern",
            ("rsync", "/beegfs/other/out"),
        ),
        (
            "rsync src /run/out --log-file /beegfs/other/audit.log",
            ("rsync", "/beegfs/other/audit.log"),
        ),
        ("wget -e dir_prefix=/beegfs/other https://x", ("wget", sb.UNRESOLVED)),
        ("curl https://x -o/beegfs/other/a", ("curl", "/beegfs/other/a")),
        ("curl -sSLo/beegfs/other/a https://x", ("curl", "/beegfs/other/a")),
        ("curl -sSD/beegfs/other/header https://x", ("curl", "/beegfs/other/header")),
        ("wget -qO/beegfs/other/a https://x", ("wget", "/beegfs/other/a")),
        ("my_solver -o/beegfs/other/a", ("my_solver", "/beegfs/other/a")),
        ("my_solver --out /beegfs/other/a", ("my_solver", "/beegfs/other/a")),
        ("my_solver -ofoo/../../outside/a", ("my_solver", "/outside/a")),
        (
            "python -c \"open('/beegfs/other/a', 'w')\"",
            ("python", "/beegfs/other/a"),
        ),
        ("gcc x.c -o /beegfs/other/a.out", ("gcc", "/beegfs/other/a.out")),
        (
            "gfortran -c x.f90 -J /beegfs/other/mod -o /run/x.o",
            ("gfortran", "/beegfs/other/mod"),
        ),
        ("git clone https://x /beegfs/other/src", ("git", "/beegfs/other/src")),
        ("git -C /beegfs/other/src clean -fdx", ("git", "/beegfs/other/src")),
        ("cp --target-directory=/beegfs/other/out src", ("cp", "/beegfs/other/out")),
        ("mv -t/beegfs/other/out src", ("mv", "/beegfs/other/out")),
        ("install -t /beegfs/other/out src", ("install", "/beegfs/other/out")),
        ("ln --target-directory /beegfs/other/out src", ("ln", "/beegfs/other/out")),
        ("tar -cf /beegfs/other/a.tar src", ("tar", "/beegfs/other/a.tar")),
        ("tar --create --file=/beegfs/other/a.tar src", ("tar", "/beegfs/other/a.tar")),
        ("tar -rf /beegfs/other/a.tar extra", ("tar", "/beegfs/other/a.tar")),
        ("tar cf /beegfs/other/a.tar src", ("tar", "/beegfs/other/a.tar")),
        ("zip /beegfs/other/a.zip result.dat", ("zip", "/beegfs/other/a.zip")),
        ("ar rcs /beegfs/other/lib.a x.o", ("ar", "/beegfs/other/lib.a")),
        ("python train.py --checkpoint_dir /beegfs/other/checkpoints", ("python", "/beegfs/other/checkpoints")),
        ("torchrun train.py --log-dir /beegfs/other/logs", ("torchrun", "/beegfs/other/logs")),
        ("lmp -log /beegfs/other/lammps.log", ("lmp", "/beegfs/other/lammps.log")),
        ("snakemake --directory /beegfs/other/work", ("snakemake", "/beegfs/other/work")),
        ("gmx mdrun -deffnm /beegfs/other/md/run", ("gmx", "/beegfs/other/md/run")),
        ("mpirun --output-filename /beegfs/other/mpi ./solver", ("mpirun", "/beegfs/other/mpi")),
        ("meson install --destdir /beegfs/other/install", ("meson", "/beegfs/other/install")),
        ("unrar x archive.rar /beegfs/other/unpacked", ("unrar", "/beegfs/other/unpacked")),
        ("tar -xf a.tar --directory=/beegfs/other/x", ("tar", "/beegfs/other/x")),
        ("tar -xf a.tar -C/beegfs/other/x", ("tar", "/beegfs/other/x")),
        ("unzip a.zip -d/beegfs/other/x", ("unzip", "/beegfs/other/x")),
        (
            "rsync -avT/beegfs/other/tmp src /run/dst",
            ("rsync", "/beegfs/other/tmp"),
        ),
        (
            "cmake --install build --prefix /beegfs/other/install",
            ("cmake", "/beegfs/other/install"),
        ),
        (
            "python -m pip install pkg --target /beegfs/other/site",
            ("python", "/beegfs/other/site"),
        ),
    ],
)
def test_network_and_compiler_output_selectors_enter_path_projection(
    command,
    expected,
):
    effects = sb._analyze_shell_path_effects(
        command, "/run", remote=True)
    assert expected in effects
    assert ("scp", "/run/22") not in effects


@pytest.mark.parametrize(
    ("command", "operation"),
    [
        ("tar -tf /inputs/a.tar", "tar"),
        ("tar --list --file=/inputs/a.tar", "tar"),
        ("unzip -l /inputs/a.zip", "unzip"),
        ("unzip -t /inputs/a.zip", "unzip"),
        ("ar t /inputs/lib.a", "ar"),
    ],
)
def test_archive_query_modes_do_not_invent_remote_writes(
    command,
    operation,
):
    effects = sb._analyze_shell_path_effects(command, "/run", remote=True)
    assert not [target for op, target in effects if op == operation]


def test_remote_unknown_application_allows_declared_input_and_run_local_output(
    tmp_path,
):
    state = State.new("experiment", tmp_path)
    run_root = tmp_path / "run"
    source_root = tmp_path / "source"
    run_root.mkdir()
    source_root.mkdir()
    source_file = source_root / "mesh.nc"
    source_file.write_text("mesh", encoding="utf-8")
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
        "source_baseline_root": {
            "path": str(source_root),
            "writable": False,
        },
    }

    result = sb._bash_path_effects_guard(
        state,
        f"my_solver --input {source_file} --output result.nc",
        cwd=str(run_root),
        remote=True,
        allow_authorization=False,
    )

    assert result is None


def test_remote_projection_does_not_turn_inputs_or_text_into_writes(tmp_path):
    state = State.new("experiment", tmp_path)
    source_root = tmp_path / "source"
    build_root = tmp_path / "build"
    run_root = tmp_path / "run"
    for path in (source_root, build_root, run_root):
        path.mkdir()
    state.hook_state["path_roles"] = {
        "source_baseline_root": {
            "path": str(source_root),
            "writable": False,
        },
        "build_root": {"path": str(build_root), "writable": True},
        "run_root": {"path": str(run_root), "writable": True},
    }
    commands = [
        f"cmake -DCMAKE_PREFIX_PATH=/usr -S {source_root} -B {build_root}",
        "python -c \"print('/etc/os-release')\"",
        "python -c \"open('/etc/os-release')\"",
        "echo --output /outside/is/text",
    ]

    for command in commands:
        result = sb._bash_path_effects_guard(
            state,
            command,
            cwd=str(run_root),
            remote=True,
            allow_authorization=False,
        )
        assert result is None, (command, result)


@pytest.mark.parametrize(
    ("command", "expected_scope"),
    [
        (
            "curl https://example.invalid/a -o {outside}",
            "remote_path_not_declared",
        ),
        (
            "wget -O {outside} https://example.invalid/a",
            "remote_path_not_declared",
        ),
        ("scp -P 22 host:/src {outside}", "remote_path_not_declared"),
        ("gcc x.c -o {outside}", "remote_path_not_declared"),
        ("gfortran -c x.f90 -J {outside} -o x.o", "remote_path_not_declared"),
        ("scp local host:/dst", "unresolved_target"),
        ("rsync local host:/dst", "unresolved_target"),
        (
            "rsync src {outside} --exclude pattern",
            "remote_path_not_declared",
        ),
        (
            "rsync src x --log-file {outside}",
            "remote_path_not_declared",
        ),
        (
            "wget -e dir_prefix=/beegfs/other https://example.invalid/a",
            "unresolved_target",
        ),
        (
            "curl https://example.invalid/a -o{outside}",
            "remote_path_not_declared",
        ),
        (
            "curl -sSLo{outside} https://example.invalid/a",
            "remote_path_not_declared",
        ),
        (
            "curl -sSD{outside} https://example.invalid/a",
            "remote_path_not_declared",
        ),
        (
            "wget -qO{outside} https://example.invalid/a",
            "remote_path_not_declared",
        ),
        ("my_solver --output {outside}", "remote_path_not_declared"),
        ("my_solver --out {outside}", "remote_path_not_declared"),
        ("my_solver -o{outside}", "remote_path_not_declared"),
        (
            "python -c \"open('{outside}', 'w')\"",
            "remote_path_not_declared",
        ),
        ("my_solver --output $DEST", "unresolved_target"),
        ("git clone https://x {outside}", "remote_path_not_declared"),
        ("cp --target-directory={outside} src", "remote_path_not_declared"),
        ("mv -t{outside} src", "remote_path_not_declared"),
        ("install -t {outside} src", "remote_path_not_declared"),
        ("ln --target-directory {outside} src", "remote_path_not_declared"),
        ("tar -cf {outside} src", "remote_path_not_declared"),
        ("tar --create --file={outside} src", "remote_path_not_declared"),
        ("tar -rf {outside} extra", "remote_path_not_declared"),
        ("zip {outside} result.dat", "remote_path_not_declared"),
        ("ar rcs {outside} x.o", "remote_path_not_declared"),
        ("python train.py --checkpoint_dir {outside}", "remote_path_not_declared"),
        ("torchrun train.py --log-dir {outside}", "remote_path_not_declared"),
        ("lmp -log {outside}", "remote_path_not_declared"),
        ("snakemake --directory {outside}", "remote_path_not_declared"),
        ("gmx mdrun -deffnm {outside}", "remote_path_not_declared"),
        ("mpirun --output-filename {outside} ./solver", "remote_path_not_declared"),
        ("meson install --destdir {outside}", "remote_path_not_declared"),
        ("unrar x archive.rar {outside}", "remote_path_not_declared"),
        ("tar -xf a.tar --directory={outside}", "remote_path_not_declared"),
        ("tar -xf a.tar -C{outside}", "remote_path_not_declared"),
        ("unzip a.zip -d{outside}", "remote_path_not_declared"),
        ("rsync -avT{outside} src dst", "remote_path_not_declared"),
        (
            "cmake --install build --prefix {outside}",
            "remote_path_not_declared",
        ),
        (
            "python -m pip install pkg --target {outside}",
            "remote_path_not_declared",
        ),
    ],
)
def test_remote_submit_blocks_transfer_and_compiler_escape_before_materialization(
    tmp_path,
    monkeypatch,
    command,
    expected_scope,
):
    state = State.new("experiment", tmp_path / expected_scope)
    _bind_operation_inputs(state)
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="验证传输与编译器显式输出在远端脚本物化前受路径角色约束。",
    ))
    assert classified["status"] == "success"
    run_root = Path(state.root) / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True)
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }
    outside = Path.home() / ".cache" / "hf-remote-output-escape" / "x"
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("路径门必须早于脚本、intent 与调度器调用")

    monkeypatch.setattr(rm, "_submit_sync", forbidden)
    result = asyncio.run(_submit_job(
        state=state,
        command=command.format(outside=outside),
        scheduler="slurm",
        workdir=str(run_root),
        dry_run=True,
    ))

    assert result["status"] == "error", result
    assert result["blocker"]["scope"] == expected_scope
    assert calls == []
    assert state.list_artifacts("external_submission_intent") == []
    assert not list(run_root.rglob("*.sh"))



def test_ast_path_guard_keeps_standard_out_of_source_build_flow(tmp_path):
    state = State.new("experiment", tmp_path)
    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    state.hook_state["path_roles"] = {
        "source_baseline_root": {
            "path": str(source),
            "writable": False,
        },
        "build_root": {"path": str(build), "writable": True},
    }

    assert sb._bash_path_effects_guard(
        state,
        f"cmake -S {source} -B {build}",
        cwd=str(source),
        allow_authorization=False,
    ) is None
    assert sb._bash_path_effects_guard(
        state,
        f"cmake --build {build} 2>&1 | tee {build / 'build.log'}",
        cwd=str(source),
        allow_authorization=False,
    ) is None


# ── 12. 轻量 Python：框架状态只读、运行目录精确可写 ─────────────────────────

def test_python_invalid_role_overlap_blocks_unrecognized_writer_before_spawn(
    tmp_path,
    monkeypatch,
):
    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="验证 poisoned path role contract 在 Python AST 早退前被拒绝。",
    ))
    assert classified["status"] == "success"
    baseline = tmp_path / "baseline"
    run_root = baseline / "run"
    run_root.mkdir(parents=True)
    state.hook_state["path_roles"] = {
        "source_baseline_root": {
            "path": str(baseline),
            "writable": False,
        },
        "run_root": {"path": str(run_root), "writable": True},
    }
    calls = []
    policy.register_approved_subprocess_write_root(state, str(run_root))

    async def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("invalid role contract must stop before executor")

    monkeypatch.setattr(sb, "_exec_and_log", forbidden)
    code = (
        "import numpy as np\n"
        "target = chr(120)\n"
        "np.save(target, [1, 2, 3])"
    )

    result = asyncio.run(sb._safe_execute_python(
        state,
        code,
        cwd=str(run_root),
    ))

    assert result["status"] == "error"
    assert result["blocker"]["scope"] == "invalid_path_role_contract"
    assert calls == []


def test_python_sandbox_profile_defensively_rejects_role_overlap(tmp_path):
    state = State.new("experiment", tmp_path)
    baseline = tmp_path / "baseline"
    run_root = baseline / "run"
    run_root.mkdir(parents=True)
    state.hook_state["path_roles"] = {
        "source_baseline_root": {
            "path": str(baseline),
            "writable": False,
        },
        "run_root": {"path": str(run_root), "writable": True},
    }

    with pytest.raises(policy.PythonSandboxContractError):
        policy.python_sandbox_roots(state, str(run_root))


def test_missing_protected_role_uses_nearest_existing_readonly_overlay(tmp_path):
    state = State.new("experiment", tmp_path)
    run_root = Path(state.root) / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True)
    missing_baseline = tmp_path / "not-created" / "baseline"
    state.hook_state["path_roles"] = {
        "source_baseline_root": {
            "path": str(missing_baseline),
            "writable": False,
        },
        "run_root": {"path": str(run_root), "writable": True},
    }

    writable, readonly = policy.python_sandbox_roots(
        state,
        str(run_root),
    )

    assert missing_baseline.parent.parent.resolve() in readonly
    assert run_root.resolve() in writable


def test_python_sandbox_overlays_framework_state_and_keeps_run_root_writable(
    tmp_path,
    monkeypatch,
):
    state = State.new("experiment", tmp_path)
    run_root = Path(state.root) / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True)
    baseline = tmp_path / "source-baseline"
    baseline.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
        "source_baseline_root": {
            "path": str(baseline),
            "writable": False,
        },
    }
    from shared.lib import dangerous_commands

    # 未绑定 Project 且全类别 bypass 时，公共 model_tool_roots 返回 None；
    # Python 完整性 profile 必须仍主动恢复基础根并加只读 overlay。
    monkeypatch.setattr(dangerous_commands, "BYPASS_ENABLED", True)

    writable, readonly = policy.python_sandbox_roots(
        state,
        str(run_root),
        authorized_targets=[],
    )

    state_root = Path(state.root).resolve()
    assert state_root in readonly
    assert baseline.resolve() in readonly
    assert run_root.resolve() in writable
    assert state_root not in writable
    assert Path(tempfile.gettempdir()).resolve() not in writable
    assert (Path.home() / ".cache").resolve() not in writable


@pytest.mark.production_sandbox
@requires_sandbox
@pytest.mark.asyncio
async def test_safe_execute_python_container_hides_control_socket_and_allows_local_socket(
    runattempt_state, tmp_path,
):
    from core import isolation

    if isolation.select_backend().name != "image":
        # 「宿主控制 socket 不存在」是 image 后端 mount namespace 的机制事实；
        # 原生 linux 后端下 Landlock FS 规则管不住 AF_UNIX connect（实测 payload
        # 能连上宿主 socket，rc=91）——这是 core/isolation 的上游能力缺口，
        # 已成文 issue_native_backend_unix_socket_0904.md 提 framework owner，
        # 不是本节点能修的墙。缺口修掉后本 skip 应删。
        pytest.skip("host-socket hiding is an image-backend mechanism; "
                    "native-backend AF_UNIX reachability is an upstream "
                    "core/isolation gap (issue_native_backend_unix_socket_0904)")
    state = runattempt_state
    _bind_operation_inputs(state)
    classified = await _classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="验证轻量 Python 不能借宿主控制 socket 逃出 RunAttempt。",
    )
    assert classified["status"] == "success"
    run_root = Path(state.root) / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True, exist_ok=True)
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }
    host_socket = tmp_path / "docker.sock"
    local_socket = "normal.sock"
    assert len(os.fsencode(host_socket)) < 108
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(host_socket))
    server.listen(1)
    code = (
        "import ctypes, socket\n"
        "class SockaddrUn(ctypes.Structure):\n"
        "    _fields_ = [(\"sun_family\", ctypes.c_ushort), "
        "(\"sun_path\", ctypes.c_char * 108)]\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "address = SockaddrUn()\n"
        "address.sun_family = socket.AF_UNIX\n"
        f"address.sun_path = {os.fsencode(host_socket)!r}\n"
        "fd = libc.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)\n"
        "rc = libc.connect(fd, ctypes.byref(address), ctypes.sizeof(address))\n"
        "libc.close(fd)\n"
        "if rc == 0:\n"
        "    raise SystemExit(91)\n"
        "local_server = socket.socket(socket.AF_UNIX)\n"
        f"local_server.bind({str(local_socket)!r})\n"
        "local_server.listen(1)\n"
        "local_client = socket.socket(socket.AF_UNIX)\n"
        f"local_client.connect({str(local_socket)!r})\n"
        "peer, _ = local_server.accept()\n"
        "peer.close(); local_client.close(); local_server.close()\n"
        "print(\"control-blocked-local-ok\")\n"
    )
    try:
        result = await sb._safe_execute_python(
            state, code, cwd=str(run_root), timeout=30,
        )
    finally:
        server.close()

    assert result["status"] == "success", result
    assert "control-blocked-local-ok" in str(result.get("stdout_tail") or "")
    frozen = sandbox.parse_manifest(state.sandbox_manifest)
    assert frozen.network_mode == "none"
    assert len(str(_attempt_instance(state).get("id") or "")) == 64


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "m=__import__(\"sub\"+\"process\"); "
            "getattr(m, \"Popen\")([\"make\"])",
            "subprocess.Popen",
        ),
        (
            "getattr(__import__(\"o\"+\"s\"), \"system\")(\"make\")",
            "os.system",
        ),
        (
            "import importlib; "
            "importlib.import_module(\"subprocess\").run([\"make\"])",
            "subprocess.run",
        ),
        (
            "imp=__import__; sp=imp(\"subprocess\"); "
            "getattr(sp, \"Popen\")([\"make\"])",
            "subprocess.Popen",
        ),
    ],
)
def test_python_reflective_process_launches_are_detected(code, expected):
    assert expected in sb._python_process_launches(code)


def test_python_benign_dynamic_import_is_not_reported_as_process_launch():
    assert sb._python_process_launches(
        "__import__(\"math\").sqrt(4)"
    ) == []


def test_python_audit_hook_allows_run_local_unix_socket(tmp_path):
    local_socket = tmp_path / "normal.sock"
    code = sb._with_no_child_process_audit(
        "import socket\n"
        "server = socket.socket(socket.AF_UNIX)\n"
        f"server.bind({str(local_socket)!r})\n"
        "server.listen(1)\n"
        "client = socket.socket(socket.AF_UNIX)\n"
        f"client.connect({str(local_socket)!r})\n"
        "peer, _ = server.accept()\n"
        "peer.close(); client.close(); server.close()\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_python_audit_hook_still_blocks_inet_socket():
    code = sb._with_no_child_process_audit(
        "import socket\nsocket.socket(socket.AF_INET)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "safe_execute_python only permits AF_UNIX sockets" in result.stderr


def test_python_audit_hook_still_blocks_reflective_child_process():
    code = sb._with_no_child_process_audit(
        "mod = __import__(\"subprocess\")\n"
        "getattr(mod, \"Popen\")([\"/bin/true\"])\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "safe_execute_python forbids child-process creation: subprocess.Popen" in (
        result.stderr
    )


def test_python_source_worktree_requires_target_and_local_capability(tmp_path):
    state = State.new("experiment", tmp_path)
    run_root = Path(state.root) / "outputs" / "experiment" / "runtime"
    run_root.mkdir(parents=True)
    source_worktree = tmp_path / "source-worktree"
    source_worktree.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
        "source_worktree_root": {
            "path": str(source_worktree),
            "writable": True,
        },
    }

    default_writable, default_readonly = policy.python_sandbox_roots(
        state,
        str(run_root),
        authorized_targets=[],
    )
    with pytest.raises(
        policy.PythonSandboxContractError, match="path_capability_required",
    ):
        policy.python_sandbox_roots(
            state,
            str(run_root),
            authorized_targets=[str(source_worktree / "patched.c")],
        )

    policy.register_approved_subprocess_write_root(state, str(source_worktree))
    confirmed_writable, confirmed_readonly = policy.python_sandbox_roots(
        state,
        str(run_root),
        authorized_targets=[str(source_worktree / "patched.c")],
    )

    assert source_worktree.resolve() in default_readonly
    assert source_worktree.resolve() not in default_writable
    assert source_worktree.resolve() in confirmed_readonly
    assert source_worktree.resolve() in confirmed_writable


def test_protected_path_role_is_never_bypassable():
    assert sb._scope_bypass_allowed("protected_global_env") is True
    assert sb._scope_bypass_allowed("protected_path_role") is False


def test_bash_sandbox_protects_framework_state_and_rebinds_run_build_roots(tmp_path):
    state = State.new("experiment", tmp_path / "bash-state-roots")
    state_root = Path(state.root).resolve()
    run_root = sb.experiment_output_dir(state, "runtime", create=True).resolve()
    build_root = sb.experiment_output_dir(state, "build", create=True).resolve()
    for role_root in (run_root, build_root):
        writable, readonly = policy.bash_sandbox_roots(
            state,
            str(role_root),
            authorized_targets=[],
        )
        assert state_root not in writable
        assert state_root in readonly
        assert role_root in writable


def test_bash_sandbox_keeps_authorized_source_root_in_exact_rw_and_ro_sets(
    tmp_path,
):
    state = State.new("experiment", tmp_path / "bash-source-state")
    run_root = sb.experiment_output_dir(state, "runtime", create=True).resolve()
    source_worktree = (tmp_path / "source-worktree-authorized").resolve()
    source_worktree.mkdir()
    state.hook_state["path_roles"] = {
        "source_worktree_root": {
            "path": str(source_worktree),
            "writable": True,
        },
    }
    policy.register_approved_subprocess_write_root(
        state, str(source_worktree),
    )
    writable, readonly = policy.bash_sandbox_roots(
        state,
        str(run_root),
        authorized_targets=[str(source_worktree / "fix.patch")],
    )

    assert source_worktree in writable
    assert source_worktree in readonly


@requires_sandbox
@pytest.mark.asyncio
async def test_bash_runattempt_blocks_framework_state_write_but_allows_run_root(
    runattempt_state,
):
    state = runattempt_state
    run_root = sb.experiment_output_dir(state, "runtime", create=True).resolve()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }
    writable, readonly = policy.bash_sandbox_roots(
        state, str(run_root), authorized_targets=[],
    )
    assert Path(state.root).resolve() in readonly
    assert Path(state.root).resolve() not in writable
    assert run_root in writable

    framework_target = Path(state.root) / "artifacts" / "unknown-poison"
    allowed_target = run_root / "allowed-output"

    blocked = await sb._exec_and_log(
        state,
        f"printf poison > {shlex.quote(str(framework_target))}",
        cwd=str(run_root),
        timeout=30,
        sandbox_profile="bash",
    )
    assert blocked["status"] == "error", blocked
    assert not framework_target.exists()

    frozen = sandbox.parse_manifest(state.sandbox_manifest)
    # PR C 后 "hardened" 的定义即「逐命令写边界」：原生后端逐命令构造墙，
    # manifest v4 的 profile 恒为 hardened（PR B 短暂用过 "native" 一词，已并回）。
    assert frozen.security_profile == "hardened"

    allowed = await sb._exec_and_log(
        state,
        f"printf allowed > {shlex.quote(str(allowed_target))}",
        cwd=str(run_root),
        timeout=30,
        sandbox_profile="bash",
    )
    assert allowed["status"] == "success", allowed
    assert allowed_target.read_text(encoding="utf-8") == "allowed"


def test_bash_sandbox_requires_core_or_human_path_capability(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path / "runs")
    external_run = tmp_path / "external-run"
    source_worktree = tmp_path / "source-worktree"
    external_run.mkdir()
    source_worktree.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(external_run), "writable": True},
        "source_worktree_root": {
            "path": str(source_worktree), "writable": True,
        },
    }
    from core import sandbox

    monkeypatch.setattr(
        sandbox, "model_tool_roots",
        lambda _state: ([Path(state.root)], []),
    )

    with pytest.raises(
        policy.BashSandboxContractError, match="path_capability_required",
    ):
        policy.bash_sandbox_roots(
            state, str(external_run), authorized_targets=[]
        )
    with pytest.raises(
        policy.PythonSandboxContractError, match="path_capability_required",
    ):
        policy.python_sandbox_roots(
            state, str(external_run), authorized_targets=[]
        )

    policy.register_approved_subprocess_write_root(state, str(external_run))
    writable, readonly = policy.bash_sandbox_roots(
        state, str(external_run), authorized_targets=[]
    )
    assert external_run.resolve() in writable
    assert source_worktree.resolve() not in writable
    assert source_worktree.resolve() in readonly

    with pytest.raises(
        policy.BashSandboxContractError, match="path_capability_required",
    ):
        policy.bash_sandbox_roots(
            state, str(source_worktree),
            authorized_targets=[str(source_worktree / "fix.patch")],
        )
    policy.register_approved_subprocess_write_root(state, str(source_worktree))
    writable_with_patch, _readonly = policy.bash_sandbox_roots(
        state, str(source_worktree),
        authorized_targets=[str(source_worktree / "fix.patch")],
    )
    assert source_worktree.resolve() in writable_with_patch


def test_workdir_role_without_local_capability_is_rejected(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path / "runs-capability")
    external = tmp_path / "external-capability-root"
    external.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(external), "writable": True},
    }
    from core import project_workspace, sandbox

    monkeypatch.setattr(
        project_workspace, "validate_tool_cwd",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("outside")),
    )
    monkeypatch.setattr(
        sandbox, "model_tool_roots",
        lambda _state: ([Path(state.root)], []),
    )

    resolved, error = sb.resolve_required_workdir(state, str(external))
    assert resolved is None
    assert error["reason"] == "path_capability_required"

    policy.register_approved_subprocess_write_root(state, str(external))
    resolved, error = sb.resolve_required_workdir(state, str(external))
    assert error is None
    assert resolved == str(external.resolve())


@requires_sandbox
@pytest.mark.asyncio
async def test_approved_root_is_writable_but_frozen_attempt_cannot_expand(
    runattempt_state, tmp_path,
):
    state = runattempt_state
    approved_root = tmp_path / "approved-execution-root"
    later_root = tmp_path / "later-execution-root"
    approved_root.mkdir()
    later_root.mkdir()
    state.hook_state["path_roles"] = {
        "approved_write_root": {
            "path": str(approved_root), "writable": True,
        },
    }
    policy.register_approved_subprocess_write_root(state, str(approved_root))
    approved_target = approved_root / "result.txt"

    first = await sb._exec_and_log(
        state,
        "printf approved > result.txt",
        cwd=str(approved_root),
        timeout=30,
        sandbox_profile="bash",
        sandbox_write_targets=[str(approved_target)],
    )
    assert first["status"] == "success", first
    assert approved_target.read_text(encoding="utf-8") == "approved"

    frozen = sandbox.parse_manifest(state.sandbox_manifest)
    assert _manifest_mode(frozen, approved_root) == "rw"
    assert _manifest_mode(frozen, later_root) is None
    # 活容器实例与 64 位 docker id 是 image 后端的机制事实；原生后端没有常驻
    # 实例可查，身份稳定性由下方 manifest hash / attempt_id 断言承担（两后端共用）。
    first_instance = _attempt_instance(state) if frozen.backend == "image" else None
    if first_instance is not None:
        assert len(str(first_instance.get("id") or "")) == 64

    state.hook_state["path_roles"] = {
        "approved_write_root": {
            "path": str(later_root), "writable": True,
        },
    }
    policy.register_approved_subprocess_write_root(state, str(later_root))
    later_target = later_root / "must-not-exist.txt"
    expanded = await sb._exec_and_log(
        state,
        "printf expanded > must-not-exist.txt",
        cwd=str(later_root),
        timeout=30,
        sandbox_profile="bash",
        sandbox_write_targets=[str(later_target)],
    )

    assert expanded["status"] == "error", expanded
    assert "not frozen into this RunAttempt" in str(expanded.get("error") or "")
    assert not later_target.exists()
    assert state.sandbox_manifest_hash == frozen.sha256
    assert sandbox.parse_manifest(state.sandbox_manifest).attempt_id == frozen.attempt_id
    if first_instance is not None:
        assert _attempt_instance(state)["id"] == first_instance["id"]


def test_unknown_remote_application_requires_static_paths_to_be_declared(
    tmp_path,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    for command in (
        "hf_unknown_solver /beegfs/unowned/result.bin",
        "hf_unknown_solver --input /beegfs/unowned/input.dat",
        "hf_unknown_solver --input=/beegfs/unowned/input.dat",
        "hf_unknown_solver ../outside/result.bin",
    ):
        block = sb._bash_path_effects_guard(
            state,
            command,
            cwd=run_root,
            remote=True,
            allow_authorization=False,
        )
        assert block is not None, command
        assert block["blocker"]["scope"] == "remote_path_not_declared", (
            command,
            block,
        )
        assert block["blocker"]["op"] == "remote_visible_path"


def test_unknown_remote_application_accepts_declared_read_and_write_roots(
    tmp_path,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    input_root = "/beegfs/hf-route-contract/input"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
        "dependency_root": {"path": input_root, "writable": False},
    }

    assert sb._bash_path_effects_guard(
        state,
        (
            "hf_unknown_solver "
            f"--input {input_root}/case.dat {run_root}/result.bin"
        ),
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    ) is None


def test_remote_visibility_does_not_turn_read_only_role_into_write_permission(
    tmp_path,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    input_root = "/beegfs/hf-route-contract/input"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
        "dependency_root": {"path": input_root, "writable": False},
    }

    block = sb._bash_path_effects_guard(
        state,
        f"hf_unknown_solver --output {input_root}/poison.dat",
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    )

    assert block is not None
    assert block["blocker"]["scope"] == "protected_dependency"
    assert block["blocker"]["op"] == "hf_unknown_solver"


def test_remote_visibility_ignores_urls_and_remote_transfer_endpoints(
    tmp_path,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    assert sb._bash_path_effects_guard(
        state,
        (
            "hf_unknown_solver https://example.invalid/data "
            "user@host:/remote/input scp://host/remote/input"
        ),
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    ) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat /beegfs/unowned/input.dat > result.dat",
        "head -n 1 /beegfs/unowned/input.dat",
        "grep needle /beegfs/unowned/input.dat",
        "cp /beegfs/unowned/input.dat result.dat",
        "mv /beegfs/unowned/input.dat result.dat",
        "install /beegfs/unowned/input.dat result.dat",
        "gcc /beegfs/unowned/source.c -o result.o",
    ],
)
def test_known_remote_commands_also_require_static_inputs_to_be_declared(
    tmp_path,
    command,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    block = sb._bash_path_effects_guard(
        state,
        command,
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    )

    assert block is not None, command
    assert block["blocker"]["scope"] == "remote_path_not_declared"


def test_remote_visibility_does_not_treat_dynamic_scalars_as_paths(
    tmp_path,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    for command in (
        "hf_unknown_solver --steps $N_STEPS",
        "hf_unknown_solver --temperature $TEMP_K",
        "hf_unknown_solver --label $CASE_ID",
    ):
        assert sb._bash_path_effects_guard(
            state,
            command,
            cwd=run_root,
            remote=True,
            allow_authorization=False,
        ) is None, command


def test_known_remote_read_from_declared_dependency_is_allowed(
    tmp_path,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    input_root = "/beegfs/hf-route-contract/input"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
        "dependency_root": {"path": input_root, "writable": False},
    }

    assert sb._bash_path_effects_guard(
        state,
        f"cat {input_root}/case.dat > result.dat",
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    ) is None


# ── E-13：路径形式命令头（argv[0]）纳入远端可见性投影 ───────────────────────
# 旧口径从 index=1 起扫 operand，`/beegfs/unowned/bin/solver` 这种路径形式
# 命令头绕过 remote_path_not_declared；裸名（远端 PATH 解析）保持放行，改为
# remote_path_lookup 留痕。


@pytest.mark.parametrize(
    "command",
    [
        "/beegfs/unowned/bin/solver --steps 5",
        "../outside/solver --steps 5",
        "env OMP_NUM_THREADS=4 /beegfs/unowned/bin/solver run",
        "command /beegfs/unowned/bin/solver run",
        "printf x | xargs /beegfs/unowned/bin/tool",
    ],
)
def test_remote_path_form_command_head_requires_declared_visibility(
    tmp_path,
    command,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    block = sb._bash_path_effects_guard(
        state,
        command,
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    )

    assert block is not None, command
    assert block["blocker"]["scope"] == "remote_path_not_declared", (
        command, block)
    assert block["blocker"]["op"] == "remote_visible_path"


@pytest.mark.parametrize(
    "command",
    [
        # cwd 在已声明 run_root 内的相对路径头。
        "./solver --steps 5",
        # 只读 dependency_root 覆盖的绝对路径头（可见 ≠ 可写）。
        "/beegfs/hf-route-contract/input/bin/solver --steps 5",
        # 执行主机系统只读根。
        "/usr/bin/ldd ./solver",
        # 执行主机节点本地 /tmp（stage_in 交付后的典型形态）。
        "/tmp/staged/tool --steps 5",
    ],
)
def test_remote_path_form_command_head_accepts_declared_visibility(
    tmp_path,
    command,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    input_root = "/beegfs/hf-route-contract/input"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
        "dependency_root": {"path": input_root, "writable": False},
    }

    assert sb._bash_path_effects_guard(
        state,
        command,
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    ) is None, command


def test_remote_bare_command_head_is_allowed_with_lookup_trail(
    tmp_path,
) -> None:
    """裸命令名靠远端 PATH：绝不硬拒，放行并留 remote_path_lookup 痕迹。"""
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    assert sb._bash_path_effects_guard(
        state,
        "cd subcase && my_solver --steps 5",
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    ) is None
    assert "remote_path_lookup" in _transcript_events(state)

    payloads = [
        json.loads(line)
        for line in state.transcript_path.read_text(
            encoding="utf-8").splitlines()
        if line.strip() and "remote_path_lookup" in line
    ]
    heads = payloads[-1].get("heads") or payloads[-1].get(
        "payload", {}).get("heads")
    # shell 语法词（cd）不查 PATH，不该淹没真正的外部程序名。
    assert heads == ["my_solver"]


def test_local_mode_keeps_path_form_head_behavior_unchanged(
    tmp_path,
) -> None:
    """不变量：head 投影仅 remote=True 生效，本地路径行为零变化。"""
    state = _planned_state(tmp_path)
    run_root = tmp_path / "run"
    run_root.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }

    assert sb._bash_path_effects_guard(
        state,
        "/beegfs/unowned/bin/solver --steps 5",
        cwd=str(run_root),
        remote=False,
    ) is None
    assert "remote_path_lookup" not in _transcript_events(state)


def test_remote_head_rejection_lists_roles_and_recovery_channels(
    tmp_path,
) -> None:
    """耦合放宽：硬拒文案必须给出已声明角色清单与两条可执行通道。"""
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    block = sb._bash_path_effects_guard(
        state,
        "/beegfs/unowned/bin/solver --steps 5",
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    )

    assert block is not None
    error = str(block["error"])
    assert "run_root" in error and run_root in error
    assert "dependency_root" in error
    assert "stage_in" in error


@pytest.mark.parametrize(
    "command",
    [
        "hf_unknown_solver file:///beegfs/unowned/input.dat",
        "hf_unknown_solver --input file:/beegfs/unowned/input.dat",
        "hf_unknown_solver --output file://localhost/beegfs/unowned/result.bin",
    ],
)
def test_remote_file_uri_is_classified_as_a_local_path(
    tmp_path,
    command,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    block = sb._bash_path_effects_guard(
        state,
        command,
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    )

    assert block is not None, command
    assert block["blocker"]["scope"] == "remote_path_not_declared"


@pytest.mark.parametrize(
    "command",
    [
        "hf_unknown_solver @/beegfs/hf-route-contract/run/args.rsp",
        "hf_unknown_solver --config @/beegfs/hf-route-contract/run/config.rsp",
    ],
)
def test_remote_response_files_fail_closed_until_expanded(
    tmp_path,
    command,
) -> None:
    state = _planned_state(tmp_path)
    run_root = "/beegfs/hf-route-contract/run"
    state.hook_state["path_roles"] = {
        "run_root": {"path": run_root, "writable": True},
    }

    block = sb._bash_path_effects_guard(
        state,
        command,
        cwd=run_root,
        remote=True,
        allow_authorization=False,
    )

    assert block is not None
    assert block["blocker"]["scope"] == "unresolved_target"
# ── 7. path projection repair: production hook, source build gate, local roots ─

def test_real_path_role_hook_keeps_workspace_baseline_contract_valid(tmp_path):
    """State-owned workspace and a baseline below it are a normal project layout."""
    state = _planned_state(tmp_path)
    _bind_worktree(state, tmp_path / "worktree")
    source = Path(state.workspace_root) / "vendor-src"
    source.mkdir()
    state.hook_state["node_inputs"] = {"source_path": str(source)}

    hooks._path_role_convention_on_turn_start(
        HookContext(harness=None, state=state, messages=[], turn=1))

    report = validate_path_roles(state)
    workspace_roles = [role for role in collect_path_roles(state)
                       if role.role == "workspace_root"]
    assert report["valid"], report
    assert "workspace_root" not in state.hook_state["path_roles"]
    assert [role.source for role in workspace_roles] == ["framework:workspace_root"]


def test_build_guard_rejects_compilation_in_managed_source(tmp_path):
    """The source/build separation rule must run before any compiler can spawn."""
    state = _planned_state(tmp_path)
    source = tmp_path / "managed-source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    (source / "a.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    state.hook_state["path_roles"] = {
        "managed_source_root": {"path": str(source), "writable": True},
        "build_root": {"path": str(build), "writable": True},
    }
    for command in ("gcc -c a.c -o a.o", "./vendor-build"):
        result = sb._activity_path_role_guard(
            state,
            command,
            cwd=str(source),
            declared_build=True,
        )
        assert result is not None
        assert result["blocker"]["scope"] == "protected_source_tree"
    assert not (source / "a.o").exists()


def test_local_dry_run_does_not_turn_declared_role_into_host_capability(
    tmp_path, monkeypatch,
):
    """A path role is semantic input, not authority to mount an external host root."""
    state = _planned_state(tmp_path)
    runtime = Path(state.root) / "runtime"
    runtime.mkdir()
    external = tmp_path / "external-scheduler-root"
    external.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(external), "writable": True},
    }
    monkeypatch.setattr(
        sandbox, "model_tool_roots",
        lambda _state: ([Path(state.root)], []),
    )

    writable, readonly = policy.bash_sandbox_roots(
        state, str(state.root), authorized_targets=[],
    )
    assert external.resolve() not in writable
    assert Path(state.root).resolve() in readonly
    assert not policy.path_has_local_write_capability(state, external)

    result = rm._submit_sync(
        runtime, "local", "printf local", "local-root-check",
        1, 1, 0, 1.0, 1.0, 1,
        None, None, None, str(state.root), True, None,
        stage_in=None,
        state=state,
    )

    assert result["status"] == "success", result
    assert result["namespace"] is None
    assert result["sandbox_contract"]["adapter"] == "docker"
    assert result["sandbox_contract"]["adapter_kind"] == "native_local_job"
    assert result["sandbox_contract"]["storage_gb"] == 1.0
