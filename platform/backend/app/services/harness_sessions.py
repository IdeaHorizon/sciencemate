"""Session-bound kept-alive Harness subprocess registry for local deployment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import HARNESS_HOME_VARIABLE, data_root, settings, the_org_home, the_projects_home
from app.services.instructions import harness_home_for
from app.models.execution import (
    PARKED_WAITING_FOR_HUMAN,
    REQUIRES_LIVE_RUNTIME_STATUSES,
    AttemptStatus,
    Run,
    RunAttempt,
    RunStatus,
    SessionProjection,
)
from app.models.model_backend import ModelBackendConfig
from app.models.user import User
from app.services import run_liveness as _liveness
from app.services.harness_progress import protocol_user_progress
from app.services.harness_runtime import (
    _drain_stderr,
    _provider_base_url,
    harness_subprocess_env,
    the_interpreter_that_runs_the_harness,
)
from app.services.model_backends import credential_is_optional, resolved_api_key
from app.services.model_role_catalog import REASONING_ROLE
from app.services.redaction import PROTOCOL_LINE_LIMIT_BYTES
from app.services.harness_imports import harness_module

logger = logging.getLogger(__name__)

ProtocolCallback = Callable[[dict], Awaitable[None]]
SANDBOX_PROTOCOL_VERSION = 2


#: 整份呈递挂在 pause 事件的这个键下（`core.decision_offer.PAUSE_OFFER_KEY`）。
PAUSE_OFFER_KEY = "offer"


class HarnessSessionError(RuntimeError):
    """A kept-alive project session cannot satisfy an operation.

    Carries the Harness's own **machine-readable** failure identity, not just
    its prose. `platform_runtime.RequestError` calls itself "a stable,
    caller-actionable request validation failure" and the runtime already puts
    `code` / `error_type` / `details` on the wire — but this boundary used to
    read `message` and drop the rest, so every one of those codes arrived here
    as one untyped error. Downstream (`run_failures._classify`) reads
    `exc.code` first; with nothing to read it could only pick the generic
    "internal error, send it again" copy.

    A stable code that never reaches the caller is not a contract. Anything
    that wants to act on *which* failure this was would have to match the
    English sentence — the very thing the code exists to avoid.
    """

    #: Default so `getattr(exc, "code", None)` is always answerable. Subclasses
    #: that own a fixed identity (see `HarnessSessionProcessError`) override it
    #: at class level, and the constructor below never clobbers that.
    code = ""
    error_type = ""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        error_type: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        # Only assign when the caller actually supplied one: a bare
        # `HarnessSessionError("...")` must keep whatever identity its class
        # declares rather than be reset to empty.
        if code:
            self.code = code
        if error_type:
            self.error_type = error_type
        self.details = details or {}


class HarnessSessionStaleError(HarnessSessionError):
    """The process that owned a resumable pause no longer exists."""


class HarnessAnswerSupersededError(HarnessSessionError):
    """答复指向的是上一次呈递 —— run 还活着，只是它问的问题已经换了。

    与 `HarnessSessionStaleError` 分开：那个会被记账层翻成 `stale_unknown`
    （运行时丢了），而这里运行时好端端的，只是人手里那张卡过期了。
    """

    def __init__(self, *, answered: str, live: str | None) -> None:
        super().__init__(
            "This answer addresses an earlier presentation; the run has moved on "
            f"(answered {answered}, live {live or 'unknown'})"
        )
        self.answered = answered
        self.live = live


class HarnessSessionProcessError(HarnessSessionError):
    """The kept-alive Harness child exited before completing an RPC."""

    code = "harness_process_exited"
    # **没有** `retryable`：能不能重试取决于它是**怎么**退出的，而这里还不知道。
    #
    # 原来这行写死 `False`，而 `run_failures.describe` 里"异常自己说了就听它的"
    # 的优先级高于文案表 —— 于是同一次失败上，文案写着「发下一条消息就从断点
    # 接着跑」、旁边的 retryable 标着 False。两句话互相拆台，而两边都不报错。
    #
    # 退出码才答得了这个问题（0 / -15 可续，-9 多半是 OOM 不可续），所以判决
    # 留给读得到退出码的那一层（`run_failures._exit_code_answer`）。

    def __init__(
        self, message: str, *, exit_code: int | None, code: str | None = None
    ) -> None:
        super().__init__(message, code=code)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class AppRunBinding:
    user_id: str
    conversation_id: str
    run_id: str
    session_id: str
    sandbox_attempt_id: str = ""
    sandbox_manifest: dict | None = None
    sandbox_manifest_hash: str = ""

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """谁、哪个会话、哪条 run 的 pause —— 答复要对上的是这四样。

        沙箱三项是 worker 自己的事实（spawn 那一刻定的），不是答复方带来的凭证；
        拿它们做相等比较，换过 release 后清单 hash 一变，一次对着正确 pause 的答复
        就被判成"没有可寻址的 pause"（2026-09-09 node20）。
        """
        return (self.user_id, self.conversation_id, self.run_id, self.session_id)


#: 角色 → 这次会话为它解析出的后端。key 取自 shared/model_roles.yaml。
RoleBindings = dict[str, ModelBackendConfig]


def reasoning_backend(role_bindings: RoleBindings) -> ModelBackendConfig:
    """主推理模型 —— 会话里"选模型"选的那个。

    它在 role_bindings 里是一条普通记录，没有旁路：任何"当前用哪个模型"的
    问题都从这里取，不另存一个字段。
    """
    backend = role_bindings.get(REASONING_ROLE)
    if backend is None:
        raise HarnessSessionError("No model backend is bound to the 'reasoning' role")
    return backend


def _backend_fingerprint(role_bindings: RoleBindings) -> str:
    """这一次 spawn 的**全部**模型绑定的指纹。

    ⚠️ 必须覆盖每一个角色，不只是主模型。worker 的绑定是 spawn 时定死在环境
    里的，指纹漏掉哪个角色，改那个角色就"改了但没生效"——UI 显示新的、进程
    里跑的是旧的，而两边都不报错。这正是这次要消灭的那类缝。
    """
    parts = []
    for role in sorted(role_bindings):
        backend = role_bindings[role]
        api_key = resolved_api_key(backend) or ""
        parts.append(
            "|".join(
                (
                    role,
                    backend.id,
                    backend.provider,
                    backend.model,
                    backend.base_url or "",
                    hashlib.sha256(api_key.encode()).hexdigest(),
                )
            )
        )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def worker_reuse_decision(*, same_bindings: bool, conversation_in_flight: bool) -> str:
    """绑定（模型/设置/上下文/指令快照）变了之后，那个还活着的 worker 怎么办。

    worker 的全部绑定都是 **spawn 时定死在环境变量与状态目录里的**
    （`_child_environment`），所以"换绑定"对一个已经起来的进程没有任何意义
    —— 只能换掉这个进程。

    - `"reuse"`   绑定没变，接着用；
    - `"respawn"` 绑定变了且没有在飞的 turn → 杀掉重开，从 checkpoint 接续。
      **停靠（paused）也走这条**：pause 呈递发生在 turn 边界之后，checkpoint
      已经落盘（agent_loop 每个 turn 末写 messages_checkpoint.json），旧进程
      手里没有不可重建的状态。它挂着的 pause 由 stale-binding 钩子立刻标成
      可恢复 —— 与登出（auth.logout → terminate_user）同一条处理路。

      2026-08-21 之前 paused 在这里被判成 conflict：用户停靠中换了模型，
      之后每条消息都撞「平台内部错误」，而登出再登录（同样是杀进程）反而
      能好 —— 同一个动作两个入口两种结局，说明 conflict 从来不是必要的。
      会话是磁盘上的记录，不是这个进程；能从 checkpoint 重建的东西，缺了
      就重建，不报错。
    - `"defer"`   有会话面 op 正在飞 → **这一轮换不掉，下一轮换**。进程带着
      旧绑定跑着，而杀掉它就是销毁一次正在跑的研究；新 run 的记录也必须与
      真实执行的绑定一致（记录不许说谎）。所以这一轮照旧用它。

      2026-08-23（D10 删除清单）之前这里叫 `"conflict"`，调用方据此抛
      `session_busy`。那是"忙 = 拒收"的最后一处 —— 而它拒的是一个完全合法
      的请求：用户换了模型又说了句话，两件事都没错。现在不拒：这一轮用旧
      绑定跑完，下一次进来（那时已空闲）自然走 `respawn` 换代。判据没变，
      **出路多了一条**，而且是自愈的（指纹还是旧的，下次还会被发现）。

      界面上那句话（`modelSwitchNotice`）本来就是这么写的：在飞时说"这一轮
      仍用原来的模型"。从前文案这么说、机制却是拒收 —— 话说了什么，这里
      现在真是什么了。
    """
    if same_bindings:
        return "reuse"
    return "defer" if conversation_in_flight else "respawn"


def _snapshot_hash(snapshot: dict | None) -> str | None:
    if snapshot is None:
        return None
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _paths(
    user: User, project_id: str, session_id: str
) -> tuple[Path, Path, Path, Path, str]:
    root = Path(settings.harness_root).expanduser().resolve()
    if not (root / "core" / "agent_loop.py").is_file():
        raise HarnessSessionError(
            "HARNESS_ROOT is not a valid current harness checkout",
            code="runtime_environment_broken",
        )
    python = the_interpreter_that_runs_the_harness()
    if not Path(python).is_file():
        # 常见原因：venv 的基础解释器被删（2026-08-21：anaconda 卸载后
        # .venv/bin/python 成了断链）。启动探针（probe_runtime_environment）
        # 应该在部署那一刻就抓住它 —— 走到这里说明环境是在运行期间坏掉的。
        raise HarnessSessionError(
            "HARNESS_PYTHON is not an executable file",
            code="runtime_environment_broken",
        )
    state_root = data_root("state").resolve()
    # 「这个用户的 harness home 在哪」只有一处回答 —— 平台把个人指令文件写到
    # 那底下，worker 的 `core.paths.home()` 读的也是那底下。两边各抄一行
    # `state_root / "users" / user.id`，就是写一处读另一处的老毛病。
    home_dir = harness_home_for(user.id)
    # Runtime scratch lives inside the Session worktree boundary.  It is kept
    # below .research/runtime (ignored by Git, but NOT a cache); validated artifacts and sanitized
    # Session records are committed through ProjectRepository, never copied to
    # a second hidden Project tree.
    from app.services.project_repository import get_project_repository
    from app.services.session_runtime_paths import session_runs_root

    worktree = get_project_repository().session_path(project_id, session_id)
    if not worktree.is_dir():
        raise HarnessSessionError("Session Git worktree is not initialized")
    # 运行时记录的落点 —— 只有 session_runtime_paths 说了算（那里也解释了
    # 为什么它不再叫 cache：git clean -xdf 会连科研过程记录一起清掉）。
    session_state = session_runs_root(worktree)
    for directory in (state_root, home_dir, session_state):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    # 「我是谁」由平台说（KB 记录的署名、按人写的算力授权都靠它）。
    from app.services.instructions import publish_identity

    # `_paths` 此前只要 `user.id`；名字和邮箱是锦上添花，不让起 worker 这件事依赖它们。
    publish_identity(str(user.id), display_name=getattr(user, "display_name", "") or "",
                     email=getattr(user, "email", "") or "")
    return root, home_dir, session_state, worktree, python


def _role_delivery_payload(role_bindings: RoleBindings) -> str:
    """建 worker 的**唯一一条**模型交付通道（HARNESS_MODEL_ROLES）。

    此前这里是一张写死的 env 透传白名单：主模型走 LLM_*，节点内的辅助模型
    （postprocess 审图 VLM）各读各的环境变量名，靠白名单里手写一行放行。
    两个后果都实测过：

      1. 名单跟 worker 侧的**凭据擦除名单**分家 —— 白名单认识审图 key、
         擦除名单不认识，于是它留在 os.environ 里被模型可控的 execute_python
         （subprocess，继承 env）够得着，也没进脱敏面。
      2. 那把 key 只能来自"后端进程自己的环境"，用户在 UI 上既看不见也
         配不了。2026-08-22 实测：本机后端的启动脚本没 export 它，
         postprocess 渲染完三张 publication 图才在 finalize 处撞墙。

    现在按角色目录**扫**：加一个角色只改 shared/model_roles.yaml，这里自动
    跟上，没有任何名单要记得改。
    """
    payload: dict[str, dict] = {}
    for role, backend in sorted(role_bindings.items()):
        base_url = _provider_base_url(backend)
        api_key = resolved_api_key(backend)
        if not base_url or (not api_key and not credential_is_optional(backend)):
            # 缺料的角色**整条不发**，而不是发一条半成品。worker 侧对
            # "没配"有明确的处理（局面，不是错误）；对"配了但缺字段"没有。
            #
            # "没有 key"只有在**我们没有别的办法够到端点**时才算缺料：用户自己
            # 填了 base_url 的自建端点大多不鉴权（credential_is_optional）。
            continue
        payload[role] = {
            "provider": backend.provider,
            "model": backend.model,
            "base_url": base_url,
            "api_key": api_key or "",
            "display_name": backend.display_name,
            "context_window_tokens": backend.context_window_tokens,
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _child_environment(
    root: Path,
    role_bindings: RoleBindings,
) -> dict[str, str]:
    backend = reasoning_backend(role_bindings)
    base_url = _provider_base_url(backend)
    if not base_url:
        raise HarnessSessionError("Selected provider has no harness-compatible base URL")
    api_key = resolved_api_key(backend)
    if not api_key and not credential_is_optional(backend):
        # 自建端点（用户自己填了 base_url）没有 key 是正常形态，不是缺料；
        # 官方托管端点没有 key 才是真的够不到。
        raise HarnessSessionError("Selected Harness provider has no usable credential")
    # 只透传**系统**变量。所有模型凭据走 HARNESS_MODEL_ROLES 一条通道，
    # worker 启动时把它整个从 env 弹出并搬进 runtime_secrets。
    #
    # Windows 系统变量（SYSTEMROOT 等）也在放行之列 —— worker 是个 python.exe，
    # 缺了 SYSTEMROOT 连 ws2_32 / 加密 DLL 都加载不了、根本起不来。这份名单一处
    # 回答（shared.lib.platform_env），worker env 与模型 python 子进程 env 同源。
    # HARNESS_FRAMEWORK_HOME 必须透传：worker 用 core.paths 读它当数据根，后端用
    # app.config 读 PLATFORM_DATA_ROOT —— publish_the_data_root 把二者钉成同一个值
    # 并写进 os.environ。不透传的话 worker 会退回自己的**默认**根，两边分叉（POSIX
    # 上默认碰巧一致所以没暴露，Windows 上后端 %LOCALAPPDATA%\afs、worker
    # ~/.harness-framework 就是 08-21 丢 43 个会话的形状）。
    # 数据根的变量名归 config 一处回答（HARNESS_HOME_VARIABLE），不在这里裸写字面量
    # —— 后端有一道闸（test_one_answer_for_where_the_data_is）钉着「谁都别自己算数据在哪」。
    # 系统变量 + UTF-8 模式（Windows）+ PYTHONPATH 走一处回答 harness_subprocess_env；
    # 这里只点名 worker 额外要放行的 HARNESS_* 与数据根变量。（HARNESS_HOME_VARIABLE 必须
    # 透传：worker 用它读数据根，不传就退回默认根、两边分叉——08-21 丢 43 会话那形状。
    # 变量名归 config 一处回答，test_one_answer_for_where_the_data_is 钉着。）
    child_env = harness_subprocess_env(root, passthrough=(
        HARNESS_HOME_VARIABLE,
        "HARNESS_EXECUTOR", "HARNESS_ENFORCEMENT_POLICY",
        "HARNESS_SANDBOX_CEILING", "HARNESS_SANDBOX_NAMESPACE",
        "HARNESS_SANDBOX_EGRESS_ALLOWLIST", "HARNESS_JOBS_ROOT",
    ))
    child_env.update(
        {
            "HARNESS_MODEL_ROLES": _role_delivery_payload(role_bindings),
            # LLM_* 仍然发：harness 里还有直读它们的老路径（CLI 入口的合成
            # 来源就是它们）。真相源是上面那条通道 —— 这三个是它对 reasoning
            # 角色的**投影**，由同一个 role_bindings 派生，不会各自演化。
            "LLM_API_KEY": api_key or "",
            "LLM_BASE_URL": base_url,
            "LLM_MODEL": backend.model,
            # 瞬时故障值得扛多久。平台上的每一次 harness 调用都属于一个**可能
            # 已经跑了几小时**的会话 —— 2026-08-10 实测：一轮做完 3 个真实
            # LAMMPS 模拟的实验，死在后端 HTTP 503 上，而当时 5xx 的重试阶梯是
            # 1s/2s/4s，总共扛 7 秒。事后直接探那个端点：200，2 秒，回 pong
            # —— 它只是短暂不可用。
            #
            # 默认 60s 是给"用户在等回复"的一次性调用的；这里给 600s：已经投入
            # 几小时的 run，值得为一次后端重启等十分钟。次数上限仍在，两个闸
            # 谁先到都停。
            "LLM_RETRY_BUDGET_SECONDS": os.getenv("LLM_RETRY_BUDGET_SECONDS", "600"),
        }
    )
    # 上下文窗口跟着**这个模型**走（2026-08-18）。
    #
    # 此前这条从来没被传下去：worker 一律吃 `LLM_CONTEXT_WINDOW` 的兜底默认
    # 120000，不管用户选的是哪个模型。摘要器按窗口的 70% 触发压缩，于是一个
    # 百万窗口的模型也在 84k 处开始反复压 —— 实测一个会话压了 15 次，最后
    # 调度器自己要求换 session。
    #
    # 没配（NULL）就不传，让 harness 用它自己的默认 —— 别替用户编一个数。
    if backend.context_window_tokens:
        child_env["LLM_CONTEXT_WINDOW"] = str(int(backend.context_window_tokens))
    # 档位不走环境变量：worker 的三个开关全部由每条请求带来的授权声明投影
    # （`session_driver.apply_autonomy`），spawn 时塞一个 HARNESS_AUTO_APPROVE 只会
    # 在第一次声明到达时被原样覆盖 —— 一个从不生效的开关不该留在这里。
    return child_env


class _WorkerHandle:
    """worker 进程本身（与 `_WorkerChannel` 对称：那是收发面，这是进程）。

    spawn 出来的有 asyncio 句柄；**接回来的只有 pid** —— 后端重启之后我们
    不是它的父进程了。两者共用同一份会话逻辑，差别收在这里。
    """

    async def wait_exit(self, timeout: float) -> bool:
        """等它退出。True = 已退出。"""
        raise NotImplementedError

    def signal_stop(self) -> None:
        return None

    def force_kill(self) -> None:
        return None

    def release(self) -> None:
        """放手：本进程退场后它接着跑。默认无事可做（接回来的本来就不归我们管）。"""
        return None

    @property
    def alive(self) -> bool:
        raise NotImplementedError


class _ChildOwnedByTheOS:
    """一个**操作系统的**子进程，外加 asyncio 的三根流。

    ## 为什么不用 `asyncio.create_subprocess_exec`

    区别只有一个词：**谁拥有它**。那条路把孩子交给事件循环的 subprocess
    transport，而 transport 收摊时会 `kill()` 还活着的孩子
    （`BaseSubprocessTransport.close()`，`__del__` 也走这一步）。于是"放手"
    只能去改 transport 的私有状态 `_closed` —— 而那个属性**只有 stdlib 的
    asyncio 有**。

    生产跑的是 uvloop（`UVProcessTransport` 没有 `_closed`）：

        AttributeError: 'uvloop.loop.UVProcessTransport' object has no attribute '_closed'
        ERROR:    Application shutdown failed. Exiting.

    node20 的 backend.log 里这一对出现了 **9 次** —— 每一次后端关机都是。而
    `detach_all` 是顺序循环，第一个 worker 抛出就把后面所有 worker 的 detach
    一起带走：PR#784 那条「部署放手不杀 worker」的保证，在生产上从来没生效过
    （2026-09-16 那次部署：23 个 worker 进去，20 个出来）。

    `subprocess.Popen` 不拥有孩子 —— 它的 `__del__` 只在还没 wait 时警告一句，
    从不 kill。所以"放手"退化成"关掉我们这头的管道"：公开 API，两种事件循环、
    两个平台同一个结果。**判据也随之不再依赖事件循环** —— 这才是这次故障的根：
    钉住 `release()` 的那条测试跑在 asyncio 上，而生产跑在 uvloop 上，
    于是它一直绿、生产一直坏（[[feedback_criterion_must_not_depend_on_the_environment]]）。

    对外的面与 `asyncio.subprocess.Process` 一致（`pid` / `returncode` /
    `wait()` / `kill()` / `stdin` / `stdout` / `stderr`），所以下游
    （`_PipeChannel`、`_drain_stderr`、`_connect_control_address` 的
    `still_starting`）一个字都不用改。
    """

    def __init__(self, popen: subprocess.Popen, *, stdin, stdout, stderr,
                 pipe_transports: list) -> None:
        self._popen = popen
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._pipe_transports = pipe_transports

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self._popen.poll()

    async def wait(self) -> int:
        """等它退出。`Popen.wait` 是阻塞调用，所以放线程里 —— 事件循环不停。"""
        return await asyncio.to_thread(self._popen.wait)

    def kill(self) -> None:
        self._popen.kill()

    def close_pipes(self) -> None:
        """关掉我们这头的三根管道。**这就是"放手"的全部。**

        worker 那头读到 EOF，我们这头不再有任何东西引用它的 fd；进程本身归
        操作系统，本后端退场与它无关。
        """
        for transport in self._pipe_transports:
            with suppress(Exception):
                transport.close()
        for stream in (self._popen.stdin, self._popen.stdout, self._popen.stderr):
            if stream is not None:
                with suppress(Exception):
                    stream.close()


def _popen_with_pipes_this_loop_can_read(argv: list[str], **kwargs) -> subprocess.Popen:
    """起进程，三根管道做成**本平台的事件循环接得上的形状**。

    `subprocess.PIPE` 给的东西，两个平台不是一回事：

    * POSIX：匿名管道的 fd。selector 循环 `select()` 它就行，`subprocess.Popen`
      给什么都能接。
    * Windows：proactor 循环把管道**注册进 IOCP**
      （`CreateIoCompletionPort(obj.fileno(), ...)`，见 `asyncio/windows_events.py`），
      所以它要的是 **overlapped 打开的管道 HANDLE**；而 `subprocess.Popen` 的
      `.stdin/.stdout/.stderr` 是架在 CRT **fd** 上的文件对象。把 fd 当 HANDLE 递给
      `CreateIoCompletionPort`，Windows 当场
      `OSError: [WinError 6] 句柄无效` —— 这就是 0.5.x 桌面版**每一轮**都失败的原因
      （`connect_write_pipe` 那一步同步抛出 → spawn 失败 → 整轮失败）。

    stdlib 里做这件事的现成件是 `asyncio.windows_utils.Popen`：`CreateNamedPipe` +
    `FILE_FLAG_OVERLAPPED`，父这头包成 `PipeHandle`（`fileno()` 返回真 HANDLE）。
    `asyncio.create_subprocess_exec` 在 Windows 上走的也正是它 —— 也就是说
    PR#1040 换成裸 `subprocess.Popen` 时，**顺手丢掉的就是这一层**：那次要换掉的是
    「谁拥有这个孩子」（transport 收摊会 kill），不是「管道长什么样」。这里只把管道
    那一层要回来：`windows_utils.Popen` 是 `subprocess.Popen` 的**子类**，
    `wait/kill/poll` 与 `__del__` 全是 `subprocess` 的，[[孩子归操作系统]] 那套语义
    一个字不变。

    为什么不改成"Windows 上退回 `asyncio.create_subprocess_exec`"：那等于把 PR#1040
    修好的东西在一个平台上原样退回去 —— 关机时 transport 照样 kill worker。
    一个平台一条路，两条路就有两种关机语义。
    """
    if sys.platform == "win32":
        from asyncio import windows_utils

        return windows_utils.Popen(  # noqa: S603 - argv 由本模块拼装，不经用户输入
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, **kwargs,
        )
    return subprocess.Popen(  # noqa: S603 - argv 由本模块拼装，不经用户输入
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0, **kwargs,
    )


async def _spawn_child_owned_by_the_os(
    argv: list[str], *, cwd: str, env: dict, limit: int, **popen_kwargs
) -> _ChildOwnedByTheOS:
    """spawn 一个不归事件循环管的子进程，并把它的三根管道接成 asyncio 流。"""
    loop = asyncio.get_running_loop()
    popen = _popen_with_pipes_this_loop_can_read(
        argv, cwd=cwd, env=env, **popen_kwargs)
    transports: list = []

    async def _reader(pipe) -> asyncio.StreamReader:
        stream = asyncio.StreamReader(limit=limit, loop=loop)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stream, loop=loop), pipe)
        transports.append(transport)
        return stream

    try:
        stdout = await _reader(popen.stdout)
        stderr = await _reader(popen.stderr)
        write_transport, write_protocol = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(loop=loop), popen.stdin)
        transports.append(write_transport)
        stdin = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    except BaseException:
        # 接管道这一步失败了，孩子还在 —— 别把一个没人管的进程留在机器上。
        with suppress(Exception):
            popen.kill()
        raise
    return _ChildOwnedByTheOS(popen, stdin=stdin, stdout=stdout, stderr=stderr,
                              pipe_transports=transports)


class _SpawnedWorker(_WorkerHandle):
    """我们自己 spawn 的：有 asyncio.subprocess 句柄。"""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    async def wait_exit(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def force_kill(self) -> None:
        with suppress(ProcessLookupError):
            self.process.kill()

    def release(self) -> None:
        """放手：关掉我们这头的管道，孩子归操作系统。

        这里曾经还有一行 `transport._closed = True` —— 全后端唯一一处碰事件
        循环私有属性的地方，用来让 asyncio 的 transport 收摊时别 kill 孩子。
        它在 uvloop 上直接抛 AttributeError（生产 9 次关机全中），把
        `detach_all` 整个循环带走。现在孩子根本不归事件循环拥有
        （`_ChildOwnedByTheOS`），这一行连同它背后那个问题一起没有了。
        """
        self.process.close_pipes()


class _ReattachedWorker(_WorkerHandle):
    """后端重启后接回来的：只有 pid。

    "还活着吗"必须**现算**（问进程），不能读一个可能陈旧的记录 —— 拿陈旧
    数字去杀进程，杀掉的是无辜的那个（PR#438 的通则：不可撤销的事发生前，
    别把判据建在记录上）。身份判据与 reap 同一份 `_process_is_a_runtime_worker`。
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid

    @property
    def alive(self) -> bool:
        return _process_is_a_runtime_worker(self.pid)

    async def wait_exit(self, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not self.alive:
                return True
            await asyncio.sleep(0.1)
        return not self.alive

    def signal_stop(self) -> None:
        harness_module("shared.lib.process_control").terminate(self.pid)

    def force_kill(self) -> None:
        harness_module("shared.lib.process_control").kill(self.pid)


class _WorkerChannel:
    """与 worker 的收发面。**管道和 socket 的唯一差别就在这里。**

    RPC 分发一个字不分叉 —— `_rpc_locked` 只跟这个接口打交道
    （"调它，别再实现一遍"）。
    """

    async def send_line(self, line: str) -> None:
        raise NotImplementedError

    async def read_line(self) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        return None


class _PipeChannel(_WorkerChannel):
    """老路：worker 的 stdin/stdout。后端一死管道 EOF，worker 跟着走。"""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @property
    def usable(self) -> bool:
        return self._process.stdin is not None and self._process.stdout is not None

    async def send_line(self, line: str) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    async def read_line(self) -> bytes:
        assert self._process.stdout is not None
        return await self._process.stdout.readline()

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()


class _SocketChannel(_WorkerChannel):
    """新路：unix socket。**后端可以死而复生，worker 原地等下一个。**

    这是"部署不再杀科研"的后端半边：连接不再是 worker 的生命线，只是
    这一次对话的收发面。断了就断了，worker 那头把转发面摘掉继续跑
    （事件照落 events.jsonl），新后端接回来按偏移补齐。
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @property
    def usable(self) -> bool:
        return not self._writer.is_closing()

    async def send_line(self, line: str) -> None:
        self._writer.write(line.encode())
        await self._writer.drain()

    async def read_line(self) -> bytes:
        return await self._reader.readline()

    def close(self) -> None:
        with suppress(Exception):
            self._writer.close()


async def reattach_session(
    project_id: str,
    session_id: str,
    *,
    owner_user_id: str,
) -> _ProjectHarnessSession | None:
    """接回一个**还活着**的 worker。接不上 → None（照旧走判无主那条路）。

    ## 判据全部现算，一条都不读陈旧记录

      1. 这个 session 的注册表行在不在（行的**位置**就绑定了身份 —— 它写在
         这个 session 自己的 state 目录里，锁着 flock）；
      2. 行里的 pid 现在是不是一个真的 runtime worker（与 reap 同一份判据）；
      3. 行里有没有 control_socket（stdio 老 worker 接不回来，如实认了）；
      4. socket 连得上（accept 线程独立于命令循环，**跑轮中也答得上**）；
      5. 它说得出自己在干什么 —— 活动自报，或者（老 worker）一次 status RPC。

    ## 体检不能是一次 RPC（2026-08-23 修）

    这里原来无条件发 `op=status` 并等 10 秒。而 worker 的命令循环在一轮
    正在跑时是**堵住的**（`await session.turn(...)` 一跑几小时，命令面多路
    复用是 P1 的事）—— 于是：

        正在跑一个几小时实验的 worker → status 超时 → 接不回来 → 判无主
          → 启动对账把它 SIGTERM 掉

    也就是说，这条恢复路径**恰好在它存在的那个场景里失效**：闲着的 worker
    接得回来，正在做研究的接不回来。P0 承诺的"部署不打断科研"因此从来没有
    真正成立过。

    正解是让体检问**现场落下的事实**，而不是问一个可能没空回答的进程：
    活动文件由 worker 自己写（拿到 session 锁之后才存在 = 它必然已 init），
    带心跳租约（`working` 静默太久会衰减成 `unknown`）。跑轮中它照样是新鲜的
    —— 因为心跳挂在事件产出上，而跑轮中事件最多。

    老 worker 不写这个文件，对它们保留原来的 status 探针（**短超时**，
    因为它答不上来这件事本身就是信息）。

    ## 接回来要连绑定一起接

    只把 socket 接回来是半截的：`session.binding` 是内存对象，后端一重启就
    没了，于是 `live_binding()` 返回 None，`mark_orphaned_harness_runs` 照旧
    把这条 run 判成无主 **并把刚接回来的 worker 杀掉**。接回来的下一秒被自己
    人杀死，比接不回来更难查。

    绑定的真相源因此必须落在进程外：派活时由 App Server 随命令送给 worker，
    worker 写进自己的活动文件。谁在跑、为谁跑，答案在**干活的那个进程**手里。
    """
    from app.services.session_event_log import registry_row

    lock_path = _session_lock_path(project_id, session_id)
    if lock_path is None or not lock_path.is_file():
        return None
    row = registry_row(lock_path)
    if int(row.get("sandbox_protocol_version") or 0) != SANDBOX_PROTOCOL_VERSION:
        return None
    pid = row.get("pid")
    control_address = str(row.get("control_socket") or "").strip()
    if not isinstance(pid, int) or pid <= 1 or not control_address:
        return None
    if not _process_is_a_runtime_worker(pid):
        return None

    try:
        # 注册表行里存的是**真实**地址（worker 绑好后写的，unix 路径或 tcp:host:port）。
        # spawn_token 也在行里 —— tcp 环回握手认它。
        # 接回来的 worker 可能是**上一个 release** 的。协议 v2 起命令面握手
        # 双向、两种传输都做；v1 的 worker 在 unix 上不认识 hello 那一行，把
        # 它塞进去等于往命令流里注入一条它读不懂的请求。
        #
        # 版本从注册表行读 —— worker 自己写的事实，不是猜。缺字段的老记录
        # 读侧必须容忍（注册表是增量演进的），所以默认按 v1 处理。
        worker_protocol = row.get("protocol_version")
        speaks_handshake = isinstance(worker_protocol, int) and worker_protocol >= 2
        if not speaks_handshake:
            logger.info(
                "Adopting a worker that predates the control handshake "
                "(protocol_version=%r) — connecting without it", worker_protocol,
            )
        channel = await _connect_control_address(
            control_address,
            spawn_token=str(row.get("spawn_token") or ""),
            timeout_seconds=3.0,
            handshake=speaks_handshake,
        )
    except (HarnessSessionError, OSError):
        return None

    session = _ProjectHarnessSession(
        project_id=project_id,
        session_id=session_id,
        owner_user_id=owner_user_id,
        backend_id="",
        backend_fingerprint="",
        platform_context_hash=None,
        process=None,
        stderr_task=None,
        provider_secrets=(),
        spawn_token=str(row.get("spawn_token") or ""),
        channel=channel,
        worker=_ReattachedWorker(pid),
    )

    activity = read_worker_activity(project_id, session_id)
    if activity is None:
        # 老 worker（不写活动文件）：退回 status 探针。它跑轮中答不上来，
        # 于是那种 worker 仍然接不回来 —— 如实认了，不假装接上。
        status = await _probe_worker_status(session)
        if status is None or not status.get("initialized"):
            channel.close()
            return None
        session.reattached_status = status
        return session

    if activity.state == "idle":
        # 闲着 = 命令循环空着 = 问得动。多一层现场核对没有代价，而"活动文件
        # 说闲着、进程其实答不上话"是一个我们想知道的分歧。
        status = await _probe_worker_status(session)
        if status is None:
            channel.close()
            return None
        session.reattached_status = status
    else:
        session.reattached_status = {
            "initialized": True,
            "from_activity": True,
            "state": activity.state,
            "declared": activity.declared,
            "turn_id": activity.turn_id,
        }

    _restore_binding_from_activity(session, activity)
    return session


async def _replay_worker_events(db: AsyncSession, *, run: Run, owner_user_id: str) -> None:
    """把这条 run 在"后端不在"那段时间里产生的持久事实补进库（P0-3）。

    补齐是**幂等**的（水位现算），所以这里不需要判断"到底断了多久" ——
    该补的自然会补，不该补的自然被挡住。
    """
    from app.services.execution_ingest import (
        DecisionAuthoritySnapshot,
        ExecutionIngestService,
        IngestContext,
    )
    from app.services.session_event_log import registry_row
    from app.services.session_event_replay import replay_missed_events

    lock_path = _session_lock_path(run.project_id, run.session_id)
    if lock_path is None:
        return
    events_path = str(registry_row(lock_path).get("events_path") or "").strip()
    if not events_path:
        return  # 老 worker 没落过盘 —— 那段历史确实不存在，别假装补得回来
    context = IngestContext(
        tenant_id=run.tenant_id,
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        session_id=run.session_id,
        run_id=run.id,
        actor_user_id=owner_user_id,
        decision_authority=DecisionAuthoritySnapshot(
            authority_type="initiating_user",
            authority_subjects=(owner_user_id,),
            required_approval_count=1,
            action_set_version="harness-normal-v1",
            policy_snapshot_id="local-runtime-v1",
        ),
    )
    report = await replay_missed_events(
        db,
        service=ExecutionIngestService(),
        context=context,
        events_path=events_path,
    )
    if report.worth_logging:
        logger.warning(
            "Replayed worker events for run %s: %d ingested, %d already known, "
            "%d malformed line(s), %d failed",
            run.id, report.ingested, report.already_known, report.malformed, report.failed,
        )


def terminal_result_on_disk(run: Run) -> tuple[dict | None, str]:
    """这条 run 的终止 result 在不在 events.jsonl 里 —— worker 在没人听时跑完了（#785）。

    返回 `(result.data, request_id)`；没有 → `(None, "")`。

    判据：文件里**最后一条** `result` 事件（会话面严格串行，最后一条就是最近
    一轮的），且它的时间在这条 run 建行**之后** —— 否则它属于上一轮，这条 run
    的派发压根没送到 worker。大 payload 在落盘前被外置成引用（P0-7），取回来
    时按引用提货。

    放在这里而不是执行层：启动对账（`mark_orphaned_harness_runs`）判"无主"之前
    要先问一句"它是不是跑完了只是没人记账"，那一步在这个模块里。
    """
    from app.services.session_event_log import read_events, registry_row

    lock_path = _session_lock_path(run.project_id, run.session_id)
    if lock_path is None:
        return None, ""
    events_path = str(registry_row(lock_path).get("events_path") or "").strip()
    if not events_path or not Path(events_path).is_file():
        return None, ""
    last: dict | None = None
    offset = 0
    while True:
        batch = read_events(Path(events_path), offset=offset)
        if batch.truncated:
            offset = 0
            last = None
            continue
        if not batch.events:
            break
        offset = batch.offset
        for record in batch.events:
            if record.get("type") == "result" and isinstance(record.get("data"), dict):
                last = record
    if last is None:
        return None, ""
    created = run.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    try:
        emitted_at = datetime.fromisoformat(str(last.get("at") or ""))
    except ValueError:
        return None, ""
    if emitted_at.tzinfo is None:
        emitted_at = emitted_at.replace(tzinfo=UTC)
    if created is not None and emitted_at < created:
        return None, ""
    return _materialize_event_blobs(last["data"]), str(last.get("request_id") or "")


def _materialize_event_blobs(value: Any) -> Any:
    """把事件日志里的引用（P0-7 外置的大 payload）换回内容。

    提货单的形状只有 `core.event_blobs` 一份定义，经契约桥取；提不到货
    （被 drop / 文件没了）就留引用原样 —— 它自己说得出自己是什么。
    """
    from app.services.harness_contract import HarnessContractUnavailable, event_blobs

    try:
        blobs = event_blobs()
    except HarnessContractUnavailable:
        return value
    if blobs.is_reference(value):
        path = value.get("path")
        if isinstance(path, str) and path:
            try:
                return Path(path).read_text(encoding="utf-8")
            except OSError:
                return value
        return value
    if isinstance(value, dict):
        return {key: _materialize_event_blobs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize_event_blobs(item) for item in value]
    return value


async def _probe_worker_status(session: _ProjectHarnessSession) -> dict | None:
    """问一句"你还在吗、在干什么"。答不上来 → None。

    超时取 10 秒：这条探针只对**闲着的** worker 有意义，闲着的 worker 答得
    是即时的。等更久换不来更多信息，只会把启动对账拖长。
    """
    try:
        status = await session._rpc_locked(
            {"op": "status", "request_id": f"reattach-{uuid4().hex}"},
            terminal_types={"status"},
            on_progress=_noop_callback,
            on_protocol_event=_noop_callback,
            timeout_seconds=10,
            conversation=False,
        )
        if int(status.get("sandbox_protocol_version") or 0) != SANDBOX_PROTOCOL_VERSION:
            return None
        return status
    except (HarnessSessionError, OSError, asyncio.IncompleteReadError):
        return None


def _restore_binding_from_activity(session: _ProjectHarnessSession, activity) -> None:
    """把"这条 run 还有主"这件事重新变成真的。

    绑定四个字段缺一不可 —— 缺了就不是一条能用的绑定，宁可不装（下游
    `binding.run_id == run.id` 会如实不匹配），也不要装一条半截的：
    挂错 run 比不挂更难查。

    停在问题上的那种还要把 pause 状态一起接回来，否则 `paused_binding()`
    返回 None，用户的答复会被当成新一轮 —— 那条 pause 就永远等不到人了。
    """
    binding = activity.app_binding if isinstance(activity.app_binding, dict) else {}
    fields = {key: str(binding.get(key) or "") for key in
              ("user_id", "conversation_id", "run_id", "session_id")}
    if all(fields.values()):
        session.binding = AppRunBinding(
            **fields,
            sandbox_attempt_id=str(binding.get("sandbox_attempt_id") or ""),
            sandbox_manifest=(
                dict(binding["sandbox_manifest"])
                if isinstance(binding.get("sandbox_manifest"), dict)
                else None
            ),
            sandbox_manifest_hash=str(binding.get("sandbox_manifest_hash") or ""),
        )
    if activity.state == "waiting_human":
        session.paused = True
        session.pause_id = str(activity.detail.get("pause_id") or "") or None
        session.offer_id = str(activity.detail.get("offer_id") or "") or None
    # 在飞那一轮的身份（P0-4 另一半，issue #785）：worker 自报 working / parked
    # 时 `turn_id` 就是它正在跑的那次 RPC 的 request_id。接回来只装 binding 而
    # 不记这个 id，等于知道"它在为谁跑"却不知道"它在跑哪一轮" —— 那一轮的
    # 后续事件与终止 result 就没有人接。判据看 **declared**（worker 自己说的），
    # 不看被租约衰减过的 state：一个跑着几小时实验、心跳静默过久的 worker
    # 会被读成 unknown，但它在跑的那一轮仍然是那一轮。
    if activity.declared in ("working", "parked") and activity.turn_id:
        session.inflight_request_id = str(activity.turn_id)


def _addressing():
    """地址规则的那一份（`core.worker_addressing`，经 harness_contract 桥）。"""
    from app.services.harness_contract import worker_addressing

    return worker_addressing()


class _ControlHandshakeRejected(HarnessSessionError):
    """连上了，但那头不认我们 —— 不是"还没起来"，重试没有意义。

    `code` 与"连不上"共用 `harness_worker_unreachable`：对用户来说这两件事是
    同一件 —— 平台没能和这一轮的执行进程接上话，重发会重新起一个。分成两个
    code 只会多一句同义的文案（`run_failures._COPY` 的加条判据）。
    """

    code = "harness_worker_unreachable"


#: 等 worker 回执的上限。握手是一次 readline，几毫秒的事；给足一秒是为了
#: 机器忙的时候不误判，但**必须有界**——没有界的等待就是把"连错了"熬成
#: "卡住了"，而后者在现场看不出成因。
_HANDSHAKE_TIMEOUT_S = 1.0

#: `op=terminate` 得到 `terminated` 之后，给 worker 自己退场的时间（存盘、摘转发面、
#: 收走 socket 文件）。超过就按老规矩 SIGTERM → SIGKILL。
_GRACEFUL_EXIT_WAIT_S = 5.0


async def _say_hello(reader, writer, *, spawn_token: str, address: str) -> None:
    """报出身份并**等一句回执**。

    以前这一步只在 TCP 上做，而且是**单向**的：写完 hello 就当连上了，从不看
    对面认不认。于是握手对不上时两边都不报错 —— 后端把 turn 写进一条马上要
    被关掉的连接，真正的 worker 一直没人接，60 秒宽限到点自己收摊，用户读到
    "执行进程中途退出了"，而事件流里一个字都没有（2026-09-15 现场）。

    单向的握手不是握手，是自言自语。

    这一句回执同时回答了另一个方向的问题：**路径不是身份**。命令面地址由
    (project, session) 算出来，每一代 worker 都绑同一个路径 —— 连"成功"只证明
    路径上有个 socket，可能是上一代还没退干净的监听。对面按自己的 token 认我们，
    认不出就 `hello_rejected`：我们据此知道"这不是我们 spawn 的那个"，不必再
    反过来盘问它一次（同一个问题两处各问一遍，迟早分叉）。
    """
    writer.write((json.dumps({"op": "hello", "spawn_token": spawn_token}) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=_HANDSHAKE_TIMEOUT_S)
    if not line:
        # EOF —— **不是拒绝**，是这根连接死了。上一代退场时它 backlog 里那些
        # 从没被 accept 过的连接就是这样被内核 reset 的（2026-09-15 node20）。
        # 拒绝要说出口才算数；没人说话的，换一根连接再问一次。
        raise ConnectionResetError(
            f"the control connection at {address} closed before answering the handshake"
        )
    try:
        answer = json.loads(line)
    except (ValueError, TypeError):
        answer = {}
    if not isinstance(answer, dict) or answer.get("type") != "hello_ok":
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        raise _ControlHandshakeRejected(
            f"Harness worker at {address} refused our identity "
            f"(spawn token {spawn_token[:8] or '<none>'}…): "
            f"{str(answer.get('reason') or line[:200])}"
        )


async def _connect_control_address(
    address: str,
    *,
    spawn_token: str = "",
    timeout_seconds: float = 5.0,
    still_starting: Callable[[], bool] | None = None,
    handshake: bool = True,
) -> _SocketChannel:
    """连上 worker 的命令面（unix socket 或 tcp 环回），等它绑好，**并核对它是谁**。

    地址带 scheme：裸路径 / `unix:` → AF_UNIX；`tcp:host:port` → TCP 环回。两种传输
    一份判据：连上先发一行 `{"op":"hello","spawn_token":…}` 并**等回执**
    （`_say_hello`）。

    这一道握手同时回答两个问题：worker 认不认我们（环回上任何本机进程都连得过来），
    以及**我们连到的是不是我们要的那一代**。后一问不是多余的 —— 命令面地址由
    (project, session) 算出来，每一代 worker 都绑同一个路径：上一代留下的死文件、
    上一代还没退干净的监听、它 backlog 里那条没人 accept 的连接，连"成功"都一样。
    路径不是身份，token 才是（2026-09-15 node20 session 822ee77f）。

    `handshake=False` 只给接回（`reattach_session`）里**换代之前的 worker**：它不认识
    hello 那一行，塞进去等于往命令流里注入一条它读不懂的请求。那条路的身份判据是注册表
    行 + 活动文件（worker 自己写的 pid / spawn_token）。

    spawn 之后 worker 要跑到 bind 才连得上 —— 这里必须等，但**有界**：等不到就是这个
    worker 起不来，如实报错，别让一个永远连不上的会话把请求挂死。`still_starting`
    返回 False = 进程已退出，别再等一个死人（它会把子进程 stderr 里真正有用的报错挤掉）。
    连上了但对面在窗口内一声不吭（上一代的 backlog 正是这个样子）→ 关掉重连，直到超时。
    """
    transport, endpoint = _addressing().parse_control_address(address)
    is_tcp = transport == _addressing().TRANSPORT_TCP
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last: Exception | None = None
    while loop.time() < deadline:
        writer: asyncio.StreamWriter | None = None
        try:
            if is_tcp:
                host, port = endpoint
                reader, writer = await asyncio.open_connection(
                    host, port, limit=PROTOCOL_LINE_LIMIT_BYTES
                )
            else:
                reader, writer = await asyncio.open_unix_connection(
                    str(endpoint), limit=PROTOCOL_LINE_LIMIT_BYTES
                )
            if handshake:
                await _say_hello(reader, writer, spawn_token=spawn_token, address=address)
            return _SocketChannel(reader, writer)
        except _ControlHandshakeRejected:
            # 连上了，但那头说"你不是 spawn 我的那个后端"。这不是"还没起来"，
            # 重试一万次都一样 —— 立刻抛，别把一次身份不符熬成 5 秒超时。
            raise
        except (FileNotFoundError, ConnectionRefusedError, OSError, TimeoutError) as exc:
            # 这里面包含"连上了但没人答"（`TimeoutError`）：上一代已经死在
            # `exit_mm` 里、accept 线程不在了，可监听 socket 要到 `exit_files`
            # 才关，内核照收我们的连接。断开重连，直到我们 spawn 的那个绑上来 ——
            # **每次都要把这根连接关掉**，否则一轮 5 秒会在它 backlog 里堆上百条。
            last = exc
            if writer is not None:
                with suppress(Exception):
                    writer.close()
        if still_starting is not None and not still_starting():
            break
        await asyncio.sleep(0.05)
    raise HarnessSessionError(
        f"Harness worker never exposed its control address at {address}: {last}",
        code="harness_worker_unreachable",
    )


class _ProjectHarnessSession:
    def __init__(
        self,
        *,
        project_id: str,
        session_id: str,
        owner_user_id: str,
        backend_id: str,
        backend_fingerprint: str,
        platform_context_hash: str | None,
        process: asyncio.subprocess.Process | None,
        stderr_task: asyncio.Task[str] | None,
        provider_secrets: Sequence[str],
        spawn_token: str = "",
        channel: _WorkerChannel | None = None,
        worker: _WorkerHandle | None = None,
    ) -> None:
        self.project_id = project_id
        self.session_id = session_id
        self.owner_user_id = owner_user_id
        self.backend_id = backend_id
        self.backend_fingerprint = backend_fingerprint
        self.platform_context_hash = platform_context_hash
        self.process = process
        self.stderr_task = stderr_task
        #: 子进程 stderr 的尾巴，`_kill` 收摊时定格 —— 起不来的 worker 临终说了什么，
        #: 由 spawn 它的那一方记进日志（`_new_session`）。
        self.stderr_tail = ""
        #: 中途死掉的 worker 的退出原因记进日志没有 —— 每次再问都会再算一遍，只记一次。
        self._exit_reason_logged = False
        # **全部**角色的凭据，不只是主模型那一把。单个字符串的时候，审图
        # key 出现在 stderr 里是照原样打出来的 —— 与 worker 侧那张擦除名单
        # 漏掉它是同一类缺陷：把"哪些是密钥"写成一个固定数量的东西。
        self._provider_secrets = [item for item in provider_secrets if item]
        #: 这次 spawn 的身份凭证（worker 会写进注册表）。P0-4 的 reattach
        #: 握手认它 —— pid 会复用、命令行会撞车，token 不会。
        self.spawn_token = spawn_token
        #: 与 worker 的收发面。socket = 后端可以死而复生；pipe = 老路。
        self.channel: _WorkerChannel = channel or _PipeChannel(process)
        #: worker 进程句柄。spawn 的有 asyncio 句柄，接回来的只有 pid。
        self.worker: _WorkerHandle = worker or _SpawnedWorker(process)
        self._operation_lock = asyncio.Lock()
        #: 多路复用（RFC D3 / P1-1）：**一个**读者把 worker 的每一行按
        #: request_id 分发给等它的那位。从前每次 RPC 自己读，于是同一时刻
        #: 只能有一个请求在飞 —— 管理面（status/stop/interject）因此永远排在
        #: 一轮几小时的对话后面，三个补偿机制都是为这件事存在的。
        self._reader: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Queue] = {}
        #: 当前会话面 RPC 的观众。worker 的事件是**会话级**的（transcript、
        #: token 流都不带请求归属），所以它们全部交给这一位 —— 管理面的回执
        #: 只走 `_pending` 那条路，不进摄取。
        self._conversation_sink: tuple[ProtocolCallback, ProtocolCallback] | None = None
        self._conversation_request_id: str = ""
        #: 接回来时 worker 正在跑的那次 RPC 的 request_id（activity.turn_id）。
        #: 空 = 接回时它闲着。非空 = 有一轮在飞而**本进程没有发起它**：它的事件
        #: 与终止 result 会从 socket 送来，得有人按这个 id 接（`rejoin`）。
        self.inflight_request_id: str = ""
        #: `rejoin` 只许接一次：第二个接的人会和第一个抢同一份终止事件。
        self._rejoined_request_ids: set[str] = set()
        self.binding: AppRunBinding | None = None
        self.paused = False
        self.pause_id: str | None = None
        #: 这次呈递的身份（`core.decision_offer.Offer.offer_id`）。与 pause_id
        #: 一起只从运行时的结果 / 自报里来，平台不自己编。
        self.offer_id: str | None = None
        self.closed = False
        #: 这个会话当下预授权的高危类别。真相源是库里的
        #: `project_configs.autonomous_authorized_risk_classes`，每次派发前由
        #: `_authorize` 从那次配置读取里刷新，`_rpc_locked` 挂在每一条请求上。
        self.authorized_risk_classes: list[str] = []
        #: 显式档位，随每条请求送到 worker。
        self.autonomy_mode: str = "assisted"

    def _authorize(self, autonomy: object | None) -> None:
        """把这次派发读到的授权范围记在会话上。

        单向刷新：`autonomy` 为 None（生命周期类操作，如 terminate）时保持
        原样，不把授权悄悄清掉 —— 收回授权的正规路径是 reset（换届）。
        """
        if autonomy is None:
            return
        self.authorized_risk_classes = list(
            getattr(autonomy, "authorized_risk_classes", ()) or ()
        )
        self.autonomy_mode = str(getattr(autonomy, "mode", "") or "assisted")

    @property
    def alive(self) -> bool:
        return not self.closed and self.worker.alive

    @property
    def conversation_in_flight(self) -> bool:
        """**本后端进程**有没有一次会话面 RPC 在飞 —— 从锁推导，不是一个布尔。

        ## 名字只回答一个问题（RFC D10 删除清单，2026-08-23）

        它原来叫 `busy`，而"忙"被拿去回答过三件事：谁拥有这个工作区、有没有
        计算在飞、这句话能不能送达。一个名字答三个问题，就是 8-21 那次事故的
        形状（两个真相源方向相反地各错一边，而分叉时没有任何一层报错）。

        现在三件事各有各的名字：所有权 = flock；活动 = worker 自报
        （`core.worker_activity`，跨进程、带心跳租约）；可寻址 = 恒真，不需要
        任何判据。**本属性只答第三种之外的那一小块**：这个后端进程手里有没有
        一次会话面 RPC 在飞。它答不了"worker 在不在算"（接回来的会话锁是空的
        而 worker 正跑着），那个问题问 `manager.is_occupied()`。

        它**永远不作为拒收理由**。占用是排队的理由，不是把话弹回去的理由。

        ## 为什么不能是标志位（wangd 2026-08-18 实测事故）

        原来 `busy` 是内存里的 bool：`_get_or_create` 置 True，`turn()` 的
        finally 清 False。它的正确性靠"每条路径都记得清"来维持，而那是维持
        不住的 —— 实测一次：worker 既没回 result 也没报错就回去等下一条请求，
        后端永远堵在读它 stdout 上（turn 的 RPC 故意没有超时：一次科研轮次
        可以跑几小时，把墙钟当取消理由是错的）。平台随后把那条 run 判成
        stale，`busy` 却还挂着 —— 这个会话被锁死到重启后端为止，用户每发一句
        「继续吧」都撞 "session is busy"。

        锁不会有这个问题：它由 `async with` 持有，return / raise / 取消
        都必然释放。让声明**就是**那把锁，"忘了清"这类 bug 从此不存在。

        窗口期（拿到 session 到进入 turn 之间）由 `claim()` 关掉：占用在
        注册表锁里一次性取得，由同一个 `finally` 释放。
        """
        return self._operation_lock.locked()

    @asynccontextmanager
    async def claim(self):
        """占住这个会话做一次操作 —— 取得和释放在同一个 with 里。"""
        await self._operation_lock.acquire()
        try:
            yield self
        finally:
            self._operation_lock.release()

    def _redact(self, text: str) -> str:
        """把**任何一个**角色凭据从要交给人看的文本里抹掉。

        遍历而不是取一个：新增角色自动被覆盖，不靠"记得再加一句 replace"。
        """
        for secret in self._provider_secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    async def _emit_progress(self, event: dict, callback: ProtocolCallback) -> None:
        progress = protocol_user_progress(event)
        if progress is not None:
            await callback(progress)

    #: 通道先于回答断掉时，等子进程退出码的上限。退出码是诊断的一部分（它带着
    #: stderr），但一个还活着的进程可以很久不退出 —— 2026-09-15 node20：连接连进了
    #: 上一代 worker 的 backlog、随即被 reset，这里 `process.wait()` 等了 60 秒才等到
    #: **新** worker 因"没人连"自行收摊，报出来的是"进程退出了（exit 0）"：时间和原因
    #: 都指错。通道断了就说通道断了；进程还活着就说它还活着（随后 `_kill` 收掉）。
    _EXIT_CODE_WAIT_S = 5.0

    async def _process_exit_error(self) -> HarnessSessionProcessError:
        """Say why the reply never came: the child exited (with its stderr), or the
        control connection died while the child is still alive."""
        try:
            exit_code = await asyncio.wait_for(
                self.process.wait(), timeout=self._EXIT_CODE_WAIT_S
            )
        except TimeoutError:
            return HarnessSessionProcessError(
                f"Harness worker (pid {self.process.pid}) is still running, but its "
                "control connection closed before it replied",
                exit_code=None,
                code="harness_worker_unreachable",
            )
        stderr = self._redact(await self.stderr_task).strip()
        message = f"Harness runtime process exited before replying (exit code {exit_code})"
        if stderr:
            message = f"{message}: {stderr[-2000:]}"
        if not self._exit_reason_logged:
            # 这条原因从前只拼进那一轮的失败信息里，后端日志里一个字都没有 ——
            # 起不来的 worker 有人记（`_new_session`），跑到一半死掉的没人记。
            # 带上会话 id：诊断包和人按会话找日志，找的就是这个。
            self._exit_reason_logged = True
            logger.error(
                "Harness worker for %s/%s exited mid-session (exit code %s); "
                "worker stderr tail: %s",
                self.project_id, self.session_id, exit_code, stderr[-2000:] or "<empty>",
            )
        return HarnessSessionProcessError(message, exit_code=exit_code)

    # ── 多路复用的读者（RFC D3 / P1-1）──────────────────────────────────────
    #
    # 从前每次 RPC 自己读 worker 的输出，读到自己的终止事件为止。那意味着
    # **同一时刻只能有一个请求在飞**：一轮跑几小时，期间 status 问不到、
    # stop 送不进、插话只能绕道文件收件箱。今天的三个补偿机制都是为这一件
    # 事存在的（收件箱、待命轮、按 request_id 丢弃不匹配事件）。
    #
    # 现在一个会话一个读者，按 request_id 分发。turn 仍然单飞（`_operation_lock`）
    # —— 那是产品语义（一个 session 一条对话），不是这层的限制。

    _EOF = object()

    def _ensure_reader(self) -> None:
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._read_forever())

    async def _read_forever(self) -> None:
        """读一行 → 交给会话面观众 → 路由给等它的那位。

        读者只有这一个，所以"两个并发 RPC 互相偷事件"这件事从结构上消失了
        （那正是文件收件箱当初存在的理由）。
        """
        try:
            while True:
                line = await self.channel.read_line()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # 一行坏的不该把整条通道带走：它是转发面的损坏，事实面
                    # （events.jsonl）里那一条是好的，恢复时补得回来。
                    logger.warning("Harness session emitted invalid JSONL; skipping one line")
                    continue
                if not isinstance(event, dict):
                    continue
                sink = self._conversation_sink
                if sink is not None and event.get("request_id") not in self._management_ids():
                    on_progress, on_protocol_event = sink
                    await on_protocol_event(event)
                    await self._emit_progress(event, on_progress)
                queue = self._pending.get(str(event.get("request_id") or ""))
                if queue is not None:
                    queue.put_nowait(event)
        finally:
            # 通道没了：把还在等的全部叫醒，别让它们等一个不会来的回答。
            for queue in list(self._pending.values()):
                queue.put_nowait(self._EOF)

    def _management_ids(self) -> set[str]:
        """此刻在飞的**管理面**请求 id。它们的回执不进摄取。"""
        conversation = self._conversation_request_id
        return {rid for rid in self._pending if rid != conversation}

    async def _rpc_locked(
        self,
        payload: dict,
        *,
        terminal_types: set[str],
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
        timeout_seconds: float | None,
        conversation: bool = True,
        include_binding: bool = True,
    ) -> dict:
        if not self.alive or not getattr(self.channel, "usable", False):
            # 进程已经退出、而它的 stderr 还在手上 → 报**为什么**退出的那条，
            # 不是一句通用的"不在了"。用户看到 "No module named platform_runtime"
            # 能自己解决；看到"进程不在了"只能来问人。
            exit_error: HarnessSessionProcessError | None = None
            if self.process is not None and self.stderr_task is not None:
                # 先**算**再抛：算的过程可能自己出错（管道已关之类），那种情况
                # 退回通用话；但算出来的那条必须真的抛出去，不能被兜底吞掉。
                with suppress(Exception):
                    exit_error = await self._process_exit_error()
            if exit_error is not None:
                raise exit_error
            raise HarnessSessionStaleError("The project Harness process is no longer alive")
        request_id = str(payload["request_id"])
        # 授权范围与派活方绑定是**会话属性**，不是某个 op 的参数 —— 所以在唯一
        # 的收发口上挂，而不是让每个 op 各自记得带。
        #
        # 2026-08-13 实测的死循环：只有 `run_unattended` 带授权字段，`answer`
        # 不带。于是一个空授权起跑的 run 停在高危点上，事后把项目改成「连续」
        # 再答复也没用 —— worker 手里仍是出发时那份空的。逐个 op 补字段是
        # "护栏写成名单"：明天加第四个 op，它默认又漏。
        payload = {
            **payload,
            "authorized_risk_classes": list(self.authorized_risk_classes),
            "autonomy_mode": self.autonomy_mode,
        }
        if include_binding and self.binding is not None:
            # worker 把它写进自己的活动文件 —— 那是**后端重启后**重建"这条 run
            # 还有主"的唯一依据。绑定原来只活在这个进程的内存里，进程一换就没了，
            # 于是刚接回来的 worker 会被启动对账当成无主进程杀掉。
            payload["app_binding"] = {
                "user_id": self.binding.user_id,
                "conversation_id": self.binding.conversation_id,
                "run_id": self.binding.run_id,
                "session_id": self.binding.session_id,
                "sandbox_attempt_id": self.binding.sandbox_attempt_id,
                "sandbox_manifest": self.binding.sandbox_manifest,
                "sandbox_manifest_hash": self.binding.sandbox_manifest_hash,
            }

        async with self._awaiting(
            request_id, on_progress, on_protocol_event, conversation=conversation
        ) as queue:
            await self.channel.send_line(json.dumps(payload, ensure_ascii=False) + "\n")
            return await self._await_terminal(
                queue, terminal_types=terminal_types, timeout_seconds=timeout_seconds
            )

    @asynccontextmanager
    async def _awaiting(
        self,
        request_id: str,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
        *,
        conversation: bool,
    ):
        """登记"我在等 `request_id` 的回答"：队列 + （会话面）观众，退出时一并摘掉。

        发起 RPC（`_rpc_locked`）和接回在飞那一轮（`rejoin`）共用这一份登记 ——
        两者的差别只在"这条请求是不是本进程发出去的"，而读者按 request_id 分发
        这件事对两者是同一件事。抄两份就会各自演化（一份忘了摘 sink），而分叉
        时没有任何一层报错。
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._pending[request_id] = queue
        if conversation:
            self._conversation_request_id = request_id
            self._conversation_sink = (on_progress, on_protocol_event)
        self._ensure_reader()
        try:
            yield queue
        finally:
            self._pending.pop(request_id, None)
            if conversation and self._conversation_request_id == request_id:
                self._conversation_sink = None
                self._conversation_request_id = ""

    async def _await_terminal(
        self,
        queue: asyncio.Queue,
        *,
        terminal_types: set[str],
        timeout_seconds: float | None,
    ) -> dict:
        async def read_terminal() -> dict:
            while True:
                event = await queue.get()
                if event is self._EOF:
                    # 通道没了。是我们 spawn 的、句柄还在手上 → 报它**为什么**退出；
                    # 接回来的（只有 pid、没有句柄）→ 如实说连接丢了。此前这里对
                    # 后者会去 `self.process.wait()` 一个 None，rejoin 死在
                    # AttributeError 上而不是一句干净的 stale（#785）。
                    if self.process is None or self.stderr_task is None:
                        raise HarnessSessionStaleError(
                            "The Harness worker connection was lost before it replied"
                        )
                    raise await self._process_exit_error()
                if event.get("type") == "error":
                    message = event.get("message") or event.get("error") or "Harness RPC failed"
                    # Carry the runtime's declared identity across, not just
                    # its prose. Dropping `code` here collapsed every distinct
                    # RequestError (`pause_pending`, `invalid_request`, …) into
                    # one untyped failure, and the only thing left to classify
                    # on was an English sentence.
                    details = event.get("details")
                    raise HarnessSessionError(
                        self._redact(str(message)),
                        code=str(event.get("code") or ""),
                        error_type=str(event.get("error_type") or ""),
                        details=details if isinstance(details, dict) else None,
                    )
                if event.get("type") in terminal_types:
                    return event

        if timeout_seconds is None:
            # A scientific turn can legitimately run for hours or days.  Its
            # lifetime belongs to the durable Run and must only end through a
            # terminal Harness result, an explicit user cancellation, process
            # failure, or App Server shutdown.  In particular, never turn a
            # wall-clock/RPC deadline into an implicit Run cancellation.
            return await read_terminal()
        try:
            return await asyncio.wait_for(read_terminal(), timeout=timeout_seconds)
        except TimeoutError as exc:
            await self._kill()
            raise HarnessSessionStaleError("Harness control operation timed out") from exc

    async def rejoin(
        self,
        *,
        request_id: str,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
        before_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> dict:
        """接回**别的后端**发出去、此刻仍在飞的那一轮（P0-4 另一半，issue #785）。

        与 `turn` / `answer` 只有一个差别：请求不由本进程发出。其余一模一样 ——
        同一份登记（队列 + 会话面观众）、同一份终止等待、同一份 pause 记账。
        worker 那头（`SocketRequestSource._adopt`）已经把转发面换到我们这根
        socket 上，所以这一轮**之后**的事件（含带同一 request_id 的终止 result）
        会实时送来；断连到重连之间那段，调用方在 `before_wait` 里按水位补
        （它在观众登记**之后**跑，于是两段之间没有缝）。

        只许接一次：第二个接的人会和第一个抢同一份终止事件。占用由调用方的
        `claim()` 持有，与 turn / answer 同一规矩。
        """
        if request_id in self._rejoined_request_ids:
            raise HarnessSessionError(
                f"Harness turn {request_id} is already being rejoined", code="already_rejoined"
            )
        if not self.alive or not getattr(self.channel, "usable", False):
            raise HarnessSessionStaleError("The project Harness process is no longer alive")
        self._rejoined_request_ids.add(request_id)
        async with self._awaiting(
            request_id, on_progress, on_protocol_event, conversation=True
        ) as queue:
            if before_wait is not None:
                await before_wait()
            terminal = await self._await_terminal(
                queue, terminal_types={"result"}, timeout_seconds=None
            )
        result = terminal.get("data")
        if not isinstance(result, dict):
            raise HarnessSessionError("Harness turn result is missing data")
        await self._apply_result_pause(result)
        self.inflight_request_id = ""
        return {**result, "_session_resumable": self.paused}

    async def initialize(
        self,
        *,
        home_dir: Path,
        org_home: Path,
        projects_home: Path,
        state_dir: Path,
        workspace_dir: Path,
        platform_context_snapshot: dict | None,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
    ) -> None:
        async with self._operation_lock:
            payload = {
                "op": "init",
                "request_id": f"init-{uuid4().hex}",
                "tenant_id": settings.runtime_tenant_id,
                "project_id": self.project_id,
                "session_id": self.session_id,
                "home_dir": str(home_dir),
                # 这一轮读写哪个组织的知识 —— 在请求里说，不靠 worker 的环境（`_temporary_home`）。
                "org_home": str(org_home),
                # 这个项目的项目层（KB、记忆、作业账本）—— 一个项目一份，谁的会话写的都进这一份。
                "projects_home": str(projects_home),
                "state_dir": str(state_dir),
                "workspace_dir": str(workspace_dir),
            }
            if platform_context_snapshot:
                payload["platform_context"] = platform_context_snapshot
            ready = await self._rpc_locked(
                payload,
                terminal_types={"ready"},
                on_progress=on_progress,
                on_protocol_event=on_protocol_event,
                timeout_seconds=settings.harness_timeout_seconds,
            )
            if int(ready.get("sandbox_protocol_version") or 0) != SANDBOX_PROTOCOL_VERSION:
                await self._kill()
                raise HarnessSessionError(
                    "Harness worker does not implement the required RunAttempt sandbox protocol"
                )

    async def turn(
        self,
        *,
        binding: AppRunBinding,
        message: str,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
        unattended: bool = False,
        max_turns: int = 200,
    ) -> dict:
        """一次平台操作。

        `unattended=True` 时改用 `op=run_unattended`：一次 RPC 跑到真正做完
        （或需要人）为止，续轮发生在 harness 进程**内部**。

        为什么这不是"又加一个模式"：UI 上的 Autonomous 开关本来就传到了这里，
        但它此前只接通了"自动批准"（HARNESS_AUTO_APPROVE），没有接通"接着往下
        走"——于是 autonomous 名不副实，每一轮仍要外部推一把。实测后果：这几轮
        E2E 全靠外挂脚本喂，而那个脚本按 DB 缓存态判断，误标过正在干活的 run。
        接上之后平台第一次真正拥有无人值守驱动。
        """
        # 授权范围不在这里逐个 op 拼 —— 它挂在 `_rpc_locked`，每条请求都带。
        op_payload: dict = (
            {
                "op": "run_unattended",
                "request_id": f"unattended-{uuid4().hex}",
                "message": message,
                "max_turns": max_turns,
            }
            if unattended
            else {"op": "turn", "request_id": f"turn-{uuid4().hex}", "message": message}
        )
        # 占用由调用方的 `claim()` 持有 —— 这里不再自己加锁、也没有标志要清。
        if self.paused:
            raise HarnessSessionError("The project is waiting for an answer")
        self.binding = binding
        terminal = await self._rpc_locked(
            op_payload,
            terminal_types={"result"},
            on_progress=on_progress,
            on_protocol_event=on_protocol_event,
            timeout_seconds=None,
        )
        result = terminal.get("data")
        if not isinstance(result, dict):
            raise HarnessSessionError("Harness turn result is missing data")
        await self._apply_result_pause(result)
        return {**result, "_session_resumable": self.paused}

    async def deliver(self, *, kind: str, text: str, author: str = "user",
                      message_id: str = "") -> dict:
        """把一句话送给正在跑的那一轮（P1-2：文件收件箱的归宿）。

        ## 为什么它不需要拿 `_operation_lock`

        这是**管理面**：worker 侧收下即回执，不碰对话状态（D3）。多路复用
        之后它可以与飞行中的 turn 并发 —— 那正是这条路存在的全部理由。
        从前它做不到，所以才有了文件收件箱那个补偿机制。

        ## 「可寻址恒真」（D10）

        这个方法**没有被拒分支**。回执里带着 worker 当下的占用状态：收下 ≠
        马上被处理，两句都要说。不许黑洞 —— 入队黑洞比一个响亮的 409 更糟，
        409 至少是响的。
        """
        op = "stop" if kind == "stop" else "interject"
        terminal = await self._rpc_locked(
            {
                "op": op,
                "request_id": f"{op}-{uuid4().hex}",
                "text": text,
                "author": author,
                "message_id": message_id,
            },
            terminal_types={"accepted"},
            on_progress=_noop_callback,
            on_protocol_event=_noop_callback,
            timeout_seconds=10.0,
            conversation=False,
        )
        return {
            "delivered": True,
            "item_id": str(terminal.get("item_id") or ""),
            "occupancy": str(terminal.get("occupancy") or ""),
        }

    async def sync_scope(self, *, include_binding: bool = True) -> list[str]:
        """把这个会话**当下**的档位推给 worker，返回它施加之后自报的档位。

        走管理面（不拿 `_operation_lock`）—— 它必须能与飞行中的一轮并发，
        因为需要它的时刻恰恰就是"一轮正跑着，人改了设置"。

        worker 侧 `sync` 自己什么都不做：档位随**每条**请求挂在必经点上，
        这条请求存在的意义就是让那个必经点被走一次（见 `_handle_management_op`）。
        """
        terminal = await self._rpc_locked(
            {
                "op": "sync",
                "request_id": f"sync-{uuid4().hex}",
            },
            terminal_types={"accepted"},
            on_progress=_noop_callback,
            on_protocol_event=_noop_callback,
            timeout_seconds=10.0,
            conversation=False,
            include_binding=include_binding,
        )
        applied = terminal.get("authorized_risk_classes")
        return [str(item) for item in applied] if isinstance(applied, list) else []

    async def reset_conversation(self) -> dict:
        """清对话历史。只在**空闲**时可用。

        与 CLI 同一条约束：它清的是正在被使用的 messages 与 hook_state，
        跑轮中清等于把 agent 的记忆从它手里抽走。`_operation_lock` 在这里既是
        互斥也是判据 —— 拿不到就说明有活在跑，如实拒绝而不是排队等。
        """
        if self._operation_lock.locked():
            raise HarnessSessionError("这个 Session 正在跑，跑完再重置对话")
        async with self._operation_lock:
            if self.paused:
                raise HarnessSessionError("有等待回答的暂停，先答复再重置")

            async def _noop(_event: dict) -> None:
                return None

            terminal = await self._rpc_locked(
                {"op": "reset", "request_id": f"reset-{uuid4().hex}"},
                terminal_types={"result"},
                on_progress=_noop,
                on_protocol_event=_noop,
                timeout_seconds=60.0,
            )
            data = terminal.get("data")
            return data if isinstance(data, dict) else {"status": "completed"}

    async def answer(
        self,
        *,
        binding: AppRunBinding,
        answer: str,
        choice: dict | None = None,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
    ) -> dict:
        # 占用由调用方的 `claim()` 持有 —— 同 turn：取得和释放在一个 with 里，
        # 这里不再自己加锁，也没有标志要清。
        if (
            not self.paused
            or self.binding is None
            or self.binding.identity != binding.identity
            or not self.pause_id
        ):
            raise HarnessSessionStaleError(
                "No addressable live pause is bound to this user, conversation, and Run"
            )
        answered = str((choice or {}).get("offer_id") or "").strip()
        if answered and self.offer_id and answered != self.offer_id:
            raise HarnessAnswerSupersededError(answered=answered, live=self.offer_id)
        consumed = (self.pause_id, self.offer_id)
        # 答复一派发，这个 pause 就被消费了 —— 同一张卡不可能被答两次。此前这里
        # 要等结果回来才翻标志，于是 RPC 在飞的几分钟里 `paused` 仍是 True：第二个
        # 答复过了入口的闸、在锁上排队，等到锁空时 pause 已经换成下一张，它带着
        # 旧 offer 被运行时拒掉，人看到的是空气泡加同一张卡（2026-09-09 node20）。
        # worker 没收下（拒了 / 传输炸了）就不算消费：它手里那个 pause 还在，还回去。
        self.paused = False
        self.pause_id = None
        self.offer_id = None
        try:
            terminal = await self._rpc_locked(
                {
                    "op": "answer",
                    "request_id": f"answer-{uuid4().hex}",
                    "pause_id": consumed[0],
                    "answer": answer,
                    # 人点的那个按钮的**身份**。带上它，harness 侧判定答复是一次
                    # 集合成员检查；不带才回落到按文案解析 —— 而文案解析正是
                    # 2026-08-19 静默丢弃人工授权的那条路。
                    **({"choice": choice} if choice else {}),
                },
                terminal_types={"result"},
                on_progress=on_progress,
                on_protocol_event=on_protocol_event,
                timeout_seconds=None,
            )
        except BaseException:
            self.pause_id, self.offer_id = consumed
            self.paused = True
            raise
        result = terminal.get("data")
        if not isinstance(result, dict):
            raise HarnessSessionError("Harness answer result is missing data")
        await self._apply_result_pause(result)
        return {**result, "_session_resumable": self.paused}

    async def _apply_result_pause(self, result: dict) -> None:
        """Atomically update the CAS token carried by a terminal result."""
        if result.get("status") != "paused":
            self.paused = False
            self.pause_id = None
            self.offer_id = None
            self.binding = None
            return
        pause_event = result.get("pause_event")
        event_pause_id = (
            str(pause_event.get("pending_tool_call_id") or "").strip()
            if isinstance(pause_event, dict)
            else ""
        )
        result_pause_id = str(result.get("pause_id") or "").strip()
        if not event_pause_id or (result_pause_id and result_pause_id != event_pause_id):
            await self._kill()
            raise HarnessSessionStaleError(
                "Harness returned a paused result without a consistent addressable pause_id"
            )
        offer = pause_event.get(PAUSE_OFFER_KEY) if isinstance(pause_event, dict) else None
        self.offer_id = (
            str(offer["offer_id"])
            if isinstance(offer, dict) and str(offer.get("offer_id") or "").strip()
            else None
        )
        self.pause_id = event_pause_id
        self.paused = True

    async def terminate(self) -> None:
        try:
            await asyncio.wait_for(self._operation_lock.acquire(), timeout=5)
        except TimeoutError:
            await self._kill()
            return
        try:
            if self.closed:
                return
            if self.alive:
                noop: ProtocolCallback = _noop_callback
                try:
                    await self._rpc_locked(
                        {"op": "terminate", "request_id": f"terminate-{uuid4().hex}"},
                        terminal_types={"terminated"},
                        on_progress=noop,
                        on_protocol_event=noop,
                        timeout_seconds=settings.harness_timeout_seconds,
                    )
                except HarnessSessionError:
                    pass
                else:
                    # 它答应退场了 —— 让它自己走完：存盘、摘转发面、**收走自己的
                    # socket 文件**。紧接着就发 SIGTERM（从前的写法）会把它杀在半路：
                    # 文件留在盘上，监听 socket 在它 `exit_files` 之前还开着；而这边
                    # 按"命令行还像不像 worker"判它已死、立刻 spawn 并连**同一个**
                    # 地址 —— 连进的是垂死那个的 backlog（2026-09-15 node20，
                    # session 822ee77f：新 worker 没人连、60 秒后自行收摊，用户那一轮
                    # 落成 "exited before replying (exit code 0)"）。
                    await self.worker.wait_exit(_GRACEFUL_EXIT_WAIT_S)
            await self._kill()
        finally:
            self._operation_lock.release()

    async def _kill(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.channel.close()          # socket 关掉 = worker 那头读到 EOF
        self.worker.signal_stop()     # 接回来的进程不是我们的子进程，得显式发信号
        if self.worker.alive and not await self.worker.wait_exit(5):
            self.worker.force_kill()
            await self.worker.wait_exit(5)
        if self.stderr_task is not None:
            self.stderr_tail = self._redact(await self.stderr_task)
        self._provider_secrets = []

    async def detach(self) -> None:
        """放手，不收摊：本**后端**退场，会话不退场（RFC 异步运行时 P0）。

        与 `terminate()` 是两件事，别用一条规则处理：
          · terminate —— 这个**会话**结束（登出、换绑定 respawn、进程已死清账）：
            发 op=terminate 让 worker 存盘退场，退不了就杀。
          · detach —— 这个**后端**结束，会话不结束：只把我们这头的收发面摘掉。
            socket 那头读到 EOF 就把转发面摘掉、回到 accept 上等下一个后端
            （事件照落 events.jsonl；新后端按注册表接回来、按水位补齐）。

        ## 为什么要有这个方法（2026-09-03 node20 部署 0c9e557）

        lifespan 的收尾段一直调的是 `terminate_all()` —— 8-03 写的，那时 worker
        是 stdin 子进程，后端一死管道 EOF 它本来就跟着走，terminate 只是让它体面
        一点。P0（8-18~8-23）让 worker 自成进程组、命令面走 socket、事件落盘，
        部署脚本据此承诺"不动 worker"，而这一行没人回头看：每次部署，停靠中的
        worker 都被自己人 terminate 掉（session b4ebcffe 的 events.jsonl 最后
        一条 `terminated reason=terminate`，与 backend.log 的 Shutting down 同一
        秒）。承诺只在零停靠 worker 时成立，脚本收尾如实报红。

        不等 `_operation_lock`：在飞的 RPC 已被 `shutdown_detached_executions`
        取消；就算还有，等它跑完也不是这一步的事 —— 转发面消失既不该拖住关机，
        也不该销毁研究。老 stdio worker（`_PipeChannel`）没有可接回的命令面：
        关掉 stdin 就是 EOF，它跑完队列自己退场（P0-3），这里同样不杀。
        """
        if self.closed:
            return
        self.closed = True
        self.channel.close()        # socket: worker 读到 EOF → 摘转发面、回 accept
        self.worker.release()       # 我们这头的管道关掉；asyncio 从此不再替我们杀它
        for task in (self._reader, self.stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader = None
        self.stderr_task = None
        # 放手之后不再持有句柄：谁再问"它为什么退出"（`_process_exit_error`
        # 要 `process.wait()`）都会等一个**不会退出**的进程。没有句柄就只剩
        # 一句如实的"不在本进程手里"。
        self.process = None
        self._provider_secrets = []


async def _noop_callback(_event: dict) -> None:
    return None


class HarnessSessionManager:
    """In-memory one-process-per-Research-Session registry for the local App Server."""

    def __init__(self) -> None:
        self._sessions: dict[str, _ProjectHarnessSession] = {}
        self._cancelled_run_ids: set[str] = set()
        self._registry_lock = asyncio.Lock()
        self._stale_binding_handler: (
            Callable[[AppRunBinding], Awaitable[None]] | None
        ) = None
        self._adoption_handler: Callable[[_ProjectHarnessSession], None] | None = None

    def set_adoption_handler(
        self, handler: Callable[[_ProjectHarnessSession], None] | None
    ) -> None:
        """接回一个活 worker 之后，把它交给谁去**接上它正在跑的那一轮**（#785）。

        接回本身只恢复"它在为谁跑"（binding）；在飞那一轮的事件与终止 result
        还得有人按 request_id 去接，并在这个进程里重跑一遍收尾（run 终态 /
        pause 呈递 / 交付）。那是执行层的事（`local_execution.rejoin_adopted_session`），
        管理器不认识 DB 行，所以只留一个钩子。生产在 main.lifespan 接；不接
        不是错误，只是退化回 #784 的状态：worker 活着、那一轮的账等下次重启补。

        钩子是**同步**的、且在注册表锁里被调：它只该起一个后台任务，不许 await。
        """
        self._adoption_handler = handler

    def set_stale_binding_handler(
        self, handler: Callable[[AppRunBinding], Awaitable[None]] | None
    ) -> None:
        """respawn 送走一个停靠中的 pause 时，用它把那条 run 立刻标成可恢复。

        生产环境在 main.lifespan 接 `mark_orphaned_harness_runs` —— 与登出
        （auth.logout）同一条处理路。不接的话不是错误，只是退化：下一条消息
        会先撞一次 HarnessSessionStaleError 再由既有恢复路自愈。
        """
        self._stale_binding_handler = handler

    @staticmethod
    def _key(project_id: str, session_id: str) -> str:
        return f"{settings.runtime_tenant_id}:{project_id}:{session_id}"

    async def _new_session(
        self,
        *,
        user: User,
        project_id: str,
        session_id: str,
        role_bindings: RoleBindings,
        platform_context_snapshot: dict | None,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
    ) -> _ProjectHarnessSession:
        backend = reasoning_backend(role_bindings)
        root, home_dir, session_state, workspace_dir, python = _paths(
            user, project_id, session_id
        )
        # spawn token：这一次 spawn 的身份凭证，worker 写进注册表（session
        # 锁记录）。pid 会被复用、命令行会撞车 —— 将来 reattach（P0-4）握手
        # 认的是它，不是"看起来像我们的进程"。
        spawn_token = uuid4().hex
        child_env = _child_environment(root, role_bindings)
        child_env["HARNESS_SPAWN_TOKEN"] = spawn_token
        control_address = _control_address(project_id, session_id)
        if control_address is not None:
            child_env["HARNESS_CONTROL_SOCKET"] = control_address
        process = await _spawn_child_owned_by_the_os(
            [python, "-m", "platform_runtime", "--serve"],
            cwd=str(root),
            env=child_env,
            limit=PROTOCOL_LINE_LIMIT_BYTES,
            # detach（RFC 异步运行时 P0）：worker 自成会话/进程组，后端的
            # 信号（Ctrl-C 打给前台进程组、uvicorn 关停给自己组的 SIGTERM）
            # 不再连坐正在跑几小时研究的 worker。父子关系不变 —— 本进程仍
            # 能 wait/kill 这个 handle；stdio 管道也照常（P0-2 换 socket 前
            # 协议不动）。后端死了管道 EOF，worker 存盘退出，与现状一致。
            **harness_module("shared.lib.process_control").detached_spawn_kwargs(),
        )
        session = _ProjectHarnessSession(
            project_id=project_id,
            session_id=session_id,
            owner_user_id=user.id,
            backend_id=backend.id,
            backend_fingerprint=_backend_fingerprint(role_bindings),
            platform_context_hash=_snapshot_hash(platform_context_snapshot),
            process=process,
            stderr_task=asyncio.create_task(_drain_stderr(process.stderr)),
            provider_secrets=[resolved_api_key(item) or "" for item in role_bindings.values()],
            spawn_token=spawn_token,
        )
        started = asyncio.get_running_loop().time()
        try:
            if control_address is not None:
                # 地址是**具体**的（unix 路径 / tcp 具体端口，都由后端定下来交给 worker），
                # 所以这里没有「先问出地址」这一步，直接连 —— 连上之后要过身份握手
                # （`_say_hello`）：同一个地址上一代也绑过，路径不是身份。这个 worker
                # 是本 release spawn 的，一定握手。
                #
                # **不再退回管道。** 那条回退是 D8 drain 换代期的东西（老 worker 不认
                # HARNESS_CONTROL_SOCKET、照旧读 stdin），换代早已结束；而它正是把「连不上」
                # 变成「静默死锁」的那一段 —— 后端往 stdin 写、worker 在 socket 上等，
                # 两边都不报错，会话永远停在"正在启动"（2026-08-19 node20 实测）。
                #
                # 新 worker 绑不上会**当场退出**（`control_socket_unavailable`），所以
                # 连不上就是连不上：如实报错，把 worker 的 stderr 交出去。stdio 仍然是
                # 一种**配置出来的模式**（`harness_control_socket` 关掉时 `control_address`
                # 为 None），只是不再当回退用。
                #
                # 连接失败也在这个 try 里：从前它在外面，连不上就把一个活着的 worker
                # 留在那儿攥着会话锁（直到它 60 秒后自己收摊）—— 用户马上重发一次撞的
                # 是 project_busy，而那个"别人"是我们自己刚起的。
                session.channel = await _connect_control_address(
                    control_address, spawn_token=spawn_token,
                    still_starting=lambda: process.returncode is None,
                )
            await session.initialize(
                home_dir=home_dir,
                # org 层是**这个组织**的（`config.the_org_home`）：项目的成员都在项目
                # 所在的组织里（加成员时按组织核过），所以就是说话这个人的组织。
                org_home=the_org_home(user.institution_id),
                projects_home=the_projects_home(),
                state_dir=session_state,
                workspace_dir=workspace_dir,
                platform_context_snapshot=platform_context_snapshot,
                on_progress=on_progress,
                on_protocol_event=on_protocol_event,
            )
        except BaseException as exc:
            await session._kill()
            # 起不来的 worker 临终说了什么，只有 spawn 它的这一方看得见（stderr 管道在
            # 我们手里）。写进日志，别让"exited before replying"成为唯一的线索。
            logger.error(
                "Harness worker for %s/%s did not come up (%s after spawn): %s "
                "[control address %s, spawn token %s…, pid %s, exit code %s]; "
                "worker stderr tail: %s",
                project_id, session_id,
                f"{asyncio.get_running_loop().time() - started:.1f}s",
                exc, control_address or "<stdio>", spawn_token[:8], process.pid,
                process.returncode, session.stderr_tail.strip()[-500:] or "<empty>",
            )
            raise
        return session

    async def _get_or_create(
        self,
        *,
        user: User,
        project_id: str,
        session_id: str,
        role_bindings: RoleBindings,
        platform_context_snapshot: dict | None,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
    ) -> _ProjectHarnessSession:
        key = self._key(project_id, session_id)
        reasoning_backend(role_bindings)  # 缺主模型立刻报，别拖到 spawn
        fingerprint = _backend_fingerprint(role_bindings)
        platform_context_hash = _snapshot_hash(platform_context_snapshot)
        async with self._registry_lock:
            existing = self._sessions.get(key)
            if existing is None:
                # 注册表空缺 ≠ 没有 worker：上一代后端放手的那个可能还在 accept
                # 上等（见 `_adopt_locked`）。先接，接不到再 spawn。
                existing = await self._adopt_locked(
                    project_id, session_id, owner_user_id=user.id
                )
            if existing and not existing.alive:
                self._sessions.pop(key, None)
                existing = None
            if existing:
                # 两种绑定一起比：模型指纹 / 平台上下文。任何一种漂了，对活
                # 进程的解法都一样 —— 换进程；分开各写一个 raise 的年代
                # （2026-08-21 之前），每一种漂移都是一个只有报错没有出路的死角。
                #
                # **指令不在这里比。** 它曾经在：指令被烤进 worker 的进程环境，
                # 改一个字就得换进程。现在 worker 每轮自己读文件，改了下一轮就
                # 生效 —— 少一种「因为哈希变了所以杀掉正在跑的研究」。
                same_bindings = (
                    existing.backend_fingerprint == fingerprint
                    and existing.platform_context_hash == platform_context_hash
                )
                decision = worker_reuse_decision(
                    same_bindings=same_bindings,
                    # 锁 **或** worker 自报（与 `is_occupied` 同一份判据）：接回来的
                    # 会话锁在 worker 手里不在我们手里，只问锁会把一次正跑着的
                    # 研究判成空闲 → respawn → terminate 掉它。
                    conversation_in_flight=self._occupied(existing, project_id, session_id),
                )
                if decision == "defer":
                    # 这一轮换不掉，下一轮换 —— 不拒收（D10）。
                    #
                    # 指纹留着旧的，所以下一次进来（那时已空闲）会再被发现一次
                    # 并走 respawn。自愈，不需要在别处记一个"待换代"标志
                    # （那种标志就是下一个会被忘记清掉的布尔）。
                    logger.info(
                        "Session %s keeps its in-flight bindings; the new ones apply"
                        " from the next turn", session_id,
                    )
                if decision == "respawn":
                    # 停靠中的 pause 随旧进程退场：立刻把那条 run 标成可恢复，
                    # 别让下一条消息去撞 HarnessSessionStaleError。
                    orphaned = existing.binding if existing.paused else None
                    await existing.terminate()
                    self._sessions.pop(key, None)
                    existing = None
                    if orphaned is not None and self._stale_binding_handler is not None:
                        await self._stale_binding_handler(orphaned)
            if existing:
                if existing.owner_user_id != user.id:
                    if existing.paused or self._occupied(existing, project_id, session_id):
                        raise HarnessSessionError(
                            "The active project session belongs to another initiating user"
                        )
                    await existing.terminate()
                    self._sessions.pop(key, None)
                    existing = None
                elif not existing.alive:
                    # 进程没了就丢掉它。会话是**磁盘上的对话记录**，不是这个
                    # 进程 —— 下面照常新建一个、把 checkpoint 读回来接着跑。
                    await existing.terminate()
                    self._sessions.pop(key, None)
                    existing = None
            if existing:
                return existing
            created = await self._new_session(
                user=user,
                project_id=project_id,
                session_id=session_id,
                role_bindings=role_bindings,
                platform_context_snapshot=platform_context_snapshot,
                on_progress=on_progress,
                on_protocol_event=on_protocol_event,
                )
            self._sessions[key] = created
            return created

    async def adopt_live_worker(
        self, project_id: str, session_id: str, *, owner_user_id: str
    ) -> bool:
        """启动时把一个还活着的 worker 接回注册表。接上了返回 True。

        接上之后 `live_binding` 就能看见它 —— 于是 `mark_orphaned_harness_runs`
        照它原来的判据自然放过这条 run（"还有主"）。**不改判无主的逻辑**，
        只是让"有主"这件事重新为真：机制接缝要接在既有判据上，不是再造一条。

        ## 这里曾经锁在一个不存在的属性上（2026-09-03 发现）

        原文是 `async with self._lock:` —— 管理器只有 `_registry_lock`。
        AttributeError 被唯一调用方的 `suppress(Exception)` 吞掉，adopted 恒为
        False：P0-4 的"先接后判"在生产里**一次都没接成过**，接不回来的 worker
        随后被启动对账 SIGTERM 掉，而 CI 全绿 —— 没有一条测试走过这个方法。
        「机制存在但没接到路径」。`test_startup_adopts_a_live_worker_through_
        the_real_entry` 从真入口（`mark_orphaned_harness_runs(reap_workers=True)`）
        钉住。
        """
        async with self._registry_lock:
            adopted = await self._adopt_locked(
                project_id, session_id, owner_user_id=owner_user_id
            )
            return adopted is not None

    async def _adopt_locked(
        self, project_id: str, session_id: str, *, owner_user_id: str
    ) -> _ProjectHarnessSession | None:
        """`_registry_lock` 已持有时，把这个 session 还活着的 worker 接回注册表。

        注册表空缺 ≠ 没有 worker（D10）：上一个后端放手（`detach_all`）或崩掉
        之后，worker 还攥着这个 session 的 flock、在 accept 上等。这时直接 spawn
        第二个，新 worker 抢锁失败以 `project_busy` 退场，用户看到的是"会话被
        占用" —— 而占用它的正是我们自己上一代放走的进程。所以凡是要 spawn 的
        地方，先问一句盘上有没有活的。接回来的会话绑定是空的（指纹、快照都
        不知道），`_get_or_create` 据此对空闲的走 respawn（op=terminate 体面
        退场、锁释放、再 spawn），对在飞的走 defer —— 既有判据，一个字不改。

        正主以 worker 自己的记录为准（活动文件里的 app_binding.user_id）：
        "为谁跑"的答案在干活的那个进程手里，不在来问的这个请求手里。
        """
        key = self._key(project_id, session_id)
        existing = self._sessions.get(key)
        if existing is not None:
            return existing
        session = await reattach_session(
            project_id, session_id, owner_user_id=owner_user_id
        )
        if session is None:
            return None
        if session.binding is not None and session.binding.user_id:
            session.owner_user_id = session.binding.user_id
        self._sessions[key] = session
        # 接回来之后立刻把它正在跑的那一轮交给执行层去接（#785）。这里是**唯一**
        # 的接回落点 —— 启动对账（adopt_live_worker）与 spawn 前接回
        # （_get_or_create）都经过它，所以钩子只挂这一处；挂两处就是两份会
        # 各自演化的接线。钩子只起后台任务不 await（我们还在注册表锁里）；
        # 它自己出错不该把刚接回来的会话又弄丢。
        if self._adoption_handler is not None:
            try:
                self._adoption_handler(session)
            except Exception:  # noqa: BLE001 - 钩子的错不许拆掉接回本身
                logger.exception(
                    "Adoption handler failed for session %s/%s", project_id, session_id
                )
        return session

    async def rejoin(
        self,
        *,
        user_id: str,
        project_id: str,
        session_id: str,
        run_id: str,
        request_id: str,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
        before_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> dict:
        """接回一个别的后端发出去、此刻仍在飞的那一轮（#785）。

        与 `turn` / `answer` 同一条规矩：在注册表锁里核对身份，占用由
        `claim()` 持有 —— 于是 `is_occupied()` 在接回期间为真，`chat.py` 对这个
        会话的新消息走插话而不是开新轮，和它当初由上一个后端发起时一模一样。
        """
        async with self._registry_lock:
            session = self._sessions.get(self._key(project_id, session_id))
            if not session or not session.alive:
                raise HarnessSessionStaleError("The Harness worker to rejoin is no longer alive")
            binding = session.binding
            if binding is None or binding.run_id != run_id or binding.user_id != user_id:
                raise HarnessSessionStaleError(
                    "The live Harness worker is not bound to this user and Run"
                )
            if session.inflight_request_id != request_id:
                raise HarnessSessionStaleError(
                    f"Harness worker is not running turn {request_id} any more"
                )
        async with session.claim():
            return await session.rejoin(
                request_id=request_id,
                on_progress=on_progress,
                on_protocol_event=on_protocol_event,
                before_wait=before_wait,
            )

    async def reset_conversation(self, project_id: str, session_id: str) -> dict:
        """清这个 Session 的对话历史。

        没有活进程时**不是错误**：进程不在，下次起来本来就会从磁盘重建对话。
        这时直接把磁盘上的对话记录清掉即可（由调用方做），这里如实返回
        没有活运行时。
        """
        session = self._sessions.get(self._key(project_id, session_id))
        if session is None or not session.alive:
            return {"status": "no_live_runtime"}
        return await session.reset_conversation()

    def has_live_runtime(self, project_id: str, session_id: str) -> bool:
        """这个 Session 现在有没有活着的执行进程。

        `paused_binding` 只回答"有没有**停在 pause 上**的活进程"，回答不了
        "进程还在但空闲"。而 harness 会话进程是**跨轮保活**的：上一轮 failed
        之后进程照样活着，等下一条消息。恢复判据必须看得见这种状态，否则会在
        进程活着时多开一条接续会话。
        """
        session = self._sessions.get(self._key(project_id, session_id))
        return bool(session and session.alive)

    def paused_binding(self, project_id: str, session_id: str) -> AppRunBinding | None:
        """它**停下来等人**了吗 —— 只服务于"能不能把答案递进去"。"""
        session = self._sessions.get(self._key(project_id, session_id))
        if session and session.alive and session.paused:
            return session.binding
        return None

    def paused_offer_id(self, project_id: str, session_id: str) -> str | None:
        """此刻停着的那次呈递的身份；没有停着的 pause（或运行时没报）就是 None。"""
        session = self._sessions.get(self._key(project_id, session_id))
        if not (session and session.alive and session.paused):
            return None
        return session.offer_id

    def paused_worker_is_stale(self, project_id: str, session_id: str) -> bool:
        """停在问题上的那个 worker 是不是**接管来的旧一代**（绑定未知 = 起它的不是本后端）。

        新一轮走 `_get_or_create`，绑定对不上就 respawn（停靠的也算，pause 由
        stale-binding 钩子标成可恢复）。答复那条路此前绕过了这个判断：一个部署
        前起的 worker 只要一直停在卡上、人一直点，它就永远跑旧代码 —— 修复对它
        不生效，只能手动换代（2026-09-09 node20，qinp 的 worker 跨了三次部署）。
        判据与 `_get_or_create` 同一份：接管来的会话指纹为空。
        """
        session = self._sessions.get(self._key(project_id, session_id))
        if not (session and session.alive and session.paused):
            return False
        if self._occupied(session, project_id, session_id):
            return False
        return not session.backend_fingerprint

    async def prepare_workspace_upgrade(self, project_id: str, session_id: str, *, owner_user_id: str) -> None:
        """记录格式升级前的**只读**核验：没有任何 worker 还在写这个会话的工作区。

        有活 worker —— 不论在飞、停在问题上、还是空闲 —— 都抛 HarnessSessionError，
        让调用方这次跳过。这里**不终止任何进程**：启动对账刚把幸存的 worker 接回
        注册表，它可能正跑着几小时的研究；空闲的也没必要杀 —— 等它自己退场、
        下次启动再迁，或由操作者停掉后手动 `python -m app.pro.manage migrate-records`。
        旧 stdio worker 接不回来，但锁文件里的 pid 若还是我们的 worker，它仍是
        一个写者。
        """
        async with self._registry_lock:
            key = self._key(project_id, session_id)
            session = self._sessions.get(key)
            if session is None:
                session = await self._adopt_locked(project_id, session_id, owner_user_id=owner_user_id)
            if session is not None and session.alive:
                if session.paused or self._occupied(session, project_id, session_id):
                    raise HarnessSessionError(
                        "Finish or cancel the active research task before upgrading its record format"
                    )
                raise HarnessSessionError(
                    "A research worker still holds this workspace; it is left running and "
                    "the record upgrade waits for a start when it is gone"
                )
        lock_path = _session_lock_path(project_id, session_id)
        if lock_path is not None and lock_path.is_file():
            try:
                pid = json.loads(lock_path.read_text()).get("pid")
            except (ValueError, OSError, AttributeError) as exc:
                raise HarnessSessionError("Cannot verify the old research worker is stopped") from exc
            if isinstance(pid, int) and _process_is_a_runtime_worker(pid):
                raise HarnessSessionError("Close the old research worker before upgrading this project")

    async def retire_paused_worker(self, project_id: str, session_id: str) -> AppRunBinding | None:
        """让停在问题上的旧一代 worker 体面退场；它挂着的 pause 按 respawn 同一条路
        标成可恢复。返回被送走的绑定（None = 没有可退的）。"""
        async with self._registry_lock:
            key = self._key(project_id, session_id)
            session = self._sessions.get(key)
            if session is None or not session.paused:
                return None
            orphaned = session.binding
            await session.terminate()
            self._sessions.pop(key, None)
        if orphaned is not None and self._stale_binding_handler is not None:
            await self._stale_binding_handler(orphaned)
        return orphaned

    def paused_bindings_for_project(self, project_id: str) -> list[AppRunBinding]:
        """这个项目下**所有**停下来等人的活会话 —— 切档即答（升档路径）用。

        判据与 `paused_binding` 同一份（alive + paused），只是不需要调用方
        先知道 session_id：切档发生在项目层，它面对的问题是"这个项目现在有
        谁停着"。挑选按会话自记的 project_id，不解析注册表 key（同
        `broadcast_autonomy` 的理由）。"""
        return [
            session.binding
            for session in list(self._sessions.values())
            if session.project_id == project_id
            and session.alive
            and session.paused
            and session.binding is not None
        ]

    def is_occupied(self, project_id: str, session_id: str) -> bool:
        """这个会话此刻**有没有一次操作在飞** —— 从锁推导，全平台一个真相源。

        ## 为什么需要这个方法（wangd 2026-08-21 实测）

        「会话忙不忙」此前有两个真相源，而它们会**方向相反地各错一边**：

        - `api/v1/chat.py` 问**库里 run 行的状态**，用来决定一句话该走插话
          还是开新一轮；
        - `_get_or_create` 问**内存里的 operation lock**，用来决定收不收这一轮。

        8-21 现场：一个 unattended worker 停靠在 4 小时的复查间隔上（还活着、
        还攥着锁），而它那条顶层 run 早在 3 小时前就被平台盖成 `incomplete`。
        库说「空闲」→ 用户的话被当成新一轮；锁说「忙」→ 新一轮被拒收。真实
        状态是第三种（「停靠等唤醒」），两边都表达不出来。用户读到的是
        「平台在记录这次运行时撞上了内部错误」。

        判据只能有一个，而且只能是**锁** —— run 行是平台对 worker 的转述，
        锁是 worker 自己的现场。转述和现场分叉时，现场是对的那个。

        ## 它回答的不是"在不在算"

        锁被持有 ≠ 有计算在飞（停靠中的 worker 什么都没算）。这个方法答的是
        「这一刻别人能不能开新一轮」，也就是 RFC D10 三拆里的**所有权/占用**
        那一维。「在不在算」是另一个问题（活动自报 + 心跳租约），别合并。

        ## 接回来的会话，锁在**它**手里不在我们手里（2026-08-23 补）

        `_operation_lock` 量的是"**这个后端进程**有没有一次 RPC 在飞"。后端
        重启之后它必然是空的 —— 可 worker 手里那一轮还在跑。只问锁，接回来的
        会话就会被判成空闲，用户的下一句话变成一次新 turn，而它其实该是插话。

        所以判据是"锁 **或** 它自己说在干活"。两者都是现场，不是转述：一个
        是我们自己的现场，一个是 worker 落在盘上的自报。没有自报（老 worker）
        时只剩锁 —— 如实退化，不猜。
        """
        session = self._sessions.get(self._key(project_id, session_id))
        if not (session and session.alive):
            return False
        return self._occupied(session, project_id, session_id)

    @staticmethod
    def _occupied(session: _ProjectHarnessSession, project_id: str, session_id: str) -> bool:
        """锁 **或** worker 自报 —— `is_occupied` 与 `_get_or_create` 共用的那一份判据。

        分开各写一遍的话，`_get_or_create` 只问锁：接回来的会话锁在 worker 手里
        不在我们手里，一次正跑着几小时研究的会话会被判成空闲 → 绑定不同 →
        respawn → terminate 掉一次真研究。一个问题一个真相源。
        """
        if session.conversation_in_flight:
            return True
        activity = read_worker_activity(project_id, session_id)
        return bool(activity and activity.occupied)

    async def deliver(
        self,
        *,
        project_id: str,
        session_id: str,
        kind: str,
        text: str,
        author: str = "user",
        message_id: str = "",
    ) -> dict:
        """把一句话送给这个会话正在跑的那一轮（P1-2）。

        没有活 worker 时抛 `ValueError` —— 那是**唯一**合法的拒收（D10：
        可寻址恒真，唯一的例外是"没有工作区/没有人在跑"）。忙不忙**不是**
        拒收理由：忙恰恰是这条路存在的理由。
        """
        session = self._sessions.get(self._key(project_id, session_id))
        if session is None or not session.alive:
            raise ValueError(
                "Nothing is running in this Session right now — there is no current turn to speak to."
            )
        return await session.deliver(
            kind=kind, text=text, author=author, message_id=message_id
        )

    async def broadcast_autonomy(
        self, project_id: str, autonomy: object, *, db: AsyncSession
    ) -> list[str]:
        """项目档位改了 —— 推给这个项目下**每一个**活着的会话。返回收到的会话 id。

        ## 为什么必须推

        档位的真相源是 `project_configs` 那一行，而 worker 手里只有一份**派发
        时**的快照。派发只发生在有人说话的时候：一轮无人值守跑几十分钟、经过
        十几个决策点，中间一次派发都没有。于是 UI 上白纸黑字改成了「连续」，
        13:39 的决策照样停下问人（2026-08-23 会话 e46448f0 实测）。

        「机制存在但没接到路径」在这里的形态很纯粹：接收端（`_apply_request_scope`）
        一直挂在必经点上，只是没有任何东西在配置变化时**走一次**那个必经点。

        推不动的会话（进程没了 / 超时）不抛 —— 它们下一次派发自然会带上新档位，
        而设置本身已经落库了。这里只负责"活着的现在就跟上"。
        """
        updated: list[str] = []
        authorized = tuple(
            sorted(set(getattr(autonomy, "authorized_risk_classes", ()) or ()))
        )
        active: dict[str, bool] = {}
        binding_updates: list[tuple[_ProjectHarnessSession, RunAttempt]] = []
        from app.services.harness_contract import sandbox_module

        sandbox = sandbox_module()

        # Authorization is part of the frozen capability. Active/paused work
        # gets a new authoritative Attempt row before the sync RPC; idle
        # sessions have no operation to protect and simply drop their stale
        # binding until the next turn creates its own Attempt.
        for session in list(self._sessions.values()):
            if session.project_id != project_id or not session.alive:
                continue
            is_active = session.paused or self.is_occupied(
                session.project_id, session.session_id
            )
            active[session.session_id] = is_active
            binding = session.binding
            if not is_active or binding is None or not binding.sandbox_attempt_id:
                continue
            current = await db.get(RunAttempt, binding.sandbox_attempt_id)
            if current is None or not isinstance(current.sandbox_manifest, dict):
                continue
            parsed = sandbox.parse_manifest(current.sandbox_manifest)
            if parsed.authorized_risk_classes == authorized:
                continue
            latest = await db.scalar(
                select(RunAttempt)
                .where(
                    RunAttempt.tenant_id == current.tenant_id,
                    RunAttempt.run_id == current.run_id,
                )
                .order_by(RunAttempt.attempt_no.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is None:
                continue
            if not isinstance(latest.sandbox_manifest, dict):
                continue
            source = sandbox.parse_manifest(latest.sandbox_manifest)
            if source.authorized_risk_classes == authorized:
                binding_updates.append((session, latest))
                continue
            replacement = RunAttempt(
                tenant_id=latest.tenant_id,
                workspace_id=latest.workspace_id,
                project_id=latest.project_id,
                session_id=latest.session_id,
                run_id=latest.run_id,
                attempt_no=latest.attempt_no + 1,
                status=AttemptStatus.LEASED,
                worker_id=f"scope-sync:{uuid4().hex}",
            )
            db.add(replacement)
            await db.flush()
            manifest = sandbox.SandboxManifest(
                attempt_id=str(replacement.id),
                run_id=source.run_id,
                mounts=source.mounts,
                image_id=source.image_id,
                ceiling=source.ceiling,
                network_mode=source.network_mode,
                security_profile=source.security_profile,
                authorized_risk_classes=authorized,
                queue_limit=source.queue_limit,
                version=source.version,
                backend=source.backend,
            )
            manifest.validate()
            now = datetime.now(UTC)
            replacement.sandbox_manifest = manifest.canonical_payload()
            replacement.sandbox_manifest_hash = manifest.sha256
            replacement.heartbeat_at = now
            replacement.lease_until = now + timedelta(
                seconds=_liveness.ACTIVITY_LEASE_SECONDS
            )
            latest.status = AttemptStatus.RELEASED
            latest.exit_reason = "sandbox_authorization_changed"
            latest.ended_at = now
            binding_updates.append((session, replacement))
        await db.commit()
        for session, replacement in binding_updates:
            binding = session.binding
            if binding is None:
                continue
            session.binding = replace(
                binding,
                sandbox_attempt_id=str(replacement.id),
                sandbox_manifest=dict(replacement.sandbox_manifest),
                sandbox_manifest_hash=replacement.sandbox_manifest_hash or "",
            )
        # 按会话自己记着的 project_id 挑，不去解析注册表的 key —— key 是
        # `tenant:project:session`，拿字符串前缀去猜它的结构，改一次布局就静默
        # 漏掉全部会话（而"没推成"和"没有活会话"长得一模一样）。
        for session in list(self._sessions.values()):
            if session.project_id != project_id or not session.alive:
                continue
            session._authorize(autonomy)
            try:
                await session.sync_scope(
                    include_binding=active.get(session.session_id, False)
                )
            except Exception:  # noqa: BLE001 —— 推不动不是设置失败
                logger.warning(
                    "Autonomy change could not be pushed to live session %s;"
                    " it will pick it up on its next dispatch",
                    session.session_id, exc_info=True,
                )
                continue
            updated.append(session.session_id)
        return updated

    def live_binding(self, project_id: str, session_id: str) -> AppRunBinding | None:
        """它**还有没有活体进程** —— 不问停没停下来。

        和 `paused_binding` 是两个问题，别合并（2026-08-11）：

        `mark_orphaned_harness_runs` 要判的是"这条 run 还有主吗"，却一直调
        `paused_binding`。启动时注册表是空的，多出来的 `paused` 条件不显形；
        可它意味着那个函数**只能在启动时调** —— 随时调会把一个正在干活的
        running run 判成尸体。

        代价就是 2026-08-11 那条 run：后端一直活着，harness 子进程死了，
        DB 里它 `running` 了一小时四十分钟，UI 上看着在干活，会话级锁占着，
        没有任何东西会发现。孤儿检测挂在"进程重启"这个事件上，而孤儿的成因
        不是进程重启。

        （同一个函数上出现过同款：以前还要求 `resumable is True`，而
        "能不能续"和"要不要回收尸体"方向正好相反。注释还在下面。）
        """
        session = self._sessions.get(self._key(project_id, session_id))
        if session and session.alive:
            return session.binding
        return None

    def has_live_run(self, run_id: str) -> bool:
        """Positive in-memory ownership evidence for an exact App Run."""
        return any(
            session.alive
            and session.binding is not None
            and session.binding.run_id == run_id
            for session in self._sessions.values()
        )

    async def turn(
        self,
        *,
        user: User,
        project_id: str,
        conversation_id: str,
        run_id: str,
        session_id: str,
        message: str,
        role_bindings: RoleBindings,
        platform_context_snapshot: dict | None = None,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
        autonomy: object | None = None,
        sandbox_attempt: RunAttempt | None = None,
    ) -> dict:
        # 一个对象带两个事实（要不要无人值守 / 预授权哪些高危类别），
        # 因为它们来自**同一次**配置读取。拆成两个参数就会出现"模式开着、
        # 授权范围丢了"这种半开状态，而两边都不报错。
        autonomous = bool(getattr(autonomy, "unattended", False))
        session = await self._get_or_create(
            user=user,
            project_id=project_id,
            session_id=session_id,
            role_bindings=role_bindings,
            platform_context_snapshot=platform_context_snapshot,
            on_progress=on_progress,
            on_protocol_event=on_protocol_event,
        )
        session._authorize(autonomy)
        # 占用取得和释放在**同一个 with** 里 —— 不存在"忘了清"这条路径。
        # 占用取得和释放在**同一个 with** 里 —— 不存在"忘了清"这条路径。
        async with session.claim():
            return await session.turn(
                binding=AppRunBinding(
                    user.id,
                    conversation_id,
                    run_id,
                    session_id,
                    str(sandbox_attempt.id) if sandbox_attempt else "",
                    dict(sandbox_attempt.sandbox_manifest or {}) if sandbox_attempt else None,
                    str(sandbox_attempt.sandbox_manifest_hash or "") if sandbox_attempt else "",
                ),
                message=message,
                on_progress=on_progress,
                on_protocol_event=on_protocol_event,
                # autonomous 的完整语义 = 自动批准 **且** 自己往下推。
                # 只接前半句，就是 UI 上写着"自主"而实际每轮等人推。
                unattended=autonomous,
            )

    async def answer(
        self,
        *,
        user: User,
        project_id: str,
        conversation_id: str,
        run_id: str,
        session_id: str,
        answer: str,
        choice: dict | None = None,
        on_progress: ProtocolCallback,
        on_protocol_event: ProtocolCallback,
        autonomy: object | None = None,
        sandbox_attempt: RunAttempt | None = None,
    ) -> dict:
        async with self._registry_lock:
            session = self._sessions.get(self._key(project_id, session_id))
            if not session or not session.alive:
                raise HarnessSessionStaleError(
                    "The paused Harness process was lost and cannot be resumed"
                )
            if session.owner_user_id != user.id:
                raise HarnessSessionError("Only the initiating user can answer this live pause")
            # 这里从前还有一道 `busy → session_busy`（D10 删除清单）。删掉的
            # 理由不是"忙了也让它过"，是**这个问题下面那层答得更准**：
            # `session.answer()` 要求 paused + binding 对得上 + pause_id 还在，
            # 三条任一不成立就如实抛 stale。而"有轮在飞"本身不是拒绝答复的
            # 理由 —— 它只意味着这次答复排在后面（会话面在 worker 里是排队的）。
            #
            # 留着它的代价是真的：它把一个可以排队的请求变成一句"会话被占用"，
            # 而用户手里那个 pause 是**平台自己请他来答的**。
        # 答复也是一次派发 —— 同样按**这一刻**库里的授权范围来。人答复的这一刻
        # 恰恰是人在场、能改授权的一刻；把它排除在外，一个空授权起跑的 run
        # 就永远拿不到后来给的授权。
        session._authorize(autonomy)
        async with session.claim():
            return await session.answer(
                binding=AppRunBinding(
                    user.id,
                    conversation_id,
                    run_id,
                    session_id,
                    str(sandbox_attempt.id) if sandbox_attempt else "",
                    dict(sandbox_attempt.sandbox_manifest or {}) if sandbox_attempt else None,
                    str(sandbox_attempt.sandbox_manifest_hash or "") if sandbox_attempt else "",
                ),
                answer=answer,
                choice=choice,
                on_progress=on_progress,
                on_protocol_event=on_protocol_event,
            )

    async def cancel_run(
        self,
        *,
        user_id: str,
        project_id: str,
        session_id: str,
        run_id: str,
    ) -> bool:
        """Kill one addressable active process without waiting for its operation lock."""
        key = self._key(project_id, session_id)
        async with self._registry_lock:
            session = self._sessions.get(key)
            if not session or not session.alive:
                return False
            binding = session.binding
            if (
                binding is None
                or binding.user_id != user_id
                or binding.run_id != run_id
                or binding.session_id != session_id
            ):
                return False
            self._cancelled_run_ids.add(run_id)
            self._sessions.pop(key, None)
            await session._kill()
            return True

    def was_cancelled(self, run_id: str) -> bool:
        return run_id in self._cancelled_run_ids

    def clear_cancelled(self, run_id: str) -> None:
        self._cancelled_run_ids.discard(run_id)

    async def terminate_user(self, user_id: str) -> list[AppRunBinding]:
        """Terminate a user's processes and return pauses made stale by that action."""
        stale_bindings: list[AppRunBinding] = []
        async with self._registry_lock:
            targets = [
                (key, session)
                for key, session in self._sessions.items()
                if session.owner_user_id == user_id
            ]
            for key, session in targets:
                await session.terminate()
                if session.paused and session.binding:
                    stale_bindings.append(session.binding)
                self._sessions.pop(key, None)
        return stale_bindings

    async def terminate_all(self) -> None:
        """显式地把所有会话**结束**：发 terminate，退不了就杀。

        给测试收尾和运维手工用的全终止，**不是关机路径**。后端自己退场走
        `detach_all()` —— 关机不是任何一个会话的结束（RFC 异步运行时 P0）。
        """
        async with self._registry_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for session in sessions:
                await session.terminate()

    async def detach_all(self) -> int:
        """后端退场：放开所有 worker，一个都不终止。返回放开了几个。

        注册表在这里清空只是本进程的记账；这些 run 有没有主，由下一个后端
        启动时按注册表行**现算**（`mark_orphaned_harness_runs` 先接后判），
        这里不写任何判决。
        """
        async with self._registry_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        released = 0
        for session in sessions:
            try:
                await session.detach()
            except Exception:  # noqa: BLE001 - 一个 worker 的失败不是其余 worker 的判决
                # 这个循环的**全部意义**是"放开所有 worker，一个都不终止"。
                # 顺序循环让第一个异常把后面每一个都带走：2026-09-16 实测，
                # `release()` 在 uvloop 上抛 AttributeError → 第一个 worker
                # 之后**一个都没 detach**，收尾报 `Application shutdown failed`，
                # 而"部署不动 worker"的承诺就是在这里被静默作废的（9 次）。
                #
                # 与 `research_migration` 同一条规矩：一个工作区（这里是一个
                # worker）的判决不是整个后端的。失败要出声、但只赔上它自己。
                logger.exception(
                    "detach failed for %s/%s — 其余 worker 照常放手",
                    session.project_id, session.session_id,
                )
                continue
            released += 1
        if released != len(sessions):
            logger.error(
                "收尾放手：%d/%d 个 worker 放开了，其余见上面的异常",
                released, len(sessions),
            )
        return released

    @property
    def active_count(self) -> int:
        return sum(1 for session in self._sessions.values() if session.alive)

    def is_anyone_working(self) -> bool:
        """此刻有没有计算在飞 —— 组织服务器夜里自己升级之前问的就是这一句。

        `active_count` 答不了：它数的是**活着的 worker**，一个空闲着的 worker 也算
        （node20 上没人用的时候它报 5）。这里问的是两件事：本进程手里有没有一次会话
        RPC 在飞，和每个活着的 worker 自报的活动（`core.worker_activity`）。

        **说不清就算在忙**：没自报过（老 worker）或租约过期（`unknown`）都算占用 ——
        把一个几小时的研究当成空闲、在它底下把服务器换掉，代价远大于晚一天升级。
        """
        for session in list(self._sessions.values()):
            if not session.alive:
                continue
            if session.conversation_in_flight:
                return True
            activity = read_worker_activity(session.project_id, session.session_id)
            if activity is None or activity.occupied:
                return True
        return False


async def probe_runtime_environment(*, self_heal: bool = False) -> str | None:
    """worker 执行环境健康探针。返回 None = 健康；字符串 = 问题与出路。

    「继续会话」在实现上是「拉起一个进程」，而进程的前置条件（解释器、依赖、
    harness checkout）都是在**别的时刻**建立的 —— 建立与使用之间它们随时会
    坏（2026-08-21：.venv 指着被卸载的 anaconda，唯一活着的 worker 被登出
    杀掉后，每条消息都撞 "HARNESS_PYTHON is not an executable file"）。

    出路只有一条：**在启动时证明 worker 能起来**，坏了先自愈一次（uv sync
    --frozen），仍坏就拒绝启动并给出修复命令。用户永远不该是第一个发现
    执行环境坏了的人。
    """
    root = Path(settings.harness_root).expanduser()
    python = the_interpreter_that_runs_the_harness()

    async def _probe() -> str | None:
        if not (root / "core" / "agent_loop.py").is_file():
            return f"HARNESS_ROOT={root} 不是一个 harness checkout（缺 core/agent_loop.py）"
        if not Path(python).is_file():
            return (
                f"HARNESS_PYTHON={python} 不存在（常见原因：venv 的基础解释器"
                "被删，例如 anaconda 卸载后符号链接断掉）"
            )
        proc = await asyncio.create_subprocess_exec(
            python,
            "-c",
            "import platform_runtime",
            cwd=str(root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        except TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
            return "worker 解释器 import platform_runtime 超时（90s）"
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
            return f"worker 解释器起不来：{detail}"
        return None

    problem = await _probe()
    if problem is None:
        return None
    fix = f"修复：cd {root} && uv sync --frozen，然后重启"
    if not self_heal:
        return f"{problem}。{fix}"
    import shutil as _shutil

    uv = _shutil.which("uv")
    if uv is None:
        return f"{problem}。机器上没有 uv，无法自动重建。{fix}"
    logger.warning("Harness runtime broken (%s); attempting `uv sync --frozen` once", problem)
    heal = await asyncio.create_subprocess_exec(
        uv,
        "sync",
        "--frozen",
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(heal.communicate(), timeout=600)
    except TimeoutError:
        with suppress(ProcessLookupError):
            heal.kill()
        return f"{problem}。自愈超时（uv sync 600s 未完成）。{fix}"
    healed = await _probe()
    if healed is None:
        logger.warning("Harness runtime self-heal succeeded (uv sync --frozen)")
        return None
    tail = output.decode("utf-8", errors="replace").strip()[-300:]
    return f"{healed}。已尝试 `uv sync --frozen` 自愈未果（{tail}）。{fix}"


harness_session_manager = HarnessSessionManager()


_STATES_REQUIRING_LIVE_RUNTIME = tuple(
    status.value for status in REQUIRES_LIVE_RUNTIME_STATUSES
)


async def mark_orphaned_harness_runs(
    db: AsyncSession,
    *,
    run_ids: set[str] | None = None,
    reap_workers: bool = False,
) -> int:
    """App Server 注册表丢失后，把所有需要活体进程的 Run 标成可恢复。

    ⚠️ 不是只标"等人"的那种：`running` 的 run 一样需要活进程，进程没了它就是
    一具尸体，而且比 waiting_human 更隐蔽 —— UI 上它看起来"正在跑"。
    """
    if run_ids is not None and not run_ids:
        return 0
    query = select(Run).where(Run.status.in_(_STATES_REQUIRING_LIVE_RUNTIME))
    # ── 先接、后判（RFC 异步运行时 P0-4）─────────────────────────────────
    #
    # 原来这里的前提是"App Server 重启 ⇒ worker 必然也没了"，因为 worker 是
    # 后端的子进程、命令面是 stdin，后端一死它跟着死。P0-1~P0-3 之后这个前提
    # **不再成立**：worker 自成进程组、事件落盘、命令面是 socket，它可能正跑
    # 着一个几小时的实验。
    #
    # 所以在判"无主"之前，先给每条 run 一次接回来的机会。接上了它就重新
    # 出现在 `live_binding` 里，下面那条既有判据自然放过它 —— 判无主的逻辑
    # 一个字不改（机制接缝要接在既有判据上，别再造一条平行的）。
    if reap_workers:
        for candidate in list((await db.execute(query)).scalars().all()):
            # owner 必须重建对：它是"别让 B 抢走 A 正在跑的会话"那道判据的
            # 依据。填错的两个方向都很难看 —— 正主被当成外人拒掉，或者他的
            # 活 worker 被当成陌生人的直接 terminate 掉。
            # Run 表没有用户列，所以从 Session 投影重建，回退顺序写明：
            # 发起者 → 创建者。
            projection = await db.scalar(
                select(SessionProjection).where(
                    SessionProjection.session_id == candidate.session_id
                )
            )
            owner = (
                (projection.initiating_user_id if projection else None)
                or (projection.created_by_user_id if projection else None)
                or ""
            )
            if not owner:
                continue  # 认不出正主就不接 —— 交给原路径按常规处理，别乱认领
            adopted = False
            with suppress(Exception):  # 接不回来不该拦住整个启动对账
                adopted = await harness_session_manager.adopt_live_worker(
                    candidate.project_id, candidate.session_id, owner_user_id=owner
                )
            if adopted:
                # 接回来的 worker 已经 idle 时它自己把绑定清掉了（"从不更新的字段
                # 不是事实"）—— 可这条 run 可能正是它在没人听时跑完的那一轮：终止
                # result 躺在 events.jsonl 里，只是没人记账。那不是"无主"，是"有主
                # 但账没记"。这里先把主认回来，接回钩子（#785）随后从盘上收尾；
                # 不认的话下面那道既有判据会把一条已经跑完的研究判成尸体，还把
                # 一个好好的空闲 worker 回收掉。盘上没有它的 result 才轮到"无主"。
                live = harness_session_manager._sessions.get(
                    harness_session_manager._key(candidate.project_id, candidate.session_id)
                )
                if live is not None and live.binding is None:
                    finished, _request_id = terminal_result_on_disk(candidate)
                    if finished is not None:
                        live.binding = AppRunBinding(
                            owner, candidate.session_id, candidate.id, candidate.session_id
                        )
                # 接上了，还要把**后端不在的那段时间**补回来（P0-3）。
                #
                # worker 一直在说话，事件一直在落盘 —— 只是当时没有人在听。
                # 不补的话，库里这条 run 的时间线在重启处断一截，而它下面的
                # 研究其实一路做到了现在：UI 停在几小时前，用户看到的是"卡住了"。
                with suppress(Exception):  # 补不动也不该拦住启动
                    await _replay_worker_events(db, run=candidate, owner_user_id=owner)
    if run_ids is not None:
        query = query.where(Run.id.in_(run_ids))
    runs = list((await db.execute(query)).scalars().all())
    changed = 0
    for run in runs:
        summary = run.summary if isinstance(run.summary, dict) else {}
        # 曾经这里还要求 executionKernel == "formal_harness" 且 resumable is True。
        # 两个判据都用错了地方：
        #   · `resumable` 回答的是"这个**暂停**能不能续"，拿它当"要不要回收
        #     尸体"的门，方向正好反了 —— 一个跑死的 run 恰恰不 resumable，
        #     却最该被回收。
        #   · executionKernel 在旧 run 上根本没写（实测那 6 个 running 的
        #     两个字段都是空），于是它们**永远**通不过这道门。
        # 现在唯一的判据就是本函数的语义：这个状态需要活体进程，而注册表里
        # 没有它 → 无主。哪个内核跑的、能不能续，都是回收**之后**的问题。
        if summary.get("executionKernel") not in (None, "", "formal_harness"):
            continue
        # 子节点 run 不由**自己的** binding 判活 —— 它压根不会有。
        #
        # 2026-08-12：子节点 run 从这天起有了自己的 Run 行（`<父>/<子>`），
        # 而注册表里只登记**顶层**那条（一个 harness 子进程 = 一条 binding）。
        # 于是每一个子节点 run 都通不过下面那道 `binding.run_id == run.id`，
        # 全被判成孤儿 —— 再加上回收器，正在干活的 curator 被 SIGTERM 掉：
        #
        #     01:28:58 run_d11394c2…/_orchestrator->_curator@d1 | pid 26592 terminated
        #
        # 子节点的死活取决于**父 run**：父还有进程，子就还活着。
        # 「顶层 run」和「子节点 run」是两种东西，判活的问题不一样。
        if run.parent_run_id:
            parent_binding = harness_session_manager.live_binding(
                run.project_id, run.session_id
            )
            if parent_binding and parent_binding.run_id == run.parent_run_id:
                continue
        # `live_binding` 而不是 `paused_binding`：这里问的是"还有没有活体
        # 进程"，不是"停下来等人了吗"。用后者会把正在干活的 running run
        # 判成尸体 —— 那正是这个函数没法在启动之外的时刻调用的原因。
        binding = harness_session_manager.live_binding(run.project_id, run.session_id)
        if binding and binding.run_id == run.id:
            continue
        # ── D10 活动租约：注册表的空缺不是死亡证明 ──────────────────────────
        #
        # 注册表是进程内缓存，分不清"worker 死了"和"worker 刚起来还没登记"。
        # 这个函数挂在高频路径上（会话 GET 走 `_reap_orphaned_runs_of` 会到
        # 这里），谁在 spawn 窗口里撞进来，谁就把一条刚出生的 run 判死 ——
        # 2026-08-24 实测：attempt 创建后 **100ms** 被这里盖 stale_unknown，
        # 毒素潜伏 33 分钟，在用户答复 pause 时发作成"永久拒答"（2026-08-21
        # 那次 9 秒判死是同一个竞态，当时只把 run.status 的盖章改成了见证，
        # attempt 的盖章原样留下 —— 改造只做一半）。
        #
        # 租约是跨进程的事实：attempt 随 `run.started` 事件出生时就有租约，
        # worker 每产出一步真实进展就续一次。租约没过期 = 它刚刚还在动（或刚
        # 出生）—— 那不是"无主"，连"不知道"都不是。只有 `unknown` 才可标。
        # 真死掉的 worker 最多再等一个租约周期（180s）被下一次扫到 —— 晚标
        # 一个周期的代价是恢复入口晚开三分钟，误标一个新生儿的代价是整条
        # 研究卡死。
        _latest_attempt = await db.scalar(
            select(RunAttempt)
            .where(
                RunAttempt.tenant_id == run.tenant_id,
                RunAttempt.run_id == run.id,
            )
            .order_by(RunAttempt.attempt_no.desc())
            .limit(1)
        )
        _now = datetime.now(UTC)
        if _liveness.activity(
            run, attempt=_latest_attempt, now=_now, has_live_binding=False
        ) != "unknown":
            continue
        if _latest_attempt is None:
            # run 行已建、第一条事件还没到 —— attempt 和租约都还不存在。这个
            # 窗口里"注册表没有它"同样证明不了任何事，按出生宽限放过：满一个
            # 租约周期还没有任何 attempt，才轮到"无主"的解释。
            _created = run.created_at
            if _created is not None:
                if _created.tzinfo is None:
                    _created = _created.replace(tzinfo=UTC)
                if (_now - _created).total_seconds() < _liveness.ACTIVITY_LEASE_SECONDS:
                    continue
        previous_status = run.status
        # ── D11：见证，不判决 ────────────────────────────────────────────
        #
        # 这里曾经直接写 `run.status = STALE_UNKNOWN`。判据是"注册表里没有它的
        # 活 binding"，而注册表是进程内缓存 —— 它分不清"worker 死了"和"worker
        # 刚起来还没登记"。2026-08-21 现场：一条正在正常推进的 run 在开跑 9 秒时
        # 被这么判死，UI 显示「这一轮没跑完」并停止轮询五分钟，而它的子节点
        # hypothesis→_reviewer→observation 一路跑完了。
        #
        # 判决错了，事实字段已经被污染，下游也已经按它行动过。所以判决不落盘：
        # 落盘的只有**见证**（"某时刻观察到注册表里没有它"，这件事永远为真），
        # "所以它没了吗"由 `run_liveness.runtime_lost` 每次被问到时现算。
        run.summary = {
            **_liveness.witness(run, reason="app_server_restart",
                                detected_at=datetime.now(UTC).isoformat()),
            "staleReason": "app_server_restart",
            # 如实记下它死之前是什么状态 —— "running 时失联"和"等人时失联"
            # 对使用者是不同的信息（前者可能有半截产物要看）。
            "staleFromStatus": previous_status,
            # 这里曾经写 `resumable: False` —— 一个关于**未来**的存储判决
            # （"这个 pause 续不上了"）。它被答复闸当权威读，而它可能错
            # （2026-08-24：误判的 attempt 让它把一个活着的 pause 说成不可续，
            # 用户被无限拒答）。"能不能续"由现场回答：binding 在不在、租约
            # 新不新鲜 —— 现算，不落盘。见证（staleReason/runtimeWitness）
            # 留下，它陈述的是"某时刻观察到"，永远为真。
        }
        attempts = list(
            (
                await db.execute(
                    select(RunAttempt).where(
                        RunAttempt.tenant_id == run.tenant_id,
                        RunAttempt.run_id == run.id,
                        RunAttempt.status.in_(
                            [AttemptStatus.RUNNING.value, AttemptStatus.RELEASED.value]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for attempt in attempts:
            attempt.status = AttemptStatus.STALE_UNKNOWN
            attempt.exit_reason = "app_server_restart"
            attempt.ended_at = datetime.now(UTC)
        # 判成"无主"之后要把它变成真的：光改一行状态，那个进程还活着、还攥着
        # 这个 session 的 flock，下一个请求会撞 `project session is already
        # running` —— 而库里明明写着 failed。见 `reap_orphaned_session_worker`。
        #
        # 但**只在判断成立的地方杀**：这个函数有 4 个调用点，只有启动那一处
        # 具备"注册表为空 ⇒ 全都无主"的性质。别处（登出、会话恢复、
        # `assert_conversation_runtime_available`）注册表里本就有活的东西，
        # 「不在注册表里」不等于「无主」。
        #
        # 2026-08-12 实测：`assert_conversation_runtime_available` 调它时不带
        # 任何过滤、扫全表，把**正在干活**的 curator 子进程杀了。记账错了还能
        # 改回来，杀进程改不回来 —— 这两件事该有不同的门槛。
        reaped = reap_orphaned_session_worker(run.project_id, run.session_id) if reap_workers else None
        if reaped:
            run.summary = {**run.summary, "staleReapedWorker": reaped}
        changed += 1
    if changed:
        await db.flush()
    return changed


def _control_address(project_id: str, session_id: str) -> str | None:
    """这个 session 的命令面地址（**带 scheme**）。关掉开关 / 契约拿不到 → None（走 stdio）。

    规则只有一份（`core.worker_addressing`，经既定的 harness_contract 桥）——
    抄一份就会分叉，而分叉时两边都不报错：一边往 A 绑、一边往 B 连，连不上
    就当"worker 没起来"。unix 走绝对路径（两边同一条规则各自算得出来）、Windows
    走 `tcp:127.0.0.1:0`（内核挑端口）。

    地址由后端定下来交给 worker（unix 按身份算路径，tcp 先问内核要一个空闲端口），
    所以 spawn 它的后端不必先问一句才知道往哪里连；连上之后核对 spawn token，因为
    同一个地址上一代也绑过。注册表行的 `control_socket` 答的是另一个问题：**没有**
    spawn 过它的新后端怎么接回来。

    拿不到契约不是致命错：退回 stdio 老路，会话照常起得来。
    """
    if not settings.harness_control_socket:
        return None
    from app.services.harness_contract import (
        HarnessContractUnavailable,
        worker_addressing,
    )

    try:
        module = worker_addressing()
    except HarnessContractUnavailable:
        return None
    # **必须绝对**：这个路径经环境变量交给 worker，而 worker 的 cwd 是 harness
    # 根、App Server 的 cwd 是 platform/backend —— 同一个相对路径在两个进程里
    # 指向两个不同的文件。实测（2026-08-19 node20）：worker 在
    # `<harness根>/data/harness_sockets/` 绑上了，后端在
    # `<harness根>/platform/backend/data/harness_sockets/` 找，连不上 → 回退
    # 管道 → 而 worker 只读 socket → **死锁**，新会话永远停在"正在启动"。
    #
    # 症状里最坏的一点是它不报错：两边各自都"成功"了。
    root = data_root("sockets").resolve()
    return module.control_address(root, project_id, session_id)


#: 一个 session 的运行时目录里有哪些文件。**规则的正主在 `core.worker_activity`**
#: （worker 那边真正建这些文件的地方）；这里是 App Server 侧的副本。
#:
#: 为什么不直接调契约模块：它要 import 一个真实的 harness checkout，而
#: `_session_lock_path` 是回收/接回路径上最基础的一步。把"算得出这个文件名"
#: 挂到"harness 根现在可读吗"上面，等于让一次配置抖动静默关掉整条回收链
#: （实测：契约模块的 lru_cache 被另一个用例污染之后，四条回收测试全灭，
#: 而症状是"什么都没发生"）。判据不许依赖运行环境。
#:
#: 两份副本的绑定靠 `test_both_sides_agree_on_the_runtime_layout` —— 写岔了
#: 当场红，而不是等到某天一个正在跑几小时研究的 worker 找不到自己的锁。
_RUNTIME_DIR_TEMPLATE = "orchestrator__{project_id}__session__{session_id}"
_LOCK_FILENAME = ".chat.lock"
_ACTIVITY_FILENAME = "activity.json"


def _session_runtime_dir(project_id: str, session_id: str) -> Path | None:
    """这个 session 的运行时目录 —— 锁、活动、事件三个文件的共同家。"""
    from app.services.project_repository import get_project_repository

    try:
        worktree = get_project_repository().session_path(project_id, session_id)
    except Exception:  # 仓库还没初始化 —— 那就不可能有 worker 在跑
        return None
    if not worktree.is_dir():
        return None
    # 读路径：现行位置优先，存量会话回落老位置（续跑是全函数）。
    from app.services.session_runtime_paths import resolve_existing

    return resolve_existing(
        worktree, "runs",
        _RUNTIME_DIR_TEMPLATE.format(project_id=project_id, session_id=session_id),
    )


def _session_lock_path(project_id: str, session_id: str) -> Path | None:
    """这个 session 的 runtime 锁在哪 —— 与 `platform_runtime._project_lock` 同一处。"""
    runtime_dir = _session_runtime_dir(project_id, session_id)
    return None if runtime_dir is None else runtime_dir / _LOCK_FILENAME


def _session_activity_path(project_id: str, session_id: str) -> Path | None:
    """worker 的活动自报文件在哪（D10）。"""
    runtime_dir = _session_runtime_dir(project_id, session_id)
    return None if runtime_dir is None else runtime_dir / _ACTIVITY_FILENAME


def read_worker_activity(project_id: str, session_id: str):
    """读一次 worker 的自报活动。没有自报 → None。

    **None 不是"它不在干活"**，是"它没说过"：老 worker 不写这个文件，而它
    可能正跑着一个几小时的研究。调用方必须据此回退到别的判据，不许把沉默
    当成空闲 —— 那正是把活人判成尸体的那一步。
    """
    from app.services.harness_contract import HarnessContractUnavailable, worker_activity

    path = _session_activity_path(project_id, session_id)
    if path is None or not path.is_file():
        return None
    try:
        # 解析规则走契约（格式只有一份定义）。这里可以依赖它：没有活动文件
        # 时本来就返回 None，契约不可用时退回同一个答案，行为一致。
        activity = worker_activity().read_activity(path)
    except HarnessContractUnavailable:
        return None
    return activity if activity.present else None


def _process_command_line(pid: int) -> str | None:
    """这个 pid 的命令行。进程不在 / 读不到就是 None（"读不到"不等于"不是它"）。

    机制在 `shared.lib.process_control.command_line`（psutil：Linux 读 /proc、macOS 与
    Windows 各走各的系统调用，都**不依赖外部程序** —— 容器里没装 procps 曾让"确认不了
    身份"静悄悄地退化成"永远不回收"，本地绿、CI 红的经典形状）。
    """
    return harness_module("shared.lib.process_control").command_line(pid)


def _process_is_a_runtime_worker(pid: int) -> bool:
    """确认这个 pid 真是我们的 worker —— pid 会被系统回收再分配。

    不确认就杀 = 拿一个陈旧的数字去杀一个无辜进程。判据是命令行里同时有
    `platform_runtime` 和 `--serve`，两者都对不上就当它不是。
    """
    command = _process_command_line(pid)
    return bool(command) and "platform_runtime" in command and "--serve" in command


def reap_orphaned_session_worker(project_id: str, session_id: str) -> str | None:
    """回收还攥着这个 session 的 runtime 进程。回收了就返回一句说明。

    ## 为什么需要（2026-08-11 实测）

    重启 App Server 时对在途请求发 SIGTERM，uvicorn 的优雅关闭**先关监听
    socket、再等在途请求结束**。而那个在途请求正等着 harness worker，于是：

        老 App Server 永不退出 → worker 的 stdin 永不 EOF
          → worker 永不 close() → session 的 flock 永不释放

    新 App Server 起来能绑端口（老的已放开监听），把那条 run 标成
    `stale_unknown` —— 但那只是**账面**上的判决。现场那个 worker 活了 2 小时
    37 分，用户重发消息只得到一句 `project session is already running`。

    「这个 session 忙不忙」于是有了两个真相源：库里的 run 状态，和 worker 进程
    里的 flock。两边分叉时**谁都不报错**，要等下一个请求才炸。

    这个函数让判决落地：既然本函数只在"注册表里没有它"时被调用，那个进程按
    定义就没有任何活着的 App Server 能指挥它 —— 它收不到命令，也交付不了结果。
    """
    import signal
    import time

    lock_path = _session_lock_path(project_id, session_id)
    if lock_path is None or not lock_path.is_file():
        return None
    try:
        record = json.loads(lock_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    pid = record.get("pid") if isinstance(record, dict) else None
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        return None
    if not _process_is_a_runtime_worker(pid):
        # 进程已经没了（锁本来就自动释放了），或者这个号被别人用了。
        return None

    process_control = harness_module("shared.lib.process_control")
    if not process_control.terminate(pid):
        return None
    # 等它退出，**不是**等它存盘 —— worker 没有 SIGTERM handler，Python 默认
    # 直接终止，finally / atexit 都不跑（2026-08-19 实测确认）。这两秒只是
    # "先礼后兵"：给一个可能正在写文件的进程把当前那次原子写做完的机会，
    # 然后再 SIGKILL。
    #
    # 那为什么不加 handler：**工作不是靠退出时保存的**。agent_loop 每个 turn
    # 末就写 messages_checkpoint.json，而会话恢复（conversation_store.
    # load_conversation）取两份里更新的那份 —— 实测一个被 kill 的会话恢复到
    # 39 条消息（走 checkpoint），而不是 conversation.json 里那 19 条。
    # 所以任何死法（SIGKILL / OOM / 断电）损失的都只是**当前 turn 内**尚未
    # 落到 checkpoint 的那一小段。再加一个只覆盖优雅信号的 handler，是给
    # 已经被兜住的问题加第二层机制。
    for _ in range(20):
        time.sleep(0.1)
        if not _process_is_a_runtime_worker(pid):
            return f"pid {pid} terminated"
    process_control.kill(pid)
    return f"pid {pid} killed after refusing SIGTERM"


async def assert_conversation_runtime_available(
    db: AsyncSession,
    *,
    project_id: str,
    conversation_id: str,
    user_id: str,
) -> None:
    """Fail with a recoverable conflict when a live pause was lost."""
    result = await db.execute(
        select(Run)
        .where(
            Run.project_id == project_id,
            Run.session_id == conversation_id,
            # D11：判决不再落进 status，所以这里也不能按 status 认"卡住了"。
            # 候选取"需要活体运行时"的全部状态，死活由 runtime_lost 现算。
            Run.status.in_([status.value for status in REQUIRES_LIVE_RUNTIME_STATUSES]),
        )
        .order_by(Run.updated_at.desc(), Run.id.desc())
    )
    for run in result.scalars().all():
        summary = run.summary if isinstance(run.summary, dict) else {}
        if summary.get("executionKernel") != "formal_harness":
            continue
        # 人已经确认过这个挂起的询问没了 —— 它不再是拦住整个 Session 的理由。
        # 判据是「有没有人确认」，不是「状态名叫什么」：光标成 stale_unknown
        # 没用，那个状态本来就在上面的拦截名单里。
        from app.services.sessions import PAUSE_ABANDONED

        if summary.get(PAUSE_ABANDONED):
            continue
        # 这个函数问的是「**挂起的那个询问**还在不在」，不是「进程还在不在」。
        # 一个正在跑的 run 根本没有挂起的询问 —— 它没什么可丢的。
        #
        # 2026-09-07 真机第五轮实测：`RUNNING` 也在 `REQUIRES_LIVE_RUNTIME_STATUSES`
        # 里（那个集合回答的是另一个问题：谁以有活进程为前提），于是顶层 run 一
        # 边正常跑着，每一次提交都走到下面那个无条件 `raise` —— **连着 7 次**，
        # 每次都告诉用户「之前那个待回答的问题随运行时一起丢了」。什么都没丢：
        # 那 7 条答复（决策选项 / 三次高危批准 / 一次插话 / 一次拒绝）条条都
        # 生效了。一句吓人的假话，说在用户最需要相信这套东西的时刻。
        #
        # 判据用既有词表 `PARKED_WAITING_FOR_HUMAN`，不新写第四份名单。
        # 进程死活由 `run_liveness` 现算（D11），不在这儿判。
        if run.status not in {status.value for status in PARKED_WAITING_FOR_HUMAN}:
            continue
        binding = harness_session_manager.paused_binding(project_id, conversation_id)
        if (
            run.status == RunStatus.WAITING_HUMAN.value
            and binding
            and binding.run_id == run.id
            and binding.conversation_id == conversation_id
            and binding.user_id == user_id
        ):
            return
        if run.status == RunStatus.WAITING_HUMAN.value:
            await mark_orphaned_harness_runs(db)
        raise HarnessSessionStaleError(
            "The paused Harness process was lost; start a new conversation to continue safely"
        )
