"""交付对账后台循环：周期性把已完成的自主 run 补发到 Artifacts 面。

**触发为什么在这里、而不在 execute_local_turn 尾部**：见
`deliverable_publishing.reconcile_pending_deliveries` 的模块注释与函数注释。
一句话——自主 run 的 root 完成由**事件 ingestion** 确立（transcript / replay /
孤儿 reconcile 都可能是那条路），execute_local_turn 的 auto-publish 块对这次完成
根本不在场（E2E v33 实证：run.summary 全空、零 revision）。所以判据必须落在
「DB 里 root run 到达 COMPLETED」这件所有路径都汇入的事实上，由本循环来对账。

本文件只负责「多久对一次账 + 生命周期」；交付逻辑与幂等全在 deliverable_publishing。
镜像 feed.scheduler.feed_collector 的 start/stop 形状。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: 对账节拍。研究 run 是小时级的，60s 内出现在 Artifacts 面完全够用。
RECONCILE_INTERVAL_SECONDS = 60.0
#: 启动后先让孤儿重整 / 数据根校验先跑完，别和它们抢开局。
INITIAL_DELAY_SECONDS = 20.0


def log_outcome(outcome: dict) -> None:
    """一条对账结果 → 一条日志。抽成模块函数是为了它能被测到。

    `unreachable` 这一支**一条 run 只会走一次**：那一轮把不可达证据记进了 run 行，
    之后每一轮 reconcile 都在漏斗里无声跳过它、不再回报（见
    `deliverable_publishing` ⑤）。所以这唯一的一次要把话说完 —— 断在哪一环、
    缺哪个路径、原始错误是什么、以及它凭什么会自己解封。
    """
    run_id = outcome.get("run_id")
    if outcome.get("unreachable"):
        logger.warning(
            "delivery blocked for run %s: workspace unreachable, %s is missing (%s). "
            "Not retrying while it stays missing; delivery resumes by itself once the "
            "workspace is reachable again. Underlying error: %s",
            run_id, outcome["unreachable"], outcome.get("probe"), outcome.get("error"),
        )
    elif outcome.get("error"):
        logger.warning("delivery reconcile error for run %s: %s", run_id, outcome["error"])
    elif outcome.get("delivered") or outcome.get("revision_no"):
        logger.info(
            "delivered run %s: %s (revision %s%s)",
            run_id, outcome.get("delivered"), outcome.get("revision_no"),
            "; flushed MEMORY.md" if outcome.get("flushed_memory") else "",
        )
    elif outcome.get("skipped"):
        logger.info(
            "delivery skipped for run %s (evidence missing): %s", run_id, outcome["skipped"]
        )


class DeliveryReconciler:
    """周期性调用 reconcile_pending_deliveries 的后台任务。"""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="delivery-reconciler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        from app.database import get_session_factory
        from app.services.deliverable_publishing import reconcile_pending_deliveries
        from app.services.restart_resume import resume_interrupted_continuous_runs

        try:
            await asyncio.sleep(INITIAL_DELAY_SECONDS)
        except asyncio.CancelledError:
            return

        while True:
            try:
                factory = get_session_factory()
                async with factory() as db:
                    outcomes = await reconcile_pending_deliveries(db)
                for outcome in outcomes:
                    log_outcome(outcome)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - 一轮失败不终结循环
                logger.exception("delivery reconciler pass failed")

            # 被重启打断的 continuous 研究：自动从断点续跑（Layer B）。与交付对账
            # 同一个节拍、同一个后台任务 —— 都是"DB 里出现了某个终态/僵态，去补做
            # 那件本该自动发生的事"。判据与幂等全在 restart_resume；这里只点火。
            try:
                resumed = await resume_interrupted_continuous_runs(get_session_factory())
                if resumed:
                    logger.info("auto-resumed %d interrupted continuous run(s): %s",
                                len(resumed), ", ".join(resumed))
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - 一轮失败不终结循环
                logger.exception("continuous-run auto-resume pass failed")

            try:
                await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return


delivery_reconciler = DeliveryReconciler()
