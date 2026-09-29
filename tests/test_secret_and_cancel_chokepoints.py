"""两条全局不变量收在必经之路上（issue #279 秘密外泄 / #284 取消不生效）。

两条 issue 表面无关，缝是同一条：**框架有机制，但只在一个调用点上接了**。

  #279：客户只授权 data 节点读 `${PODSYS_SAFE_SOURCE}` 指向的目录，节点却
        `read_file("/proc/self/environ")` —— 整个进程环境（含 LLM_API_KEY）
        进了工具结果 → 模型上下文 → transcript → provider 日志。
        只堵 read_file 没用：`run_bash("cat /proc/self/environ")` 一句就绕过。
  #284：用户 `/stop`，`loop_cancelled` 已记录，0.7ms 后 data 的
        `data_auto_planning_recovery` 又起来跑了两轮。信号传播本来是全的
        （chat 会写进每个 active run 的 state），但全仓只有 run_loop 轮初查
        一次，而且是 `pop` —— 弹掉之后自定义 loop 再进来就干净了。
        模型调用另有 9 个直调点，一个都没查。

修法同形：`tool_registry.execute` 和 `LLMClient.chat` 是进程里派发工具、发模型
请求的**唯二**出口，把检查放在那儿，谁也绕不过，也不用同事改自己的节点。
"""
from __future__ import annotations

import asyncio

import pytest

from core import cancellation, secrets
from core.bootstrap import bootstrap
from core.state import State


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield


# ── #279-A：敏感路径 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/proc/self/environ",
    "/proc/1234/environ",
    "/proc/self/cmdline",
    "/home/u/.ssh/id_ed25519",
    "/home/u/.aws/credentials",
    "/srv/app/.env",
    "/srv/app/.env.production",
    "/home/u/.git-credentials",
    r"C:\Users\researcher\.SSH\id_ed25519",
    r"C:\Users\researcher\.aws\credentials",
    r"C:\research\.ENV.production::$DATA",
])
def test_sensitive_paths_denied(path):
    assert secrets.is_sensitive_path(path), f"{path} 必须拒读"


@pytest.mark.parametrize("path", [
    "/data/podsys/runs/summary.csv",
    "/home/u/project/.environment_notes.md",   # 不是 .env 文件
    "/proc/meminfo",                            # 不是环境块
    "/home/u/paper/environ_discussion.tex",
])
def test_normal_paths_not_denied(path):
    assert secrets.is_sensitive_path(path) is None, f"{path} 是正常文件，不许误伤"


@pytest.mark.asyncio
async def test_read_file_refuses_process_environ(tmp_path):
    """现场复刻：模型点名要读 /proc/self/environ。"""
    from core.tool_registry import execute

    state = State.new(node_type="data", base_dir=tmp_path)
    r = await execute("read_file", state, path="/proc/self/environ")
    assert r["status"] == "error"
    assert "拒绝读取" in r["error"]
    # 拒绝理由里绝不能带内容
    assert "=" not in r.get("error", "").split("：")[-1][:40]


# ── #279-B：值脱敏（根治那一层）─────────────────────────────────────────

@pytest.mark.asyncio
async def test_secret_value_redacted_whatever_tool_leaked_it(tmp_path, monkeypatch):
    """模型进程既读不到宿主 secret，出口脱敏仍保留为纵深防御。

    这里用 run_bash 验证更强的不变量：宿主环境变量根本不进入沙盒。
    """
    from core.tool_registry import execute

    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret-value-123456")
    state = State.new(node_type="data", base_dir=tmp_path)

    r = await execute("run_bash", state,
                      cmd="echo $LLM_API_KEY")
    blob = str(r)
    assert "sk-super-secret-value-123456" not in blob, "秘密不许进模型上下文"
    assert "«REDACTED:LLM_API_KEY»" not in blob
    assert r["stdout_tail"].strip() == ""


def test_redaction_keeps_shape_and_spares_short_values(monkeypatch):
    """脱敏保形，且不误伤短值（"true"/端口号之类会被换掉就没法用了）。"""
    monkeypatch.setenv("MY_TOKEN", "abcdefghijklmnop")
    monkeypatch.setenv("MY_SECRET_FLAG", "on")        # 太短 → 不当秘密
    monkeypatch.setenv("HARNESS_MODEL", "deepseek-v4-pro")   # 名字不像秘密
    out = secrets.redact({
        "status": "success",
        "rows": [{"note": "token=abcdefghijklmnop"}, {"note": "mode=on"}],
        "count": 3,
        "model": "deepseek-v4-pro",
    })
    assert out["status"] == "success"          # 结构/键名不动
    assert out["count"] == 3                   # 非字符串不动
    assert out["model"] == "deepseek-v4-pro"   # 名字不像秘密 → 不动
    assert "abcdefghijklmnop" not in str(out)
    assert out["rows"][1]["note"] == "mode=on"  # 短值不误伤


def test_contains_secret_reports_names_never_values(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abcdefghijklmnop")
    hits = secrets.contains_secret({"x": "…abcdefghijklmnop…"})
    assert hits == ["MY_TOKEN"]
    assert "abcdefghijklmnop" not in str(hits)


# ── #284：取消咽喉 ────────────────────────────────────────────────────────

def _cancelled_state(tmp_path):
    state = State.new(node_type="data", base_dir=tmp_path)
    state.hook_state["kill_signal"] = {
        "reason": "用户 /stop（panic）", "requested_by": "user_panic"}
    return state


@pytest.mark.asyncio
async def test_tool_dispatch_refused_after_cancel(tmp_path):
    from core.tool_registry import execute

    state = _cancelled_state(tmp_path)
    with cancellation.bind_run(state):
        with pytest.raises(cancellation.RunCancelled) as ei:
            await execute("list_artifacts", state)
    assert ei.value.where == "tool:list_artifacts"


@pytest.mark.asyncio
async def test_llm_call_refused_after_cancel(tmp_path):
    """9 个直调点（含 data 的 preprocessing_planner）共用这一个出口。"""
    from core.llm import LLMClient, LLMMessage

    state = _cancelled_state(tmp_path)
    client = LLMClient(api_key="x", base_url="http://localhost", model="m")
    with cancellation.bind_run(state):
        with pytest.raises(cancellation.RunCancelled):
            await client.chat([LLMMessage(role="user", content="hi")])


def test_cancel_is_sticky_across_reentry(tmp_path):
    """#284 的核心：run_loop 弹掉 kill_signal 之后，取消状态必须还在。

    否则自定义 loop 再调一次 run_default 就看不见任何取消痕迹 —— 现场
    `loop_cancelled` 之后 0.7ms 又起 recovery 就是这么发生的。
    """
    state = _cancelled_state(tmp_path)
    sig = state.hook_state.pop("kill_signal")      # ← run_loop 轮初就是这么干的
    assert cancellation.signal_for(state) is None, "测试前提：弹掉就没了"
    cancellation.mark_cancelled(state, sig)
    assert cancellation.signal_for(state)["requested_by"] == "user_panic"


def test_only_new_user_input_clears_cancel(tmp_path):
    """粘性不能粘死：新的用户输入要能解除，否则这个 state 永久废掉。"""
    state = _cancelled_state(tmp_path)
    cancellation.mark_cancelled(state, state.hook_state["kill_signal"])
    cancellation.clear(state)
    assert cancellation.signal_for(state) is None


def test_unbound_context_never_blocks(tmp_path):
    """没绑定 run 时（脚本直调、测试替身）不许误拦。"""
    assert cancellation.current_signal() is None
    cancellation.check("llm.chat")      # 不抛


@pytest.mark.asyncio
async def test_binding_is_per_task_not_global(tmp_path):
    """并行子 run：一个被取消不能牵连另一个。"""
    from core.tool_registry import execute

    cancelled = _cancelled_state(tmp_path / "a")
    healthy = State.new(node_type="literature", base_dir=tmp_path / "b")

    async def _cancelled_side():
        with cancellation.bind_run(cancelled):
            await asyncio.sleep(0)
            with pytest.raises(cancellation.RunCancelled):
                await execute("list_artifacts", cancelled)
            return "raised"

    async def _healthy_side():
        with cancellation.bind_run(healthy):
            await asyncio.sleep(0)
            r = await execute("list_artifacts", healthy)
            return r["status"]

    got = await asyncio.gather(_cancelled_side(), _healthy_side())
    assert got == ["raised", "success"]


@pytest.mark.asyncio
async def test_cancelled_run_still_finalizes(tmp_path, monkeypatch):
    """取消要能体面收场：仍写 summary，不是抛 traceback（验收 3）。

    自定义 loop 不走 run_loop 的收口，取消从工具咽喉一路冒到 execute_node ——
    那里必须收住并照常 finalize，否则「取消」退化成「炸掉」。
    """
    from core import executor as exec_mod
    from core.harness import NodeHarness
    from core.llm import LLMClient

    harness = NodeHarness(node_type="data", system_prompt="t",
                          tools=["list_artifacts"], max_turns=5,
                          required_outputs=[])
    monkeypatch.setattr(exec_mod, "load_harness",
                        lambda node_type, nodes_dir=None: harness)

    async def _greedy_custom_loop(harness, state, messages, llm):
        """模拟 data 的自定义 loop：取消之后还想接着调工具。"""
        state.hook_state["kill_signal"] = {
            "reason": "用户 /stop（panic）", "requested_by": "user_panic"}
        from core.tool_registry import execute
        await execute("list_artifacts", state)      # ← 必须在这里被拦
        raise AssertionError("取消之后不该还能派发工具")

    from core import custom_loop as _cl
    monkeypatch.setattr(_cl, "resolve_custom_loop",
                        lambda node_type: _greedy_custom_loop)

    summary = await exec_mod.execute_node(
        node_type="data", state_dir=tmp_path / "run",
        llm=LLMClient(api_key="x", base_url="http://localhost", model="m"))
    assert summary["status"] == "cancelled"
    assert summary["cancel_meta"]["requested_by"] == "user_panic"
    assert summary["cancel_meta"]["cancelled_at"] == "tool:list_artifacts"
