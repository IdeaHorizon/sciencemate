"""autonomous 是"只在高风险处停"，不是"什么都不问"。

平台 UI 上的 Autonomous 此前只影响"跑完自动 publish 版本"；harness 的
AUTO_APPROVE_ENABLED 只有 CLI(chat.py) 设过，平台没有任何通往它的路径 ——
于是从 UI 出发不管选哪种模式，每个决策点都停人（E2E v16 实测）。

接线之前必须先立这条边界：experiment 的批量写入 HITL、外部作业提交门都走
highrisk_confirm / permission 类型，把它们一起自动放行就等于把安全门拆了。
"""

import pytest

from core import pause_driver
from shared.lib import dangerous_commands as dc


@pytest.fixture(autouse=True)
def _not_continuous():
    """这个文件锁的是 **autonomous** 档的边界。

    「连续」档（= autonomous + 预授权全部类别 `["*"]`）是另一档，边界不同 ——
    见文件末尾那两条。默认把绕行关掉，免得别的用例留下的全局状态让这里
    测的是另一档（2026-08-19 实测栽过一次）。
    """
    before = dc.BYPASS_ENABLED
    dc.set_bypass_mode(False)
    yield
    dc.set_bypass_mode(before)


class _Pause:
    def __init__(self, pause_type=None, options=None):
        self.metadata = {"type": pause_type} if pause_type else {}
        self.options = options or []
        self.question = "q"
        self.context = ""
        self.asking_node_type = "experiment"
        self.asking_run_id = "r1"


def test_high_risk_types_are_never_auto_approved_under_autonomous():
    """autonomous 档：批量写入 HITL / 外部作业提交门照旧停人。"""
    for pause_type in ("highrisk_confirm", "permission"):
        assert pause_driver._is_high_risk(_Pause(pause_type)), pause_type


def test_decision_packages_are_auto_approvable():
    """决策包正是 autonomous 要自动过的东西。"""
    assert not pause_driver._is_high_risk(_Pause("decision_package"))


def test_plain_human_input_is_auto_approvable():
    assert not pause_driver._is_high_risk(_Pause())


def test_boundary_set_is_explicit():
    """边界写成常量，别散在判断里 —— 新增高风险类型时有唯一登记处。"""
    assert pause_driver.NEVER_AUTO_APPROVE_TYPES == frozenset(
        {"highrisk_confirm", "permission"}
    )


def test_env_var_is_the_platform_entry_point(monkeypatch):
    """平台通过 HARNESS_AUTO_APPROVE 开启 —— 这是它唯一能接到的开关。"""
    import importlib

    monkeypatch.setenv("HARNESS_AUTO_APPROVE", "1")
    reloaded = importlib.reload(pause_driver)
    try:
        assert reloaded.AUTO_APPROVE_ENABLED is True
    finally:
        monkeypatch.delenv("HARNESS_AUTO_APPROVE", raising=False)
        importlib.reload(pause_driver)


def test_default_is_off():
    import importlib
    reloaded = importlib.reload(pause_driver)
    assert reloaded.AUTO_APPROVE_ENABLED is False


# ── 连续档是另一条边界（wangd 2026-08-19）──────────────────────────────────


def test_continuous_mode_releases_even_high_risk():
    """「连续模式下就不应该出现任何需要人操作的行为，必须连续进行下去，
    除非彻底结束完整的研究了，才能停止。」

    连续 = autonomous + **预授权全部类别**（UI 写的就是 `["*"]`）。用户已经
    逐类授权过，再停下来问是自相矛盾。放行的钥匙是那份全类别授权本身
    （`BYPASS_ENABLED`），不是新开一个开关。
    """
    dc.set_bypass_mode(True)
    for pause_type in ("highrisk_confirm", "permission"):
        assert not pause_driver._is_high_risk(_Pause(pause_type)), pause_type


def test_partial_authorization_is_not_continuous():
    """**边界必须是"授权了全部"**：只授权了某几类 ≠ 连续，高危照旧停人。

    这条是上一条的反面 —— 少了它，"连续放行高危"会退化成"只要授权过任何
    一类就全放行"，那是把安全门拆了。
    """
    dc.set_preauthorized_categories(["shell_write"])
    dc.set_bypass_mode(False)          # 施加层只在 "*" 时才开绕行
    try:
        for pause_type in ("highrisk_confirm", "permission"):
            assert pause_driver._is_high_risk(_Pause(pause_type)), pause_type
    finally:
        dc.set_preauthorized_categories([])
