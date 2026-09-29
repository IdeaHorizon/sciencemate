"""shell_probe_only：协调类节点的探查专用 shell（角色边界，v3.3 → 见证）。

实测事故：orchestrator（"协调者不做研究"）决定替 experiment 干活——write_file 被
白名单弹回后，直接绕道 run_bash heredoc 写 Python 跑实验。"run_bash 只用于只读
探查"当时是纯 prompt 文字，零机械后果。v3.3 起 harness `shell_probe_only: true`
让框架机械识别写文件/内联解释器/装软件。

判决拆除第三波（builtin:645 降格）：识别照跑，但不再硬拒 —— 那是角色/资格判决
（S3）；写边界真正的守卫在 spawn 层沙箱。越界命令照跑，如实见证
`probe_only_shell_violation` 进 transcript 与返回值，referee 终审。不联网。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.state import State
from shared.lib.dangerous_commands import match_probe_only_violation

bootstrap()


# ══ 1. 模式判定（纯函数）══════════════════════════════════════════════════════

# 合法探查——一律放行
_ALLOWED = [
    "ls -la ~/experiments",
    "grep -rn 'pattern' output/ 2>/dev/null",
    "nproc && free -h",
    "nvidia-smi",
    "which lammps || echo missing",
    "pip show numpy",
    "git status",
    "cat output/run1/summary.json | head -50",
    "df -h > /dev/null 2>&1; echo ok",       # /dev/null + fd 重定向是只读惯用法
    "sysctl -n hw.ncpu",
    "wc -l data.csv",
]

# 越界——一律拦
_DENIED = [
    ("cat > analysis.py", "重定向写文件"),
    ("echo x >> results.txt", "重定向写文件"),
    ("python3 - <<'EOF'\nprint(1)\nEOF", None),          # heredoc（python - 也命中）
    ("cat <<EOF > script.py\ncode\nEOF", None),
    ("python3 -c 'import numpy; print(1)'", "python -c 内联执行"),
    ("bash -c 'do_stuff'", "shell -c 内联执行"),
    ("pip install torch", "安装软件（环境变更）"),
    ("conda install -y lammps", "安装软件（环境变更）"),
    ("sed -i 's/a/b/' config.yaml", "sed -i 原地改写"),
    ("mkdir -p workspace && cp a.py workspace/", None),
    ("rm -f old_results.json", None),
    ("tee out.log", "tee 写文件"),
    ("wget https://example.com/data.tar.gz", "下载写盘"),
    ("curl -o data.json https://api.example.com", "下载写盘"),
    ("touch marker.done", None),
]


@pytest.mark.parametrize("cmd", _ALLOWED)
def test_probe_commands_allowed(cmd):
    assert match_probe_only_violation(cmd) is None, f"探查命令被误拦: {cmd!r}"


@pytest.mark.parametrize("cmd,expected", _DENIED)
def test_violations_denied(cmd, expected):
    got = match_probe_only_violation(cmd)
    assert got is not None, f"越界命令没拦住: {cmd!r}"
    if expected:
        assert got == expected, f"{cmd!r}: got {got!r}"


# ══ 2. run_bash 集成（flag 生效 / 不设不影响）═══════════════════════════════════

def _events(state: State) -> list[dict]:
    import json
    return [json.loads(l) for l in state.transcript_path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture()
def _no_real_shell(monkeypatch):
    """见证发生在 spawn 之前，与沙箱能不能起无关 —— 把 spawn 换成桩，本机没有
    Landlock/seatbelt 也能验这条路径（真 spawn 的用例另有，且是环境红）。"""
    async def _fake_spawn(cmd, **kw):
        return "ok", 0, b"ran\n", b""

    monkeypatch.setattr("shared.tools.builtin.spawn_and_wait", _fake_spawn)


@pytest.mark.asyncio
async def test_run_bash_witnesses_a_write_in_probe_mode(tmp_path: Path, _no_real_shell):
    """越界命令不再被硬拒：照跑，见证进 transcript 与返回值。墙加回去这条转红。"""
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_po")
    state.hook_state["_shell_probe_only"] = True
    res = await execute_tool("run_bash", state, cmd="echo 'raw_results = ...' | tee probe.txt")
    assert "探查专用" not in (res.get("error") or ""), res
    assert res.get("probe_only_shell_violation") == "tee 写文件", res
    witnessed = [e for e in _events(state) if e.get("event") == "probe_only_shell_violation"]
    assert witnessed and witnessed[-1]["category"] == "tee 写文件"


@pytest.mark.asyncio
async def test_run_bash_witnesses_heredoc_python_in_probe_mode(tmp_path: Path, _no_real_shell):
    """实测逃逸路径：heredoc 内联 python —— 照跑 + 见证，不拦。"""
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_po2")
    state.hook_state["_shell_probe_only"] = True
    res = await execute_tool("run_bash", state,
                              cmd="python3 - <<'EOF'\nprint(1+1)\nEOF")
    assert "探查专用" not in (res.get("error") or ""), res
    assert res.get("probe_only_shell_violation"), res
    assert any(e.get("event") == "probe_only_shell_violation" for e in _events(state))


@pytest.mark.asyncio
async def test_run_bash_allows_probe_in_probe_mode(tmp_path: Path):
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_po3")
    state.hook_state["_shell_probe_only"] = True
    res = await execute_tool("run_bash", state, cmd="echo probe-ok && ls /tmp > /dev/null 2>&1; echo done")
    assert res["status"] == "success", res
    assert "probe-ok" in res.get("stdout_tail", "")


@pytest.mark.asyncio
async def test_run_bash_unrestricted_without_flag(tmp_path: Path):
    """未开 flag 的节点可写自己的 run root，但仍不能写宿主父目录。"""
    from core.tool_registry import execute as execute_tool

    state = State.new(node_type="postprocess", base_dir=tmp_path, project_id="p_po4")
    target = Path(state.root) / "ok.txt"
    res = await execute_tool("run_bash", state, cmd=f"echo data > {target}")
    assert res["status"] == "success", res
    assert target.exists()


# ══ 3. harness 装配链（yaml → NodeHarness → agent_loop → hook_state）══════════

def test_orchestrator_harness_declares_probe_only():
    h = load_harness("_orchestrator")
    assert h.shell_probe_only is True


def test_other_nodes_default_off():
    for node in ("experiment", "literature", "writing"):
        assert load_harness(node).shell_probe_only is False, node


@pytest.mark.asyncio
async def test_agent_loop_propagates_flag(tmp_path: Path):
    """run_loop 开始即把 harness.shell_probe_only 写进 hook_state（显式赋值，
    orchestrator 持久 hook_state 也能随 yaml 改动更新）。"""
    from core.agent_loop import run_loop
    from core.harness import NodeHarness
    from core.llm import LLMMessage

    class _StubLLM:
        async def chat(self, *a, **kw):
            from core.llm import LLMResponse
            return LLMResponse(content="done", tool_calls=[],
                                finish_reason="stop", usage={})

    h = NodeHarness(node_type="_orchestrator")
    h.shell_probe_only = True
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p_po5")
    await run_loop(h, state, [LLMMessage(role="user", content="hi")], _StubLLM())
    assert state.hook_state["_shell_probe_only"] is True
