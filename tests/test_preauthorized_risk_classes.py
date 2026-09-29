"""出发前声明授权范围 —— "无人值守"才可能真的无人值守。

## 现场（2026-08-10）

一轮无人值守 E2E 停在「真实外部作业提交」的高危审批上，**静默挂了两小时**。
而提交真实作业**正是**这一趟要干的事。

当时只有两个极端：

    bypass_dangerous=True    连 `dd of=/dev/`、`sudo`、`mkfs` 一起放行
                             —— 扩大授权，不能顺手搭车
    bypass_dangerous=False   每个高危点都停 —— 无人值守时没有人会来答

于是"无人值守"这个承诺，在**最需要它的那个节点**上必然失效。缺的不是开关，
是粒度：出发前说清楚这一趟授权什么。

## 为什么检查放在 build_pause_payload 里

它是每一次高危确认的必经漏斗，而且已经拿到 category。放到调用方就是三处
（experiment ×2、safe_bash ×1），分属两个 owner 的节点目录 —— 写成名单式检查
等于"新增一个高危工具默认漏过预授权"，还越界改了别人的节点。

## 为什么走"发通行证 + 重调一次"而不是直接执行

和人批准**完全同一条路**。另开一条"框架替你执行"的路径，就是 E2E v19 里模型
误以为"批准 = 已执行"、然后去查一个不存在的结果那个坑 —— 多一条路就多一种误解。
"""
from __future__ import annotations

import pytest

from shared.lib import dangerous_commands as dc


class _FakeState:
    def __init__(self) -> None:
        self.hook_state: dict = {}
        self.node_type = "experiment"
        self.run_id = "run_x"
        self.transcript: list = []

    def append_transcript(self, event: str, **fields) -> None:
        self.transcript.append((event, fields))


@pytest.fixture(autouse=True)
def _clean_policy():
    before = set(dc.PREAUTHORIZED_CATEGORIES)
    dc.set_preauthorized_categories(None)
    yield
    dc.set_preauthorized_categories(before)


def _build(state, category: str) -> dict:
    return dc.build_pause_payload(
        state, tool="submit_job", text="lmp_serial -in in.ka_lj",
        category=category, preview="scheduler=local\ncommand=lmp_serial …",
    )


def test_default_still_stops_and_asks() -> None:
    """默认必须是"每次都问"。授权只能由发起的人显式给出。"""
    state = _FakeState()
    assert _build(state, "真实外部作业提交")["status"] == "pause"
    assert not dc.is_confirmed(state, "lmp_serial -in in.ka_lj")


def test_declared_category_gets_a_pass_without_asking() -> None:
    state = _FakeState()
    dc.set_preauthorized_categories(["真实外部作业提交"])
    result = _build(state, "真实外部作业提交")

    assert result["status"] == "preauthorized"
    # 走的是同一张一次性通行证，绑定逐字命令。
    assert dc.is_confirmed(state, "lmp_serial -in in.ka_lj")
    assert any(e == "highrisk_preauthorized" for e, _ in state.transcript), (
        "预授权必须留痕 —— 没有惊动人不等于没有发生"
    )


def test_undeclared_category_still_stops() -> None:
    """授权是**按类别**给的，不是给整个 run 开绿灯。"""
    state = _FakeState()
    dc.set_preauthorized_categories(["真实外部作业提交"])
    assert _build(state, "块设备写入 (dd of=)")["status"] == "pause"
    assert not dc.is_confirmed(state, "lmp_serial -in in.ka_lj")


def test_pass_is_bound_to_the_verbatim_command() -> None:
    """改参数作废通行证 —— 批准的是那一条具体命令，不是那个工具。"""
    state = _FakeState()
    dc.set_preauthorized_categories(["真实外部作业提交"])
    _build(state, "真实外部作业提交")
    assert not dc.is_confirmed(state, "lmp_serial -in in.ka_lj -var T 2.0")


def test_preauthorized_result_tells_the_model_to_recall() -> None:
    """契约必须送到调用方：不说清楚，模型会以为框架替它跑了。"""
    state = _FakeState()
    dc.set_preauthorized_categories(["真实外部作业提交"])
    contract = _build(state, "真实外部作业提交")["approval_contract"]
    assert "不会" in contract and "重新调用" in contract
    assert "submit_job" in contract


# 这里原来有三条**源码检视**测试（`inspect.getsource` + 字符串匹配）：
# 授权挂没挂在会话上、run_unattended 里有没有那句声明、报错串里有没有
# "match_high_risk"。三条都是"复刻判据给自己打分"：保持行为的重构会打红
# 它们，而写法相似、语义损坏的改动照样绿。
#
# 已换成真跑 `serve_jsonl` 的行为测试，见
# `tests/test_authorization_reaches_every_op.py` —— 那里真发 JSONL 请求、
# 真让模型调一条 sudo，看它到底停不停。
