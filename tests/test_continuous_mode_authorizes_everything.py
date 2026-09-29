"""「连续」档：预授权全部高危类别，一路跑到底。

## 为什么需要这一档

无人值守的承诺，在最需要它的那个节点上会失效：experiment 提交真实作业时
命中「真实外部作业提交」，于是停下来等一个不在的人。2026-08-13 一轮 E2E
因此静默停了三小时 —— 我开了自主模式，但没声明任何授权类别，而 UI 上根本
没有声明它的入口。开关自称"自己往下推"，实际走到第一个作业提交就不动了。

## 为什么是通配符而不是"把已知类别都列上"

类别集合不封闭：节点可以定义自己的（`resource_manager` 的「真实外部作业
提交」就不在本模块的表里）。写成名单，任何人新加一个类别，所有声称"全部
授权"的会话都会在那儿停下问一个不在的人 —— 而且不报错。

## 关于下面那个 fixture

授权范围是**模块级全局**。不还原的话，这个文件里一句
`set_preauthorized_categories(["*"])` 会让同一进程后面所有测试都处于"全部
预授权"状态 —— 实测污染了 `test_highrisk_approval_retry_contract` 的 4 条，
而且它们单跑全绿、全量才红。这正是全局可变状态最难查的那种症状。
"""
import pytest

from shared.lib import dangerous_commands as dc


@pytest.fixture(autouse=True)
def _restore_authorization_scope():
    """出去时恢复原样 —— 谁改全局谁负责还。"""
    saved = set(dc.PREAUTHORIZED_CATEGORIES)
    try:
        yield
    finally:
        dc.set_preauthorized_categories(saved)


def test_wildcard_authorizes_categories_nobody_has_invented_yet():
    """通配符必须覆盖**未来**的类别 —— 这正是不写名单的理由。"""
    dc.set_preauthorized_categories(["*"])
    assert dc.preauthorized("真实外部作业提交")
    assert dc.preauthorized("某个明年才会有的类别")
    for known in dc.known_risk_categories():
        assert dc.preauthorized(known), known

def test_wildcard_is_not_reported_as_a_typo():
    """`*` 被当成拼错的话，用户每次开连续模式都会看到一句假警告。"""
    assert dc.unrecognized_categories(["*"]) == []
    assert dc.unrecognized_categories(["*", "拼错的"]) == ["拼错的"]

def test_default_still_stops_at_everything():
    """默认必须仍是「都停」—— 授权只能由人显式给出。"""
    dc.set_preauthorized_categories(None)
    assert not dc.preauthorized("真实外部作业提交")
    dc.set_preauthorized_categories([])
    assert not dc.preauthorized("真实外部作业提交")

def test_a_narrow_grant_stays_narrow():
    """给一类不等于给全部 —— 通配符不能让粒度档失效。"""
    dc.set_preauthorized_categories(["真实外部作业提交"])
    assert dc.preauthorized("真实外部作业提交")
    assert not dc.preauthorized("提权: sudo")
    assert not dc.preauthorized("*"), "字面量 `*` 本身不该是个可命中的类别名"
