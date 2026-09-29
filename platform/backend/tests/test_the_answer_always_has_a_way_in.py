"""**能驱动这个会话的人，任何时刻都恰好有一个可用的答复入口。**

这条不变量是 2026-09-01 那次事故的全部内容。现场（node20 会话 de9bd47f，
cuikl 的肺腺癌综述，卡了 6 小时 25 分）：

    屏幕上：  「Your input is needed · Choose a response below to continue this run.」
    下面：    什么都没有 —— 一个选项都没画出来
    输入框：  灰的，「This Run is paused for input.」

三段互不相干的代码各自给出了一个自洽的答案：

    后端 canSend=false              「输入框关掉，答案走卡片」
    后端 pendingApproval 有 5 个选项 「卡片的内容在这儿」
    前端 ChatRunActivity            「status='alive' 不在我的名单里 → 不画」

没有任何一层报错，因为每一层单独看都是对的。错的是**这个问题有三个答案**。

所以这里测的不是"卡片画没画出来"（那是渲染，测不到），而是**后端有没有可能
产出那个组合**。产不出来，前端就没有东西可以据以锁死用户。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.execution import AWAITING_HUMAN_RUN_STATUSES, Run, RunStatus
from app.services import execution_view as view


def _run(status: str) -> Run:
    run = Run()
    run.id = "run_3fac3c518099434aa597a7b06749ad53"
    run.status = status
    run.started_at = datetime(2026, 9, 1, 2, 5, 51, tzinfo=UTC)
    run.created_at = run.started_at
    run.summary = None
    return run


#: 现场那份 `pendingApproval` —— 从 node20 生产库原样取回（会话 de9bd47f）。
#:
#: 注意 `optionDetails` 是空的、`offer` 是 null：写事件那一端当时还在发平铺
#: 字段，而读事件那一端已经只认 `offer`。**自造样本会把这个形状造没**，而
#: 恰恰是这个形状（呈递没有身份）让 UI 掉进坏的那条渲染分支。
INCIDENT_PAUSE = {
    "runId": "run_3fac3c518099434aa597a7b06749ad53",
    "reason": "waiting_human",
    "prompt": "这篇综述的选题方向，你希望我按哪个来写？（这决定文献铺陈范围和全文主线）",
    "context": "你给了 5 个可选方向，默认是“综合前沿综述”。",
    "options": ["综合前沿综述", "靶向治疗专项", "免疫治疗专项", "早筛早诊专项", "耐药机制专项"],
    "optionDetails": [],
    "recommendedOptionIndex": None,
    "offer": None,
    "askingNodeType": "_orchestrator",
    "pauseKind": "structured_question",
}

#: 同一次呈递，写端修好之后的形状（0bb3e1f 起）：身份齐全。
#: 两个来源一起喂 —— 一个真样本只证明理解自洽，第二个来源才照得出
#: "身份缺席时走的是另一条路"。
REPAIRED_PAUSE = {
    **INCIDENT_PAUSE,
    "optionDetails": [
        {"id": f"option_{i}", "label": label, "description": "", "recommended": i == 1}
        for i, label in enumerate(INCIDENT_PAUSE["options"], start=1)
    ],
    "recommendedOptionIndex": 0,
    "offer": {
        "offer_id": "orchestrator__…:q1e8085aa:oa0a7478b",
        "decision_id": "orchestrator__…:q1e8085aa",
        "recommended_choice_id": "option_1",
    },
}


# ── 不变量本身 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pause", [INCIDENT_PAUSE, REPAIRED_PAUSE, None, {}])
@pytest.mark.parametrize("status", [s.value for s in RunStatus])
@pytest.mark.parametrize("may_drive", [True, False])
def test_a_driver_is_never_left_without_a_way_to_answer(pause, status, may_drive) -> None:
    """状态 × 卡片在不在 × 有没有驾驶权 —— 全组合扫一遍。

    这是**扫盘**不是名单（[[护栏要扫盘不要写名单]]）：加第 14 个 run 状态、
    或者上游换一种 pause 形状，都自动进这张表，不需要谁记得回来补一行。
    """
    built = view.build_session_view(
        _run(status), observed_status=status, pause=pause, may_drive=may_drive,
        readonly_reason="只读" if not may_drive else None,
    )
    answer = built["answer"]
    assert answer["via"] in {view.VIA_COMPOSER, view.VIA_PAUSE, view.VIA_NONE}

    # ① 说了走卡片，就一定带着卡片。反过来也成立：没带卡片就不许说走卡片。
    assert (answer["via"] == view.VIA_PAUSE) == bool(answer.get("pause")), (
        status, bool(pause), may_drive,
    )
    # ② 能驱动的人永远有入口。**这一条就是那 6 小时。**
    if may_drive:
        assert answer["via"] != view.VIA_NONE, (status, bool(pause))
    else:
        assert answer["via"] == view.VIA_NONE
        assert answer["reason"]


def test_waiting_for_a_person_without_the_question_reopens_the_composer() -> None:
    """在等人回答、却拿不出那个问题 —— 唯一诚实的出口是把输入框还给人。

    这正是事故那一刻的形状（卡片渲染不出来）。旧代码在这里无条件
    `canSend=False`，于是"没有卡片"和"关掉输入框"同时成立，人被锁在中间。
    """
    for status in AWAITING_HUMAN_RUN_STATUSES:
        built = view.build_session_view(
            _run(status.value), observed_status=status.value, pause=None,
        )
        assert built["waitingOn"]["kind"] in {"human", "permission"}
        assert built["answer"]["via"] == view.VIA_COMPOSER, status
        # 而且要如实说出来 —— 降级不许静默。
        assert built["answer"]["degraded"] == view.DEGRADED_NO_PAUSE_BODY, status


def test_the_incident_payload_now_produces_a_usable_entry() -> None:
    """把现场那份 payload 原样喂回去：它必须产出一个能用的入口。

    旧实现在这里给的是 canSend=False + 一份放在别处的 pendingApproval，
    而"别处"那份能不能画出来，这个函数根本不知道。
    """
    built = view.build_session_view(
        _run(RunStatus.WAITING_HUMAN.value),
        observed_status=RunStatus.WAITING_HUMAN.value,
        pause=INCIDENT_PAUSE,
        live_runtime=False,
    )
    assert built["answer"]["via"] == view.VIA_PAUSE
    assert built["answer"]["pause"]["options"][0] == "综合前沿综述"


def test_waiting_for_compute_does_not_take_the_composer_away() -> None:
    """没有人能替算力回答 —— 关掉输入框只是让人连插话都做不到。"""
    built = view.build_session_view(
        _run(RunStatus.WAITING_COMPUTE.value),
        observed_status=RunStatus.WAITING_COMPUTE.value,
        pause=None,
    )
    assert built["waitingOn"]["kind"] == "compute"
    assert built["answer"]["via"] == view.VIA_COMPOSER
    assert "degraded" not in built["answer"]


# ── 结构判据：一条 run 的 view 里不许有答复入口 ──────────────────────────────


def test_a_run_view_carries_no_answer_affordance() -> None:
    """`build()` 是给单条 run 用的，它**不带** `answer`。

    带上就等于允许"从一条早就结束的 run 上渲染出一个能点的卡片"。历史 run
    停在哪个问题上是**记录**，记录永远只读。
    """
    built = view.build(_run(RunStatus.WAITING_HUMAN.value),
                       observed_status=RunStatus.WAITING_HUMAN.value)
    assert "answer" not in built
    assert "canSend" not in built, "canSend 是被删掉的那个孤立布尔，不许回来"


def test_the_session_payload_has_no_second_copy_of_the_pause() -> None:
    """会话 payload 里不许再出现平级的 `pendingApproval`。

    结构判据，扫的是**源码**：那个字段与 `answer.pause` 是同一件事的两份抄件，
    而两份抄件就是这次事故的引擎。行为判据在任何单一时刻都可能一致 ——
    它们是**各自演化**才出的事。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = [
        f"{path.relative_to(root)}:{n}"
        for path in root.rglob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if '"pendingApproval"' in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"这些地方又把待答问题发成了平级字段：{offenders} —— "
        "它必须长在 executionView.answer.pause 里，和入口是同一个字段"
    )


# ── 归并：同一次暂停的三条事件，回放真实那三条 ─────────────────────────────


#: node20 会话 de9bd47f 的 `execution_events` 里那次暂停的**全部三条** run.paused
#: （sequence 8 / 10 / 12），原样取回。三个 ingest 分支各落一条，几十毫秒之内，
#: 各自带着不同的字段子集。
INCIDENT_PAUSE_EVENTS = [
    (8, {
        "prompt": "这篇综述的选题方向，你希望我按哪个来写？（这决定文献铺陈范围和全文主线）",
        "reason": "waiting_human",
        "submissionId": "unattended-99c19e78a6f14de48df2ef2808a51770",
        "detailsUnavailable": True,
    }),
    (10, {
        "prompt": "这篇综述的选题方向，你希望我按哪个来写？（这决定文献铺陈范围和全文主线）",
        "reason": "waiting_human",
        "submissionId": "unattended-99c19e78a6f14de48df2ef2808a51770",
    }),
    (12, {
        "prompt": "这篇综述的选题方向，你希望我按哪个来写？（这决定文献铺陈范围和全文主线）",
        "reason": "waiting_human",
        "context": "你给了 5 个可选方向，默认是“综合前沿综述”。",
        "options": ["综合前沿综述", "靶向治疗专项", "免疫治疗专项", "早筛早诊专项", "耐药机制专项"],
        "pauseKind": "structured_question",
        "resumable": True,
        "askingNodeType": "_orchestrator",
    }),
]


@pytest.mark.asyncio
async def test_the_three_bubbles_of_one_pause_merge_into_one_question(db_session) -> None:
    """三条讲的是同一次呈递，合起来才是完整的那个问题。

    ⚠️ 这条**测不出**新旧实现的差别，我实测过：把 `max(open_pauses,
    key=len(payload))` 放回去，它照样全绿 —— 因为在这份真实数据上 seq 12 恰好
    既最长又最全。所以它守的是另一件事：**到达次序不影响答案**（ingest 是三个
    分支各自落库，顺序没有保证）。

    新旧规则真正分岔的地方在下一条：窗口里出现**两次不同的呈递**时，
    "谁字段多"会挑中已经被取代的那一次。
    """
    from itertools import permutations

    from app.config import settings
    from app.models.execution import ExecutionEvent, Run
    from app.services.sessions import _pending_approval

    for order in permutations(INCIDENT_PAUSE_EVENTS):
        session_id = "s-" + "".join(str(seq) for seq, _ in order)
        run = Run(
            id=f"run-{session_id}", tenant_id=settings.runtime_tenant_id,
            workspace_id="w", project_id="p", session_id=session_id,
            status=RunStatus.WAITING_HUMAN.value,
        )
        db_session.add(run)
        for arrival, (seq, payload) in enumerate(order, start=1):
            db_session.add(ExecutionEvent(
                id=f"{session_id}-{seq}", tenant_id=settings.runtime_tenant_id,
                workspace_id="w", project_id="p", session_id=session_id,
                run_id=run.id, sequence=arrival, occurred_at=datetime(2026, 9, 1, 2, 6, arrival, tzinfo=UTC),
                origin="raw_transcript", kind="run.paused", visibility="summary",
                payload=payload, adapter_version="test",
            ))
        await db_session.flush()

        pending = await _pending_approval(db_session, run=run)
        assert pending is not None, order
        # 到达次序不改变答案：五个选项、上下文、提问节点，一个都不许丢。
        assert len(pending["options"]) == 5, order
        assert "综合前沿综述" in pending["context"], order
        assert pending["askingNodeType"] == "_orchestrator", order
        assert pending["pauseKind"] == "structured_question", order
        # 「这条事件自己没细节」是**那条事件**的处境，不是这次呈递的处境。
        # 归并之后还带着它就是张冠李戴：合起来的这份恰恰是有细节的。
        assert "detailsUnavailable" not in pending, order


@pytest.mark.asyncio
async def test_a_superseded_offer_never_wins_on_size(db_session) -> None:
    """一次呈递被**取代**之后，旧的那次不许因为"字段多"赢回来。

    上游明确支持重呈递：`execution_ingest` 的注释写着「同一个 producing run 的
    package 可以被呈递多次（每次一个新 decision_id）；后一次呈递取代前一次」。
    而在**没有 resume 事件**的情况下（重呈递不发 resume），两次呈递的事件会同时
    落在同一个未答复窗口里。

    旧判据 `max(key=len(payload))` 对"呈递身份"一无所知：它只会挑长的那条 ——
    于是人拿到的是**上一次**的问题和上一次的 choice id，点下去撞不上当前合法集，
    服务端回 `choice_not_offered`，卡片原样重现。这正是 2026-08-19 那次"点三次
    零反馈"的形状。

    新判据先按 offer 身份圈出"最新那一次"，再在其中归并。字段多少不再是判据。
    """
    from app.config import settings
    from app.models.execution import ExecutionEvent, Run
    from app.services.sessions import _pending_approval

    run = Run(
        id="run-supersede", tenant_id=settings.runtime_tenant_id,
        workspace_id="w", project_id="p", session_id="s-supersede",
        status=RunStatus.WAITING_HUMAN.value,
    )
    db_session.add(run)
    events = [
        # 先到：旧的那一次呈递，字段给足（还带着 context / facts / 一堆选项）。
        (1, {
            "reason": "waiting_human",
            "prompt": "旧问题：要不要重跑 reviewer？",
            "context": "上一版的完整决策包正文",
            "options": ["retry_reviewer", "revise", "abort"],
            "pauseKind": "decision_package",
            "askingNodeType": "_reviewer",
            "resumable": True,
            "offer": {"offer_id": "offer-OLD", "decision_id": "d-old"},
        }),
        # 后到：新的那一次呈递取代它，字段更少。
        (2, {
            "reason": "waiting_human",
            "prompt": "新问题：这一版直接定稿还是再改一轮？",
            "options": ["定稿", "再改一轮"],
            "offer": {"offer_id": "offer-NEW", "decision_id": "d-new"},
        }),
    ]
    for seq, payload in events:
        db_session.add(ExecutionEvent(
            id=f"supersede-{seq}", tenant_id=settings.runtime_tenant_id,
            workspace_id="w", project_id="p", session_id="s-supersede",
            run_id=run.id, sequence=seq,
            occurred_at=datetime(2026, 9, 1, 3, 0, seq, tzinfo=UTC),
            origin="raw_transcript", kind="run.paused", visibility="summary",
            payload=payload, adapter_version="test",
        ))
    await db_session.flush()

    pending = await _pending_approval(db_session, run=run)
    assert pending is not None
    assert pending["offer"]["offer_id"] == "offer-NEW", (
        "呈上去的是已经被取代的那一次 —— 人点了会撞不上当前合法动作集"
    )
    assert pending["prompt"].startswith("新问题"), pending["prompt"]
    assert pending["options"] == ["定稿", "再改一轮"], pending["options"]
    # 旧那次的正文也不许漏进来：它说的是另一个决定点。
    assert not pending["context"], pending["context"]
