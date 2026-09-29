"""Pytest 全局配置 —— 自动隔离 HARNESS_FRAMEWORK_HOME。

为啥要这个：
  - 没有它，pytest 会写到 ~/.harness-framework/（开发者本机真 KB），污染数据
  - 不同 test 之间互相看到对方的 concepts / claims → 测试结果不稳
  - CI 跑 pytest 跟开发者本地行为不一致

机制：autouse fixture，所有 test 自动用 tmp 目录当 HOME；test 结束 pytest 自动清。
要 opt-out（极少数 migration 测试想打真 home）：在 test 里 `monkeypatch.delenv("HARNESS_FRAMEWORK_HOME")`。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# 让 `import core.*` / `import shared.*` 工作
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolate_harness_home(monkeypatch, tmp_path):
    """所有 test 默认隔离 HARNESS_FRAMEWORK_HOME / ORG_HOME。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path))
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(tmp_path / "org"))
    # 默认关 LLM cache（test 不依赖 cache 行为，除非显式设）
    monkeypatch.delenv("HARNESS_LLM_CACHE", raising=False)
    # 默认禁用 semantic dedup（避免每 test 触发 sentence-transformers 模型加载，
    # 跑 KB v3 写测试从分钟级变秒级）。要测 dedup 的 test 显式
    # `monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "0")`
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")
    # 默认关流式（生产 LLM_STREAM 默认开）：存量 LLM 测试 mock 的是非流式
    # _post_with_retry / httpx post 路径。测流式的 test 显式 setenv("LLM_STREAM","1")。
    monkeypatch.setenv("LLM_STREAM", "0")
    # 高危闸的三个投影是**进程级全局**，必须逐条测试归零（2026-08-19）。
    #
    # `core.session_driver.apply_continuous_mode` 按 state 施加它们，注释里写得
    # 很清楚："它和上面两个开关是同一件事的三个投影"：
    #
    #     dangerous_commands.PREAUTHORIZED_CATEGORIES   预授权了哪些高危类别
    #     dangerous_commands.BYPASS_ENABLED             连续档绕行高危确认
    #     pause_driver.AUTO_APPROVE_ENABLED/_COUNTDOWN  倒计时自动放行
    #
    # 作用域是"这一趟"，由平台在换届时收 —— 但**测试进程里没有换届**。于是一条
    # 开了连续档的用例跑完，这三个全留在进程里，同进程后面每一条高危审批测试的
    # 暂停就全部消失：本该停下问人的操作直接跑成功，测试红在"没停"上。
    #
    # 实测（探针打在 teardown 上，不是猜的）：
    #     after test_omitting_the_field_keeps_the_current_scope:
    #         BYPASS False→True, AUTO_APPROVE False→True, COUNTDOWN 5→1
    # 于是 `test_authorization_reaches_every_op.py` 跑在
    # `test_highrisk_approval_retry_contract.py` 前面 → 后者 13 条红 7 条。
    # 串行时按字母序恰好没撞上；xdist 一分派就现形。
    #
    # 和上面 HARNESS_FRAMEWORK_HOME 同一条理由：进程级全局在测试之间必须归零。
    # 要测这三个的用例自己设 —— 它们本来就是这么写的。
    # 同一条理由的第四条（2026-08-21）：**pause 注册表也是进程级全局**。
    #
    # `core.pause._PAUSED_RUNS / _ACTIVE_RUNS / _DRIVEN` 是模块级 dict —— 不在
    # 磁盘上，所以上面那道 HARNESS_FRAMEWORK_HOME 隔离对它一点用都没有。
    # `clear_all()` 一直存在（docstring 就写着"主要给测试用"），但靠**各个测试
    # 文件自己记得调** —— 那是一张手写名单，谁注册了 pause 忘了清就漏给下一个。
    #
    # 代价是 CI 整片挂死，不是几条红（2026-08-21 在 runner 上 py-spy 抓到的现场）：
    #
    #   1. 残留 pause 让 `_wait_for_child_progress` 醒在 `pause_pending` 而不是
    #      本该的理由 → `tests/test_child_wait.py` 一次红 5 条（CI 日志里那 5 个
    #      F 逐条对得上，报错逐字是 `- user_stopped / + pause_pending`）；
    #   2. 更贵的是 `session_driver.next_action` 看到**没人认领**的 pause 会去做
    #      孤儿解析 → `pause_driver.drive_pause_chain` → `agent_loop.resume_loop`
    #      → 一个单元测试里跑起了**真的 agent loop**，栈是：
    #
    #         estimate_tokens (core/summarizer.py:83)
    #         should_compress (core/summarizer.py:278)
    #         _run_loop_body (core/agent_loop.py:433)
    #         ...
    #         test_replays_e2e4_busywait_no_longer_aborts (test_child_wait.py:273)
    #
    #      `active+gil` —— 在烧 CPU，不是在等。于是 pytest 永不退出，CI 那一步
    #      把 job 的时间预算烧光（实测 12m37s / 16m29s），容器还因此拆不掉
    #      （"volume is in use"）。
    #
    # 为什么只在 xdist 下现形：`--dist loadfile` 一个 worker 顺序跑很多**文件**
    # 但共用一个**进程**，泄漏因此跨文件传染；单独跑那个文件 20 次全绿。
    from core import pause as _pause
    from core import pause_driver as _pd
    from shared.lib import dangerous_commands as _dc

    def _close_the_gates():
        _dc.set_preauthorized_categories(None)
        _dc.set_bypass_mode(False)
        _pd.set_auto_approve(False, 5)
        _pause.clear_all()

    _close_the_gates()
    yield tmp_path
    _close_the_gates()


@pytest.fixture
def mem_worktree(tmp_path):
    """一个绑好的 Project worktree —— 记忆层的测试都要它。

    没有 worktree 就没有项目记忆（唯一路径），所以这些测试**必须绑真
    worktree**。上一代的断点全部藏在"测试跑在不绑 worktree 的分支上"里：
    平台模式静默失效，而 CI 全绿。
    """
    import subprocess

    wt = tmp_path / "wt"
    wt.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=wt, check=True)
    (wt / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=wt, check=True)
    return wt
