"""调度器路由前必须看得见"研究走到哪了"，不只是"流程走到哪了"。

## 现场（2026-08-18，英国饮食）

调度器把一份 S0-S17 全是文献裁决的 research_plan 派给了**计算实验**节点。而
"literature / data 只有 README、零证据产物"这个致命事实，是 experiment 进场
之后自己翻盘发现的 —— 它当时的原话：

    KB 里已有完整的证据基础：9 条 claim……**literature 和 data 节点只有
    README，没有实际史料/数据产物。**

那本该是**派发前**就该知道的事：证据只有 7 件、一手史料 0 件，此时派裁决必然
得到 inconclusive。整趟 1353 万 tokens，63% 烧在格式与仪式上。

统筹者看不见局面，就只能按默认剧本办事 —— "官僚"是机械成因，不是模型不聪明。

## 这组测试守什么

守**事实的计算**，不守注入文案（那会随措辞变红得没有意义）。三条事实各自
对应一次真实的误判：目标缺席 → 对着流程而不是对着目标路由；承诺账的条目
类型缺席 → 范畴错误派发；证据库存缺席 → 派发撞空。
"""
from __future__ import annotations

from core.prereg_commitments import ClosureTally
from core.research_situation import (
    EvidenceInventory,
    ResearchSituation,
    _evidence_inventory,
    render_situation_facts,
)


class _FakeState:
    """只实现被盘点用到的那点接口。"""

    def __init__(self, artifacts=None, project_id=None):
        self._artifacts = artifacts or []
        self.project_id = project_id
        self.project_root = None

    def list_artifacts(self, artifact_type=None):
        if artifact_type is None:
            return self._artifacts
        return [a for a in self._artifacts if a.get("type") == artifact_type]


class TestEvidenceInventory:
    def test_authoritative_records_and_supporting_material_are_counted_apart(self):
        """权威记录与支撑材料必须分开数。

        `clean_results` 能勾账，但没有裁决理由和可信度段落。把它算进"研究执行
        记录"会让"有一张数字表"看起来像"做过一次研究"。
        """
        state = _FakeState([
            {"type": "experiment_log"},
            {"type": "experiment_log"},
            {"type": "clean_results"},
            {"type": "research_plan"},      # 既非记录也不勾账
        ])
        inventory = _evidence_inventory(state)
        assert inventory.records == {"experiment_log": 2}
        assert inventory.supporting == 1
        assert inventory.record_total == 2
        assert inventory.is_empty is False

    def test_the_british_food_situation_reads_as_empty(self):
        """英国饮食派发前的真实盘面：零执行记录。

        这正是当时该让调度器看见、却看不见的那个事实。
        """
        state = _FakeState([{"type": "research_plan"}, {"type": "pre_registration"}])
        assert _evidence_inventory(state).is_empty is True

    def test_inventory_survives_a_broken_artifact_store(self):
        """盘点失败不许掀翻整轮注入，而且要往"零证据"的方向失败。

        往"证据充足"方向兜底会把一次读盘故障变成一次错误派发的依据。
        """
        class _Broken(_FakeState):
            def list_artifacts(self, artifact_type=None):
                raise RuntimeError("store unavailable")

        assert _evidence_inventory(_Broken()).is_empty is True

    def test_unknown_artifact_types_are_not_counted_as_evidence(self):
        """没在类型注册表里声明的东西不算证据 —— fail-closed。"""
        state = _FakeState([{"type": "some_new_log"}, {"type": ""}])
        inventory = _evidence_inventory(state)
        assert inventory.records == {}
        assert inventory.supporting == 0


class TestClosureProgressShape:
    def test_all_open_are_statements_is_the_category_signal(self):
        """"还欠的账全是陈述条"是派发前范畴检查的机械信号。

        英国饮食的闭合条件几乎全是陈述条（"识别出书写主体"、"检验声誉与事实
        脱节"），而它被派给了计算实验节点。
        """
        statements_only = ClosureTally(
            total=10, fulfilled=4, open_numeric=0, open_statement=6)
        assert statements_only.all_open_are_statements is True

        mixed = ClosureTally(total=10, fulfilled=4, open_numeric=2, open_statement=4)
        assert mixed.all_open_are_statements is False

    def test_a_fully_closed_plan_raises_no_category_signal(self):
        """全兑现时不该发出"别派实验"的信号 —— 那时该走的是收尾，不是路由。"""
        done = ClosureTally(total=8, fulfilled=8, open_numeric=0, open_statement=0)
        assert done.all_open_are_statements is False
        assert done.open_total == 0


class TestTheGoalIsReadFromTheRealIntakeContract:
    """目标锚的字段名以 `core.research_intake` 写入的那份为准。

    2026-08-19 真跑一次抓到的：这里最初按**猜的**字段名写成 `text`，而真实记录
    是 `original_text` + `amendments` 修订链 —— 于是目标行永远是空的、注入里根本
    不出现。单测当时全绿，因为我自己造的假数据也叫 `text`。

    「契约必须送到调用方」的反面：自己猜一个字段名，再用同样猜出来的假数据去验，
    绿得毫无意义。所以这组测试**用 research_intake 自己写的记录**。
    """

    def _project_with_intake(self, tmp_path, text, *, amendments=()):
        from core.research_intake import record_intake

        record_intake(tmp_path, text)
        for extra in amendments:
            record_intake(tmp_path, extra)
        return tmp_path

    def test_the_goal_comes_from_a_record_written_by_research_intake(self, tmp_path):
        from core.research_situation import compute_situation

        root = self._project_with_intake(tmp_path, "研究英国饮食文化的声誉形成")

        class _State:
            project_root = root
            project_id = None

            def list_artifacts(self, artifact_type=None):
                return []

        assert compute_situation(_State()).goal == "研究英国饮食文化的声誉形成"

    def test_a_later_amendment_wins_over_the_original(self, tmp_path):
        """用户后来说的话才是当前目标。

        拿原始那句去路由，等于对着过期的意图做决策 —— 而修订恰恰发生在用户
        改主意的时候。
        """
        from core.research_situation import compute_situation

        root = self._project_with_intake(
            tmp_path, "先研究英国饮食", amendments=("改成只看营养品质这一段",)
        )

        class _State:
            project_root = root
            project_id = None

            def list_artifacts(self, artifact_type=None):
                return []

        assert compute_situation(_State()).goal == "改成只看营养品质这一段"


class TestRenderingStaysCheap:
    """注入必须只有计数和单行摘要 —— 调度器 800KB 衰减是实测硬墙。"""

    def _situation(self, **kw):
        base = dict(
            goal=None,
            closure=None,
            evidence=EvidenceInventory(records={}, supporting=0, anchored_claims=0),
            research_state_path=None,
            research_state_verdict=None,
        )
        base.update(kw)
        return ResearchSituation(**base)

    def test_a_long_goal_is_truncated(self):
        """用户第一条指令可能是几百字的一整段 —— 目标行不许原样铺进去。"""
        lines = render_situation_facts(self._situation(goal="研究" * 300))
        goal_line = next(line for line in lines if "🎯" in line)
        assert len(goal_line) < 200
        assert goal_line.endswith("…")

    def test_empty_evidence_is_stated_explicitly_not_omitted(self):
        """零证据必须**说出来**。

        省略它会让"没查到"和"没有"看起来一样 —— 而这两件事对派发决策的含义
        完全相反。
        """
        lines = render_situation_facts(self._situation())
        assert any("零份" in line for line in lines)

    def test_nothing_is_rendered_without_facts_to_report(self):
        """没有目标、没有承诺账时不产出占位噪音，只报证据面。"""
        lines = render_situation_facts(self._situation())
        assert not any("🎯" in line for line in lines)
        assert not any("📊" in line for line in lines)
