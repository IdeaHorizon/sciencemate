"""删掉 `transcript_ingest_checkpoints` —— 一本从来没人记的账。

这张表只有一个写者：`ExecutionIngestService.ingest_transcript_file`。而那个
方法**零生产调用方**（只有测试在调）。生产的两条摄取路径 ——
`ingest_transcript_wrapper`（worker 在说话）和 `replay_missed_events`（从
events.jsonl 补齐）—— 都不写它。实测 2026-08-25：全库 0 行。

## 代价不是"多张空表"，是两道闸从落地起就没生效过

两个判据读这本空账，因此都恒定给同一个答案：

1. `_owning_transcript`：「这份 transcript 是不是本 run 的第一份」。0 行 →
   `not total` 恒真 → **每一份子节点 transcript 都自称是第一份**。实测
   session 2220d882 的 10 个 run（含孙节点）`owningRun` 全是 true。这道闸
   要防的正是子节点的 `run_end` 关掉父 run 的 attempt。

2. `_root_step_id` 读的 `dispatchKey`：只在 `ingest_transcript_file` 里被
   写进 adapter_state。生产路径拿不到它，于是同一个节点每次重新派发都算出
   同一个 step id，新的 `step.started` 被幂等插入静默吞掉。实测同一会话：
   hypothesis 派发 7 次，库里 `step.started` 1 条、distinct stepId 1 个。

两道闸的修复都写在这条没人走的路上，测试也都断言在这条路上 —— 于是
**判据在两条路下答案相反，而断言落在没人走的那条**，CI 一直全绿。

修法不是给这本账补一个写者（那是给死概念续命），是让判据去问真正在写的那
张表：`execution_events`。它本来就是事实所在，`replay_missed_events` 的水位
也一直是从它现算的。表删掉之后，这两个问题各自只剩一个真相源。

Revision ID: 032_drop_ingest_checkpoints
Revises: 031_event_source_dead_key
"""

from alembic import op

revision = "032_drop_ingest_checkpoints"
down_revision = "031_event_source_dead_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("transcript_ingest_checkpoints")


def downgrade() -> None:
    # 不重建。这张表的内容是派生的（字节水位现在从 execution_events 现算），
    # 空表重建出来也只是把那两道恒定失效的闸再装回去。
    raise NotImplementedError(
        "transcript_ingest_checkpoints was never written in production; "
        "it is not restorable and nothing reads it"
    )
