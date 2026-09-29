"""2026-08-19 stale 锁死的两个根因 —— 人点 27 次、界面毫无变化、25 条一样的 ERROR。

那天的事故有两个症状。前一半（点 PROCEED 卡片重现）已在 harness 侧修掉。这里是
**后一半**：13:47 之后点了完全没反应。

它由两个互相独立的根因叠成：

  C  归属判据用 turn 级变量回答 run 级问题
     `owningTranscript = not states`，而 `states` 是 execute_local_turn 的局部
     变量，每 turn 清零 —— 它实际回答的是"本轮第一个出声的文件"。第一轮两者
     恰好相等；**续跑落进子节点时不相等**，于是子节点的 run_end 关掉了父 run
     的 attempt，两条生命周期永久分叉，之后所有人工答复被判 stale。

  D  自愈写被自己的异常回滚
     检测到矛盾时把 run 落成 stale_unknown 好让 UI 显示"可恢复"，但
     `db.flush()` 之后紧接着 `raise` —— 异常抛回调用方的 `async with`，
     session 关闭即 rollback，**这个写从来没落过盘**。于是 run 永远停在
     waiting_human，UI 永远渲染那张可应答的卡片，点多少次都一样。
"""
from __future__ import annotations

import inspect

from app.services import local_execution as le


def test_ownership_is_decided_per_run_not_per_turn():
    """判据必须问「这个 **run** 有没有别的 transcript」，不是「本轮第一个」。

    ⚠️ 这里只守作用域。判据**问的是哪张表**曾经也写在这里（断言
    `"TranscriptIngestCheckpoint" in src`）—— 那张表全库 0 行，于是这条断言
    保住的是"继续查那本空账"，判据恒真而测试全绿。断言要落在能翻转的东西上，
    行为面由 test_execution_foundation 那两个用例在真实摄取路上验。
    """
    src = inspect.getsource(le._owning_transcript)
    assert "context.run_id" in src, "作用域必须是 run，不是 turn"

    # 老判据不许回来：`not states` 就是那个 turn 级答案。
    body = inspect.getsource(le._harness_adapter_state)
    assert '"owningTranscript": not states' not in body, (
        "归属又回到 turn 级局部变量了 —— 续跑一次就会重演 attempt 被子节点关掉"
    )


def test_there_is_only_one_ingestion_path():
    """事实只有一条到达路径 —— 没有可分叉的第二份实现。

    从前这里比对实时与补录两份实现的源码，防它们分叉。真正的下场比分叉更糟：
    补录那份（`ingest_transcript_file`）**零生产调用方**，两道闸的修复都落在
    它身上，生产里一天也没生效过，而源码比对一直是绿的。

    现在只有 `ingest_transcript_wrapper` 一份实现，`replay_missed_events` 调
    的是它。守住"第二份别回来"，而不是守"两份别分叉"。
    """
    from app.services.execution_ingest import ExecutionIngestService

    assert not hasattr(ExecutionIngestService, "ingest_transcript_file"), (
        "第二条摄取路回来了 —— 上一次它的代价是两道闸从落地起就没生效过"
    )


def test_the_answer_path_has_no_ledger_verdict_gates():
    """根因 D 的终局（2026-08-24）：不再有"自愈写"，因为不再有拒答可自愈。

    `_mark_run_stale` 连同三道按账面拒答的闸（状态白名单 / resumable 标志 /
    attempt 必须 RUNNING）已整体删除：活 pause 在场时账面矛盾一律和解放行。
    这里守住"删了的不许回来"—— 行为面由
    test_pause_outliving_attempt_is_recoverable.py 用真实事故形态回放。
    """
    assert not hasattr(le, "_mark_run_stale"), (
        "存储式活性判决回来了 —— 它上一次的下场是把活着的 pause 说成不可续，"
        "用户无限撞'not safely resumable'"
    )
    caller = inspect.getsource(le._validate_resume_binding)
    assert "not safely resumable" not in caller
    # 允许的 raise 只有三种：身份不匹配、durable Run 不存在、人已定案
    # （CANCELLED/COMPLETED）。多出第四种就是账面闸回来了。
    # （不按字符串扫 resumable：它作为**矛盾检测输入**合法出现 —— 断言要落在
    # 行为上，行为面由 test_pause_outliving_attempt_is_recoverable.py 回放。）
    assert caller.count("raise HarnessSessionStaleError") == 3, (
        "答复路径多出了一道拒绝 —— 新闸必须过'活 pause 在场时凭什么拒'这一问"
    )
