"""FastAPI application entry point."""

import asyncio
import json
import importlib
import os
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import settings
from app.core.logging import get_logger, setup_logging
from app.middleware import RequestBodyCapMiddleware, RequestLoggingMiddleware
from app import assembly

logger = get_logger("app")


def _expected_alembic_heads() -> set[str]:
    """Resolve the migration heads shipped with this application."""
    backend_root = Path(__file__).resolve().parents[1]
    alembic_config = AlembicConfig(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    return set(ScriptDirectory.from_config(alembic_config).get_heads())


def _tables_this_build_reads() -> list[str]:
    """本服务实际读写的表 —— 从 ORM 元数据现算，不写名单。

    ## 现场（2026-08-13/14 每次部署）

    `/health/ready` 长期返回 `degraded`，唯一红的是这一项：

        missing: kb_chunks, kb_concepts, kb_claims,
                 memory_entries, memory_proposals, research_skills

    这六张表是迁移 `022_retire_platform_kb_domain` **故意**删的：知识与记忆由
    harness 沉淀，本服务的平行 KB/memory 领域连一行数据都没有过，读路径早已
    改走 `app.services.harness_kb`。也就是说"缺失"正是预期终态，而就绪检查里
    那份手写名单没跟着迁移一起走。

    ## 为什么不是把六个名字删掉了事

    删名字只能修好这一次。名单式判据的毛病是**它和真相之间没有机械联系**：下
    一次退役、下一次新增，同样得有人记得回来改这一行，而漏改不会有任何东西报
    错 —— 唯一的症状就是这个：一个永远 degraded 的就绪端点。

    **一个长期红着的健康检查等于没有健康检查** —— 真出问题那天，degraded 是
    它每天的样子，没人会看第二眼。

    所以判据改成现算：`Base.metadata` 里有哪些表，就是这个 build 会读写哪些
    表。模型删了，要求自动跟着消失；模型加了，要求自动跟着出现。

    （对照 `_expected_alembic_heads` 与 `tests/test_readiness.py` 里写死的
    head：那里写死是对的，因为新迁移默认**变红**要求有人确认；这里写死是错的，
    因为退役的表默认**永远红**、新增的表默认**漏过**。方向相反。）

    ## 为什么要把 `app.models` 下的每个模块都 import 一遍

    `Base.metadata` 是**导入即注册**的，所以"有哪些表"取决于此刻谁被 import
    过。`app.models.__init__` 只覆盖 40 张；`ReflectionResult` 是在
    `app.core.reflection` 里按需 import 的，它的 `reflection_results` 只在那条
    路径跑过之后才出现在 metadata 里。照搬 `import app.models` 就等于让就绪结
    果取决于此前碰巧执行过什么 —— 同一个库同一个 build，答案会变。整包扫一遍
    才是恒定的。
    """
    import pkgutil

    import app.models
    from app.database import Base

    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"{app.models.__name__}.{module.name}")

    return sorted(Base.metadata.tables)


async def _refuse_to_serve_a_schema_we_were_not_written_for() -> None:
    """库落后就不许起。

    ## 为什么这是启动闸而不是健康检查项

    这个比对**本来就写好了** —— 在 `/readyz` 里，逻辑一字不差。但它长在一条
    没人走的路上：App Server 明知库落后也照常起，然后在**几层之外**炸。

    2026-08-10 实测：我加了 `project_configs.autonomous_authorized_risk_classes`
    的模型字段和迁移，忘了在跑着的库上执行。症状是

        InFailedSQLTransactionError: current transaction is aborted

    —— 一个完全看不出病因的错误（真因藏在 postgres 日志里：`column ... does
    not exist`，而它导致整个事务中止、后面所有查询连带失败）。一轮 E2E 因此
    起不来，我花了几分钟才找到那行 postgres ERROR。

    **一个跑在自己没被写来适配的 schema 上的服务，产生的错误必然指不到病因。**
    所以判据前移到启动：要么对得上，要么不起，并且报错里直接给出解法。

    ## 三态，不是两态

    `alembic_version` 表**不存在** = 这个库不是 alembic 建的（测试用
    `Base.metadata.create_all`），不是"落后"。把这两种混成一个 bool，就只能在
    "测试起不来"和"生产带病起"之间选一个坏的。
    """
    from sqlalchemy import text

    from app.database import get_engine, has_table

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            if not await has_table(conn, "alembic_version"):
                logger.info("No alembic_version table — schema not managed by Alembic here")
                return
            applied = set(
                (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars()
            )
    except Exception:
        # 连不上库是另一回事，交给后面的路径按它自己的方式失败 —— 别把
        # "库没起来"伪装成"schema 不匹配"。
        logger.exception("Could not read the applied Alembic head")
        return

    expected = _expected_alembic_heads()
    if applied == expected:
        return
    raise RuntimeError(
        "Database schema is not the one this build expects — refusing to start.\n"
        f"  expected: {', '.join(sorted(expected)) or '(none)'}\n"
        f"  applied : {', '.join(sorted(applied)) or '(none)'}\n"
        "  fix     : cd platform/backend && alembic upgrade head\n"
        "带病启动的代价是错误指不到病因（2026-08-10：漏跑一个迁移，症状是"
        "几层之外的 InFailedSQLTransactionError）。"
    )


def stranded_by_root(configured: Path, recorded: Iterable[str | None]) -> dict[str, int]:
    """记录在案的 worktree 里，有多少不在 `configured` 这个根下（按根分组计数）。

    抽成纯函数是为了让判据本身可被变异测试打到：读库那半段在测试里容易被
    替身遮住，而"根一样算不算搁浅"正是这道闸的全部内容。
    """
    stranded: dict[str, int] = {}
    for path in recorded:
        if not path:
            continue
        root = str(Path(path).parent.parent)
        if root != str(configured):
            stranded[root] = stranded.get(root, 0) + 1
    return stranded


async def _refuse_to_serve_where_the_data_is_not() -> None:
    """配置指的根 ≠ 数据实际所在的根，就不许起。

    ## 这道闸挡的是什么

    Project 仓库与 Session worktree 的路径是**现算**的：
    `session_path(pid, sid) == <worktree 根>/<pid>/<sid>`（路径即身份，是设计）。
    现算没问题 —— 有问题的是**根可以在两次启动之间悄悄改变**，而没有任何一层
    能发现。改变之后：

    * 旧会话：目录查无此处 → 每发一条消息 0.1 秒内 `Session Git worktree is
      not initialized`。用户看到的是"整个平台停了"。
    * 新会话：在新根下**重新建**一套仓库 —— 于是数据分裂成两处，谁也不完整。
    * 健康检查：全绿。它检查的是"库连得上""harness 在不在"，没有一项问
      "我的数据还在我以为的地方吗"。

    2026-08-21 node20 实测就是这样：43 个会话在一个根下，服务指着另一个根，
    `/health/ready` 报 `"status":"ready"`。8-19 发生过两次、8-20 被人手工
    export 修回来、8-21 又来一次 —— 三次都没有任何一层出过声。

    ## 判据取哪一处

    取 `sessions.git_worktree_path`：它在会话创建时被写死成绝对路径
    （`sessions.py` / `revisions.py` 四处），是"数据当时被放在哪"的**证据**。
    此前它被写入却从来没有任何代码读过 —— 一个问题两份真相源，其中一份没人
    看，于是分叉时两边都不报错。这道闸让它变成承重的：

        现算的根（判决，可以变） vs 记录的根（证据，不该被悄悄推翻）

    两者不一致 = 有人在数据底下换了根。不是"降级服务"，是**停下来**：带着
    错的根服务下去，就是一边给用户看空项目、一边在新地方建第二套数据。

    ## 换根的合法出口

    确实要搬（换机器、换盘、合并两处），走
    `scripts/relocate_project_data.py` —— 它搬目录、`git worktree repair`、
    并改写这一列。报错里直接给出这条命令，不让人只能去猜。
    """
    from sqlalchemy import text

    from app.config import DataRootError, data_root
    from app.database import get_session_factory

    try:
        configured = data_root("worktrees").resolve()
    except DataRootError as exc:
        raise RuntimeError(f"Refusing to start — {exc}") from exc

    try:
        factory = get_session_factory()
        async with factory() as db:
            recorded = list(
                (
                    await db.execute(
                        text(
                            "SELECT git_worktree_path FROM sessions "
                            "WHERE archived_at IS NULL AND git_worktree_path IS NOT NULL"
                        )
                    )
                ).scalars()
            )
    except Exception:
        # 连不上库、表还没建（全新部署跑 alembic 之前）都不是"根不对"。别把
        # 别的病伪装成这一个 —— 那正是本文件另一道闸的教训。
        logger.info("Could not read recorded worktree roots; skipping data-root check")
        return

    stranded = stranded_by_root(configured, recorded)
    if not stranded:
        return  # 空库（全新部署）也走这条：配的根就是将来的根。

    lines = "\n".join(
        f"    {count:>4} 个会话的数据在 {root}" for root, count in sorted(stranded.items())
    )
    raise RuntimeError(
        "Session worktree 根与库里记录的不一致 —— 拒绝启动。\n"
        f"  配置指向: {configured}\n"
        f"{lines}\n"
        "  这多半是启动方式变了（相对路径的根会跟着 cwd 走），而不是数据丢了。\n"
        "  fix: 指回记录里的那个根（PLATFORM_DATA_ROOT / PROJECT_WORKTREE_ROOT），\n"
        "       或者真要搬就走 python scripts/relocate_project_data.py --to <新根>\n"
        "  带着错的根服务下去 = 旧会话全报 'worktree is not initialized'，"
        "同时在新根下建第二套数据。"
    )


async def _refuse_to_serve_with_a_broken_runtime() -> None:
    """Harness 桥开着，worker 就必须真的能起来 —— 启动时证明，不等用户发现。

    「继续会话」在实现上要先拉起一个 worker 进程，而它的前置条件（解释器、
    依赖、checkout）都是部署时建立的，随时可能在建立与使用之间坏掉。
    2026-08-21 现场：.venv/bin/python 指着已卸载的 anaconda，唯一活着的
    worker 被登出杀掉后，用户每条消息都撞 "HARNESS_PYTHON is not an
    executable file" —— 这个错的归宿是部署那一刻，不是用户的聊天窗口。

    探针先自愈一次（uv sync --frozen），仍坏才拒绝启动，并把修复命令原样
    给出。与数据根闸同一个哲学：带着坏环境服务下去，只是把同一个错误推迟
    到更糟的时刻、换一张更无辜的脸。
    """
    from app.config import settings as _settings

    if not _settings.harness_bridge_enabled:
        return
    from app.services.harness_sessions import probe_runtime_environment

    problem = await probe_runtime_environment(self_heal=True)
    if problem:
        raise RuntimeError(f"Refusing to start — {problem}")


#: 问这台机器守得住哪几条不变量。**它从不失败** —— 「一条都守不住」也是一个
#: 答案，写进 record 的 unavailable_reason 里。从前这里 `raise SystemExit(...)`，
#: 于是「这台机器没有沙箱」这件事只能以「服务起不来」的形式表达；那是把记账
#: 变成了启动条件（RFC_EXECUTOR_TIERS §3.4：沙箱能力是记账与显示）。
_EXECUTION_BOUNDARY_PROBE = (
    "import json\n"
    "from core import isolation\n"
    "try:\n"
    "    backend = isolation.select_backend()\n"
    "except isolation.IsolationContractError as exc:\n"
    "    record = {\n"
    "        'backend': None,\n"
    "        'policy': isolation.enforcement_policy(),\n"
    "        'enforced': [],\n"
    "        'missing_for_unattended': sorted(c.value for c in isolation.UNATTENDED_MINIMUM),\n"
    "        'missing_for_attended': sorted(c.value for c in isolation.ATTENDED_MINIMUM),\n"
    "        'unavailable_reason': str(exc),\n"
    "    }\n"
    "else:\n"
    "    record = isolation.enforcement_record(backend).as_event()\n"
    "print(json.dumps(record))\n"
)


def _boundary_unavailable(reason: str) -> dict:
    """探针自己跑不起来时的 record —— 仍然是一个答案，不是一次拒绝。"""
    return {
        "backend": None,
        "enforced": [],
        "missing_for_unattended": [],
        "missing_for_attended": [],
        "unavailable_reason": reason,
    }


async def _one_home_per_project() -> None:
    """项目层从前按人分存（`users/<uid>/projects/<项目>`）；合成一个项目一份再起 worker。

    必须在任何 worker 被接回或起来之前：它们一开工就读写新的那一份
    （`docs/RFC_PROJECT_HOME_20260924.md`、`services.one_home_per_project`）。合不动不拦启动 ——
    源目录原封不动，下次启动接着合；但要大声说出来。
    """
    from app.config import data_root, the_projects_home
    from app.services.one_home_per_project import merge_the_per_person_copies

    try:
        await asyncio.to_thread(merge_the_per_person_copies, data_root("state"), the_projects_home())
    except Exception:
        logger.exception("项目层没合成一个项目一份 —— 旧的按人分存的还在原处，下次启动再合")


async def _record_what_this_machine_enforces() -> None:
    """这台机器守得住哪几条不变量 —— 记下来，不据此拒绝启动。

    ## 为什么这里一个 raise 都没有了（2026-09-05，个人版 WP-02 / RFC X6）

    执行器分档 RFC §3.4 的原话是「沙箱能力是**记账与显示**，不是启动条件」。
    可这个函数从前叫 `_refuse_to_serve_without_a_sandbox`，守不住写边界就
    `RuntimeError`，于是同一句话在代码里成了它的反面：一台没装 bwrap 的
    Linux 机器上，连「打开界面看看以前的项目」都做不到。

    个人档把这条推到极限：软件下载下来就得能打开。守不住哪条就如实显示哪条
    （`/health/ready` 的 `execution_boundary`），要不要因此拒绝**由派发那一刻
    决定** —— `core.isolation._resolve_auto` 仍然守着写边界（I1），一个守不住
    I1 的机器不会真的把作业跑起来。判决留在够得着现场的那一层。

    探针本身也不再抛：跑不起来、超时、吐了非 JSON，都变成一条带
    `unavailable_reason` 的 record。**「不知道」和「没有」都是答案，不是故障。**
    """
    if not settings.harness_bridge_enabled:
        app.state.sandbox_readiness = "not_applicable"
        return
    root = Path(settings.harness_root).expanduser().resolve()
    # A venv interpreter is commonly a symlink on macOS. Resolving the final
    # component turns ``.venv/bin/python`` into the base interpreter and loses
    # pyvenv.cfg, so the startup child no longer has the installed environment.
    # Make the configured path absolute without dereferencing its identity.
    from app.services.harness_runtime import (
        harness_subprocess_env,
        the_interpreter_that_runs_the_harness,
    )

    python = Path(os.path.abspath(Path(the_interpreter_that_runs_the_harness()).expanduser()))
    # 一处回答：启动探针要 `import core`（→ asyncio/_overlapped），必须带 Windows 系统变量，
    # 否则 WinError 10106、谎报「没有执行边界」。走 harness_subprocess_env 收口（曾漏掉，见其 docstring）。
    environment = harness_subprocess_env(root, passthrough=(
        "HARNESS_EXECUTOR", "HARNESS_ENFORCEMENT_POLICY", "HARNESS_SANDBOX_CEILING"))
    try:
        proc = await asyncio.create_subprocess_exec(
            str(python),
            "-c",
            _EXECUTION_BOUNDARY_PROBE,
            cwd=str(root),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # 起不起得来这个子进程也是「不知道」的一种：解释器不在、harness 根不在，
        # 都会在这里 FileNotFoundError。2026-09-05 真机点验就是被这一句挡住的 ——
        # 前一版把 spawn 放在 try 之外，于是「探针不再抛」只覆盖了一半的路。
        app.state.execution_boundary = _boundary_unavailable(
            f"could not run the execution boundary probe: {exc}"
        )
        app.state.sandbox_readiness = f"none: {exc}"
        logger.warning("No execution boundary on this host: %s", exc)
        return
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        record = _boundary_unavailable("execution boundary probe timed out after 45s")
    else:
        if proc.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            record = _boundary_unavailable(detail or f"probe exited {proc.returncode}")
        else:
            text = (stdout or b"").decode("utf-8", errors="replace").strip()
            try:
                record = json.loads(text.splitlines()[-1]) if text else {}
            except (ValueError, IndexError):
                record = _boundary_unavailable(f"probe printed non-JSON: {text[:200]}")

    app.state.execution_boundary = record
    reason = record.get("unavailable_reason")
    # 「有个后端」和「这个后端守得住写边界」是两件事：Linux 后端在没有 bwrap 也没有
    # Landlock 的机器上仍然叫 linux，capabilities() 却是空集。按名字判就会在这里说
    # 一句 ok，而它守不住任何东西。判据落在不变量上。
    enforcing = "write_boundary" in (record.get("enforced") or [])
    app.state.sandbox_readiness = "ok" if enforcing else f"none: {reason or 'enforces nothing'}"
    if enforcing:
        logger.info(
            "Execution boundary: backend=%s enforced=%s missing_for_unattended=%s",
            record.get("backend"), record.get("enforced"), record.get("missing_for_unattended"),
        )
    else:
        # 醒目但不致命：这台机器跑不了作业，但界面、账面、历史都还在。
        logger.warning("No execution boundary on this host: %s", reason)


async def _rekey_credentials_that_used_the_published_constant() -> None:
    """把还用出厂密钥加密的凭据换成这份安装自己的密钥。

    2026-09-06 之前，个人档的主密钥是 `sha256("dev-secret-key-change-in-production")`
    —— 源码里的常量，每一份安装都一样。换密钥之后，如果不把存量一并换掉，那些
    弱密文会一直躺在库里：**"以后新写的安全了"不是修复**，用户已经存进去的那把
    才是要保护的东西。

    跑不动不拦启动：这是一次加固，不是服务的前提；失败就吵一句，凭据仍然解得开
    （`decrypt_api_key` 会回退到出厂密钥）。
    """
    from app.database import get_session_factory
    from app.services.model_backends import rekey_legacy_credentials

    try:
        async with get_session_factory()() as session:
            rekeyed = await rekey_legacy_credentials(session)
            if rekeyed:
                await session.commit()
    except Exception as exc:  # noqa: BLE001 - 加固失败不该挡住服务
        logger.warning("凭据换密钥没跑成（不影响使用）：%s", exc)
        return
    if rekeyed:
        from app.services.credential_key import where_the_key_lives

        logger.info(
            "已把 %d 条用出厂密钥加密的凭据换成本机密钥（存放于 %s）",
            rekeyed, where_the_key_lives(),
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown."""
    setup_logging(debug=settings.debug)
    logger.info("Starting %s", settings.app_name)

    # 装配在**所有闸之前**：它决定了鉴权是谁、数据落在哪个根，而那两件事
    # 正是后面几道闸要检查的东西。
    assembly.install(app)

    # 工具执行器的注册随 app.tools 一起删了：它喂的是本服务自己那套 agent loop，
    # 而 /chat/global 与 /chat/projects/{id} 早已返回 410 —— 执行统一走
    # platform_runtime 桥到 harness，工具由 harness 的 tool_registry 提供。
    # 留着等于每次启动都为一个不会运行的 loop 建一张表。

    # 启动时校验 harness YAML 的那一步随图引擎一起删了：它校验的是
    # `backend/app/harnesses/*.yaml`，那是图引擎自带的第二套节点定义。
    # 真正在跑的节点 harness 在 harness-framework 的 `nodes/*/harness.yaml`，
    # 由该仓自己的契约 CI 把关，不经过 App Server。

    from app.database import get_session_factory
    from app.services.harness_sessions import (
        harness_session_manager,
        mark_orphaned_harness_runs,
    )

    from app.database import create_schema_for_unmanaged_databases

    await create_schema_for_unmanaged_databases()
    await _rekey_credentials_that_used_the_published_constant()
    await _refuse_to_serve_a_schema_we_were_not_written_for()
    from app.database import normalise_retired_roles

    await normalise_retired_roles()
    await _refuse_to_serve_where_the_data_is_not()
    await _one_home_per_project()
    await _refuse_to_serve_with_a_broken_runtime()
    await _record_what_this_machine_enforces()
    # 发行的启动钩子（专业版：零用户的组织服务器发一次认领码）。个人版这里是空的。
    for hook in assembly.EDITION_HOOKS.after_startup:
        await hook()

    # uvicorn 的信号 handler 此时已装好；包住它，好让"关机被请求"这个事实从
    # **信号到达的那一刻**起就为真，而不是等连接排空之后的 lifespan 收尾段。
    from app.services.lifecycle import install_shutdown_event, watch_for_shutdown_signal

    watch_for_shutdown_signal()
    # 长活的流靠这个事件在信号到达那一刻醒过来（见 app/services/sse.py）。
    # 没有它，SSE 连接排不空 → uvicorn 永远等下去 → 下面这个 finally
    # 一次都跑不到（2026-08-23 实测三次生产重启）。
    install_shutdown_event()

    # respawn（换绑定杀掉停靠中的 worker）送走的 pause，立刻标成可恢复 ——
    # 与登出（auth.logout → terminate_user → mark_orphaned_harness_runs）同一
    # 条处理路。不接这个钩子的话，下一条消息会先撞一次 stale 才自愈。
    async def _orphan_stale_binding(binding) -> None:
        factory = get_session_factory()
        async with factory() as db:
            await mark_orphaned_harness_runs(db, run_ids={binding.run_id})
            await db.commit()

    harness_session_manager.set_stale_binding_handler(_orphan_stale_binding)
    # 接回一个活 worker 之后，把它正在跑（或刚跑完）的那一轮接到本进程（#785）：
    # 登记观众、补断连窗口、等终止 result，然后同一份收尾在这里跑完。不接的话
    # 退化回 #784 的状态 —— worker 活着，那一轮的账要等下次重启才补（终止 result
    # 永远补不上）。钩子只挂在 `_adopt_locked` 一处：启动对账与 spawn 前接回都经它。
    from app.services.local_execution import rejoin_adopted_session

    harness_session_manager.set_adoption_handler(rejoin_adopted_session)

    app.state.startup_reconciliation = "ok"
    try:
        factory = get_session_factory()
        async with factory() as db:
            # 启动是唯一"注册表为空 ⇒ 全都无主"成立的时刻，也是唯一该动手
            # 杀进程的地方（别处杀会误伤正在干活的子进程）。
            stale_count = await mark_orphaned_harness_runs(db, reap_workers=True)
            await db.commit()
        if stale_count:
            logger.warning("Marked %d orphaned Harness Run(s) stale_unknown", stale_count)
            # 重启就停在那儿，等人再开口（wangd 2026-08-18 明确要求）。
            #
            # 这里一度加过"平台自己接着跑"：它需要伪造一条用户消息才能推进
            # （op=turn 强制非空 message），那条假消息会出现在用户自己的聊天
            # 记录里。用户原话：「重启就重启，给重启还打个补丁」。
            #
            # 走到这里的是**真丢了**的：worker 不再跟着后端死（P0 + 收尾段
            # `detach_all`），能接回来的在上面 `mark_orphaned_harness_runs`
            # 里已经接回来了；剩下的要么进程真没了，要么是接不回来的老 stdio
            # worker。continuous 档由 `restart_resume` 自己续，其它档中断就是
            # 中断，不拿一层机制去补救另一层的后果。
    except Exception as exc:
        app.state.startup_reconciliation = f"error: {type(exc).__name__}"
        logger.exception("Could not reconcile orphaned Harness Runs at startup")

    # 科研资讯流的采集循环。它是平台自己的杂活，不是任何人的研究：起得晚
    # （排在孤儿重整之后）、停得早（下面 finally 的第一件事），失败只影响
    # 资讯流本身。
    from app.services.feed.scheduler import feed_collector
    from app.services.literature_harvester import literature_index_harvester

    if assembly.background_collection_enabled():
        feed_collector.start()
        literature_index_harvester.start()
    else:
        # 个人档不做后台爬取：一个刚装好的软件不该在用户还没说要什么之前
        # 就开始连外网。资讯流功能本身没删，用户点开时按需采（RFC X8）。
        logger.info("background collection is off in this profile")

    # 交付对账：把已完成的自主 run 补发到 Artifacts（命门交付链的自愈网）。
    # 触发挂在 DB 完成状态而不是 execute_local_turn 尾部——见
    # app/services/delivery_scheduler.py 与 deliverable_publishing 的注释
    # （E2E v33 实证：完成由事件 ingestion 确立，turn 尾对它不在场）。
    from app.services.delivery_scheduler import delivery_reconciler

    delivery_reconciler.start()

    # 发行的长活任务（专业版：装出来的组织服务器夜里自己升级）。钩子自己决定起不起。
    long_running: list[asyncio.Task] = []
    for start in assembly.EDITION_HOOKS.long_running:
        coroutine = start()
        if coroutine is not None:
            long_running.append(asyncio.create_task(coroutine, name=f"edition-{start.__name__}"))

    try:
        yield
    finally:
        for task in long_running:
            task.cancel()
        from app.services.delivery_scheduler import delivery_reconciler
        from app.services.lifecycle import begin_shutdown
        from app.services.local_execution import shutdown_detached_executions

        # 装配拆干净：装上不拆，下一次进出 lifespan 就带着上一次的接线跑。
        assembly.uninstall(app)

        # 先停采集：它持有出网连接和一条 DB 事务，而下面的关机步骤会开始
        # 拆进程。让它在自己的边界上退出，别在一次 HTTP 抓取中间被撕掉。
        await feed_collector.stop()
        from app.services.literature_harvester import literature_index_harvester
        await literature_index_harvester.stop()  # 没起过也安全
        # 交付对账同理：它每轮持一条 DB 事务，让它在自己的节拍边界退出。
        await delivery_reconciler.stop()

        # 先立旗再动手：下面每一次在途执行被撤销，都要能分辨"研究失败了"和
        # "我们自己在关机"。旗立晚了，第一条被撤销的 run 就会被记成 failed
        # 并告诉用户"改改请求再试" —— 对着一件没发生的事给建议。
        begin_shutdown()
        await shutdown_detached_executions()
        # 放手，不终止（RFC 异步运行时 P0）：worker 自成进程组、事件落盘、命令面
        # 是 socket —— 后端退场只是"没人在说话"，不是任何一个会话的结束。下一个
        # 后端启动时按注册表把它们接回来（上面 `mark_orphaned_harness_runs` 的
        # 先接后判）。2026-09-03 之前这里是 `terminate_all()`：stdin 子进程年代
        # 的写法，每次部署把停靠中的 worker 全收掉，部署脚本"不动 worker"的
        # 收尾核对因此报红（部署前 1 个 / 部署后 0 个）。
        detached = await harness_session_manager.detach_all()
        logger.info(
            "Shutting down %s (%d Harness worker(s) left running for the next App Server)",
            settings.app_name, detached,
        )


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware (order matters: last added = first executed)
# 转交排在**最里面**（第一个 add = 最后被包）：CORS、体积闸、访问日志三样都要
# 落在一条转交出去的请求上。CORS 尤其 —— 那台服务器回的跨源头对着它自己的名单
# （出厂 :3000），而窗口开在本机一个随机端口上；由这一侧按本机的源重新加。
#
# 这一层的开关是**数据**不是档位：没有任何连接时它对每条请求都是直通
# （`where_this_request_is_answered` 返回 None）。个人版根本建不出连接 ——
# `/connections` 那几个端点在没有 `connections` 能力的发行上是 404。
# 转交中间件由专业版挂（`app/pro`）；这一句同时把它的路由、家、策略钩子接上。
# 个人版没有 app/pro，这一句是空操作。
assembly.wire_the_edition(app)
app.add_middleware(RequestLoggingMiddleware)
# 体积闸在日志之后加 = 在请求路径上**更靠外**（Starlette 按注册倒序包裹），
# 所以被它拒掉的请求仍然进访问日志。
app.add_middleware(RequestBodyCapMiddleware)
_cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
# 写死名单 = 换个端口就默认漏过（这里是默认拒绝）。自部署文档明写"按需改
# 端口"，而 3000 之外的任何端口都会在 CORS 预检拿 400，浏览器只报
# "Failed to fetch" —— 没有任何线索指向 CORS。所以 loopback 用**扫盘**
# （正则匹配任意端口），非 loopback 依旧要显式配置。
_cors_kwargs: dict = {}
if _cors_origins:
    _cors_kwargs["allow_origins"] = _cors_origins
elif settings.local_demo_mode or settings.debug:
    _cors_kwargs["allow_origin_regex"] = r"http://(localhost|127\.0\.0\.1)(:\d+)?"
else:
    _cors_kwargs["allow_origins"] = ["http://localhost:3000", "http://127.0.0.1:3000"]

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    **_cors_kwargs,
)

# Mount API routes
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Basic liveness check."""
    return {"status": "ok"}


def is_ready(checks: dict, *, bridge_enabled: bool) -> bool:
    """就绪 = 这台 App Server 服务得了。

    **沙箱守得住什么不在这里。** 那是一个显示项：守不住的机器照样该能打开界面、
    看历史、读账面；要不要真把作业跑起来由派发那一刻决定
    （`core.isolation._resolve_auto` 仍然守着写边界 I1），那一层够得着现场。

    抽成纯函数是为了让这条判断本身可被变异打到：从路由上验它，就得先让库里
    每张表都在、harness_root 指对 —— 一堆与沙箱无关的前提，其中任何一个不满足
    都会让「加回沙箱这一项」这个变异照样绿（2026-09-05 实测）。
    """
    return (
        checks.get("database") == "ok"
        and checks.get("schema_tables") == "ok"
        and checks.get("alembic_version") in {"ok", "unmanaged"}
        and checks.get("startup_reconciliation") == "ok"
        and (not bridge_enabled or checks.get("harness_root") == "ok")
    )


@app.get("/health/ready")
async def readiness_check() -> dict:
    """Readiness check — verifies DB connectivity and required schema."""
    from sqlalchemy import text

    from app.database import get_session_factory, has_table, table_names

    checks: dict[str, str] = {}
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            connection = await session.connection()
            if await has_table(connection, "alembic_version"):
                applied_versions = set(
                    (
                        await session.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalars()
                )
                expected_versions = _expected_alembic_heads()
                checks["alembic_version"] = (
                    "ok"
                    if applied_versions == expected_versions
                    else (
                        f"expected {', '.join(sorted(expected_versions)) or 'missing'}, "
                        f"got {', '.join(sorted(applied_versions)) or 'missing'}"
                    )
                )
            else:
                # 三态，和启动闸同一个判断：没有 alembic_version 表 = 这个库不是
                # alembic 建的（个人档的 SQLite），不是“落后”。
                checks["alembic_version"] = "unmanaged"
            required_tables = _tables_this_build_reads()
            existing = await table_names(connection)
            missing = [table for table in required_tables if table not in existing]
            checks["schema_tables"] = (
                "ok"
                if not missing
                else (
                    f"missing: {', '.join(missing)} "
                    "— fix: cd platform/backend && alembic upgrade head"
                )
            )
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    checks["local_worker"] = "in_process"
    checks["startup_reconciliation"] = getattr(app.state, "startup_reconciliation", "not_run")
    from app.services.harness_sessions import harness_session_manager

    checks["active_harness_sessions"] = str(harness_session_manager.active_count)
    checks["sandbox"] = getattr(app.state, "sandbox_readiness", "not_run")
    # 这台机器实际守到哪几条不变量（backend / enforced / missing_for_unattended）。
    # 个人机器上「弱资源墙」就在这里被看见，而不是被启动闸挡在门外。
    checks["execution_boundary"] = getattr(app.state, "execution_boundary", None) or {}
    capabilities = {
        "demo_executor": "available" if settings.local_demo_mode else "disabled",
        "formal_harness": "not_configured",
    }

    if settings.harness_root:
        from pathlib import Path

        harness_root = Path(settings.harness_root).expanduser()
        harness_valid = (harness_root / "core" / "agent_loop.py").is_file()
        checks["harness_root"] = "ok" if harness_valid else "invalid"
        if settings.harness_bridge_enabled:
            capabilities["formal_harness"] = "available" if harness_valid else "degraded"
        else:
            capabilities["formal_harness"] = "disabled"
    else:
        checks["harness_root"] = "not_configured"

    all_ok = is_ready(checks, bridge_enabled=settings.harness_bridge_enabled)
    return {
        "status": "ready" if all_ok else "degraded",
        "checks": checks,
        "capabilities": capabilities,
    }


# ── 静态 UI ────────────────────────────────────────────────────────────────────
if settings.static_ui_root:
    from app.static_site import StaticUI

    # 中间件不是路由：它跑在路由之前，只接管"不是 API、应用自己也没有"的地址。
    # 为什么不能是一条 catch-all 路由，见 StaticUI 的文档 —— 一句话：那样会把
    # API 的补斜杠重定向变成 405，而且只在装出来的包里发生。
    app.add_middleware(
        StaticUI,
        root=Path(settings.static_ui_root).expanduser().resolve(),
        api_prefix=settings.api_v1_prefix,
        owner=app,
    )
