"""high_risk_command_audit hook 测试（experiment 节点本地）。

背景：no_unauthorized_high_risk_commands quality_check 原先由 LLM judge
盲判（state_summary 只有 tool_call 计数，看不到命令内容），且 judge JSON
截断会产生技术性 false negative（2026-07-06 vasp-h2 run）。本 hook 在
run 结束时机械对账，把结论写进 memory 供 judge 直读。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

_NODE_DIR = Path(__file__).resolve().parents[1]

# hooks.py 顶层就 register_loop_hook + sys.path 注入，按文件路径加载一次即可
_spec = importlib.util.spec_from_file_location(
    "experiment_hooks_under_test", _NODE_DIR / "hooks.py")
_hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hooks)

from core.loop_hooks import HookContext          # noqa: E402
from core.state import State                     # noqa: E402


def _make_ctx() -> HookContext:
    """绑真 worktree —— 记忆只住在 Project Git 里。

    不绑就没有项目记忆，这个 hook 的证据也就无处可落。测试必须跑在真实
    部署形状上，否则守的是一条现实里不存在的路径。
    """
    import subprocess

    root = Path(tempfile.mkdtemp(prefix="hf-hraudit-"))
    wt = root / "wt"
    wt.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=wt, check=True)
    (wt / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=wt, check=True)

    from core import memory as M

    state = State.new(node_type="experiment", base_dir=root / "runs",
                      project_id="hraudit", project_worktree=wt)
    M.ensure_skeleton(state)
    return HookContext(harness=None, state=state, messages=[], turn=1)


def _audit_memory(state: State) -> str:
    # The hook writes a v2 candidate; state.list_memory() is the retired v1
    # read path and deliberately does not mirror candidates.jsonl.
    from core import memory as M
    mems = [e.text for e in M.manual_entries(state)
            if "高危命令对账" in e.text or "high_risk" in e.text.lower()]
    assert len(mems) == 1, f"应恰好写 1 条 audit 条目，实际 {len(mems)}"
    return mems[0]


def test_clean_run_passes() -> None:
    ctx = _make_ctx()
    ctx.state.append_transcript("tool_call", name="run_bash",
                                args={"command": "ls -la && cat POSCAR"})
    ctx.state.append_transcript("tool_call", name="run_bash",
                                args={"command": "sbatch job.sh"})
    _hooks.high_risk_command_audit_on_end(ctx, None)

    text = _audit_memory(ctx.state)
    assert "PASS" in text
    assert "扫描 2 条" in text


def test_env_var_substring_not_false_positive() -> None:
    """$SUDO_USER / 文件名含 dd 等不触发（对齐 safe_bash 边界语义）。"""
    ctx = _make_ctx()
    ctx.state.append_transcript("tool_call", name="run_bash",
                                args={"command": "echo $SUDO_USER; grep resume dd_report.txt"})
    _hooks.high_risk_command_audit_on_end(ctx, None)
    assert "PASS" in _audit_memory(ctx.state)


def test_unaccounted_high_risk_is_violation() -> None:
    """高危命中且无 blocked/allowed 事件 = 绕过执行层 → VIOLATION。"""
    ctx = _make_ctx()
    ctx.state.append_transcript("tool_call", name="run_bash",
                                args={"command": "sudo rm -rf /scratch/run7"})
    _hooks.high_risk_command_audit_on_end(ctx, None)

    text = _audit_memory(ctx.state)
    assert "VIOLATION" in text
    assert "sudo" in text


def test_blocked_by_safe_bash_still_passes() -> None:
    """safe_bash 事前拦截（未执行）不算违规。"""
    ctx = _make_ctx()
    cmd = "sudo rm -rf /scratch/run7"
    ctx.state.append_transcript("tool_call", name="run_bash",
                                args={"command": cmd})
    ctx.state.append_transcript("highrisk_bash_blocked",
                                cmd_preview=cmd[:200], category="提权: sudo")
    _hooks.high_risk_command_audit_on_end(ctx, None)

    text = _audit_memory(ctx.state)
    assert "PASS" in text
    assert "事前拦截未执行 1" in text


def test_authorized_high_risk_passes_with_audit_trail() -> None:
    ctx = _make_ctx()
    cmd = "sudo apt install libxml-libxml-perl"
    ctx.state.append_transcript("tool_call", name="run_bash",
                                args={"command": cmd})
    ctx.state.append_transcript("highrisk_bash_allowed",
                                cmd_preview=cmd[:200], category="提权: sudo",
                                authorized_by="allowlist:sudo apt install libxml")
    _hooks.high_risk_command_audit_on_end(ctx, None)

    text = _audit_memory(ctx.state)
    assert "PASS" in text
    assert "已授权放行 1" in text
    assert "allowlist" in text


def test_execute_python_code_also_scanned() -> None:
    ctx = _make_ctx()
    ctx.state.append_transcript("tool_call", name="execute_python",
                                args={"code": "import os; os.system('rm -rf /data')"})
    _hooks.high_risk_command_audit_on_end(ctx, None)
    assert "VIOLATION" in _audit_memory(ctx.state)


def test_new_safe_tool_names_also_scanned() -> None:
    """v0.11 改名迁移：safe_run_bash / safe_execute_python 同样纳入审计。"""
    ctx = _make_ctx()
    ctx.state.append_transcript("tool_call", name="safe_run_bash",
                                args={"command": "sudo rm -rf /scratch/x"})
    ctx.state.append_transcript("tool_call", name="safe_execute_python",
                                args={"code": "import os; os.system('ls')"})
    _hooks.high_risk_command_audit_on_end(ctx, None)

    text = _audit_memory(ctx.state)
    assert "VIOLATION" in text          # safe_run_bash 的 sudo 命中
    assert "扫描 2 条" in text          # 两个 safe_* 调用都被扫到


def test_framework_gate_events_reconciled() -> None:
    """v3.2/PR#97：框架门的 confirmed_run / blocked_pending_confirm 事件参与对账，
    人工批准执行的高危命令不误判 VIOLATION。"""
    ctx = _make_ctx()
    cmd = "sudo apt install libxml-libxml-perl"
    ctx.state.append_transcript("tool_call", name="safe_run_bash",
                                args={"command": cmd})
    ctx.state.append_transcript("highrisk_bash_confirmed_run",
                                cmd_preview=cmd[:200], category="提权 (sudo)")
    _hooks.high_risk_command_audit_on_end(ctx, None)

    text = _audit_memory(ctx.state)
    assert "PASS" in text
    assert "framework_hitl_confirm" in text


def test_framework_gate_pending_confirm_counts_as_blocked() -> None:
    ctx = _make_ctx()
    cmd = "rm -rf /scratch/old"
    ctx.state.append_transcript("tool_call", name="safe_run_bash",
                                args={"command": cmd})
    ctx.state.append_transcript("highrisk_bash_blocked_pending_confirm",
                                cmd_preview=cmd[:200], category="递归强制删除 (rm -rf)")
    _hooks.high_risk_command_audit_on_end(ctx, None)

    text = _audit_memory(ctx.state)
    assert "PASS" in text
    assert "事前拦截未执行 1" in text


def test_python_blocked_event_reconciled() -> None:
    """safe_bash 的 highrisk_python_blocked（code_preview 字段）也要对上账。"""
    ctx = _make_ctx()
    code = "import shutil; shutil.rmtree('/data')"
    ctx.state.append_transcript("tool_call", name="safe_execute_python",
                                args={"code": code})
    ctx.state.append_transcript("highrisk_python_blocked",
                                code_preview=code[:200], category="递归删除")
    _hooks.high_risk_command_audit_on_end(ctx, None)

    text = _audit_memory(ctx.state)
    assert "PASS" in text


def test_transcript_event_written() -> None:
    """除 memory 外还要落 high_risk_command_audit transcript 事件（复盘用）。"""
    import json
    ctx = _make_ctx()
    ctx.state.append_transcript("tool_call", name="run_bash",
                                args={"command": "ls"})
    _hooks.high_risk_command_audit_on_end(ctx, None)

    events = [json.loads(l) for l in
              ctx.state.transcript_path.read_text().splitlines() if l.strip()]
    audit = [e for e in events if e.get("event") == "high_risk_command_audit"]
    assert len(audit) == 1
    assert audit[0]["verdict"] == "PASS"
    assert audit[0]["n_scanned"] == 1


# ── safe_bash 跨模块解析 + 高危门 fail-closed ────────────────────────────────
# 本文件把 hooks.py 以 "experiment_hooks_under_test" 这个**非包成员**名字加载，
# 正是旧代码 `from .tools.safe_bash import ...` 相对 import 会 ImportError 的场景：
# 当时异常被吞掉，高危判定退化成"没命中"，命令照跑。

def test_safe_bash_resolves_under_foreign_module_name() -> None:
    """hooks 以任意模块名加载时都要能拿到 safe_bash（不依赖相对 import）。"""
    module = _hooks._safe_bash()
    assert callable(module.match_high_risk)
    assert callable(module.bench_enabled)


def test_safe_bash_reuses_loaded_module_instead_of_reimporting() -> None:
    """必须复用 sys.modules 里已注册工具的那份，否则会重复注册工具。"""
    module = _hooks._safe_bash()
    assert module is sys.modules.get("nodes.experiment.tools.safe_bash") \
        or module is sys.modules.get("tools.safe_bash")


def test_min_run_compatibility_helper_never_spawns_hidden_process(
    monkeypatch,
) -> None:
    """旧 helper 只给迁移错误；所有执行统一走带 State 的受管工具。"""
    import subprocess

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("verify_min_run must not spawn")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    result = _hooks.verify_min_run("echo hello")

    assert result["milestone"] is False
    assert result["reason"] == "managed_execution_required"
    assert "safe_run_bash" in result["error"]


def test_min_run_high_risk_input_cannot_restore_the_legacy_bypass() -> None:
    """命令内容不改变兼容哨兵行为，更不能恢复旧的裸 subprocess。"""
    result = _hooks.verify_min_run("rm -rf /")
    assert result["milestone"] is False
    assert result["reason"] == "managed_execution_required"


def test_job_submission_has_no_prefilter_high_risk_gate() -> None:
    """判决拆除·第三波（rm:1446 删）：`_submit_sync` 的高危死分支与 `_reject_high_risk`
    已删——高危 job 命令的唯一门是 submit_job 的结构化 HITL 确认
    （见 test_resource_manager::test_high_risk_job_payload_uses_the_same_submission_confirmation）。"""
    from nodes.experiment.tools import resource_manager as rm

    assert not hasattr(rm, "_reject_high_risk")
