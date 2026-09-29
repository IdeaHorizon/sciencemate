"""给会话起一个短标题 —— 通过跑对话的那同一座桥。

## 为什么会话标题需要单独一层

会话标题此前就是用户第一条消息的机械截断。一条真实的科研指令是一整段话
（"你帮我研究一下，为什么英国的饮食文化……然后我要求你输出一份非常严谨的
论文"），截断之后侧边栏和顶栏各挂着半段话，既读不出这是什么课题，又占掉
好几行。标题要回答的是"这是哪个课题"，而截断回答的是"这段话开头是什么"。

## 为什么模型调用在 harness 那边

App Server 全流程一次模型调用都没有：provider 分支、重试、超时、密钥解析
只存在于 `core.llm.LLMClient`。为了一个标题在这一层再实现一遍 HTTP 调用，
就是又造一处会各自演化的 provider 逻辑。所以这里只做三件本层的事 ——
判断该不该命名、把凭据交给桥、把结果写回库 —— 模型那一步交给 `op=name_session`。

## 为什么不落 `title_source` 字段

"这个标题是机器起的还是人挑的"是**现算**的（见 `sessions.session_title_is_machine_made`）：
标题要么还是占位符，要么逐字等于第一条消息的机械截断。存一个标记就是把判决
写进库里，规则一变那份标记就和事实对不上，而且不会报错。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.config import data_root, settings
from app.database import get_session_factory
from app.models.execution import SessionMessage, SessionProjection
from app.models.model_backend import ModelBackendConfig
from app.services import harness_bridge_once
from app.services.harness_runtime import (
    _provider_base_url,
    harness_subprocess_env,
    the_interpreter_that_runs_the_harness,
)
from app.services.model_backends import resolved_api_key
from app.services.sessions import session_title_is_machine_made

logger = logging.getLogger(__name__)

# 推理模型起个标题也要先想一轮：GPUStack 上的 deepseek-v4-pro 实测 47 秒
# （harness 导入只占 0.07 秒，时间全在模型上）。命名是后台任务，不挡任何人，
# 所以这里可以等得起。
#
# ⚠️ 两个超时的**相对关系**是有意义的：内层（模型调用）必须严格小于外层
# （子进程），否则永远是子进程先被杀掉 —— 拿到的是一句"timed out"，而不是
# harness 那条说得清是连不上、还是被拒、还是真的慢的错误。
_MODEL_TIMEOUT_SECONDS = 120
_TIMEOUT_SECONDS = 150
assert _MODEL_TIMEOUT_SECONDS < _TIMEOUT_SECONDS


class SessionNamingError(RuntimeError):
    """这次命名没能完成。"""


async def schedule_session_autoname(
    *,
    user_id: str,
    project_id: str,
    session_id: str,
    backend: ModelBackendConfig,
) -> asyncio.Task[None] | None:
    """在后台给这个会话起标题；不阻塞正在开始的这一轮。

    命名和研究这一轮是并行的：研究要跑几分钟，命名一秒就回来，等这一轮
    收尾时前端本来就会重取会话，新标题正好在那时候到位 —— 不需要为它另
    开一条推送通道。

    凭据必须在这里就取出来（`backend` 是绑在调用方事务上的对象），detached
    任务里再去碰它就是在一个已经关掉的 session 上取属性。
    """
    api_key = resolved_api_key(backend)
    base_url = _provider_base_url(backend)
    if not api_key or not base_url:
        # 命名是锦上添花：后端没凭据时保持机械标题，不要把它变成一条报错。
        return None

    task = asyncio.create_task(
        _autoname(
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            api_key=api_key,
            base_url=base_url,
            model=backend.model,
        )
    )
    from app.services.local_execution import HOUSEKEEPING_KIND, retain_detached_execution

    # 命名是平台杂活，不是用户的研究 —— 别记进"还有几个执行在跑"那本账。
    retain_detached_execution(task, kind=HOUSEKEEPING_KIND)
    return task


async def _autoname(
    *,
    user_id: str,
    project_id: str,
    session_id: str,
    api_key: str,
    base_url: str,
    model: str,
) -> None:
    """整条命名链，跑在自己的事务里。失败只留日志。"""
    try:
        factory = get_session_factory()
        async with factory() as db:
            session = await db.get(SessionProjection, session_id)
            if session is None:
                return
            first_message = await db.scalar(
                select(SessionMessage.content)
                .where(
                    SessionMessage.session_id == session_id,
                    SessionMessage.role == "user",
                )
                .order_by(SessionMessage.sequence.asc())
                .limit(1)
            )
            if not first_message or not first_message.strip():
                return
            if not session_title_is_machine_made(session, first_message):
                # 人挑过的标题，或者已经命过名了。两种情况都不碰。
                return

        title = await _ask_harness_for_title(
            user_id=user_id,
            project_id=project_id,
            message=first_message,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        if not title:
            return

        async with factory() as db:
            session = await db.get(SessionProjection, session_id)
            if session is None:
                return
            # 再判一次：命名期间用户可能刚好手动改了名字。第一次判断和写入
            # 之间隔着一次模型调用，中间的改名不能被这里盖掉。
            if not session_title_is_machine_made(session, first_message):
                return
            session.title = title
            await db.commit()
    except Exception:
        # 起不出标题就保持机械标题 —— 下一轮会自己再试。这条路径上任何失败
        # 都不该影响研究本身。
        logger.warning("session autoname failed for %s", session_id, exc_info=True)


async def _ask_harness_for_title(
    *,
    user_id: str,
    project_id: str,
    message: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    """`op=name_session` —— 与 KB 查询同一座桥，只是这一次要带模型凭据。"""
    root = Path(settings.harness_root).expanduser().resolve()
    if not (root / "core" / "llm.py").is_file():
        raise SessionNamingError("HARNESS_ROOT is not a valid current harness checkout")
    python = the_interpreter_that_runs_the_harness()

    state_root = data_root("state").resolve()
    request = {
        "op": "name_session",
        "request_id": f"name-{uuid4().hex}",
        "project_id": project_id,
        "home_dir": str(state_root / "users" / user_id),
        "message": message,
    }
    child_env = harness_subprocess_env(root, extra={
        "LLM_API_KEY": api_key,
        "LLM_BASE_URL": base_url,
        "LLM_MODEL": model,
        "LLM_TIMEOUT": str(_MODEL_TIMEOUT_SECONDS),
        # 不重试：命名是尽力而为的，失败下一轮自己会再试。留着默认重试
        # 只会把一次慢调用乘上几倍，然后照样撞外层超时。
        "LLM_MAX_RETRIES": "0",
    })

    event = await harness_bridge_once.ask_once(
        request,
        expect="name_session_result",
        error=SessionNamingError,
        root=root,
        python=python,
        child_env=child_env,
        timeout_s=_TIMEOUT_SECONDS,
        # 子进程环境里带着模型凭据，它的 traceback 里出现过 —— 打码后才进日志。
        secret=api_key,
    )
    title = event.get("title")
    return title.strip() if isinstance(title, str) else ""
