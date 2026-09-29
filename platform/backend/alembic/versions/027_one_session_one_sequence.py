"""一个会话一条时间线：消息与执行事件共用一个序号发生器；呈递有身份。

## 在修什么

`sessions` 表原本有两个计数器，各自从 0 开始：

    next_message_sequence   → session_messages.sequence
    next_event_sequence     → execution_events.sequence

于是"消息 3"和"事件 3"是两个毫不相干的东西，而两列在类型上一模一样 ——
谁把它们放在一起比较都不会报错。前端就这么比了（`messageRunSegments` 用消息号
切窗口、`inWindow` 拿事件号落窗口），注释里还白纸黑字写着「消息和执行事件共用
同一个会话级 sequence 空间」。实测会话 e46448f0：消息号 1–5、事件号 1–869 ——
一条 run 的全部活动统统落进第一条消息的开放尾窗，13:32 的动作画在 13:17 的
对话上面。

"这段活动发生在哪两条消息之间"是一条时间线上的先后问题。要它有答案，两者就
必须在同一个序里，而那只能有一个发生器。

## 存量怎么办

新号从此共用一个发生器，但库里已有的消息号仍停在旧的小空间里 —— 不回填的话，
所有历史会话的顺序照旧是错的，而且错得看不出来。

回填的依据是**库里现成的事实**：每条消息在 `execution_events` 里都有一条
`kind='session.message'`，那条事件的序号就是这条消息在时间线上的真实位置。
配对按正文（而不是按名次）——名次会被多出来或少掉的一条整体带偏，正文不会。

配不上的消息（老会话的事件流被裁剪过、或早于摄取上线）按"紧跟着上一条已定位
的消息"顺延。它们的绝对位置无从考证，但相对次序保得住，而那正是渲染需要的。

回填分两趟写：先把全部消息挪进一段无人使用的**高位号段**，再写进最终值。
`uq_session_messages_sequence` 是即时生效的，一趟写会在中途撞上还没搬走的旧号。

中转段取在"全库最大号"之上（消息和事件两边都算），这样它既不与任何旧号相撞，
也不与任何将要写入的目标号相撞。第一版用的是负数区 —— 更直观，但真实库里有
`ck_session_messages_sequence_positive`（sequence >= 1），一上来就炸。
（判据不能凭空推：拿真库演练一遍才看得见。）
"""

from collections import defaultdict

from alembic import op
import sqlalchemy as sa

revision = "027_one_session_one_sequence"
down_revision = "026_model_roles"
branch_labels = None
depends_on = None


def _targets(messages, events):
    """算出每条消息在事件序里的位置。返回 [(message_id, sequence), …]。

    `messages` / `events` 都按各自的旧序号升序给。事件只给 `session.message`
    那一类。返回的序号严格递增 —— 唯一约束要的就是这个。
    """
    by_content = defaultdict(list)
    for index, (content, _sequence) in enumerate(events):
        by_content[content].append(index)
    taken = [False] * len(events)

    out = []
    cursor = 0                      # 最后一次定位到的事件序号
    for message_id, content in messages:
        anchored = None
        # 同一句话可能出现多次（"继续"）；按出现顺序取还没被认领的那一条，
        # 且只往后看 —— 消息的先后本身就是约束。
        for index in by_content.get(content, ()):
            if not taken[index] and events[index][1] > cursor:
                anchored, taken[index] = events[index][1], True
                break
        cursor = anchored if anchored is not None else cursor + 1
        out.append((message_id, cursor))
    return out


def upgrade() -> None:
    bind = op.get_bind()

    # 0) 一条消息可以**就是**一次呈递。带上它的身份，待答卡片才能画在这条消息
    #    的位置上，而不是靠"文案一字不差"去猜（见 SessionMessage.offer_id）。
    #    历史行留空：老会话没有这份身份，前端退回到底部那块兜底面板。
    op.add_column(
        "session_messages",
        sa.Column("offer_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_session_messages_offer_id", "session_messages", ["offer_id"]
    )

    # 1) 事件计数器改名成它现在回答的问题：整个会话的序号发生器。
    op.alter_column("sessions", "next_event_sequence", new_column_name="next_sequence")

    # 2) 全部消息先挪进一段无人使用的高位号段。
    #
    #    段起点 = 全库最大号（消息与事件取大者）。于是中转值既高于所有旧号，
    #    也高于所有将要写入的目标号（目标号来自事件序，上界就是事件最大号）,
    #    两趟写都不会撞唯一约束。
    staging_base = bind.execute(sa.text("""
        SELECT GREATEST(
            COALESCE((SELECT max(sequence) FROM session_messages), 0),
            COALESCE((SELECT max(sequence) FROM execution_events), 0)
        )
    """)).scalar_one()
    bind.execute(sa.text("""
        WITH ranked AS (
            SELECT id, row_number() OVER (ORDER BY session_id, sequence) AS rank
            FROM session_messages
        )
        UPDATE session_messages AS m SET sequence = :base + r.rank
          FROM ranked r WHERE m.id = r.id
    """), {"base": int(staging_base)})

    # 3) 按会话回填。数据量是"人说过的话"，一个会话几十条 —— 在 Python 里走一遍
    #    比把这套配对逻辑挤进一条 SQL 可读得多，而可读性正是回填脚本最该有的。
    sessions = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT DISTINCT session_id FROM session_messages")
        )
    ]
    for session_id in sessions:
        messages = list(
            bind.execute(
                sa.text(
                    "SELECT id, content FROM session_messages"
                    " WHERE session_id = :s ORDER BY sequence"
                ),
                {"s": session_id},
            )
        )
        # 中转段保序（rank 是按 (session_id, 旧 sequence) 排的），所以升序就是
        # 对话正序。
        messages = [(row[0], row[1]) for row in messages]
        events = [
            (row[0], int(row[1]))
            for row in bind.execute(
                sa.text(
                    "SELECT payload->>'content' AS content, sequence"
                    " FROM execution_events"
                    " WHERE session_id = :s AND kind = 'session.message'"
                    " ORDER BY sequence"
                ),
                {"s": session_id},
            )
        ]
        for message_id, sequence in _targets(messages, events):
            bind.execute(
                sa.text("UPDATE session_messages SET sequence = :q WHERE id = :i"),
                {"q": sequence, "i": message_id},
            )

    # 4) 发生器要追上它已经发出去的最大号，否则下一次分配会撞历史行。
    bind.execute(sa.text("""
        UPDATE sessions s
           SET next_sequence = GREATEST(
                   s.next_sequence,
                   COALESCE((SELECT max(sequence) FROM session_messages m
                              WHERE m.session_id = s.session_id), 0),
                   COALESCE((SELECT max(sequence) FROM execution_events e
                              WHERE e.session_id = s.session_id), 0))
    """))

    # 5) 第二个计数器连同它的约束一起删掉 —— 留着就还会有人从它领号。
    op.drop_constraint(
        "ck_sessions_message_sequence_nonnegative", "sessions", type_="check"
    )
    op.drop_column("sessions", "next_message_sequence")


def downgrade() -> None:
    # 回不去：两个空间合并之后，"这个号原本属于哪个计数器"这件事已经不存在了。
    # 与其发明一段假的历史号段，不如如实拒绝。
    raise NotImplementedError(
        "027 合并了消息与事件的序号空间；拆回两个计数器需要凭空发明历史号段"
    )
