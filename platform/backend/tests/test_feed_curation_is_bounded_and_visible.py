"""自动挖掘的四条纪律：有界、可见、不越权、失败不外溢。

挖掘是**后台自动跑、花用户自己的钱**的东西。这三个属性叠在一起意味着：它出
任何差错都很难被当场发现。所以下面每一条锁的都是"出错时会不会被看见"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.models.user import User
from app.services.feed import curation, discovery

REPO_ROOT = Path(__file__).resolve().parents[3]


def _user(**feed_prefs) -> User:
    return User(
        id="aaaa1111-1111-4111-8111-111111111111",
        email="c@lab.test",
        display_name="Curator",
        hashed_password="x",
        preferences={"feed": feed_prefs} if feed_prefs else None,
    )


def test_auto_curation_is_off_until_the_user_turns_it_on() -> None:
    """默认关。它花的是用户自己的模型额度 —— 默认替他花钱不该由我们决定。"""
    assert curation.is_enabled(_user()) is False
    assert curation.is_enabled(_user(domains=["cs.LG"])) is False
    assert curation.is_enabled(_user(auto_curation=True)) is True


def test_explicit_choices_outrank_inferred_ones() -> None:
    """手选的排前面且不会被推断结果挤掉 —— 顺序本身也是一种表达。"""
    user = _user(
        domains=["cond-mat.stat-mech"],
        inferred_domains=["cs.LG", "cond-mat.stat-mech"],
    )
    effective = curation.effective_domains(user)
    assert effective[0] == "cond-mat.stat-mech"
    # 同一个域同时被手选和推断出来，只算一次。
    assert effective == ("cond-mat.stat-mech", "cs.LG")


def test_a_rejected_inference_never_comes_back() -> None:
    """删掉一条推断项要被记住。

    只从列表里移除是不够的：下次挖掘会把它重新推断出来，于是用户每天删同一个，
    而系统看起来像没听见他说话。
    """
    user = _user(
        domains=[],
        inferred_domains=["cs.LG", "q-fin.TR"],
        rejected_domains=["q-fin.TR"],
    )
    assert curation.effective_domains(user) == ("cs.LG",)


def test_turning_it_off_does_not_silently_change_what_you_follow() -> None:
    """关掉开关不清空已推断的方向 —— 他可能只是想省钱，而那些方向仍然是对的。

    （真要清掉，删具体某一条才是他表达"这个推错了"的动作。）
    """
    user = _user(auto_curation=False, inferred_domains=["cs.LG"])
    assert curation.is_enabled(user) is False
    assert curation.effective_domains(user) == ("cs.LG",)


def test_only_material_project_changes_trigger_a_rerun() -> None:
    """按课题文本的指纹判断要不要重算。

    每次请求都重算 = 用户每刷一次页面就烧一次模型调用。
    """
    from app.models.project import Project

    def project(name: str, description: str) -> Project:
        return Project(
            id="p1", owner_id="u1", name=name,
            research_domain="statistical mechanics", description=description,
        )

    first = curation.project_fingerprint([project("Ising", "finite size scaling")])
    same = curation.project_fingerprint([project("Ising", "finite size scaling")])
    changed = curation.project_fingerprint([project("Ising", "critical exponents")])
    assert first == same
    assert first != changed

    user = _user(auto_curation=True, curated_fingerprint=first)
    assert curation.needs_refresh(user, first) is False
    assert curation.needs_refresh(user, changed) is True
    # 开关关着时永远不重算 —— 哪怕课题变了。
    assert curation.needs_refresh(_user(curated_fingerprint=first), changed) is False


@pytest.mark.asyncio
async def test_scheduled_due_round_refreshes_even_when_project_text_is_unchanged(
    monkeypatch,
) -> None:
    """24 小时闸已判定到期后，Project 指纹不能再次把定时检索挡掉。"""
    user = _user(auto_curation=True, curated_fingerprint="unchanged")
    seen: list[bool] = []

    async def fake_due_users(_db):
        return [user]

    async def fake_curate_one(_client, _db, **kwargs):
        seen.append(bool(kwargs.get("force")))
        return {"domains": 0, "found": 0, "digests": 0}

    class ClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(discovery, "due_users", fake_due_users)
    monkeypatch.setattr(discovery, "curate_one", fake_curate_one)
    monkeypatch.setattr(discovery.collectors, "make_client", ClientContext)

    await discovery.run_round(object())

    assert seen == [True]


def test_a_failure_is_remembered_so_the_switch_can_explain_itself() -> None:
    """开着但没工作，必须说得出为什么。

    一个静默失败的开关，和一个"确实没什么可推断的"的开关，从界面上看一模一样。
    """
    user = _user(auto_curation=True)
    curation._remember(user, error="没有配资讯挖掘模型")
    assert curation.stored(user)["curated_error"] == "没有配资讯挖掘模型"

    # 成功时要**清掉**旧错误，否则一条早就修好的错误会一直挂在那儿。
    curation._remember(user, fingerprint="abc", domains=["cs.LG"], queries=["a b"])
    assert "curated_error" not in curation.stored(user)
    assert curation.inferred_domains(user) == ("cs.LG",)


def test_curation_does_not_fall_back_to_the_reasoning_model() -> None:
    """没配挖掘模型时**不回退到主推理模型**。

    回退看起来体贴，实际是拿用户没授权的预算干他没要求的事：他给会话选的模型
    是为了做研究，不是为了后台每天替他挖资讯。
    """
    from app.services.feed import bridge

    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert 'role=FEED_CURATION_ROLE' in source
    assert bridge.FEED_CURATION_ROLE == "feed_curation"
    # 角色目录里必须真有这个角色 —— 只在代码里写一个字符串，
    # 设置页上就永远勾不出它来。
    catalog = (REPO_ROOT / "shared" / "model_roles.yaml").read_text(encoding="utf-8")
    assert "id: feed_curation" in catalog
    assert "required: false" in catalog


def test_the_miner_gets_no_tools_and_leaves_no_research_trace() -> None:
    """挖掘不复用调度器那套：零工具、不建 run、不进 KB、不留执行事件。

    判据落在**那个 op 的实现**上。它一旦开始带工具面，爆炸半径就从"资讯流"
    扩到"别人的课题"，而这件事不会有任何一层报错。
    """
    runtime = (REPO_ROOT / "platform_runtime.py").read_text(encoding="utf-8")
    start = runtime.index("async def run_feed_curate_profile")
    end = runtime.index("def parse_feed_curation")
    body = runtime[start:end]

    assert "LLMClient()" in body
    for forbidden in ("tool", "State.new", "write_kb", "emit_event", "run_dir"):
        assert forbidden not in body, f"挖掘的 op 里出现了 {forbidden!r}"


@pytest.mark.parametrize(
    ("raw", "expected_domains"),
    [
        ('{"domains":["cs.LG"],"queries":["a b"]}', ["cs.LG"]),
        ('```json\n{"domains":["cs.LG"],"queries":[]}\n```', ["cs.LG"]),
        ('好的：{"domains":["cs.LG"],"queries":[]} 供参考', ["cs.LG"]),
        # 编出来的 slug 必须被词表挡掉 —— 它谁也匹配不上，而且不报错。
        ('{"domains":["cs.LG","physics.fake-ph"],"queries":[]}', ["cs.LG"]),
        ("我觉得你该关注机器学习", []),
        ("", []),
    ],
)
def test_the_model_answer_is_filtered_through_the_vocabulary(
    raw: str, expected_domains: list[str]
) -> None:
    """模型的回答要过词表，且围栏/解释文字要机械剥掉。

    不起模型就能验 —— 真正容易出错的是这些边界，不是模型调用本身。
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from platform_runtime import parse_feed_curation

    result = parse_feed_curation(raw, ["cs.LG", "cond-mat.stat-mech"])
    assert result["domains"] == expected_domains


def test_each_project_keywords_are_capped_at_two() -> None:
    """每个课题的检索词最多 2 个，多出来的机械丢弃 —— 词要属于「这个」课题，
    一个课题不该吐五六个词挤掉别人。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from platform_runtime import parse_feed_curation

    result = parse_feed_curation(
        '{"domains":["cs.LG"],"projects":['
        '{"queries":["steer-by-wire chassis control","vehicle dynamics modeling",'
        '"new energy vehicle","fourth"],"news_queries":[]}]}',
        ["cs.LG"],
    )
    assert result["projects"][0]["queries"] == [
        "steer-by-wire chassis control",
        "vehicle dynamics modeling",
    ]  # 第 3、4 个被机械丢弃


def test_project_keywords_do_not_depend_on_domain_slots() -> None:
    """关键词脱域：queries 不要求落在候选分类里，所以即便 domains 为空
    （如车辆工程不在 arXiv 分类里），关键词照样能产出。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from platform_runtime import parse_feed_curation

    result = parse_feed_curation(
        '{"domains":[],"projects":[{"queries":["steer-by-wire chassis control"],"news_queries":[]}]}',
        ["cs.LG"],  # 车辆工程不在里面，但不影响 queries
    )
    assert result["domains"] == []
    assert result["projects"][0]["queries"] == ["steer-by-wire chassis control"]


def test_news_queries_are_parsed_separately_from_paper_queries() -> None:
    """资讯检索词（news_queries）与论文检索词（queries）在**每个课题内**分开，
    各自最多 2 个，且不依赖 domains —— 网页搜索靠它找新闻/政策/产业动态。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from platform_runtime import parse_feed_curation

    result = parse_feed_curation(
        '{"domains":["cs.LG"],"projects":['
        '{"queries":["steer-by-wire chassis control"],'
        '"news_queries":["线控底盘 国标","自动驾驶 准入 政策","智能网联汽车 产业动态"]}]}',
        ["cs.LG"],
    )
    proj = result["projects"][0]
    assert result["domains"] == ["cs.LG"]
    assert proj["queries"] == ["steer-by-wire chassis control"]
    assert proj["news_queries"] == ["线控底盘 国标", "自动驾驶 准入 政策"]  # 第 3 个被丢


def test_projects_array_matches_input_order_and_count() -> None:
    """每个课题一组词、顺序一一对应 —— 这是合并层 floor 对齐的前提。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from platform_runtime import parse_feed_curation

    result = parse_feed_curation(
        '{"domains":[],"projects":['
        '{"queries":["a1"],"news_queries":[]},'
        '{"queries":["b1","b2"],"news_queries":["c1"]}]}',
        ["cs.LG"],
    )
    assert [p["queries"] for p in result["projects"]] == [["a1"], ["b1", "b2"]]
    assert result["projects"][1]["news_queries"] == ["c1"]


def test_missing_news_queries_returns_empty_list() -> None:
    """老模型的回答没有 news_queries 字段时，返回空数组而不是崩。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from platform_runtime import parse_feed_curation

    result = parse_feed_curation(
        '{"domains":[],"projects":[{"queries":["x"]}]}',
        ["cs.LG"],
    )
    assert result["projects"][0]["news_queries"] == []


def test_legacy_flat_queries_still_parse_as_one_project() -> None:
    """老模型仍只返回顶层 queries/news_queries（没有 projects）时，当成单课题。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from platform_runtime import parse_feed_curation

    result = parse_feed_curation(
        '{"domains":[],"queries":["a b","c d"],"news_queries":["e f"]}',
        ["cs.LG"],
    )
    assert result["projects"] == [
        {"queries": ["a b", "c d"], "news_queries": ["e f"]}
    ]


def test_merge_gives_each_project_at_least_one_word() -> None:
    """round-robin 合并：每个课题至少 1 词（floor），有富余再补齐。"""
    from datetime import datetime, timezone

    per_project = [
        {"queries": ["a1", "a2"], "news_queries": []},
        {"queries": ["b1"], "news_queries": []},
        {"queries": ["c1", "c2"], "news_queries": []},
    ]
    now = datetime.now(timezone.utc)
    merged = curation._merge_project_queries(
        per_project, [now, now, now], field="queries", cap=5
    )
    # 第一轮 floor：a1、b1、c1；第二轮补齐：a2、c2（b 只有 1 词）。
    assert merged == ["a1", "b1", "c1", "a2", "c2"]


def test_merge_orders_by_activity_when_capped() -> None:
    """词槽不够时，活跃的课题优先补齐 —— floor 保底，活跃度定谁拿第 2 个词。"""
    from datetime import datetime, timedelta, timezone

    per_project = [
        {"queries": ["dormant1", "dormant2"], "news_queries": []},
        {"queries": ["active1", "active2"], "news_queries": []},
    ]
    now = datetime.now(timezone.utc)
    last_active = [now - timedelta(days=30), now]  # 第二个更活跃
    merged = curation._merge_project_queries(
        per_project, last_active, field="queries", cap=3
    )
    # floor 轮按活跃度先取 active1、再 dormant1；补齐轮 active2（撞 cap 停）。
    assert merged == ["active1", "dormant1", "active2"]


@pytest.mark.asyncio
async def test_only_active_projects_drive_keyword_mining(db_session) -> None:
    """归档/完成/暂停的课题不参与关键词推断 —— 生命周期得接进这条管线。

    否则一个已经放下的旧课题会一直压着新课题：用户改了订阅方向，资讯流里
    却还冒出旧课题的内容（「选了宗教方向还看到 ISO 26262」的根源）。
    """
    from app.models.project import Project, ProjectStatus
    from uuid import uuid4

    uid = str(uuid4())
    db_session.add_all(
        [
            Project(id=str(uuid4()), owner_id=uid, name="active", status=ProjectStatus.ACTIVE),
            Project(id=str(uuid4()), owner_id=uid, name="archived", status=ProjectStatus.ARCHIVED),
        ]
    )
    await db_session.flush()

    user = User(id=uid, email="active@lab.test", display_name="A", hashed_password="x")
    projects = await curation.user_projects(db_session, user)
    assert {p.name for p in projects} == {"active"}


@pytest.mark.asyncio
async def test_project_keywords_lean_on_most_recent_inputs(db_session) -> None:
    """关键词提炼读该课题最近几条用户原始输入，而不是最早几条。

    一个做久了的课题，当下的诉求只在最近的消息里；拿「最早的 40 条」去提炼，
    提炼的是过时的意图。
    """
    from datetime import datetime, timedelta, timezone

    from app.models.execution import SessionMessage, SessionProjection

    pid, sid = "p-recent", "s-recent"
    db_session.add(
        SessionProjection(
            tenant_id="t", workspace_id="w", project_id=pid, session_id=sid,
            title="s", lifecycle_status="active",
        )
    )
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    contents = ["最早的想法", "中间的调整", "最新的方向：转向宗教研究"]
    for index, content in enumerate(contents):
        db_session.add(
            SessionMessage(
                session_id=sid, sequence=index + 1, role="user", content=content,
                created_at=now + timedelta(minutes=index),
            )
        )
    await db_session.flush()

    got = await curation.project_user_inputs(db_session, pid, limit=2)
    # 取最近 2 条，翻正成时间先后（最旧在前）。
    assert got == ["中间的调整", "最新的方向：转向宗教研究"]


@pytest.mark.asyncio
async def test_projects_last_active_uses_most_recent_user_message(db_session) -> None:
    """活跃度取该课题下最近一条 user 消息的时间：跨 session、忽略 assistant，
    从没说过话的课题是 None（合并时沉底）。"""
    from datetime import datetime, timedelta, timezone

    from app.models.execution import SessionMessage, SessionProjection

    pid_a, pid_b = "p-a", "p-b"
    db_session.add_all(
        [
            SessionProjection(tenant_id="t", workspace_id="w", project_id=pid_a, session_id="s-a", title="a"),
            SessionProjection(tenant_id="t", workspace_id="w", project_id=pid_b, session_id="s-b", title="b"),
        ]
    )
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    db_session.add_all(
        [
            SessionMessage(session_id="s-a", sequence=1, role="user", content="旧", created_at=base),
            SessionMessage(session_id="s-a", sequence=2, role="assistant", content="AI", created_at=base + timedelta(days=5)),
            SessionMessage(session_id="s-a", sequence=3, role="user", content="新", created_at=base + timedelta(days=10)),
        ]
    )
    await db_session.flush()

    got = await curation.projects_last_active(db_session, [pid_a, pid_b])
    assert got[pid_a] == base + timedelta(days=10)  # 取最近 user 消息，忽略 assistant
    assert got[pid_b] is None
