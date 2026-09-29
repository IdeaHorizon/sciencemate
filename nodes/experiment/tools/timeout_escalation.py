"""safe_run_bash 的超时升级阶梯。

背景（2026-07-31 实证，项目 e2e-jicq221-poiseuille）：experiment 节点和
orchestrator 先后 6 次尝试下载同一份 21MB 源码，网络实测 ~17KB/s（需 ~20 分钟），
每次都同步挂在一个 bash 调用里等，被 300/600 秒超时砍断，然后**从头再来**。
6 次累计下载 >60MB，最终留在磁盘上的是 0 字节 —— 因为 git clone 被打断后会自己
删掉半成品目录。全程没有任何一次改用后台执行、加大 timeout、或问一句用户
"要不要延长时间"。最后 agent 的结论是"环境不可行"，而实际上只是没人告诉它
超时之后还有别的路可走。

根因不在模型，在工具返回值：超时时只返回

    {"status": "timeout", "cmd": ..., "timeout_s": ...}

这个返回值有三个问题：
  1. **丢弃了已有进展**。执行器已经保留 stdout/stderr 尾部，但调用点没往外传 —— agent 看不到
     "Receiving objects: 68%"，无从判断"快好了"还是"根本没动"，只能当纯失败重试。
  2. **没给出路**。超时是所有失败里最不像死路的一种（进程可能只差 10 秒），
     但返回值里没有任何"下一步可以怎么办"，于是模型只会原样重试。
  3. **没有记忆**。同一个目标超时第 5 次和第 1 次拿到的返回值一模一样，
     不累积状态就不可能有升级行为。

本模块把这三点补上：返回值带**残留输出** + **按次数升级的阶梯** + **跨调用的账本**，
首次超时后对同一目标的后续同步执行硬拒（受管提交或问人是唯一出口）。

⚠️ 作用域：只覆盖 experiment 节点自己的 ``safe_run_bash``。框架版
``shared/tools/builtin.py:_run_bash``（orchestrator 等节点在用）有**完全相同的三个
缺陷**，外加"超时只 kill shell 不 kill 进程组"的孤儿进程 bug —— 那份不在本节点
所有权范围内，已单独报给 owner，不在这里改。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .bash_semantics import BashAnalysis, analyze_bash, analyzer_unavailable_reason

# 超时账本存放位置（state.hook_state 跨 pause/resume 保持，正合适）
_LEDGER_KEY = "_timeout_ledger"

# 同一目标首次同步超时后，后续同步重试就必须改为受管提交。
# 超时后的进程已被杀；再靠加长前台 timeout 只会占用 agent 并无法跨 chat 恢复。
_HARD_BLOCK_AT = 1

# 残留输出保留长度：够看清进度条和最后的报错，又不至于灌爆上下文
_PARTIAL_TAIL = 2000

_URL_RE = re.compile(r"https?://[^\s'\"|;)&]+")

_SCHEDULER_SUBMITTERS = frozenset({"sbatch", "qsub", "bsub", "salloc"})
_SCHEDULER_QUERY_OPTIONS = frozenset({"--help", "--usage", "--version", "-h", "-v"})


def _query_only(args: tuple[str, ...]) -> bool:
    return bool(args) and all(arg.lower() in _SCHEDULER_QUERY_OPTIONS for arg in args)


def bash_analyzer_unavailable_reason() -> str | None:
    return analyzer_unavailable_reason()


@dataclass(frozen=True)
class BashExecutionDecision:
    analysis: BashAnalysis
    known_background_launch: bool
    known_srun_launch: bool
    uncertainty_kind: str | None

    @property
    def unverifiable_execution(self) -> bool:
        return self.uncertainty_kind is not None


def classify_bash_execution(cmd: str) -> BashExecutionDecision:
    analysis = analyze_bash(cmd)
    known_background = (
        analysis.backgrounded
        or analysis.detached_launch
        or any(
            invocation.head in _SCHEDULER_SUBMITTERS
            and not _query_only(invocation.args)
            for invocation in analysis.scheduler_invocations
        )
    )
    known_srun = any(
        invocation.head == "srun" and not _query_only(invocation.args)
        for invocation in analysis.scheduler_invocations
    )
    if analysis.analyzer_unavailable is not None:
        uncertainty = "analyzer_unavailable"
    elif analysis.ignore_errors_build:
        uncertainty = "ignore_errors_build"
    elif analysis.parse_error:
        uncertainty = "parse_error"
    elif analysis.dynamic_execution:
        uncertainty = "dynamic_execution"
    else:
        uncertainty = None
    return BashExecutionDecision(
        analysis=analysis,
        known_background_launch=known_background,
        known_srun_launch=known_srun,
        uncertainty_kind=uncertainty,
    )


def looks_backgrounded(cmd: str) -> bool:
    """Whether the analyzer proved an unmanaged background launch."""
    return classify_bash_execution(cmd).known_background_launch


# 判决拆除·第三波（te:195/247 删，2026-09-02）：
#   - `uses_srun` + safe_bash 裸 srun 墙：Attempt 沙盒 network=none，srun 连不上
#     slurmctld，现实自己拒绝，纯预测墙；唯一调用点随删。
#   - `managed_submission_requirement`：「声明 expected_duration_s≥600 就必须走
#     submit_job」是路线仪式（check_after 分支早已死：注册表先 pop 掉该参数）。
#     长命令照跑，受 timeout 与沙盒 walltime 约束；safe_bash 在结果与 transcript
#     上见证 `managed_submission_recommended`（阈值仍由这里唯一声明）。
_SYNC_LONG_THRESHOLD_S = 600


def target_signature(cmd: str) -> str:
    """给命令算一个"目标"签名，用来跨不同写法归并同一件事。

    带 URL 的命令按 **远端 host** 归并：``git clone https://github.com/...``、
    ``curl -L https://github.com/...``、``wget https://github.com/...`` 是同一件
    事的三种写法，按整条命令文本做签名会把它们算成三次"第一次超时"，升级阶梯
    永远升不上去 —— 这正是实证里发生的事。不带 URL 的命令退回归一化文本。
    """
    m = _URL_RE.search(cmd or "")
    if m:
        netloc = urlparse(m.group(0)).netloc
        if netloc:
            return f"host:{netloc}"
    return "cmd:" + " ".join((cmd or "").split())[:200]


def _ledger(state: Any) -> dict:
    store = getattr(state, "hook_state", None)
    if store is None:            # 极端情况下（裸 state）退化成不记账，但不报错
        return {"total": 0, "by_target": {}}
    led = store.get(_LEDGER_KEY)
    if not isinstance(led, dict):
        led = {"total": 0, "by_target": {}}
        store[_LEDGER_KEY] = led
    led.setdefault("total", 0)
    led.setdefault("by_target", {})
    return led


def timeout_count(state: Any, cmd: str) -> int:
    """这个目标此前已经同步超时过几次（不含本次）。"""
    return int(_ledger(state)["by_target"].get(target_signature(cmd), 0))


def record_timeout(state: Any, cmd: str) -> int:
    """记一次超时，返回该目标累计超时次数（含本次）。"""
    led = _ledger(state)
    sig = target_signature(cmd)
    n = int(led["by_target"].get(sig, 0)) + 1
    led["by_target"][sig] = n
    led["total"] = int(led.get("total", 0)) + 1
    return n



def _ladder_text(level: int, timeout_s: int, has_partial: bool) -> str:
    """Timeout recovery has one durable route: managed submission or user direction."""
    submit_hint = (
        "改为受管提交：先 `submit_job(dry_run=true, workdir=<声明的 build_root/run_root>, ...)` "
        "检查脚本，再 `submit_job(dry_run=false, ...)`。提交后用一次 `job_status` 确认身份/输出，"
        "随后结束节点交给 external-job handoff；不要 nohup/setsid/& 后在本节点轮询。"
    )
    ask_hint = (
        "`request_human_input` 问用户：说明已跑了多久、进展到哪、"
        "预估还需多久，请对方在\"受管提交 / 换个方案 / 手动提供产物\"里选。"
        "用户在旁边时，问一句比猜便宜得多。"
    )
    resume_hint = (
        "⚠️ 下载/编译类**不要删掉半成品重来**：curl 用 `-C -` 续传、"
        "git 用 `git fetch` 接着拉、make 直接重跑会复用 .o。"
        "确认服务端不支持续传（curl 报 33）再考虑从头下。"
    )

    head = (
        "⏱️ 同步命令已超时并被终止；这不是可跨会话恢复的作业。"
        + ("先看 partial_stdout/partial_stderr 判断已有进展。"
           if has_partial else "本次没有可用残留输出。")
    )
    return (
        f"{head}\n\n⛔ 不允许以更大的 timeout 再次同步执行同一目标。必须二选一：\n"
        f"   ① {submit_hint}\n"
        f"   ② {ask_hint}\n\n"
        f"如果输出显示可续传或增量构建，须把续跑命令放进 submit_job；"
        f"不要删掉半成品或用 nohup/setsid/& 绕过。\n\n{resume_hint}"
    )


def build_timeout_payload(
    state: Any,
    *,
    tool: str,
    cmd: str,
    timeout_s: int,
    stdout: bytes | str = b"",
    stderr: bytes | str = b"",
) -> dict:
    """把裸 timeout 换成"带残留输出 + 带出路 + 带次数"的结构化返回值。

    调用点在停止沙盒之后调用；``stdout``/``stderr`` 是执行器收回的残留输出。
    """
    def _tail(b: bytes | str) -> str:
        s = b.decode("utf-8", errors="replace") if isinstance(b, bytes) else (b or "")
        return s[-_PARTIAL_TAIL:]

    out_tail, err_tail = _tail(stdout), _tail(stderr)
    has_partial = bool(out_tail.strip() or err_tail.strip())
    level = record_timeout(state, cmd)

    payload = {
        "status": "timeout",
        "cmd": cmd[:200],
        "timeout_s": timeout_s,
        "timeout_count_for_target": level,
        "target": target_signature(cmd),
        "made_output_before_kill": has_partial,
        "partial_stdout": out_tail,
        "partial_stderr": err_tail,
        "next_steps": _ladder_text(level, timeout_s, has_partial),
    }
    if level >= _HARD_BLOCK_AT:
        payload["warning"] = (
            f"对 {target_signature(cmd)} 的后续同步执行将被拒绝，"
            f"请改为受管提交或调用 request_human_input。")

    try:
        state.append_transcript(
            "bash_timeout", tool=tool, cmd_preview=cmd[:200],
            timeout_s=timeout_s, target=target_signature(cmd),
            count=level, made_output=has_partial)
    except Exception:
        pass                      # 观测性失败不能影响命令返回
    return payload


def check_sync_block(state: Any, *, tool: str, cmd: str) -> dict | None:
    """执行前检查：同一目标已同步超时够多次，就不再让它同步跑第 N 次。

    返回 ``None`` 表示放行；返回 dict 表示拦截（调用方直接把它当结果返回）。
    自行后台化不是本节点的恢复路径：safe_bash 会在更早的 guard 拒绝它；
    这里也不再把它当作熔断豁免。

    判决拆除二审（te:402 保留·升 A，2026-08-31）：这是算力熔断（A 类，守护
    不可逆资源边界），不是充分性判决；其文案形态 —— 明列可走的出口 +
    「这是执行方式限制不是不可行判定」—— 立为全仓熔断措辞范本。
    """
    n = timeout_count(state, cmd)
    if n < _HARD_BLOCK_AT:
        return None

    sig = target_signature(cmd)
    try:
        state.append_transcript(
            "bash_sync_retry_blocked", tool=tool,
            cmd_preview=cmd[:200], target=sig, prior_timeouts=n)
    except Exception:
        pass
    return {
        "status": "error",
        "error": (
            f"⛔ 同步执行被拒绝：目标 `{sig}` 在本 run 内已同步超时 {n} 次，"
            f"继续原路重试只会再烧一轮。\n\n"
            f"两个出口（任选其一即可放行）：\n"
            f"   ② 受管提交：用 `submit_job` 提交到声明的 build_root/run_root，"
            f"由 external-job handoff 交接；\n"
            f"   ③ `request_human_input`：向用户说明已尝试 {n} 次、每次卡在哪，"
            f"请对方决定「延长时长 / 换方案 / 手动提供产物」。\n\n"
            f"注意：这是执行方式的限制，不是「此事不可行」的判定 —— "
            f"在走完 ③ 之前不要写 infeasible。"
        ),
        "target": sig,
        "prior_timeouts": n,
    }
