"""把 worker 的一条 transcript 包装事件变成一条持久记录 —— **只有这一份实现**。

## 为什么单独成模块

同一件事有两条到达路径：

  1. **实时**：worker 在 socket 上说话，`local_execution.ingest_harness_protocol`
     一条条摄取；
  2. **补齐**：后端不在的那段时间里 worker 照样在说话，事件落在
     `events.jsonl` 里，恢复之后从文件里补读（RFC 异步运行时 P0-3）。

两条路径必须逐字节做同一件事。各写一份的下场是可以预演的：补齐路径少解析
一个字段、少走一次归属判定，于是"重启之后那段历史"和"实时那段历史"在库里
长得不一样，而两边都不报错 —— 只有人去翻某一条 run 的时间线时才会发现中间
少了一截。所以这里只有一份，两条路径都调它。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DataRootError, data_root
from app.models.execution import ExecutionEvent
from app.services.execution_ingest import (
    LOCAL_RECORD_IDENTITY_PREFIX,
    NARRATION_IDENTITY_SUFFIX,
    RECOVERED_CHILD_RUN_KEY,
    ExecutionIngestService,
    IngestContext,
)

#: 一条 transcript 记录的字节上限。超过就是包装事件坏了，不是一条大记录。
MAX_TRANSCRIPT_RECORD_BYTES = 5_000_000



def _session_runtime_root(project_id: str, session_id: str) -> Path:
    from app.services.project_repository import get_project_repository

    from app.services.session_runtime_paths import resolve_existing

    worktree = get_project_repository().session_path(project_id, session_id)
    return resolve_existing(worktree, "runs").resolve()


def _approved_runtime_root(path: Path, project_id: str, session_id: str) -> Path | None:
    """Return the trusted runtime root containing ``path``.

    New Harness sessions keep runtime scratch in the Session worktree.  The
    configured legacy state root remains an explicit compatibility boundary
    for already-running bridge processes and test doubles during rollout; an
    arbitrary path is never accepted.
    """
    roots = [_session_runtime_root(project_id, session_id)]
    try:
        roots.append(data_root("state").resolve())
    except DataRootError:
        pass
    return next((root for root in roots if path.is_relative_to(root)), None)


def _harness_adapter_state(states: dict[str, dict], file_identity: str,
                           owning: bool, *, recovered_child_run_id: str | None = None) -> dict:
    """Per-transcript adapter state, tagged with which file owns this Run.

    子节点 run 有自己的 transcript 文件（因此有自己的 adapter_state），单文件内
    的深度计数看不见跨文件嵌套。归属判据是**这个 run 的第一份 transcript**：
    bridge 从 run 起就在 tail orchestrator transcript，之后出现的任何 transcript
    都是子节点 run，其生命周期不得驱动父 Run/attempt 状态机。

    ⚠️ 判据必须是 **run 级**的，不能是 turn 级的。
    -------------------------------------------------------------------------
    原实现是 `not states` —— `states` 是 `execute_local_turn` 的局部变量，
    **每个 turn 清零**，于是它实际回答的是"本轮第一个出声的文件"。

    第一轮两者恰好相等（orchestrator 从 run 起就在被 tail，它就是第一个），
    **续跑不相等**：2026-08-19 session 976e70e1，05:29:41 人答完 pause，恢复点
    在 experiment 子节点内部，本轮第一个出声的是它的 transcript → 它被盖上
    `owningRun: true` → 05:47:56 它的 `run_end` 关掉了**父 run** 的 attempt。
    真正的 orchestrator transcript 直到 05:48:35 才出声，晚了 48 秒。

    随后 run 被 `decision.required` / `run.paused` 拉回 waiting_human（这两个
    kind 不在 owningRun 闸门的名单里），而 attempt 没人拉回 —— 两条生命周期
    永久分叉，之后**所有人工答复被判 stale**。

    判据现在读**事实表**（见 `_owning_transcript`）—— run 级、与摄取同源。
    """
    existing = states.get(file_identity)
    if existing is not None:
        return existing
    state: dict = {"owningTranscript": owning}
    if not owning:
        # 「这一次派发」的身份 = **这份 transcript 文件**（`_root_step_id` 读它）。
        #
        # 不给它，`_root_step_id` 回落到 `(run_id, attempt_no, 'root')`。而子 run
        # id 标识的是**槽位**（父 run + 节点类型 + 深度），不是这一次派发 ——
        # 同一个节点被重新派发时算出同一个 step id，新的 `step.started` 被
        # `_insert_event` 按 event_id 幂等**静默吞掉**，新一轮的工具全部追加到
        # 上一次那张已标"完成"的卡上，而「当前」恒空。
        #
        # 这件事 2026-08-20 修过一次，修的是 `ingest_transcript_file` —— 那条路
        # 生产环境从来没走过（零调用方）。实测 2026-08-25 session 2220d882：
        # hypothesis 派发 7 次，`_orchestrator->hypothesis@d1` 名下
        # `step.started` **1 条**、distinct stepId **1 个**，六次派发全被吞。
        # 修在没人走的那条路上，等于没修。
        #
        # 文件身份恰好就是要的语义：新的一次派发是新文件（新 inode），同一趟
        # 续跑是追加进同一份文件。
        state["dispatchKey"] = file_identity
        # 子节点身份也得跨 turn 活下来（#947）。见 `_child_run_of` 的 docstring：
        # 它只写在这份 transcript 的**第一条** `run_start` 里，而 `states` 每个 turn
        # 清零、续跑又从字节偏移接着读，那一行不会被重读。`dispatchKey` 一直是从
        # 文件身份现算的所以没事，身份这一半却随 turn 一起消失了 —— 同一个事实
        # 两个半边，一个持久一个易失。
        if recovered_child_run_id:
            state[RECOVERED_CHILD_RUN_KEY] = recovered_child_run_id
    states[file_identity] = state
    return state


async def _child_run_of(db: AsyncSession, context: IngestContext,
                        file_identity: str) -> str | None:
    """这份 transcript 的事件上一次被记到哪条**子** run 上 —— 跨 turn 的持久事实。

    ## 病例（#947）

    同一次 `submit_job`：第一次调用在 Experiment child 上产生
    `tool.started(dry_run=false)` 与 `tool.completed(status=pause)`；人批准之后
    真正提交的第二次调用却产生在**父 project_chat root** 上、`status=success`。
    于是公开审计无法把真实受管提交绑定到产出三件套的那条 Experiment run，
    按 child producer 验证真实执行的 benchmark 会正确地失败。

    ## 机制

    子节点身份 (`sub_run_id` / `parent_run_id` / `depth`) 只出现在它那份 transcript
    的**第一条** `run_start` 里（见 `execution_ingest.child_run_identity`），之后每
    一行都不带。平台把它记在 `adapter_state` 里 —— 而 `harness_states` 是
    `execute_local_turn` 的局部变量，**每个 turn 清零**。

    人答复 pause 是**新的一轮**：新 turn、空 states、从上次的字节偏移接着读，
    那条 `run_start` 不会被重读。于是这份文件的 adapter_state 里没有子节点身份，
    `ingest_raw_record` 不再把 context 换成子 run，事件全部落回父 run。

    ## 修法

    身份早就落库了 —— 这份文件此前的每一条事件都记着它归谁（`run_id` +
    `file_identity`）。从**那张表**取回来，而不是让它随 turn 一起消失。
    判据与 `_owning_transcript` 同源（都问 `execution_events`），不新开第二本账。
    """
    prefix = f"{context.run_id}::"
    attributed = await db.scalar(
        select(ExecutionEvent.run_id)
        .where(
            ExecutionEvent.tenant_id == context.tenant_id,
            ExecutionEvent.session_id == context.session_id,
            ExecutionEvent.file_identity.in_(
                [file_identity, f"{file_identity}{NARRATION_IDENTITY_SUFFIX}"]
            ),
            ExecutionEvent.run_id.startswith(prefix),
        )
        .order_by(ExecutionEvent.sequence.desc())
        .limit(1)
    )
    if not attributed:
        return None
    return str(attributed)[len(prefix):] or None


async def _adapter_state_for(db: AsyncSession, context: IngestContext,
                             states: dict[str, dict], file_identity: str) -> dict:
    """取这份 transcript 的 adapter_state；**新建时**才去库里问那两件持久事实。

    此前归属判定是作为实参 `await` 的（`_harness_adapter_state(..., await
    _owning_transcript(...))`）—— 于是**每一行**都跑两条 COUNT，哪怕这份文件的
    state 早就缓存住了。这里先看缓存，命中就直接返回。
    """
    cached = states.get(file_identity)
    if cached is not None:
        return cached
    owning = await _owning_transcript(db, context, file_identity)
    recovered = None if owning else await _child_run_of(db, context, file_identity)
    return _harness_adapter_state(
        states, file_identity, owning, recovered_child_run_id=recovered
    )


async def _owning_transcript(db: AsyncSession, context: IngestContext,
                             file_identity: str) -> bool:
    """这份 transcript 是不是**这个 run** 的第一份。

    判据读 `execution_events` —— 摄取写进去的**那张表**。曾经它读的是旁边一本
    账（`TranscriptIngestCheckpoint`），而生产路径从来不写那本账：全库 0 行，
    于是 `not total` 恒真，每一份子节点 transcript 都自称"我是本 run 的第一份"。
    实测（2026-08-25，session 2220d882）10 个 run 的 `owningRun` 全是 true，
    包括孙节点 —— 这道闸从落地起就没挡住过任何东西。

    测试一直是绿的，因为测试走的是写那本账的另一条摄取路（`ingest_transcript_file`），
    生产走 `ingest_transcript_wrapper`。**同一个判据在两条路下答案相反，而断言
    落在没人走的那条上。** 判据必须问真正在写的那张表。

    口径：这个 file 已经为本 run 供过稿（→ 是），或者本 run 还没收过任何
    transcript（→ 它是第一个）。都不成立 = 后来者 = 子节点 run。
    """
    # 只数 worker transcript。平台自己写的记录（run_start / session_message…）
    # 用 `LOCAL_RECORD_IDENTITY_PREFIX` 打头，它们在第一条 transcript 之前就
    # 落库了 —— 把它们算进"本 run 收过东西没有"，父 transcript 自己就会被判成
    # 后来者，闸从恒真翻成恒假，同样错。
    def _is_transcript(column):
        return (
            column.is_not(None)
            & (column != "")
            & ~column.startswith(LOCAL_RECORD_IDENTITY_PREFIX)
        )

    mine = await db.scalar(
        select(func.count())
        .select_from(ExecutionEvent)
        .where(
            ExecutionEvent.tenant_id == context.tenant_id,
            ExecutionEvent.run_id == context.run_id,
            # 同一份文件的叙述条目带派生后缀（execution_ingest 里加的），
            # 它也是"这个 file 供过稿"的证据。
            ExecutionEvent.file_identity.in_(
                [file_identity, f"{file_identity}{NARRATION_IDENTITY_SUFFIX}"]
            ),
        )
    )
    if mine:
        return True
    total = await db.scalar(
        select(func.count())
        .select_from(ExecutionEvent)
        .where(
            ExecutionEvent.tenant_id == context.tenant_id,
            ExecutionEvent.run_id == context.run_id,
            _is_transcript(ExecutionEvent.file_identity),
        )
    )
    return not total


def resolve_wrapper(
    event: dict[str, Any], *, project_id: str, session_id: str
) -> tuple[Path, int, int]:
    """校验一条 transcript 包装事件，返回（文件、起、止）。

    越界或字节区间不合法一律抛 —— 这两件事都不是"这条记录有点怪"，是"有人
    在让我们读一个不该读的文件"。
    """
    transcript_path = Path(str(event.get("transcript_path") or "")).resolve()
    state_root = _approved_runtime_root(transcript_path, project_id, session_id)
    if state_root is None or not transcript_path.is_file():
        raise RuntimeError("Harness transcript escaped the configured state root")
    byte_start = int(event.get("byte_start") or 0)
    byte_end = int(event.get("byte_end") or 0)
    if (
        byte_start < 0
        or byte_end <= byte_start
        or byte_end - byte_start > MAX_TRANSCRIPT_RECORD_BYTES
    ):
        raise RuntimeError("Harness transcript wrapper has an invalid byte range")
    return transcript_path, byte_start, byte_end


def file_identity_of(path: Path) -> str:
    """一份 transcript 的身份 = 它的 (设备, inode)。

    不用路径：同一个 session 换代重建目录时路径会重合，而 inode 不会 ——
    幂等键要认得出"这是不是同一份文件"。
    """
    stat = path.stat()
    return f"{stat.st_dev:x}:{stat.st_ino:x}"


async def ingest_transcript_wrapper(
    db: AsyncSession,
    *,
    service: ExecutionIngestService,
    context: IngestContext,
    event: dict[str, Any],
    harness_states: dict[str, dict],
) -> dict[str, Any]:
    """摄取一条 transcript 包装事件，返回它内部那条原生事件。

    调用方拿返回值做自己的副作用（进度播报、checkpoint 请求…）；**摄取本身
    只有这一处**。

    ## 原生记录只认 transcript 文件里那份

    包装事件里同一条记录躺着两份：`[byte_start, byte_end)` 那段字节，和
    `event` 里那个已解析的 dict。原来 `raw_line` 取前者、`raw` 取后者 ——
    两个真相源，而它们不必相等。

    2026-08-23 起真的不相等了：事件写入面会把超阈值的大字段外置成引用
    （P0-7），于是 `event` 里可能是一张提货单，而 transcript 文件里是原文。
    照 `event` 摄取会把提货单当成研究记录存进库。

    所以 `raw` 也从字节现解。文件是事实，包装只是信封。
    """
    transcript_path, byte_start, byte_end = resolve_wrapper(
        event, project_id=context.project_id, session_id=context.session_id
    )
    with transcript_path.open("rb") as source:
        source.seek(byte_start)
        raw_line = source.read(byte_end - byte_start).rstrip(b"\r\n")
    try:
        native = json.loads(raw_line)
    except ValueError as exc:
        raise RuntimeError(
            f"Transcript bytes [{byte_start},{byte_end}) of {transcript_path} are not JSON"
        ) from exc
    if not isinstance(native, dict):
        raise RuntimeError("A transcript record must be a JSON object")
    file_identity = file_identity_of(transcript_path)
    await service.ingest_raw_record(
        db,
        context=context,
        file_identity=file_identity,
        byte_offset=byte_start,
        raw_line=raw_line,
        raw=native,
        adapter_state=await _adapter_state_for(db, context, harness_states, file_identity),
    )
    return native
