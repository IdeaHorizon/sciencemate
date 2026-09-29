"""run_bash + execute_python 安全包装层 —— 高危操作【事前硬拦截】。

背景（2026-06-10 审计 ① 闭环；2026-06-11 扩到 execute_python）：
  harness.yaml:187 有"高危命令红线"prompt 规则 + quality_check 事后审计，
  但二者都不能在 subprocess 启动前阻止破坏性命令——节点真按下 `rm -rf`
  时数据已删，才在 verdict 阶段报 fail。本模块补上唯一缺的一层：**事前拦截**。
  且不止 run_bash：execute_python 能用 os.system / subprocess / shutil.rmtree
  做等价破坏，是绕过 shell 拦截的口子，故一并注册 safe_* 包装工具拦截。

实现方式（符合"只动 nodes/experiment"原则；v0.11 起不再同名覆盖）：
  注册独立工具名 `safe_run_bash` / `safe_write_file` / `safe_execute_python`，
  在任何目录物化或 spawn 前完成 scope、路线、路径和高危检查；
  Bash 与 Python 最终都进入 Core 的强制 Docker RunAttempt；容器内 PID 1
  统一承担日志、进程树和 cgroup 资源边界。"LLM 逃不掉安全层"由
  harness.yaml 工具白名单保证：
  白名单只放 safe 版，builtin 原名不在其中，调了就是 tool not found。
  （v0.11 前用同名覆盖实现，因跨节点全局污染被 tool_registry 弃用，
  deadline=2026-08-01。）

  Python AST 识别只负责对常见外部进程写法给出早期友好错误，不是资源
  安全边界；变量拼接、动态 import 或 C 扩展即使绕过 AST，仍受同一
  RunAttempt 的 PID、内存、时间上限和整树终止约束。

授权语义（v0.11.1 起统一到框架 dangerous_commands，PR #97；scope_guard 的路径授权同样复用 HITL 回流）：
  检测 = 框架 pattern ∪ 节点补充 pattern，命中后同一套流程——
  1. bypass 模式（`HARNESS_BYPASS_DANGEROUS_COMMANDS=1` / chat.py --bypass）→ 直跑留痕
  2. `state.hook_state["highrisk_bash_allowlist"]`（list[str] 子串）—— fixture/编排层
     对特定命令的**事前**精确预授权，
     非 agent 来源，防自我授权
  3. pause 后人工批准 → 一次性消费放行（highrisk_confirm always-on hook 回流）
  4. 都没有 → pause 问人（真实 HITL，与框架 builtin 行为一致）
  旧 `EXPERIMENT_ALLOW_HIGHRISK_BASH` 整 run 粒度 env 授权已删除（会抢在框架门
  之前放行，粒度也差；同类需求用框架 bypass 模式）。

bypass 模式的节点语义（平台产品决策，2026-07-08）：
  `--bypass-permissions` / `HARNESS_BYPASS_DANGEROUS_COMMANDS=1` = 完全无人值守，
  experiment 的可授权环境类 guard 可以让路并留 `*_bypassed` 审计；
  未解析目标、路径角色冲突、源码基线、框架状态和路线有效性均为硬拒，
  不受 bypass 影响；
  非阻断增强（build_env source、完整日志、repro 采集）照常。
"""
from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import json as _json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence
from urllib.parse import unquote, urlsplit

from core.tool_registry import register_tool, _REGISTRY
from shared.lib.platform_env import WINDOWS_SYSTEM_ENV as _WINDOWS_SYSTEM_ENV
from shared.lib.platform_env import system_env_passthrough as _system_env_passthrough
from shared.lib.cancellable_subprocess import (
    group_killer,
    spawn_and_wait,
    the_interpreter_for_model_code,
)
from shared.tools.builtin import _run_bash as _orig_run_bash
from shared.tools.builtin import _write_file as _orig_write_file
from shared.tools.library.python_exec import (
    _missing_glyph_summary,
    _with_matplotlib_font_preamble,
)
try:
    from . import bash_semantics as _bash_semantics
    from . import timeout_escalation as _te
    from .build_resource_guard import (
        build_resource_plan_block, classify_resource_health,
        derive_build_limits, filesystem_free_bytes,
        host_memory_admission_block,
        kill_guarded_tree, parse_build_resource_event,
        wait_for_cgroup_quiescence, wrap_with_cgroup,
        _cgroup_event_deltas, _cgroup_files_snapshot, _host_memory_snapshot,
    )
    from .preflight import EXECUTION_STAGES
    from .run_contract import record_actual_run_params as _record_actual_run_params
    from .path_roles import (
        IMMUTABLE_ROLES,
        collect_path_roles,
        conflicting_role_paths,
        experiment_output_dir,
        default_stage_workdir,
        matching_path_roles,
        project_workspace_dir,
        validate_path_roles,
    )
except ImportError:  # loaded as top-level ``tools.safe_bash`` by node runtime
    from tools import bash_semantics as _bash_semantics
    from tools import timeout_escalation as _te
    from tools.build_resource_guard import (
        build_resource_plan_block, classify_resource_health,
        derive_build_limits, filesystem_free_bytes,
        host_memory_admission_block,
        kill_guarded_tree, parse_build_resource_event,
        wait_for_cgroup_quiescence, wrap_with_cgroup,
        _cgroup_event_deltas, _cgroup_files_snapshot, _host_memory_snapshot,
    )
    from tools.preflight import EXECUTION_STAGES
    from tools.run_contract import record_actual_run_params as _record_actual_run_params
    from tools.path_roles import (
        IMMUTABLE_ROLES,
        collect_path_roles,
        conflicting_role_paths,
        experiment_output_dir,
        default_stage_workdir,
        matching_path_roles,
        project_workspace_dir,
        validate_path_roles,
    )

# tail 截断阈值，与框架 _run_bash 保持一致（stdout 3000 / stderr 1500）
_STDOUT_TAIL = 3000
_STDERR_TAIL = 1500

# 轻量 Python 也进入统一 cgroup；线程环境只写入本次子进程，不能污染宿主。
_SAFE_PYTHON_MEMORY_GB = 4.0
_SAFE_PYTHON_MEMORY_MAX_BYTES = int(
    _SAFE_PYTHON_MEMORY_GB * 1.25 * 1024**3
)
_SAFE_PYTHON_THREAD_LIMIT = 4
_SAFE_PYTHON_THREAD_ENV = (
    "OMP_NUM_THREADS",
    "OMP_THREAD_LIMIT",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
)

# Only these node-constructed values may cross into the Python payload.  This
# deliberately is not derived from ``os.environ``: the harness process may hold
# API keys, proxy credentials, scheduler tokens, or unrelated run-local state.
_SAFE_PYTHON_CHILD_ENV = frozenset({
    "HOME", "LANG", "LC_ALL", "PATH",
    "PYTHONPATH",
    "TMPDIR", "TMP", "TEMP",
    "XDG_CACHE_HOME", "MPLCONFIGDIR", "PYTHONPYCACHEPREFIX",
    "EXPERIMENT_RUN_ROOT", "EXPERIMENT_REPRO_ROOT",
    "EXPERIMENT_REPRO_EVIDENCE", "EXPERIMENT_SOURCE_WORKTREE_ROOTS",
    "EXPERIMENT_SOURCE_PATCH_ROOTS",
    # Windows 系统变量（SYSTEMROOT 等）：`env -i` 会把它们剥掉，缺了它们 python.exe
    # 加载不了 DLL。名单取自 shared.lib.platform_env，不手抄。
    #
    # 不摊开整份 `passthrough_names()`：那份还含 Linux 用户会话总线
    # （XDG_RUNTIME_DIR、DBUS_SESSION_BUS_ADDRESS），是 Core 造墙那一层
    # （systemd-run --user）要的，不是模型代码要的。宿主 AF_UNIX 可达（#845）修好
    # 之前不交给模型子进程（#849 节点侧，用户 2026-09-13 定）；注入点
    # `_command_with_safe_child_env` 按同一份 Windows 名单过滤。
    *_WINDOWS_SYSTEM_ENV,
    *_SAFE_PYTHON_THREAD_ENV,
})


#: 一个路径操作数的词法形状（带引号的整体，或不含分隔符的裸串）。多处
#: 命令解析共用，故与其它模块常量一起定义在前。
_PATH_TOKEN_RE = r"(?:\"[^\"]+\"|'[^']+'|[^\s;&|]+)"


# ── 受控工作目录（2026-08-18）────────────────────────────────────────────────
# 本次执行"在哪个目录里跑"只有一个权威来源：``cwd`` 工具参数。它经
# validate_tool_cwd 解析后交给 create_subprocess_exec(cwd=...)，目录不存在时
# 子进程根本不会 spawn —— 零副作用，天然 fail-closed。
#
# 命令文本里的 `cd` 不是契约：它是 shell 内部行为，可能失败，而失败在
# `bash -c` 的命令列表里既不中断后续语句，也不影响退出码（退出码只反映最后
# 一条命令）。实测事故链：`cd <不存在目录>` 失败 → 源码照写、nvcc 照编、
# 末尾 `ls` 成功 → 工具上报 success，而产物落在继承的 cwd 里，
# 声明工作目录与实际工作目录就此脱节，provenance 断裂。
#
# 因此两道防线：① cwd 参数是权威，事前校验；② 命令文本里位于语句开头的
# `cd` 被改写为进不去就 exit 73，让 fail-open 变 fail-closed。不加全局
# `set -e`：grep 无匹配、which 缺失、探测命令预期非零在诊断里是正常语义，
# 一刀切会把大量正常命令拦腰截断。
WORKDIR_UNAVAILABLE = "required_workdir_unavailable"
DEFAULT_WORKDIR_UNAVAILABLE = "default_workdir_unavailable"
_CD_FAILFAST_RC = 73
_CD_FAILFAST_MARKER = "__HF_REQUIRED_WORKDIR_UNAVAILABLE__"

#: 语句开头、且后面不接 `||` 的 `cd`——`cd x || fallback` 是显式容错写法，
#: 改写它等于破坏调用方本来就写对的语义，故排除。
_CD_STMT_RE = re.compile(
    rf"(?:^|(?<=\n)|(?<=;)|(?<=&&))(\s*)cd\s+((?:--\s+)?{_PATH_TOKEN_RE})\s*(?=$|[\n;]|&&)"
)


def _harden_leading_cd(cmd: str) -> tuple[str, list[str]]:
    """把语句开头的 `cd <dir>` 改写成进不去就退出，返回 (新命令, 被加固的目标)。

    只扫第一个 heredoc 操作符之前的部分：heredoc 正文是**数据**，里面出现的
    `cd` 属于将要写出的文件内容，改写它就是篡改产物。
    """
    if not cmd or "cd" not in cmd:
        return cmd, []
    heredoc = cmd.find("<<")
    head, tail = (cmd[:heredoc], cmd[heredoc:]) if heredoc >= 0 else (cmd, "")
    hardened: list[str] = []

    def _rewrite(m: re.Match) -> str:
        target = m.group(2).strip()
        if target in {"-", "--"} or target.startswith("-") and not target.startswith("--"):
            return m.group(0)
        operand = target[2:].strip() if target.startswith("--") else target
        if not operand:
            return m.group(0)
        hardened.append(operand)
        # 失败路径本身由 bash 的 `cd` 打到 stderr（"cd: <dir>: No such file
        # or directory"），这里只补一个机器可判的标记 + 硬退出；不重复 echo
        # 操作数，免得把 "$VAR" 这类带引号的原始 token 拼进双引号里拼坏。
        return (
            f"{m.group(1)}cd -- {operand} || {{ "
            f"echo '{_CD_FAILFAST_MARKER}' >&2; "
            f"exit {_CD_FAILFAST_RC}; }} "
        )

    return _CD_STMT_RE.sub(_rewrite, head) + tail, hardened


def _default_workdir_failure(stage: str, exc: Exception) -> dict:
    return {
        "status": "error",
        "reason": DEFAULT_WORKDIR_UNAVAILABLE,
        "blocker": {"kind": DEFAULT_WORKDIR_UNAVAILABLE, "stage": stage},
        "error": (
            "⛔ 默认工作目录无法从 stage 结构化派生；未执行任何命令或代码。\n"
            f"stage={stage}\n原因：{type(exc).__name__}: {exc}"),
    }


def _default_run_workdir(state: Any) -> str:
    """调用方没给 cwd 时的权威工作目录：本 run 的 run_root。

    框架的 ``validate_tool_cwd(state, None)`` 给的是 workspace_root —— 那是节点
    所有权边界，不是可写应用角色。详见 ``_exec_and_log`` 里改用本函数的原因。
    物化失败时退回框架默认值：默认目录取不到不该让命令直接失败，宽根下的既有行为
    仍然是可用的（只是墙更弱），由路径角色门继续把关。
    """
    try:
        return str(experiment_output_dir(state, "runtime", create=True))
    except Exception:
        from core.project_workspace import validate_tool_cwd

        return str(validate_tool_cwd(state, None))


def resolve_required_workdir(state: Any, cwd: str | None,
                             kind: str = "命令") -> tuple[str | None, dict | None]:
    """解析 ``cwd`` 参数为本次执行的权威工作目录；不可用则返回事前拒绝结果。

    返回 ``(workdir, error)``：error 非 None 时调用方必须直接返回它，**不得**
    启动任何子进程 —— 不变量是"工作目录不可用 ⇒ 副作用为 0 ⇒ 状态非 success"。

    shell 与 python 两个执行工具共用本函数：目录不存在时它们的反应必须一致，
    否则同一件事在两个工具上语义相反（bash 拒绝、python 静默建目录），
    agent 被 bash 拦下后换 python 就能跑通，规则也就不再是规则。
    """
    if cwd is None or not str(cwd).strip():
        return None, None
    from core.project_workspace import validate_tool_cwd
    try:
        resolved = str(validate_tool_cwd(state, str(cwd)))
    except Exception as e:
        # 路径角色只描述节点语义，不等于宿主 OS 写能力。外部共享盘目录必须同时
        # 命中可写角色，并由 Core 写根或一次性人工批准签发本地能力。
        candidate = _norm_path(str(cwd))
        roles = [
            role for role in matching_path_roles(candidate, state)
            if role.writable and not role.container_only
        ]
        if roles:
            try:
                from .subprocess_policy import path_has_local_write_capability
            except ImportError:  # pragma: no cover - node runtime import style
                from tools.subprocess_policy import path_has_local_write_capability
            if path_has_local_write_capability(state, candidate):
                resolved = candidate
            else:
                return None, {
                    "status": "error",
                    "reason": "path_capability_required",
                    "blocker": {
                        "kind": "path_capability_required",
                        "target": candidate,
                    },
                    "error": (
                        f"⛔ 路径角色仅声明了语义边界，尚未获得宿主写能力（{kind}未执行）。\n"
                        f"cwd={cwd}\n解析为：{candidate}\n\n"
                        "需要通过人工确认签发该路径的 run-local 写能力；"
                        "不能仅凭 agent 自行声明的 path_role 扩大 OS 权限。"),
                }
        else:
            return None, {
                "status": "error",
                "reason": WORKDIR_UNAVAILABLE,
                "blocker": {"kind": WORKDIR_UNAVAILABLE, "target": str(cwd)},
                "error": (
                    f"⛔ 声明的工作目录不可用（事前拦截，{kind}未执行，无任何副作用）。\n"
                    f"cwd={cwd}\n原因：{type(e).__name__}: {e}\n\n"
                    "工作目录必须落在本 run 的目录内，或落在已声明且可写的 "
                    "run_root / build_root / source_worktree_root / source_patch_root 中。"),
            }
    if not os.path.isdir(resolved):
        return None, {
            "status": "error",
            "reason": WORKDIR_UNAVAILABLE,
            "blocker": {"kind": WORKDIR_UNAVAILABLE, "target": resolved},
            "error": (
                f"⛔ 声明的工作目录不存在（事前拦截，{kind}未执行，无任何副作用）。\n"
                f"cwd={cwd}\n解析为：{resolved}\n\n"
                "工作目录不会被本次调用顺带创建：先用一条只负责建目录的命令"
                "（`mkdir -p <dir>`，不传 cwd 或传其已存在的父目录），"
                "确认成功后再以该目录作为 cwd 发起真正的构建/运行。\n"
                "顺带创建看着方便，代价是 cwd 打错一个字母也照跑不误 —— "
                "产物落进一个谁都没打算创建的目录，且没有任何提示。"),
        }
    return resolved, None


async def _communicate_bounded_probe(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> tuple[bytes, bytes]:
    """有界读取内部探针；任何异常或取消都先 kill+wait，禁止监督器泄漏 PID。"""

    async def _reap() -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            # 重新 communicate 而非只 wait：必须同时收束 stdout/stderr transport，
            # 否则进程虽死，事件循环关闭时仍会出现未关闭 pipe 警告。
            await asyncio.wait_for(
                asyncio.shield(proc.communicate()), timeout=2,
            )
        except (asyncio.TimeoutError, ProcessLookupError, RuntimeError):
            try:
                await asyncio.wait_for(
                    asyncio.shield(proc.wait()), timeout=2,
                )
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
        await asyncio.sleep(0)

    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        reap_task = asyncio.create_task(_reap())
        while not reap_task.done():
            try:
                await asyncio.shield(reap_task)
            except asyncio.CancelledError:
                continue
        await reap_task
        raise


def _attach_diagnosis(result: dict, *, stdout: str, stderr: str) -> None:
    """失败的命令把配置规则（diagnose_patterns.yaml）的修法直接放进工具结果（049-3 接线）。

    模型在看到失败的同一条结果里就有"修法 / 上下文"，不依赖某个 hook 恰好扫到日志
    （Codex 049/01 实测 generic_failure_detector 只 glob state.root，活体里扫不到
    runtime/logs）。纯信息：不改 status / returncode，任何门都不读它。
    """
    if result.get("returncode") == 0:
        return
    try:
        try:
            from .diagnose import configured_guidance
        except ImportError:  # pragma: no cover - node runtime import style
            from tools.diagnose import configured_guidance
        guidance = configured_guidance(
            (stderr or "") + "\n" + (stdout or ""), source="stderr+stdout")
    except Exception as exc:  # 诊断永远不能让工具结果失败
        import logging
        logging.getLogger(__name__).warning("attach_diagnosis 跳过: %s", exc)
        return
    if guidance:
        result["diagnosis"] = guidance


async def _bounded_process_wait(
    proc: asyncio.subprocess.Process,
    *,
    state: Any,
    cmd: str,
    timeout: int,
    limits: Any,
    unit: str | None,
    strong_guard: bool,
    workdir: str | None = None,
    workdir_authority: str = "inherited",
) -> dict[str, Any]:
    """有界流式保存输出并监督整棵进程树。

    所有调用只在内存中保留固定大小的 stdout/stderr tail；非只读普通动作
    安装进程组超时/取消和磁盘余量监督，构建/主要运行再用 ``strong_guard``
    叠加 cgroup PID/内存监督。两层共用一个执行器，避免安全语义分叉。
    """
    if strong_guard and not unit:
        raise ValueError("strong_guard 需要 cgroup unit")
    fallback_kill = group_killer(proc)
    stop_event = asyncio.Event()
    stop_reason: dict[str, Any] = {}
    warned: set[str] = set()
    active_warnings: set[str] = set()
    resource_health: dict[str, Any] = {
        "resource_health": "healthy",
        "decision": "continue",
        "decision_reasons": [],
        "active_warnings": [],
        "hard_stop": None,
    }
    probe_failures = {
        "cgroup": 0, "disk": 0, "host_memory": 0,
    }
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    tail_caps = {"stdout": _STDOUT_TAIL * 4, "stderr": _STDERR_TAIL * 4}
    output_lock = asyncio.Lock()
    written = 0
    log_path: Path | None = None
    log_file = None
    digest = hashlib.sha256()
    started = time.monotonic()
    last_evidence: dict[str, Any] = {
        "elapsed_seconds": 0,
        "pids": 0,
        "memory_bytes": 0,
        "memory_growth_per_second": 0.0,
        "swap_bytes": 0,
        "host_swap_free_bytes": None,
        "host_swap_total_bytes": None,
        "cgroup_memory_psi": None,
        "host_memory_psi": None,
        "cgroup_events": {},
        "cgroup_event_deltas": {},
        "log_bytes": 0,
        "average_log_bytes_per_second": 0,
        "pid_growth_per_second": 0.0,
        "disk_free_bytes": None,
        "host_memory_available_bytes": None,
    }
    previous_probe_at = started
    previous_pids = 0
    previous_memory: int | None = None
    previous_cgroup_events: dict[str, int] = {}

    try:
        if getattr(state, "root", None) is not None:
            seq = state.hook_state.get("_bash_seq", 0) + 1
            state.hook_state["_bash_seq"] = seq
            command_sha8 = hashlib.sha256(cmd.encode("utf-8")).hexdigest()[:8]
            logdir = experiment_output_dir(state, "runtime/logs", create=True)
            log_path = logdir / f"{seq:04d}_{command_sha8}.log"
            log_file = log_path.open("wb")
            header = (
                f"# cmd: {cmd}\n"
                f"# workdir: {workdir or ''}\n"
                f"# workdir_authority: {workdir_authority}\n"
                "# execution_supervisor: bounded_stream_process_group\n"
                + (
                    "# resource_guard: cgroup_v2 + disk_pressure_monitor\n"
                    if strong_guard else
                    "# resource_guard: disk_pressure_monitor\n"
                )
            ).encode("utf-8", errors="replace")
            log_file.write(header)
            digest.update(header)
            written = len(header)
    except Exception:
        if log_file is not None:
            log_file.close()
        log_file = None
        log_path = None

    if getattr(state, "root", None) is not None and log_file is None:
        if strong_guard:
            await asyncio.to_thread(
                kill_guarded_tree, unit, fallback_kill
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            return {
                "status": "error",
                "reason": "build_log_unavailable",
                "error": "构建日志无法创建，已终止整个 cgroup，未降级为无日志执行。",
                "blocker": {"kind": "build_log_unavailable"},
                "resource_guard": {**limits.public(), "unit": unit},
                "execution_supervisor": "bounded_stream_process_group",
            }
        try:
            state.append_transcript(
                "execution_log_unavailable",
                cmd_preview=cmd[:200],
                action="continue_with_bounded_tails",
            )
        except Exception:
            pass

    def _apply_resource_health(health: dict[str, Any]) -> bool:
        nonlocal active_warnings, resource_health
        current = set(health.get("active_warnings") or [])
        added = current - active_warnings
        recovered = active_warnings - current
        warned.update(added)
        for name in sorted(added):
            try:
                state.append_transcript(
                    "build_resource_pressure_warning",
                    resource=name,
                    resource_health=health.get("resource_health"),
                    decision=health.get("decision"),
                    decision_reasons=health.get("decision_reasons") or [],
                    action="continue_with_fast_sampling",
                    unit=unit,
                    cmd_preview=cmd[:200],
                    behavior_evidence=dict(last_evidence),
                )
            except Exception:
                pass
        for name in sorted(recovered):
            try:
                state.append_transcript(
                    "build_resource_pressure_recovered",
                    resource=name,
                    resource_health=health.get("resource_health"),
                    action="normal_sampling",
                    unit=unit,
                    cmd_preview=cmd[:200],
                    behavior_evidence=dict(last_evidence),
                )
            except Exception:
                pass
        active_warnings = current
        resource_health = health
        hard_stop = health.get("hard_stop")
        if (
            isinstance(hard_stop, dict)
            and hard_stop
            and not stop_event.is_set()
        ):
            stop_reason.update(hard_stop)
            stop_event.set()
        return health.get("decision") != "continue"

    def _warn_disk(current: int) -> None:
        if "disk_free_bytes" in warned:
            return
        warned.add("disk_free_bytes")
        try:
            state.append_transcript(
                ("build_resource_pressure_warning" if strong_guard
                 else "execution_disk_pressure_warning"),
                resource="disk_free_bytes", current=current,
                warning=limits.disk_warning_free_bytes,
                emergency_stop=limits.disk_stop_free_bytes,
                action=("analyze_build_behavior" if strong_guard
                        else "analyze_execution_behavior"), unit=unit,
                cmd_preview=cmd[:200],
                behavior_evidence=dict(last_evidence))
        except Exception:
            pass

    def _request_disk_stop(current: int) -> None:
        if stop_event.is_set():
            return
        stop_reason.update(
            reason=("build_disk_reserve_exhausted" if strong_guard
                    else "execution_disk_reserve_exhausted"),
            resource="disk_free_bytes", current=current,
            maximum=limits.disk_stop_free_bytes)
        stop_event.set()

    if log_path is not None:
        initial_disk_free = filesystem_free_bytes(log_path.parent)
        if initial_disk_free is None:
            if strong_guard:
                await asyncio.to_thread(kill_guarded_tree, unit, fallback_kill)
            else:
                fallback_kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            if log_file is not None:
                log_file.close()
            return {
                "status": "error",
                "reason": ("build_disk_probe_unavailable" if strong_guard
                           else "execution_disk_probe_unavailable"),
                "error": "无法读取执行日志文件系统剩余空间，已终止整棵进程树。",
                "blocker": {
                    "kind": ("build_disk_probe_unavailable" if strong_guard
                             else "execution_disk_probe_unavailable")},
                **({"resource_guard": {**limits.public(), "unit": unit}}
                   if strong_guard else {}),
                "execution_supervisor": "bounded_stream_process_group",
            }
        last_evidence["disk_free_bytes"] = initial_disk_free
        if strong_guard:
            _apply_resource_health(classify_resource_health(
                limits, usage={}, disk_free_bytes=initial_disk_free,
                host_snapshot=None, probe_failures=probe_failures,
            ))
        elif initial_disk_free <= limits.disk_warning_free_bytes:
            _warn_disk(initial_disk_free)
        if initial_disk_free <= limits.disk_stop_free_bytes:
            _request_disk_stop(initial_disk_free)

    async def _read_stream(stream: Any, label: str) -> None:
        nonlocal written
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return
            tail = tails[label]
            tail.extend(chunk)
            if len(tail) > tail_caps[label]:
                del tail[:-tail_caps[label]]
            if strong_guard and label == "stderr" and not stop_event.is_set():
                event = parse_build_resource_event(
                    bytes(tail).decode("utf-8", errors="replace"))
                if event is not None:
                    stop_reason.update(event)
                    stop_event.set()
            async with output_lock:
                if log_file is not None:
                    try:
                        log_file.write(chunk)
                        log_file.flush()
                        digest.update(chunk)
                        written += len(chunk)
                    except OSError as exc:
                        if not stop_event.is_set():
                            stop_reason.update(
                                reason=("build_log_write_failed" if strong_guard
                                        else "execution_log_write_failed"),
                                resource="disk_free_bytes",
                                detail=f"{type(exc).__name__}: {exc}")
                            stop_event.set()
                        return

    async def _monitor_cgroup() -> None:
        """共享分类器只凭实测资源证据预警；权威耗尽事实才终止。"""
        nonlocal previous_probe_at, previous_pids, previous_memory
        nonlocal previous_cgroup_events
        while proc.returncode is None and not stop_event.is_set():
            values: dict[str, Any] = {}
            try:
                probe = await asyncio.create_subprocess_exec(
                    "systemctl", "--user", "show", unit,
                    "-p", "MemoryCurrent", "-p", "TasksCurrent",
                    "-p", "ControlGroup",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                raw, _ = await _communicate_bounded_probe(
                    probe, timeout=2
                )
                for line in raw.decode(errors="replace").splitlines():
                    key, separator, value = line.partition("=")
                    if not separator:
                        continue
                    if key in {"MemoryCurrent", "TasksCurrent"}:
                        if value.isdigit():
                            values[key] = int(value)
                    elif key == "ControlGroup" and value:
                        values[key] = value
                control_group = str(values.get("ControlGroup") or "")
                if control_group:
                    values.update(await asyncio.to_thread(
                        _cgroup_files_snapshot, control_group
                    ))
            except Exception:
                # 内核硬限制仍在；瞬时遥测失败累计后只降级健康度，不抢杀。
                pass

            cgroup_ok = any(
                isinstance(values.get(key), int)
                for key in ("MemoryCurrent", "TasksCurrent")
            )
            probe_failures["cgroup"] = (
                0 if cgroup_ok
                else probe_failures["cgroup"] + 1
            )
            host_probe_enabled = (
                limits.host_memory_warning_free_bytes > 0
                or limits.host_memory_stop_free_bytes > 0
            )
            host_snapshot = (
                await asyncio.to_thread(_host_memory_snapshot)
                if host_probe_enabled
                else None
            )
            probe_failures["host_memory"] = (
                0
                if not host_probe_enabled or host_snapshot is not None
                else probe_failures["host_memory"] + 1
            )
            disk_free = (
                filesystem_free_bytes(log_path.parent)
                if log_path is not None
                else None
            )
            probe_failures["disk"] = (
                0
                if log_path is None or disk_free is not None
                else probe_failures["disk"] + 1
            )

            now = time.monotonic()
            elapsed = max(0.001, now - started)
            current_pids = values.get("TasksCurrent")
            current_memory = values.get("MemoryCurrent")
            current_cgroup_events = values.get("CgroupEvents")
            cgroup_event_deltas = (
                _cgroup_event_deltas(
                    previous_cgroup_events, current_cgroup_events,
                )
                if isinstance(current_cgroup_events, dict)
                else None
            )
            if cgroup_event_deltas is not None:
                values["CgroupEventDeltas"] = cgroup_event_deltas
            probe_interval = max(0.001, now - previous_probe_at)
            pid_growth = (
                (current_pids - previous_pids) / probe_interval
                if isinstance(current_pids, int)
                else 0.0
            )
            memory_growth = (
                (current_memory - previous_memory) / probe_interval
                if isinstance(current_memory, int)
                and isinstance(previous_memory, int)
                else 0.0
            )
            last_evidence.update(
                elapsed_seconds=round(elapsed, 3),
                pids=(
                    current_pids
                    if isinstance(current_pids, int)
                    else last_evidence["pids"]
                ),
                memory_bytes=(
                    current_memory
                    if isinstance(current_memory, int)
                    else last_evidence["memory_bytes"]
                ),
                memory_growth_per_second=round(memory_growth, 3),
                swap_bytes=values.get("MemorySwapCurrent", 0),
                log_bytes=written,
                average_log_bytes_per_second=int(written / elapsed),
                pid_growth_per_second=round(pid_growth, 3),
                disk_free_bytes=disk_free,
                cgroup_memory_psi=values.get("MemoryPSI"),
                cgroup_events=values.get("CgroupEvents") or {},
                cgroup_event_deltas=cgroup_event_deltas or {},
            )
            if host_snapshot is not None:
                last_evidence.update(
                    host_memory_available_bytes=host_snapshot.get(
                        "available_bytes"),
                    host_swap_free_bytes=host_snapshot.get(
                        "swap_free_bytes"),
                    host_swap_total_bytes=host_snapshot.get(
                        "swap_total_bytes"),
                    host_memory_psi=host_snapshot.get("memory_psi"),
                )

            health = classify_resource_health(
                limits,
                usage=values,
                disk_free_bytes=disk_free,
                host_snapshot=host_snapshot,
                probe_failures=probe_failures,
                memory_growth_per_second=memory_growth,
            )
            high_pressure = _apply_resource_health(health)
            previous_probe_at = now
            if isinstance(current_pids, int):
                previous_pids = current_pids
            if isinstance(current_memory, int):
                previous_memory = current_memory
            if isinstance(current_cgroup_events, dict):
                previous_cgroup_events = dict(current_cgroup_events)
            try:
                # 压力/临界/遥测退化时加密到 4 Hz；恢复后自动回到 1 Hz。
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=0.25 if high_pressure else 1.0,
                )
            except TimeoutError:
                pass

    async def _monitor_disk() -> None:
        """轻量动作只做文件系统余量监督，不启动 systemctl 采样。"""
        while proc.returncode is None and not stop_event.is_set():
            if log_path is not None:
                disk_free = filesystem_free_bytes(log_path.parent)
                if disk_free is None:
                    stop_reason.update(
                        reason="execution_disk_probe_unavailable",
                        resource="disk_free_bytes",
                    )
                    stop_event.set()
                    return
                if disk_free <= limits.disk_warning_free_bytes:
                    _warn_disk(disk_free)
                if disk_free <= limits.disk_stop_free_bytes:
                    _request_disk_stop(disk_free)
                    return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    readers = [
        asyncio.create_task(_read_stream(proc.stdout, "stdout")),
        asyncio.create_task(_read_stream(proc.stderr, "stderr")),
    ]
    wait_task = asyncio.create_task(proc.wait())
    monitor_task = asyncio.create_task(
        _monitor_cgroup() if strong_guard else _monitor_disk())
    pressure_task = asyncio.create_task(stop_event.wait())
    kill_event = getattr(state, "kill_event", None)
    cancel_task = asyncio.create_task(kill_event.wait()) if kill_event is not None else None
    waiters = [wait_task, pressure_task] + ([cancel_task] if cancel_task else [])
    async def _cleanup_after_external_cancel() -> None:
        """外层 Task.cancel 也必须先回收 payload 整树，再把取消传播给路线层。"""
        stop_event.set()
        cleanup_errors: list[str] = []
        for auxiliary in (monitor_task, pressure_task, cancel_task):
            if auxiliary is not None and not auxiliary.done():
                auxiliary.cancel()
        try:
            if strong_guard:
                await asyncio.to_thread(
                    kill_guarded_tree, unit, fallback_kill
                )
            else:
                fallback_kill()
        except Exception as exc:
            cleanup_errors.append(
                f"kill:{type(exc).__name__}:{exc}"
            )
            try:
                fallback_kill()
            except Exception as fallback_exc:
                cleanup_errors.append(
                    f"fallback_kill:{type(fallback_exc).__name__}:"
                    f"{fallback_exc}"
                )
        try:
            await asyncio.wait_for(
                asyncio.shield(wait_task), timeout=5
            )
        except Exception as exc:
            cleanup_errors.append(
                f"wait:{type(exc).__name__}:{exc}"
            )
            if not wait_task.done():
                wait_task.cancel()

        _, pending_readers = await asyncio.wait(readers, timeout=5)
        for reader in pending_readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await asyncio.gather(
            *[
                task for task in (monitor_task, pressure_task, cancel_task)
                if task is not None
            ],
            return_exceptions=True,
        )

        quiescence = None
        if strong_guard and unit:
            try:
                quiescence = await asyncio.to_thread(
                    wait_for_cgroup_quiescence,
                    str(unit),
                    grace_s=limits.cgroup_quiescence_grace_s,
                )
                if quiescence.get("status") != "quiet":
                    await asyncio.to_thread(
                        kill_guarded_tree, unit, fallback_kill
                    )
                    quiescence = await asyncio.to_thread(
                        wait_for_cgroup_quiescence,
                        str(unit),
                        grace_s=limits.cgroup_quiescence_grace_s,
                    )
                last_evidence["cgroup_quiescence"] = quiescence
            except Exception as exc:
                cleanup_errors.append(
                    f"quiescence:{type(exc).__name__}:{exc}"
                )

        if log_file is not None:
            try:
                log_file.flush()
            except OSError as exc:
                cleanup_errors.append(
                    f"log_flush:{type(exc).__name__}:{exc}"
                )
            log_file.close()
        try:
            state.append_transcript(
                "execution_cancel_cleanup",
                command=cmd[:200],
                process_returncode=proc.returncode,
                cgroup_quiescence=quiescence,
                cleanup_errors=cleanup_errors,
            )
        except Exception:
            pass

    async def _await_with_external_cancel(awaitable: Any) -> Any:
        try:
            return await awaitable
        except asyncio.CancelledError as cancel_exc:
            # shield 只保护清理 task 不被同一次取消连带取消；若又收到取消，
            # 继续等同一个有界清理，最终仍把原始取消传播给上层。
            cleanup_task = asyncio.create_task(
                _cleanup_after_external_cancel()
            )
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            await cleanup_task
            raise cancel_exc

    done, _ = await _await_with_external_cancel(asyncio.wait(
        waiters, timeout=timeout,
        return_when=asyncio.FIRST_COMPLETED,
    ))

    if cancel_task is not None and cancel_task in done:
        status = "cancelled"
    elif pressure_task in done:
        status = "resource_stopped"
    elif wait_task in done:
        status = "done"
    else:
        status = "timeout"
    if status == "done" and strong_guard:
        quiescence = await _await_with_external_cancel(asyncio.to_thread(
            wait_for_cgroup_quiescence,
            str(unit),
            grace_s=limits.cgroup_quiescence_grace_s,
        ))
        last_evidence["cgroup_quiescence"] = quiescence
        if quiescence.get("status") != "quiet":
            stop_reason.update(
                reason=(
                    "build_cgroup_not_quiescent"
                    if quiescence.get("status") == "busy"
                    else "build_cgroup_quiescence_unavailable"
                ),
                resource="pids",
                current=quiescence.get("tasks_current"),
            )
            status = "resource_stopped"
    if status != "done":
        if strong_guard:
            await _await_with_external_cancel(asyncio.to_thread(
                kill_guarded_tree, unit, fallback_kill
            ))
        else:
            fallback_kill()
        try:
            await _await_with_external_cancel(asyncio.wait_for(
                asyncio.shield(wait_task), timeout=5
            ))
        except Exception:
            if not wait_task.done():
                wait_task.cancel()
    for task in readers:
        try:
            await _await_with_external_cancel(
                asyncio.wait_for(task, timeout=5)
            )
        except Exception:
            task.cancel()
    # payload wrapper 的 sentinel 可能与 systemd-run 退出同时到达：先完成
    # wait_task、后由 reader 消费时，不能因先前已暂定 ``done`` 而丢掉资源
    # 耗尽的终态分类。
    if status == "done" and stop_event.is_set() and stop_reason:
        status = "resource_stopped"
    for task in (monitor_task, pressure_task, cancel_task):
        if task is not None and not task.done():
            task.cancel()
    if log_file is not None:
        try:
            log_file.flush()
        except OSError:
            pass
        log_file.close()

    if (
        strong_guard
        and stop_reason.get("failure_class") == "resource_exhaustion"
        and resource_health.get("resource_health") != "exhausted"
    ):
        resource_health = {
            "resource_health": "exhausted",
            "decision": "emergency_stop",
            "decision_reasons": [str(stop_reason.get("reason"))],
            "active_warnings": sorted(
                active_warnings
                | {str(stop_reason.get("resource") or "resource")}
            ),
            "hard_stop": dict(stop_reason),
        }
        active_warnings = set(resource_health["active_warnings"])
        warned.update(active_warnings)

    out = bytes(tails["stdout"]).decode("utf-8", errors="replace")
    err = bytes(tails["stderr"]).decode("utf-8", errors="replace")
    result: dict[str, Any] = {
        "status": status,
        "returncode": proc.returncode,
        "stdout_tail": out[-_STDOUT_TAIL:],
        "stderr_tail": err[-_STDERR_TAIL:],
        "log_bytes": written,
        "truncated": False,
        "behavior_evidence": last_evidence,
        "execution_supervisor": "bounded_stream_process_group",
    }
    _attach_diagnosis(result, stdout=out, stderr=err)
    if strong_guard:
        result["resource_guard"] = {
            **limits.public(),
            "unit": unit,
            "warnings": sorted(warned),
            "active_warnings": sorted(active_warnings),
            "resource_health": resource_health.get("resource_health"),
            "decision": resource_health.get("decision"),
            "decision_reasons": resource_health.get("decision_reasons") or [],
        }
        result["resource_health"] = resource_health.get("resource_health")
        result["resource_decision"] = resource_health.get("decision")
        result["resource_decision_reasons"] = (
            resource_health.get("decision_reasons") or []
        )
    if (
        "disk_free_bytes" in (
            active_warnings if strong_guard else warned
        )
    ):
        result["required_action"] = (
            "analyze_build_behavior" if strong_guard
            else "analyze_execution_behavior"
        )
    elif strong_guard and active_warnings & {
        "host_memory_available_bytes",
        "host_swap_free_bytes",
        "host_memory_psi",
    }:
        result["required_action"] = "wait_or_reschedule_host_memory"
    if log_path is not None:
        result["log_path"] = str(log_path)
        result["log_sha256"] = digest.hexdigest()
    if status == "resource_stopped":
        result.update(
            status="error", reason=stop_reason.get("reason"),
            blocker={
                "kind": (
                    "build_process_tree_not_quiescent"
                    if stop_reason.get("reason") in {
                        "build_cgroup_not_quiescent",
                        "build_cgroup_quiescence_unavailable",
                    }
                    else (
                        "build_resource_pressure" if strong_guard
                        else "execution_resource_pressure"
                    )
                ),
                **stop_reason,
            },
            error=(
                (
                    "顶层进程退出后 cgroup 仍有后代或无法证明已清空，"
                    if stop_reason.get("reason") in {
                        "build_cgroup_not_quiescent",
                        "build_cgroup_quiescence_unavailable",
                    }
                    else (
                        "构建资源接近硬上限，" if strong_guard
                        else "执行日志文件系统接近紧急保留线，"
                    )
                )
                + "已终止整棵进程树；请先分析行为和资源计划，不要直接重试。"
            ))
    return result


async def _bounded_build_wait(
    proc: asyncio.subprocess.Process,
    *,
    state: Any,
    cmd: str,
    timeout: int,
    limits: Any,
    unit: str,
) -> dict[str, Any]:
    """兼容入口：构建测试与调用方继续获得 cgroup 强监督语义。"""
    return await _bounded_process_wait(
        proc,
        state=state,
        cmd=cmd,
        timeout=timeout,
        limits=limits,
        unit=unit,
        strong_guard=True,
    )


def _sandbox_roots_for_payload(
    state: Any,
    cwd: str,
    *,
    sandbox_profile: str,
    authorized_targets: list[str] | None = None,
) -> tuple[list[Path], list[Path]]:
    """Project already-authorized node paths into one Core RunAttempt call."""
    try:
        from .subprocess_policy import bash_sandbox_roots, python_sandbox_roots
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.subprocess_policy import bash_sandbox_roots, python_sandbox_roots

    if sandbox_profile == "bash":
        return bash_sandbox_roots(
            state, cwd, authorized_targets=authorized_targets)
    if sandbox_profile == "python":
        writable, readonly = python_sandbox_roots(
            state, cwd, authorized_targets=authorized_targets)
        # The trusted image intentionally contains no repository checkout.
        # safe Python imports the current code through an explicit read-only
        # mount; never infer this path from the parent process PYTHONPATH.
        repo_root = Path(__file__).resolve().parents[3]
        if repo_root not in readonly:
            readonly.append(repo_root)
        return writable, readonly
    raise ValueError("sandbox_profile must be bash or python")


def _sandbox_roots_error(exc: Exception) -> dict[str, Any]:
    detail = str(exc)
    reason = (
        "path_capability_required"
        if "path_capability_required" in detail
        else "sandbox_path_contract_invalid"
    )
    return {
        "status": "error",
        "reason": reason,
        "error": (
            "Experiment 每调用文件系统能力无法投影到 Core RunAttempt；"
            f"payload 未启动：{type(exc).__name__}: {detail}"
        ),
        "blocker": {"kind": reason, "detail": detail},
    }


def _ensure_hardened_attempt_manifest(state: Any) -> Any:
    """Freeze a capability-backed run superset before the first payload.

    The immutable manifest is a run capability, not a per-command grant.  It
    contains Core roots plus paths that already have a local write capability;
    Landlock still receives only the narrower roots for the current command.
    """
    from core import sandbox
    try:
        from .subprocess_policy import path_has_local_write_capability
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.subprocess_policy import path_has_local_write_capability

    raw_manifest = getattr(state, "sandbox_manifest", None)
    if isinstance(raw_manifest, Mapping):
        declared_profile = sandbox.parse_manifest(raw_manifest).security_profile
    else:
        declared_profile = sandbox.effective_security_profile()
    # 判据是事实（该 attempt 有没有**逐命令**写边界），词沿用 Docker 年代的
    # "hardened"（PR C 后 core 的定义即如此）：原生后端（linux/darwin）逐命令
    # 构造墙，manifest v4 的 profile 恒为 hardened、effective_security_profile()
    # 只在写边界起不来时报 portable —— 此时拒绝是正确的（命令本来就没墙可进）。
    if declared_profile != "hardened":
        raise RuntimeError(
            "hardened_sandbox_profile_required: Experiment per-call roots "
            "require a per-command write boundary; this host cannot bring one "
            "up (no Landlock/seatbelt/bwrap write boundary available).")

    writable, readonly = sandbox.model_tool_roots(state)
    writable, readonly = list(writable or []), list(readonly or [])
    for role in collect_path_roles(state):
        if role.container_only or not role.writable:
            continue
        path = Path(role.path).expanduser().resolve(strict=False)
        if (path.exists()
                and path_has_local_write_capability(state, path)
                and path not in writable):
            writable.append(path)

    hook_state = getattr(state, "hook_state", None)
    approved = (
        hook_state.get("_approved_subprocess_write_roots", [])
        if isinstance(hook_state, dict) else []
    )
    if isinstance(approved, list):
        for value in approved:
            path = Path(str(value)).expanduser().resolve(strict=False)
            if path.exists() and path not in writable:
                writable.append(path)

    repo_root = Path(__file__).resolve().parents[3]
    if repo_root not in readonly:
        readonly.append(repo_root)
    return sandbox.manifest_for(
        state, writable_roots=writable, readonly_roots=readonly)


def _frozen_attempt_capability_gap(state: Any) -> dict[str, Any] | None:
    """Read an existing attempt manifest without adding or widening mounts.

    A local manifest belongs to exactly one RunAttempt. When a child receives
    its parent manifest, this preflight reports the missing child base roots
    before route payload construction reaches Core spawn. It is deliberately
    diagnostic only: Experiment neither clears nor mutates the manifest.
    """
    raw_manifest = getattr(state, "sandbox_manifest", None)
    if not isinstance(raw_manifest, Mapping):
        return None
    try:
        from core import sandbox
        manifest = sandbox.parse_manifest(raw_manifest)
        # 与 _ensure_hardened_attempt_manifest 同一把尺：manifest v4（原生后端）
        # 的 profile 恒为 hardened 且携带冻结 mounts，诊断照常适用；portable
        # （写边界起不来）没有可诊断的能力缺口。
        if manifest.security_profile != "hardened":
            return None
    except Exception:
        # Core emits the authoritative malformed-manifest rejection below.
        return None

    def existing_path(value: Any) -> Path | None:
        try:
            return Path(value).expanduser().resolve(strict=True)
        except (OSError, TypeError, ValueError):
            return None

    workspace_root = existing_path(getattr(state, "workspace_root", None))
    run_root = existing_path(getattr(state, "root", None))
    project_worktree = existing_path(getattr(state, "project_worktree", None))
    required = [
        ("workspace_root", workspace_root),
        ("run_root", run_root),
    ]

    manifest_roots = [(Path(path), mode) for path, mode in manifest.mounts]

    def effective_mode(path: Path) -> str | None:
        matches = [
            (len(root.parts), mode)
            for root, mode in manifest_roots
            if path == root or path.is_relative_to(root)
        ]
        return max(matches)[1] if matches else None

    missing_roles = [
        role for role, path in required
        if path is not None and effective_mode(path) != "rw"
    ]
    if not missing_roles:
        return None

    def mount_scope(path: Path) -> str:
        if workspace_root is not None and path == workspace_root:
            return "workspace_root"
        if run_root is not None and path == run_root:
            return "run_root"
        if project_worktree is not None and path == project_worktree:
            return "project_worktree"
        return "other"

    return {
        "manifest_attempt_id": manifest.attempt_id,
        "manifest_run_id": manifest.run_id,
        "state_run_id": str(getattr(state, "run_id", "") or ""),
        "manifest_scope_matches_state": manifest.run_id == str(
            getattr(state, "run_id", "") or ""
        ),
        "missing_writable_roles": missing_roles,
        "frozen_mounts": [
            {"scope": mount_scope(Path(path)), "mode": mode}
            for path, mode in manifest.mounts
        ],
    }


def _attempt_manifest_error(
    exc: Exception,
    *,
    state: Any | None = None,
    capability_gap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify immutable attempt failures without proposing an unsafe retry."""
    if capability_gap is None and state is not None:
        capability_gap = _frozen_attempt_capability_gap(state)
    detail = str(exc)
    if "hardened_sandbox_profile_required" in detail:
        reason = "hardened_sandbox_profile_required"
        suggested_owner = "framework"
    elif "not frozen into this RunAttempt" in detail:
        reason = "sandbox_capability_not_frozen"
        suggested_owner = "run_owner"
    else:
        reason = "sandbox_manifest_unavailable"
        suggested_owner = "framework"

    inherited_child_manifest = bool(
        reason == "sandbox_capability_not_frozen"
        and capability_gap
        and not capability_gap.get("manifest_scope_matches_state")
    )
    if inherited_child_manifest:
        suggested_owner = "core"
        node_action = "core_reissue_child_attempt_with_bound_workspace_and_run_root"
        retry_policy = "do_not_retry_same_child_run"
        safe_detail = "immutable RunAttempt capability omits required child writable roots"
    elif reason == "sandbox_capability_not_frozen":
        node_action = "start_new_run_with_required_capabilities"
        retry_policy = None
        safe_detail = detail
    else:
        node_action = "provide_hardened_core_sandbox"
        retry_policy = None
        safe_detail = detail

    blocker = {
        "kind": reason,
        "detail": safe_detail,
        "suggested_owner": suggested_owner,
        "node_action": node_action,
    }
    if capability_gap is not None:
        blocker["capability_diff"] = capability_gap
    if retry_policy is not None:
        blocker["retry_policy"] = retry_policy
    return {
        "status": "error",
        "reason": reason,
        "error": (
            "Experiment RunAttempt 能力预检失败，payload 未启动："
            f"{type(exc).__name__}: {safe_detail}"
        ),
        "blocker": blocker,
    }


def _sandbox_limits_for_payload(
    sandbox_profile: str, resource_profile: str | None, timeout: int,
) -> Any:
    from core.sandbox import SandboxLimits, limits_for_profile

    if sandbox_profile == "python":
        # 4 GiB flexible request plus the existing 25% transient allowance.
        # The lower 64-PID request and thread=4 environment are node invariants;
        # Core remains the cgroup owner and may reject a ceiling that cannot fit.
        return SandboxLimits(
            memory_bytes=_SAFE_PYTHON_MEMORY_MAX_BYTES,
            cpus=2.0,
            pids=64,
            walltime_seconds=max(1, int(timeout)),
            storage_bytes=8 * 1024**3,
            storage_entries=100_000,
            output_bytes=16 * 1024**2,
            tmpfs_bytes=256 * 1024**2,
        )
    return limits_for_profile(resource_profile, walltime_seconds=timeout)


def _command_with_safe_child_env(
    cmd: str, child_env: Mapping[str, str] | None,
) -> str:
    """Inject a small node-owned environment without copying harness secrets."""
    if not child_env:
        return cmd
    unknown = sorted(set(child_env) - _SAFE_PYTHON_CHILD_ENV)
    if unknown:
        raise ValueError(
            "unsafe child environment keys: " + ", ".join(unknown))
    # `env -i` 起一个干净环境，只保留下面显式列出的赋值。Windows 上必须把系统
    # 变量（SYSTEMROOT 等）从 os.environ 补回来 —— 否则 python.exe 连网络/加密 DLL
    # 都加载不了。POSIX 上 system_env_passthrough() 是空的，这段是无操作。调用方
    # 传的同名值优先（child_env 覆盖系统默认）。
    #
    # 只补 Windows 系统变量这一份名单，不是 platform_env 以后可能加进来的全部透传名：
    # 用户会话总线（XDG_RUNTIME_DIR、DBUS_SESSION_BUS_ADDRESS）是 Core 造墙那一层
    # （systemd-run --user）要的，不是模型代码要的。宿主 AF_UNIX socket 可达（#845）
    # 修好之前，把总线地址交给模型子进程等于多给一条出墙的路（#849，用户 2026-09-13 定）。
    effective = {
        key: value
        for key, value in _system_env_passthrough().items()
        if key in _WINDOWS_SYSTEM_ENV and key not in child_env
    }
    effective.update(child_env)
    assignments: list[str] = []
    for key in sorted(effective):
        value = str(effective[key])
        if "\x00" in value or len(value) > 8192:
            raise ValueError(f"invalid child environment value: {key}")
        assignments.append(f"{key}={value}")
    # Every argv atom is shell-quoted as data.  ``exec`` removes the wrapping
    # shell before Python starts; no value is evaluated as Bash syntax.
    return shlex.join([
        "exec", "/usr/bin/env", "-i", *assignments,
        "/bin/bash", "-o", "pipefail", "-c", cmd,
    ])


def _execution_timing(started_wall: float, started_monotonic: float) -> dict[str, Any]:
    """命令的起止时间与耗时（收敛任务书 K12、缺陷 #9）：模型要判断「跑了多久、何时结束」时
    不必再自己包 date/time。耗时按单调时钟算，起止时刻按墙钟换算成 UTC。

    PID 没有返回：命令经 shared.lib.cancellable_subprocess.spawn_and_wait 起在受管沙箱里，
    它只交回 (status, returncode, stdout, stderr)，节点侧拿不到进程号（归 shared owner）。
    """
    from datetime import datetime, timezone

    elapsed = max(0.0, time.monotonic() - started_monotonic)
    return {
        "started_at": datetime.fromtimestamp(started_wall, timezone.utc).isoformat(),
        "finished_at": datetime.fromtimestamp(started_wall + elapsed, timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 3),
    }


async def _exec_and_log(
    state: Any,
    cmd: str,
    timeout: int = 600,
    cwd: str | None = None,
    resource_profile: str | None = None,
    *,
    sandbox_profile: str = "bash",
    sandbox_write_targets: list[str] | None = None,
    child_env: Mapping[str, str] | None = None,
    sandbox_roots: tuple[list[Path], list[Path]] | None = None,
    execution_action: dict[str, Any] | None = None,
    execution_decision: dict[str, Any] | None = None,
    execution_route_binding: dict[str, Any] | None = None,
) -> dict:
    """在强制 RunAttempt 容器内执行命令，并落盘可用的 stdout/stderr。

    命令通过 ``spawn_and_wait`` 进入 Core 冻结的 Docker RunAttempt；容器内
    PID 1 负责进程树收敛、walltime、输出和存储上限。沙箱不可用或合同无法
    兑现时，执行在 payload 启动前 fail-closed。

    工作目录：``cwd`` 参数经 validate_tool_cwd 解析后交给容器，是本次执行的
    唯一权威工作目录；命令文本里语句开头的 `cd` 由 ``_harden_leading_cd`` 改写
    为进不去就 exit 73，其失败不再能被后续命令的成功退出码掩盖。
    """
    # 判决拆除·第三波（sb:235 → schema，2026-09-02）：cmd 非空由 safe_run_bash
    # schema 的 minLength 在派发口核一次，这里不再手写。
    # 权威工作目录先于任何 spawn 校验：不可用即零副作用返回。
    spawn_boundary_entered = False
    spawn_observation_attempted = False
    observed_payload_spawned: bool | None = None
    observed_spawn_proof_source: str | None = None
    action_token: dict[str, Any] | None = None

    def _observe_spawn(
        payload_spawned: bool | None,
        proof_source: str,
    ) -> dict[str, Any] | None:
        """Persist the fact owned by this exact ``spawn_and_wait`` boundary."""
        nonlocal spawn_observation_attempted, observed_payload_spawned
        nonlocal observed_spawn_proof_source
        if action_token is None:
            return None
        spawn_observation_attempted = True
        observed_payload_spawned = payload_spawned
        observed_spawn_proof_source = proof_source
        try:
            try:
                from .execution_action_census import (
                    observe_execution_action_spawn,
                )
            except ImportError:  # pragma: no cover - node runtime import style
                from tools.execution_action_census import (
                    observe_execution_action_spawn,
                )
            observation = observe_execution_action_spawn(
                state,
                action_token,
                payload_spawned=payload_spawned,
                job_submitted=False,
                proof_source=proof_source,
            )
        except Exception as exc:
            observation = {
                "status": "error",
                "error_code": "execution_action_census_persistence_failed",
                "reason": "spawn observation could not be persisted",
                "error_type": type(exc).__name__,
            }
        if observation.get("status") == "success":
            return None
        blocked = {
            **observation,
            "status": "error",
            "execution_action_phase": "spawn_observation",
            "payload_spawned": payload_spawned,
            "job_submitted": False,
            "safe_to_retry": False,
            "payload_must_not_rerun": True,
            "do_not_retry_payload": True,
        }
        blocked.setdefault("action_token", action_token)
        blocked.setdefault("missing_phase", "spawn_observation")
        blocked.setdefault("phase_facts", {
            "payload_spawned": payload_spawned,
            "job_submitted": False,
            "proof_source": proof_source,
        })
        blocked.setdefault("next_action", {
            "owner": "experiment_runtime",
            "action": "retry_missing_census_phase_only",
            "phase": "spawn_observation",
            "model_callable": False,
        })
        blocked.setdefault("model_next_action", {
            "action": "report_blocker_and_end_current_run",
            "reason": "runtime reconciliation is not a model tool",
        })
        return blocked

    def _census_block(
        census_error: dict[str, Any],
        execution_outcome: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            **census_error,
            "status": "error",
            "safe_to_retry": False,
            "payload_must_not_rerun": True,
            "do_not_retry_payload": True,
            "execution_outcome": dict(execution_outcome),
        }

    def _terminalize(result: dict[str, Any]) -> dict[str, Any]:
        if action_token is None:
            return result
        terminal_error = _settle_execution_action_census(
            state,
            action_token,
            payload_spawned=observed_payload_spawned,
            proof_source=(
                observed_spawn_proof_source
                or "spawn_and_wait.boundary_unknown"
            ),
            result=result,
        )
        return _census_block(terminal_error, result) if terminal_error else result

    declared_workdir, workdir_error = resolve_required_workdir(state, cwd)
    if workdir_error is not None:
        try:
            state.append_transcript(
                "required_workdir_unavailable", tool="safe_run_bash",
                cwd=str(cwd), cmd_preview=cmd[:200])
        except Exception:
            pass
        return workdir_error
    cmd, hardened_cd = _harden_leading_cd(cmd)
    try:
        from core.project_workspace import validate_tool_cwd

        # 没传 cwd 时落在本 run 的 run_root，而不是框架的 workspace_root。
        #
        # 两个理由，缺一都不足以改默认值：
        # 1. 节点自己的规矩就是这么写的 —— skills/hpc-build/SKILL.md「P3 cwd 正确：
        #    workdir 必须是声明的 build_root 或 run_root」。workspace_root 是框架的
        #    所有权边界，不是应用角色（path_roles.py 对它有同样的表述），拿它当默认
        #    工作目录等于默认值违反本节点的规则。
        # 2. workspace_root 住在 project worktree（只读覆盖）里面，于是它在
        #    core.isolation._native.write_layers 里落进 priority 层 —— bwrap 的绑定
        #    次序是 broad(rw) → readonly(ro) → priority(rw)，priority 在只读之后，
        #    于是「把 workspace_root 绑成可写」会盖掉嵌套在它内部的 source_baseline_root
        #    的 --ro-bind。2026-09-08 实测：不可变基线被成功改写。改用 run_root 后
        #    workspace_root 不必再可写，嵌套只读角色的墙才真正立得住。
        effective_cwd = declared_workdir or _default_run_workdir(state)
        if sandbox_roots is None:
            writable, readonly = _sandbox_roots_for_payload(
                state, effective_cwd, sandbox_profile=sandbox_profile,
                authorized_targets=sandbox_write_targets,
            )
        else:
            writable, readonly = sandbox_roots
        writable, readonly = list(writable), list(readonly)
        effective_path = Path(effective_cwd).resolve(strict=True)
        if not writable:
            raise ValueError("command has no authorized writable root")
        if not any(
            effective_path == root or effective_path.is_relative_to(root)
            for root in [*writable, *readonly]
        ):
            raise ValueError(
                f"sandbox cwd is outside per-call roots: {effective_path}")
        if child_env is not None and sandbox_profile != "python":
            raise ValueError("child_env is reserved for safe_execute_python")
        capability_gap = _frozen_attempt_capability_gap(state)
        if capability_gap is not None:
            return _attempt_manifest_error(
                RuntimeError("writable root was not frozen into this RunAttempt"),
                state=state,
                capability_gap=capability_gap,
            )
        try:
            _ensure_hardened_attempt_manifest(state)
        except Exception as manifest_exc:
            return _attempt_manifest_error(manifest_exc, state=state)
        payload_cmd = _command_with_safe_child_env(cmd, child_env)

        from shared.lib.shell import bash_shell

        started_wall, started_monotonic = time.time(), time.monotonic()
        shell = bash_shell()
        if execution_action is not None or execution_decision is not None:
            if not (
                isinstance(execution_action, dict)
                and isinstance(execution_decision, dict)
            ):
                return {
                    "status": "error",
                    "error_code": "execution_action_census_input_invalid",
                    "reason": "execution action and decision must be supplied together",
                    "payload_spawned": False,
                    "job_submitted": False,
                }
            action_token, admission_error = _begin_execution_action_census(
                state,
                execution_action,
                execution_decision,
                execution_route_binding,
            )
            if admission_error is not None:
                return admission_error
        spawn_boundary_entered = True
        try:
            status, returncode, stdout, stderr = await spawn_and_wait(
                shell, "-o", "pipefail", "-c", payload_cmd,
                state=state,
                timeout=timeout,
                cwd=effective_cwd,
                writable_roots=writable,
                readonly_roots=readonly,
                sandbox_limits=_sandbox_limits_for_payload(
                    sandbox_profile, resource_profile, timeout),
            )
        except BaseException:
            _observe_spawn(None, "spawn_and_wait.raised")
            raise
        payload_spawned = (
            False if status == "spawn_failed"
            else True if status in {"done", "timeout", "cancelled"}
            else None
        )
        _observe_spawn(payload_spawned, "spawn_and_wait.status")
    except BaseException as exc:
        if not spawn_observation_attempted:
            _observe_spawn(
                None if spawn_boundary_entered else False,
                (
                    "spawn_and_wait.boundary_unknown"
                    if spawn_boundary_entered
                    else "spawn_and_wait.not_invoked_exception"
                ),
            )
        terminal_error = (
            _settle_execution_action_census(
                state,
                action_token,
                payload_spawned=observed_payload_spawned,
                proof_source=(
                    observed_spawn_proof_source
                    or "spawn_and_wait.boundary_unknown"
                ),
                error=exc,
            )
            if action_token is not None
            else None
        )
        if not isinstance(exc, Exception):
            raise
        if (
            "path_capability_required" in str(exc)
            or type(exc).__module__.endswith("subprocess_policy")
        ):
            result = _sandbox_roots_error(exc)
        else:
            result = {
                "status": "error",
                "error": f"启动 shell 失败：{type(exc).__name__}: {exc}",
            }
        return _census_block(terminal_error, result) if terminal_error else result

    if status == "spawn_failed":
        error = stderr.decode("utf-8", errors="replace")
        return _terminalize({
            "status": "error", "error": f"启动 shell 失败：{error[:500]}"})
    timing = _execution_timing(started_wall, started_monotonic)
    if status == "timeout":
        result = _te.build_timeout_payload(
            state, tool="safe_run_bash", cmd=cmd, timeout_s=timeout,
            stdout=stdout, stderr=stderr)
        result.update({
            **timing,
            "safe_to_retry": False,
            "retry_guidance": (
                "The command may have partial writes. Inspect outputs before an explicit retry."
            ),
        })
        if b"HARNESS_SANDBOX_LIMIT walltime" in stderr:
            result.update({
                "error_code": "sandbox_resource_exhausted",
                "resource": "walltime",
            })
        return _terminalize(result)

    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    res = {
        "status": "cancelled" if status == "cancelled"
                  else ("success" if returncode == 0 else "error"),
        "cmd": cmd[:200],
        "returncode": returncode,
        "stdout_tail": out[-_STDOUT_TAIL:],
        "stderr_tail": err[-_STDERR_TAIL:],
        "workdir": effective_cwd,
        "workdir_authority": "cwd_param" if declared_workdir else "inherited",
        **timing,
    }
    if returncode in {125, 126, 137, 138} and "HARNESS_SANDBOX_LIMIT" in err:
        resource = {
            125: "storage",
            126: "output",
            137: "memory",
            138: "pids",
        }[int(returncode)]
        res.update({
            "error_code": "sandbox_resource_exhausted",
            "resource": resource,
            "safe_to_retry": False,
            "retry_guidance": (
                "Do not rerun automatically: inspect partial outputs first, then explicitly "
                "resume or select a larger initial resource_profile."
            ),
        })
    if returncode in (126, 127) and "HARNESS_SANDBOX_LIMIT" not in err:
        # 判决拆除·第三波（sb:1855/1868 降格，2026-09-02）：可执行目标不存在/
        # 无执行位不再事前拦整条命令（预测失败墙）；让 bash 用 rc=127/126 拒绝，
        # 同一份诊断附到结果上。与旧墙相反：其前的语句（mkdir/heredoc/编译）
        # **已经生效**，诊断文案如实说明，别让调用方带着「什么都没跑」的错误前提。
        diagnosis = _exec_target_diagnosis(cmd, effective_cwd)
        if diagnosis is not None:
            res["exec_diagnosis"] = diagnosis
            try:
                state.append_transcript(
                    "exec_target_diagnosis", tool="safe_run_bash",
                    returncode=returncode, cmd_preview=cmd[:200], **diagnosis)
            except Exception:
                pass
    if (returncode == _CD_FAILFAST_RC
            and _CD_FAILFAST_MARKER in err and hardened_cd):
        res["status"] = "error"
        res["reason"] = WORKDIR_UNAVAILABLE
        res["blocker"] = {"kind": WORKDIR_UNAVAILABLE, "target": hardened_cd[0]}
        res["error"] = (
            "⛔ 命令内的 `cd` 进入工作目录失败，其后语句未执行。\n"
            f"目标：{hardened_cd[0]}\n"
            "工作目录不是命令文本的一部分：请用 `cwd=<运行目录>` 参数声明它，"
            "命令内改用相对路径；目录尚不存在时先单独 `mkdir -p`。")
        try:
            state.append_transcript(
                "required_workdir_unavailable", tool="safe_run_bash",
                target=hardened_cd[0], source="inline_cd",
                cmd_preview=cmd[:200])
        except Exception:
            pass
    err = err.replace(_CD_FAILFAST_MARKER + "\n", "").replace(
        _CD_FAILFAST_MARKER, "")
    res["stderr_tail"] = err[-_STDERR_TAIL:]
    _attach_diagnosis(res, stdout=out, stderr=err)

    try:
        if getattr(state, "root", None) is not None:
            seq = state.hook_state.get("_bash_seq", 0) + 1
            state.hook_state["_bash_seq"] = seq
            command_sha8 = hashlib.sha256(cmd.encode("utf-8")).hexdigest()[:8]
            logdir = experiment_output_dir(state, "runtime/logs", create=True)
            lp = logdir / f"{seq:04d}_{command_sha8}.log"
            content = (
                f"# cmd: {cmd}\n# workdir: {effective_cwd}\n"
                f"# workdir_authority: {res['workdir_authority']}\n"
                f"# returncode: {returncode}\n"
                f"# === STDOUT ({len(out)} chars) ===\n{out}\n"
                f"# === STDERR ({len(err)} chars) ===\n{err}\n")
            lp.write_text(content, encoding="utf-8")
            res["log_path"] = str(lp)
            res["command_sha8"] = command_sha8
            res["log_sha256"] = hashlib.sha256(
                content.encode("utf-8")).hexdigest()
            res["truncated"] = (
                len(out) > _STDOUT_TAIL or len(err) > _STDERR_TAIL)
    except Exception:
        pass
    return _terminalize(res)


def match_high_risk(cmd: str) -> str | None:
    """Compatibility export for the shared Experiment classifier."""
    from .execution_guard import classify_high_risk
    return classify_high_risk(cmd, mode="shell")


def _allowlist_authorized(text: str, state: Any) -> str | None:
    """fixture/编排层预授权（非 agent 来源），返回放行来源描述，无则 None。

    v0.11.1：EXPERIMENT_ALLOW_HIGHRISK_BASH 整 run 粒度 env 授权已删除——
    粗粒度且会抢在框架门（PR #97）之前放行；同类需求由框架
    HARNESS_BYPASS_DANGEROUS_COMMANDS 覆盖。allowlist 保留：单命令粒度的
    **事前**预授权，
    框架门暂无对应通道（见 records/issue-headless-hitl.md）。
    """
    try:
        allowlist = state.hook_state.get("highrisk_bash_allowlist") or []
        for allowed in allowlist:
            if allowed and allowed in text:
                return f"allowlist:{allowed[:40]}"
    except Exception:
        pass
    return None


def _approval_preview(
    text: str,
    *,
    mode: str,
    python_effects: PythonEffectProjection | None = None,
) -> str:
    """Put deterministic, human-readable facts before a high-risk preview.

    The framework owns the pause UI and the approval token.  Experiment may
    nevertheless make the existing ``preview`` useful: Python's first 300
    source characters often end halfway through a helper definition and hide
    the actual subprocess argv.  This is deliberately a shallow AST summary,
    not a claim that arbitrary Python has been understood or is harmless.
    """
    if mode != "python":
        return text

    if python_effects is None:
        try:
            tree = ast.parse(text)
        except (SyntaxError, TypeError, ValueError):
            return "【审批摘要（静态提取）】Python 代码无法解析；请查看原始内容确认。\n\n" + text
    else:
        tree = python_effects.syntax_tree
        if tree is None:
            return "【审批摘要（静态提取）】Python 代码无法解析；请查看原始内容确认。\n\n" + text

    def dotted_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = dotted_name(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    def argv_preview(node: ast.AST | None) -> str:
        if not isinstance(node, (ast.List, ast.Tuple)):
            return "<动态命令>"
        values: list[str] = []
        for item in node.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                values.append(item.value)
            else:
                values.append("<动态参数>")
        return shlex.join(values) if values else "<空命令>"

    subprocesses: list[str] = []
    direct_writes: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted_name(node.func)
        if name and name.startswith("subprocess."):
            invocation = argv_preview(node.args[0] if node.args else None)
            if invocation not in subprocesses:
                subprocesses.append(invocation)
        if name in {"os.mkdir", "os.makedirs", "Path.mkdir", "pathlib.Path.mkdir"}:
            if name not in direct_writes:
                direct_writes.append(name)

    lines = ["【审批摘要（由代码静态提取，非 LLM 自述）】"]
    if subprocesses:
        rendered = "；".join(subprocesses[:2])
        if len(subprocesses) > 2:
            rendered += f"；另有 {len(subprocesses) - 2} 个调用"
        lines.append(f"- 将启动子进程：{rendered}")
    else:
        lines.append("- 触发原因：代码包含高危 shell-out 形式；未能提取字面量子进程参数")
    if direct_writes:
        lines.append("- 可见直接写入：" + "、".join(direct_writes)
                     + "（目标可能由变量计算，详见原始内容）")
    lines.append("- 未静态证明子进程的全部副作用；批准绑定下方原始代码的逐字内容。")
    return "\n".join(lines) + "\n\n【原始内容】\n" + text


def _unified_highrisk_gate(state: Any, text: str, *, mode: str,
                           tool: str, kind: str,
                           cwd: str | None = None,
                           python_effects: PythonEffectProjection | None = None,
                           ) -> dict | None:
    """统一高危门（v0.11.1，采纳 core review）：唯一授权语义 = 框架
    dangerous_commands（PR #97）。

    检测 = 框架 pattern ∪ 节点补充 pattern（su/pkexec/fdisk/killall/kill -9/
    git reset --hard/git clean 等框架清单没有的）；命中后全部走同一套流程：
      bypass 模式 → 直跑留痕
      fixture allowlist → 事前预授权放行留痕
      已批准（pause 后人工同意）→ 一次性消费放行
      否则 → pause 问人（highrisk_confirm always-on hook 负责回流批准）
    不再有 EXPERIMENT_ALLOW_HIGHRISK_BASH 旧机制抢跑。

    返回 None=放行继续；dict=拦截（pause payload，调用方直接 return）。
    mode ∈ {"shell","python"}（框架 matcher 口径）；kind ∈ {"bash","python"}
    （transcript 事件名前缀，与框架 builtin 的事件名对齐）。
    """
    from shared.lib import dangerous_commands as _danger
    try:
        from .execution_guard import classify_high_risk
    except ImportError:  # node runtime loads this module as tools.safe_bash
        from tools.execution_guard import classify_high_risk
    category = classify_high_risk(text, mode=mode)
    if category is None:
        return None

    preview_field = "cmd_preview" if kind == "bash" else "code_preview"

    def _ev(name: str, **extra: Any) -> None:
        try:
            state.append_transcript(
                name, **{preview_field: text[:200]}, category=category, **extra)
        except Exception:
            pass

    if _danger.bypass_enabled():
        _ev(f"highrisk_{kind}_bypass")
        return None
    auth = _allowlist_authorized(text, state)
    if auth is not None:
        _ev(f"highrisk_{kind}_allowed", authorized_by=auth)
        return None
    if mode == "shell":
        contained = _contained_cleanup(state, text, cwd)
        if contained is not None:
            _ev(f"highrisk_{kind}_contained", roots=contained)
            return None
    if _danger.is_confirmed(state, text):
        _danger.consume_confirmation(state, text)
        _ev(f"highrisk_{kind}_confirmed_run")
        return None
    _ev(f"highrisk_{kind}_blocked_pending_confirm")
    return _danger.build_pause_payload(
        state, tool=tool, text=text, category=category,
        preview=_approval_preview(
            text, mode=mode, python_effects=python_effects))


def _cleanup_requests(segment: str, base: str, *,
                      opaque: bool = False) -> list[tuple[str, bool]] | None:
    """Destructive operands of one segment as ``(path, removes_that_path)``.

    ``None`` means "cannot prove what this removes".  The boolean separates
    *clearing a directory* from *removing it*: ``rm -rf build/*`` clears
    ``build``, ``rm -rf build`` removes it, and the two need different
    permissions even though they resolve to the same directory.
    """
    try:
        tokens = _strip_write_wrappers(shlex.split(segment, posix=True))
    except ValueError:
        return None
    if not tokens:
        return []
    op = tokens[0].split("/")[-1]
    if op not in _DESTRUCTIVE_OPS:
        return []
    if opaque or _command_is_opaque(segment):
        return None

    if op == "rm":
        operands = _operand_args(tokens)
    elif op == "git":
        # `git clean`/`reset --hard` rewrite a whole worktree; treat the
        # resolved root as cleared, never as removable.
        targets = _git_destructive_targets(tokens, base)
        return None if targets is None else [(t, False) for t in targets]
    elif op == "find":
        targets = _find_destructive_targets(tokens, base)
        return None if targets is None else [(t, False) for t in targets]
    elif op == "rsync" and any(t.startswith("--delete") for t in tokens[1:]):
        operands = _operand_args(tokens)[-1:]
    else:
        return None

    requests: list[tuple[str, bool]] = []
    for token in operands:
        if _has_unresolved_var(token):
            return None
        if any(ch in token for ch in _GLOB_CHARS):
            resolved = _containment_target(token, base)
            if resolved == UNRESOLVED:
                return None
            requests.append((resolved, False))
        else:
            requests.append((_norm_path(token, base), True))
    return requests


# Labels whose *only* hazard is removing files, and which a proven-contained
# target therefore neutralizes.  Everything else — privilege escalation, block
# devices, system control, remote history rewrites — stays a pause no matter
# where it points.
_RELEASABLE_RISK_LABELS = frozenset({
    "递归删除: rm -r/-rf",            # node pattern
    "强清: git clean -fd",             # node pattern
    "硬重置: git reset --hard",        # node pattern
    "递归强制删除 (rm -rf)",           # framework pattern
})


def _all_risk_labels(text: str) -> set[str]:
    """Every high-risk label matching ``text`` — not just the first one.

    Both matchers return on first hit, and ``sudo rm -rf x`` happens to hit the
    deletion pattern first.  Releasing on that single reported category would
    have let privilege escalation ride through on a contained deletion, because
    ``_strip_write_wrappers`` removes the ``sudo`` before the target is read.
    """
    try:
        from .execution_guard import all_shell_risk_labels
        labels = all_shell_risk_labels(text)
    except Exception:
        # Unknown framework surface → assume something unreleasable matched.
        labels = {"<framework-patterns-unavailable>"}
    return labels


def _contained_cleanup(state: Any, cmd: str, cwd: str | None) -> list[str] | None:
    """Roots proving every destructive target stays inside declared, writable space.

    Returns the matched root paths when the whole command is provably
    contained, else ``None`` (the caller then pauses as before).

    This replaces "the command text hashed to something a human approved" with
    "the command's targets are inside a root whose role permits this".  The
    former re-asked whenever a build flag changed; the latter does not care
    about the command text at all, which is why it generalizes past CMake to
    any toolchain.
    """
    if not _all_risk_labels(cmd or "") <= _RELEASABLE_RISK_LABELS:
        return None
    expanded = _expand_cmd_vars(cmd or "")
    base = _command_base_dir(expanded, cwd)
    matched: list[str] = []
    saw_destructive = False
    opaque = _command_is_opaque(expanded)
    conflicts = _conflict_paths(state)
    protected_evidence = _frozen_result_evidence_paths(state)
    for segment in _split_shell_segments(expanded):
        cd_match = re.match(rf"^cd\s+({_PATH_TOKEN_RE})\s*$", segment)
        if cd_match:
            resolved = _lexical_dir(cd_match.group(1), base)
            if resolved is None:
                return None       # cannot follow the cwd → cannot prove anything
            base = resolved
            continue
        requests = _cleanup_requests(segment, base, opaque=opaque)
        if requests is None:
            return None
        for path, removes_root in requests:
            saw_destructive = True
            if any(_under(evidence, path) or _under(path, evidence)
                   for evidence in protected_evidence):
                _record_evidence_retention_block(state, path, protected_evidence)
                return None
            if any(_under(path, c) or _under(c, path) for c in conflicts):
                return None
            roles = matching_path_roles(path, state)
            if not roles or len(roles) > 1:
                return None       # undeclared, or ambiguous at the same root
            role = roles[0]
            # Removing the root itself needs cleanup="root"; clearing its
            # contents needs "contents" or better.  A path *below* a declared
            # root is always just contents of that root.
            delete_root = removes_root and _norm_path(path) == _norm_path(role.path)
            if not role.allows_cleanup(delete_root=delete_root):
                return None
            matched.append(role.path)
    return sorted(set(matched)) if saw_destructive else None


def _frozen_result_evidence_paths(state: Any) -> list[str]:
    """Paths retained by frozen clean results or protected raw-result manifests."""
    out: list[str] = []
    try:
        for summary in state.list_artifacts("clean_results"):
            record = state.read_artifact(summary["id"]) or {}
            metadata = record.get("metadata") or {}
            # 2026-09-11：原先这里还排除 analysis_eligible=False 的记录，而唯一写
            # 那个值的地方是 operation 收尾（恒 False）——等于把最该保护的 operation
            # 证据排除在外。现在只问一件事：这份记录冻结了没有。
            if not metadata.get("frozen"):
                continue
            values = [metadata.get("source_path")] + list(metadata.get("source_paths") or [])
            for value in values:
                if isinstance(value, str) and value.strip():
                    out.append(_norm_path(value))
        for summary in state.list_artifacts("raw_results"):
            record = state.read_artifact(summary["id"]) or {}
            metadata = record.get("metadata") or {}
            if not metadata.get("frozen"):
                continue
            try:
                manifest = json.loads(str(record.get("content") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for item in manifest.get("files", []) if isinstance(manifest, dict) else []:
                if (isinstance(item, dict) and item.get("retention") == "protected"
                        and isinstance(item.get("path"), str) and item["path"].strip()):
                    out.append(_norm_path(item["path"]))
    except Exception:
        return sorted(set(out))
    return sorted(set(out))


def _record_evidence_retention_block(state: Any, target: str, evidence: list[str]) -> None:
    try:
        state.append_transcript(
            "frozen_result_evidence_deletion_blocked", target=target,
            protected_evidence_paths=evidence,
            reason="frozen clean_results or protected raw_results references this evidence",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# scope guard —— 按写入目标路径分级拦截
# ─────────────────────────────────────────────────────────────────────────────

_RESTORE_SUFFIXES = (".bak_conda", ".bak_gfortran", ".bak_scope_guard")
_SOURCE_EXTS = (
    ".F90", ".f90", ".F", ".f", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".py", ".cmake",
)
def _norm_path(path: str, base: str | None = None) -> str:
    p = os.path.expandvars(os.path.expanduser(str(path).strip().strip("\"'")))
    if not os.path.isabs(p):
        p = os.path.join(base or os.getcwd(), p)
    return os.path.abspath(p)


_CMD_VAR_ASSIGN_RE = re.compile(
    r"(?:^|[\n;&|]|\bexport\s+)\s*([A-Za-z_][A-Za-z0-9_]*)="
    r"(\"([^\"]*)\"|'([^']*)'|(\S+))",
    re.M,
)


def _expand_cmd_vars(cmd: str) -> str:
    """把命令文本内定义的 shell 变量（`WORKDIR="/abs/path"`）代入 `$VAR`/`${VAR}` 引用，
    返回仅供 scope_guard 分析用的展开副本（不影响实际执行的命令）。

    背景：os.path.expandvars 只认进程环境变量，命令内自定义变量展不开 →
    `cd "$WORKDIR"` 被当作无效路径回退到 cwd，写目标被错误归类到 cwd 所在
    git tree（2026-07-10/07-13 meiyu e2e 两次误拦的根因之一）。
    """
    if not cmd or "$" not in cmd:
        return cmd
    assigns: dict[str, str] = {}
    for m in _CMD_VAR_ASSIGN_RE.finditer(cmd):
        val = m.group(3) if m.group(3) is not None else (
            m.group(4) if m.group(4) is not None else m.group(5) or "")
        # 值本身可能引用更早定义的变量
        for name, prev in assigns.items():
            val = val.replace(f"${{{name}}}", prev).replace(f"${name}", prev)
        assigns[m.group(1)] = val
    out = cmd
    # 按名字长度倒序替换，避免 $WORK 吃掉 $WORKDIR 的前缀
    for name in sorted(assigns, key=len, reverse=True):
        out = out.replace(f"${{{name}}}", assigns[name])
        out = out.replace(f"${name}", assigns[name])
    return out


def _dedupe_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if not p:
            continue
        try:
            q = os.path.abspath(os.path.expanduser(os.path.expandvars(str(p))))
        except Exception:
            continue
        if q not in out:
            out.append(q)
    return out


def _node_inputs(state: Any) -> dict:
    try:
        return state.hook_state.get("node_inputs") or {}
    except Exception:
        return {}


def _collect_path_values(obj: Any, keys: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                if isinstance(v, str):
                    found.append(v)
                elif isinstance(v, list):
                    found.extend(str(x) for x in v if isinstance(x, str))
            found.extend(_collect_path_values(v, keys))
    elif isinstance(obj, list):
        for x in obj:
            found.extend(_collect_path_values(x, keys))
    return found


def _source_roots(state: Any) -> list[str]:
    """Compatibility helper: only immutable baselines are protected source roots."""
    return _dedupe_paths([
        role.path for role in collect_path_roles(state)
        if role.role in IMMUTABLE_ROLES
    ])


def _conflict_paths(state: Any) -> list[str]:
    """Trees whose path-role declarations conflict; writes there are refused."""
    try:
        return conflicting_role_paths(state)
    except Exception:
        return []


def _global_env_roots() -> list[str]:
    roots: list[str] = ["/opt", "/usr", "/etc"]
    for env_name in ("CONDA_PREFIX", "VIRTUAL_ENV"):
        v = os.getenv(env_name)
        if v:
            roots.append(v)
    home = Path.home()
    for pat in ("miniconda*", "anaconda*", ".conda"):
        roots.extend(str(p) for p in home.glob(pat))
    return _dedupe_paths(roots)


def _artifact_declared_paths(state: Any, keys: set[str]) -> list[str]:
    """Extract explicit path roles from route/prereg artifacts.

    This is intentionally simple and schema-tolerant: official upstream artifacts often
    describe paths in markdown, while declared_route may be YAML-ish or JSON. Only keys
    that name a path role are considered; arbitrary path mentions are ignored.
    """
    paths: list[str] = []
    try:
        artifacts = state.list_artifacts()
    except Exception:
        return paths
    key_alt = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    line_re = re.compile(
        rf"(?:^|\n)\s*(?:[-*]\s*)?(?:{key_alt})\s*[:=]\s*`?([~/][^\s`,'\")]+)",
        re.I,
    )
    for a in artifacts:
        if a.get("type") not in {"declared_route", "pre_registration", "platform_profile"}:
            continue
        try:
            rec = state.read_artifact(a["id"]) or {}
        except Exception:
            continue
        meta = rec.get("metadata") or {}
        paths.extend(str(meta[k]) for k in keys if isinstance(meta.get(k), str))
        content = str(rec.get("content") or "")
        paths.extend(m.group(1) for m in line_re.finditer(content))
        try:
            obj = _json.loads(content)
            paths.extend(_collect_path_values(obj, keys))
        except Exception:
            pass
    return paths


def _under(path: str, root: str) -> bool:
    try:
        p = Path(path).expanduser().resolve(strict=False)
        r = Path(root).expanduser().resolve(strict=False)
        return p == r or r in p.parents
    except Exception:
        path = os.path.abspath(path)
        root = os.path.abspath(root)
        return path == root or path.startswith(root.rstrip("/") + "/")


def _under_framework_data(path: str) -> bool:
    """path 是否在框架数据领地（HARNESS_FRAMEWORK_HOME 或 org home）内。"""
    try:
        from core import paths as _paths
        return _under(path, str(_paths.home())) or _under(path, str(_paths.org_root()))
    except Exception:
        return False


def _git_root(path: str) -> str | None:
    p = Path(path)
    if not p.exists():
        p = p.parent
    for cur in [p, *p.parents]:
        if (cur / ".git").exists():
            return str(cur)
    return None


def _best_matching_root(path: str, roots: list[str]) -> str | None:
    matches = [r for r in roots if r and _under(path, r)]
    if not matches:
        return None
    return max(matches, key=lambda r: len(str(Path(r).expanduser())))


def _is_git_metadata(path: str) -> bool:
    return "/.git/" in path or path.endswith("/.git")


_REMOTE_WRITABLE_ROLES = frozenset({
    "run_root", "build_root", "source_worktree_root",
})
_REMOTE_HOST_VISIBLE_READ_ROOTS = (
    "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/etc", "/proc", "/sys", "/dev",
)


def _declared_remote_visible_roles_hint(state: Any) -> str:
    """回显已声明角色清单并给出两条可执行恢复通道。

    与命令头远端可见性收紧同一提交上线：硬拒必须让 LLM 一两轮就能自动
    纠偏——要么补声明，要么走 stage_in 交付，不留"只能换命令碰运气"的死路。
    """
    entries = [
        f"{role.role}: {role.path}"
        for role in collect_path_roles(state)
        if not role.container_only
    ]
    listing = "；".join(entries) if entries else "（当前没有任何已声明角色）"
    return (
        "\n当前已声明的 remote-visible 角色：" + listing + "\n"
        "下一步二选一：\n"
        "① 该路径确在共享盘且远端可见 → 在 path_roles 声明一个覆盖它的只读 "
        "dependency_root（writable: false）后重新提交；\n"
        "② 该路径只在提交机可见 → 用 submit_job 的 stage_in（按 sha256 钉住"
        "内容）把文件交付到调度器 scratch，命令改引用交付后的目标路径再提交。"
    )


def _classify_remote_visible_path(
    path: str, state: Any,
) -> tuple[str, str]:
    """校验远端进程需要看见的静态路径，不把“可见”误当成“可写”。

    这是提交前的挂载/共享路径契约：只接受已有 path role 覆盖的路径或
    调度器节点本地 scratch。它不授予写权限；真实写入仍由
    _classify_write_path(remote=True) 单独判定。
    """
    if path == UNRESOLVED:
        return "unresolved_target", (
            "远端命令含无法静态解析的路径参数，不能证明执行节点可见")
    if path == REMOTE_SCRATCH:
        return "safe", "execution-host node-local scheduler scratch"

    p = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
    for conflict in _conflict_paths(state):
        if _under(p, conflict) or _under(conflict, p):
            return "invalid_path_role_contract", (
                f"path-role contract conflict involves {conflict}")

    matches = matching_path_roles(p, state)
    semantics = {
        (role.role, role.writable, role.container_only) for role in matches
    }
    if len(semantics) > 1:
        names = ", ".join(sorted(role.role for role in matches))
        return "ambiguous_path_role", (
            f"conflicting roles at same root: {names}")
    if matches:
        role = matches[0]
        if role.container_only:
            return "container_only", (
                f"{role.role} is a logical container, not a remote-visible root: "
                f"{role.path}")
        return "safe", f"declared remote-visible {role.role}: {role.path}"

    for root in _REMOTE_HOST_VISIBLE_READ_ROOTS:
        if _under(p, root):
            return "safe", f"execution-host system path under {root}"

    worktree = getattr(state, "project_worktree", None)
    if _under(p, "/tmp") and not (
            worktree is not None and _under(p, str(worktree))):
        return "safe", "execution-host node-local scratch under /tmp"

    return "remote_path_not_declared", (
        "remote command path is not covered by a declared non-container path role"
        + _declared_remote_visible_roles_hint(state))



def _classify_write_path(path: str, state: Any, *, remote: bool = False) -> tuple[str, str]:
    """Classify by an explicit role, never by a directory name or file suffix."""
    # A target the parser could not pin down is unknown, not absent.  Callers
    # must ask about it; this is the whole point of UNRESOLVED existing.
    if path == UNRESOLVED:
        return "unresolved_target", (
            "命令的写入目标无法静态证明（未展开变量 / $(...) / 循环 / xargs / "
            "source 外部脚本 / 中间层通配符）")

    # 调度器分配的节点本地 scratch。远端作业里这是合法且常见的写入面：它不是
    # 共享盘上的项目数据，作业结束即回收，而且它**永远不可能成为输出根** ——
    # `_scheduler_role_guard` 已把 output_dir / output_paths 限死在
    # build_root / run_root，声明不进去，所以 provenance 不受影响。
    # 提交机上（remote=False）这个变量指向哪里同样不可证明，维持"未知"。
    if path == REMOTE_SCRATCH:
        if remote:
            return "safe", (
                "execution-host node-local scratch "
                f"({'/'.join(sorted(_SCHEDULER_SCRATCH_VARS))} 之一)")
        return "unresolved_target", (
            "调度器 scratch 变量只在远端作业体内可判定；本机执行时它指向哪里"
            "无法静态证明")

    p = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))

    # A broken contract poisons only the trees it names.  Blocking the entire
    # run — including an unrelated run-local log write — left the agent with no
    # command that could succeed and no way to repair the declaration.
    for conflict in _conflict_paths(state):
        if _under(p, conflict) or _under(conflict, p):
            return "invalid_path_role_contract", (
                f"path-role contract conflict involves {conflict}")

    for root in _global_env_roots():
        if _under(p, root):
            return "protected_global_env", f"global environment root: {root}"

    if _is_git_metadata(p):
        return "protected_source_tree", ".git metadata"

    matches = matching_path_roles(p, state)
    semantics = {
        (role.role, role.writable, role.container_only) for role in matches
    }
    if len(semantics) > 1:
        names = ", ".join(sorted(role.role for role in matches))
        return "ambiguous_path_role", f"conflicting roles at same root: {names}"
    if matches:
        role = matches[0]
        if role.role in IMMUTABLE_ROLES:
            return "protected_source_tree", (
                f"immutable source_baseline_root: {role.path}")
        if role.role == "dependency_root" and not role.writable:
            return "protected_dependency", (
                f"read-only dependency_root: {role.path}")
        if role.container_only:
            return "container_only", (
                f"{role.role} is a logical container, not a writable root: "
                f"{role.path}")
        if role.writable:
            if remote and role.role not in _REMOTE_WRITABLE_ROLES:
                return "remote_path_not_declared", (
                    "remote job writes require a declared run_root, build_root, "
                    f"or source_worktree_root, not {role.role}: {role.path}")
            # Project-owned source/build/run/dependency roles must remain in
            # the project workspace. This is a route-validity boundary, so a
            # role declaration or bypass mode cannot redirect builds to ~.
            workspace_root = project_workspace_dir(state)
            if (workspace_root is not None
                    and not remote
                    and not _under(p, str(workspace_root))):
                return "outside_project_workspace", (
                    f"project writes must stay under workspace: {workspace_root}")
            return "safe", f"writable {role.role}: {role.path}"
        return "protected_path_role", f"read-only {role.role}: {role.path}"

    # Core owns the framework run root (artifacts/transcript/checkpoints).  A
    # node command may write only a more specific canonical role within it,
    # such as outputs/experiment/runtime; /tmp fallback must not reopen it.
    state_root = getattr(state, "root", None)
    if state_root and _under(p, str(state_root)):
        return "protected_framework_state", (
            f"framework state root is not an application workspace: {state_root}")

    # /tmp is available for incidental process temporaries, but never overrides
    # a more specific declared baseline/dependency/container role.
    #
    # remote 下这条同样生效，且这是**有意的**，不是判据顺序的副产品：远端 /tmp
    # 是执行主机的节点本地 scratch，不是共享盘上的项目数据，作业结束即回收；
    # 它也永远成不了输出根（output_dir / output_paths 由 _scheduler_role_guard
    # 限死在 build_root / run_root）。同理见 REMOTE_SCRATCH —— `$TMPDIR` 走的是
    # 同一条判断，只是它的值要到运行期才知道。
    worktree = getattr(state, "project_worktree", None)
    if _under(p, "/tmp") and not (
            worktree is not None and _under(p, str(worktree))):
        return "safe", (
            "execution-host node-local scratch under /tmp" if remote
            else "unclaimed process temporary path under /tmp")

    # Undeclared git trees remain protected as a conservative compatibility
    # fallback. It does not grant permissions and is never used to override a
    # canonical role.
    if not _under_framework_data(p):
        gr = _git_root(p)
        if gr:
            return "protected_source_tree", f"git working tree: {gr}"
    if remote:
        return "remote_path_not_declared", (
            "remote job write target is not under a declared execution root")
    if os.path.isabs(path):
        return "unknown_absolute", "absolute path is not declared writable"
    return "unknown_relative", "relative path outside declared writable root"


def _worktree_requires_version_control(path: str, state: Any) -> str | None:
    for role in matching_path_roles(path, state):
        if role.role != "source_worktree_root":
            continue
        if (Path(role.path).exists()
                and not (Path(role.path) / ".git").exists()):
            return (
                "source_worktree_root must be the root of a Git worktree before writes "
                "so every source change can be reverted and reproduced")
    return None


_WORKTREE_FINGERPRINT_KEY = "_source_worktree_fingerprints"
_WORKTREE_SANDBOX_DEGRADED_KEY = "_source_worktree_audit_sandbox_degraded"
_WORKTREE_MAX_NEW_FILES = 200
_WORKTREE_MAX_PATCH_BYTES = 8_000_000
_WORKTREE_NEW_FILE_BUDGET_S = 20.0


# ── 来源 worktree 的 Git 配置是外部输入 ──────────────────────────────────────
# source_worktree_root 通常是别人准备好的目录或源码包，`.git/config` 跟着一起来。
# Git 会照它执行外部 diff、textconv、clean/smudge 过滤器和 fsmonitor —— 全是任意
# 命令，且默认继承 harness 的完整环境（含凭据）。这些查询只是审计记录，没有任何
# 理由带着凭据在宿主上无约束地跑。本机实测确认可打通的四条：diff.external、
# diff.<名字>.textconv（都走 `diff --no-index`）、filter.<名字>.clean 与
# core.fsmonitor（都走 `status`）；`--no-ext-diff` 已挡住 diff.<名字>.command。
_GIT_UNSAFE_CONFIG_KEYS = frozenset({
    "core.fsmonitor", "core.hookspath", "diff.external",
    "core.sshcommand", "credential.helper", "core.pager", "core.editor",
})
_GIT_UNSAFE_CONFIG_SUFFIXES = (".command", ".textconv", ".clean", ".smudge",
                               ".process", ".driver")


def _git_audit_env() -> dict[str, str]:
    """审计查询的环境：去掉凭据，钉死配置来源，禁止交互。

    脱敏判据与隔离后端同源（``core.secrets.is_secret_name``），不另抄一份正则。
    """
    from core.secrets import is_secret_name

    env = {k: v for k, v in os.environ.items() if not is_secret_name(k)}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",      # 不读 /etc/gitconfig
        "GIT_CONFIG_GLOBAL": os.devnull,  # 不读 ~/.gitconfig
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_OPTIONAL_LOCKS": "0",        # 不去改来源树里的 .git/index
    })
    return env


def _git_config_neutralizers(worktree: str, timeout: int = 10) -> list[str]:
    """把这个仓库配置里会执行外部命令的键逐个置空。

    Git 没有「禁用全部过滤器」的开关，而 ``filter.<名字>.clean`` 是任意命令、
    名字由配置作者定，静态参数挡不住。读配置本身不执行任何东西，所以先枚举再逐个
    ``-c`` 覆盖；``--list`` 给的是生效后的键，``include.path`` 拉进来的也在内。
    过滤器另配 ``required=false``：只置空 clean 时 required 仓库会 fatal 退出
    （实测过滤器不会执行，安全，但审计记录就空了）。
    """
    # 走 _git_text 自身（neutralize=False 断掉递归），而不是另开一个 subprocess
    # 调用点：本模块对外只保留一个 spawn 口，读配置这一步也受同一套环境脱敏与
    # 沙箱包裹。多开一个调用点还会让根测试
    # tests/test_model_commands_reach_the_os_through_one_throat.py 的
    # 「文件+函数」对账转红，而它对账的 framework_exemptions.yaml 是权限元文件，
    # 本节点改不了。
    #
    # 这一层**要**兜异常，`_git_text` 本体不兜：两者失败的含义不同。读配置失败只是
    # 少了一层防线（静态参数 `--no-ext-diff`/`--no-textconv`、环境脱敏与沙箱都还在），
    # 退化成「无中和器」但审计记录照出；而查询本体失败意味着这次审计**没有记录**，
    # 那必须让调用方看见 —— 三个调用点各自已有 try/except。
    try:
        listing = _git_text(worktree, "config", "--list", "--local", "--name-only",
                            timeout=timeout, neutralize=False)
    except Exception:
        return []
    if not listing:
        return []
    flags: list[str] = []
    filters: set[str] = set()
    for raw in listing.splitlines():
        key = raw.strip()
        low = key.lower()
        if not low:
            continue
        if low in _GIT_UNSAFE_CONFIG_KEYS or low.endswith(_GIT_UNSAFE_CONFIG_SUFFIXES):
            flags += ["-c", f"{key}="]
        if low.startswith("filter."):
            name = key.split(".", 2)[1] if key.count(".") >= 2 else ""
            if name:
                filters.add(name)
    for name in sorted(filters):
        flags += ["-c", f"filter.{name}.required=false"]
    return flags


def _git_text(worktree: str, *args: str, timeout: int = 60,
              state: Any = None, sandbox_root: Any = None,
              neutralize: bool = True) -> str:
    """Run a read-only git query, decoding defensively.

    Old Fortran/C trees routinely carry non-UTF-8 bytes.  Without
    ``errors="replace"`` the decode raises, the caller swallows it, and the
    audit record silently disappears — the one outcome this record exists to
    prevent.  A replaced glyph in a patch is strictly better than no patch.

    两层防线，缺一不可：配置中和让攻击代码根本不执行；原生沙箱兜住没枚举到的
    向量 —— 即便有东西跑起来，也拿不到凭据、出不了网、写不进宿主与来源树。
    """
    neutralizers = _git_config_neutralizers(worktree) if neutralize else []
    argv = ["git", "-C", worktree, *neutralizers, *args]
    env = _git_audit_env()
    # 本模块只保留**一个** spawn 调用点（见下方唯一的 subprocess.run）：沙箱可用
    # 时把它换成后端包裹过的 argv/env，不可用时按原样跑。两条路共用同一次调用，
    # 降级与否只体现在 argv/env 上。
    spawn_argv, spawn_env = argv, env
    if state is not None and sandbox_root is not None:
        try:
            from core.sandbox import SandboxLimits, prepare_attempt_command

            launch = prepare_attempt_command(
                argv, state=state, cwd=Path(sandbox_root),
                writable_roots=[Path(sandbox_root)],
                readonly_roots=[Path(worktree)],
                limits=SandboxLimits(memory_bytes=2 * 1024**3, cpus=1, pids=64,
                                     walltime_seconds=timeout),
                environment={k: env[k] for k in env if k.startswith("GIT_")},
            )
            spawn_argv = list(launch.argv)
            spawn_env = getattr(launch, "env", None) or env
        except Exception as exc:
            # 沙箱起不来不该让审计记录消失：配置中和已经关掉了本机实测可打通的每
            # 一条，环境里也没有凭据。降级照跑，但把降级本身如实记一笔 —— 少一层
            # 防线是事实，不能让记录看起来跟两层都在时一样。每 run 报一次即可。
            try:
                if not state.hook_state.get(_WORKTREE_SANDBOX_DEGRADED_KEY):
                    state.hook_state[_WORKTREE_SANDBOX_DEGRADED_KEY] = True
                    state.append_transcript(
                        "source_worktree_audit_sandbox_unavailable",
                        reason=f"{type(exc).__name__}: {exc}",
                        mitigation="git config neutralized and credentials scrubbed; "
                                   "native isolation layer not applied")
            except Exception:
                pass
    # 这里**不**兜异常：三个调用点（safe_run_bash / safe_write_file /
    # safe_execute_python）都已经各自包了 try/except，在此再吞一层只会把「探针失败」
    # 从调用方看得见的异常，变成一条静默的空审计记录 —— 正是本函数 docstring 说的
    # 「这段代码存在要防的唯一后果」。
    return subprocess.run(
        spawn_argv, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        env=spawn_env,
    ).stdout


def _record_source_worktree_diffs(state: Any, cmd_preview: str) -> None:
    """Persist a compact current Git patch after a mutating tool call.

    Runs after every mutating call, so it must stay cheap when nothing moved:
    the two cheap queries (diff + status) form a fingerprint, and an unchanged
    worktree returns before the per-new-file scan.
    """
    if _is_read_only_shell_command(cmd_preview, state):
        return
    roles = collect_path_roles(state)
    worktrees = [
        role for role in roles
        if role.role == "source_worktree_root" and Path(role.path).exists()
    ]
    if not worktrees:
        return
    patch_roles = [
        Path(role.path) for role in roles
        if role.role == "source_patch_root" and role.writable
    ]
    record_root = patch_roles[0] if patch_roles \
        else experiment_output_dir(state, "repro/source_patches")
    record_root.mkdir(parents=True, exist_ok=True)
    try:
        fingerprints = state.hook_state.setdefault(_WORKTREE_FINGERPRINT_KEY, {})
    except Exception:
        fingerprints = {}

    for worktree in worktrees:
        if not (Path(worktree.path) / ".git").exists():
            continue
        tracked = _git_text(worktree.path, "diff", "--binary", "--no-ext-diff",
                            "--no-textconv", "--", ".",
                            state=state, sandbox_root=record_root)
        status = _git_text(worktree.path, "status", "--porcelain=v1",
                           "--untracked-files=normal", "--", ".",
                           state=state, sandbox_root=record_root)
        if not tracked and not status:
            continue

        # 编译循环里绝大多数命令不动源码：指纹没变就不再扫新文件、不重写 patch。
        fingerprint = hashlib.sha256(
            (tracked + "\0" + status).encode("utf-8", "replace")).hexdigest()
        if fingerprints.get(worktree.path) == fingerprint:
            continue

        # 新增的源码/构建配置也要进 patch，但绝不把任意构建产物抄进审计记录。
        body = tracked
        new_files = 0
        truncated = False
        deadline = time.monotonic() + _WORKTREE_NEW_FILE_BUDGET_S
        for rel in _git_text(worktree.path, "ls-files", "--others",
                             "--exclude-standard", "--", ".",
                             state=state, sandbox_root=record_root).splitlines():
            candidate = Path(worktree.path) / rel
            try:
                if (not candidate.is_file()
                        or candidate.stat().st_size > 5_000_000
                        or not (candidate.name == "CMakeLists.txt"
                                or candidate.name.lower().startswith("makefile")
                                or candidate.suffix in _SOURCE_EXTS)):
                    continue
            except OSError:
                continue
            if (new_files >= _WORKTREE_MAX_NEW_FILES
                    or len(body) >= _WORKTREE_MAX_PATCH_BYTES
                    or time.monotonic() > deadline):
                truncated = True
                break
            new_files += 1
            body += _git_text(worktree.path, "diff", "--binary", "--no-index",
                              "--no-ext-diff", "--no-textconv",
                              "--", "/dev/null", rel,
                              state=state, sandbox_root=record_root)

        tag = hashlib.sha256(worktree.path.encode("utf-8")).hexdigest()[:10]
        patch_path = record_root / f"worktree_{tag}.patch"
        status_path = record_root / f"worktree_{tag}.status"
        patch_path.write_text(body, encoding="utf-8")
        status_path.write_text(status, encoding="utf-8")
        fingerprints[worktree.path] = fingerprint
        try:
            state.append_transcript(
                "source_worktree_diff_recorded",
                source_worktree_root=worktree.path,
                patch_path=str(patch_path),
                patch_sha256=hashlib.sha256(
                    body.encode("utf-8", "replace")).hexdigest(),
                status_path=str(status_path),
                new_files=new_files,
                # 截断的 patch 必须自报，否则审计记录会假装自己是完整的。
                truncated=truncated,
                cmd_preview=cmd_preview[:200],
            )
        except Exception:
            pass


def _dependency_install_hint(scope: str, target: str) -> str:
    """装依赖被拦时，告诉模型**被批准的那条路**存在。

    2026-07-31 之前这里只说"dependency_root 默认只读"，没说可以声明成可写。
    实测 run 1785469523-90f86c：节点想装 OpenCL ICD，先试 ~/.local 被拦，
    读完消息判定"用户目录这条路走不通"，于是转去写 /etc 和 sudo apt-get，
    在那里空转 40 余轮。可写 dependency_root 的机制当时**已经存在并可用**，
    只是从没被告知过 —— 缺的是 affordance，不是能力。
    """
    if scope not in {"unknown_absolute", "protected_dependency"}:
        return ""
    try:
        home = str(Path.home()).rstrip("/")
    except (RuntimeError, OSError):
        return ""
    if not target or not target.startswith("/"):
        return ""
    in_home = target == home or target.startswith(home + "/")
    if not in_home or target == home:
        return ""
    return (
        "\n📦 这是用户级依赖安装路径。优先级：\n"
        "  ① 在 run_root 或 build_root 内创建 venv / conda env / spack 环境——首选且隔离；\n"
        "  ② 不要安装到 `~/.local` 或其他账户级目录：项目 run 会拒绝该写入，避免跨项目复用；\n"
        "  ③ 系统级安装（sudo / apt / 写 /etc）不属于项目运行路径，应由平台管理员处理。\n"
    )


_SCOPE_GUARD_EVENT = {
    "bash": ("scope_guard_bash_blocked", "cmd_preview"),
    "write_file": ("scope_guard_write_file_blocked", None),
    "python": ("scope_guard_python_blocked", "code_preview"),
}

# These scopes are environmental boundaries, not integrity boundaries. A human
# may approve one exact command/target when a dependency really must be
# installed outside the run. Undeclared/unresolved paths may be approved only
# through that explicit normal-mode path; bypass never grants a route change.
# Source baselines, framework state, ambiguous role contracts and poisoned
# trees are hard stops as well.
_SCOPE_APPROVAL_SCOPES = frozenset({
    "protected_global_env", "protected_dependency", "protected_path_role",
    "unknown_absolute", "unresolved_target", "path_capability_required",
})

# Approving one of these grants nothing beyond this command: the target is a
# global environment, a read-only role someone declared deliberately, or a
# path that the human explicitly elected to register for this run.
_SCOPE_REGISTRABLE = frozenset({
    "unknown_absolute", "path_capability_required",
})
# Route and target identity are validity facts, not human-confirmation choices.
# A bypass mode can skip an approved job-submission confirmation, but never
# permit an undeclared/unresolved filesystem write to become the run route.
_NON_BYPASSABLE_ROUTE_SCOPES = frozenset({
    "unknown_absolute", "unresolved_target", "path_capability_required",
    "invalid_path_role_contract",
    "ambiguous_path_role", "container_only", "protected_path_role",
    "protected_framework_state",
    "protected_source_tree", "outside_project_workspace", "unknown_relative",
    "remote_path_not_declared", "unversioned_source_worktree",
})
_APPROVED_ROOT_ROLE = "approved_write_root"
_PENDING_REGISTRATION_KEY = "_scope_guard_pending_registration"


def _scope_bypass_allowed(scope: str) -> bool:
    """只有精确、可人工授权的环境边界可以使用 bypass。

    路线身份、框架/源码完整性和无法解析的目标都是有效性事实；
    ``--bypass-permissions`` 不能把这些事实改写成可写路径。
    """
    return (
        scope in _SCOPE_APPROVAL_SCOPES
        and scope not in _NON_BYPASSABLE_ROUTE_SCOPES
    )


def _scope_guard_tool_name(kind: str) -> str:
    """把内部审计类别映射到真实注册工具名，供 pause/resume 同源消费。"""
    return {
        "bash": "safe_run_bash",
        "python": "safe_execute_python",
        "write_file": "safe_write_file",
    }.get(kind, f"safe_{kind}")



def _registration_candidate(target: str, op: str | None) -> str | None:
    """Which directory a human approval should register, if any.

    A file operand registers its parent directory: approving a write to
    ``<dir>/CMakeCache.txt`` means ``<dir>`` is the working area.  Registering
    the file itself would leave the next file in the same directory asking
    again, which is exactly the fatigue this mechanism exists to remove.
    """
    if not target or target == UNRESOLVED or not os.path.isabs(target):
        return None
    if os.path.isdir(target):
        return target
    parent = os.path.dirname(target.rstrip("/"))
    return parent or None


def _register_approved_write_root(state: Any, target: str,
                                  op: str | None) -> str | None:
    """Promote a human-approved path into a durable, run-scoped writable role.

    A one-shot grant keyed on the command text is consumed by the retry and
    then gone, so re-running the same build step with one flag changed asked
    again — measured on a real GROMACS run as one approval per CMake attempt.
    The human was shown a *path* and approved writing to it, so that is what
    gets recorded.  The grant is deliberately narrow: it says "writable, and
    its contents may be cleared"; removing the directory itself still asks,
    and it confers none of build_root/run_root's activity semantics.
    """
    path = _registration_candidate(target, op)
    if path is None:
        return None
    try:
        from .subprocess_policy import register_approved_subprocess_write_root
        register_approved_subprocess_write_root(state, path)
    except Exception:
        return None
    if any(
        role.writable and not role.container_only
        and role.role != _APPROVED_ROOT_ROLE
        for role in matching_path_roles(path, state)
    ):
        # 已有语义角色只缺 OS capability；再叠一层 approved_write_root 会让
        # 同一路径出现冲突角色，反而把批准后的调用永久毒死。
        return path
    try:
        roles = state.hook_state.setdefault("path_roles", {})
        existing = roles.get(_APPROVED_ROOT_ROLE)
        entries = list(existing) if isinstance(existing, list) else (
            [existing] if existing else [])
        for entry in entries:
            known = entry.get("path") if isinstance(entry, dict) else entry
            if known and _norm_path(str(known)) == _norm_path(path):
                return path          # exact capability already recorded
        entries.append({"path": path, "writable": True,
                        "cleanup": "contents", "authority": "human"})
        roles[_APPROVED_ROOT_ROLE] = entries
    except Exception:
        return None
    try:
        state.append_transcript(
            "approved_write_root_registered", path=path, op=op,
            note=("human-approved writable root; contents clearable, "
                  "removing the root itself still asks"))
    except Exception:
        pass
    return path


def _scope_authorization_text(*, kind: str, cmd: str, cwd: str | None,
                              target: str, scope: str, op: str | None) -> str:
    """Build a node-local exact grant key without changing framework APIs."""
    return (
        f"scope_guard:{kind}\ncommand={cmd}\n"
        f"cwd={_norm_path(cwd) if cwd else ''}\n"
        f"target={_norm_path(target)}\nscope={scope}\nop={op or ''}"
    )


def _scope_guard_authorization(
        state: Any, *, kind: str, cmd: str, cwd: str | None,
        target: str, scope: str, reason: str, op: str | None,
        preview: str | None = None,
) -> tuple[bool, dict | None]:
    """Check or request a single-use path authorization.

    Returns ``(True, None)`` when a previously approved exact grant is
    consumed, ``(False, pause/error)`` when the caller must stop, and
    ``(False, None)`` for hard scopes that the caller should report normally.
    """
    if scope not in _SCOPE_APPROVAL_SCOPES:
        return False, None
    from shared.lib import dangerous_commands as _danger
    if _danger.bypass_enabled():
        return False, None
    risk_mode = "python" if kind == "python" else "shell"
    highrisk = bool(_danger.match_high_risk(cmd, mode=risk_mode))
    # For a high-risk command the existing framework confirmation must see the
    # original command so the unified high-risk gate can consume the same
    # approval.  Ordinary path approvals use a richer node-local key that
    # includes cwd/target/scope and cannot widen to another path.
    approval_text = (
        cmd if highrisk else _scope_authorization_text(
            kind=kind, cmd=cmd, cwd=cwd, target=target, scope=scope, op=op))
    if _danger.is_confirmed(state, approval_text):
        if not highrisk:
            _danger.consume_confirmation(state, approval_text)
        registered = None
        if scope in _SCOPE_REGISTRABLE:
            registered = _register_approved_write_root(state, target, op)
        try:
            state.append_transcript(
                "scope_guard_bash_approved_once"
                if kind == "bash" else f"scope_guard_{kind}_approved_once",
                target=target, scope=scope, op=op,
                authorization_key=approval_text,
                registered_root=registered,
            )
        except Exception:
            pass
        return True, None
    return False, _scope_guard_error(
        cmd, scope, reason, target, state=state, kind=kind, op=op,
        preview=preview, approval_required=True, approval_text=approval_text)


def _scope_guard_error(cmd_or_path: str, scope: str, reason: str, target: str,
                       *, state: Any = None, kind: str = "bash",
                       op: str | None = None,
                       preview: str | None = None,
                       approval_required: bool = False,
                       approval_text: str | None = None) -> dict:
    """构造 scope_guard 拦截结果，并在同一处发审计事件。

    事件发射必须跟返回值绑死：2026-07 实测 run 1785469523-90f86c 里三次拦截
    （build_root / unknown_absolute / protected_path）只发出 1 个
    scope_guard_bash_blocked —— 事件是在个别调用点手写的，其余 return 点漏了。
    靠 transcript 做自动审计的人会漏掉 2/3 的拦截。现在只要走这个函数就留痕。

    state=None 时只返回错误、不发事件（少数纯构造场景，如单测直接调用）。
    """
    if state is not None:
        event, preview_field = _SCOPE_GUARD_EVENT.get(
            kind, _SCOPE_GUARD_EVENT["bash"])
        fields: dict[str, Any] = {"target": target, "scope": scope, "reason": reason}
        if preview_field:
            text = preview if preview is not None else str(cmd_or_path)
            fields[preview_field] = text[:200]
        if op is not None:
            fields["op"] = op
        try:
            state.append_transcript(event, **fields)
        except Exception:
            pass
        if approval_required and approval_text:
            from shared.lib import dangerous_commands as _danger
            _danger.register_pending_ask(
                state, tool=_scope_guard_tool_name(kind),
                text=approval_text, category=f"scope_guard:{scope}")
            try:
                state.append_transcript(
                    "scope_guard_approval_requested",
                    target=target, scope=scope, op=op,
                    authorization_key=approval_text,
                )
            except Exception:
                pass
            # What approval grants differs by scope, and the human must be told
            # which one they are answering.  A plain undeclared path becomes a
            # run-scoped writable root so the same directory stops asking; every
            # other scope stays bound to this one command.
            registrable = scope in _SCOPE_REGISTRABLE
            grant_root = (_registration_candidate(target, op)
                          if registrable else None)
            if grant_root:
                question = (
                    f"⚠️ 命令要写入未声明的路径。是否把 {grant_root} "
                    "登记为本次实验的可写目录？")
                grant_note = (
                    f"批准后果（仅本 run 有效）：\n"
                    f"  ✅ {grant_root} 及其子路径可写，后续不再逐条询问\n"
                    f"  ✅ 允许清空该目录的**内容**（构建缓存重配等）\n"
                    f"  ❌ 删除 {grant_root} 目录本身仍会再次询问\n"
                    f"  ❌ 不授予父目录、兄弟目录或任何子树之外的权限\n"
                    f"  ❌ 不获得 build_root/run_root 的活动语义\n")
            else:
                question = (
                    f"⚠️ 命令要写入受保护路径（{scope}），是否只允许这条命令执行一次？")
                grant_note = (
                    "批准只会绑定本条命令、当前 cwd 和该目标路径，使用一次后失效；"
                    "拒绝或修改命令后不会放行。\n")
            return {
                "status": "pause",
                "pause_event": {
                    "question": question,
                    "context": (
                        f"工具：{_scope_guard_tool_name(kind)}\n"
                        f"目标路径：{target}\n作用域：{scope}\n原因：{reason}\n"
                        f"操作：{op or 'write'}\n"
                        f"完整内容（前 500 字符）：\n"
                        f"{(preview if preview is not None else str(cmd_or_path))[:500]}\n\n"
                        + grant_note +
                        "回复“批准”/“同意”即可。"
                    ),
                    "options": [
                        "批准并登记该目录" if grant_root else "批准执行一次",
                        "拒绝",
                    ],
                    "asking_node_type": state.node_type,
                    "asking_run_id": state.run_id,
                    "metadata": {
                        "type": "scope_guard_confirm",
                        "tool": _scope_guard_tool_name(kind),
                        "scope": scope, "target": target,
                        "registers_root": grant_root,
                        "authorization_key": approval_text,
                    },
                },
            }
    return {
        "status": "error",
        # 结构化拦截标识：下游 hook 据此精确判重，不必去正则解析中文错误正文。
        # repeated_error_detector 的指纹是给编译错误设计的（靠 symbol/source 定位），
        # guard 拦截没有这些字段 → 旧逻辑一律 confidence=low → 永不硬计数。
        # 同一 (scope, target) 重复出现是确定性事实，直接给高置信度 key。
        "blocker": {
            "kind": "scope_guard", "scope": scope, "target": target,
            **({"op": op} if op is not None else {}),
        },
        "error": (
            "⛔ 命令被 experiment scope_guard【事前拦截】，未执行。\n"
            f"目标路径：{target}\n"
            f"作用域：{scope}\n"
            f"原因：{reason}\n\n"
            "写权限只由路径角色决定：source_worktree_root/source_patch_root/"
            "build_root/run_root 可写；experiment_root 仅作容器；"
            "source_baseline_root/dependency_root 默认只读。\n"
            "include/lib 冲突应通过本 run 的环境变量、CPATH/CMAKE_PREFIX_PATH/"
            "PKG_CONFIG_PATH/LD_LIBRARY_PATH 或 case-local 配置解决，不得移动、重命名或删除 "
            "conda/system 文件。若确需越界修改，工具会暂停并请求这条命令的路径级一次性批准；"
            "源码基线和框架状态目录永远不能授权。\n\n"
            "✅ 标准改写模式：在 run/workspace 运行目录内工作——用 `cwd=<运行目录>` 参数"
            "声明工作目录，命令内一律用相对路径；外部大数据/安装目录用只读 symlink 链进运行目录。\n"
            "⚠️ 不要靠命令文本里的 `cd` 表达工作目录，更不要为绕过拦截把它整个去掉："
            "许多科学程序从进程 cwd 读取输入并写日志，工作目录错了会在错误目录启动或静默失败；"
            "而 `cd` 是可失败的普通语句，只有 `cwd` 参数能保证目录不可用时命令根本不启动。\n"
            + _dependency_install_hint(scope, target) +
            f"\n原始操作：{str(cmd_or_path)[:300]}"
        ),
    }


def _command_base_dir(cmd: str, cwd: str | None) -> str:
    """Resolve the cwd a command's relative paths are interpreted against.

    ``cd`` operands compose, so they are applied in order against the running
    base rather than each being taken as an absolute answer.
    """
    base = _norm_path(cwd) if cwd else os.getcwd()
    cd_re = re.compile(rf"(?:^|[\n;&|])\s*cd\s+({_PATH_TOKEN_RE})")
    for m in cd_re.finditer(cmd or ""):
        resolved = _lexical_dir(str(m.group(1)), base)
        if resolved:
            base = resolved
    return base


def _split_shell_segments(cmd: str) -> list[str]:
    return [s.strip() for s in re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd or "") if s.strip()]


def _strip_env_prefix(tokens: list[str]) -> list[str]:
    out = list(tokens)
    while out and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0]):
        out.pop(0)
    return out


def _strip_write_wrappers(tokens: list[str]) -> list[str]:
    """Expose the actual mutating command for target extraction.

    ``sudo tee /etc/...`` is a common dependency-install form.  The old
    parser saw ``sudo`` as the command and missed the path, so scope_guard
    returned a generic hard error instead of offering an exact approval.
    This helper is intentionally limited to command wrappers; it does not
    attempt to interpret arbitrary shell syntax.
    """
    out = _strip_env_prefix(tokens)
    while out and out[0] in {"sudo", "doas"}:
        out = out[1:]
        while out and out[0].startswith("-"):
            flag = out.pop(0)
            if flag in {"-u", "--user", "-g", "--group", "-p", "--prompt"} and out:
                out.pop(0)
    return out


def _restore_mv_allowed(tokens: list[str], base: str) -> bool:
    if not tokens or tokens[0] != "mv":
        return False
    args = [t for t in tokens[1:] if not t.startswith("-")]
    if len(args) != 2:
        return False
    src = _norm_path(args[0], base)
    dst = _norm_path(args[1], base)
    return any(src.endswith(suf) and dst == src[:-len(suf)] for suf in _RESTORE_SUFFIXES)


def _pathish(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token in {"|", ">", ">>", "2>", "2>>", "&>", "<"}:
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
        return False
    return (
        token.startswith(("/", "~", "."))
        or "/" in token
        or token.endswith(_SOURCE_EXTS)
        or any(token.endswith(suf) for suf in _RESTORE_SUFFIXES)
    )


# ── UNRESOLVED：目标存在但无法被证明落在哪里 ──────────────────────────────
# 这是第三种结局，不是"没有目标"。旧逻辑只有"有目标→判定"和"无目标→放行"两档，
# 于是 `rm -rf *`（glob 不是 pathish）、`rm -rf build`（裸名不含 /）、
# `rsync --delete`、`find . -delete` 四类命令都提取到空列表，被静默放行——
# 在 baseline cwd 里执行也一样放行。现在提取不出可证明的路径就归入 UNRESOLVED，
# 由调用方按"未知"处理（询问），绝不放行。
UNRESOLVED = "<unresolved>"

# ── REMOTE_SCRATCH：调度器定义的节点本地临时目录 ─────────────────────────────
# `$TMPDIR` 这类变量的值只有运行期才知道，所以静态看它和任何未展开变量一样不可
# 证明 —— 但它**是谁**是确定的：调度器在计算节点上给这个作业分配的本地 scratch。
#
# 分开成一个独立结局，是因为字面 `/tmp` 一直被放行（"unclaimed process temporary
# path"），而 `$TMPDIR` 被归入 UNRESOLVED 拒掉。净效果是护栏在教模型把 `/tmp`
# 写死 —— 而很多集群的 `/tmp` 是小容量 tmpfs，`$TMPDIR` 才指向节点本地 NVMe，
# 写大 scratch 应该走后者。规则不该把人往差的写法上推。
#
# 只认这几个**调度器自己定义**的名字，只认它们出现在路径首段、且其后不再有别的
# `$` 引用；不是通用变量展开。远端之外（remote=False）仍按未知处理：提交机上
# 这个变量指向哪里，本进程同样无法证明。
REMOTE_SCRATCH = "<scheduler-scratch>"

_SCHEDULER_SCRATCH_VARS = frozenset({"TMPDIR", "SLURM_TMPDIR", "PBS_JOBFS"})
_SCHEDULER_SCRATCH_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(/|$)")


def _scheduler_scratch_target(token: str) -> str | None:
    """把调度器 scratch 操作数归为安全 marker 或不可解析。

    AST 外部变量绑定会把 TMPDIR 路径投影成 scheduler marker 路径，
    因此 raw token 与投影 token 必须走同一条规范化路径。任何父级跳转、
    二次变量、动态参数或绝对重解释都必须在提交前 fail-closed。
    """
    text = str(token).strip().strip("\"" + chr(39))
    suffix: str | None = None
    if text == REMOTE_SCRATCH:
        suffix = ""
    elif text.startswith(REMOTE_SCRATCH + "/"):
        suffix = text[len(REMOTE_SCRATCH) + 1:]
    else:
        match = _SCHEDULER_SCRATCH_RE.match(text)
        if match and match.group(1) in _SCHEDULER_SCRATCH_VARS:
            suffix = text[match.end():]
    if suffix is None:
        return None
    if (
        suffix.startswith("/")
        or "$" in suffix
        or "__hf_dynamic__" in suffix
        or any(ch in suffix for ch in ("*", "?", "["))
        or ".." in suffix.split("/")
    ):
        return UNRESOLVED
    return REMOTE_SCRATCH


# 命令替换、子 shell、循环、xargs：目标只在运行期才存在，静态不可知。
_RUNTIME_ONLY_RE = re.compile(
    r"\$\(|`|\bxargs\b|(?:^|\s)for\s+\w+\s+in\b|(?:^|\s)while\s|\{\s*\w+\s*\.\.")
# source/. 载入外部脚本后，其定义的变量对本模块不可见；此后由变量派生的目标
# 必须视为不可解析。实测过一个更隐蔽的形态：命令里的 $BUILD 未在命令内赋值时，
# os.path.expandvars 会拿**节点进程自己的**同名环境变量去填（conda 环境里
# BUILD=x86_64-conda-linux-gnu），于是判定的是 A、运行时删的是 B。
_SOURCES_SCRIPT_RE = re.compile(r"(?:^|[\s;&|])(?:source|\.)\s+\S+")


def _command_is_opaque(text: str) -> bool:
    """整条命令是否含"目标只在运行期可知"的构造。

    必须按整条命令判，不能按 segment：`source ./env.sh && rm -rf $BUILD` 里
    污染源在前一段，而 `ls d | xargs rm -rf` 根本不会被 _split_shell_segments
    切开（它只切 `&&`/`||`/`;`/换行，不切单个管道）。
    """
    return bool(_RUNTIME_ONLY_RE.search(text or "")
                or _SOURCES_SCRIPT_RE.search(text or ""))


def _has_unresolved_var(token: str) -> bool:
    """展开后仍残留 $ 引用 → 该 token 指向哪里无法证明。"""
    return "$" in token


def _ignore_write_target(path: str) -> bool:
    p = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
    return p in {"/dev/null"} or p.startswith("/dev/std") or p.startswith("/proc/self/fd")


def _command_args(tokens: list[str]) -> list[str]:
    """Return non-option args after the command token.

    This is best-effort for common shell commands. It intentionally ignores options
    and option values are not fully modeled; scope_guard is a safety rail, not a shell
    interpreter.
    """
    return [t for t in tokens[1:] if _pathish(t)]


def _operand_args(tokens: list[str]) -> list[str]:
    """Every non-option operand after the command token.

    Unlike ``_command_args`` this does **not** filter by ``_pathish``: for a
    destructive verb every operand is a filesystem target, and requiring a
    ``/`` in the token is what made ``rm -rf build`` invisible.
    """
    out: list[str] = []
    for t in tokens[1:]:
        if t == "--":
            continue
        if t.startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            continue
        if t in {"|", ">", ">>", "2>", "2>>", "&>", "<"}:
            continue
        out.append(t)
    return out


_GLOB_CHARS = ("*", "?", "[")


def _containment_target(token: str, base: str) -> str:
    """Resolve one operand to the path whose containment must be proven.

    A glob is deliberately *not* expanded against the filesystem: expansion
    races the command itself and depends on what happens to exist right now.
    The directory holding the glob is what must be contained, which is both
    stronger and cheaper to prove — ``rm -rf *`` reduces to the resolved cwd,
    ``rm -rf build/*`` to ``<base>/build``.
    """
    if str(token).lower().startswith("file:"):
        try:
            parsed = urlsplit(str(token))
        except ValueError:
            return UNRESOLVED
        if (
            parsed.scheme.lower() != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
            or not parsed.path
        ):
            return UNRESOLVED
        token = unquote(parsed.path)
    scratch_target = _scheduler_scratch_target(token)
    if scratch_target is not None:
        return scratch_target
    if _has_unresolved_var(token):
        return UNRESOLVED
    if any(ch in token for ch in _GLOB_CHARS):
        head = token.rsplit("/", 1)[0] if "/" in token else ""
        if any(ch in head for ch in _GLOB_CHARS):
            # 通配符出现在中间层（build/*/CMakeCache.txt）→ 覆盖面无法证明
            return UNRESOLVED
        return _norm_path(head, base) if head else _norm_path(".", base)
    return _norm_path(token, base)


def _remote_transfer_operand(token: str) -> bool:
    """判断传输目的地是否指向另一台主机。

    不能把 ``host:/path`` 交给 ``Path``/``abspath``；否则它会被
    错写成 ``base`` 下看似安全的相对路径。SCP 本身也把带冒号的名字视为
    远端地址，除非调用方用 ``./name:with-colon`` 明确声明为本地路径。
    """
    value = str(token or "").strip()
    if value.lower().startswith("file:"):
        return False
    if value.startswith(("scp://", "rsync://")):
        return True
    if re.match(r"^\[[^\]]+\]:", value):
        return True
    if value.startswith(("./", "../", "/")):
        return False
    prefix, separator, _rest = value.partition(":")
    return bool(separator and prefix and "/" not in prefix)


def _option_path_values(
    tokens: list[str], flags: frozenset[str], *,
    equals_flags: frozenset[str] | None = None,
) -> tuple[list[str], bool]:
    """提取值为输出路径的 CLI 选项，并报告缺失值。"""
    equals_flags = equals_flags or flags
    values: list[str] = []
    missing = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in flags:
            if index + 1 >= len(tokens) or (
                tokens[index + 1].startswith("-")
                and tokens[index + 1] != "-"
            ):
                missing = True
                index += 1
                continue
            values.append(tokens[index + 1])
            index += 2
            continue
        for flag in equals_flags:
            prefix = flag + "="
            if token.startswith(prefix):
                values.append(token[len(prefix):] or UNRESOLVED)
                break
        index += 1
    return values, missing


def _scp_payload_operands(tokens: list[str]) -> list[str]:
    """返回已剔除选项值的 SCP 源和目的操作数。"""
    options_with_value = frozenset({
        "-c", "-D", "-F", "-i", "-J", "-l", "-o", "-P", "-S", "-X",
    })
    operands: list[str] = []
    index = 1
    options_done = False
    while index < len(tokens):
        token = tokens[index]
        if not options_done and token == "--":
            options_done = True
            index += 1
            continue
        if not options_done and token in options_with_value:
            index += 2
            continue
        if not options_done and token.startswith("-"):
            # OpenSSH 接受 -P22/-iKEY 这类紧邻值和布尔短选项组合；
            # 两者都不是 payload 操作数。
            index += 1
            continue
        operands.append(token)
        index += 1
    return operands


def _bundled_short_option_values(
    tokens: list[str],
    *,
    value_options: frozenset[str],
    boolean_options: frozenset[str],
    captured_options: frozenset[str],
) -> tuple[dict[str, list[str]], set[str], bool]:
    """按 getopt 语义解析短选项 bundle；值选项消费余串或下一 argv。"""
    captured = {name: [] for name in captured_options}
    seen: set[str] = set()
    unresolved = False
    index = 1
    while index < len(tokens):
        token = str(tokens[index])
        if token == "--":
            break
        if not token.startswith("-") or token.startswith("--") or len(token) <= 2:
            index += 1
            continue
        body = token[1:]
        position = 0
        consumed_next = False
        while position < len(body):
            option = body[position]
            seen.add(option)
            if option in value_options:
                remainder = body[position + 1:]
                if remainder:
                    value = remainder
                elif index + 1 < len(tokens) and (
                    not str(tokens[index + 1]).startswith("-")
                    or str(tokens[index + 1]) == "-"
                ):
                    value = str(tokens[index + 1])
                    consumed_next = True
                else:
                    value = UNRESOLVED
                    unresolved = True
                if option in captured:
                    captured[option].append(value)
                break
            if option not in boolean_options:
                if any(item in captured_options for item in body[position + 1:]):
                    unresolved = True
                break
            position += 1
        index += 2 if consumed_next else 1
    return captured, seen, unresolved


def _network_write_targets(op: str, tokens: list[str], base: str) -> list[str]:
    """投影常见传输客户端在 shell 上可见的写入目的地。

    远端调度器没有本机 mount sandbox，因此已知输出选择器必须进入与重定向、
    构建输出相同的 path-role 分类器。配置文件可以隐藏输出指令，无法静态展开
    时按 UNRESOLVED 关闭，而不是把配置文件内容当作路径授权。
    """
    targets: list[str] = []
    if op == "curl":
        output_flags = frozenset({
            "-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar",
            "--trace", "--trace-ascii",
        })
        values, missing = _option_path_values(
            tokens,
            output_flags,
            equals_flags=frozenset({
                "--output", "--dump-header", "--cookie-jar",
                "--trace", "--trace-ascii",
            }),
        )
        output_dirs, output_dir_missing = _option_path_values(
            tokens,
            frozenset({"--output-dir"}),
            equals_flags=frozenset({"--output-dir"}),
        )
        curl_bundle, curl_seen, curl_bundle_unresolved = (
            _bundled_short_option_values(
                tokens,
                value_options=frozenset({
                    "A", "b", "c", "d", "D", "e", "E", "F", "H",
                    "K", "m", "o", "P", "Q", "r", "T", "u", "w",
                    "x", "X", "Y", "y", "z",
                }),
                boolean_options=frozenset({
                    "0", "1", "2", "3", "4", "6", "a", "B", "C",
                    "f", "g", "G", "h", "i", "I", "j", "J", "k",
                    "l", "L", "M", "n", "N", "O", "q", "R", "s",
                    "S", "t", "v", "V", "Z", "#",
                }),
                captured_options=frozenset({"o", "D", "c"}),
            )
        )
        values.extend(curl_bundle["o"] + curl_bundle["D"] + curl_bundle["c"])
        for value in values:
            if value != "-":
                targets.append(_containment_target(value, base))
        targets.extend(
            _containment_target(value, base) for value in output_dirs
        )
        remote_name = (
            "O" in curl_seen
            or any(
                token in {"-O", "--remote-name", "--remote-name-all"}
                for token in tokens[1:]
            )
        )
        if remote_name and not output_dirs:
            targets.append(_norm_path(".", base))
        if (
            "K" in curl_seen
            or any(
                token in {"-K", "--config"}
                or token.startswith("--config=")
                for token in tokens[1:]
            )
        ):
            targets.append(UNRESOLVED)
        if missing or output_dir_missing or curl_bundle_unresolved:
            targets.append(UNRESOLVED)
        return targets

    if op == "wget":
        documents, document_missing = _option_path_values(
            tokens,
            frozenset({"-O", "--output-document"}),
            equals_flags=frozenset({"--output-document"}),
        )
        directories, directory_missing = _option_path_values(
            tokens,
            frozenset({"-P", "--directory-prefix"}),
            equals_flags=frozenset({"--directory-prefix"}),
        )
        auxiliaries, auxiliary_missing = _option_path_values(
            tokens,
            frozenset({
                "-o", "--output-file", "-a", "--append-output",
                "--save-cookies", "--warc-file",
            }),
            equals_flags=frozenset({
                "--output-file", "--append-output", "--save-cookies",
                "--warc-file",
            }),
        )
        wget_bundle, wget_seen, wget_bundle_unresolved = (
            _bundled_short_option_values(
                tokens,
                value_options=frozenset({
                    "a", "A", "B", "D", "e", "i", "I", "l", "o",
                    "O", "P", "Q", "R", "t", "T", "U", "w", "X",
                }),
                boolean_options=frozenset({
                    "b", "c", "d", "E", "F", "h", "H", "k", "m",
                    "n", "N", "p", "q", "r", "S", "v", "V", "x",
                }),
                captured_options=frozenset({"O", "P", "o", "a"}),
            )
        )
        documents.extend(wget_bundle["O"])
        directories.extend(wget_bundle["P"])
        auxiliaries.extend(wget_bundle["o"] + wget_bundle["a"])
        targets.extend(
            _containment_target(value, base)
            for value in documents + directories + auxiliaries
            if value != "-"
        )
        spider = "--spider" in tokens[1:]
        if not spider and not documents and not directories:
            targets.append(_norm_path(".", base))
        if (
            "e" in wget_seen
            or any(
                token in {"-e", "--execute", "--config"}
                or token.startswith(("--execute=", "--config="))
                for token in tokens[1:]
            )
        ):
            targets.append(UNRESOLVED)
        if (
            document_missing
            or directory_missing
            or auxiliary_missing
            or wget_bundle_unresolved
        ):
            targets.append(UNRESOLVED)
        return targets

    if op == "scp":
        operands = _scp_payload_operands(tokens)
        if len(operands) < 2:
            return [UNRESOLVED]
        destination = operands[-1]
        if _remote_transfer_operand(destination):
            return [UNRESOLVED]
        return [_containment_target(destination, base)]

    return []


def _is_exact_capability_probe(
    args: Sequence[str],
    *,
    flags: frozenset[str],
    flag_prefixes: tuple[str, ...] = (),
) -> bool:
    """Return whether every argv item is an explicit capability-probe flag.

    The non-empty/all-items shape is the safety property shared by route and
    write classification: seeing ``--version`` somewhere is insufficient when
    the same command also carries a source, target, or other operand.
    """
    return bool(args) and all(
        arg in flags or any(arg.startswith(prefix) for prefix in flag_prefixes)
        for arg in args
    )


_RSYNC_CAPABILITY_PROBE_FLAGS = frozenset({"--version", "--help", "-V"})


def _rsync_write_targets(tokens: list[str], base: str) -> list[str]:
    """提取 rsync payload 与自身日志/临时/备份输出，避免选项值冒充目的地。"""
    if _is_exact_capability_probe(
        tokens[1:], flags=_RSYNC_CAPABILITY_PROBE_FLAGS,
    ):
        return []
    options_with_value = frozenset({
        "-B", "-e", "-f", "-T",
        "--address", "--backup-dir", "--block-size", "--bwlimit",
        "--checksum-choice", "--chmod", "--chown", "--compare-dest",
        "--compress-choice", "--compress-level", "--contimeout",
        "--copy-dest", "--exclude", "--exclude-from", "--files-from",
        "--filter", "--groupmap", "--iconv", "--include", "--include-from",
        "--link-dest", "--log-file", "--log-file-format", "--max-size",
        "--min-size", "--out-format", "--partial-dir", "--password-file",
        "--port", "--protocol", "--remote-option", "--rsync-path", "--rsh",
        "--skip-compress", "--sockopts", "--stop-after", "--stop-at",
        "--temp-dir", "--timeout", "--usermap", "--write-batch",
        "--only-write-batch",
    })
    write_options = frozenset({
        "-T", "--backup-dir", "--log-file", "--partial-dir", "--temp-dir",
        "--write-batch", "--only-write-batch",
    })
    operands: list[str] = []
    option_writes: list[str] = []
    unresolved = False
    rsync_bundle, _rsync_seen, bundle_unresolved = _bundled_short_option_values(
        tokens,
        value_options=frozenset({"B", "e", "f", "T"}),
        boolean_options=frozenset({
            "a", "b", "c", "C", "d", "D", "g", "h", "H", "i",
            "k", "K", "l", "L", "m", "n", "o", "p", "P", "q",
            "r", "R", "s", "S", "t", "u", "v", "W", "x", "X",
            "y", "z", "V",
        }),
        captured_options=frozenset({"T"}),
    )
    option_writes.extend(rsync_bundle["T"])
    unresolved = unresolved or bundle_unresolved
    index = 1
    options_done = False
    while index < len(tokens):
        token = tokens[index]
        if not options_done and token == "--":
            options_done = True
            index += 1
            continue
        if not options_done and token.startswith("--"):
            name, separator, attached = token.partition("=")
            if separator:
                if name in write_options:
                    option_writes.append(attached or UNRESOLVED)
                index += 1
                continue
            if name in options_with_value:
                if index + 1 >= len(tokens):
                    unresolved = True
                    index += 1
                    continue
                value = tokens[index + 1]
                if name in write_options:
                    option_writes.append(value)
                index += 2
                continue
            # 未建模长选项在已有 source+destination 后仍携带独立值时，
            # 不能让那个值覆盖真实 destination。
            if (
                len(operands) >= 2
                and index + 1 < len(tokens)
                and not tokens[index + 1].startswith("-")
            ):
                unresolved = True
            index += 1
            continue
        if not options_done and token in {"-B", "-e", "-f", "-T"}:
            if index + 1 >= len(tokens):
                unresolved = True
                index += 1
                continue
            if token == "-T":
                option_writes.append(tokens[index + 1])
            index += 2
            continue
        if not options_done and token.startswith("-"):
            # 短选项组合或紧邻值都不属于 source/destination。
            if token.startswith("-T") and token != "-T":
                option_writes.append(token[2:] or UNRESOLVED)
            index += 1
            continue
        operands.append(token)
        index += 1

    targets = [
        _containment_target(value, base)
        for value in option_writes
        if value != "-"
    ]
    if len(operands) < 2:
        targets.append(UNRESOLVED)
    else:
        destination = operands[-1]
        targets.append(
            UNRESOLVED
            if _remote_transfer_operand(destination)
            else _containment_target(destination, base)
        )
    if unresolved:
        targets.append(UNRESOLVED)
    return targets



_COMPILER_CAPABILITY_PROBE_FLAGS = frozenset({
    "--version", "-V", "-v", "--help", "-dumpversion", "-dumpmachine",
})
_COMPILER_CAPABILITY_PROBE_PREFIXES = ("-print-",)


def _compiler_write_targets(tokens: list[str], base: str) -> list[str]:
    """投影标准编译器/链接器输出、依赖文件和 Fortran module 目录。"""
    probe_args = tokens[1:]
    if _is_exact_capability_probe(
        probe_args,
        flags=_COMPILER_CAPABILITY_PROBE_FLAGS,
        flag_prefixes=_COMPILER_CAPABILITY_PROBE_PREFIXES,
    ):
        return []
    value_flags = frozenset({
        "-o", "--output-file", "-J", "-module", "-mod", "-MF", "-MJ",
    })
    values, missing = _option_path_values(
        tokens,
        value_flags,
        equals_flags=frozenset({"--output-file", "-module", "-mod"}),
    )
    targets = [
        _containment_target(value, base) for value in values if value != "-"
    ]
    for token in tokens[1:]:
        for prefix in ("-o", "-J", "-MF", "-MJ"):
            if (
                token.startswith(prefix)
                and token != prefix
                and not token.startswith(prefix + "=")
            ):
                value = token[len(prefix):]
                targets.append(
                    _containment_target(value, base) if value else UNRESOLVED)
                break
        if token.startswith("@"):
            # 响应文件可以隐藏上面的全部输出选择器，不在 shell 边界重写
            # 各编译器自己的小语言。
            targets.append(UNRESOLVED)
    if missing:
        targets.append(UNRESOLVED)
    if not targets:
        # 无显式选择器的真实编译默认写 cwd；纯能力探查已在上方排除。
        targets.append(_norm_path(".", base))
    return targets


_GENERIC_CLI_OUTPUT_OPTION_WORDS = frozenset({
    "output", "output-file", "output-dir", "output-path",
    "out", "out-file", "out-dir", "out-path",
    "destination", "dest", "dump", "dump-file", "log-file",
    "save", "save-dir", "save-path", "write", "write-file",
    "checkpoint-dir", "default-root-dir", "results-dir", "work-dir",
    "log-dir", "logdir", "output-prefix",
})


def _generic_cli_output_option(token: str) -> bool:
    key = str(token or "").split("=", 1)[0].lstrip("-").lower()
    key = key.replace("_", "-")
    return key in _GENERIC_CLI_OUTPUT_OPTION_WORDS or any(
        key.endswith("-" + suffix)
        for suffix in _GENERIC_CLI_OUTPUT_OPTION_WORDS
    )


def _generic_cli_output_targets(tokens: list[str], base: str) -> list[str]:
    """只投影未知 CLI 明确声明的输出选项，不把任意路径参数当写入。"""
    targets: list[str] = []
    index = 1
    while index < len(tokens):
        token = str(tokens[index])
        value: str | None = None
        if token.startswith("-") and _generic_cli_output_option(token):
            if "=" in token:
                value = token.split("=", 1)[1]
            elif index + 1 < len(tokens) and (
                not str(tokens[index + 1]).startswith("-")
                or str(tokens[index + 1]) == "-"
            ):
                value = str(tokens[index + 1])
                index += 1
            else:
                value = UNRESOLVED
        elif token == "-o":
            if index + 1 < len(tokens) and (
                not str(tokens[index + 1]).startswith("-")
                or str(tokens[index + 1]) == "-"
            ):
                value = str(tokens[index + 1])
                index += 1
            else:
                value = UNRESOLVED
        elif token.startswith("-o") and token != "-o":
            attached = token[2:]
            if (
                attached.startswith(("/", "../", "./", "~", "$"))
                or "/" in attached
                or "__hf_dynamic__" in attached
            ):
                value = attached or UNRESOLVED
        if value is not None and value != "-":
            if not value or "__hf_dynamic__" in value:
                targets.append(UNRESOLVED)
            else:
                targets.append(_containment_target(value, base))
        index += 1
    return targets


# 远端作业体内裸命令名靠执行节点的 PATH 解析。这里不做硬拒（低风险面，
# 提交机既证明不了远端 PATH，也不该逼 LLM 把每个系统工具写成绝对路径），
# 只按 remote_path_lookup 留痕供收尾核验；shell 语法词/内建从不查 PATH，
# 记录它们只会淹没真正的外部程序名。
_REMOTE_NO_PATH_LOOKUP_HEADS = frozenset({
    "cd", "true", "false", ":", "exit", "return", "break", "continue",
    "export", "set", "unset", "read", "umask", "shopt", "trap", "wait",
    "eval", "source", ".", "builtin", "echo", "printf", "test", "[",
    "local", "declare", "typeset", "readonly", "pushd", "popd",
    "__hf_noexec__", "__hf_dynamic__",
})


_REMOTE_VISIBLE_PATH_OPTION_WORDS = frozenset({
    "input", "input-file", "input-dir", "input-path",
    "file", "file-path", "directory", "dir", "path",
    "config", "config-file", "config-path",
    "source", "source-file", "source-dir", "source-path",
    "hostfile", "machinefile", "prefix", "root",
    "checkpoint", "checkpoint-dir", "restart", "restart-file",
}) | _GENERIC_CLI_OUTPUT_OPTION_WORDS


def _remote_visible_path_option(token: str) -> bool:
    key = str(token or "").split("=", 1)[0].lstrip("-").lower()
    key = key.replace("_", "-")
    return key in _REMOTE_VISIBLE_PATH_OPTION_WORDS or any(
        key.endswith("-" + suffix)
        for suffix in _REMOTE_VISIBLE_PATH_OPTION_WORDS
    )


def _remote_visible_operand_targets(
    tokens: list[str], base: str, *, head_path: str | None = None,
) -> list[str]:
    """投影远端 argv 中可证明是路径的静态 operand 与路径形式命令头。

    通用层只认绝对路径、含父级跳转的路径和明确 path-like 选项，避免把
    --steps $N、--temperature $T 等动态标量误判成动态路径。输出选择器
    仍由写目标分析 fail-closed；这里只补输入/可见性，不授予写权限。

    ``head_path`` 是 AST 层保留的原始 argv[0] token（仅路径形式，含 "/"）：
    POSIX 下它必然按文件系统路径执行而不查 PATH，所以与其他 operand 同规则
    投影——包括相对形式（bin/x），它按 ``base`` 解析。裸命令名不在此投影，
    由调用方按 remote_path_lookup 留痕放行。
    """
    targets: list[str] = []
    if head_path is not None:
        if "__hf_dynamic__" in head_path:
            targets.append(UNRESOLVED)
        else:
            head_target = _containment_target(head_path, base)
            if (
                head_target in {UNRESOLVED, REMOTE_SCRATCH}
                or not _ignore_write_target(head_target)
            ):
                targets.append(head_target)
    if not tokens:
        return targets
    program = os.path.basename(str(tokens[0])).lower()
    if program in {"echo", "printf"}:
        return targets

    index = 1
    while index < len(tokens):
        token = str(tokens[index] or "")
        if token.startswith("-"):
            if "=" in token:
                option, value = token.split("=", 1)
                if _remote_visible_path_option(option):
                    if (
                        not value
                        or value.startswith("@")
                        or "__hf_dynamic__" in value
                    ):
                        targets.append(UNRESOLVED)
                    elif not _remote_transfer_operand(value):
                        targets.append(_containment_target(value, base))
                index += 1
                continue
            if _remote_visible_path_option(token):
                if index + 1 >= len(tokens):
                    targets.append(UNRESOLVED)
                    index += 1
                    continue
                value = str(tokens[index + 1] or "")
                if value.startswith("-") and "__hf_dynamic__" not in value:
                    targets.append(UNRESOLVED)
                    index += 1
                    continue
                if (
                    not value
                    or value.startswith("@")
                    or "__hf_dynamic__" in value
                ):
                    targets.append(UNRESOLVED)
                elif value != "-" and not _remote_transfer_operand(value):
                    targets.append(_containment_target(value, base))
                index += 2
                continue
            index += 1
            continue

        if token.startswith("@"):
            targets.append(UNRESOLVED)
            index += 1
            continue
        if "__hf_dynamic__" in token:
            index += 1
            continue
        if token == "-" or _remote_transfer_operand(token):
            index += 1
            continue
        parts = Path(token).parts
        externally_resolved = (
            token.lower().startswith("file:")
            or os.path.isabs(os.path.expanduser(token))
            or token.startswith("~")
            or ".." in parts
        )
        if externally_resolved:
            target = _containment_target(token, base)
            if (
                target in {UNRESOLVED, REMOTE_SCRATCH}
                or not _ignore_write_target(target)
            ):
                targets.append(target)
        index += 1
    return targets



def _known_application_write_targets(
    tokens: list[str], base: str,
) -> tuple[list[str], bool]:
    """投影少数具有稳定公开 CLI 语义的 HPC/工作流写入目标。

    未知程序仍由通用显式输出选项处理；这里不猜任意位置参数。
    """
    if not tokens:
        return [], False
    op = os.path.basename(str(tokens[0])).lower()

    if re.fullmatch(r"lmp(?:_[a-z0-9_.-]+)?", op):
        values, missing = _option_path_values(
            tokens, frozenset({"-log"}), equals_flags=frozenset())
        targets = [
            _containment_target(value, base)
            for value in values
            if value not in {"-", "none", "NONE"}
        ]
        return [*targets, *([UNRESOLVED] if missing else [])], bool(
            targets or missing)

    if op == "snakemake":
        if any(flag in tokens[1:] for flag in {
            "-n", "--dry-run", "--lint", "--list", "--list-rules",
            "--summary", "--detailed-summary", "--dag", "--rulegraph",
        }):
            return [], False
        values, missing = _option_path_values(
            tokens, frozenset({"-d", "--directory"}),
            equals_flags=frozenset({"--directory"}))
        targets = [
            _containment_target(value, base) for value in values if value != "-"
        ]
        if missing:
            targets.append(UNRESOLVED)
        return targets or [_norm_path(".", base)], True

    if op in {"gmx", "gmx_mpi"} and "mdrun" in tokens[1:]:
        values, missing = _option_path_values(
            tokens, frozenset({"-deffnm"}),
            equals_flags=frozenset({"-deffnm"}))
        targets = [
            _containment_target(value, base) for value in values if value != "-"
        ]
        if missing:
            targets.append(UNRESOLVED)
        return targets or [_norm_path(".", base)], True

    if op in _MPI_LAUNCHERS:
        values, missing = _option_path_values(
            tokens, frozenset({"--output-filename"}),
            equals_flags=frozenset({"--output-filename"}))
        targets = [
            _containment_target(value, base) for value in values if value != "-"
        ]
        if missing:
            targets.append(UNRESOLVED)
        return targets, bool(targets)

    if op == "meson" and _first_non_option(tokens[1:]) == "install":
        values, missing = _option_path_values(
            tokens, frozenset({"--destdir"}),
            equals_flags=frozenset({"--destdir"}))
        targets = [
            _containment_target(value, base) for value in values if value != "-"
        ]
        if missing or not targets:
            targets.append(UNRESOLVED)
        return targets, True

    if op == "unrar" and len(tokens) > 1:
        mode = str(tokens[1]).lower()
        if mode not in {"x", "e"}:
            return [], False
        operands = [str(item) for item in tokens[2:] if not str(item).startswith("-")]
        targets = [_norm_path(".", base)]
        if len(operands) >= 2:
            targets.append(_containment_target(operands[-1], base))
        return targets, True

    return [], False


def _inline_python_write_targets(tokens: list[str], base: str) -> list[str]:
    program = os.path.basename(str(tokens[0] or "")) if tokens else ""
    if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", program):
        return []
    try:
        code_index = tokens.index("-c") + 1
    except ValueError:
        return []
    if code_index >= len(tokens):
        return [UNRESOLVED]
    code = str(tokens[code_index])
    if "__hf_dynamic__" in code:
        return [UNRESOLVED]
    paths, dynamic = _python_direct_write_paths(code, base)
    return [*paths, *([UNRESOLVED] if dynamic else [])]


def _pip_install_write_targets(
    tokens: list[str], base: str,
) -> list[str] | None:
    """投影 pip/python -m pip install 的显式安装、报告与缓存目的地。"""
    if not tokens:
        return None
    program = os.path.basename(str(tokens[0]))
    args = list(tokens[1:])
    if re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", program):
        if len(args) < 2 or args[0] != "-m" or args[1] not in {"pip", "pip3"}:
            return None
        args = args[2:]
    elif program not in {"pip", "pip3"}:
        return None
    if not args or args[0] != "install":
        return []
    path_flags = frozenset({
        "--target", "--prefix", "--root", "--report", "--cache-dir",
    })
    synthetic = [program, *args]
    values, missing = _option_path_values(
        synthetic, path_flags, equals_flags=path_flags)
    targets = [
        _containment_target(value, base)
        for value in values
        if value != "-" and "__hf_dynamic__" not in value
    ]
    if (
        missing
        or any("__hf_dynamic__" in value for value in values)
        or "--user" in args
        or not targets
    ):
        targets.append(UNRESOLVED)
    return targets


def _sed_inplace_targets(tokens: list[str], base: str) -> list[str]:
    """Return write targets for common `sed -i` forms.

    The sed script itself often contains slashes (`s|old|new|g`) and must not be
    treated as a filesystem path. We skip the script operand and only classify
    following file operands.
    """
    targets: list[str] = []
    saw_script = False
    skip_next = False
    for t in tokens[1:]:
        if skip_next:
            skip_next = False
            saw_script = True
            continue
        if t == "--":
            continue
        if t == "-i" or t.startswith("-i"):
            continue
        if t in {"-e", "-f"}:
            skip_next = True
            continue
        if t.startswith("-"):
            continue
        if not saw_script:
            saw_script = True
            continue
        targets.append(_containment_target(t, base))
    return targets


# Verbs that remove or overwrite existing content.  Every one of these needs
# its operands resolved before the command runs; `rm` is merely the most
# famous.  `rsync --delete`, `find -delete` and `git clean` passed *both*
# gates untouched before 2026-08-04 — they match no high-risk pattern and
# produced no scope-guard target.
_MUTATING_BUILD_HEADS = frozenset({"make", "gmake", "ninja", "cmake", "case.build", "buildlib"})
_BUILD_DRY_RUN_FLAGS_BY_TOOL = {
    "make": frozenset({"-n", "--dry-run"}),
    "gmake": frozenset({"-n", "--dry-run"}),
    "ninja": frozenset({"-n", "--dry-run"}),
}
_BUILD_CAPABILITY_PROBE_FLAGS_BY_TOOL = {
    "make": frozenset({"--version", "-v", "--help"}),
    "gmake": frozenset({"--version", "-v", "--help"}),
    "ninja": frozenset({"--version", "--help"}),
    "cmake": frozenset({"--version", "--help"}),
    "compile": frozenset({"--help"}),
}

def _is_build_probe(tokens: list[str]) -> bool:
    if not tokens:
        return False
    tool = os.path.basename(str(tokens[0])).lower()
    # Shell redirections are not argv operands.  Strip them at the shared
    # classification boundary so route and write projection cannot disagree;
    # each pipeline stage has already been separated by the caller.
    args = _drop_redirections(tokens[1:])
    # These spellings are tool-specific: ninja -v is a real verbose build,
    # while bare `help` can be an ordinary Make/Ninja target.
    flags = _BUILD_CAPABILITY_PROBE_FLAGS_BY_TOOL.get(tool)
    if flags is not None and _is_exact_capability_probe(args, flags=flags):
        return True

    # Preserve the established dry-run boundary: only a leading make/ninja
    # mode switch exempts the command.  A later ``-n`` can be a make option
    # value (``make -C -n all``), a target-side token, or Ninja subtool input
    # (``ninja -t cleandead -n``); treating any occurrence as dry-run silently
    # releases real builds from the managed lifecycle.
    dry_run_flags = _BUILD_DRY_RUN_FLAGS_BY_TOOL.get(tool, frozenset())
    return bool(args) and args[0] in dry_run_flags

_DESTRUCTIVE_OPS = frozenset({
    "rm", "rmdir", "mv", "truncate", "shred", "rsync", "find", "git", "install",
    "tar", "unzip", "zip", "ar", "dd",
})

# `find` earns its place in _DESTRUCTIVE_OPS only through actions that write
# or delete.  A pure query (`find … -iname … | xargs basename`) writes nothing,
# yet the coarse membership test used to combine with an opaque pipeline and
# push the whole command to UNRESOLVED — a read-only probe became a pause the
# agent could not answer in --no-interactive.  _find_destructive_targets
# already draws this line on the segment path; keep both sides in agreement.
# `-fprint*`/`-fls` write their operand file just as surely as `-delete` removes
# one; leaving them out would let `find / -fprintf /tmp/x '%p'` through unasked.
_FIND_DESTRUCTIVE_ACTIONS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprint0", "-fprintf", "-fls",
})
_FIND_WRITE_OPERAND_ACTIONS = frozenset({"-fprint", "-fprint0", "-fprintf", "-fls"})


def _stage_is_destructive(head: str, args: list[str]) -> bool:
    """Does this one command stage hold a verb that can write or delete?"""
    if head == "find":
        return any(a in _FIND_DESTRUCTIVE_ACTIONS for a in args)
    return head in _DESTRUCTIVE_OPS


def _mentions_destructive(text: str) -> bool:
    """Does any *command position* in ``text`` hold a destructive verb?

    Used only to push an already-unprovable command toward UNRESOLVED, never
    as grounds for blocking on its own.  It still has to read command
    positions rather than searching the raw text: ``pip install foo`` and
    ``echo find`` both contain a listed verb as an argument, and treating them
    as destructive would add prompts to commands that delete nothing.
    """
    for stage in _command_stages(text or ""):
        head, args = _stage_head(stage)
        if not head:
            continue
        if _stage_is_destructive(head, args):
            return True
        # `... | xargs rm -rf` puts the real verb after the launcher.
        if head in {"xargs", "parallel"} and any(
                _stage_is_destructive(a.split("/")[-1], args) for a in args):
            return True
    return False


def _git_destructive_targets(tokens: list[str], base: str) -> list[str] | None:
    """解析 Git 全局工作树上下文后投影会改写工作树的子命令。"""
    effective_base = base
    explicit_worktree: str | None = None
    index = 1
    value_options = {
        "-c", "--config-env", "--git-dir", "--namespace",
        "--super-prefix",
    }
    while index < len(tokens):
        token = str(tokens[index])
        if token == "--":
            index += 1
            break
        if token == "-C":
            if index + 1 >= len(tokens):
                return [UNRESOLVED]
            effective_base = _containment_target(
                str(tokens[index + 1]), effective_base)
            if effective_base in {UNRESOLVED, REMOTE_SCRATCH}:
                return [UNRESOLVED]
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            effective_base = _containment_target(
                token[2:] or UNRESOLVED, effective_base)
            if effective_base in {UNRESOLVED, REMOTE_SCRATCH}:
                return [UNRESOLVED]
            index += 1
            continue
        if token == "--work-tree":
            if index + 1 >= len(tokens):
                return [UNRESOLVED]
            explicit_worktree = str(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--work-tree="):
            explicit_worktree = token.split("=", 1)[1] or UNRESOLVED
            index += 1
            continue
        name = token.split("=", 1)[0]
        if name in value_options:
            index += 1 if "=" in token else 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(tokens):
        return None

    sub = str(tokens[index])
    worktree = (
        _containment_target(explicit_worktree, effective_base)
        if explicit_worktree is not None
        else effective_base
    )
    if sub == "clone":
        clone_value_options = {
            "-b", "--branch", "--depth", "-j", "--jobs", "--origin",
            "-o", "--reference", "--reference-if-able", "--separate-git-dir",
            "--shallow-since", "--shallow-exclude", "--template", "-u",
            "--upload-pack", "--server-option", "-c", "--config",
        }
        operands: list[str] = []
        cursor = index + 1
        while cursor < len(tokens):
            token = str(tokens[cursor])
            if token == "--":
                operands.extend(str(item) for item in tokens[cursor + 1:])
                break
            name = token.split("=", 1)[0]
            if name in clone_value_options:
                cursor += 1 if "=" in token else 2
                continue
            if token.startswith("-"):
                cursor += 1
                continue
            operands.append(token)
            cursor += 1
        destination = operands[1] if len(operands) > 1 else "."
        return [_containment_target(destination, effective_base)]
    if sub in {"clean", "switch", "restore", "stash"}:
        return [worktree]
    if sub == "checkout" and "--" in tokens[index + 1:]:
        separator = tokens.index("--", index + 1)
        rest = tokens[separator + 1:]
        return (
            [_containment_target(item, worktree) for item in rest]
            or [worktree]
        )
    if sub == "reset" and "--hard" in tokens[index + 1:]:
        return [worktree]
    return None


def _find_destructive_targets(tokens: list[str], base: str) -> list[str] | None:
    """``find <roots> ... -delete|-exec rm|-fprint FILE|...`` → what it can write.

    Search roots cover the delete/exec forms; ``-fprint*``/``-fls`` additionally
    name one output file each, and that file is the thing actually written.
    """
    if not any(t in _FIND_DESTRUCTIVE_ACTIONS for t in tokens[1:]):
        return None
    roots: list[str] = []
    for t in tokens[1:]:
        if t.startswith("-"):
            break
        roots.append(t)
    written: list[str] = []
    for idx, tok in enumerate(tokens[1:], start=1):
        if tok in _FIND_WRITE_OPERAND_ACTIONS and idx + 1 < len(tokens):
            written.append(tokens[idx + 1])
    targets = [_containment_target(t, base) for t in roots + written]
    return targets or [_norm_path(".", base)]


def _redirection_write_targets(segment: str, base: str) -> list[str]:
    """用 shell lexer 提取真实输出重定向，忽略引号内的 ``>`` 数据。"""
    try:
        lexer = shlex.shlex(
            segment, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return [UNRESOLVED] if ">" in segment else []
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if (">" not in token or "<" in token
                or not set(token).issubset(set("&>|"))):
            continue
        if index + 1 >= len(tokens):
            targets.append(UNRESOLVED)
            continue
        destination = tokens[index + 1]
        # 文件描述符复制/关闭不是文件写目标。
        if destination.isdigit() or destination in {"-", "&"}:
            continue
        targets.append(_containment_target(destination, base))
    return targets


def _gnu_target_directory_targets(
    tokens: list[str], base: str,
) -> list[str] | None:
    """解析 cp/mv/install/ln 的 GNU target-directory，None 表示未使用。"""
    values, missing = _option_path_values(
        tokens,
        frozenset({"-t", "--target-directory"}),
        equals_flags=frozenset({"--target-directory"}),
    )
    for token in tokens[1:]:
        if (
            token.startswith("-t")
            and token != "-t"
            and not token.startswith("--")
        ):
            values.append(token[2:] or UNRESOLVED)
    if not values and not missing:
        return None
    targets = [
        _containment_target(value, base)
        for value in values
        if value != "-"
    ]
    if missing or not targets:
        targets.append(UNRESOLVED)
    return targets


_TAR_CAPABILITY_PROBE_FLAGS = frozenset({"--version", "--help"})


def _tar_write_targets(
    tokens: list[str], base: str,
) -> tuple[list[str], bool]:
    """按 tar 操作模式区分归档写入、解压目录与只读列表。"""
    if _is_exact_capability_probe(
        tokens[1:], flags=_TAR_CAPABILITY_PROBE_FLAGS,
    ):
        return [], False
    modes: set[str] = set()
    archive: str | None = None
    directory: str | None = None
    unresolved = False
    index = 1

    # POSIX/GNU 旧式 tar cf archive：首个无短横线 token 是选项字。
    if index < len(tokens):
        old_style = str(tokens[index])
        if (
            old_style
            and not old_style.startswith("-")
            and any(char in "cruxt" for char in old_style)
            and all(char.isalpha() for char in old_style)
        ):
            value_index = index + 1
            for char in old_style:
                if char in "cruxt":
                    modes.add(char)
                elif char in {"f", "C"}:
                    if value_index >= len(tokens):
                        unresolved = True
                        value = None
                    else:
                        value = str(tokens[value_index])
                        value_index += 1
                    if char == "f":
                        archive = value
                    else:
                        directory = value
            index = value_index

    long_modes = {
        "--create": "c",
        "--append": "r",
        "--update": "u",
        "--extract": "x",
        "--get": "x",
        "--list": "t",
    }
    while index < len(tokens):
        token = str(tokens[index])
        if token in long_modes:
            modes.add(long_modes[token])
            index += 1
            continue
        if token in {"-f", "--file", "-C", "--directory"}:
            if index + 1 >= len(tokens):
                unresolved = True
                value = None
            else:
                value = str(tokens[index + 1])
            if token in {"-f", "--file"}:
                archive = value
            else:
                directory = value
            index += 2
            continue
        if token.startswith("--file="):
            archive = token.split("=", 1)[1] or UNRESOLVED
            index += 1
            continue
        if token.startswith("--directory="):
            directory = token.split("=", 1)[1] or UNRESOLVED
            index += 1
            continue
        if token.startswith("-") and not token.startswith("--"):
            body = token[1:]
            position = 0
            while position < len(body):
                char = body[position]
                if char in "cruxt":
                    modes.add(char)
                if char in {"f", "C"}:
                    remainder = body[position + 1:]
                    if remainder:
                        value = remainder
                    elif index + 1 < len(tokens):
                        value = str(tokens[index + 1])
                        index += 1
                    else:
                        value = None
                        unresolved = True
                    if char == "f":
                        archive = value
                    else:
                        directory = value
                    break
                position += 1
        index += 1

    targets: list[str] = []
    mutating = bool(modes.intersection({"c", "r", "u", "x"}))
    if modes.intersection({"c", "r", "u"}):
        if archive == "-":
            pass
        elif archive:
            targets.append(_containment_target(archive, base))
        else:
            targets.append(UNRESOLVED)
    if "x" in modes:
        targets.append(
            _containment_target(directory or ".", base)
            if directory != UNRESOLVED
            else UNRESOLVED
        )
    if unresolved:
        targets.append(UNRESOLVED)
    return targets, mutating


def _unzip_write_targets(
    tokens: list[str], base: str,
) -> tuple[list[str], bool]:
    """区分 unzip 的只读 list/test 与真实解压。"""
    args = set(tokens[1:])
    if args.intersection({"-l", "-Z", "-t", "--help", "-h", "-v"}):
        return [], False
    values, missing = _option_path_values(
        tokens, frozenset({"-d"}), equals_flags=frozenset())
    attached = [
        token[2:] for token in tokens[1:]
        if token.startswith("-d") and token != "-d"
    ]
    values.extend(attached)
    targets = [
        _containment_target(value, base)
        for value in values
        if value
    ]
    if missing:
        targets.append(UNRESOLVED)
    if not targets:
        targets.append(_norm_path(".", base))
    return targets, True


def _zip_archive_targets(tokens: list[str], base: str) -> list[str]:
    """zip 的第一个数据 operand 是会被创建或改写的 archive。"""
    if any(token in {"-h", "--help", "-v", "--version"} for token in tokens[1:]):
        return []
    value_options = {
        "-b", "--temp-path", "-n", "--suffixes", "-P", "--password",
        "-s", "--split-size", "-x", "--exclude", "-i", "--include",
    }
    index = 1
    options_done = False
    while index < len(tokens):
        token = str(tokens[index])
        if not options_done and token == "--":
            options_done = True
            index += 1
            continue
        name = token.split("=", 1)[0]
        if not options_done and name in value_options:
            index += 1 if "=" in token else 2
            continue
        if not options_done and token.startswith("-"):
            index += 1
            continue
        return [] if token == "-" else [_containment_target(token, base)]
    return [UNRESOLVED]


def _ar_archive_targets(tokens: list[str], base: str) -> list[str]:
    """ar 只在 d/m/q/r/s 操作下改写 archive；t/p/x 为只读。"""
    index = 1
    while index < len(tokens) and str(tokens[index]).startswith("--"):
        index += 1
    if index >= len(tokens):
        return []
    operation = str(tokens[index]).lstrip("-")
    if not operation or not set(operation).intersection(set("dmqrs")):
        return []
    index += 1
    if index >= len(tokens):
        return [UNRESOLVED]
    archive = str(tokens[index])
    return (
        [UNRESOLVED]
        if archive.startswith("-")
        else [_containment_target(archive, base)]
    )


def _extract_write_targets(
        segment: str, base: str, *,
        opaque: bool = False) -> tuple[str, list[str], bool]:
    """Parse one segment's write targets.  Returns (op, targets, restore_allowed).

    ``targets`` may contain :data:`UNRESOLVED`, meaning "this command writes
    somewhere we cannot prove".  Callers must treat that as unknown-and-ask,
    never as nothing-to-check.  ``opaque`` carries the command-level verdict
    (a sourced script, a pipeline, a loop) that a single segment cannot see.
    """
    try:
        tokens = _strip_write_wrappers(shlex.split(segment, posix=True))
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return "", [], False
    op = tokens[0].split("/")[-1]
    # 破坏性动词 + 不可解析上下文 → 不管它长得多像字面量，都不可证明。
    if (opaque or _command_is_opaque(segment)) and _mentions_destructive(segment):
        return op, [UNRESOLVED], False
    targets: list[str] = []
    restore_allowed = False
    destructive = op in _DESTRUCTIVE_OPS
    pip_targets = _pip_install_write_targets(tokens, base)

    if pip_targets is not None:
        targets = pip_targets
    elif op == "mv":
        target_directory = _gnu_target_directory_targets(tokens, base)
        if target_directory is not None:
            targets = target_directory
        else:
            restore_allowed = _restore_mv_allowed(tokens, base)
            targets = [
                _containment_target(t, base) for t in _operand_args(tokens)
            ]
    elif op in {"cp", "ln"}:
        target_directory = _gnu_target_directory_targets(tokens, base)
        if target_directory is not None:
            targets = target_directory
        else:
            args = _operand_args(tokens)
            # source operands are read-only；仅最后一个 destination 会被写入。
            if args:
                targets = [_containment_target(args[-1], base)]
    elif op in {"rm", "truncate", "shred"}:
        targets = [_containment_target(t, base) for t in _operand_args(tokens)]
    elif op in {"touch", "mkdir", "rmdir"}:
        targets = [_containment_target(t, base) for t in _operand_args(tokens)]
    elif op in {"chmod", "chown"}:
        args = _operand_args(tokens)
        targets = [_containment_target(t, base) for t in args[1:]]
    elif op in _MUTATING_BUILD_HEADS:
        # CMake -B/--build 与 Make/Ninja -C 显式指定真实写入目录；只有
        # 未给选择器的构建才写 cwd。路径层与 activity guard 共用同一 helper，
        # 避免把标准 out-of-source configure 错判为写 source cwd。
        if not _is_build_probe(tokens):
            if op == "cmake" and "--install" in tokens[1:]:
                prefixes, prefix_missing = _option_path_values(
                    tokens,
                    frozenset({"--prefix"}),
                    equals_flags=frozenset({"--prefix"}),
                )
                targets = [
                    _containment_target(value, base)
                    for value in prefixes
                    if value != "-"
                ]
                if prefix_missing or not targets:
                    targets.append(UNRESOLVED)
            else:
                targets = _explicit_build_output_dirs(segment, base)
                if not targets:
                    targets = [_norm_path(".", base)]
    elif op in {"curl", "wget", "scp"}:
        targets = _network_write_targets(op, tokens, base)
    elif _COMPILER_RE.match(op):
        targets = _compiler_write_targets(tokens, base)
    elif op == "rsync":
        targets = _rsync_write_targets(tokens, base)
        destructive = any(t.startswith("--delete") for t in tokens[1:])
    elif op == "find":
        found = _find_destructive_targets(tokens, base)
        targets = found or []
        destructive = found is not None
    elif op == "git":
        found = _git_destructive_targets(tokens, base)
        targets = found or []
        destructive = found is not None
    elif op == "tar":
        targets, destructive = _tar_write_targets(tokens, base)
    elif op == "unzip":
        targets, destructive = _unzip_write_targets(tokens, base)
    elif op == "zip":
        targets = _zip_archive_targets(tokens, base)
        destructive = bool(targets)
    elif op == "ar":
        targets = _ar_archive_targets(tokens, base)
        destructive = bool(targets)
    elif op == "install":
        target_directory = _gnu_target_directory_targets(tokens, base)
        if target_directory is not None:
            targets = target_directory
        else:
            args = _operand_args(tokens)
            if "-d" in tokens[1:] or "--directory" in tokens[1:]:
                targets = [_containment_target(item, base) for item in args]
            elif args:
                targets = [_containment_target(args[-1], base)]
    elif op == "dd":
        for t in tokens[1:]:
            if t.startswith("of="):
                targets = [_containment_target(t[3:], base)]
    elif op == "sed" and any(t == "-i" or t.startswith("-i") for t in tokens[1:]):
        targets = _sed_inplace_targets(tokens, base)
    elif op == "perl" and any("i" in t and t.startswith("-") for t in tokens[1:]):
        targets = [_containment_target(t, base) for t in _command_args(tokens)]
    elif op == "tee":
        targets = [_containment_target(t, base) for t in _operand_args(tokens)]

    application_targets, application_mutating = (
        _known_application_write_targets(tokens, base)
    )
    targets.extend(application_targets)
    destructive = destructive or application_mutating
    targets.extend(_redirection_write_targets(segment, base))

    # A destructive verb whose operands we could not read at all is exactly the
    # case that used to fall through as "no targets" and run unchecked.
    # (Opaque contexts were already handled at the top of this function.)
    if destructive and not targets:
        targets = [UNRESOLVED]

    # 标记项不是路径，绝不能进 _dedupe_paths —— 那里会对每一项做 abspath，
    # 把 `<unresolved>` 变成 `<cwd>/<unresolved>`，标记就此静默变成一个假路径。
    markers = [m for m in (UNRESOLVED, REMOTE_SCRATCH) if m in targets]
    concrete = [t for t in _dedupe_paths(
        [t for t in targets if t not in {UNRESOLVED, REMOTE_SCRATCH}])
        if not _ignore_write_target(t)]
    return op, markers + concrete, restore_allowed


_EXEC_WRAPPERS = {"nohup", "env", "time", "exec", "stdbuf", "nice", "ionice"}
_MPI_LAUNCHERS = {"mpirun", "mpiexec", "srun", "orterun"}

# MPI launcher 带值选项 → 值的个数。值本身常是路径（--hostfile /path/to/hosts），
# 必须跳过，否则会被误当成被启动程序检查。--opt=value 形式以 - 开头自然跳过。
_MPI_OPT_NARGS = {
    "-np": 1, "-n": 1, "-c": 1, "--ntasks": 1, "--np": 1,
    "-hostfile": 1, "--hostfile": 1, "-machinefile": 1, "--machinefile": 1,
    "-f": 1, "-rf": 1, "--rankfile": 1, "-configfile": 1, "--configfile": 1,
    "--map-by": 1, "--bind-to": 1, "--rank-by": 1,
    "-wdir": 1, "--wdir": 1, "-path": 1, "--path": 1,
    "-x": 1, "-genvlist": 1, "-env": 2, "-genv": 2, "-mca": 2, "--mca": 2,
    "-gpus-per-node": 1, "--gpus-per-node": 1, "-N": 1, "--nodes": 1,
    "-p": 1, "--partition": 1, "-t": 1, "--time": 1, "-J": 1, "--job-name": 1,
}


def _mpi_launch_target(tokens: list[str]) -> str | None:
    """返回 MPI launcher 后真正被启动的程序 token（跳过选项及其值）。"""
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            i += 1 + _MPI_OPT_NARGS.get(t, 0)
            continue
        return t
    return None


def _strip_exec_wrappers(tokens: list[str]) -> list[str]:
    """剥掉 nohup/env/time 等包装器及其 VAR=/选项前缀，露出真实 argv0。"""
    out = list(tokens)
    while out:
        t = out[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            out.pop(0)
        elif t.split("/")[-1] in _EXEC_WRAPPERS:
            out.pop(0)
            while out and (out[0].startswith("-")
                           or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0])):
                out.pop(0)
        else:
            break
    return out


def collect_exec_path_targets(
    cmd: str, cwd: str | None = None,
) -> list[dict[str, str]]:
    """收集命令中路径形态的可执行目标（argv0 与 MPI launcher 的启动程序）。

    只复用 exec_preflight 既有的词法组件（命令内变量展开、shell 段切分、
    包装器剥离、MPI 目标定位），不判断存在性/执行位——判定归
    `_exec_preflight_bash`。返回 [{"token","path","segment","base"}]，
    `path` 是按各段 `cd` 语义解析后的绝对路径，`base` 是该段生效的 cwd。"""
    if not cmd or not cmd.strip():
        return []
    expanded = _expand_cmd_vars(cmd)
    base = _command_base_dir(expanded, cwd)
    targets: list[dict[str, str]] = []
    for segment in _split_shell_segments(expanded):
        cd_match = re.match(rf"^cd\s+({_PATH_TOKEN_RE})\s*$", segment)
        if cd_match:
            base = _norm_path(cd_match.group(1), base)
            continue
        try:
            tokens = _strip_exec_wrappers(shlex.split(segment, posix=True))
        except ValueError:
            continue
        if not tokens:
            continue
        candidates = [tokens[0]]
        if tokens[0].split("/")[-1] in _MPI_LAUNCHERS:
            mpi_target = _mpi_launch_target(tokens)
            if mpi_target is not None:
                candidates.append(mpi_target)
        for token in candidates:
            if not (token.startswith(("/", "./", "../", "~")) or "/" in token):
                continue
            targets.append({
                "token": token,
                "path": _norm_path(token, base),
                "segment": segment,
                "base": base,
            })
    return targets


_IMAGE_PROVIDED_HOST_ROOTS = (
    # core.sandbox._validate_mount 对落在这些树内的任何路径直接 raise，
    # 因此它们永远不可能是 bind mount：容器里看到的只能是镜像自带的文件。
    "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/dev", "/etc", "/proc", "/root", "/run", "/sys",
    # /opt 不是禁止挂载项，但它是镜像工具链的惯用位置，而且一个宿主机上并不
    # 存在的 /opt/<toolchain> 依赖角色会在挂载最小化时上溯成 /opt，凭空给整棵
    # /opt 赋予宿主机判定权。镜像内的 /opt/openmpi/bin/mpirun 会因此被判成
    # "不存在"，与 E-14 同形。
    "/opt",
)


def _image_provided_host_roots() -> tuple[str, ...]:
    """宿主机对哪些树没有判定权：容器里的它们由镜像提供。

    与 ``core.sandbox._SENSITIVE_HOST_ROOTS`` 取并集，避免那份禁挂清单变长
    之后本清单悄悄落后。
    """
    roots = list(_IMAGE_PROVIDED_HOST_ROOTS)
    try:
        from core.sandbox import _SENSITIVE_HOST_ROOTS
        for root in _SENSITIVE_HOST_ROOTS:
            if str(root) not in roots:
                roots.append(str(root))
    except Exception:
        pass
    return tuple(roots)


def _path_forms(value: Any) -> list[Path]:
    """一条路径的两种归一形态：词法（不跟随符号链接）与解析后。"""
    forms: list[Path] = []
    try:
        expanded = os.path.expanduser(str(value))
    except (AttributeError, TypeError, ValueError):
        return forms
    for factory in (
        lambda: Path(os.path.abspath(expanded)),
        lambda: Path(expanded).resolve(strict=False),
    ):
        try:
            candidate = factory()
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate not in forms:
            forms.append(candidate)
    return forms


def _path_within_roots(
    path: str, roots: Sequence[str], *, resolve_target: bool = True,
) -> bool:
    """``path`` 是否落在 ``roots`` 中某一棵树内（含等于根本身）。

    默认对目标取词法形态与解析形态的并集：解析形态容忍根路径自身含符号链接，
    词法形态则保证受管挂载点内的目标不会因为软链指向别处而被判到范围外。并集
    只会让判定范围更大，不会因为一次归一化选择把该判的目标漏掉。

    ``resolve_target=False`` 只按词法判定，用于"这是不是镜像自带的系统树"这
    一问：``workdir/solver -> /usr/local/bin/nope`` 这条软链住在受管挂载点上，
    它是不是悬空由宿主机说了算；跟着它解析到 /usr 再放行，等于把上一轮构建
    遗留的悬空软链放进真实提交。
    """
    targets = (_path_forms(path) if resolve_target
               else _path_forms(path)[:1])
    if not targets:
        return False
    for root in roots:
        for base in _path_forms(root):
            for target in targets:
                if target == base or base in target.parents:
                    return True
    return False


def _host_verifiable_exec_target(
    path: str, roots: Sequence[str] | None,
) -> bool:
    """宿主机文件系统对这个可执行目标有没有判定权。

    - ``roots is None``：本地 bash 主路径，全盘按宿主机判定（行为不变）。
    - 落在镜像自带系统树内：永远不可能是 bind mount，宿主机无判定权。
    - ``roots`` 为空：判定范围算不出来（路径角色合同不可读之类）。此时
      fail-closed 回宿主机口径，而不是把守卫静默关成全放行。
    """
    if roots is None:
        return True
    if _path_within_roots(
            path, _image_provided_host_roots(), resolve_target=False):
        return False
    if not roots:
        return True
    return _path_within_roots(path, roots)


def _exec_preflight_bash(
    cmd: str,
    cwd: str | None = None,
    *,
    staged_targets: dict[str, dict[str, str]] | None = None,
    check_script_interpreter: bool = True,
    host_verifiable_roots: Sequence[str] | None = None,
) -> dict | None:
    """机械预检：路径形式的可执行目标（./x、/abs/x、bin/x，含 MPI launcher 后
    的程序）必须存在且有执行位，否则事前拒绝。

    回归来源（2026-07-13 meiyu e2e）：HPC 二进制在错误 cwd 下启动静默秒崩、
    launcher 在 workdir 无目标文件时 not found——prompt 规则只能约束，不能强制；
    这里做成确定性防线。argv0 含 / 即文件系统路径（shell 语义不走 PATH），
    零误报；裸命令名走 PATH 查找，不碰。MPI launcher 的选项值（--hostfile
    /path 等）经 _mpi_launch_target 跳过，不会误当被启动程序。

    提交链专用可选参数（默认关，本地 bash 主路径行为不变）：
    - ``staged_targets``：stage_in 将物化的目标（dst 绝对路径 → 收据
      {mode, sha256, src}）。目标此刻不在磁盘上，按收据判定：mode=0755
      放行，无执行位拒绝并给出可执行下一步。
    - ``check_script_interpreter=False``：payload 将在容器/作业环境内运行，
      宿主机上的 shebang 解释器解析对它不成立，跳过该检查。
    - ``host_verifiable_roots``：宿主机文件系统对哪些树有判定权（本次作业
      真实解析出的 bind mount 集合）。给出后按 `_host_verifiable_exec_target`
      判定：镜像自带系统树内的目标一律放行，其余只在落进这些树时才做存在性/
      执行位判定；空列表＝范围不可知，fail-closed 回宿主机口径。默认 None
      ＝全盘按宿主机判定（本地 bash 主路径行为不变）。这条开关与
      ``check_script_interpreter`` 同源：payload 运行在受管容器里，镜像自带的
      绝对系统路径（``/usr/local/bin/python3.12`` 之类）在宿主机上根本不存在，
      拿宿主机 ``os.path.exists`` 判它等于把镜像内完全可用的解释器判成"不存在"。
      2026-08-30 airsea run 1788187822-56908f 实证：四次真实提交被这样误拦死。"""


    if not cmd or not cmd.strip():
        return None

    def _script_interpreter(path: str) -> tuple[str | None, str | None, bool]:
        """返回脚本 shebang、解释器显示名及其可用性。

        只处理内核会识别的首行 ``#!``，不尝试解释脚本内容；因此不会把普通
        可执行二进制或动态 shell 命令误判为脚本。``/usr/bin/env`` 的目标解释器
        也一并解析，避免 ``env csh`` 把根因藏成笼统的 ``ENOENT``。
        """
        try:
            with open(path, "rb") as handle:
                first = handle.readline(512)
        except OSError:
            return None, None, True
        if not first.startswith(b"#!"):
            return None, None, True
        directive = first[2:].decode("utf-8", errors="replace").strip()
        try:
            parts = shlex.split(directive, posix=True)
        except ValueError:
            return directive, None, True
        if not parts:
            return directive, None, True

        interpreter = parts[0]
        if os.path.basename(interpreter) == "env":
            # ``env`` 可带 ``-S``、``-i`` 或 ``NAME=value``；第一个真正的
            # 命令词才是脚本所需解释器。
            interpreter = ""
            for part in parts[1:]:
                if part == "-S" or part.startswith("-"):
                    continue
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", part):
                    continue
                interpreter = part
                break
            if not interpreter:
                return directive, None, True
            resolved = shutil.which(interpreter)
            return directive, resolved or interpreter, bool(resolved)
        if os.path.isabs(interpreter):
            return directive, interpreter, os.path.exists(interpreter)
        resolved = shutil.which(interpreter)
        return directive, resolved or interpreter, bool(resolved)

    def _check(token: str, p: str, seg: str, seg_base: str) -> dict | None:
        staged = (staged_targets or {}).get(p)
        if staged is not None:
            if str(staged.get("mode")) == "0755":
                # stage_in 会在作业启动前以可执行位物化该目标：按收据放行，
                # 内容一致性由收据 sha256 在提交前复检保证。
                return None
            return {"status": "error", "error": (
                "⛔ 该目标将由 stage_in 物化，但源文件没有可执行位"
                f"（收据 mode={staged.get('mode')}），作业启动时会 "
                "Permission denied（exec_preflight 事前拦截，未提交）。\n"
                f"目标：{token}（stage_in dst，src={staged.get('src')}）\n"
                "下一步二选一：① 先对 src 执行 `chmod +x` 再重新调用 "
                "submit_job（收据 mode 会重算为 0755）；② 命令改用 "
                "`bash <dst路径>` 显式解释执行。\n"
                f"命令片段：{seg[:200]}")}
        if not _host_verifiable_exec_target(p, host_verifiable_roots):
            # 目标不在受管挂载点内：它由镜像/作业环境提供，宿主机看不见不等于
            # 容器里没有。放行，交由作业启动时的真实语义处理。
            return None
        if not os.path.exists(p):
            return {"status": "error", "error": (
                "⛔ 可执行目标不存在（exec_preflight 事前拦截）。\n"
                f"目标：{token}\n解析为：{p}\n（解析 cwd：{seg_base}）\n\n"
                "⚠️ 整条命令一句都没有执行：其中的 mkdir、写文件（含 heredoc）、"
                "编译等前置语句同样【未生效】。不要假设目录已建好或源码已写出——"
                "下一条命令必须自己重新完成这些前置步骤。\n\n"
                "常见原因：① 工作目录不对（用 `cwd=<运行目录>` 参数声明，"
                "别靠命令内的 `cd`；nohup/mpirun 行内同理）；"
                "② 二进制路径拼错或产物还没构建。\n"
                "正确做法是拆成两步：先一条命令建目录+写源码+编译并 `ls` 确认产物，"
                "再单独一条命令执行已存在的二进制。\n"
                f"命令片段：{seg[:200]}")}
        if os.path.isfile(p) and not os.access(p, os.X_OK):
            return {"status": "error", "error": (
                "⛔ 目标存在但无执行权限（exec_preflight 事前拦截，命令未执行）。\n"
                f"目标：{p}\n如确为可执行文件，先 `chmod +x`；"
                "如是脚本可用 `bash <path>` 显式解释执行。\n"
                f"命令片段：{seg[:200]}")}
        if check_script_interpreter and os.path.isfile(p):
            directive, interpreter, interpreter_available = _script_interpreter(p)
            if directive is not None and interpreter is not None:
                if not interpreter_available:
                    return {"status": "error", "error": (
                        "⛔ 脚本解释器不可用（exec_preflight 事前拦截，命令未执行）。\n"
                        f"脚本：{p}\nshebang：#!{directive}\n"
                        f"缺失解释器：{interpreter}\n\n"
                        "这是环境/入口前置条件失败，不得把官方脚本静默回退为裸 `make`。"
                        "请先安装或声明正确解释器；若上游文档指定了另一入口，先记录"
                        "证据并更新构建配方后再执行。\n"
                        f"命令片段：{seg[:200]}"),
                        "blocker": {
                            "kind": "script_interpreter_unavailable",
                            "script": p,
                            "shebang": directive,
                            "interpreter": interpreter,
                        }}
        return None

    for entry in collect_exec_path_targets(cmd, cwd):
        blocked = _check(
            entry["token"], entry["path"], entry["segment"], entry["base"])
        if blocked:
            return blocked
    return None


# ─────────────────────────────────────────────────────────────────────────────
# runtime ABI/toolchain consistency preflight（2026-07-13 设计定稿）
# 定位：抓最危险的 缺库 / 混 MPI / 错环境 启动，不是环境一致性的形式化证明。
# 判据分层：① 编译档案对账（platform_profile/build_env 记录的 MPI 前缀）>
#          ② libmpi soname × launcher --version 族判定 > ③ 前缀差异（仅 warning）。
# 关键约束：ldd 必须在【命令将要使用的环境】下跑（含行内 env LD_LIBRARY_PATH=、
# 命令内变量赋值），否则合法的行内补库启动形态会被误判缺库；仅对 ELF 做 ldd。
# ─────────────────────────────────────────────────────────────────────────────


def _exec_target_diagnosis(cmd: str, cwd: str | None = None) -> dict | None:
    """bash 以 rc=127/126 收场之后的机械诊断：找出路径形式的可执行目标里
    不存在或无执行位的那个。恒不拦截——只产出挂到结果上的诊断 dict。

    判决拆除·第三波（sb:1855/1868 降格，2026-09-02）：本地 bash 路径上的
    「可执行目标不存在」曾是事前拦整条命令的预测失败墙；现在让 bash 自己
    拒绝，再把同一份诊断附上。与旧墙相反：其前的语句（mkdir/heredoc/编译）
    **已经生效**，文案如实说明。提交链（submit_job payload 预检）仍走
    `_exec_preflight_bash`：那条路上作业一旦进调度器就不可逆，事前判定成立。
    """
    for entry in collect_exec_path_targets(cmd, cwd):
        token, path, segment = entry["token"], entry["path"], entry["segment"]
        if not os.path.exists(path):
            return {
                "kind": "exec_target_missing", "target": token, "resolved": path,
                "cwd": entry["base"], "segment": segment[:200],
                "hint": (
                    "可执行目标不存在（bash rc=127）。命令已执行到这一步：其前的 "
                    "mkdir、写文件（含 heredoc）、编译等语句**已生效**，不要重做。"
                    "常见原因：① 工作目录不对（用 `cwd=<运行目录>` 参数声明，别靠"
                    "命令内的 `cd`；nohup/mpirun 行内同理）；② 二进制路径拼错或"
                    "产物还没构建——先 `ls` 确认产物再单独执行。"),
            }
        if os.path.isfile(path) and not os.access(path, os.X_OK):
            return {
                "kind": "exec_target_not_executable", "target": token,
                "resolved": path, "cwd": entry["base"], "segment": segment[:200],
                "hint": (
                    "目标存在但无执行权限（bash rc=126）。如确为可执行文件，先 "
                    "`chmod +x`；如是脚本可用 `bash <path>` 显式解释执行。"),
            }
    return None


_MPI_SONAME_FAMILIES = (
    ("openmpi", re.compile(r"libmpi\.so\.(?:40|[4-9]\d)")),
    ("mpich",   re.compile(r"libmpi\.so\.12")),
    ("intel",   re.compile(r"libmpi_rt|/impi/|libmpifort\.so\.12.*intel", re.I)),
)
_MPI_VERSION_FAMILIES = (
    ("openmpi", re.compile(r"open\s*mpi|openrte", re.I)),
    ("mpich",   re.compile(r"hydra|mpich", re.I)),
    ("intel",   re.compile(r"intel\(r\)\s*mpi", re.I)),
)


def _is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _cmd_ld_library_path(expanded_cmd: str) -> str | None:
    """从展开后的命令文本提取将生效的 LD_LIBRARY_PATH（env 前缀 / 行内赋值）。"""
    hits = re.findall(r'LD_LIBRARY_PATH=("([^"]*)"|\'([^\']*)\'|(\S+))', expanded_cmd)
    if not hits:
        return None
    g = hits[-1]
    return g[1] or g[2] or g[3] or None


def _framework_guard_failure(
    guard: str,
    failure: BaseException | str,
    *,
    reason: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Return one non-bypassable framework blocker before payload spawn."""
    if isinstance(failure, BaseException):
        exception_type = type(failure).__name__
        detail = f"{exception_type}: {failure}"
    else:
        exception_type = "FrameworkCheckError"
        detail = str(failure)
    detail = detail[:400] + ("…" if len(detail) > 400 else "")
    blocker_kind = reason or f"{guard}_unavailable"
    return {
        "status": "error",
        "reason": blocker_kind,
        "error": (
            f"Experiment {guard} framework check unavailable; payload 未启动。"
            f"{detail}"
        ),
        "blocker": {
            "kind": blocker_kind,
            "guard": guard,
            "source": source or guard,
            "exception_type": exception_type,
            "detail": detail,
            "suggested_owner": "framework",
            "retryable_after_change": True,
            "bypass_allowed": False,
        },
    }


def _is_non_bypassable_framework_block(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("blocker"), dict)
        and value["blocker"].get("bypass_allowed") is False
    )


async def _sandbox_probe(
    state: Any,
    argv: list[str],
    cwd: str,
    environment: dict[str, str] | None = None,
) -> str | dict[str, Any]:
    """Run a trusted ABI probe inside the execution boundary, never on the host.

    ldd 会驱动目标二进制的动态加载器 —— 对模型指定的二进制裸跑 ldd 就是在宿主
    上执行不可信代码，所以探针必须在墙内。执行器分档后按后端走两条路：image
    后端经 prepare_attempt_command 的容器请求（原样保留，其 subprocess.run 调用
    点在 framework_exemptions 登记）；原生后端（linux/darwin）经咽喉
    spawn_and_wait（Landlock/seatbelt 逐命令根）。两条路共用同一套根收窄：
    输入/能力根一律降为只读，只授予框架自有的临时 scratch 为可写。
    """
    from core.sandbox import SandboxLimits, prepare_attempt_command
    try:
        try:
            from .subprocess_policy import path_has_local_write_capability
        except ImportError:
            from tools.subprocess_policy import path_has_local_write_capability

        runtime_candidate = experiment_output_dir(
            state, "runtime", create=False).expanduser().resolve(strict=False)
        if not path_has_local_write_capability(state, runtime_candidate):
            raise RuntimeError(
                f"path_capability_required: ABI probe runtime={runtime_candidate}"
            )
        runtime_root = experiment_output_dir(
            state, "runtime", create=True).expanduser().resolve(strict=True)
        scratch_candidate = runtime_root / ".abi-probe-scratch"
        if scratch_candidate.is_symlink():
            raise RuntimeError(
                f"runtime_abi_probe_scratch_symlink: {scratch_candidate}"
            )
        scratch_candidate.mkdir(mode=0o700, parents=False, exist_ok=True)
        probe_scratch = scratch_candidate.resolve(strict=True)
        if (
            not probe_scratch.is_dir()
            or not probe_scratch.is_relative_to(runtime_root)
            or probe_scratch == runtime_root
        ):
            raise RuntimeError(
                f"runtime_abi_probe_scratch_invalid: {probe_scratch}"
            )
        if not path_has_local_write_capability(state, probe_scratch):
            raise RuntimeError(
                f"path_capability_required: ABI probe scratch={probe_scratch}"
            )
        os.chmod(probe_scratch, 0o700)
    except Exception as exc:
        reason = (
            "path_capability_required"
            if "path_capability_required" in str(exc)
            else "runtime_abi_probe_unavailable"
        )
        return _framework_guard_failure(
            "runtime_abi_probe",
            exc,
            reason=reason,
            source="probe_scratch",
        )

    try:
        _ensure_hardened_attempt_manifest(state)
    except Exception as exc:
        reason = (
            "hardened_sandbox_profile_required"
            if "hardened_sandbox_profile_required" in str(exc)
            else "runtime_abi_probe_unavailable"
        )
        return _framework_guard_failure(
            "runtime_abi_probe",
            exc,
            reason=reason,
            source="attempt_manifest",
        )

    try:
        capability_writable, capability_readonly = _sandbox_roots_for_payload(
            state,
            cwd,
            sandbox_profile="bash",
            authorized_targets=[],
        )
        probe_cwd = Path(cwd).expanduser().resolve(strict=True)
        readonly: list[Path] = []
        for value in [*capability_writable, *capability_readonly]:
            root = Path(value).expanduser().resolve(strict=False)
            if root not in readonly:
                readonly.append(root)
        if not any(
            probe_cwd == root or probe_cwd.is_relative_to(root)
            for root in readonly
        ):
            raise RuntimeError(
                f"path_capability_required: ABI probe cwd={probe_cwd}"
            )
        if not any(
            probe_scratch == Path(root).expanduser().resolve(strict=False)
            or probe_scratch.is_relative_to(
                Path(root).expanduser().resolve(strict=False))
            for root in capability_writable
        ):
            raise RuntimeError(
                f"path_capability_required: ABI probe scratch={probe_scratch}"
            )
        if probe_scratch in readonly:
            raise RuntimeError(
                f"runtime_abi_probe_scratch_overlap: {probe_scratch}"
            )
        probe_environment = dict(environment or {})
        probe_environment["TMPDIR"] = str(probe_scratch)
        probe_argv = [
            "/usr/bin/env",
            f"TMPDIR={probe_scratch}",
            f"TMP={probe_scratch}",
            f"TEMP={probe_scratch}",
            *argv,
        ]
    except Exception as exc:
        reason = (
            "path_capability_required"
            if "path_capability_required" in str(exc)
            else "runtime_abi_probe_unavailable"
        )
        return _framework_guard_failure(
            "runtime_abi_probe",
            exc,
            reason=reason,
            source="bash_sandbox_roots",
        )

    launch = None
    output: str | None = None
    probe_failure: dict[str, Any] | None = None
    cleanup_failure: dict[str, Any] | None = None
    try:
        probe_limits = SandboxLimits(
            memory_bytes=1024**3,
            cpus=1,
            pids=32,
            walltime_seconds=15,
            storage_bytes=64 * 1024**2,
            output_bytes=2 * 1024**2,
        )
        # 执行器分档：探针经咽喉 spawn_and_wait 在墙内执行（#793 owner 迁移，
        # select_backend/prepare 只许咽喉碰 —— test_..._one_throat 唯一性闸）。
        # image 分支自 PR C 删除 image 后端起不可达（attempt_capability 不再返回
        # "image"），保留仅为对齐 framework_exemptions 里 _sandbox_probe 的
        # subprocess.run 登记（wangd 属地）；豁免登记清理后本分支应删。
        from core import isolation

        if isolation.attempt_capability()["backend"] == "image":
            launch = prepare_attempt_command(
                probe_argv,
                state=state,
                cwd=probe_cwd,
                writable_roots=[probe_scratch],
                readonly_roots=readonly,
                limits=probe_limits,
                environment=probe_environment,
            )
            result = subprocess.run(
                launch.argv,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode != 0:
                diagnostic = (result.stderr or result.stdout or "")[-400:]
                probe_failure = _framework_guard_failure(
                    "runtime_abi_probe",
                    RuntimeError(
                        f"trusted probe exited {result.returncode}: {diagnostic}"
                    ),
                    reason="runtime_abi_probe_unavailable",
                    source="attempt_request_exit",
                )
            else:
                output = (result.stdout or "") + (result.stderr or "")
        else:
            from shared.lib.cancellable_subprocess import spawn_and_wait

            status, returncode, stdout_bytes, stderr_bytes = await spawn_and_wait(
                *probe_argv,
                state=state,
                timeout=20,
                cwd=str(probe_cwd),
                writable_roots=[probe_scratch],
                readonly_roots=list(readonly),
                sandbox_limits=probe_limits,
                sandbox_environment=probe_environment,
            )
            if status == "timeout":
                probe_failure = _framework_guard_failure(
                    "runtime_abi_probe",
                    subprocess.TimeoutExpired(cmd=list(probe_argv), timeout=20),
                    reason="runtime_abi_probe_unavailable",
                    source="attempt_request_timeout",
                )
            elif status != "done" or returncode != 0:
                diagnostic = (
                    (stderr_bytes or stdout_bytes or b"")
                    .decode("utf-8", errors="replace")[-400:]
                )
                probe_failure = _framework_guard_failure(
                    "runtime_abi_probe",
                    RuntimeError(
                        f"trusted probe {status} rc={returncode}: {diagnostic}"
                    ),
                    reason="runtime_abi_probe_unavailable",
                    source="attempt_request_exit",
                )
            else:
                output = (
                    stdout_bytes.decode("utf-8", errors="replace")
                    + stderr_bytes.decode("utf-8", errors="replace")
                )
    except subprocess.TimeoutExpired as exc:
        probe_failure = _framework_guard_failure(
            "runtime_abi_probe",
            exc,
            reason="runtime_abi_probe_unavailable",
            source="attempt_request_timeout",
        )
    except Exception as exc:
        probe_failure = _framework_guard_failure(
            "runtime_abi_probe",
            exc,
            reason="runtime_abi_probe_unavailable",
            source="prepare_or_request_client",
        )
    finally:
        if launch is not None:
            try:
                launch.cleanup()
            except Exception as exc:
                cleanup_failure = _framework_guard_failure(
                    "runtime_abi_probe",
                    exc,
                    reason="runtime_abi_probe_unavailable",
                    source="attempt_request_cleanup",
                )
    if cleanup_failure is not None:
        return cleanup_failure
    if probe_failure is not None:
        return probe_failure
    if output is None:
        return _framework_guard_failure(
            "runtime_abi_probe",
            "trusted probe returned neither output nor a terminal failure",
            reason="runtime_abi_probe_unavailable",
            source="attempt_request_client",
        )
    return output


async def _run_ldd(
    state: Any,
    binary: str,
    ld_path: str | None,
    cwd: str,
) -> str | dict[str, Any]:
    environment = {"LD_LIBRARY_PATH": ld_path} if ld_path else None
    return await _sandbox_probe(state, ["ldd", binary], cwd, environment)


def _parse_ldd(output: str) -> tuple[list[str], str | None]:
    """返回 (not_found 库名列表, 解析到的 libmpi 真实路径)。"""
    missing: list[str] = []
    libmpi: str | None = None
    for line in output.splitlines():
        if "not found" in line:
            missing.append(line.split("=>")[0].strip())
        m = re.match(r"\s*(libmpi[^\s]*)\s*=>\s*(\S+)", line)
        if m and m.group(2).startswith("/") and libmpi is None:
            libmpi = os.path.realpath(m.group(2))
    return missing, libmpi


def _family_from_soname(libmpi_path: str) -> str | None:
    for fam, pat in _MPI_SONAME_FAMILIES:
        if pat.search(libmpi_path):
            return fam
    return None


async def _launcher_version_output(
    state: Any, launcher: str, cwd: str,
) -> str | dict[str, Any]:
    return await _sandbox_probe(state, [launcher, "--version"], cwd)


def _family_from_version(output: str) -> str | None:
    for fam, pat in _MPI_VERSION_FAMILIES:
        if pat.search(output):
            return fam
    return None


def _install_prefix(path: str) -> str:
    """向上剥 bin/、lib/、lib64/ 得到安装前缀，用于同族时的前缀 warning。"""
    p = Path(os.path.realpath(path))
    if p.is_file() or not p.is_dir():
        p = p.parent
    while p.name in {"bin", "lib", "lib64", "sbin"}:
        p = p.parent
    return str(p)


def _recorded_mpi_prefixes(state: Any) -> list[str]:
    """编译档案：platform_profile / build_env artifact 里记录的 mpirun/mpicc 路径。"""
    prefixes: list[str] = []
    try:
        v = state.hook_state.get("build_mpi_prefix")
        if v:
            prefixes.append(str(v))
    except Exception:
        pass
    try:
        for a in state.list_artifacts():
            if a.get("type") not in {"platform_profile", "build_env"}:
                continue
            rec = state.read_artifact(a["id"]) or {}
            content = str(rec.get("content") or "") + _json.dumps(rec.get("metadata") or {})
            for m in re.finditer(r"(?:mpirun|mpiexec|mpicc|mpif90|mpifort)\s*[:=]\s*(/\S+)", content):
                prefixes.append(_install_prefix(m.group(1)))
    except Exception:
        pass
    return _dedupe_paths(prefixes)


def _resolve_launcher(name: str) -> str | None:
    if "/" in name:
        p = os.path.realpath(os.path.expanduser(name))
        return p if os.path.exists(p) else None
    # Bare launchers are resolved by the trusted image, not by the host control plane.
    return name


async def _runtime_abi_preflight_bash(state: Any, cmd: str,
                                cwd: str | None = None,
                                *,
                                path_substitutions: dict[str, str] | None = None,
                                host_verifiable_roots: Sequence[str] | None = None,
                                ) -> dict | None:
    """major run / MPI 启动命令的 ABI 一致性预检。恒返回 None（不拦截）。

    判决拆除（2026-08-31，专审二 sb:2093/2109/2121 删 ×3）：全部检查降为
    transcript warning（runtime_preflight_warning），领域修复知识挂 hint。

    探针目标的正确性与拦不拦无关，两个结构化参数保留：
    ``path_substitutions``（默认空）把 payload 视角的目标路径映射到宿主机上
    真实存在的同内容文件——提交链里 stage_in 的 dst 尚未物化，ELF 判定与 ldd
    在映射后的 src 上进行；warning 仍指向 payload 路径。
    ``host_verifiable_roots``（默认 None ＝全盘按宿主机判定）与
    ``_exec_preflight_bash`` 共用 `_host_verifiable_exec_target`：镜像自带系统树
    内的目标不做 ELF/ldd 判定。镜像内路径的 ABI 事实在镜像里，宿主机上同名文件
    是另一个二进制，拿它的 ldd 结果下结论既可能误报也可能漏报。"""
    expanded = _expand_cmd_vars(cmd)
    base = _command_base_dir(expanded, cwd)
    for segment in _split_shell_segments(expanded):
        cd_match = re.match(rf"^cd\s+({_PATH_TOKEN_RE})\s*$", segment)
        if cd_match:
            base = _norm_path(cd_match.group(1), base)
            continue
        try:
            tokens = _strip_exec_wrappers(shlex.split(segment, posix=True))
        except ValueError:
            continue
        if not tokens:
            continue
        argv0 = tokens[0]
        launcher_tok: str | None = None
        target_tok: str | None = None
        if argv0.split("/")[-1] in _MPI_LAUNCHERS:
            launcher_tok = argv0
            target_tok = _mpi_launch_target(tokens)
        elif "/" in argv0:
            target_tok = argv0
        if not target_tok or "/" not in target_tok:
            continue
        binary = _norm_path(target_tok, base)
        probe_binary = (path_substitutions or {}).get(binary, binary)
        if not _host_verifiable_exec_target(probe_binary, host_verifiable_roots):
            continue   # 镜像内目标：宿主机视角的 ELF/ldd 判定对它不成立

        if not os.path.isfile(probe_binary) or not _is_elf(probe_binary):
            continue   # 不存在/无执行位由 bash 自己拒绝（rc 127/126 后附 exec_diagnosis）；非 ELF 不做 ldd
        if launcher_tok is None and binary.startswith(
                ("/usr/", "/bin/", "/sbin/", "/lib")):
            continue   # 系统工具按绝对路径调用（非 launcher）不属于 HPC 目标

        ld_path = _cmd_ld_library_path(expanded)
        ldd_result = _run_ldd(state, probe_binary, ld_path, base)
        if asyncio.iscoroutine(ldd_result) or asyncio.isfuture(ldd_result):
            # 测试桩常用同步 lambda 替换 _run_ldd —— 真实实现是协程，两者都接。
            ldd_result = await ldd_result
        if not isinstance(ldd_result, str):
            continue   # 探针不可用/返回非文本 → 降级放行（判决拆除：不替调用方预测失败）
        missing, libmpi = _parse_ldd(ldd_result)
        # 判决拆除（sb:2093/2109/2121 删 ×3，2026-08-31，专审二）：三条预测失败
        # 闸整体降为 warning —— 预测失败替调用方做决定；放行后果由现实兜住
        # （链接器立刻失败≈零成本，混族 MPI 最坏本机超时由 timeout_escalation
        # A 类熔断接管）。同函数分支③同族异前缀本来就只 warning，自证这是阈值
        # 判决非安全边界。领域知识挂到 warning hint 上零成本保留。
        if missing:
            try:
                state.append_transcript(
                    "runtime_preflight_warning",
                    binary=binary, missing_libraries=missing[:8],
                    ld_library_path_source=("command" if ld_path else "process_default"),
                    cmd_preview=segment[:200],
                    hint=("目标二进制有未解析的共享库依赖，运行大概率立刻失败。"
                          "只把缺失库所在的**单个目录**加进 LD_LIBRARY_PATH"
                          "（find 定位后 export），禁止整个 conda/lib 塞入——"
                          "会引入第二套 MPI/netCDF 运行时。"))
            except Exception:
                pass

        if libmpi and launcher_tok:
            launcher = _resolve_launcher(launcher_tok)
            if launcher:
                # ① 编译档案对账 → warning，不拦
                recorded = _recorded_mpi_prefixes(state)
                lp = _install_prefix(launcher) if os.path.isabs(launcher) else None
                if recorded and lp is not None and lp not in recorded:
                    try:
                        state.append_transcript(
                            "runtime_preflight_warning",
                            binary=binary, launcher=launcher, launcher_prefix=lp,
                            recorded_mpi_prefixes=recorded,
                            cmd_preview=segment[:200],
                            hint=("MPI launcher 与编译档案不一致；运行应使用与编译"
                                  "同一套 MPI 安装的 mpirun/mpiexec，否则易死锁/崩溃。"))
                    except Exception:
                        pass
                # ② 族判定（无档案 fallback）→ warning，不拦
                fam_bin = _family_from_soname(libmpi)
                version_result = _launcher_version_output(
                    state, launcher, base)
                if asyncio.iscoroutine(version_result) or asyncio.isfuture(version_result):
                    version_result = await version_result
                if not isinstance(version_result, str):
                    # 探针不可用（沙箱能力缺失/返回非文本）→ 跳过族判定，不拦。
                    # 与上方 _run_ldd 同一处理：本函数整体恒返回 None（判决拆除
                    # sb:2093/2109/2121），探针自身失效不得变成一堵新墙。
                    try:
                        state.append_transcript(
                            "runtime_preflight_warning",
                            binary=binary, launcher=launcher,
                            cmd_preview=segment[:200],
                            hint="MPI launcher 版本探针不可用，已跳过族一致性判定。")
                    except Exception:
                        pass
                    continue
                fam_launcher = (
                    _family_from_version(version_result)
                    if version_result else None
                )
                if fam_bin and fam_launcher and fam_bin != fam_launcher:
                    try:
                        state.append_transcript(
                            "runtime_preflight_warning",
                            binary=binary, libmpi=libmpi, binary_family=fam_bin,
                            launcher=launcher, launcher_family=fam_launcher,
                            cmd_preview=segment[:200],
                            hint=("二进制与 launcher 的 MPI 实现不同族，混载大概率"
                                  "死锁/崩溃；可用与二进制同族同安装的 mpirun 启动"
                                  "（ldd | grep libmpi 定位其 bin/）。"))
                    except Exception:
                        pass
                # ③ 同族异前缀 → warning，不拦
                if lp is not None and _install_prefix(libmpi) != lp:
                    try:
                        state.append_transcript(
                            "runtime_preflight_warning",
                            binary=binary, libmpi=libmpi, launcher=launcher,
                            note="libmpi 与 launcher 安装前缀不同（同族，放行）")
                    except Exception:
                        pass
        if libmpi or launcher_tok:   # 只有涉 MPI 的通过才值得留痕，控噪
            try:
                state.append_transcript(
                    "runtime_preflight_passed", binary=binary,
                    libmpi=libmpi, launcher=launcher_tok)
            except Exception:
                pass
    return None


def _analyze_shell_path_effects(
    cmd: str, cwd: str | None = None, *, remote: bool = False,
) -> list[tuple[str, str]]:
    """用既有 tree-sitter AST 投影 shell 可见写操作，不授予路径权限。

    AST 层负责 pipeline/subshell/function/shell-c 和 cwd 作用域；本层只保留
    各命令 CLI 的写目标语义并复用 path_roles 分类。任何动态 cwd、运行期
    operand 或分析不确定性都显式落入 UNRESOLVED。
    """
    base = _norm_path(cwd) if cwd else os.getcwd()
    external_bindings = (
        {name: (REMOTE_SCRATCH,) for name in _SCHEDULER_SCRATCH_VARS}
        if remote else None
    )
    analysis = _te.analyze_bash(
        cmd or "",
        initial_cwd=base,
        external_bindings=external_bindings,
    )
    effects: list[tuple[str, str]] = []
    dynamic_operand = "*/__hf_dynamic__"
    remote_projection_root = "/__hf_remote_scratch__"
    unknown_projection_root = "/__hf_unknown_cwd__"

    def normalize_projected(
        target: str, projection_root: str, marker: str,
    ) -> str:
        if target in {UNRESOLVED, REMOTE_SCRATCH}:
            return target
        try:
            inside_projection = (
                os.path.commonpath([target, projection_root])
                == projection_root
            )
        except ValueError:
            inside_projection = False
        return marker if inside_projection else target

    def project_command(event: Any, event_cwd: str) -> tuple[str, list[str]]:
        if not event.head:
            return "shell", []
        args = [
            dynamic_operand if item == "__hf_dynamic__" else item
            for item in event.args
        ]
        if event.runtime_args:
            args.append(dynamic_operand)
        segment = shlex.join([event.head, *args])
        if event_cwd == REMOTE_SCRATCH:
            projection_root = remote_projection_root
            marker = REMOTE_SCRATCH
        elif event_cwd == UNRESOLVED:
            projection_root = unknown_projection_root
            marker = UNRESOLVED
        else:
            projection_root = event_cwd
            marker = ""
        op, targets, _restore_allowed = _extract_write_targets(
            segment,
            projection_root,
            opaque=False,
        )
        if remote:
            event_tokens = [str(event.head), *args]
            if not _route_event_is_known(event, str(event.head)):
                targets.extend(_generic_cli_output_targets(
                    event_tokens, projection_root))
            targets.extend(_inline_python_write_targets(
                event_tokens, projection_root))
        if event.dynamic_args and event.head in _MUTATING_BUILD_HEADS:
            targets.append(UNRESOLVED)
        if marker:
            targets = [
                normalize_projected(target, projection_root, marker)
                for target in targets
            ]
        return op or event.head or "write", targets

    def project_remote_visible(event: Any, event_cwd: str) -> list[str]:
        if not remote or not event.head:
            return []
        args = [
            dynamic_operand if item == "__hf_dynamic__" else item
            for item in event.args
        ]
        if event.runtime_args:
            args.append(dynamic_operand)
        if event_cwd == REMOTE_SCRATCH:
            projection_root = remote_projection_root
            marker = REMOTE_SCRATCH
        elif event_cwd == UNRESOLVED:
            projection_root = unknown_projection_root
            marker = UNRESOLVED
        else:
            projection_root = event_cwd
            marker = ""
        targets = _remote_visible_operand_targets(
            [str(event.head), *args], projection_root,
            head_path=event.head_path,
        )
        if marker:
            targets = [
                normalize_projected(target, projection_root, marker)
                for target in targets
            ]
        return targets


    def project_redirect(event: Any, event_cwd: str) -> list[str]:
        if event.target_unknown:
            return [UNRESOLVED]
        targets: list[str] = []
        for value in event.target_values:
            scratch_target = _scheduler_scratch_target(value)
            if scratch_target is not None:
                targets.append(scratch_target)
                continue
            if event_cwd == REMOTE_SCRATCH:
                projected = _containment_target(value, remote_projection_root)
                targets.append(normalize_projected(
                    projected, remote_projection_root, REMOTE_SCRATCH))
            elif event_cwd == UNRESOLVED:
                projected = _containment_target(value, unknown_projection_root)
                targets.append(normalize_projected(
                    projected, unknown_projection_root, UNRESOLVED))
            else:
                targets.append(_containment_target(value, event_cwd))
        return targets

    for event in analysis.static_path_events:
        event_cwds = list(event.cwd.values)
        if event.cwd.unknown:
            event_cwds.append(UNRESOLVED)
        if not event_cwds:
            event_cwds = [UNRESOLVED]
        for event_cwd in event_cwds:
            if event.kind == "redirect":
                op = "redirect"
                targets = project_redirect(event, event_cwd)
                visible_targets: list[str] = []
            else:
                op, targets = project_command(event, event_cwd)
                visible_targets = project_remote_visible(event, event_cwd)
                if (
                    remote
                    and event.head
                    and event.head_path is None
                    and event.dispatch_role != "transparent"
                    and event.head not in _REMOTE_NO_PATH_LOOKUP_HEADS
                ):
                    # 裸命令名（远端 PATH 解析）：不参与拒绝，只产出留痕事实。
                    effects.append(("remote_path_lookup", str(event.head)))
            for target in targets:
                if target not in {UNRESOLVED, REMOTE_SCRATCH}:
                    if _ignore_write_target(target):
                        continue
                effects.append((op, target))
            effects.extend(
                ("remote_visible_path", target)
                for target in visible_targets
            )

    if (
        analysis.analyzer_unavailable is not None
        or analysis.parse_error
        or analysis.dynamic_execution
        or analysis.path_unverifiable
    ):
        effects.append(("shell", UNRESOLVED))

    return list(dict.fromkeys(effects))


def _bash_path_effects_guard(
    state: Any, cmd: str, cwd: str | None = None, *,
    remote: bool = False, allow_authorization: bool = True,
) -> dict | None:
    """把 shell 可见写目标投影到既有 path-role 真相源。

    本地交互命令可沿用精确一次性环境授权；外部作业传入
    ``allow_authorization=False``，任何非安全目标都在提交前硬拒。
    """
    contract = validate_path_roles(state)
    if not contract["valid"]:
        return _scope_guard_error(
            cmd, "invalid_path_role_contract", "; ".join(contract["errors"]),
            "<path_roles>", state=state)
    from shared.lib import dangerous_commands as danger
    remote_lookup_heads: list[str] = []
    for op, target in _analyze_shell_path_effects(
            cmd, cwd, remote=remote):
        if op == "remote_path_lookup":
            # 裸命令名靠远端 PATH：放行 + 留痕，绝不参与拒绝——提交机上
            # 无法证明远端 PATH，硬拒只会逼出更差的绝对路径写死。
            remote_lookup_heads.append(str(target))
            continue
        if op == "remote_visible_path":
            scope, reason = _classify_remote_visible_path(target, state)
        else:
            scope, reason = _classify_write_path(
                target, state, remote=remote
            )
        if scope == "safe":
            vc_error = _worktree_requires_version_control(target, state)
            if vc_error:
                return _scope_guard_error(
                    cmd, "unversioned_source_worktree", vc_error, target,
                    state=state, op=op)
            if (allow_authorization and any(
                    role.role == "source_worktree_root"
                    for role in matching_path_roles(target, state))):
                confirmation = _source_write_confirmation(
                    state, kind="bash", text=cmd, cwd=cwd, target=target,
                    op=op, preview=cmd)
                if confirmation is not None:
                    return confirmation
            continue
        if allow_authorization:
            approved, approval = _scope_guard_authorization(
                state, kind="bash", cmd=cmd, cwd=cwd, target=target,
                scope=scope, reason=reason, op=op, preview=cmd)
            if approved:
                continue
            if approval is not None:
                return approval
        block = _scope_guard_error(
            cmd, scope, reason, target, state=state, op=op, preview=cmd)
        if (allow_authorization and danger.bypass_enabled()
                and _scope_bypass_allowed(scope)):
            try:
                state.append_transcript(
                    "scope_guard_bash_bypassed", target=target, scope=scope,
                    reason=reason, op=op, cmd_preview=cmd[:200])
            except Exception:
                pass
            continue
        return block
    if remote_lookup_heads:
        try:
            state.append_transcript(
                "remote_path_lookup",
                heads=sorted(dict.fromkeys(remote_lookup_heads)),
                cmd_preview=cmd[:200])
        except Exception:
            pass
    return None


def _path_role_contract_block(state: Any) -> dict | None:
    """Reject an incoherent role contract without interpreting shell text."""
    contract = validate_path_roles(state)
    if not contract["valid"] and not state.hook_state.get(
            "_path_role_contract_reported"):
        state.hook_state["_path_role_contract_reported"] = True
        try:
            state.append_transcript(
                "path_role_contract_invalid",
                errors=contract["errors"],
                conflict_paths=contract.get("conflict_paths"))
        except Exception:
            pass
    if not contract["valid"]:
        return _scope_guard_error(
            "safe_run_bash", "invalid_path_role_contract",
            "; ".join(contract["errors"]), "<path_roles>", state=state,
            kind="bash")
    return None


def _scope_guard_bash(state: Any, cmd: str, cwd: str | None = None,
                      *, remote: bool = False) -> dict | None:
    """Validate the declared path-role contract without interpreting Bash text."""
    del cmd, cwd, remote
    return _path_role_contract_block(state)


def _source_write_confirmation(
        state: Any, *, kind: str, text: str, cwd: str | None,
        target: str, op: str | None, preview: str | None = None,
) -> dict | None:
    """Ask once before mutating an approved source worktree.

    This is deliberately a tool gate, not a prompt instruction.  A high-risk
    source edit reuses that command's high-risk approval key, so it still
    pauses only once; ordinary source edits receive a target-bound one-shot
    authorization.
    """
    from shared.lib import dangerous_commands as danger
    from .execution_guard import classify_high_risk

    if danger.bypass_enabled():
        return None
    highrisk = classify_high_risk(text, mode="python" if kind == "python" else "shell")
    approval_text = (
        text if highrisk else _scope_authorization_text(
            kind=kind, cmd=text, cwd=cwd, target=target,
            scope="source_worktree_modification", op=op))
    if danger.is_confirmed(state, approval_text):
        if not highrisk:
            danger.consume_confirmation(state, approval_text)
        try:
            state.append_transcript(
                "source_worktree_write_confirmed", target=target, op=op,
                authorization_key=approval_text, highrisk_category=highrisk)
        except Exception:
            pass
        return None
    return _scope_guard_error(
        text, "source_worktree_modification",
        "source changes require an auditable patch and one-time approval", target,
        state=state, kind=kind, op=op, preview=preview,
        approval_required=True, approval_text=approval_text)


# ─────────────────────────────────────────────────────────────────────────────
# build gate（第3步）：首次主要 build 前的客观侦察 gate（事前拦，工具层）
# 阻断的是"缺客观前置证据"，不是"存在 warning"。分级：缺 profile/recon/declared_route
# → 阻断（自动生成侦察 + 缓存）；warning/needs_review → 不自动拦；缺则要求确认。
# ─────────────────────────────────────────────────────────────────────────────
# 主要 build（真实构建，区别于探查）
#
# 判定必须落在**命令位**，不能全串搜关键词：2026-07 实测 run 1785469523-90f86c
# 里 `which git; which make; which g++; g++ --version` 被判成"正在编译"并要求先
# 声明 build_root。旧正则用 (?<![\w.\-])make(?![\w.\-]) 全串搜，于是 `man make`、
# `grep make f`、`echo "make sure"` 一律误判。现在按 shell 段 + 管道 stage 拆开，
# 只看每段第一个可执行名（剥掉 FOO=bar 前缀与 sudo/time/nice 等包装器）。
_BUILD_TOOL_RE = re.compile(
    r"^(?:make|gmake|ninja|compile|case\.build|buildlib)$", re.I)
# 编译器全集。-c（只编译）和 -o（链接）用同一张表：旧正则两条 alternative 的
# 编译器清单不一致（-o 那条漏了 nvcc/nvc/nvc++/mpicc/mpicxx），导致
# `nvcc -arch=sm_90 -o app a.cu` 这类 GPU 链接命令完全不触发 build gate。
# 另一个旧坑：旧正则写 `g\+\+\b`，而 `+` 是非词字符，其后跟空格时 \b 永不成立，
# 所以 g++ / nvc++ 的任何编译命令历史上都没被 gate 认出来过。
_COMPILER_RE = re.compile(
    r"^(?:nvfortran|gfortran|gcc|g\+\+|nvc\+\+|nvc|nvcc|ifx|ifort|icx|icc|icpx|icpc|"
    r"mpif90|mpifort|mpicc|mpicxx|mpic\+\+|mpiicc|mpiicx|mpiicpc|mpiicpx|"
    r"mpiifort|mpiifx|clang|clang\+\+|hipcc)$", re.I)
# 包装器：本身不是 build，但后面跟的才是真正的命令
_CMD_WRAPPERS = frozenset({
    "sudo", "env", "time", "nice", "ionice", "taskset", "nohup", "stdbuf",
    "setarch", "chrt", "unbuffer",
})
# `bash -c "make -j8"` 这类内嵌命令要递归展开，否则成了绕过 build gate 的后门
_SHELL_EXECUTABLES = frozenset({"bash", "sh", "zsh", "dash", "ksh"})

def _command_stages(cmd: str) -> list[str]:
    """把命令拆成"每个可独立执行的 stage"：先按 ; && || 换行分段，再按管道拆。"""
    stages: list[str] = []
    for segment in _split_shell_segments(cmd):
        for stage in segment.split("|"):
            stage = stage.strip()
            if stage:
                stages.append(stage)
    return stages


def _stage_head(stage: str) -> tuple[str, list[str]]:
    """返回 (处于命令位的可执行名, 其余 argv)。剥离环境赋值与包装器前缀。

    可执行名取 basename，所以 /usr/bin/make、./case.build 都能识别。
    解析不了（引号不配对等）就退回空串，调用方按"不是 build"处理 —— 宁可漏判
    也不要把 `echo "make"` 判成编译。
    """
    try:
        tokens = shlex.split(stage, comments=False)
    except ValueError:
        tokens = stage.split()
    while tokens:
        head = tokens[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):     # FOO=bar make
            tokens = tokens[1:]
            continue
        name = os.path.basename(head)
        if name in _CMD_WRAPPERS:                            # sudo/time/nice ...
            tokens = tokens[1:]
            while tokens and (tokens[0].startswith("-")
                              or re.match(
                                  r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])
                              # 包装器 flag 的独立取值，如 `nice -n 10 make`
                              or re.fullmatch(r"[0-9]+", tokens[0])):
                tokens = tokens[1:]
            continue
        return name, tokens[1:]
    return "", []


def _stage_route_head(stage: str) -> tuple[str, list[str]]:
    """与 ``_stage_head`` 同步剥包装器，但保留入口路径语义。"""
    try:
        tokens = shlex.split(stage, comments=False)
    except ValueError:
        tokens = stage.split()
    while tokens:
        head = tokens[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            tokens = tokens[1:]
            continue
        name = os.path.basename(head)
        if name in _CMD_WRAPPERS:
            tokens = tokens[1:]
            while tokens and (
                tokens[0].startswith("-")
                or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])
                or re.fullmatch(r"[0-9]+", tokens[0])
            ):
                tokens = tokens[1:]
            continue
        return head.replace("\\", "/"), tokens[1:]
    return "", []
_GATE_ATTEMPT_KEY = "_build_gate_attempts"
_GATE_FP_KEY = "_build_gate_recon_fp"
_GATE_MAX_ATTEMPTS = 4


# ── 第6步 benchmark A/B/C 能力开关（单一环境变量 EXPERIMENT_BENCH_GROUP）──
# 未设/非法 → "C"（=正常完整行为，日常零影响）。高危拦截不在此表，三组恒开（安全基线）。
# 此机制为 benchmark 基础设施，不改任何能力本身的逻辑。hooks 侧用 bench_enabled 守卫。
_BENCH_CAPS: dict[str, set] = {
    "full_log":         {"C", "D"},        # _exec_and_log 自执行落盘 + focus_error
    "recon":            {"B", "C", "D"},   # platform_profile + source_recon 自动生成
    "build_gate":       {"C", "D"},        # 事前硬阻断
    "provision_first":  {"C", "D"},        # build_env + build_graph provision path
    "repeated_error":   {"C", "D"},        # fault fingerprint / repeated_error hook
    "strategic_review": {"C", "D"},        # route conflict + 命名里程碑/无进展复盘 hook
    "repro_snapshot":   {"B", "C", "D"},   # run 末环境快照 hook
}


def _bench_group() -> str:
    g = (os.getenv("EXPERIMENT_BENCH_GROUP") or "").strip().upper()
    return g if g in ("A", "B", "C", "D") else "C"


def bench_enabled(cap: str) -> bool:
    """该能力在当前 benchmark 组是否启用。未知能力默认全开（安全）。"""
    return _bench_group() in _BENCH_CAPS.get(cap, {"A", "B", "C", "D"})


def _is_major_build(cmd: str, _depth: int = 0) -> bool:
    """是否首次主要 build（排除 version/help/-n/dry-run 等探查）。

    只认命令位上的 build 工具/编译器 —— `which make`、`man make`、
    `grep make f`、`echo "make sure"` 里的 make 是参数，不是构建。
    """
    if not cmd or not cmd.strip():
        return False
    for stage in _command_stages(cmd):
        head, args = _stage_head(stage)
        if not head:
            continue
        # `bash -c "make -j8"`：展开内嵌命令，别让它绕过 gate
        if head in _SHELL_EXECUTABLES and _depth < 3:
            for i, tok in enumerate(args):
                if tok == "-c" and i + 1 < len(args):
                    if _is_major_build(args[i + 1], _depth + 1):
                        return True
            continue
        if _BUILD_TOOL_RE.match(head):
            if _is_build_probe([head, *args]):
                continue
            return True
        if head.lower() == "cmake" and "--build" in args:
            return True
        if _COMPILER_RE.match(head):
            if _is_exact_capability_probe(
                args,
                flags=_COMPILER_CAPABILITY_PROBE_FLAGS,
                flag_prefixes=_COMPILER_CAPABILITY_PROBE_PREFIXES,
            ):
                continue
            if "-c" in args or "-o" in args:
                return True
    return False


_MAJOR_RUN_RE = re.compile(r"\b(?:mpirun|mpiexec|srun)\b", re.IGNORECASE)


_MAJOR_RUN_HEADS = frozenset({"mpirun", "mpiexec", "srun"})

# Asking a launcher who it is does not launch anything.  `mpirun --version`
# inside an environment probe used to match _MAJOR_RUN_RE as raw text and get
# the whole probe rejected for "MPI/HPC execution cwd must be inside a declared
# run_root" — with the default cwd, that rejection is unavoidable, so a
# read-only capability probe became a dead end (observed: CESM install run
# 1787043322-1ab164 turn 3).
_LAUNCHER_PROBE_FLAGS = frozenset({
    "--version", "-version", "-V", "--help", "-h", "-?", "--usage",
})

_REDIRECTION_RE = re.compile(r"^[0-9]*(?:>>?|<|&>)")


def _drop_redirections(args: list[str]) -> list[str]:
    """Remove redirection tokens so flag-only checks see the real argv."""
    out: list[str] = []
    skip_target = False
    for arg in args:
        if skip_target:
            skip_target = False
            continue
        if _REDIRECTION_RE.match(arg):
            # `2>&1` carries its target; a bare `>` takes the next token.
            skip_target = bool(re.fullmatch(r"[0-9]*(?:>>?|<|&>)", arg))
            continue
        out.append(arg)
    return out


# A stage headed by one of these can *run* a launcher it merely mentions:
# `bash -c "mpirun …"`, `timeout 600 srun …`, `xargs -I{} mpirun …`.  Command
# position alone cannot see inside them, so a launcher named anywhere in such a
# stage keeps the conservative reading.  `which`/`echo`/`grep` are deliberately
# absent: naming a launcher is all they ever do.
_LAUNCHER_HIDING_HEADS = frozenset({
    "bash", "sh", "zsh", "dash", "ksh", "eval", "exec", "source", ".",
    "timeout", "flock", "setsid", "script", "xargs", "parallel", "ssh",
})
# NOTE: the launchers themselves are deliberately absent — they are handled by
# the command-position branch below, which is what applies the probe exclusion.  Listing
# `srun` here would short-circuit that and make `srun --help` a major run.


def _is_major_run(cmd: str) -> bool:
    """Does this command actually *launch* an MPI/HPC job?

    Command position decides, not raw text: `which mpirun` and a quoted
    `echo "srun ..."` name a launcher without running one.  A stage we cannot
    parse stays a major run — an unreadable command is the one case where
    guessing "harmless" is the expensive mistake.
    """
    if not _MAJOR_RUN_RE.search(cmd or ""):
        return False
    for stage in _command_stages(cmd or ""):
        head, args = _stage_head(stage)
        if not head:
            return _MAJOR_RUN_RE.search(stage) is not None
        if head in _LAUNCHER_HIDING_HEADS and _MAJOR_RUN_RE.search(stage):
            return True
        if head not in _MAJOR_RUN_HEADS:
            continue
        args = _drop_redirections(args)
        if _is_exact_capability_probe(
            args, flags=_LAUNCHER_PROBE_FLAGS,
        ):
            continue
        return True
    return False


def _explicit_build_output_dirs(cmd: str, base: str) -> list[str]:
    """Return explicit build-output directories for common generic tools.

    CWD alone is not a reliable build location: ``cmake -S SRC -B BUILD`` and
    ``make -C BUILD`` are normal out-of-source forms.  These selectors are
    interpreted only for tools whose CLI assigns them that meaning; unknown
    tools remain governed by the declared contract and the source-CWD guard.
    """
    result: list[str] = []
    for segment in _split_shell_segments(cmd):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if not tokens:
            continue
        command = Path(tokens[0]).name
        options = tokens[1:]
        if command == "cmake":
            selectors = {"-B", "--build"}
        elif command in {"make", "gmake", "ninja"}:
            selectors = {"-C", "--directory"}
        else:
            continue
        index = 0
        while index < len(options):
            token = options[index]
            value = None
            if token in selectors and index + 1 < len(options):
                value = options[index + 1]
                index += 1
            else:
                for selector in selectors:
                    if token.startswith(selector + "="):
                        value = token[len(selector) + 1:]
                        break
                    # cmake accepts -B<dir>; make/ninja accept -C<dir>.
                    if selector.startswith("-") and not selector.startswith("--") \
                            and token.startswith(selector) and len(token) > len(selector):
                        value = token[len(selector):]
                        break
            if value:
                resolved = _lexical_dir(value, base)
                if resolved:
                    result.append(resolved)
            index += 1
    return _dedupe_paths(result)


def _is_major_build_or_run(cmd: str) -> bool:
    return _is_major_build(cmd) or _is_major_run(cmd)


_PACKAGE_MANAGER_HEADS = frozenset({
    "pip", "pip3", "conda", "mamba", "micromamba", "spack",
    "poetry", "uv", "npm", "pnpm", "yarn", "gem", "cargo",
})
_PACKAGE_MUTATING_VERBS = frozenset({
    "install", "uninstall", "remove", "update", "upgrade", "create",
    "sync", "add", "build", "wheel", "concretize", "develop",
})
_SETUP_ENTRYPOINTS = frozenset({
    "configure", "autogen.sh", "bootstrap", "bootstrap.sh",
})
_PROBE_FLAGS = frozenset({
    "--help", "-h", "--version", "-v", "version", "help", "-n",
    "--dry-run",
})


def _first_non_option(args: list[str]) -> str:
    for item in args:
        if not item.startswith("-"):
            return item.lower()
    return ""


def _is_provision_or_configure_action(cmd: str, _depth: int = 0) -> bool:
    """识别应用无关的安装、解包和 configure 动作；能力探查不算。"""
    for stage in _command_stages(cmd or ""):
        head, args = _stage_head(stage)
        lowered = [item.lower() for item in args]
        if not head:
            continue
        if head in _SHELL_EXECUTABLES and _depth < 3:
            for index, item in enumerate(args[:-1]):
                if item == "-c" and _is_provision_or_configure_action(
                    args[index + 1], _depth + 1
                ):
                    return True
            continue
        if set(lowered).intersection(_PROBE_FLAGS):
            continue
        if head in _SETUP_ENTRYPOINTS:
            return True
        if head == "python" and len(lowered) >= 3 and lowered[:2] == ["-m", "pip"]:
            if _first_non_option(args[2:]) in _PACKAGE_MUTATING_VERBS:
                return True
        if head in _PACKAGE_MANAGER_HEADS:
            if _first_non_option(args) in _PACKAGE_MUTATING_VERBS:
                return True
        if head == "meson" and _first_non_option(args) in {"setup", "install", "compile"}:
            return True
        if head == "cmake" and "--build" not in lowered and any(
            item in lowered for item in {"-s", "-b", "--install", "--preset"}
        ):
            return True
        if head == "tar" and lowered:
            mode = lowered[0]
            if mode == "--extract" or "x" in mode.lstrip("-")[:4]:
                return True
        if head in {"unzip", "7z"} and not set(lowered).intersection({"-l", "-t"}):
            return True
    return False


def _is_network_acquire_action(cmd: str) -> bool:
    """识别会取得/更新外部内容的通用命令，不把版本探查算作下载。"""
    for stage in _command_stages(cmd or ""):
        head, args = _stage_head(stage)
        lowered = [item.lower() for item in args]
        if not head or set(lowered).intersection(_PROBE_FLAGS):
            continue
        if head in {"curl", "wget", "scp", "rsync"}:
            return True
        if head == "git" and _first_non_option(args) in {
            "clone", "fetch", "pull", "submodule", "remote",
        }:
            return True
        if head in _PACKAGE_MANAGER_HEADS and _first_non_option(args) in {
            "download", "fetch", "install", "update", "upgrade", "sync", "add",
        }:
            return True
        if head == "python" and len(lowered) >= 3 and lowered[:2] == ["-m", "pip"]:
            if _first_non_option(args[2:]) in {"download", "install", "wheel"}:
                return True
    return False


def _activity_path_role_guard(
        state: Any, cmd: str, cwd: str | None = None,
        route_decision: dict[str, Any] | None = None, *,
        declared_build: bool = False,
) -> dict | None:
    """Require only the roles implied by the activity being attempted."""
    roles = collect_path_roles(state)
    if declared_build or _is_major_build(cmd):
        if not any(role.role == "build_root" for role in roles):
            return _scope_guard_error(
                cmd, "missing_path_role",
                "build_root is required before compilation", "<build_root>",
                state=state)
        base = _command_base_dir(_expand_cmd_vars(cmd), cwd)
        matches = matching_path_roles(base, state)
        protected_source_roles = {
            "source_baseline_root", "managed_source_root",
        }
        route_allows_worktree = bool(
            isinstance(route_decision, dict)
            and route_decision.get("decision") == "matched_ready_step"
            and route_decision.get("authoritative") is True
            and route_decision.get("declared_workdir_role") == "source_worktree_root"
            and route_decision.get("workdir_role_observed") is True
        )
        if any(role.role in protected_source_roles for role in matches):
            return _scope_guard_error(
                cmd, "protected_source_tree",
                "compilation must never run inside source_baseline_root or "
                "managed_source_root; use build_root, or a separately authorized "
                "source_worktree_root when the project requires in-tree build",
                base, state=state)
        if (any(role.role == "source_worktree_root" for role in matches)
                and not route_allows_worktree):
            return _scope_guard_error(
                cmd, "protected_source_tree",
                "in-tree compilation requires both a trusted source_worktree_root "
                "and a matching frozen route step selecting that role",
                base, state=state)
        # If a common build tool explicitly names an output directory, that
        # directory is authoritative even when the command itself runs from a
        # parent cwd.  It must be the declared build area, never source/unknown.
        explicit_output_dirs = _explicit_build_output_dirs(cmd, base)
        for output_dir in explicit_output_dirs:
            output_roles = matching_path_roles(output_dir, state)
            if any(role.role in protected_source_roles for role in output_roles):
                return _scope_guard_error(
                    cmd, "protected_source_tree",
                    "build output directory must not be inside a source tree; "
                    "choose the declared build_root",
                    output_dir, state=state)
            if any(role.role == "source_worktree_root" for role in output_roles):
                if route_allows_worktree:
                    continue
                return _scope_guard_error(
                    cmd, "protected_source_tree",
                    "in-tree build output requires a matching frozen route step "
                    "selecting source_worktree_root",
                    output_dir, state=state)
            if not any(role.role == "build_root" for role in output_roles):
                return _scope_guard_error(
                    cmd, "invalid_build_root",
                    "explicit build output directory must be inside a declared "
                    "build_root",
                    output_dir, state=state)

        # Wrappers and compilers often have no generic ``-B``/``-C`` selector.
        # In that case the process cwd is the only mechanically enforceable
        # build destination.  Merely having some build_root elsewhere must not
        # authorize a build in the current frame/source directory: that was the
        # escape hatch behind the recursive-make incident.
        if not explicit_output_dirs:
            in_build_root = any(role.role == "build_root" for role in matches)
            in_route_worktree = route_allows_worktree and any(
                role.role == "source_worktree_root" for role in matches)
            if not in_build_root and not in_route_worktree:
                return _scope_guard_error(
                    cmd, "invalid_build_root",
                    "build commands without an explicit output selector must "
                    "run inside the declared build_root; an in-tree build is "
                    "allowed only in a trusted source_worktree_root selected "
                    "by the matching frozen route step",
                    base, state=state)

    if _is_major_run(cmd or ""):
        base = _command_base_dir(_expand_cmd_vars(cmd), cwd)
        matches = matching_path_roles(base, state)
        if not any(role.role == "run_root" for role in matches):
            return _scope_guard_error(
                cmd, "missing_path_role",
                "MPI/HPC execution cwd must be inside a declared run_root",
                base, state=state)
    return None


def _clean_path_token(token: str) -> str:
    token = token.strip().strip("\"'")
    token = token.rstrip("),，。")
    return os.path.abspath(os.path.expandvars(os.path.expanduser(token)))


def _valid_dir(token: str) -> str | None:
    if not token:
        return None
    p = _clean_path_token(token)
    return p if os.path.isdir(p) else None


def _lexical_dir(token: str, base: str) -> str | None:
    """Resolve a ``cd`` operand lexically — existence is not required.

    ``_valid_dir`` returns None for a directory that does not exist *yet*, so
    ``mkdir -p X && cd X && rm -rf *`` fell back to the old cwd and every
    target was then resolved against the wrong base.  The directory is created
    by the very command being checked; refusing to name it is not caution, it
    is a wrong answer.
    """
    if not token:
        return None
    token = token.strip().strip("\"'").rstrip("),，。")
    if not token or token == "-" or _has_unresolved_var(token):
        return None
    if any(ch in token for ch in _GLOB_CHARS):
        return None
    return _norm_path(token, base)


def _is_harness_dir(path: str) -> bool:
    p = os.path.abspath(os.path.expanduser(path))
    return (
        p.endswith("/harness-framework")
        or "/node4-experiment/harness-framework" in p
    )


def _paths_same_tree(a: str, b: str) -> bool:
    """True if a/b are the same directory tree (repo root vs subdir both OK)."""
    try:
        pa = Path(a).expanduser().resolve()
        pb = Path(b).expanduser().resolve()
    except Exception:
        return False
    return pa == pb or pa in pb.parents or pb in pa.parents


def _canonical_source_candidates(state: Any) -> list[str]:
    """从唯一 path-role 权威读取源码根；明确角色可合法位于 harness 子目录。"""
    source_roles = {
        "source_baseline_root",
        "managed_source_root",
        "source_worktree_root",
    }
    try:
        roles = collect_path_roles(state)
    except Exception:
        return []
    paths = [
        role.path for role in roles
        if role.role in source_roles and _valid_dir(role.path)
    ]
    return _dedupe_paths(paths)


def _declared_source_candidates(state: Any) -> list[str]:
    """返回可信源码根；canonical path roles 优先于旧文本/artifact 猜测。"""
    canonical = _canonical_source_candidates(state)
    if canonical:
        return canonical

    # 只在没有 canonical 角色的旧 run 中保留兼容发现。这里仍禁止把整个
    # harness 工作目录误当目标软件源码；新路线不会再让 agent artifact
    # 覆盖框架路径授权。
    keys = {
        "source", "source_dir", "source_path", "src_dir", "srcroot", "src_root",
        "repo_dir", "repo_root", "code_dir", "code_root",
    }
    paths: list[str] = []
    try:
        inputs = state.hook_state.get("node_inputs") or {}
        paths.extend(_collect_path_values(inputs, keys))
    except Exception:
        pass
    paths.extend(_artifact_declared_paths(state, keys))
    out = []
    for path in _dedupe_paths(paths):
        valid = _valid_dir(path)
        if valid and not _is_harness_dir(valid):
            out.append(valid)
    return _dedupe_paths(out)


def _path_roles_for(state: Any, path: str) -> set[str]:
    try:
        return {role.role for role in matching_path_roles(path, state)}
    except Exception:
        return set()


def _infer_build_root(
    cmd: str,
    state: Any,
    cwd: str | None = None,
) -> str | None:
    base = _command_base_dir(cmd, cwd)
    candidates = _explicit_build_output_dirs(cmd, base)
    for candidate in reversed(candidates):
        if _path_roles_for(state, candidate).intersection({
            "build_root", "source_worktree_root",
        }):
            return candidate
    if _path_roles_for(state, base).intersection({
        "build_root", "source_worktree_root",
    }):
        return base
    return None


def _cmake_home_directory(
    build_root: str | None,
    canonical_sources: list[str],
) -> str | None:
    if not build_root:
        return None
    cache = Path(build_root) / "CMakeCache.txt"
    try:
        lines = cache.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    prefix = "CMAKE_HOME_DIRECTORY:INTERNAL="
    for line in lines:
        if not line.startswith(prefix):
            continue
        source = _valid_dir(line[len(prefix):])
        if not source:
            return None
        if canonical_sources and not any(
            _paths_same_tree(source, declared)
            for declared in canonical_sources
        ):
            return None
        return source
    return None


def _looks_like_case_or_build_dir(path: str) -> bool:
    p = Path(path)
    parts = {x.lower() for x in p.parts}
    if {"bld", "build", "cmake-bld", "run"} & parts:
        return True
    if (p / "env_case.xml").exists() or (p / "case.run").exists() or (p / "case.build").exists():
        return True
    return False


def _command_source_candidates(cmd: str, cwd: str | None = None) -> list[str]:
    """Infer source roots from high-confidence build-tool syntax.

    The role model is build-system independent.  This helper recognizes only
    unambiguous source operands from common tools; unknown syntax falls back to
    explicit path_roles instead of guessing from a directory name or cwd.
    """
    command = cmd or ""
    cd_re = re.compile(rf"(?:^|[\n;&|])\s*cd\s+({_PATH_TOKEN_RE})")

    def base_before(position: int) -> Path:
        base = Path(cwd).expanduser() if cwd else Path.cwd()
        for cd_match in cd_re.finditer(command[:position]):
            cd_path = Path(os.path.expandvars(os.path.expanduser(
                cd_match.group(1).strip().strip("\""))))
            base = cd_path if cd_path.is_absolute() else base / cd_path
        return base

    candidates: list[str] = []

    def add_directory(token: str, position: int, *, file_entry: bool = False) -> None:
        path = Path(os.path.expandvars(os.path.expanduser(token)))
        if not path.is_absolute():
            path = base_before(position) / path
        if file_entry:
            path = path.parent
        resolved = _valid_dir(str(path))
        if resolved:
            # 明确的构建工具 source operand 是高置信度事实；是否属于获授权
            # 源码角色由 _infer_source_path 结合 canonical path roles 再判。
            candidates.append(resolved)

    # Explicit, cross-tool source flags are the strongest signal.
    source_flag = re.compile(
        r"(?:--source(?:-dir|-root)?|--src(?:dir|-dir)?)(?:=|\s+)([^\s;&|]+)")
    for match in source_flag.finditer(command):
        add_directory(match.group(1).strip().strip("\""), match.start())

    # CMake configure: cmake [-D/-G/-B ...] <source>, or cmake -S <source>.
    cmake_re = re.compile(r"\bcmake\b\s+([^\n;&|]+)")
    for match in cmake_re.finditer(command):
        try:
            tokens = shlex.split(match.group(1), comments=True)
        except ValueError:
            continue
        if not tokens or tokens[0] in {"--build", "--install", "-E", "--preset"}:
            continue
        positional: list[str] = []
        skip_next = False
        for token in tokens:
            if skip_next:
                skip_next = False
                continue
            if token in {"-S", "--source"}:
                skip_next = True
                continue
            if token.startswith("-S") and token != "-S":
                add_directory(token[2:], match.start())
                continue
            if token.startswith("--source="):
                add_directory(token.split("=", 1)[1], match.start())
                continue
            if token in {"-B", "--build", "-D", "-G", "-A", "-T", "-C", "-U"}:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            positional.append(token)
        # Handle separated -S after parsing so it is never confused with -B.
        for index, token in enumerate(tokens[:-1]):
            if token in {"-S", "--source"}:
                add_directory(tokens[index + 1], match.start())
        if positional:
            add_directory(positional[0], match.start())

    # Meson setup: meson setup [options] <builddir> <sourcedir>.
    meson_re = re.compile(r"\bmeson\s+([^\n;&|]+)")
    for match in meson_re.finditer(command):
        try:
            tokens = shlex.split(match.group(1), comments=True)
        except ValueError:
            continue
        if not tokens or tokens[0] != "setup":
            continue
        positional = [t for t in tokens[1:] if not t.startswith("-")]
        if len(positional) >= 2:
            add_directory(positional[-1], match.start())

    # Autotools and Python package entrypoints explicitly name a source file.
    entry_re = re.compile(
        r"(?:^|[\n;&|])\s*(?:env\s+[^\n;&|]+\s+)?(?:sh|bash|python(?:\d+(?:\.\d+)?)?)?\s*"
        r"([^\s;&|]+/(?:configure|setup\.py|pyproject\.toml))\b")
    for match in entry_re.finditer(command):
        add_directory(match.group(1).strip().strip("\""), match.start(), file_entry=True)

    return _dedupe_paths(candidates)


def _infer_source_path(cmd: str, state: Any, cwd: str | None = None) -> str:
    """把源码根与构建根分开派生；歧义时返回空值而不是猜 build cwd。"""
    command_candidates: list[str] = []
    canonical_sources = _canonical_source_candidates(state)
    declared_sources = _declared_source_candidates(state)
    build_root = _infer_build_root(cmd, state, cwd=cwd)

    # configure 后的 CMakeCache 是 build→source 的领域权威映射，优先于
    # “最后一个声明源码”之类启发式。
    cmake_home = _cmake_home_directory(build_root, canonical_sources)
    if cmake_home:
        return cmake_home

    def source_for_build_path() -> str:
        if len(canonical_sources) == 1:
            return canonical_sources[0]
        if not canonical_sources and len(declared_sources) == 1:
            return declared_sources[0]
        return ""

    tool_sources = _command_source_candidates(cmd, cwd=cwd)
    for source in reversed(tool_sources):
        if canonical_sources and not any(
            _paths_same_tree(source, declared)
            for declared in canonical_sources
        ):
            continue
        if not canonical_sources and _is_harness_dir(source):
            continue
        return source

    # 多段命令里使用最后一个 cd 更接近实际工作目录；但 build_root 永远
    # 不能直接升级成 source_root。
    cd_re = re.compile(rf"(?:^|[\n;&|])\s*cd\s+({_PATH_TOKEN_RE})")
    command_candidates.extend(m.group(1) for m in cd_re.finditer(cmd or ""))
    flag_res = [
        re.compile(rf"\b(?:make|gmake|ninja)\s+(?:[^\n;&|]*\s)?-C\s+({_PATH_TOKEN_RE})"),
        re.compile(rf"\bcmake\b[^\n;&|]*\s-S\s+({_PATH_TOKEN_RE})"),
        re.compile(rf"\bcmake\s+--build\s+({_PATH_TOKEN_RE})"),
        re.compile(rf"\bgit\s+-C\s+({_PATH_TOKEN_RE})"),
    ]
    for regex in flag_res:
        command_candidates.extend(
            match.group(1) for match in regex.finditer(cmd or "")
        )

    for token in reversed(command_candidates):
        path = _valid_dir(str(token))
        if not path:
            continue
        roles = _path_roles_for(state, path)
        if roles.intersection({"build_root"}):
            return source_for_build_path()
        if roles.intersection({
            "source_baseline_root",
            "managed_source_root",
            "source_worktree_root",
        }):
            return path
        if not _is_harness_dir(path):
            return path

    if cwd:
        path = _valid_dir(str(cwd))
        if path:
            roles = _path_roles_for(state, path)
            if "build_root" in roles:
                return source_for_build_path()
            if roles.intersection({
                "source_baseline_root",
                "managed_source_root",
                "source_worktree_root",
            }):
                return path
            if not _is_harness_dir(path):
                return path

    # 旧 run 的显式 build_source_path 只在没有 canonical source role 时兼容。
    build_source_path = (
        state.hook_state.get("build_source_path")
        if hasattr(state, "hook_state") else None
    )
    if build_source_path and not canonical_sources:
        path = _valid_dir(str(build_source_path))
        if path and not _is_harness_dir(path):
            return path

    unique = _dedupe_paths(canonical_sources or declared_sources)
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1 or build_root:
        return ""

    cwd_now = os.getcwd()
    return "" if _is_harness_dir(cwd_now) else cwd_now


def _source_recon_paths(state: Any) -> list[str]:
    paths: list[str] = []
    try:
        artifacts = state.list_artifacts("source_recon")
    except Exception:
        return paths
    for a in artifacts:
        try:
            rec = state.read_artifact(a["id"]) or {}
        except Exception:
            continue
        meta = rec.get("metadata") or {}
        sp = meta.get("source_path")
        if not sp:
            try:
                content = _json.loads(rec.get("content") or "{}")
                sp = content.get("source_path")
            except Exception:
                sp = None
        if sp:
            paths.append(str(sp))
    return paths


def _has_matching_source_recon(state: Any, source_path: str) -> bool:
    if not source_path:
        return True  # 无法推断时不做硬校验；但也不会自动扫 harness cwd。
    return any(_paths_same_tree(source_path, sp) for sp in _source_recon_paths(state))


def _source_recon_status(state: Any, source_path: str) -> str:
    paths = _source_recon_paths(state)
    if not paths:
        return "missing"
    if not source_path:
        return "unknown_source"
    return "matched" if any(_paths_same_tree(source_path, sp) for sp in paths) else (
        "mismatch: existing=" + ", ".join(paths[-3:])
    )


def _recon_fingerprint(source_path: str, env_path: str | None = None) -> str:
    """缓存指纹：source commit + dirty + 关键工具链。变化才重新侦察。"""
    try:
        from .repro_snapshot import _q as _rs_q
        from .repro_snapshot import _run as _rs_run
    except ImportError:  # pragma: no cover - node runtime import style
        from tools.repro_snapshot import _q as _rs_q
        from tools.repro_snapshot import _run as _rs_run
    if env_path:
        old = os.environ.get("EXPERIMENT_ACTIVE_BUILD_ENV")
        os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] = str(env_path)
    else:
        old = None
    # source_path 由 _infer_source_path 从模型命令（cd / -C / -S）与声明的源码角色
    # 推导，是模型可影响的值；而 _rs_run 是 shell=True。不加引号插进去，路径里的
    # shell 元字符就是任意命令执行 —— 2026-09-08 实测：source_path 取
    # "/x; touch /tmp/PWNED; echo" 时该文件被创建。repro_snapshot 自己的 15 处
    # 路径插值一律走 _q()，这里是唯一漏掉的两行。
    commit = _rs_run(f"git -C {_rs_q(source_path)} rev-parse HEAD 2>/dev/null")
    dirty = ("D" if _rs_run(
        f"git -C {_rs_q(source_path)} status --porcelain 2>/dev/null") else "C")
    tc = _rs_run(
        "command -v gfortran nvfortran ifx icx icpx gcc cmake mpif90 mpifort "
        "mpiifort mpiifx mpiicc mpiicx mpiicpc mpiicpx 2>/dev/null"
    )
    if env_path:
        if old is None:
            os.environ.pop("EXPERIMENT_ACTIVE_BUILD_ENV", None)
        else:
            os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] = old
    try:
        from tools.env_provision import env_info
        env_sha = env_info(env_path)["sha256"] if env_path else "no-env"
    except Exception:
        env_sha = "unknown-env"
    return hashlib.sha256(f"{source_path}|{commit}|{dirty}|{tc}|{env_sha}".encode()).hexdigest()[:16]


def _content_obj(rec: dict[str, Any] | None) -> Any:
    if not rec:
        return {}
    content = rec.get("content")
    if isinstance(content, str):
        try:
            return _json.loads(content)
        except Exception:
            return content
    return content or {}


def _latest_build_graph(state: Any) -> dict[str, Any] | None:
    rec = _latest_artifact(state, "build_graph")
    obj = _content_obj(rec)
    return obj if isinstance(obj, dict) else None


def _merge_confirmed_edges(state: Any, graph: dict[str, Any]) -> dict[str, Any]:
    try:
        arts = state.list_artifacts("build_graph_confirmed_edge")
    except Exception:
        arts = []
    if not arts:
        return graph
    nodes = graph.setdefault("nodes", {})
    edges = graph.setdefault("edges", [])
    seen = {(e.get("from"), e.get("to"), e.get("kind")) for e in edges if isinstance(e, dict)}
    merged = 0
    for art in arts[-20:]:
        try:
            rec = state.read_artifact(art["id"]) or {}
        except Exception:
            continue
        obj = _content_obj(rec)
        edge_list = obj.get("edges") if isinstance(obj, dict) else None
        for edge in edge_list or []:
            if not isinstance(edge, dict) or not edge.get("from") or not edge.get("to"):
                continue
            key = (edge.get("from"), edge.get("to"), edge.get("kind"))
            if key in seen:
                continue
            seen.add(key)
            edge = dict(edge)
            edge["confidence"] = "confirmed"
            edges.append(edge)
            dep = str(edge["from"])
            dst = str(edge["to"])
            nodes.setdefault(dep, {"id": dep, "outputs": [], "deps": [], "source": "confirmed_failure"})
            nodes.setdefault(dst, {"id": dst, "outputs": [], "deps": [], "source": "confirmed_failure"})
            if dep not in nodes[dst].setdefault("deps", []):
                nodes[dst]["deps"].append(dep)
            merged += 1
    if merged:
        graph["status"] = "partial" if graph.get("status") == "unknown" else graph.get("status", "partial")
        graph.setdefault("diagnostics", []).append(f"merged {merged} confirmed dependency edge(s) from failure logs")
        graph["dag_id"] = hashlib.sha256(
            _json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
    return graph


def _autogen_build_graph(state: Any, source_path: str, build_root: str | None = None) -> dict[str, Any] | None:
    if not source_path:
        return None
    try:
        from tools.build_graph import extract_build_dag
        from tools.build_state import initialize_from_dag, load_state_from_hook, mark_stale_on_env_change, save_state_to_hook
        from tools.env_provision import ensure_env_script
    except Exception:
        return None
    env_path = None
    try:
        env_path = ensure_env_script(state).get("env_path")
    except Exception:
        env_path = None
    recon = _latest_artifact(state, "source_recon") or {}
    recon_content = _content_obj(recon)
    if not isinstance(recon_content, dict):
        recon_content = {}
    old_env = os.environ.get("EXPERIMENT_ACTIVE_BUILD_ENV")
    if env_path:
        os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] = str(env_path)
    try:
        graph = extract_build_dag(
            source_path, state=state, build_root=build_root, source_recon=recon_content)
    finally:
        if env_path:
            if old_env is None:
                os.environ.pop("EXPERIMENT_ACTIVE_BUILD_ENV", None)
            else:
                os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] = old_env
    graph = _merge_confirmed_edges(state, graph)
    try:
        state.save_artifact("build_graph", f"build_graph_{state.run_id}",
                            _json.dumps(graph, ensure_ascii=False, indent=2),
                            metadata={"source_path": source_path,
                                      "build_root": build_root,
                                      "status": graph.get("status"),
                                      "dag_id": graph.get("dag_id")})
    except Exception:
        pass
    try:
        prof = _latest_artifact(state, "platform_profile") or {}
        prof_content = _content_obj(prof)
        fp = prof_content.get("env_fingerprint") if isinstance(prof_content, dict) else None
        st = load_state_from_hook(state.hook_state)
        if st is None or st.get("dag_id") != graph.get("dag_id"):
            st = initialize_from_dag(graph, env_fingerprint=fp)
        else:
            st = mark_stale_on_env_change(st, fp)
        save_state_to_hook(state.hook_state, st)
    except Exception:
        pass
    return graph


def _build_graph_actionable(graph: dict[str, Any] | None) -> bool:
    """Only high-confidence, material build-system DAGs may hard-block builds."""
    if not isinstance(graph, dict):
        return False
    return graph.get("status") == "extracted" and bool(graph.get("actionable"))


def _autogen_recon(
    state: Any,
    source_path: str,
    build_root: str | None = None,
) -> str:
    """自动生成 platform_profile + source_recon；graph 保留独立 build_root。"""
    from datetime import datetime, timezone
    if not source_path:
        return "failed_no_source_path"
    env_meta: dict[str, Any] = {}
    env_path = None
    if bench_enabled("provision_first"):
        try:
            from tools.env_provision import ensure_env_script
            env_meta = ensure_env_script(state)
            env_path = env_meta.get("env_path")
        except Exception:
            env_meta = {}
            env_path = None
    fp = _recon_fingerprint(source_path, env_path=env_path)
    arts = {a.get("type") for a in state.list_artifacts()}
    if (state.hook_state.get(_GATE_FP_KEY) == fp
            and "platform_profile" in arts and _has_matching_source_recon(state, source_path)):
        return "cached"   # 指纹未变且 artifact 在 → 不重扫
    from tools.repro_snapshot import collect_platform_profile
    from tools.source_recon import scan_source
    prof = collect_platform_profile(env_path=env_path) if env_path else collect_platform_profile()
    recon = scan_source(source_path)
    meta = {"source_path": source_path, "fingerprint": fp,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git": recon.get("git", {}), "schema_version": "1.0",
            "env_path": env_path, "env": env_meta}
    state.save_artifact("platform_profile", f"platform_profile_{state.run_id}",
                        _json.dumps(prof, ensure_ascii=False, indent=2), metadata=meta)
    state.save_artifact("source_recon", f"source_recon_{state.run_id}_{fp}",
                        _json.dumps(recon, ensure_ascii=False, indent=2), metadata=meta)
    if bench_enabled("provision_first"):
        _autogen_build_graph(state, source_path, build_root=build_root)
    state.hook_state[_GATE_FP_KEY] = fp
    return "generated"


def _latest_artifact(state: Any, artifact_type: str) -> dict[str, Any] | None:
    try:
        artifacts = state.list_artifacts(artifact_type)
    except Exception:
        artifacts = []
    if not artifacts:
        return None
    art = artifacts[-1]
    try:
        return state.read_artifact(art["id"]) or art
    except Exception:
        return art


def _build_contract_gate(state: Any) -> dict[str, Any] | None:
    """Validate the fallback declared_route before allowing a major build.

    The harness contract says a fallback route must be valid.  Returning a
    structured block here keeps that rule mechanical while still allowing the
    normal build-graph path to proceed without a declared_route artifact.
    """
    try:
        from tools.build_contract import validate_contract
    except Exception as exc:
        # 判决拆除（sb:3112 删，2026-08-31）：校验器自己 import 不出不再拒绝构建
        # —— 与同文件「预检自身异常降级放行」惯例一致；留痕后放行。
        error = f"无法加载 declared_route 校验器（{type(exc).__name__}: {exc}）"
        try:
            state.append_transcript(
                "build_contract_validator_unavailable", errors=[error],
            )
        except Exception:
            pass
        return None

    route = _latest_artifact(state, "declared_route") or {}
    platform = _latest_artifact(state, "platform_profile") or {}
    recon = _latest_artifact(state, "source_recon") or {}

    def content_obj(rec: dict[str, Any]) -> Any:
        content = rec.get("content")
        if isinstance(content, str):
            try:
                return _json.loads(content)
            except Exception:
                return content
        return content or {}

    platform_content = content_obj(platform)
    recon_content = content_obj(recon)
    report = validate_contract(route.get("content") or "", platform_content, recon_content)
    if report.get("valid"):
        try:
            state.append_transcript("build_contract_valid",
                                    required_domains=report.get("required_domains"),
                                    warnings=report.get("warnings"))
        except Exception:
            pass
        return None

    # 判决拆除 O12（sb:3153 降格，2026-08-31）：contract 校验有真实探测价值，
    # 但校验失败不再拦构建 —— build 照跑，errors 如实进 transcript 记录
    # （build_contract_invalid），供收尾审计与 reviewer 消费。
    try:
        state.append_transcript("build_contract_invalid", valid=False,
                                errors=report.get("errors"),
                                warnings=report.get("warnings"),
                                required_domains=report.get("required_domains"),
                                build_proceeded=True)
    except Exception:
        pass
    return None


# configure-first 构建系统标记：DAG 需 configure / 生成构建系统文件之后才能物化。
_CONFIGURE_FIRST_MARKERS = (
    "CMakeLists.txt", "configure", "configure.ac", "configure.in",
    "meson.build", "Makefile.am", "autogen.sh", "bootstrap",
    "path_names", "list_paths",   # mkmf（MOM6/FMS 类）：先生成 Makefile 再 make
)


def _source_is_configure_first(source_path: str | None) -> bool:
    """源码是否为 configure-first 构建系统（DAG 在 configure 之前不可物化）。

    经验（2026-06-25 D 组 dryrun 实证）：CMake/autotools/mkmf 工程的 build 系统文件
    （CMakeCache/Makefile）要 configure 之后才生成，gate 若在此之前硬要 build_graph 或
    declared_route，弱模型会卡在反复写不过校验的 contract → exhausted → execute_python 绕过。
    """
    if not source_path:
        return False
    root = Path(os.path.expanduser(source_path))
    if not root.is_dir():
        return False
    return any((root / m).exists() for m in _CONFIGURE_FIRST_MARKERS)


def _build_target_from_cmd(cmd: str, st: dict | None) -> str | None:
    """把 build 命令映射到 DAG node id（best-effort）。None = 映射不到 → warn-only。"""
    if not cmd:
        return None
    m = re.search(r"--target(?:=|\s+)(\S+)", cmd)      # cmake --build . --target X
    if m:
        cand: str | None = m.group(1)
    else:
        cand = None
        # 多段命令取最后一个 make/ninja 段，更接近实际 build target。
        segments = [s.strip() for s in re.split(r"&&|\|\||;|\n", cmd) if s.strip()]
        option_needs_arg = {
            "-C", "-f", "--file", "--makefile", "-I", "--include-dir",
            "-o", "--old-file", "--assume-old", "-W", "--what-if", "--new-file",
            "--assume-new", "-l", "--load-average", "-j", "--jobs",
        }
        for segment in reversed(segments):
            mm = re.search(r"\b(?:make|gmake|ninja)\b(.*)", segment)
            if not mm:
                continue
            try:
                tokens = shlex.split(mm.group(1))
            except Exception:
                tokens = mm.group(1).split()
            skip_next = False
            for tok in tokens:
                if skip_next:
                    skip_next = False
                    continue
                if tok in {">", "1>", "2>", ">>", "1>>", "2>>", "|"} or re.match(r"^\d?>", tok):
                    break
                if tok in option_needs_arg:
                    skip_next = True
                    continue
                if tok.startswith("-C") and tok != "-C":  # make -Cbuild
                    continue
                if tok.startswith(("--directory=", "--file=", "--makefile=", "--include-dir=",
                                   "--old-file=", "--assume-old=", "--what-if=", "--new-file=",
                                   "--assume-new=", "--load-average=", "--jobs=")):
                    continue
                if tok.startswith("-") or "=" in tok or re.fullmatch(r"\d+", tok):
                    continue
                cand = tok                              # 第一个非 flag/赋值/目录参数 token
                break
            if cand:
                break
        # 裸 make（无 target）→ None：当前 warn-only（见 WORKLOG 局限①）
    if not cand:
        return None
    cand = cand.strip().strip("\"'")
    try:
        from tools.build_graph import _clean_node
        nid = _clean_node(cand)
    except Exception:
        nid = cand
    nodes = (st or {}).get("nodes") or {}
    if nid in nodes:
        return nid
    low = {k.lower(): k for k in nodes}                 # 缓解命名大小写不一（issue ③）
    return low.get(nid.lower())


def _build_gate(
    state: Any,
    cmd: str,
    cwd: str | None = None,
    *,
    route_decision: dict[str, Any] | None = None,
) -> dict | None:
    """首次主要 build 前的 gate。返回 None=放行；dict=拒绝（error）。"""
    if not _is_major_build(cmd):
        return None   # 非主要 build（探查/普通命令）不拦
    gate_on = bench_enabled("build_gate")
    recon_on = bench_enabled("recon")
    if not gate_on and not recon_on:
        return None   # benchmark A 组：无 recon、无 gate
    try:
        arts = {a.get("type") for a in state.list_artifacts()}
    except Exception:
        return None   # state 无 artifact 能力 → 不拦（降级）
    build_root = _infer_build_root(cmd, state, cwd=cwd)
    source_path = _infer_source_path(cmd, state, cwd=cwd)
    source_recon_ok = _has_matching_source_recon(state, source_path)
    if not source_path:
        try:
            state.append_transcript(
                "build_context_unresolved",
                build_root=build_root,
                cmd_preview=cmd[:200],
            )
        except Exception:
            pass

    if recon_on and not gate_on:
        # benchmark B 组：只生成 recon（缺则补），但【永不阻断】——解耦 recon 与 gate
        if not ({"platform_profile", "source_recon"} <= arts) or not source_recon_ok:
            status = _autogen_recon(
                state, source_path, build_root=build_root
            )
            try:
                state.append_transcript("build_gate_recon_only",
                                        source_path=source_path, recon_status=status)
            except Exception:
                pass
        return None
    # build_gate now means "ensure evidence exists and hard-stop only on objective hazards".
    # Missing/weak route evidence is advisory; otherwise weak models spend their budget
    # satisfying contracts instead of compiling.
    attempts = state.hook_state.get(_GATE_ATTEMPT_KEY, 0)
    if attempts >= _GATE_MAX_ATTEMPTS:
        # Legacy runs may carry an old counter. Do not keep burning turns on gate loops.
        try:
            state.append_transcript("build_gate_advisory", reason="legacy_attempt_counter_reset",
                                    attempts=attempts)
        except Exception:
            pass
        state.hook_state[_GATE_ATTEMPT_KEY] = 0
        attempts = 0

    # 缺 platform_profile / source_recon → 自动生成并放行本次 build。
    # 这是通用 HPC 跑通优先的关键：证据收集不能成为 configure/build 的第一道死锁。
    recon_status = _source_recon_status(state, source_path)
    if not ({"platform_profile", "source_recon"} <= arts) or not source_recon_ok:
        status = _autogen_recon(
            state, source_path, build_root=build_root
        )
        try:
            state.append_transcript("build_gate_advisory", reason="recon_autogenerated",
                                    source_path=source_path, existing_recon=recon_status,
                                    recon_status=status)
        except Exception:
            pass
        try:
            arts = {a.get("type") for a in state.list_artifacts()}
        except Exception:
            arts = set()

    graph = _latest_build_graph(state)
    if bench_enabled("provision_first") and source_path and (
            graph is None
            or graph.get("source_root") not in (None, source_path)
            or graph.get("build_root") != build_root
            or (graph.get("status") or "unknown") != "extracted"):
        # source/build 二元身份变化或 configure 后图由 unknown 变得可提取时重抽。
        # build_root 不得再作为 source_root 传入。
        graph = _autogen_build_graph(
            state, source_path, build_root=build_root
        )
    graph_status = (graph or {}).get("status")
    graph_actionable = _build_graph_actionable(graph)
    authoritative_v2_match = bool(
        isinstance(route_decision, dict)
        and route_decision.get("decision") == "matched_ready_step"
        and route_decision.get("authoritative") is True
        and route_decision.get("route_ref")
    )
    if graph and not graph_actionable:
        try:
            state.append_transcript("build_gate_advisory", reason="dag_not_hard_actionable",
                                    graph_status=graph_status,
                                    actionability_reason=(graph or {}).get("actionability_reason"))
        except Exception:
            pass

    # provision-first 依赖顺序强制：build target 前，其前置必须 verified（产物已落盘）。
    # 前置未就绪是「可被满足」的正常信号 → 不计入 _GATE_ATTEMPT_KEY（不触发 exhausted）。
    if (bench_enabled("provision_first") and graph_actionable
            and not authoritative_v2_match):
        try:
            try:
                from .build_state import (
                    check_prerequisites, load_state_from_hook,
                )
            except ImportError:
                from tools.build_state import (
                    check_prerequisites, load_state_from_hook,
                )
            _bs = load_state_from_hook(state.hook_state)
        except Exception:
            _bs = None
        if _bs:
            _tgt = _build_target_from_cmd(cmd, _bs)
            _pre = check_prerequisites(_bs, _tgt)
            if _tgt and not _pre.get("ok") and _pre.get("blocked_by"):
                # 判决拆除（sb:3341 删，2026-08-31）：预测失败闸 —— 同函数下方
                # 自认 DAG 模型不可靠（unknown/partial 降级放行），且实测后果是
                # 弱模型绕行 execute_python。留痕不拦，构建系统自己拒绝缺前置。
                #
                # 合并 2026-09-03：E-11 的跨 run 遗留判定（inherited）原本骑在
                # 这堵墙上。墙随判决拆除，但诊断价值无代价，整体并入 warning ——
                # observable/advisory 分流与 inherited 的逐文件 mtime 详单照记，
                # 只是不再 return error。判断"要不要重建"回到调用方手里。
                graph_nodes = (graph or {}).get("nodes") or {}
                state_nodes = _bs.get("nodes") or {}
                blocked_by = [str(item) for item in _pre["blocked_by"]]
                observable_blockers, advisory_blockers = [], []
                for dependency in blocked_by:
                    graph_node = (graph_nodes.get(dependency, {})
                                  if isinstance(graph_nodes, dict) else {})
                    state_node = (state_nodes.get(dependency, {})
                                  if isinstance(state_nodes, dict) else {})
                    outputs = (graph_node.get("outputs")
                               or state_node.get("outputs") or [])
                    (observable_blockers if outputs else advisory_blockers).append(dependency)
                inherited_blockers = [
                    dependency for dependency in observable_blockers
                    if (state_nodes.get(dependency) or {}).get("state") == "inherited"
                ]
                stale_detail: list[dict[str, Any]] = []
                for dependency in inherited_blockers:
                    dep_node = state_nodes.get(dependency) or {}
                    stale_detail.append({
                        "dependency": dependency,
                        "run_started_at": dep_node.get("inherited_run_started_at"),
                        "outputs": [
                            {"path": rec.get("path"), "mtime": rec.get("mtime")}
                            for rec in (dep_node.get("inherited_outputs") or [])
                        ],
                    })
                try:
                    state.append_transcript(
                        "build_gate_prereq_warning",
                        target=_tgt, blocked_by=blocked_by,
                        observable=observable_blockers,
                        advisory=advisory_blockers,
                        inherited=inherited_blockers,
                        # 跨 run 遗留产物逐文件 mtime：判定为 realpath 归一化后
                        # 严格比较 mtime < run_started_at，无容差。要复用就在任一
                        # 产物 metadata 写 reused_inputs，要刷新就重 build。
                        inherited_stale_outputs=stale_detail,
                    )
                except Exception:
                    pass

    # DAG 不可物化（unknown/partial）时不再硬拦 contract。recon 仍会自动采集，
    # 但不会阻断本次 configure/build。
    # configure-first 工程：configure 后的后续 build 会重抽到 DAG 并启用 prereq 强制；
    # hand-rolled 工程：降级放行（构建系统自身负责单次 build 内顺序，跨组件顺序待 DAG 可得）。
    # 经验（2026-06-25 D 组 dryrun 实证）：对弱模型硬要 build_contract → 反复写不过校验
    # → build_gate_exhausted → execute_python 绕过，护栏反成负担、且 provision 链全程不触发。
    if bench_enabled("provision_first") and not graph_actionable:
        reason = ("dag_pending_configure" if _source_is_configure_first(source_path)
                  else "dag_unavailable_degraded")
        try:
            state.append_transcript("build_gate_passed", reason=reason,
                                    source_path=source_path, graph_status=graph_status,
                                    actionability_reason=(graph or {}).get("actionability_reason"))
        except Exception:
            pass
        return None

    # provision-first：框架能从构建系统/编排线索提取 DAG 时，不再强制 agent 声明路线。
    # declared_route/build_contract 只在 DAG 不可用时作为 fallback。
    if "declared_route" not in arts:
        if graph_actionable:
            try:
                state.append_transcript("build_gate_provision_passed",
                                        source_path=source_path,
                                        graph_status=graph_status,
                                        dag_id=(graph or {}).get("dag_id"))
            except Exception:
                pass
        else:
            try:
                state.append_transcript("build_gate_advisory", reason="missing_actionable_dag_and_contract",
                                        graph_status=graph_status,
                                        actionability_reason=(graph or {}).get("actionability_reason"))
            except Exception:
                pass
            return None

    if "declared_route" in arts and not graph_actionable:
        # canonical resolver 已机械确认当前动作精确匹配本 run 的 frozen v2
        # 步骤；旧 build_contract 只理解 route_type/activities，不能再拿另一套
        # schema 对同一权威路线二次授权。侦察、DAG 刷新和依赖门均在上方照常
        # 执行；legacy/未匹配路线仍保留原 fallback 校验。
        if authoritative_v2_match:
            try:
                state.append_transcript(
                    "build_gate_v2_route_accepted",
                    route_step_id=route_decision.get("route_step_id"),
                    route_ref=route_decision.get("route_ref"),
                    graph_status=graph_status,
                )
            except Exception:
                pass
        else:
            contract_block = _build_contract_gate(state)
            if contract_block is not None:
                return contract_block

    # 前置证据齐全 → 放行（warning/needs_review 不在此硬拦；blocking 由 declared_route 内容承诺）
    try:
        state.append_transcript("build_gate_passed",
                                artifacts=["platform_profile", "source_recon",
                                           "build_graph" if graph_actionable else "advisory_route"],
                                source_path=source_path, source_recon=recon_status,
                                graph_status=graph_status,
                                graph_actionable=graph_actionable)
    except Exception:
        pass
    return None


def _coerce_timeout(t: Any, default: int) -> int:
    """把 timeout 强制成合法正整数；无法解析（LLM 输出损坏如 'yel30'/'六十'/'INS60'）→ 默认值。
    背景：模型解码损坏可能把 timeout 写成字符串，原 _run_bash
    的 `timeout <= 0` 触发 TypeError → communicate() 协程泄漏 + 命令误判失败，污染大量轮次。
    在工具层入口归一化，损坏 timeout 的命令照常执行（用默认超时），不再因参数损坏而失败。"""
    try:
        v = int(t)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


_READ_ONLY_COMMANDS = {
    "pwd", "ls", "cat", "head", "tail", "stat", "file", "wc", "du", "df",
    "grep", "rg", "which", "whereis", "realpath", "readlink", "sha256sum",
    "sha1sum", "md5sum", "nproc", "free", "uname", "lscpu", "env", "printenv",
    "test", "[", "true", "false",
    # 收敛任务书 K6：只加不会写文件、也不会执行命令的。sort / uniq / xxd 不加——判定只看
    # 命令名，而 sort -o、uniq 输入 输出、xxd -r 都会写文件。compgen 不加：-C/-F 会执行命令
    # 或调用函数（2026-09-14 第三会话复审 P1 实测 compgen -C 'touch pwned' 写出了文件）。
    # tar 与 pip 按参数形状另判。
    "od", "hexdump", "cut", "type",
}
_READ_ONLY_GIT_SUBCOMMANDS = {
    "status", "diff", "log", "show", "rev-parse", "describe",
}
# 已降级为文档/加速用途：capability probe 准入不再前置程序名名单，
# 而由入口信任谓词（可信系统根，或“本 run 影响不了它”）承担。
_STRICT_CAPABILITY_PROBE_PROGRAMS = frozenset({
    "make", "gmake", "ninja", "cmake",
    "gcc", "g++", "clang", "clang++", "gfortran", "nvfortran",
    "nvc", "nvc++", "nvcc", "ifx", "ifort", "icx", "icpx",
    "mpicc", "mpicxx", "mpif90", "mpifort",
    "curl", "wget",
})
_STRICT_CAPABILITY_PROBE_FLAGS = frozenset({
    "--version", "-version", "-V", "--help",
})


def _is_strict_capability_probe(program: str, args: list[str]) -> bool:
    """只接受不会加载项目规则/目标的单一 capability flag。

    程序名不再是准入前提（nc-config/h5pcc 等科学工具链探测无法穷举）；
    真正的门是调用方随后的入口信任检查。``make -n`` 和 ``make help``
    故意不在这里：Makefile 展开仍可执行 ``$(shell ...)``，不能凭
    dry-run/help 名称承诺无副作用。
    """
    del program
    return len(args) == 1 and _is_exact_capability_probe(
        args, flags=_STRICT_CAPABILITY_PROBE_FLAGS,
    )


_GIT_EXEC_CONFIG_EXACT = frozenset({
    "diff.external", "core.fsmonitor", "core.pager", "credential.helper",
})


def _git_exec_config_key(value: str) -> str:
    return str(value or "").split("=", 1)[0].strip().lower()


def _git_config_can_launch_process(value: str) -> bool:
    key = _git_exec_config_key(value)
    return (
        key in _GIT_EXEC_CONFIG_EXACT
        or key.startswith("pager.")
        or (
            key.startswith("diff.")
            and key.endswith((".command", ".textconv"))
        )
    )


def _git_embedded_launcher_option(args: list[str]) -> str | None:
    """返回会让“查询”Git 派生外部进程的 argv 证据。"""
    for index, token in enumerate(args):
        lowered = str(token).lower()
        if lowered in {
            "--ext-diff", "--textconv", "--exec-path", "--paginate",
            "--show-signature",
        } or lowered.startswith((
            "--exec-path=", "--config-env=", "--config-env",
        )):
            return str(token)
        config_value: str | None = None
        if token == "-c":
            if index + 1 >= len(args):
                return "-c"
            config_value = str(args[index + 1])
        elif token.startswith("-c") and token != "-c":
            config_value = str(token[2:])
        if (
            config_value is not None
            and _git_config_can_launch_process(config_value)
        ):
            return f"-c {config_value}"
    return None


def _rg_embedded_launcher_option(args: list[str]) -> str | None:
    for token in args:
        if token == "--pre" or token.startswith("--pre="):
            return str(token)
    return None


def _embedded_shell_launcher(
    analysis: Any,
) -> tuple[str, str] | None:
    """从 Bash AST 识别会在工具内部再次解释可执行入口的选项。"""
    for event in getattr(analysis, "static_path_events", ()):
        if getattr(event, "kind", "") != "command":
            continue
        program = os.path.basename(str(getattr(event, "head", "") or ""))
        args = [str(item) for item in getattr(event, "args", ())]
        if program == "git":
            evidence = _git_embedded_launcher_option(args)
        elif program == "rg":
            evidence = _rg_embedded_launcher_option(args)
        else:
            evidence = None
        if evidence is not None:
            return program, evidence
    return None



def _git_command_is_read_only(args: list[str]) -> bool:
    """保守识别 Git 查询；branch/remote/tag 必须验证 argv 语义。"""
    if args == ["--version"]:
        return True
    if _git_embedded_launcher_option(args) is not None:
        return False
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-C", "--git-dir", "--work-tree", "-c"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return False
    subcommand = args[index]
    tail = args[index + 1:]
    if any(token == "--output" or token.startswith("--output=") for token in tail):
        return False
    if subcommand in _READ_ONLY_GIT_SUBCOMMANDS:
        return True
    if subcommand == "submodule":
        return bool(tail) and tail[0] == "status"
    if subcommand == "remote":
        if not tail or tail == ["-v"]:
            return True
        return tail[0] == "get-url"
    if subcommand == "branch":
        mutating = {
            "-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete",
            "--move", "--copy", "--force", "--edit-description",
            "--set-upstream-to", "--unset-upstream", "--track", "--no-track",
            "--create-reflog",
        }
        if any(token.split("=", 1)[0] in mutating for token in tail):
            return False
        if not tail:
            return True
        list_mode = any(token in {"-a", "--all", "-r", "--remotes", "-l", "--list"}
                        for token in tail)
        has_positional = any(not token.startswith("-") for token in tail)
        return list_mode or not has_positional
    if subcommand == "tag":
        mutating = {
            "-d", "--delete", "-a", "--annotate", "-s", "--sign", "-u",
            "--local-user", "-f", "--force", "-m", "--message", "-F", "--file",
            "--create-reflog",
        }
        if any(token.split("=", 1)[0] in mutating for token in tail):
            return False
        if not tail:
            return True
        list_mode = any(token in {
            "-l", "--list", "--contains", "--no-contains", "--merged",
            "--no-merged", "--points-at",
        } for token in tail)
        return list_mode
    return False


def _mask_quoted_regions(cmd: str) -> str | None:
    """把引号/转义保护的字符替换为 ``x``，长度不变；引号不配对返回 None。

    只读结构判定必须只看**未被引号包裹**的 shell 元字符：``grep -i '2>&1'``
    里的重定向是字面量，``echo "a|b"`` 里的竖线不是管道。掩码保持长度，
    使掩码串上的 span 能原样切回原串。
    """
    out: list[str] = []
    quote = ""
    escaped = False
    for ch in cmd:
        if escaped:
            out.append("x")
            escaped = False
        elif quote == "'":
            out.append("'" if ch == "'" else "x")
            if ch == "'":
                quote = ""
        elif quote == '"':
            out.append('"' if ch == '"' else "x")
            if ch == '"':
                quote = ""
            elif ch == "\\":
                escaped = True
        elif ch == "\\":
            out.append("x")
            escaped = True
        elif ch in "'\"":
            out.append(ch)
            quote = ch
        else:
            out.append(ch)
    if quote or escaped:
        return None
    return "".join(out)


# 纯 stderr 合流/丢弃：不写任何文件，也不能把副作用藏进目标里。
# ``2>&1`` 把 stderr 并进当前 stdout（若 stdout 另有写重定向，那条 ``>``
# 会被下面的写重定向判据单独拒掉）；``2>/dev/null``/``2>&-`` 只是丢弃。
#
# 两侧边界都必须钉死，否则挖掉的会是一条真实写重定向的**中间片段**，剩下的
# 串里不再有 ``>``，写动作反被判成只读：
# - 左边界：``2`` 若粘在前一个词尾上（``y2>&1results.txt``），bash 读到的是
#   词 ``y2`` 加 ``>&1results.txt``——``>&`` 后跟非数字即把 stdout 与 stderr
#   一并重定向到**文件** ``1results.txt``（实测会创建该文件）。所以 ``2`` 前
#   只允许是串首或 shell 词分隔符。
# - 右边界：``\b`` 在任意非词字符前都成立，``2>/dev/null.log``、
#   ``2>>/dev/null-x`` 会被整段挖掉，而它们写的是 /dev 下的真实文件。所以
#   ``/dev/null`` 后只允许是串尾或 shell 词分隔符。
_BENIGN_STDERR_REDIRECT_RE = re.compile(
    r"(?<![^\s;|&])2>(?:&1|&-|>?\s*/dev/null)(?=[\s;|&]|$)")


def _read_only_pipeline_stages(segment: str) -> list[str] | None:
    """把一段命令拆成管道各段；含副作用重定向/替换时返回 None。

    只读探测的判据从"整条命令不得含任何重定向或管道"收敛为"不得含产生
    副作用的重定向、且管道各段都得自己是只读的"。放行的只有纯 stderr
    合流/丢弃与切段本身；写重定向（``>`` ``>>`` ``&>`` ``2>文件``）、输入
    重定向与 heredoc、``|&``、命令替换与进程替换一律仍判非只读——各段是否
    只读由调用方沿用既有逐段判定，任一段不是只读即整条不是只读。
    """
    masked = _mask_quoted_regions(segment)
    if masked is None:
        # 语法不完整时不能把命令提升为可信只读；后续 Bash AST 会细分错误。
        return None
    if "$(" in masked or "`" in masked or "<(" in masked or ">(" in masked:
        return None
    if "|&" in masked:
        return None
    kept_src: list[str] = []
    kept_masked: list[str] = []
    pos = 0
    for match in _BENIGN_STDERR_REDIRECT_RE.finditer(masked):
        kept_src.append(segment[pos:match.start()])
        kept_masked.append(masked[pos:match.start()])
        pos = match.end()
    kept_src.append(segment[pos:])
    kept_masked.append(masked[pos:])
    stripped_src = "".join(kept_src)
    stripped_masked = "".join(kept_masked)
    if "<" in stripped_masked or ">" in stripped_masked:
        return None
    # 到这里剩下的 ``&`` 只可能是后台运算符：``&&`` 已被 _split_shell_segments
    # 切走，``2>&1``/``2>&-`` 刚被挖掉，其余带 ``&`` 的重定向（``&>``、``>&``、
    # ``|&``）都已在上面被拒。而 _split_shell_segments 不把单个 ``&`` 当切分点，
    # 于是 ``ls 2>/dev/null & rm -rf /x`` 会整条落进一个以只读命令开头的段里。
    if "&" in stripped_masked:
        return None
    stages: list[str] = []
    start = 0
    for index, ch in enumerate(stripped_masked):
        if ch == "|":
            stages.append(stripped_src[start:index])
            start = index + 1
    stages.append(stripped_src[start:])
    return [stage.strip() for stage in stages if stage.strip()]


_TRUSTED_READ_ONLY_BUILTINS = frozenset({
    "pwd", "test", "[", "true", "false", "command", "type",
})
_TRUSTED_EXECUTABLE_ROOTS = tuple(
    Path(path).resolve(strict=False)
    for path in ("/bin", "/usr/bin", "/sbin", "/usr/sbin")
)
#: 历史遗留：曾经用"列出会改身份的变量"来挡。它追不完 —— LD_PRELOAD 有等效的
#: LD_AUDIT，`git -c` 有等效的 GIT_CONFIG_COUNT/KEY_n/VALUE_n，`ls --version`
#: 这种"能力探查形状"也保护不了自己（实测：ld.so 在程序打印版本号之前就已经去
#: 加载 LD_AUDIT 指向的对象）。判据因此反过来：**只放行可证明改变不了"被执行的
#: 是哪段代码"的变量**，名单外一律不判只读。
#:
#: 只保留给 `export NAME=...` 这条语句形式做兼容读取，见 _segment_has_identity_override。
_IDENTITY_ENV_NAMES = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV",
    "PYTHONPATH", "PERL5LIB", "RUBYLIB", "GIT_EXTERNAL_DIFF",
    "GIT_PAGER", "PAGER", "SHELL",
    # GNU tar 在列目录时也会执行 TAR_OPTIONS 里的 --checkpoint-action=exec（第三会话复审 P1）。
    "TAR_OPTIONS",
})

#: 白名单：命令位之前出现这些赋值时，命令仍可判只读。每一条都要能回答
#: "它为什么改变不了被执行的代码"。
_READ_ONLY_ENV_PREFIX_EXACT: dict[str, str] = {
    # 只选 locale 数据表（排序、大小写、消息翻译），不指定任何被加载或被执行的文件。
    "LANG": "locale 数据选择",
    "LANGUAGE": "消息翻译的语言优先级列表",
    # 只选时区数据；TZ 取 ":/path" 形式时也只是读 tzfile，不执行它。
    "TZ": "时区数据选择",
    # 只选 terminfo 条目名（能力描述表）。指向 terminfo 目录的是 TERMINFO，不在名单内。
    "TERM": "terminfo 条目名",
    # 纯排版/着色，程序读了只改输出字节，不改执行路径。
    "COLUMNS": "输出宽度",
    "LINES": "输出高度",
    "NO_COLOR": "关闭着色",
    "GREP_COLORS": "着色配置（只是颜色码，不含命令）",
}
#: 前缀匹配：LC_* 是 locale 分类（LC_ALL / LC_CTYPE / …），名单外的 LC_ 名字 glibc
#: 直接忽略；CLICOLOR / CLICOLOR_FORCE 同 NO_COLOR。
_READ_ONLY_ENV_PREFIX_FAMILIES: tuple[str, ...] = ("LC_", "CLICOLOR")
#: 只在取特定值时才无害的：PAGER 本身是"要执行哪个程序"，只有恒等分页器 cat 例外。
_READ_ONLY_ENV_PREFIX_VALUES: dict[str, frozenset[str]] = {
    "PAGER": frozenset({"cat"}),
}


def _read_only_env_prefix_allowed(name: str, value: str) -> bool:
    """这个 NAME=value 前缀能否出现在一条仍判只读的命令里。

    只读判定看得见 argv，看不见环境变量能把"执行哪段代码"换掉。所以判据是白名单：
    名单外一律不算只读（命令并不因此被拒，只是走正常的效果/路线判定）。
    """
    if name in _READ_ONLY_ENV_PREFIX_EXACT:
        return True
    if any(name.startswith(family) for family in _READ_ONLY_ENV_PREFIX_FAMILIES):
        return True
    allowed_values = _READ_ONLY_ENV_PREFIX_VALUES.get(name)
    return allowed_values is not None and value.strip() in allowed_values


def _segment_has_env_assignment(segment: str) -> bool:
    """命令位之前有 NAME=value 赋值前缀。按参数形状判只读的 tar/pip 一律不认带前缀的形状：
    形状只看得见 argv，看不见环境变量能改变的行为（TAR_OPTIONS、PIP_* 配置）。"""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return True
    return bool(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]) is not None
_MECHANICALLY_BOUNDED_SHELL_PROGRAMS = frozenset({
    ":", "echo", "printf", "mkdir", "touch", "cp", "mv", "ln",
    "chmod", "rm", "rmdir", "truncate", "sleep", "tee",
})


def _segment_has_identity_override(segment: str) -> bool:
    """命令位之前有改变得了"执行哪段代码"的环境赋值。

    白名单判据（047）：只有 _read_only_env_prefix_allowed 认可的赋值才算无害，
    其余一律算身份覆盖。反过来列（而不是列"坏变量"）是因为坏变量追不完：
    LD_PRELOAD 有 LD_AUDIT，`git -c` 有 GIT_CONFIG_COUNT/KEY_n/VALUE_n。
    名单外的新变量默认落到"算覆盖"那一侧，是加严不是放松。
    """
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return True
    if tokens and tokens[0] == "export":
        return any(
            "=" in token
            and not _read_only_env_prefix_allowed(*token.split("=", 1))
            for token in tokens[1:]
        )
    route_head, _ = _stage_route_head(segment)
    if not route_head:
        return True
    normalized_head = route_head.replace("\\", "/")
    for token in tokens:
        if token.replace("\\", "/") == normalized_head:
            break
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", token)
        if match and not _read_only_env_prefix_allowed(
                match.group(1), token.split("=", 1)[1]):
            return True
    return False


def _segment_uses_wrapper(segment: str) -> bool:
    """wrapper 不能获得只读快速路径，但其后有界写入仍由既有门处理。"""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return True
    for token in tokens:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return os.path.basename(token) in _CMD_WRAPPERS
    return False


def _resolve_route_entry_paths(segment: str) -> tuple[Path, Path] | None:
    """返回解开 wrapper 后入口的 (调用绝对路径, realpath)；解析不了返回 None。"""
    route_head, _ = _stage_route_head(segment)
    if not route_head:
        return None
    if "/" in route_head:
        if not os.path.isabs(route_head):
            return None
        candidate = route_head
    else:
        candidate = shutil.which(route_head) or ""
    if not candidate or not os.path.isabs(candidate):
        return None
    try:
        resolved = Path(candidate).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return Path(os.path.normpath(candidate)), resolved


def _path_inside_roots(path: Path, roots: Any) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _route_entry_resolves_to_trusted_root(segment: str) -> bool:
    """把解开 wrapper 后的真实入口绑定到可信系统目录。"""
    entry = _resolve_route_entry_paths(segment)
    if entry is None:
        return False
    _candidate, resolved = entry
    return _path_inside_roots(resolved, _TRUSTED_EXECUTABLE_ROOTS)


def _run_writable_entry_roots(state: Any) -> list[Path] | None:
    """本 run 的可写根能力集合；无法确定时返回 None（调用方保守拒绝）。

    复用 subprocess_policy 的唯一能力来源（Core 授予写根 + 本 run 消费的
    人工批准精确路径）；bash_sandbox_roots 的 writable 侧是它的子集。
    无 run 上下文（state=None）时不存在能被本 run 写到的根。
    """
    if state is None:
        return []
    try:
        from .subprocess_policy import _local_write_capability_roots
    except ImportError:
        from tools.subprocess_policy import _local_write_capability_roots
    try:
        return _local_write_capability_roots(state)
    except Exception:
        return None


_ROUTE_ENTRY_IDENTITY_KEY = "_route_entry_identity"


_REFERENCED_FILE_MAX_COUNT = 64
_REFERENCED_FILE_HASH_LIMIT = 16 * 1024 * 1024
_REFERENCED_FILES_HASH_BUDGET = 64 * 1024 * 1024
_REFERENCED_FILE_MAX_WORDS = 512


def _shell_words(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return text.split()


# 第三会话复审 0915 P1：失败的那次运行会把日志/输出写到命令点名的路径上（> run.log、tee、
# --out result.json、位置参数）。这些文件也纳入指纹的话，下一次摘要就「变了」，没修的重跑被
# 放行——比只算命令文本还松。所以只纳入像脚本的文件：扩展名像脚本，或首行是 #!（可执行位不算：失败运行构建出的二进制也带）。
# 代价：只改配置文件（cfg.yaml）不会解锁重跑，与 K9 之前相同。
_SCRIPT_SUFFIXES = frozenset({
    ".py", ".sh", ".bash", ".zsh", ".r", ".jl", ".pl", ".rb", ".js", ".mjs", ".lua", ".m", ".tcl",
})


def _looks_like_script(path: Path) -> bool:
    if path.suffix.lower() in _SCRIPT_SUFFIXES:
        return True
    # 可执行位本身不算：失败运行构建出的二进制（bash build.sh && ./app 里的 app）也带可执行位，
    # 算进来又会让没修的重跑放行（第三会话复审 0915b P3）。无扩展名的脚本靠首行 #! 认。
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def _referenced_file_candidates(cmd: str) -> list[str]:
    """命令里的每个 shell 词；含空白的词再切一层（``bash -c 'python x.py'``），
    ``--opt=value`` / ``key=value`` 另取等号右边。不挑「第一个非选项操作数」：
    ``python -X dev x.py``、``env python x.py``、``mpirun -np 4 python x.py`` 都不漏。"""
    words: list[str] = []
    for word in _shell_words(cmd):
        nested = _shell_words(word) if re.search(r"\s", word) else []
        for item in (word, *nested):
            words.append(item)
            if "=" in item:
                words.append(item.split("=", 1)[1])
        if len(words) >= _REFERENCED_FILE_MAX_WORDS:
            break
    return words[:_REFERENCED_FILE_MAX_WORDS]


def _referenced_file_fingerprints(cmd: str, cwd: Any, state: Any) -> dict[str, list[Any]]:
    """命令引用的、本 run 改得动的文件的指纹（收敛任务书 K9 改法 1）。

    恢复守卫按载荷摘要判「重跑的是不是同一份载荷」。摘要原先只算命令文本：修好
    wrapper 脚本后逐字重跑被拒，模型改命令文本绕过，没修的处理程序又跑了一次
    （活体 partial_validation_failure）。脚本内容变了，载荷就变了。
    只认像脚本的文件（见 _looks_like_script）：日志、输出、数据文件会被失败的那次运行
    自己改写，算进来就等于放行没修的重跑。

    只取 resolve 后落在 cwd 或本 run 可写根内、真实存在的普通文件（符号链接 resolve
    后在界外的不算）：本 run 改不了的文件不会因为「修好了」而变。不超过 16 MiB 取
    sha256，更大的、或本次累计 sha256 超过 64 MiB 之后的取 (size, mtime_ns)。
    没有引用任何文件时返回空，摘要与旧公式逐字相同。
    """
    try:
        base = Path(str(cwd)).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return {}
    roots = [base, *(_run_writable_entry_roots(state) or [])]
    found: dict[str, Path] = {}
    for word in _referenced_file_candidates(cmd or ""):
        if not word or word.startswith("-") or "\0" in word:
            continue
        candidate = Path(word) if os.path.isabs(word) else base / word
        try:
            resolved = candidate.resolve(strict=True)
            if (
                not resolved.is_file()
                or not _path_inside_roots(resolved, roots)
                or not _looks_like_script(resolved)
            ):
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            key = os.path.relpath(resolved, base)
        except ValueError:
            key = str(resolved)
        found.setdefault(key, resolved)
    fingerprints: dict[str, list[Any]] = {}
    budget = _REFERENCED_FILES_HASH_BUDGET
    for key in sorted(found)[:_REFERENCED_FILE_MAX_COUNT]:
        resolved = found[key]
        try:
            size = resolved.stat().st_size
            if size <= _REFERENCED_FILE_HASH_LIMIT and size <= budget:
                digest = hashlib.sha256()
                with resolved.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                fingerprints[key] = ["sha256", digest.hexdigest()]
                budget -= size
            else:
                fingerprints[key] = ["size_mtime_ns", size, resolved.stat().st_mtime_ns]
        except OSError:
            continue
    return fingerprints


def _referenced_files_payload(cmd: str, cwd: Any, state: Any) -> dict[str, Any]:
    fingerprints = _referenced_file_fingerprints(cmd, cwd, state)
    return {"referenced_files": fingerprints} if fingerprints else {}


def _bash_payload_digest(
    cmd: str, cwd: Any, *, timeout: Any, execution_params: Any, state: Any,
    read_only: bool = False,
) -> str:
    """safe_run_bash 的载荷摘要：只被恢复守卫（重跑同一载荷）使用，不参与审批匹配。"""
    payload = {
        "command": cmd,
        "cwd": str(Path(str(cwd)).resolve(strict=False)),
        "timeout": timeout,
        "execution_params": execution_params,
        # 只读调用不绑路线、摘要用不上，不去读文件。
        **({} if read_only else _referenced_files_payload(cmd, cwd, state)),
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _path_lookup_influenced_by_run(writable_roots: list[Path]) -> bool:
    """PATH 里任何空段、相对段或位于本 run 可写根内的目录都算可被影响。"""
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            return True
        try:
            resolved = Path(entry).resolve(strict=False)
        except (OSError, RuntimeError):
            return True
        if _path_inside_roots(resolved, writable_roots):
            return True
    return False


def _route_entry_identity_pin_ok(
    state: Any, name: str, resolved: Path | None,
    *, pin_key: str | None = None,
) -> bool:
    """入口身份钉住：首次判可信时记录，同 run 内同名入口任一变化即撤销豁免。

    ``pin_key`` 让调用方把记录挂到比程序名更窄的键上。裸名入口的身份就是
    "PATH 查找到什么"，键只能是程序名；而绝对路径入口的身份是"这条路径上
    是什么"，必须按调用路径分键——否则 ``/usr/local/bin/python3.12`` 的缺失
    记录会撞上 conda ``python3.12`` 的 realpath 记录，两条互不相干的入口
    互相撤销对方的豁免，造出本 run 内不可恢复的锁死。
    """
    if resolved is None:
        identity: dict[str, Any] = {"realpath": None, "missing": True}
    else:
        try:
            stat_result = resolved.stat()
        except OSError:
            return False
        identity = {
            "realpath": str(resolved),
            "st_dev": stat_result.st_dev,
            "st_ino": stat_result.st_ino,
            "st_size": stat_result.st_size,
            "st_mtime": stat_result.st_mtime,
        }
    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        return False
    pins = hook_state.setdefault(_ROUTE_ENTRY_IDENTITY_KEY, {})
    if not isinstance(pins, dict):
        return False
    key = pin_key or name
    pinned = pins.get(key)
    if pinned is not None and not isinstance(pinned, dict):
        return False
    if pinned is None:
        pins[key] = {"identity": identity, "revoked": False}
        try:
            state.append_transcript(
                "route_entry_identity_pinned", program=name, **identity)
        except Exception:
            pass
        return True
    if pinned.get("revoked"):
        return False
    if pinned.get("identity") != identity:
        pinned["revoked"] = True
        try:
            state.append_transcript(
                "route_entry_identity_revoked", program=name,
                pinned=pinned.get("identity"), observed=identity)
        except Exception:
            pass
        return False
    return True


# 未展开的 shell 元字符：本谓词按字面串与可写根比对，而 payload 最终交给
# `/bin/bash -c`，由 shell 再做一次展开。字面串上"不在可写根内"因此不能推出
# 展开后也不在（`<runs>/*/bin/ls` 字面上不含 run root，展开后正是 run 自产
# 二进制）。含这些字符的入口一律不进本分支。
_UNEXPANDED_ENTRY_METACHARS = frozenset("*?[]{}~$`!\\\"'")


def _path_entry_pin_key(route_head: str) -> str:
    """路径形态入口的身份钉住键：按调用路径分键，不占用裸名键。

    裸名入口的身份是"PATH 查找到什么"，只能按程序名钉住；路径形态入口的
    身份是"这条路径上是什么"。两者共用 basename 键会让互不相干的入口
    （镜像里的 ``/usr/local/bin/python3.12`` 与 conda 的 ``python3.12``）
    互相撤销对方的豁免，且 ``revoked`` 不复位 → 本 run 内该程序名的探测
    全部锁死。同一条路径上"缺失 → 出现"仍然共用一个键，撤销语义不变。
    """
    return f"path:{os.path.normpath(route_head)}"


def _absent_path_entry_outside_run_writable_roots(
    route_head: str, name: str, writable_roots: list[Path], state: Any,
) -> bool:
    """路径形态入口当前解析不到时，仍按"本 run 影响不了它"裁决。

    只读探测必须能复核"这个入口到底在不在"：宿主机上不存在的绝对路径
    （容器/作业镜像自带的 ``/usr/local/bin/python3.12`` 之类）会让
    ``_resolve_route_entry_paths`` 的 strict resolve 失败，若就此一律判不可信，
    ``<路径> --version`` 这类纯只读探测会被路线门拦死——而它恰恰是推翻
    "目标不存在"前提所需的复核手段。这里不放宽任何执行语义：argv 形状门
    （单个 capability flag）在上游 ``_read_only_entry_is_trusted`` 的调用方，
    本函数只回答入口是否落在本 run 能写到的根内。

    该判据只对**字面路径**成立，因此含未展开元字符的入口一律拒绝。
    相对路径入口按 cwd 解析，而 cwd 通常就是本 run 可写根，保守拒绝。
    身份钉住按调用路径分键（见 ``_path_entry_pin_key``）：本次记 missing，
    同一路径上的入口一旦出现即身份变化 → 撤销豁免。
    """
    del name
    if not os.path.isabs(route_head):
        return False
    if _UNEXPANDED_ENTRY_METACHARS.intersection(route_head):
        return False
    candidate = Path(os.path.normpath(route_head))
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if _path_inside_roots(candidate, writable_roots):
        return False
    if _path_inside_roots(resolved, writable_roots):
        return False
    if state is None:
        return True
    return _route_entry_identity_pin_ok(
        state, str(candidate), None, pin_key=_path_entry_pin_key(route_head))


def _entry_outside_run_writable_roots(
    segment: str, name: str, state: Any,
    *, allow_absent_path_entry: bool = False,
) -> bool:
    """新信任谓词：入口可信 ⟺ 本 run 影响不了它。

    调用路径与 realpath 都不得落在本 run 任何可写根内；可写根集合无法
    确定时保守拒绝。系统根已由快速路径先行放行，这里承接 conda/spack 等
    科学工具链入口，并附加同 run 身份钉住。当前解析不到的入口只会得到
    command-not-found：裸名只要 PATH 查找不受本 run 可写根影响、绝对路径
    （仅纯只读探测语境，见 ``allow_absent_path_entry``）只要该路径不落在
    本 run 可写根内，本 run 同样影响不了它（后续出现即身份变化 → 撤销豁免）。
    """
    writable_roots = _run_writable_entry_roots(state)
    if writable_roots is None:
        return False
    route_head, _ = _stage_route_head(segment)
    if not route_head:
        return False
    # 路径形态入口按调用路径钉身份，裸名入口按程序名钉身份；同一条路径上的
    # "缺失 → 出现"共用一个键，撤销语义与 266d3023 一致。
    is_path_entry = "/" in route_head
    pin_key = _path_entry_pin_key(route_head) if is_path_entry else None
    entry = _resolve_route_entry_paths(segment)
    if entry is None:
        if is_path_entry:
            return allow_absent_path_entry and (
                _absent_path_entry_outside_run_writable_roots(
                    route_head, name, writable_roots, state))
        if _path_lookup_influenced_by_run(writable_roots):
            return False
        if state is None:
            return True
        return _route_entry_identity_pin_ok(state, name, None)
    candidate, resolved = entry
    if _path_inside_roots(resolved, writable_roots):
        return False
    if _path_inside_roots(candidate, writable_roots):
        return False
    if state is None:
        return True
    return _route_entry_identity_pin_ok(
        state, name, resolved, pin_key=pin_key)


def _bounded_entry_is_trusted(segment: str, name: str) -> bool:
    """builtin 直接可信；外部有界工具必须解析到可信系统根。"""
    route_head, _ = _stage_route_head(segment)
    if (
        name in {":", "echo", "printf"}
        and "/" not in route_head
        and not _segment_uses_wrapper(segment)
    ):
        return True
    return _route_entry_resolves_to_trusted_root(segment)


def _read_only_entry_is_trusted(
    segment: str, name: str, state: Any | None = None,
    *, allow_absent_path_entry: bool = False,
) -> bool:
    """把只读豁免绑定到 shell builtin、可信系统目录，或本 run 影响不了的入口。

    ``allow_absent_path_entry`` 只由整条命令级的只读判定
    （``_is_read_only_shell_command``）传 True：那里已经排除了重定向、管道、
    命令替换与非只读 argv 形状，剩下的才是纯只读探测。逐事件的入口已知性
    投影（``_route_event_is_known``）不传，宿主机上解析不到的路径入口在那里
    仍算未知 → ``unknown_executable`` → 高后果策略，重定向/管道等形态照旧
    必须匹配路线。
    """
    if _segment_uses_wrapper(segment):
        return False
    route_head, _ = _stage_route_head(segment)
    if not route_head:
        return False
    if name in _TRUSTED_READ_ONLY_BUILTINS and "/" not in route_head:
        return True
    if _route_entry_resolves_to_trusted_root(segment):
        return True
    if _segment_has_identity_override(segment):
        return False
    return _entry_outside_run_writable_roots(
        segment, name, state,
        allow_absent_path_entry=allow_absent_path_entry)


def _is_static_command_lookup(name: str, args: list[str]) -> bool:
    """Recognize Bash's non-executing ``command -v/-V`` builtin form.

    This intentionally accepts only literal command-name operands. It must
    not turn ``command <program> ...`` into a read-only shortcut, since that
    form executes the named program.
    """
    return (
        name == "command"
        and len(args) >= 2
        and args[0] in {"-v", "-V"}
        and all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.+@-]*", item)
            for item in args[1:]
        )
    )


_ROUTE_PIPELINE_SIDECAR_PROGRAMS = frozenset(
    set(_READ_ONLY_COMMANDS) | {"echo", "printf", "tee"}
)


def _route_event_program(event: Any) -> str:
    """保留单个 AST 事件的显式入口路径；wrapper 只作为语法事实。"""
    head = str(event.head or "")
    if not head or head == "__hf_noexec__":
        return ""
    route_head, _args = _stage_route_head(str(event.raw or ""))
    if route_head and os.path.basename(route_head) == head:
        return route_head
    return head


def _route_event_trust_segment(event: Any, program: str) -> str:
    """用已解析入口重建单事件 argv，避免再次解释整条 shell 文本。"""
    return shlex.join([program, *(str(item) for item in event.args)])


def _route_event_is_known(
    event: Any, program: str, state: Any | None = None,
) -> bool:
    """判断一个真实或委托执行头是否已有受信任的机械语义。"""
    name = os.path.basename(program)
    args = [str(item) for item in event.args]
    if not name:
        return False
    if name in {"cd", "export", "set", "umask", "__hf_noexec__"}:
        return True
    segment = _route_event_trust_segment(event, program)
    external_control = _bash_semantics.classify_external_control_command(name, args)
    if external_control != _bash_semantics.EXTERNAL_CONTROL_NONE:
        return _read_only_entry_is_trusted(segment, name, state)
    if name in _MECHANICALLY_BOUNDED_SHELL_PROGRAMS:
        return _bounded_entry_is_trusted(segment, name)
    if _is_static_command_lookup(name, args):
        return _read_only_entry_is_trusted(segment, name, state)
    if _is_strict_capability_probe(name, args):
        return _read_only_entry_is_trusted(segment, name, state)
    if name == "git" and _git_command_is_read_only(args):
        return _read_only_entry_is_trusted(segment, name, state)
    if name == "rg" and _rg_embedded_launcher_option(args) is not None:
        return False
    if name == "find" and not any(token in {
            "-delete", "-exec", "-execdir", "-ok", "-okdir",
            "-fprint", "-fprintf",
    } for token in args):
        return _read_only_entry_is_trusted(segment, name, state)
    if (name == "tar" and _tar_command_is_list_only(args)
            and not _segment_has_env_assignment(segment)):
        return _read_only_entry_is_trusted(segment, name, state)
    if (name in {"pip", "pip3"} and _pip_command_is_read_only(args)
            and not _segment_has_env_assignment(segment)):
        return _read_only_entry_is_trusted(segment, name, state)
    if name in _READ_ONLY_COMMANDS:
        return _read_only_entry_is_trusted(segment, name, state)
    return False


def _route_pipeline_group(event: Any) -> tuple[str, ...] | None:
    for index, item in enumerate(event.context):
        if str(item).startswith("pipeline:"):
            return tuple(str(part) for part in event.context[:index])
    return None


def _route_event_is_pipeline_sidecar(
    event: Any, program: str, state: Any | None = None,
) -> bool:
    name = os.path.basename(program)
    return (
        name in _ROUTE_PIPELINE_SIDECAR_PROGRAMS
        and _route_event_is_known(event, program, state)
    )


def _project_bash_route(
    cmd: str, analysis: Any | None = None, state: Any | None = None,
) -> dict[str, Any]:
    """从同一 Bash AST 同时投影 route 身份与未知入口事实。

    路径层继续检查所有 redirect/写目标；这里只决定哪些真实执行头需要
    路线与强监督，以及唯一可领取路线收据的 payload 入口。
    """
    try:
        parsed = analysis or _te.analyze_bash(
            cmd or "", initial_cwd=os.getcwd())
    except Exception as exc:
        return {
            "program": "", "unknown_entry": True,
            "analysis_error": type(exc).__name__,
        }
    uncertain = bool(
        parsed.analyzer_unavailable is not None
        or parsed.parse_error
        or parsed.dynamic_execution
        or parsed.path_unverifiable
    )
    command_events = [
        event for event in parsed.static_path_events
        if event.kind == "command"
    ]
    unknown_entry = uncertain
    identity_events: list[tuple[Any, str]] = []
    for event in command_events:
        raw = str(event.raw or "")
        if _segment_has_identity_override(raw):
            unknown_entry = True
        if event.dispatch_role == "transparent":
            # 字面 shell -c 只是语法 wrapper，但其二进制本身仍须来自可信根。
            if (str(event.head or "") in _SHELL_EXECUTABLES
                    and not _route_entry_resolves_to_trusted_root(raw)):
                unknown_entry = True
            continue
        if str(event.head or "") == "__hf_noexec__":
            continue
        program = _route_event_program(event)
        name = os.path.basename(program)
        if event.dispatch_role == "direct" and name not in {
                "cd", "export", "set", "umask", "__hf_noexec__",
        }:
            identity_events.append((event, program))
        if not _route_event_is_known(event, program, state):
            unknown_entry = True

    programs = [program for _event, program in identity_events if program]
    if not programs:
        program = ""
    elif len(programs) == 1:
        program = programs[0]
    else:
        groups = [_route_pipeline_group(event) for event, _ in identity_events]
        primary = [
            (event, item) for event, item in identity_events
            if not _route_event_is_pipeline_sidecar(event, item, state)
        ]
        same_pipeline = (
            all(group is not None for group in groups)
            and len(set(groups)) == 1
        )
        if same_pipeline and len(primary) == 1:
            program = primary[0][1]
        else:
            program = "compound:" + "+".join(programs)
    return {"program": program, "unknown_entry": unknown_entry}


def _shell_entry_is_unknown(cmd: str) -> bool:
    """兼容薄包装：未知性与 program 必须来自同一次 AST 投影。"""
    return bool(_project_bash_route(cmd)["unknown_entry"])


_TAR_LIST_ONLY_SHORT = re.compile(r"[tvfzjJa]+")
_TAR_LIST_ONLY_LONG = frozenset({
    "--list", "--file", "--verbose", "--gzip", "--gunzip", "--bzip2", "--xz",
    "--zstd", "--auto-compress", "--wildcards", "--no-wildcards",
})


def _tar_command_is_list_only(args: list[str]) -> bool:
    """tar 只在「列目录」形状下算只读（收敛任务书 K6）。

    短选项只认 t v f z j J a 的组合，长选项只认上面的白名单；-x/-c/-r/-u/-C、
    --to-command、-I/--use-compress-program、--checkpoint-action、-F/--info-script
    这类会解包、写文件或执行外部程序的一律不认。含「:」的操作数不认：GNU tar 把
    host:path 当远端归档，会经 rsh 执行命令。
    """
    listing = False
    for index, token in enumerate(args):
        if token.startswith("--"):
            option = token.split("=", 1)[0]
            if option not in _TAR_LIST_ONLY_LONG:
                return False
            if ":" in token.split("=", 1)[-1] and "=" in token:
                return False
            listing = listing or option == "--list"
        elif token.startswith("-") and len(token) > 1:
            if not _TAR_LIST_ONLY_SHORT.fullmatch(token[1:]):
                return False
            listing = listing or "t" in token[1:]
        elif index == 0 and _TAR_LIST_ONLY_SHORT.fullmatch(token):
            listing = listing or "t" in token     # 旧式捆绑选项：tar tvf archive.tar
        elif ":" in token:
            return False
    return listing


_PIP_READ_ONLY_OPTIONS = frozenset({"--format", "-v", "--verbose", "-f", "--files"})


def _pip_command_is_read_only(args: list[str]) -> bool:
    """pip 只认不带网络/缓存类选项的 list、show（收敛任务书 K6）。"""
    if not args or args[0] not in {"list", "show"}:
        return False
    return all(
        not token.startswith("-") or token.split("=", 1)[0] in _PIP_READ_ONLY_OPTIONS
        for token in args[1:]
    )


def _is_read_only_shell_command(cmd: str, state: Any | None = None) -> bool:
    """Recognize a deliberately small set of inspection-only shell commands."""
    # 命令替换按**原串字面**拒绝：``"$(...)"`` 在双引号里照样展开，掩码判据
    # 看不到它，所以这一条不能挪进 _read_only_pipeline_stages。
    if "$(" in cmd or "`" in cmd:
        return False
    stages: list[str] = []
    for piece in _split_shell_segments(_expand_cmd_vars(cmd)):
        expanded = _read_only_pipeline_stages(piece)
        if expanded is None:
            return False
        stages.extend(expanded)
    for segment in stages:
        if re.match(r"^cd\b", segment):
            continue
        if re.match(r"^export\b", segment):
            return False
        if _segment_has_identity_override(segment):
            return False
        try:
            tokens = _strip_env_prefix(shlex.split(segment, posix=True))
        except ValueError:
            return False
        if not tokens:
            continue
        if any(token in {"|", ">", ">>", "&>", "2>", "2>>"}
               for token in tokens):
            return False
        name, args = _stage_head(segment)
        if not name:
            continue
        external_control = _bash_semantics.classify_external_control_command(name, args)
        if external_control != _bash_semantics.EXTERNAL_CONTROL_NONE:
            if not _read_only_entry_is_trusted(segment, name, state):
                return False
            if external_control == _bash_semantics.EXTERNAL_CONTROL_QUERY:
                continue
            return False
        # 纯只读探测形状（参数恰为单个 capability flag）才允许"入口当前解析
        # 不到"的绝对路径通过信任谓词——这正是复核"这个入口到底在不在"所需的
        # 形式。真实执行形态（无 flag、带参数、带重定向/管道）拿不到这条豁免。
        # 规格里"ls/stat/file/which 作用于路径实参"那一档不需要本豁免：那时
        # 入口是裸名 ls，早已由 _TRUSTED_EXECUTABLE_ROOTS 快路径放行；若按
        # basename 开豁免，反而会让"解析不到的绝对入口 + 任意 argv"（例如
        # `<某路径>/ls --do-anything`）被判只读，故此处只认 capability flag。
        probe_shape = _is_strict_capability_probe(name, args)
        if not _read_only_entry_is_trusted(
                segment, name, state, allow_absent_path_entry=probe_shape):
            return False
        if _is_static_command_lookup(name, args):
            continue
        if _is_strict_capability_probe(name, args):
            continue
        if name == "git":
            if not _git_command_is_read_only(args):
                return False
            continue
        if name == "rg" and _rg_embedded_launcher_option(args) is not None:
            return False
        if name == "find":
            if any(token in {
                    "-delete", "-exec", "-execdir", "-ok", "-okdir",
                    "-fprint", "-fprintf"} for token in args):
                return False
            continue
        if name == "tar":
            if _segment_has_env_assignment(segment) or not _tar_command_is_list_only(args):
                return False
            continue
        if name in {"pip", "pip3"}:
            if _segment_has_env_assignment(segment) or not _pip_command_is_read_only(args):
                return False
            continue
        if name in {"echo", "printf", ":"} and _bounded_entry_is_trusted(
                segment, name):
            # 活体探测普遍用 `echo "=== ... ==="` 给各段打标题。没有写重定向的
            # echo/printf/: 只往 stdout 写字节，不碰文件系统——写重定向与命令
            # 替换已在 _read_only_pipeline_stages / 本函数入口一律拒绝，所以
            # 这里不会放行 `echo x > f` 或 `echo $(...)`。入口信任仍走
            # _bounded_entry_is_trusted：wrapper 与路径形态入口不吃这条。
            continue
        if name not in _READ_ONLY_COMMANDS:
            return False
    return True


def _route_observed_program(cmd: str, _depth: int = 0) -> str:
    """兼容薄包装；``_depth`` 仅保留旧调用签名。"""
    del _depth
    return str(_project_bash_route(cmd)["program"])


def _static_submit_program_sequence(
    cmd: str,
    analysis: Any | None = None,
) -> tuple[list[str] | None, str | None]:
    """Return a route-bindable static linear command sequence for submit_job.

    One managed job may carry a linear, statically proven compound payload, but
    it cannot receive a route receipt merely because its first command matches.
    This is intentionally narrower than the general Bash analyzer: branches,
    pipelines, functions, subshells, delegated commands and shell wrappers are
    not an opt-in compound identity.  Dynamic scripts remain rejected by the
    existing submit_job static validity gate.
    """
    try:
        parsed = analysis or _te.analyze_bash(
            cmd or "", initial_cwd=os.getcwd())
    except Exception as exc:
        return None, f"analysis_error:{type(exc).__name__}"
    if parsed.analyzer_unavailable is not None:
        return None, "analyzer_unavailable"
    if parsed.parse_error:
        return None, "parse_error"
    if parsed.dynamic_execution:
        return None, "dynamic_execution"
    if parsed.path_unverifiable:
        return None, "path_unverifiable"

    programs: list[str] = []
    for event in parsed.static_path_events:
        if event.kind != "command":
            continue
        if event.dispatch_role != "direct":
            return None, f"non_direct_dispatch:{event.dispatch_role}"
        if event.dynamic_args or event.runtime_args:
            return None, "runtime_or_dynamic_arguments"
        if any(
            item != "root"
            and not item.startswith(("list-left:&&", "list-right:&&"))
            for item in event.context
        ):
            return None, "nonlinear_or_nested_control_flow"
        program = _route_event_program(event)
        if not program or program == "__hf_noexec__":
            continue
        programs.append(program)
    if len(programs) < 2:
        return None, "not_compound"
    return programs, None


def _observed_workdir_roles(state: Any, workdir: str | None) -> list[str]:
    try:
        return sorted({
            role.role for role in matching_path_roles(str(workdir or ""), state)
        })
    except Exception:
        return []


def _bash_route_action(
    cmd: str,
    *,
    execution_stage: str,
    workdir_roles: list[str] | None = None,
    route_step_id: str | None = None,
    state: Any | None = None,
) -> dict[str, Any]:
    try:
        route_analysis = _te.analyze_bash(
            cmd or "", initial_cwd=os.getcwd())
    except Exception:
        route_analysis = None
    read_only = _is_read_only_shell_command(cmd, state)
    major_build = _is_major_build(cmd)
    managed_setup = _is_provision_or_configure_action(cmd)
    network_acquire = _is_network_acquire_action(cmd)
    route_projection = _project_bash_route(
        cmd, analysis=route_analysis, state=state)
    external_control = getattr(
        route_analysis, "external_control", _bash_semantics.EXTERNAL_CONTROL_NONE)
    unknown_entry = (
        not read_only and bool(route_projection["unknown_entry"])
    )
    effects: set[str] = set()
    if not read_only:
        effects.add("workspace_write")
    if external_control == _bash_semantics.EXTERNAL_CONTROL_EFFECT:
        effects.add("external_job")
    # 旧 stage 只能保留兼容提示，不能把机械只读的 ``pwd``/``ls`` 升格成
    # 进程树。真实构建/运行由动作分析兜底，路线 effects 还可继续加严。
    major_run = _is_major_run(cmd)
    if major_build or major_run or managed_setup or network_acquire:
        effects.add("process_tree")
    # 所有权要求只给"需要活过本次调用"的动作：真构建与真启动器。解包、configure、
    # 拉取依赖有界且本地，照常留在 safe_run_bash 的有界生命周期里（2026-09-12）。
    if major_build or major_run:
        effects.add("managed_lifecycle")
    program = str(route_projection["program"])
    if not read_only and network_acquire:
        effects.add("network_access")
    if managed_setup:
        effects.add("environment_change")
    if unknown_entry:
        # 未识别不等于低风险：框架词汇之外的机械事实会把策略提升为
        # guarded_unknown_effect；匹配路线后也必须安装强资源守卫。
        effects.add("unknown_executable")
    return {
        "tool": "safe_run_bash",
        "program": program,
        "route_step_id": str(route_step_id or "").strip(),
        "read_only": read_only,
        "observed_effects": sorted(effects),
        "workdir_roles": list(workdir_roles or []),
        "dry_run": False,
        "legacy_policy": {
            "stage": execution_stage,
            "guarded_build": (
                execution_stage == "toolchain_build"
                or major_build
                or managed_setup
                or network_acquire
            ),
            "formal_simulation": execution_stage == "simulation",
        },
    }


def _python_route_action(
    *,
    code: str,
    execution_stage: str,
    requirements: list | None,
    workdir_roles: list[str] | None = None,
    route_step_id: str | None = None,
    effect_projection: PythonEffectProjection | None = None,
) -> dict[str, Any]:
    effects = {"workspace_write"}
    has_archive_unpack = (
        effect_projection.direct_archive_unpack
        if effect_projection is not None
        else _python_has_direct_archive_unpack(code)
    )
    if requirements or has_archive_unpack:
        effects.add("environment_change")
    return {
        "tool": "safe_execute_python",
        "program": "python",
        "route_step_id": str(route_step_id or "").strip(),
        "read_only": False,
        "observed_effects": sorted(effects),
        "workdir_roles": list(workdir_roles or []),
        "dry_run": False,
        "legacy_policy": {
            "stage": execution_stage,
            "guarded_build": execution_stage == "toolchain_build",
            "formal_simulation": execution_stage == "simulation",
        },
    }


def _resolve_route_context(state: Any, action: dict[str, Any]) -> dict[str, Any]:
    try:
        try:
            from .execution_route import resolve_execution_context
        except ImportError:
            from tools.execution_route import resolve_execution_context
        return resolve_execution_context(state, action)
    except Exception as exc:
        return {
            "decision": "resolver_error",
            "reason": type(exc).__name__,
            "tool": str(action.get("tool") or ""),
        }


def _route_workdir_choice(
    state: Any,
    decision: dict[str, Any],
    *,
    create: bool = True,
) -> dict[str, Any]:
    try:
        try:
            from .execution_route import route_default_workdir
        except ImportError:
            from tools.execution_route import route_default_workdir
        return route_default_workdir(state, decision, create=create)
    except Exception as exc:
        return {"status": "resolver_error", "reason": type(exc).__name__}


def _record_route_context(
    state: Any,
    action: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    try:
        try:
            from .execution_route import record_execution_route_shadow
        except ImportError:
            from tools.execution_route import record_execution_route_shadow
        record_execution_route_shadow(state, action, decision)
    except Exception:
        pass


def _enforce_route_context(
    state: Any,
    action: dict[str, Any],
    decision: dict[str, Any],
    *,
    phase: str = "pre_spawn",
) -> dict[str, Any] | None:
    """调用两阶段统一路线门；真实执行时门异常一律 fail-closed。"""
    try:
        try:
            from .execution_route import enforce_execution_route
        except ImportError:
            from tools.execution_route import enforce_execution_route
        return enforce_execution_route(
            state, action, decision, phase=phase)
    except Exception as exc:
        effects = {
            str(item) for item in (
                decision.get("effective_effects")
                or action.get("observed_effects")
                or []
            )
        }
        exact_read_only = (
            action.get("read_only") is True and not effects
        )
        if action.get("dry_run") is True or exact_read_only:
            return None
        return {
            "status": "error",
            "reason": "execution_route_resolver_failed",
            "error": (
                "执行上下文门异常，真实动作已在启动前拒绝；"
                "只读和 dry-run 诊断仍可继续。"
            ),
            "route_blocked": True,
            "blocker": {
                "kind": "execution_route_resolver_failed",
                "reason": type(exc).__name__,
                "suggested_owner": "framework",
                "node_action": "report_framework_blocker_keep_read_only_diagnostics",
            },
        }


def _begin_route_attempt(
    state: Any,
    decision: dict[str, Any],
    *,
    tool: str,
    action: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        try:
            from .execution_route import (
                begin_route_step_attempt, route_binding_block,
            )
        except ImportError:
            from tools.execution_route import (
                begin_route_step_attempt, route_binding_block,
            )
        binding = begin_route_step_attempt(
            state, decision, tool=tool, action=action)
        return binding, route_binding_block(binding)
    except Exception as exc:
        if decision.get("decision") != "matched_ready_step":
            return None, None
        return None, {
            "status": "error",
            "reason": "route_binding_persistence_failed",
            "error": "路线步骤开始收据无法持久化，高后果进程未启动。",
            "route_blocked": True,
            "blocker": {
                "kind": "route_binding_persistence_failed",
                "reason": type(exc).__name__,
                "suggested_owner": "framework",
                "node_action": "repair_transcript_persistence_before_retry",
            },
        }


def _finish_route_attempt(
    state: Any,
    binding: dict[str, Any] | None,
    *,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any] | None:
    try:
        try:
            from .execution_route import (
                finish_route_step_attempt, managed_execution_result_receipt,
                route_attempt_receipt, route_outcome_block,
            )
        except ImportError:
            from tools.execution_route import (
                finish_route_step_attempt, managed_execution_result_receipt,
                route_attempt_receipt, route_outcome_block,
            )
        event = finish_route_step_attempt(
            state, binding, result=result, error=error)
        attempt_receipt = route_attempt_receipt(event)
        if isinstance(result, dict) and attempt_receipt is not None:
            result["route_attempt"] = attempt_receipt
        block = route_outcome_block(event)
        if isinstance(block, dict):
            if attempt_receipt is not None:
                block["route_attempt"] = attempt_receipt
            execution_receipt = managed_execution_result_receipt(result)
            if execution_receipt is not None:
                block["execution_receipt"] = execution_receipt
        return block
    except Exception as exc:
        if not binding:
            return None
        binding_receipt = {
            key: binding.get(key)
            for key in (
                "attempt_id",
                "route_artifact_id",
                "route_version",
                "route_content_hash",
                "route_step_id",
                "step_definition_hash",
                "tool",
            )
            if binding.get(key) is not None
        }
        return {
            "status": "error",
            "reason": "route_outcome_persistence_failed",
            "error": "动作已返回，但路线结果收据处理失败；状态未知，禁止重试。",
            **({"execution_outcome": dict(result)}
               if isinstance(result, dict) else {}),
            "route_attempt_binding": binding_receipt,
            "do_not_repeat_action": True,
            "payload_must_not_rerun": True,
            "safe_to_retry": False,
            "model_next_action": {
                "action": "report_blocker_and_end_current_run",
                "reason": "route reconciliation is not a model tool",
            },
            "blocker": {
                "kind": "route_outcome_persistence_failed",
                "reason": type(exc).__name__,
                "route_attempt_binding": binding_receipt,
                "suggested_owner": "framework",
                "node_action": "repair_and_reconcile_attempt_before_retry",
            },
        }


def _physical_execution_outcome(result: dict[str, Any]) -> dict[str, Any]:
    """Keep census persistence failures separate from payload truth."""
    outcome = result.get("execution_outcome")
    if (
        result.get("execution_action_phase") in {"spawn_observation", "terminal"}
        and isinstance(outcome, dict)
    ):
        return outcome
    return result


def _begin_execution_action_census(
    state: Any,
    action: dict[str, Any],
    decision: dict[str, Any],
    route_binding: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Persist census admission after the route attempt, before payload spawn."""
    try:
        try:
            from .execution_action_census import begin_execution_action
        except ImportError:  # pragma: no cover - node runtime import style
            from tools.execution_action_census import begin_execution_action
        token = begin_execution_action(
            state, action, decision, route_binding=route_binding)
    except Exception as exc:
        token = {
            "status": "error",
            "error_code": "execution_action_census_persistence_failed",
            "reason": "execution action admission could not be persisted",
            "error_type": type(exc).__name__,
        }
    if token.get("status") == "success":
        return token, None
    return None, {
        **token,
        "status": "error",
        "execution_action_phase": "admission",
        "payload_spawned": False,
        "job_submitted": False,
        "safe_to_retry": False,
    }


def _settle_execution_action_census(
    state: Any,
    token: dict[str, Any],
    *,
    payload_spawned: bool | None,
    proof_source: str,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any] | None:
    """Settle spawn then terminal; return a no-replay blocker if incomplete."""
    try:
        try:
            from .execution_action_census import settle_execution_action
        except ImportError:  # pragma: no cover - node runtime import style
            from tools.execution_action_census import settle_execution_action
        settlement = settle_execution_action(
            state,
            token,
            payload_spawned=payload_spawned,
            job_submitted=False,
            proof_source=proof_source,
            result=result,
            error=error,
        )
    except Exception as exc:
        settlement = {
            "passed": False,
            "status": "error",
            "error_code": "execution_action_census_persistence_failed",
            "reason": "execution action settlement could not be persisted",
            "error_type": type(exc).__name__,
        }
    if settlement.get("passed") is True:
        return None
    spawn = settlement.get("spawn")
    terminal = settlement.get("terminal")
    problem = (
        spawn
        if isinstance(spawn, dict) and spawn.get("status") != "success"
        else terminal
        if isinstance(terminal, dict)
        else settlement
    )
    missing_phase = (
        problem.get("missing_phase") if isinstance(problem, dict) else None
    )
    return {
        **(problem if isinstance(problem, dict) else {}),
        "status": "error",
        "execution_action_phase": missing_phase or "terminal",
        "execution_action_census": settlement,
        "payload_must_not_rerun": True,
        "do_not_retry_payload": True,
        "safe_to_retry": False,
        **({"deferred_terminal": terminal}
           if isinstance(terminal, dict) and terminal.get("status") == "deferred"
           else {}),
    }


def _is_formal_scientific_action(
    state: Any,
    *,
    execution_stage: str,
    route_decision: dict[str, Any],
    mechanical_major_run: bool = False,
    mechanical_run_root_write: bool = False,
) -> bool:
    # ``stage`` 是旧调用方的兼容提示，只用于迁移期 shadow 对比。科学身份必须
    # 来自冻结 run_contract 与本次路线/机械动作，否则调用方写一个字符串就能
    # 绕过或误触发 prereg、输入与参数门。
    del execution_stage
    if route_decision.get("policy") == "formal_scientific_execution":
        return True
    try:
        try:
            from .run_contract import load_run_contract, requires_experiment_fallback_input_gate
        except ImportError:
            from tools.run_contract import load_run_contract, requires_experiment_fallback_input_gate
        contract = load_run_contract(state)
    except Exception:
        return False
    scientific_primary = (
        str(contract.get("execution_mode") or "scientific") == "scientific"
        and str(contract.get("run_role") or "") == "primary"
    )
    scientific_formal_input = (
        scientific_primary or requires_experiment_fallback_input_gate(contract)
    )
    if not scientific_formal_input:
        return False
    if mechanical_major_run or mechanical_run_root_write:
        return True
    effects = {
        str(item) for item in (route_decision.get("effective_effects") or [])
    }
    return (
        route_decision.get("decision") == "matched_ready_step"
        and route_decision.get("authoritative") is True
        and route_decision.get("declared_workdir_role") == "run_root"
        and bool(effects.intersection({
            "process_tree", "scientific_execution", "workspace_write",
        }))
    )



_MPI_RANK_OPTS = ("-np", "-n", "--np", "--ntasks")


def _record_mpi_actual_params(state: Any, cmd: str) -> None:
    """从实际执行命令机械提取 MPI 规模，供协议偏离审计使用。"""
    expanded = _expand_cmd_vars(cmd or "")
    if not _MPI_LAUNCHERS.intersection(
            Path(token).name for token in expanded.split()):
        return
    for segment in _split_shell_segments(expanded):
        try:
            tokens = _strip_env_prefix(shlex.split(segment, posix=True))
        except ValueError:
            continue
        tokens = _strip_exec_wrappers(tokens)
        if not tokens or Path(tokens[0]).name not in _MPI_LAUNCHERS:
            continue
        for index, token in enumerate(tokens[1:-1], start=1):
            if token in _MPI_RANK_OPTS and tokens[index + 1].isdigit():
                _record_actual_run_params(
                    state, f"{Path(tokens[0]).name}:{token}",
                    {"mpi_ranks": int(tokens[index + 1])})
                return


def _simulation_contract_block(
    state: Any,
    execution_params: Any,
    *,
    runner: str,
    input_package_artifact_id: str | None = None,
    input_package_bindings: dict[str, str] | None = None,
) -> dict | None:
    """simulation 执行前的契约审计：照跑、照记、不再拦截（判决拆除 2026-08-31）。

    - O3（sb:3512 降格）：preflight 聚合闸拆开全是记录完备性，未过项进
      hook_state/manifest；stage 不合法留 C。
    - O2（sb:3517）：输入包未验收照跑 + 如实记一条执行前提见证。
    - O1（sb:3521 降格）：prereg 参数偏离必须申报（三张表），不再禁止执行。

    ``input_package_bindings`` 是本分支为输入包逐项绑定校验加的结构化入参，
    与拦不拦无关，随审计一起传给 audit_input_delivery_for_execution。
    """
    try:
        try:
            from .preflight import audit_execution_contract, audit_experiment_preflight
            from .contract_audit import audit_input_delivery_for_execution
            from .run_contract import record_execution_precondition_witness, record_prereg_deviation
        except ImportError:
            from tools.preflight import audit_execution_contract, audit_experiment_preflight
            from tools.contract_audit import audit_input_delivery_for_execution
            from tools.run_contract import record_execution_precondition_witness, record_prereg_deviation
        preflight = audit_experiment_preflight(state, phase="simulation")
        state.append_transcript("experiment_preflight", **preflight)
        if not preflight.get("passed"):
            if "stage" in (preflight.get("blocking_reasons") or []):
                return {"status": "error",
                        "error": "stage must be diagnostic, build, or simulation",
                        "preflight": preflight}
            try:
                incomplete = state.hook_state.setdefault("experiment_preflight_incomplete", [])
                if isinstance(incomplete, list):
                    incomplete.append({
                        "phase": "simulation",
                        "blocking_reasons": preflight.get("blocking_reasons"),
                        "reason": preflight.get("reason"),
                    })
            except Exception:
                pass
        input_delivery = audit_input_delivery_for_execution(
            state,
            input_package_artifact_id,
            input_package_bindings,
        )
        state.append_transcript("input_delivery_preflight", **input_delivery)
        if not input_delivery.get("passed"):
            record_execution_precondition_witness(
                state, runner, "formal input package unverified at simulation execution")
        execution_contract = audit_execution_contract(state, execution_params, stage="simulation", runner=runner)
        state.append_transcript("execution_contract_preflight", **execution_contract)
        if not execution_contract.get("passed"):
            record_prereg_deviation(state, runner, {
                "kind": "execution_params_deviate_from_frozen_prereg",
                "blocking_reasons": execution_contract.get("blocking_reasons"),
                "mismatched_parameters": execution_contract.get("mismatched_parameters"),
                "missing_expected_parameters": execution_contract.get("missing_expected_parameters"),
                "unexpected_execution_parameters": execution_contract.get("unexpected_execution_parameters"),
            })
    except Exception:
        # 审计自身异常不阻断执行（与本文件预检降级惯例一致）；留痕。
        try:
            state.append_transcript("simulation_contract_audit_error", runner=runner)
        except Exception:
            pass
    return None


def _bash_static_validity_block(
    state: Any,
    cmd: str,
    *,
    expected_duration_s: int | None,
    check_after_seconds: int | None,
) -> dict[str, Any] | None:
    """纯静态/身份检查；不解析 cwd、不创建目录、不启动进程。"""
    from shared.lib import dangerous_commands as danger

    boundary = danger.match_boundary_violation(cmd, mode="shell")
    if boundary:
        try:
            state.append_transcript(
                "boundary_write_blocked",
                tool="safe_run_bash",
                cmd_preview=cmd[:200],
                category=boundary,
            )
        except Exception:
            pass
        return {
            "status": "error",
            "error": danger.BOUNDARY_DENY_MESSAGE.format(category=boundary),
        }

    bash_decision = _te.classify_bash_execution(cmd)
    analyzer_reason = (
        _te.bash_analyzer_unavailable_reason()
        or bash_decision.analysis.analyzer_unavailable
    )
    if analyzer_reason is not None:
        try:
            state.append_transcript(
                "bash_semantic_analyzer_unavailable",
                tool="safe_run_bash",
                reason=analyzer_reason,
                cmd_preview=cmd[:200],
            )
        except Exception:
            pass
        return {
            "status": "error",
            "error": (
                "Experiment Bash 语义分析器不可用，命令未执行："
                f"{analyzer_reason}。这是框架运行时依赖/部署配置缺失；"
                "experiment 不得安装或修改全局 Python 环境。请如实 report_blocker，"
                "由 framework owner 在部署依赖中提供 tree-sitter 与 "
                "tree-sitter-bash 后重试。"
            ),
            "blocker": {
                "kind": "bash_semantic_analyzer_unavailable",
                "reason": analyzer_reason,
                "suggested_owner": "framework",
                "node_action": "report_blocker_do_not_modify_global_environment",
            },
        }

    embedded_launcher = _embedded_shell_launcher(bash_decision.analysis)
    if embedded_launcher is not None:
        program, option = embedded_launcher
        try:
            state.append_transcript(
                "embedded_process_launcher_blocked",
                tool="safe_run_bash",
                program=program,
                option=option,
                cmd_preview=cmd[:200],
            )
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "embedded_process_launcher",
            "error": (
                "⛔ 命令未执行：检测到会在查询工具内部再次派生任意进程的"
                f"选项（{program} {option}）。请移除该选项；需要执行独立程序时"
                "必须把真实入口写成可审查的路线步骤并进入受管执行器。"
            ),
            "blocker": {
                "kind": "embedded_process_launcher",
                "program": program,
                "option": option,
            },
        }

    # 判决拆除·第三波（sb:3631 降格 / sb:3650 删，2026-09-02）：
    #   - nohup/setsid/行尾 &/disown/裸 sbatch：不在这里新增预测性拒绝；原生后端
    #     能否在 shell 退出后回收整棵进程树取决于主机能力（无 systemd scope 时
    #     后台后代可能存活）。照跑但留下 host-dependent witness，并指向 submit_job。
    #   - 裸 srun：Attempt 沙盒 network=none，srun 连不上 slurmctld，现实自己拒绝；
    #     纯预测墙整块删（含 te.uses_srun）。
    #
    # ssh / pdsh / kubectl exec / docker / systemd-run / tmux / screen / at 这类
    # 外部控制面把执行搬到受管边界之外，本 run 的容器收不了它、账本也看不见它 ——
    # 那不是"预测会孤儿化"，是确定的边界逃逸（A 类）。这一支保留拦截。
    if (bash_decision.analysis.external_control
            == _bash_semantics.EXTERNAL_CONTROL_EFFECT):
        try:
            state.append_transcript(
                "external_control_launch_blocked",
                tool="safe_run_bash",
                cmd_preview=cmd[:200],
            )
        except Exception:
            pass
        return {
            "status": "error",
            "error": (
                "⛔ 不允许通过 ssh、pdsh、kubectl exec、docker、systemd-run、"
                "tmux/screen、at/batch 等外部控制面把执行搬到 experiment 的受管"
                "边界之外：本 run 的容器与账本都管不到那边的进程。远端/受管作业"
                "请使用 submit_job（先 dry_run，再真实提交），提交后用 job_status "
                "确认一次，再由 external-job handoff 交接。"
            ),
            "blocker": {"kind": "unmanaged_background_launch"},
        }
    if bash_decision.unverifiable_execution:
        uncertainty = bash_decision.uncertainty_kind or "unknown"
        blocker_kind = (
            "bash_parse_error"
            if uncertainty == "parse_error"
            else "unverifiable_dynamic_execution"
        )
        detail = (
            "命令不是可可靠解析的 Bash 语法"
            if uncertainty == "parse_error"
            else "可执行命令头或 shell 代码来自运行时数据，无法静态确定"
        )
        try:
            state.append_transcript(
                "bash_execution_unverifiable",
                tool="safe_run_bash",
                uncertainty_kind=uncertainty,
                cmd_preview=cmd[:200],
            )
        except Exception:
            pass
        return {
            "status": "error",
            "reason": blocker_kind,
            "error": (
                "⛔ Bash 命令未执行：" + detail + "。"
                "这不表示检测到了后台任务或 srun。请把命令头改为静态字面量、"
                "内联可审查的静态 shell payload，或使用对应的结构化专用工具。"
            ),
            "blocker": {
                "kind": blocker_kind,
                "uncertainty_kind": uncertainty,
            },
        }

    # 判决拆除·第三波（te:247 降格，2026-09-02）：「声明 expected_duration_s>=600
    # 就必须走 submit_job」是路线仪式，不是不可逆损害。长命令照跑（受 timeout 与
    # 沙盒 walltime 约束），由调用处见证 managed_submission_recommended。
    # managed_job_kill_block 的定义在 ffc6c245（fail-closed 资源隔离）随调用
    # 一起下线；22630d25 重构时把调用搬进本函数却没恢复定义，import 永远失败、
    # 被 except 吞掉 —— 这道「禁止裸 kill 受管作业」的门自那以后一行都没执行过。
    # 2026-09-03 owner 裁定按判决拆除方向清理死代码而非复活墙：裸 kill 的残余
    # 风险由 ffc6c245 的进程树归属/cgroup 清理兜底，生命周期修正走 cancel_job。
    return None


async def _safe_run_bash(state: Any, cmd: str, timeout: int = 600,
                         cwd: str | None = None, **kw: Any) -> dict:
    """boundary deny → scope guard → 高危门 → build gate → 自执行落盘。

    顺序原则：确定性节点边界检查（scope_guard）先于交互式框架门——
    对基线/框架状态等不可授权边界直接拒绝；对可由 owner 承担的环境路径，
    scope_guard 先发路径级一次性 pause，批准后再进入高危门，避免重复授权。

    bypass 模式（平台级产品语义）：
    `--bypass-permissions` 只让精确、可人工授权的环境 scope 与确认门让路，
    并保留 `*_bypassed` 审计。源码基线、框架状态、未解析目标、路径角色、
    route/scope 身份和后台生命周期都是 validity 事实，任何模式都不能绕过。
    非阻断增强（build_env source、完整日志落盘、repro 采集）照常执行。"""
    timeout = _coerce_timeout(timeout, 600)
    from shared.lib import dangerous_commands as _danger
    _bypass = _danger.bypass_enabled()
    # 判决拆除·第三波（sb:3584 → schema，2026-09-02）：stage 词表由 schema enum
    # （preflight.EXECUTION_STAGES 同源）在派发口核，这里不再手写。
    execution_stage = str(kw.get("stage") or "diagnostic").strip().lower()
    if execution_stage not in set(EXECUTION_STAGES):
        execution_stage = "diagnostic"
    execution_params = kw.get("execution_params")
    bash_write_targets: list[str] = []
    explicit_cwd = cwd is not None and bool(str(cwd).strip())
    if cmd and cmd.strip():
        static_block = _bash_static_validity_block(
            state,
            cmd,
            expected_duration_s=kw.get("expected_duration_s"),
            check_after_seconds=kw.get("check_after_seconds"),
        )
        if static_block is not None:
            return static_block
    route_action = _bash_route_action(
        cmd or "",
        execution_stage=execution_stage,
        route_step_id=kw.get("route_step_id"),
        state=state,
    )
    route_decision = _resolve_route_context(state, route_action)
    pre_materialization_block = _enforce_route_context(
        state, route_action, route_decision, phase="pre_materialization")
    if pre_materialization_block is not None:
        return pre_materialization_block
    workdir_resolution = "explicit" if explicit_cwd else "action_default"
    if explicit_cwd:
        # 只物化 resolver 唯一选中的 framework-owned 惰性根，并且调用方 cwd
        # 必须与它精确相等。外部/显式角色、歧义角色、拼错或嵌套路径仍不创建。
        route_choice = _route_workdir_choice(
            state, route_decision, create=False
        )
        if route_choice.get("status") == "resolved":
            try:
                exact_canonical_root = (
                    Path(str(cwd)).expanduser().resolve(strict=False)
                    == Path(str(route_choice["path"])).expanduser().resolve(
                        strict=False
                    )
                )
            except (OSError, RuntimeError, ValueError):
                exact_canonical_root = False
            if exact_canonical_root:
                _route_workdir_choice(state, route_decision, create=True)
    else:
        route_choice = _route_workdir_choice(
            state, route_decision, create=True
        )
        try:
            if route_choice.get("status") == "resolved":
                cwd = str(route_choice["path"])
                workdir_resolution = "resolved"
            elif _is_major_build(cmd or ""):
                cwd = str(default_stage_workdir(state, "toolchain_build", create=True))
            elif _is_major_run(cmd or ""):
                cwd = str(experiment_output_dir(state, "runtime", create=True))
            else:
                cwd = str(experiment_output_dir(state, "runtime", create=True))
        except Exception as exc:
            role = "build_root" if _is_major_build(cmd or "") else "run_root"
            return _default_workdir_failure(role, exc)

    if cmd and cmd.strip():
        route_action["workdir_roles"] = _observed_workdir_roles(state, cwd)
        route_action["payload_digest"] = _bash_payload_digest(
            cmd, cwd, timeout=timeout, execution_params=execution_params, state=state,
            read_only=route_action.get("read_only") is True,
        )
        route_decision = dict(route_decision)
        route_decision["workdir_role_observed"] = (
            route_decision.get("declared_workdir_role")
            in route_action["workdir_roles"]
        )
        route_decision["workdir_resolution_status"] = workdir_resolution
        route_decision["resolved_workdir"] = str(cwd)
        _record_route_context(state, route_action, route_decision)
    formal_scientific_action = _is_formal_scientific_action(
        state,
        execution_stage=execution_stage,
        route_decision=route_decision,
        mechanical_major_run=_is_major_run(cmd or ""),
    )
    # 本次执行的如实见证（降格后的前置检查照跑、不拒、结果带标记）。
    execution_witness: dict[str, Any] = {}

    def _bypass_note(event: str) -> None:
        try:
            state.append_transcript(event, cmd_preview=cmd[:200])
        except Exception:
            pass

    if cmd and cmd.strip():
        # 权威工作目录不可用 → 事前拒绝。放在这里（确定性检查之后、所有
        # pause/HITL 门之前）：让人去批准一条注定跑不起来的命令是纯浪费，
        # 而它与权限无关，故同样不受 bypass 影响。
        workdir_error = resolve_required_workdir(state, cwd)[1]
        if (workdir_error is not None
                and workdir_error.get("reason") == "path_capability_required"):
            approved, approval = _scope_guard_authorization(
                state, kind="bash", cmd=cmd, cwd=cwd, target=str(cwd),
                scope="path_capability_required",
                reason=str(workdir_error.get("error") or ""),
                op="cwd", preview=cmd,
            )
            if approved:
                workdir_error = resolve_required_workdir(state, cwd)[1]
            elif approval is not None:
                return approval
        if workdir_error is not None:
            try:
                state.append_transcript(
                    "required_workdir_unavailable", tool="safe_run_bash",
                    cwd=str(cwd), cmd_preview=cmd[:200])
            except Exception:
                pass
            return workdir_error
        try:
            path_effect_block = _bash_path_effects_guard(
                state, cmd, cwd=cwd)
        except Exception as exc:
            return {
                "status": "error",
                "reason": "bash_path_analysis_failed",
                "error": "Shell 写目标路径分析异常，命令未启动。",
                "blocker": {
                    "kind": "bash_path_analysis_failed",
                    "reason": type(exc).__name__,
                    "suggested_owner": "framework",
                },
            }
        if path_effect_block is not None:
            return path_effect_block
        # Reuse the same tree-sitter projection that just passed the path gate.
        # These are capabilities already validated/confirmed above, never paths
        # inferred from payload text inside the executor.
        bash_write_targets = [
            target for op, target in _analyze_shell_path_effects(cmd, cwd)
            if op != "remote_visible_path"
            and target not in {UNRESOLVED, REMOTE_SCRATCH}
        ]
        activity_block = _activity_path_role_guard(
            state,
            cmd,
            cwd=cwd,
            route_decision=route_decision,
        )
        if activity_block is not None:
            return activity_block
        if formal_scientific_action:
            execution_contract_block = _simulation_contract_block(
                state,
                execution_params,
                runner="safe_run_bash",
                input_package_artifact_id=kw.get("input_package_artifact_id"),
                input_package_bindings=kw.get("input_package_bindings"),
            )
            if execution_contract_block is not None:
                return execution_contract_block
        # 判决拆除·第三波（sb:1855/1868 降格，2026-09-02）：可执行目标存在性/
        # 执行位不再事前拦本地 bash；rc=127/126 之后由 _exec_and_log 附
        # `exec_diagnosis`（见 _exec_target_diagnosis）。提交链预检不受影响：
        # 作业一旦进调度器就不可逆，那条路上事前判定仍然成立。
        # 判决拆除·第三波（sb:3631 降格 / sb:3650 删 / te:247 降格，2026-09-02）：
        #   - nohup/setsid/行尾 &/disown/裸 sbatch：不新增预测性拒绝；后台后代是否
        #     被回收取决于主机的进程监管能力。照跑并留下 host-dependent witness，
        #     需要跨调用存活的工作明确指向 submit_job。
        #   - 裸 srun：Attempt 沙盒 network=none，srun 连不上 slurmctld，现实自己
        #     拒绝；纯预测墙整块删（含 te.uses_srun）。
        #   - expected_duration_s ≥ 阈值：路线仪式；照跑（受 timeout 与沙盒
        #     walltime 约束），见证 managed_submission_recommended。
        if _te.looks_backgrounded(cmd):
            execution_witness["background_launch_dies_with_shell"] = {
                "hint": (
                    "命令含 nohup/setsid/行尾 &/disown 或裸 sbatch/qsub：后台进程是否活过本次调用取决于主机；"
                    "无 systemd scope 时可能存活并继续写可写根，不能把本次调用结束当作进程树已回收的证据。"
                    "需要跨调用存活的工作请用 submit_job（先 dry_run 再真实提交），提交后 "
                    "job_status 确认一次，再由 external-job handoff 交接。"),
            }
            try:
                state.append_transcript(
                    "background_launch_dies_with_shell",
                    tool="safe_run_bash", cmd_preview=cmd[:200])
            except Exception:
                pass
        try:
            declared_duration = int(kw.get("expected_duration_s") or 0)
        except (TypeError, ValueError):
            declared_duration = 0
        if declared_duration >= _te._SYNC_LONG_THRESHOLD_S:
            execution_witness["managed_submission_recommended"] = {
                "expected_duration_s": declared_duration,
                "threshold_s": _te._SYNC_LONG_THRESHOLD_S,
                "hint": (
                    "已声明为长任务：前台 safe_run_bash 只受 timeout 与沙盒 walltime "
                    "约束，到点即被砍断且无持久身份；建议改用 submit_job 受管提交。"),
            }
            try:
                state.append_transcript(
                    "managed_submission_recommended", tool="safe_run_bash",
                    cmd_preview=cmd[:200], expected_duration_s=declared_duration,
                    threshold_s=_te._SYNC_LONG_THRESHOLD_S)
            except Exception:
                pass
        # runtime ABI 预检：内部只对路径形式的 ELF 目标做 ldd（裸命令名/脚本/
        # 系统目录二进制跳过），探查命令近零开销；裸启动 HPC 二进制也覆盖
        # （2026-07-13 混 MPI 挂死正是不带 mpirun 的裸启动）
        try:
            abi_block = await _runtime_abi_preflight_bash(state, cmd, cwd=cwd)
        except Exception as exc:
            return _framework_guard_failure(
                "runtime_abi_preflight", exc,
                reason="runtime_abi_preflight_unavailable",
                source="runtime_abi_preflight",
            )
        if abi_block is not None:
            try:
                state.append_transcript("runtime_preflight_blocked",
                                        cmd_preview=cmd[:200])
            except Exception:
                pass
            if _is_non_bypassable_framework_block(abi_block):
                return abi_block
            if _bypass:
                _bypass_note("runtime_preflight_bypassed")
            else:
                return abi_block
        # 超时熔断：首次同步超时后，后续同步重试必须切换到受管提交。
        # 排在高危门之前：要拒的命令别先弹 pause 白耗一次用户批准。
        timeout_block = _te.check_sync_block(state, tool="safe_run_bash", cmd=cmd)
        if timeout_block is not None:
            # A previously timed-out foreground job has no durable identity.
            # This is a route-validity block, never a bypass permission.
            return timeout_block
        route_block = _enforce_route_context(
            state, route_action, route_decision, phase="pre_spawn")
        if route_block is not None:
            return route_block
        gate_block = _unified_highrisk_gate(
            state, cmd, mode="shell", tool="safe_run_bash", kind="bash",
            cwd=cwd)
        if gate_block is not None:
            # bypass 时统一门内部已直跑留痕、不会返回 block；此分支仅普通模式可达
            return gate_block
    # canonical 路线把 configure/build 作为高后果步骤时，必须先把用户给出的
    # 资源预算提交为现有 build_resource_plan。它是有效性/宿主保护门，不受 bypass
    # 影响，也必须发生在 route_step_bound 和任何 spawn 之前。
    if (
        route_decision.get("authoritative")
        and (
            _is_provision_or_configure_action(cmd)
            or _is_major_build(cmd)
            or (
                route_decision.get("declared_workdir_role") == "build_root"
                and "workspace_write" in set(
                    route_decision.get("effective_effects") or []
                )
            )
        )
    ):
        resource_plan_block = build_resource_plan_block(state, cmd)
        if resource_plan_block is not None:
            try:
                state.append_transcript(
                    "build_resource_plan_blocked",
                    reason=resource_plan_block.get("reason"),
                    cmd_preview=cmd[:200],
                )
            except Exception:
                pass
            return resource_plan_block

    # build gate（第3步）：首次主要 build 前客观侦察 gate（事前拦）
    try:
        gate = _build_gate(
            state, cmd, cwd=cwd, route_decision=route_decision)
    except Exception as exc:
        return _framework_guard_failure(
            "build_gate", exc,
            reason="build_gate_unavailable",
            source="build_gate",
        )
    if gate is not None:
        if _is_non_bypassable_framework_block(gate):
            return gate
        if _bypass:
            _bypass_note("build_gate_bypassed")
        else:
            return gate

    if bench_enabled("provision_first") and _is_major_build_or_run(cmd):
        try:
            from tools.env_provision import ensure_env_script, wrap_command
            env_meta = ensure_env_script(state)
            wrapped = wrap_command(cmd, env_meta.get("env_path"))
            if wrapped != cmd:
                try:
                    state.append_transcript("build_env_wrapped",
                                            env_path=env_meta.get("env_path"),
                                            env_sha256=env_meta.get("sha256"),
                                            cmd_preview=cmd[:200])
                except Exception:
                    pass
                cmd = wrapped
        except Exception:
            pass

    try:
        bash_sandbox_roots = _sandbox_roots_for_payload(
            state, str(cwd), sandbox_profile="bash",
            authorized_targets=bash_write_targets,
        )
    except Exception as exc:
        return _sandbox_roots_error(exc)

    # 每条命令都进入同一个强制 RunAttempt 边界；模型可选择的
    # resource_profile 只是冻结 ceiling 内的初始资源档位，不能切换执行器。
    # route attempt 仍包住真实容器调用，确保 outcome 与路线账本一致。
    route_binding, route_binding_error = _begin_route_attempt(
        state, route_decision, tool="safe_run_bash", action=route_action)
    if route_binding_error is not None:
        return route_binding_error
    exec_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "cwd": cwd,
        "sandbox_profile": "bash",
        "sandbox_write_targets": bash_write_targets,
        "sandbox_roots": bash_sandbox_roots,
        "execution_action": route_action,
        "execution_decision": route_decision,
        "execution_route_binding": route_binding,
    }
    if kw.get("resource_profile") is not None:
        exec_kwargs["resource_profile"] = kw["resource_profile"]
    try:
        result = await _exec_and_log(state, cmd, **exec_kwargs)
    except BaseException as exc:
        _finish_route_attempt(state, route_binding, error=exc)
        raise
    route_result_block = _finish_route_attempt(
        state, route_binding, result=_physical_execution_outcome(result))
        # 转发分支不经过 _exec_and_log，cd 加固要在这里自己做一次。
        # 转发分支：builtin _run_bash 内部还有一道框架门会对 pipefail_cmd 再判。
        # 统一门已放行的命中命令需预标记确认，防止同一命令被二次 pause。
    if execution_witness and isinstance(result, dict):
        # 降格后的前置检查结论随结果一起返回（记录带整个命题，含不利的那半）。
        result["execution_witness"] = execution_witness
    try:
        _record_mpi_actual_params(state, cmd)
    except Exception:
        pass
    if (formal_scientific_action and isinstance(execution_params, dict)
            and result.get("status") == "success"):
        try:
            try:
                from .run_contract import record_actual_run_params
            except ImportError:
                from tools.run_contract import record_actual_run_params
            record_actual_run_params(state, "safe_run_bash", execution_params)
        except Exception:
            pass
    try:
        _record_source_worktree_diffs(state, cmd)
    except Exception as exc:
        try:
            state.append_transcript(
                "source_worktree_diff_record_failed",
                error=f"{type(exc).__name__}: {exc}",
                cmd_preview=cmd[:200])
        except Exception:
            pass
    if route_result_block is not None:
        return route_result_block
    return result


# 注册覆盖：复用框架原始 ToolDefinition（schema / description / risk_level 完全一致），
# 只把 executor 换成安全包装。需在框架 builtin 注册之后 import 才能覆盖生效
# （由 tools/__init__.py 的 import 时机保证）。
# v0.11 迁移（tool_registry 同名覆盖 deadline=2026-08-01）：不再覆盖全局
# run_bash，改注册独立名 safe_run_bash；experiment harness.yaml 白名单只放
# safe 版，builtin run_bash 恢复全局语义。"LLM 逃不掉安全层"的保证由节点
# 白名单承担（旧名不在白名单，调了就是 tool not found）。
import dataclasses as _dc

_INPUT_PACKAGE_BINDINGS_SCHEMA = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": (
        "Complete spec_id -> verified dataset/experiment_fallback_inputs artifact id "
        "mapping when this simulation consumes more than one formal input package."
    ),
}

_orig_def = _REGISTRY.tools.get("run_bash")
if _orig_def is not None:
    _safe_schema = dict(_orig_def.parameters_schema or {})
    _safe_props = dict(_safe_schema.get("properties") or {})
    # 契约归 schema（判决拆除·第三波，2026-09-02）：cmd 非空在派发口核。
    _safe_props["cmd"] = {**dict(_safe_props.get("cmd") or {"type": "string"}), "minLength": 1}
    _safe_props["expected_duration_s"] = {
        "type": "integer", "minimum": 1,
        "description": (
            "Known expected wall duration. >=600 still runs here (bounded by timeout and the "
            "sandbox walltime) but is witnessed as managed_submission_recommended; prefer submit_job."),
    }
    _safe_props["cwd"] = {
        "type": "string",
        "description": (
            "Authoritative working directory for this command. Declare the run-local "
            "directory here and use relative paths inside cmd; do not express the working "
            "directory with a `cd` in the command text. Must already exist — a missing "
            "directory is rejected before the shell starts, so create it in a separate "
            "mkdir-only call first."),
    }
    # 运行时仍接受旧 caller 传入的 stage，但不再向 LLM 暴露；它不能参与授权、
    # 工作目录、科学身份或资源强度判断。词表唯一真相源仍是
    # preflight.EXECUTION_STAGES（判决拆除·第三波「一题一答」），只是这里
    # 不把它作为模型可填参数登记 —— 认不出的取值按兼容默认归一并留痕，不拒。
    _safe_props.pop("stage", None)
    _safe_props["execution_params"] = {"type": "object", "description": "Must exactly match frozen prereg scientific parameters when a formal input package is consumed."}
    _safe_props["route_step_id"] = {
        "type": "string",
        "description": (
            "Identifier of the frozen execution-route step this action fulfils. "
            "Provide it when route matching is ambiguous or the route requires explicit binding."
        ),
    }
    _safe_props["input_package_artifact_id"] = {"type": "string", "description": "Verified dataset or experiment_fallback_inputs artifact consumed by this simulation."}
    _safe_props["input_package_bindings"] = _INPUT_PACKAGE_BINDINGS_SCHEMA
    _safe_schema["properties"] = _safe_props
    register_tool(_dc.replace(
        _orig_def,
        name="safe_run_bash",
        description=(
            "执行有界、同步的 shell 诊断或只读探针。timeout 是该诊断调用的显式"
            "硬边界，到期会终止整棵进程树；真正的构建、simulation 与启动器必须从启动时"
            "使用 submit_job(scheduler=local/SLURM/PBS) —— 它们需要活过本次调用的作业"
            "身份，不要把 expected_duration 当作 safe_run_bash 的续时依据。"
            "有界的本地动作留在本工具即可。是否需要路线以本次调用返回的机械判定和 "
            "next_action 为准，不要靠命令名称清单猜测。当前冻结路线已有与本次命令入口"
            "对应的就绪步骤时，必须按解析结果绑定该步骤；解析结果要求显式绑定时传 "
            "route_step_id。有界且被机械判定为 low_risk_effectful 的 run-local 写入"
            "通常无需新建路线。"
        ),
        parameters_schema=_safe_schema,
    ), _safe_run_bash)
else:  # pragma: no cover —— 框架未先注册时的兜底（理论不该发生）
    from core.tool_registry import ToolDefinition
    register_tool(
        ToolDefinition(
            name="safe_run_bash",
            description=(
                "执行有界同步 shell 诊断；timeout 到期会终止进程树。真正的构建、"
                "simulation 与启动器使用 submit_job。"
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout": {"type": "integer", "default": 600},
                    "cwd": {"type": "string"},
                    "resource_profile": {
                        "type": "string",
                        "enum": ["small", "standard", "large", "xlarge"],
                        "default": "standard",
                    },
                    "expected_duration_s": {"type": "integer", "minimum": 1},
                    "route_step_id": {"type": "string"},
                    "execution_params": {"type": "object"},
                    "input_package_artifact_id": {"type": "string"},
                    "input_package_bindings": _INPUT_PACKAGE_BINDINGS_SCHEMA,
                },
                "required": ["cmd"],
            },
            risk_level="high",
        ),
        _safe_run_bash,
    )


# ─────────────────────────────────────────────────────────────────────────────
# write_file 覆盖 —— 同一套作用域 guard
# ─────────────────────────────────────────────────────────────────────────────

async def _safe_write_file(state: Any, path: str, content: str,
                           create_dirs: bool = True, **kw: Any) -> dict:
    base = str(getattr(state, "root", None) or os.getcwd())
    target = _norm_path(path, base)
    # write_file 不属于可执行路线步骤，但它是持久副作用：必须在任何
    # create_dirs/原始 writer 之前冻结 run scope。低风险文件写不生成
    # route attempt，也不被迫声明完整路线。
    route_action = {
        "tool": "safe_write_file",
        "program": "write_file",
        "read_only": False,
        "observed_effects": ["workspace_write"],
        "workdir_roles": _observed_workdir_roles(state, target),
        "dry_run": False,
    }
    route_decision = _resolve_route_context(state, route_action)
    scope_block = _enforce_route_context(
        state, route_action, route_decision, phase="pre_materialization")
    if scope_block is not None:
        return scope_block
    _record_route_context(state, route_action, route_decision)
    contract = validate_path_roles(state)
    if not contract["valid"]:
        scope, reason = (
            "invalid_path_role_contract", "; ".join(contract["errors"]))
    else:
        scope, reason = _classify_write_path(target, state)
    if scope == "safe":
        vc_error = _worktree_requires_version_control(target, state)
        if vc_error:
            scope, reason = "unversioned_source_worktree", vc_error
        elif any(role.role == "source_worktree_root"
                 for role in matching_path_roles(target, state)):
            confirmation = _source_write_confirmation(
                state, kind="write_file", text=f"write_file(path={path!r})",
                cwd=base, target=target, op="write_file",
                preview=f"write_file(path={path!r})")
            if confirmation is not None:
                return confirmation
    if scope != "safe":
        from shared.lib import dangerous_commands as _danger
        if not _danger.bypass_enabled():
            approved, approval = _scope_guard_authorization(
                state, kind="write_file",
                cmd=f"write_file(path={path!r})", cwd=base, target=target,
                scope=scope, reason=reason, op="write_file",
                preview=f"write_file(path={path!r})")
            if approved:
                scope = "safe"
            elif approval is not None:
                return approval
        if scope != "safe":
            block = _scope_guard_error(
                f"write_file(path={path!r})", scope, reason, target,
                state=state, kind="write_file")
            if not (_danger.bypass_enabled() and _scope_bypass_allowed(scope)):
                return block
            try:
                state.append_transcript(
                    "scope_guard_write_file_bypassed", target=target,
                    scope=scope, reason=reason)
            except Exception:
                pass
    # P0a v2 B2：写入必须入账。复审实测 pending run 里 safe_write_file 真写盘而
    # census 看不见，配一条 tar 路线步 + 假 solver 就能让 ROC(build) 绿色假成功。
    # 这里过一次兼容 lane 门（裁决 (a)：support tool 放行），再以 receipted 身份
    # 走三阶段 census，write_target 随 admission 落账供 ROC(build) 对照。
    try:
        from .execution_action_census import (
            begin_execution_action, pending_operation_action_block,
            settle_execution_action,
        )
    except ImportError:
        from tools.execution_action_census import (
            begin_execution_action, pending_operation_action_block,
            settle_execution_action,
        )
    census_action = {**route_action, "write_target": target}
    census_decision = {**route_decision, "tool": "safe_write_file"}
    pending_block = pending_operation_action_block(
        state, census_action, census_decision)
    if pending_block is not None:
        return pending_block
    token = begin_execution_action(state, census_action, census_decision)
    if token.get("status") != "success":
        return token
    result: dict | None = None
    error: BaseException | None = None
    settled: dict | None = None
    try:
        result = await _orig_write_file(
            state, path=path, content=content, create_dirs=create_dirs, **kw)
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            settled = settle_execution_action(
                state, token, payload_spawned=False, job_submitted=False,
                proof_source="safe_write_file",
                result=result if error is None else None, error=error)
        except Exception as exc:  # 记账本身抛错也不能伪装成普通 success
            settled = {"passed": False, "error_code": "settle_raised",
                       "error": f"{type(exc).__name__}: {exc}"}
    try:
        _record_source_worktree_diffs(
            state, f"safe_write_file {target}")
    except Exception:
        pass
    if not (isinstance(settled, dict) and settled.get("passed") is True):
        # P0a v3（Codex 复审 20 号）：文件已经写了、账没记上——不能返回普通 success。
        # 副作用已发生，盲重跑会重复写；返回结构化 uncertain，并给出结算出口。
        return {
            "status": "error",
            "error_code": "write_committed_but_census_settlement_uncertain",
            "side_effect_committed": True,
            "written_path": target,
            "payload_must_not_rerun": True,
            "do_not_retry_payload": True,
            "census_action_id": token.get("action_id"),
            "settlement": settled,
            "write_result": result,
            "error": (
                f"文件已写入 {target}，但 action census 未能结算这次写入；"
                "不要重跑写入，也不要把它当成功。运行时对账不是模型工具：按 "
                "model_next_action 报 blocker 结束本 run。"
            ),
            # P0a v4（Codex 复审 23 号）：出口必须是真实存在的入口。census 自己的
            # 持久化失败已经带了 runtime-owned 的 next_action（retry_missing_census_
            # phase_only，model_callable=False）——原样透传；settle 抛异常时给同一形状。
            "next_action": (
                dict(settled["next_action"])
                if isinstance(settled, dict)
                and isinstance(settled.get("next_action"), dict)
                else {
                    "owner": "experiment_runtime",
                    "action": "retry_missing_census_phase_only",
                    "phase": "terminal",
                    "model_callable": False,
                    "census_action_id": token.get("action_id"),
                }
            ),
            "model_next_action": {
                "action": "report_blocker_and_end_current_run",
                "reason": "runtime reconciliation is not a model tool",
            },
        }
    return result


_orig_wf_def = _REGISTRY.tools.get("write_file")
if _orig_wf_def is not None:
    register_tool(
        _dc.replace(
            _orig_wf_def,
            name="safe_write_file",
            description=(
                "把完整内容写入文件（创建新的或整体覆盖现有的）。路径相对 state.root "
                "解释；可传绝对路径。create_dirs=True（默认）会自动建父目录。⚠️ 对已存在"
                "的文件：本次 run 必须先 read_file 一次，否则 write 被拒。（新建文件不要求 "
                "read。）这是防止盲写覆盖的安全约束；局部修改也应先读取，再提交包含完整"
                "目标内容的写入。"
            ),
        ),
        _safe_write_file,
    )


# ─────────────────────────────────────────────────────────────────────────────
# execute_python 覆盖 —— Python 源码里的等价高危操作
# ─────────────────────────────────────────────────────────────────────────────

_PY_PATH_WRITE_METHODS = frozenset({
    "write_text", "write_bytes", "touch", "mkdir", "unlink",
})
_PY_PATH_MOVE_METHODS = frozenset({"rename", "replace"})
_PY_OS_WRITE_CALLS = frozenset({
    "remove", "unlink", "rename", "replace", "symlink", "mkdir", "makedirs",
})
_PY_SHUTIL_WRITE_CALLS = frozenset({
    "move", "copy", "copy2", "copyfile", "copytree", "rmtree",
})


def _python_dotted_name(node: ast.AST) -> str | None:
    """Return a syntactic dotted name without guessing aliases or values."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _python_dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _python_archive_module_root(node: ast.AST) -> str | None:
    """Return a direct stdlib archive-module root through attribute/call chains.

    This deliberately does not follow imported aliases, assignments, getattr,
    subscripts, or other value flow.  The projection is only a lower bound:
    unrecognised code retains the existing workspace-write policy.
    """
    current = node
    while True:
        if isinstance(current, ast.Name):
            return current.id if current.id in {"tarfile", "zipfile"} else None
        if isinstance(current, ast.Attribute):
            current = current.value
            continue
        if isinstance(current, ast.Call):
            current = current.func
            continue
        return None


class PythonEffectProjection(NamedTuple):
    """Positive, syntactic lower bounds derived from one parsed source tree.

    Every public field means "at least this effect was recognised".  Absence
    never means read-only, safe, authorised, or effect-free: the runtime
    capability boundary and the existing default ``workspace_write`` policy
    remain authoritative.  ``syntax_tree`` is a non-policy carrier retained
    only so recovery and preview diagnostics can reuse the same parse.
    """

    process_launches: tuple[str, ...]
    direct_archive_unpack: bool
    literal_write_targets: tuple[str, ...]
    has_dynamic_write_target: bool
    syntax_tree: ast.AST | None

    def direct_write_paths(self, cwd: str | None) -> tuple[list[str], bool]:
        """Resolve recognised literal operands against the final workdir."""
        base = str(cwd) if cwd else os.getcwd()
        paths: list[str] = []
        for value in self.literal_write_targets:
            resolved = value if os.path.isabs(value) else os.path.join(base, value)
            normalized = os.path.abspath(
                os.path.expanduser(os.path.expandvars(resolved)))
            if normalized not in paths:
                paths.append(normalized)
        return paths, self.has_dynamic_write_target


def _python_has_direct_archive_unpack_from_tree(tree: ast.AST) -> bool:
    """Recognise only canonical, directly named stdlib archive extraction."""

    direct_imports = {
        item.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for item in node.names
        if item.asname is None and item.name in {"shutil", "tarfile", "zipfile"}
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _python_dotted_name(node.func)
        if dotted == "shutil.unpack_archive" and "shutil" in direct_imports:
            return True
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"extractall", "extract"}
            and _python_archive_module_root(node.func.value) in direct_imports
        ):
            return True
    return False


def _python_has_direct_archive_unpack(code: str) -> bool:
    """Compatibility helper; the main executor supplies one shared projection."""
    try:
        return _project_python_effects(code).direct_archive_unpack
    except (TypeError, ValueError):
        return False


def _python_literal_path(node: ast.AST) -> str | None:
    """Return a literal path, including ``Path('literal')``; never evaluate code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and _python_dotted_name(node.func) in {
            "Path", "pathlib.Path"} and node.args:
        return _python_literal_path(node.args[0])
    return None


def _python_open_is_write(call: ast.Call, *, mode_index: int) -> bool:
    """Whether a builtin/Path ``open`` call can mutate its target."""
    mode: ast.AST | None = call.args[mode_index] if len(call.args) > mode_index else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
            break
    if mode is None:  # Python defaults to read-only mode="r".
        return False
    value = _python_literal_path(mode)
    # A dynamic mode cannot be proved read-only. Treat it as a direct write
    # attempt, but do not invent a filesystem target from unrelated text.
    return value is None or any(flag in value for flag in ("w", "a", "x", "+"))


def _python_direct_write_targets_from_tree(
    tree: ast.AST,
) -> tuple[tuple[str, ...], bool]:
    """Extract positive direct-write operands from an existing syntax tree.

    ``subprocess`` and ``os.system`` deliberately do not appear here: their
    argv/source text is not a filesystem-write declaration. They remain under
    the framework's high-risk shell-out gate. This separation prevents a
    read-only probe such as ``subprocess.run(['/usr/bin/python', '--version'])``
    from being misreported as a write to ``/usr``.

    The boolean reports a direct write with a non-literal target. It is only
    used to retain source-worktree confirmation when the effective target is
    the declared cwd; sandboxing remains the authoritative boundary for every
    dynamic target.
    """
    literal_targets: list[str] = []
    has_dynamic_target = False

    def add_target(node: ast.AST | None) -> None:
        nonlocal has_dynamic_target
        if node is None:
            has_dynamic_target = True
            return
        value = _python_literal_path(node)
        if value is None:
            has_dynamic_target = True
            return
        literal_targets.append(value)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _python_dotted_name(node.func)
        method = node.func.attr if isinstance(node.func, ast.Attribute) else None

        if dotted == "open":
            if _python_open_is_write(node, mode_index=1):
                add_target(node.args[0] if node.args else None)
            continue
        if method == "open" and _python_open_is_write(node, mode_index=0):
            add_target(node.func.value)
            continue
        if method in _PY_PATH_WRITE_METHODS:
            add_target(node.func.value)
            continue
        if method in _PY_PATH_MOVE_METHODS:
            add_target(node.func.value)
            add_target(node.args[0] if node.args else None)
            continue
        if dotted and dotted.rsplit(".", 1)[0] == "os" and method in _PY_OS_WRITE_CALLS:
            add_target(node.args[0] if node.args else None)
            if method in {"rename", "replace", "symlink"}:
                add_target(node.args[1] if len(node.args) > 1 else None)
            continue
        if dotted and dotted.rsplit(".", 1)[0] == "shutil" and method in _PY_SHUTIL_WRITE_CALLS:
            add_target(node.args[0] if node.args else None)
            if method != "rmtree":
                add_target(node.args[1] if len(node.args) > 1 else None)

    return tuple(literal_targets), has_dynamic_target


def _python_direct_write_paths(code: str, cwd: str | None) -> tuple[list[str], bool]:
    """Compatibility helper; resolve one independently built projection."""
    try:
        return _project_python_effects(code).direct_write_paths(cwd)
    except (AttributeError, TypeError, ValueError):
        return [], False


def _scope_guard_python(
    state: Any,
    code: str,
    cwd: str | None = None,
    *,
    effect_projection: PythonEffectProjection | None = None,
) -> dict | None:
    """Guard direct Python filesystem writes without treating argument text as a path write."""
    if not code:
        return None
    targets, has_dynamic_target = (
        effect_projection.direct_write_paths(cwd)
        if effect_projection is not None
        else _python_direct_write_paths(code, cwd)
    )
    contract = validate_path_roles(state)
    if not contract["valid"]:
        return _scope_guard_error(
            "execute_python", "invalid_path_role_contract",
            "; ".join(contract["errors"]), "<path_roles>",
            state=state, kind="python", preview=code)
    if not targets and not has_dynamic_target:
        return None
    for role in collect_path_roles(state):
        targets_in_worktree = any(_under(target, role.path) for target in targets)
        cwd_in_worktree = bool(cwd and _under(os.path.abspath(str(cwd)), role.path))
        if (role.role == "source_worktree_root" and targets_in_worktree
                and Path(role.path).exists()
                and not (Path(role.path) / ".git").exists()
                and "fix_source" not in code and "SourceFixer" not in code):
            return _scope_guard_error(
                "execute_python", "unversioned_source_worktree",
                "direct Python writes to source_worktree_root require a Git "
                "worktree; use diagnose.fix_source for an automatically "
                "recorded patch on non-Git source",
                role.path, state=state, kind="python", preview=code)
        if role.role == "source_worktree_root" and (
                targets_in_worktree or (has_dynamic_target and cwd_in_worktree)):
            confirmation = _source_write_confirmation(
                state, kind="python", text=code, cwd=cwd, target=role.path,
                op="python_write", preview=code)
            if confirmation is not None:
                return confirmation
    for target in targets:
        scope, reason = _classify_write_path(target, state)
        if scope == "safe":
            continue
        # safe_execute_python 不负责扩张文件系统能力。已有的
        # approved_write_root 会在上面的 safe 分支被复用；新的越界目录应先
        # 通过受管 Bash/write_file 工作流登记，不能在 Python 调用中先消费
        # 一个 mount profile 无法兑现的批准。
        return _scope_guard_error(
            "execute_python", scope, reason, target, state=state,
            kind="python", preview=code)
    return None


def _python_process_launches_from_tree(tree: ast.AST) -> tuple[str, ...]:
    """Find positive process-launch facts in an existing syntax tree."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"

    sensitive_modules = {
        "subprocess", "asyncio", "os", "pty", "multiprocessing",
        "concurrent.futures",
    }

    def literal_string(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = literal_string(node.left)
            right = literal_string(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    def sensitive_base(value: str) -> bool:
        return (
            value == "<dynamic-process-capability>"
            or any(
                value == module or value.startswith(module + ".")
                for module in sensitive_modules
            )
        )

    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call):
            call_name = dotted(node.func)
            if call_name in {
                "__import__", "builtins.__import__", "importlib.import_module",
            }:
                module_name = (
                    literal_string(node.args[0]) if node.args else None
                )
                return (
                    module_name
                    if module_name is not None
                    else "<dynamic-process-capability>"
                )
            if len(node.args) >= 2 and call_name == "getattr":
                base = dotted(node.args[0])
                attr = literal_string(node.args[1])
                if attr is not None:
                    return f"{base}.{attr}" if base else attr
                if sensitive_base(base):
                    return f"{base}.<dynamic>"
        return ""

    # 简单赋值别名是 LLM 常见写法；迭代几轮覆盖一到多跳别名。
    for _ in range(4):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value = dotted(node.value)
            if not value:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and aliases.get(target.id) != value:
                    aliases[target.id] = value
                    changed = True
        if not changed:
            break

    exact = {
        "subprocess.Popen", "subprocess.run", "subprocess.call",
        "subprocess.check_call", "subprocess.check_output",
        "subprocess.getoutput", "subprocess.getstatusoutput",
        "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
        "os.system", "os.popen", "os.fork", "os.forkpty",
        "os.execl", "os.execle", "os.execlp", "os.execlpe",
        "os.execv", "os.execve", "os.execvp", "os.execvpe",
        "pty.spawn",
        "os.posix_spawn", "os.posix_spawnp",
        "multiprocessing.Process", "multiprocessing.Pool",
        "concurrent.futures.ProcessPoolExecutor",
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted(node.func)
        if (name in exact or name.startswith("os.spawn")
                or name.endswith(".<dynamic>")
                or name.startswith("<dynamic-process-capability>")):
            found.add(name)
    return tuple(sorted(found))


def _project_python_effects(code: str) -> PythonEffectProjection:
    """Parse once and derive every additive Python effect projection.

    A missing fact is deliberately not a negative capability statement.  In
    particular, an empty projection never means that the payload is read-only
    or may receive a weaker sandbox policy.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Preserve the executor contract: syntax errors are reported by the
        # original Python runner, while policy remains workspace-write.
        return PythonEffectProjection((), False, (), False, None)
    literal_targets, has_dynamic_target = (
        _python_direct_write_targets_from_tree(tree)
    )
    return PythonEffectProjection(
        process_launches=_python_process_launches_from_tree(tree),
        direct_archive_unpack=_python_has_direct_archive_unpack_from_tree(tree),
        literal_write_targets=literal_targets,
        has_dynamic_write_target=has_dynamic_target,
        syntax_tree=tree,
    )


def _python_process_launches(code: str) -> list[str]:
    """Compatibility helper; the main executor reads the shared projection."""
    if not code or not code.strip():
        return []
    return list(_project_python_effects(code).process_launches)


def _with_no_child_process_audit(code: str) -> str:
    """审计钩子兜住派生进程，并保留 AF_UNIX 之外的网络拒绝。"""
    preamble = (
        "import socket as _hf_socket\n"
        "import sys as _hf_sys\n"
        # Windows 上 socket 没有 AF_UNIX —— 直接引用它会在钩子里抛 AttributeError，
        # 把"只放行 AF_UNIX"变成"每次 socket 都以无关报错炸"。getattr 回落到 None：
        # 于是 `family != None` 恒真 → Windows 上一律禁 socket（更紧，正确）。
        "_hf_af_unix = getattr(_hf_socket, \"AF_UNIX\", None)\n"
        "def _hf_reject_child_process(event, args, _blocked=frozenset({"
        "\"subprocess.Popen\", \"os.system\", \"os.exec\", "
        "\"os.posix_spawn\", \"os.fork\", \"os.forkpty\", "
        "})):\n"
        "    if event in _blocked:\n"
        "        raise PermissionError("
        "\"safe_execute_python forbids child-process creation: \" + event)\n"
        "    if event == \"socket.__new__\" and ("
        "len(args) < 2 or args[1] != _hf_af_unix):\n"
        "        raise PermissionError("
        "\"safe_execute_python only permits AF_UNIX sockets\")\n"
        "    if event in {\"socket.connect\", \"socket.bind\"} and ("
        "not args or getattr(args[0], \"family\", None) != _hf_af_unix):\n"
        "        raise PermissionError("
        "\"safe_execute_python only permits AF_UNIX sockets\")\n"
        "    if event == \"socket.getaddrinfo\":\n"
        "        raise PermissionError("
        "\"safe_execute_python forbids network name resolution\")\n"
        "_hf_sys.addaudithook(_hf_reject_child_process)\n"
    )
    return preamble + code


def _python_sqlite_reason_path_analysis(
    code: str,
    cwd: str | None,
    *,
    effect_projection: PythonEffectProjection | None = None,
) -> tuple[list[Path], bool]:
    """Return paths only when SQLite use is complete enough for reason attribution.

    This conservative AST pass protects the optional run-root reason label. It
    must never gate generic SQLite recovery guidance; runtime SQLite errors get
    that guidance even when this analysis is incomplete.

    SQLite often omits the database path from its error, so readonly-failure
    attribution cannot rely on stderr alone.  Track the module/connect
    capability through direct attributes, ``dbapi2``, ``getattr`` and simple
    name aliases.  Any structured alias, argument/return escape, dynamic
    lookup or shadowed binding makes the result incomplete instead of guessed.
    """
    if effect_projection is None:
        try:
            tree = ast.parse(code)
        except (SyntaxError, TypeError, ValueError):
            return [], False
    else:
        tree = effect_projection.syntax_tree
        if tree is None:
            return [], False

    module_capability = "sqlite-module"
    connect_capability = "sqlite-connect"
    aliases: dict[str, str] = {}
    complete = True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name not in {"sqlite3", "sqlite3.dbapi2"}:
                    continue
                aliases[item.asname or "sqlite3"] = module_capability
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "sqlite3", "sqlite3.dbapi2",
        }:
            for item in node.names:
                if item.name == "*":
                    complete = False
                    continue
                if item.name == "connect":
                    aliases[item.asname or item.name] = connect_capability
                elif item.name == "dbapi2":
                    aliases[item.asname or item.name] = module_capability

    def capability(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Name):
            return aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            base = capability(node.value)
            if base == module_capability and node.attr == "dbapi2":
                return module_capability
            if base == module_capability and node.attr == "connect":
                return connect_capability
            return None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and capability(node.args[0]) == module_capability
        ):
            attribute = _python_literal_path(node.args[1])
            if attribute == "dbapi2":
                return module_capability
            if attribute == "connect":
                return connect_capability
        return None

    # Simple aliases are common (``open_db = sqlite3.connect``).  Resolve
    # those to a fixed point.  Structured targets are deliberately not aliases:
    # their capability reference is caught as an escape by the audit below.
    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    for _iteration in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value_node = node.value
            value_capability = capability(value_node)
            if value_capability not in {module_capability, connect_capability}:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and aliases.get(target.id) != value_capability
                ):
                    aliases[target.id] = value_capability
                    changed = True
        if not changed:
            break

    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    def is_simple_alias_value(node: ast.AST, parent: ast.AST | None) -> bool:
        if isinstance(parent, ast.Assign) and parent.value is node:
            return bool(parent.targets) and all(
                isinstance(target, ast.Name) for target in parent.targets
            )
        return bool(
            isinstance(parent, ast.AnnAssign)
            and parent.value is node
            and isinstance(parent.target, ast.Name)
        )

    def module_reference_is_consumed(
        node: ast.AST,
        parent: ast.AST | None,
    ) -> bool:
        if (
            isinstance(parent, ast.Attribute)
            and parent.value is node
            and parent.attr in {"connect", "dbapi2"}
        ):
            return True
        if (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "getattr"
            and parent.args
            and parent.args[0] is node
            and len(parent.args) >= 2
            and _python_literal_path(parent.args[1]) in {"connect", "dbapi2"}
        ):
            return True
        return is_simple_alias_value(node, parent)

    # Every reference to either capability must have a locally understood
    # consumer.  Merely finding one well-formed call is insufficient if another
    # reference escapes into a tuple, mapping, callback, return value, etc.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            # Binding targets are audited for exact capability-preserving
            # assignment in the shadow/rebind pass below.
            continue
        node_capability = capability(node)
        if node_capability is None:
            continue
        parent = parents.get(node)
        if node_capability == module_capability:
            if not module_reference_is_consumed(node, parent):
                complete = False
        elif not (
            isinstance(parent, ast.Call) and parent.func is node
        ) and not is_simple_alias_value(node, parent):
            complete = False

    # Rebinding or lexical shadowing of a tracked name makes the simple alias
    # table non-authoritative.  Fail closed rather than attempting scope/value
    # flow in this recovery-only diagnostic.
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.arg in aliases:
            complete = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in aliases:
                complete = False
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id not in aliases:
                continue
            parent = parents.get(node)
            if isinstance(parent, (ast.Assign, ast.AnnAssign)):
                value_capability = capability(parent.value)
                if value_capability == aliases[node.id]:
                    continue
            complete = False

    paths: list[Path] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if capability(node.func) != connect_capability:
            continue
        database_node = node.args[0] if node.args else None
        if database_node is None:
            database_node = next(
                (item.value for item in node.keywords if item.arg == "database"),
                None,
            )
        value = _python_literal_path(database_node) if database_node else None
        if value is None:
            complete = False
            continue
        if value.startswith("file:"):
            value = unquote(urlsplit(value).path)
        if not value:
            complete = False
            continue
        if value == ":memory:":
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            if not cwd:
                continue
            path = Path(cwd) / path
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if path not in paths:
            paths.append(path)
    return paths, complete and bool(paths)


def _sqlite_readonly_recovery(
    sqlite_paths: Sequence[Path],
    scratch_tmp: str | Path | None,
    *,
    paths_complete: bool,
) -> str:
    """Build safe guidance without making path attribution a prerequisite.

    SQLite may need to update a rollback journal even for a logical read. A
    readonly-class SQLite error therefore always gets recovery guidance. Paths
    passed here have already been confined to declared run roots. Incomplete
    analysis may enrich the message with extant candidates, but never decides
    whether the generic guidance is present.
    """
    scratch_display = str(
        scratch_tmp or "<run_root>/.python-scratch/tmp"
    )
    path_details: list[str] = []
    path_label = (
        "数据库真实路径" if paths_complete else "候选数据库真实路径"
    )
    for database in sqlite_paths:
        companions: list[Path] = []
        for suffix in ("-journal", "-wal", "-shm"):
            companion = Path(f"{database}{suffix}")
            # SQLite sidecars are regular files. Do not follow a sidecar
            # symlink, which could cross the already-validated root boundary.
            if not companion.is_symlink() and companion.is_file():
                companions.append(companion)
        if not database.is_file() and not companions:
            continue
        if companions:
            path_details.append(
                f"{path_label} {database}；已检测到伴随文件："
                + "、".join(str(path) for path in companions)
                + "。"
            )
        else:
            path_details.append(
                f"{path_label} {database}；当前未检测到 -journal/-wal/-shm。"
            )
    observed_paths = "".join(path_details)
    return (
        "SQLite 遇到只读类错误。"
        f"{observed_paths}"
        "先检查数据库真实路径旁是否存在 -journal/-wal/-shm 伴随文件；只要"
        "任一存在，禁止使用 immutable=1，因为它会忽略伴随文件并可能静默"
        "读取未提交状态。请用 safe_run_bash 将数据库和实际存在的 "
        f"-journal/-wal/-shm 一起 cp 到 {scratch_display}/（这是 Python "
        "$TMPDIR 的字面路径），再由 Python 从 $TMPDIR 打开副本，让 SQLite "
        "完成恢复。只有确认数据库已干净关闭且真实路径旁没有任何伴随文件"
        "时，才可用 URI mode=ro&immutable=1 只读打开。正式写入请改用 "
        "submit_job。"
    )


def _readonly_error_literal_paths(detail: str, cwd: str | None) -> list[Path]:
    """Extract only paths explicitly attached to an OS readonly error."""
    paths: list[Path] = []
    pattern = re.compile(
        r"(?:Read-only file system|Permission denied|\[Errno 30\]|\[Errno 13\])"
        r"[^\n'\"]*['\"]([^'\"\n]+)['\"]"
    )
    for value in pattern.findall(detail):
        path = Path(value).expanduser()
        if not path.is_absolute():
            if not cwd:
                continue
            path = Path(cwd) / path
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if path not in paths:
            paths.append(path)
    return paths


def _python_resource_envelope(
    result: dict[str, Any],
    *,
    scientific_primary: bool = False,
    code: str = "",
    cwd: str | None = None,
    run_roots: Sequence[str | Path] = (),
    scratch_tmp: str | Path | None = None,
    effect_projection: PythonEffectProjection | None = None,
) -> dict[str, Any]:
    """把通用 supervisor 的历史 ``build_*`` 标签映射为 Python 终态。"""
    reason = str(result.get("reason") or "")
    if reason.startswith("build_"):
        python_reason = "python_" + reason[len("build_"):]
        result["reason"] = python_reason
        blocker = result.get("blocker")
        if isinstance(blocker, dict):
            blocker["reason"] = python_reason
            if blocker.get("kind") == "build_resource_pressure":
                blocker["kind"] = "python_resource_pressure"
            elif blocker.get("kind") == "build_resource_guard_unavailable":
                blocker["kind"] = "python_resource_guard_unavailable"
        error = str(result.get("error") or "")
        if error:
            result["error"] = error.replace("构建资源", "Python 资源").replace(
                "无界构建", "无界 Python 执行")
    if result.get("required_action") == "analyze_build_behavior":
        result["required_action"] = "analyze_python_behavior"
    if scientific_primary and result.get("status") == "error":
        detail = "\n".join(str(result.get(field) or "") for field in (
            "error", "stderr_tail", "stdout_tail"))
        os_readonly_markers = (
            "Read-only file system",
            "Permission denied",
            "[Errno 30]",
            "[Errno 13]",
        )
        sqlite_readonly_markers = (
            "attempt to write a readonly database",
            "disk I/O error",
            "unable to open database file",
        )
        canonical_roots = []
        for value in run_roots:
            try:
                canonical_roots.append(Path(value).resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                continue
        affected_paths = _readonly_error_literal_paths(detail, cwd)
        sqlite_failure = any(
            marker in detail for marker in sqlite_readonly_markers
        )
        sqlite_paths: list[Path] = []
        sqlite_analysis_complete = False
        if sqlite_failure:
            sqlite_paths, sqlite_analysis_complete = (
                _python_sqlite_reason_path_analysis(
                    code, cwd, effect_projection=effect_projection)
            )
        explicit_os_error_in_run_root = any(
            path == root or path.is_relative_to(root)
            for path in affected_paths
            for root in canonical_roots
        )
        all_sqlite_paths_in_run_root = bool(
            sqlite_analysis_complete
            and sqlite_paths
            and all(
                any(path == root or path.is_relative_to(root)
                    for root in canonical_roots)
                for path in sqlite_paths
            )
        )
        attributed_to_run_root = (
            all_sqlite_paths_in_run_root
            if sqlite_failure
            else explicit_os_error_in_run_root
        )
        if sqlite_failure:
            sqlite_recovery_paths = [
                path
                for path in sqlite_paths
                if any(
                    path == root or path.is_relative_to(root)
                    for root in canonical_roots
                )
            ]
            result["recovery"] = _sqlite_readonly_recovery(
                sqlite_recovery_paths,
                scratch_tmp,
                paths_complete=sqlite_analysis_complete,
            )
        if (
            attributed_to_run_root
            and (
                sqlite_failure
                or any(marker in detail for marker in os_readonly_markers)
            )
        ):
            result.setdefault("reason", "scientific_primary_run_root_readonly")
            if result.get("reason") != "scientific_primary_run_root_readonly":
                return result
            if not sqlite_failure:
                result["recovery"] = (
                    "scientific-primary 的 run_root 只读。临时文件请使用 tempfile，"
                    "或让 numpy/matplotlib 等库函数直接写 Python $TMPDIR；复制现有"
                    "文件请用 safe_run_bash cp 到 <run_root>/.python-scratch/tmp/。"
                    "轻量 Python 的直接 open(..., 'w') / Path.write_text 会被前置门"
                    "拒绝；正式实验写入请改用 submit_job。"
                )
    return result


def _managed_python_workload_block() -> dict[str, Any]:
    return {
        "status": "error",
        "reason": "managed_python_workload_required",
        "error": (
            "safe_execute_python 只用于轻量、单进程诊断。依赖安装、正式科学"
            "计算或声明会派生进程树的 Python 必须交给 safe_run_bash；"
            "长任务使用 submit_job，以安装 PID、内存、时间和日志守卫。"
        ),
        "blocker": {
            "kind": "managed_python_workload_required",
            "node_action": "move_workload_to_managed_bash_or_submission",
        },
    }


async def _safe_execute_python(state: Any, code: str, timeout: int = 300,
                               cwd: str | None = None, **kw: Any) -> dict:
    """scope/route/path → 高危门 → Core Docker RunAttempt。

    顺序与 safe_run_bash 一致：确定性边界检查先于交互式框架门。
    bypass 只跳过可人工授权的环境类 scope；路径与路线有效性仍硬拒。"""
    # ``requirements`` 已从工具 schema 删除：依赖安装不是轻量 Python 能力。
    # 这里只兼容直接旧调用并给出结构化迁移提示，不把隐藏字段重新暴露给 LLM。
    requirements = kw.pop("requirements", None)
    timeout = _coerce_timeout(timeout, 300)
    # 判决拆除·第三波（sb:4004 → schema，2026-09-02）：stage 词表由 schema enum 核。
    execution_stage = str(kw.get("stage") or "diagnostic").strip().lower()
    if execution_stage not in set(EXECUTION_STAGES):
        execution_stage = "diagnostic"
    execution_params = kw.get("execution_params")
    effect_projection = _project_python_effects(code)
    process_launches = list(effect_projection.process_launches)
    if process_launches:
        return {
            "status": "error",
            "reason": "unmanaged_python_process_launch",
            "error": ("safe_execute_python 不负责派生外部进程或进程池；"
                      "请将外部命令交给 safe_run_bash，长任务交给 submit_job。"),
            "blocker": {
                "kind": "unmanaged_python_process_launch",
                "calls": process_launches,
            },
        }
    if requirements:
        return _managed_python_workload_block()
    explicit_cwd = cwd is not None and bool(str(cwd).strip())
    route_action = _python_route_action(
        code=code,
        execution_stage=execution_stage,
        requirements=requirements,
        route_step_id=kw.get("route_step_id"),
        effect_projection=effect_projection,
    )
    route_decision = _resolve_route_context(state, route_action)
    pre_materialization_block = _enforce_route_context(
        state, route_action, route_decision, phase="pre_materialization")
    if pre_materialization_block is not None:
        return pre_materialization_block
    early_route_effects = {
        str(item) for item in (route_decision.get("effective_effects") or [])
    }
    if (
        route_decision.get("policy") == "formal_scientific_execution"
        or "managed_lifecycle" in early_route_effects
        or "scientific_execution" in early_route_effects
    ):
        return _managed_python_workload_block()
    workdir_resolution = "explicit" if explicit_cwd else "action_default"
    if not explicit_cwd:
        route_choice = _route_workdir_choice(state, route_decision)
        try:
            if route_choice.get("status") == "resolved":
                cwd = str(route_choice["path"])
                workdir_resolution = "resolved"
            else:
                cwd = str(experiment_output_dir(state, "runtime", create=True))
        except Exception as exc:
            return _default_workdir_failure("run_root", exc)
    route_action["workdir_roles"] = _observed_workdir_roles(state, cwd)
    route_action["payload_digest"] = hashlib.sha256(json.dumps(
        {
            "code": code,
            "cwd": str(Path(str(cwd)).resolve(strict=False)),
            "timeout": timeout,
            "requirements": requirements or [],
            "execution_params": execution_params,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    route_decision = dict(route_decision)
    route_decision["workdir_role_observed"] = (
        route_decision.get("declared_workdir_role")
        in route_action["workdir_roles"]
    )
    route_decision["workdir_resolution_status"] = workdir_resolution
    route_decision["resolved_workdir"] = str(cwd)
    _record_route_context(state, route_action, route_decision)
    python_write_targets, python_has_dynamic_target = (
        effect_projection.direct_write_paths(cwd)
    )
    formal_scientific_action = _is_formal_scientific_action(
        state,
        execution_stage=execution_stage,
        route_decision=route_decision,
        mechanical_run_root_write=(
            "run_root" in route_action["workdir_roles"]
            and bool(python_write_targets or python_has_dynamic_target)
        ),
    )
    if formal_scientific_action:
        execution_contract_block = _simulation_contract_block(
            state,
            execution_params,
            runner="safe_execute_python",
            input_package_artifact_id=kw.get("input_package_artifact_id"),
            input_package_bindings=kw.get("input_package_bindings"),
        )
        if execution_contract_block is not None:
            return execution_contract_block
    if formal_scientific_action:
        return _managed_python_workload_block()
    # 与 safe_run_bash 共享同一工作目录能力契约：不存在或角色无效
    # 直接拒绝；仅“语义角色已声明、但 Core 尚未授予 OS 写能力”可请求
    # 一次路径级批准，批准后立即重新解析，不能跳过验证。
    workdir_error = resolve_required_workdir(state, cwd, kind="代码")[1]
    if (workdir_error is not None
            and workdir_error.get("reason") == "path_capability_required"):
        approved, approval = _scope_guard_authorization(
            state, kind="python", cmd=code, cwd=cwd, target=str(cwd),
            scope="path_capability_required",
            reason=str(workdir_error.get("error") or ""),
            op="cwd", preview=code,
        )
        if approved:
            workdir_error = resolve_required_workdir(
                state, cwd, kind="代码"
            )[1]
        elif approval is not None:
            return approval
    if workdir_error is not None:
        try:
            state.append_transcript(
                "required_workdir_unavailable", tool="safe_execute_python",
                cwd=str(cwd), code_preview=code[:200] if code else "")
        except Exception:
            pass
        return workdir_error
    route_block = _enforce_route_context(
        state, route_action, route_decision, phase="pre_spawn")
    if route_block is not None:
        return route_block
    if code and code.strip():
        scope_block = _scope_guard_python(
            state, code, cwd=cwd, effect_projection=effect_projection)
        if scope_block is not None:
            # Python 的一次性 OS 写能力只能来自可持久登记的
            # approved_write_root；其它 scope 不支持“批准后仍 EACCES”的
            # 虚假授权，因此也不服从全局 bypass。
            return scope_block
        gate_block = _unified_highrisk_gate(
            state, code, mode="python", tool="safe_execute_python", kind="python",
            python_effects=effect_projection)
        if gate_block is not None:
            return gate_block
    python_sandbox_targets = list(python_write_targets)
    if python_has_dynamic_target and cwd:
        python_sandbox_targets.append(str(cwd))

    roles = collect_path_roles(state)
    try:
        try:
            from .subprocess_policy import scientific_python_scratch_root
        except ImportError:  # pragma: no cover - node runtime import style
            from tools.subprocess_policy import scientific_python_scratch_root
        strict_scratch = scientific_python_scratch_root(
            state, require_exists=False)
    except Exception as exc:
        return _sandbox_roots_error(exc)
    python_scratch_root = strict_scratch or (
        Path(experiment_output_dir(state, "runtime", create=True))
        / ".python-scratch"
    )
    scratch_tmp = python_scratch_root / "tmp"
    scratch_cache = python_scratch_root / "cache"
    try:
        for directory in (
            scratch_tmp,
            scratch_cache,
            scratch_cache / "matplotlib",
            scratch_cache / "pycache",
        ):
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "status": "error",
            "reason": "python_scratch_unavailable",
            "error": (
                "轻量 Python 的 run-local 临时目录不可用，解释器未启动："
                f"{type(exc).__name__}: {exc}"
            ),
            "blocker": {
                "kind": "python_scratch_unavailable",
                "path": str(python_scratch_root),
            },
        }
    if strict_scratch is not None:
        try:
            python_scratch_root = scientific_python_scratch_root(
                state, require_exists=True) or python_scratch_root
        except Exception as exc:
            return _sandbox_roots_error(exc)

    repo_root_path = Path(__file__).resolve().parents[3]
    repo_root = str(repo_root_path)
    pythonpath_roots = [repo_root]
    venv_lib = repo_root_path / ".venv" / "lib"
    if venv_lib.is_dir():
        pythonpath_roots.extend(
            str(path) for path in sorted(venv_lib.glob("python*/site-packages"))
            if path.is_dir()
        )
    repro_root = experiment_output_dir(state, "repro")
    run_roots = [
        role.path for role in roles
        if role.role == "run_root" and role.writable
    ]
    child_env: dict[str, str] = {
        "HOME": "/run/harness-home",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join(pythonpath_roots),
        "TMPDIR": str(scratch_tmp),
        "TMP": str(scratch_tmp),
        "TEMP": str(scratch_tmp),
        "XDG_CACHE_HOME": str(scratch_cache),
        "MPLCONFIGDIR": str(scratch_cache / "matplotlib"),
        "PYTHONPYCACHEPREFIX": str(scratch_cache / "pycache"),
        "EXPERIMENT_REPRO_ROOT": str(repro_root),
        "EXPERIMENT_REPRO_EVIDENCE": str(
            repro_root / "min_run_evidence.jsonl"),
        "EXPERIMENT_SOURCE_WORKTREE_ROOTS": _json.dumps([
            role.path for role in roles
            if role.role == "source_worktree_root" and role.writable
        ]),
        "EXPERIMENT_SOURCE_PATCH_ROOTS": _json.dumps([
            role.path for role in roles
            if role.role == "source_patch_root" and role.writable
        ]),
    }
    if run_roots:
        child_env["EXPERIMENT_RUN_ROOT"] = run_roots[0]
    for name in _SAFE_PYTHON_THREAD_ENV:
        child_env[name] = str(_SAFE_PYTHON_THREAD_LIMIT)

    try:
        python_roots = _sandbox_roots_for_payload(
            state, str(cwd), sandbox_profile="python",
            authorized_targets=python_sandbox_targets,
        )
    except Exception as exc:
        return _sandbox_roots_error(exc)

    route_binding, route_binding_error = _begin_route_attempt(
        state, route_decision, tool="safe_execute_python",
        action=route_action)
    if route_binding_error is not None:
        return route_binding_error
    try:
        code_to_run = _with_no_child_process_audit(
            _with_matplotlib_font_preamble(code)
        )
        # 用**跑着 harness 的那个解释器**，不是一个绝对路径。
        # 为什么，见 `the_interpreter_for_model_code` 的注释 —— 一句话：
        # 硬编码的 /usr/bin/python3 在 macOS 上是 Xcode 的 3.9，没有 numpy，
        # 模型只能满硬盘找；而模型代码要的依赖与 harness 自己的是同一套。
        python_cmd = (
            f"{shlex.quote(the_interpreter_for_model_code())} "
            f"-c {shlex.quote(code_to_run)}"
        )
        result = await _exec_and_log(
            state,
            python_cmd,
            timeout=timeout,
            cwd=cwd,
            sandbox_profile="python",
            sandbox_write_targets=python_sandbox_targets,
            child_env=child_env,
            sandbox_roots=python_roots,
            execution_action=route_action,
            execution_decision=route_decision,
            execution_route_binding=route_binding,
        )
        result = _python_resource_envelope(
            result,
            scientific_primary=strict_scratch is not None,
            code=code,
            cwd=str(cwd),
            run_roots=run_roots,
            scratch_tmp=scratch_tmp,
            effect_projection=effect_projection,
        )
        result["workspace"] = str(cwd)
        result["python_memory_request_gb"] = _SAFE_PYTHON_MEMORY_GB
        result["python_memory_max_bytes"] = _SAFE_PYTHON_MEMORY_MAX_BYTES
        result["python_pid_limit"] = 64
        result["python_thread_limit"] = _SAFE_PYTHON_THREAD_LIMIT
        glyph_warning = _missing_glyph_summary(
            str(result.get("stdout_tail") or "") + "\n"
            + str(result.get("stderr_tail") or ""))
        if glyph_warning:
            result["figure_glyph_warning"] = glyph_warning
    except BaseException as exc:
        _finish_route_attempt(state, route_binding, error=exc)
        raise

    route_result_block = _finish_route_attempt(
        state, route_binding, result=_physical_execution_outcome(result))
    try:
        _record_source_worktree_diffs(
            state, f"safe_execute_python {code[:200]}")
    except Exception:
        pass
    if (formal_scientific_action and result.get("status") == "success"
            and isinstance(execution_params, dict)):
        _record_actual_run_params(
            state, "safe_execute_python", execution_params)
    if route_result_block is not None:
        return route_result_block
    return result



_orig_py_def = _REGISTRY.tools.get("execute_python")
if _orig_py_def is not None:
    _safe_py_schema = dict(_orig_py_def.parameters_schema or {})
    _safe_py_props = dict(_safe_py_schema.get("properties") or {})
    _safe_py_props["cwd"] = {
        "type": "string",
        "description": (
            "Authoritative working directory for this code. Must already exist — a missing "
            "directory is rejected before the interpreter starts and is never created "
            "implicitly; create it in a separate mkdir-only call first. Same contract as "
            "safe_run_bash."),
    }
    _safe_py_props.pop("stage", None)
    _safe_py_props.pop("requirements", None)
    _safe_py_props.pop("resource_profile", None)
    _safe_py_props["execution_params"] = {"type": "object", "description": "Must exactly match frozen pre_registration.metadata.expected_params for a primary simulation."}
    _safe_py_props["route_step_id"] = {
        "type": "string",
        "description": (
            "Identifier of the frozen execution-route step this action fulfils. "
            "Provide it when route matching is ambiguous or the route requires explicit binding."
        ),
    }
    _safe_py_props["input_package_artifact_id"] = {
        "type": "string",
        "description": "Verified dataset or experiment_fallback_inputs artifact consumed by this simulation.",
    }
    _safe_py_props["input_package_bindings"] = _INPUT_PACKAGE_BINDINGS_SCHEMA
    _safe_py_schema["properties"] = _safe_py_props
    # ⚠️ allowed_node_types 必须显式改写：`dataclasses.replace` 会把没点名的字段
    # 原样继承过来，而 execute_python 自 2026-08-08（P6-b）起是
    # allowed_node_types=["postprocess"]。不改写 = 这个**为 experiment 而生的**
    # 包装工具把 experiment 自己挡在门外（实测活了 14 天：能力上有 safe_run_bash
    # 兜底所以没瘫痪，代价是 stage / execution_params 那层对账契约从未生效过）。
    register_tool(_dc.replace(
        _orig_py_def, name="safe_execute_python", internal_only=False,
        description=(
            "在受管隔离沙箱中执行有界、同步的轻量单进程 Python 诊断或机械计算；限制 PID、"
            "内存与时间，日志有界记录。不得安装依赖或派生外部进程。本工具不把任何文件写入"
            "一概升级为新路线步骤；是否需要新路线以本次调用返回的 reason 与提示为准。当前"
            "冻结路线已有与 safe_execute_python 对应的就绪步骤时，真实计算必须传该步骤的 "
            "route_step_id；只读诊断改用 safe_run_bash，不会消耗该步骤。不要把诊断绑定到"
            "该步骤上，否则会把步骤记成已完成。直接写出的 tarfile/zipfile extractall 或 "
            "extract，以及 shutil.unpack_archive，会被识别为 environment_change，必须先"
            "声明对应 Python 路线步骤；动态调用、别名和其他未识别的 Python 效果不因此自动"
            "获得路线保护。真正需要受管生命周期的计算使用 submit_job；"
            "需要 shell 效果分析的动作使用 "
            "safe_run_bash，以便获得可审计的机械判定。不得根据这段描述推定未识别的 Python "
            "效果已经受路线保护。"
        ),
        allowed_node_types=["experiment"],
        parameters_schema=_safe_py_schema), _safe_execute_python)
else:  # pragma: no cover —— 框架未先注册时的兜底
    from core.tool_registry import ToolDefinition
    register_tool(
        ToolDefinition(
            name="safe_execute_python",
            description=(
                "在受管隔离沙箱中执行有界同步的轻量 Python 诊断；构建、simulation "
                "与长任务使用 submit_job。"),
            parameters_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "default": 300},
                    "cwd": {"type": "string"},
                    "route_step_id": {"type": "string"},
                    "resource_profile": {
                        "type": "string",
                        "enum": ["small", "standard", "large", "xlarge"],
                        "default": "standard",
                    },
                    "requirements": {"type": "array", "items": {"type": "string"}},
                    "execution_params": {"type": "object"},
                    "input_package_artifact_id": {"type": "string"},
                    "input_package_bindings": _INPUT_PACKAGE_BINDINGS_SCHEMA,
                },
                "required": ["code"],
            },
            risk_level="high",
        ),
        _safe_execute_python,
    )


# ── v0.11：request_human_input 覆盖已移除（HITL 是框架契约，节点不得改写）──
# 原"缺口#2 修复"（EXPERIMENT_AUTONOMOUS/BENCH 模式 auto-answer，防
# --no-interactive 遇 pause 直接退出）随覆盖一并移除 —— 这两个 env var 从此
# 不再影响 pause 行为。
# 正确的家在**框架应答侧**："无人在场"是运行环境的属性，不是工具的属性，
# 且所有节点跑 benchmark 都需要，应做成 pause_driver 的 headless 策略
# （auto_answer / 超时默认 / fail-fast，已有 AUTO_APPROVE 先例）。
# bypass 已由框架 request_human_input 自动选择默认项并留痕；但 --no-interactive
# 且未开启 bypass 时，pause 仍会退出。不要在节点层重复实现应答策略。
