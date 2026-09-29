"""把这个项目的科研记录升到本版本读得懂的格式 —— 在它**将被使用**的那一刻。

## 为什么不是启动时扫一遍（2026-09-15 改）

第一版把这道工序挂在 `main.lifespan`：后端起来时遍历全部项目，各自记账，
汇总成 `/health` 里一个 `deferred: N, failed: M` 的字符串。三个后果：

1. **被拒的工作区没有任何人会知道。** 迁移器对坏历史必须停下（不伪造历史），
   但那句"为什么停下"只进了日志。撞墙的用户读到的是另一套话，两边对不上。
2. **重试要重启整个应用。** 有 worker 攥着工作区就推迟到"下次启动"——桌面版
   用户的下次启动是退出再打开 ScienceMate。
3. 它按**进程生命周期**触发，而这件事的真正条件是"有人要用这个项目"。没人
   打开的项目在开机时被改写 Git 历史，是白花的风险。

所以触发点在派发前：`execute_local_turn` 起 worker 之前调一次。没有旧信封时
它只是一次 glob 的空转；有的话就在这一轮里升级完再开跑。升级失败**就是这一轮
的失败**，原因原样进产品文案 —— 事实送到了做决定那个人手里，而"重试"不再
需要一条专门的路：发下一条消息就是重试。

顺带删掉的还有"项目仓本体也算一个候选工作区"那一条：它走的是 `session is None`
那条岔路，于是**跳过了活 worker 检查**——一边有会话正在写，一边改写共用仓的
Git 历史。而记录是在会话工作区里读的，项目仓本体根本没有读者。

## 为什么不拿 DB 里那个 head 当前提（2026-09-15 第二次改）

原来这里还有一道守卫：`session.git_head_commit_sha` 与工作区真实 HEAD 对不上
就拒绝迁移（`Project HEAD changed outside migration`），外加一段读迁移回执把
两者接回来的恢复分支。

上机量过之后这道守卫必须删：node20 上 19 个迁不动的工作区里 **15 个**倒在它
手上，而真正的坏历史只有 3 个。"DB head 落后于真实 HEAD"根本不是异常，是部署
之后的常态 —— 旧 worker 提交了、平台自己的旧提交没回写（`deliverable_publishing`
里记着同一件事）。11 个会话的 DB head 连真实 HEAD 的祖先都不是。

而它**保护不了任何东西**：迁移器全程在一个分离工作区里核验，打 `backup_ref`
留下迁移前的完整状态，最后只做 ff-merge，并且建在**真实 HEAD** 上。它的安全性
一条都不依赖 DB 那份抄件对不对。倒过来说才对：迁完回写真实 HEAD，这份陈旧就
被**修好**了。
"""
import logging
from pathlib import Path

from app.services.harness_contract import record_migration_module
from app.services.harness_sessions import HarnessSessionError, harness_session_manager
from app.services.project_repository import get_project_repository, run_in_repository_thread

logger = logging.getLogger(__name__)


class RecordUpgradeBlocked(RuntimeError):
    """这次升不了：另一个进程攥着这个工作区。

    `code` 借 `project_busy` —— 用户要做的事和那条一模一样（等它放手 / 点停止，
    然后再发一次）。文案表的加条判据是"用户能做的事不一样"，这里不一样的只有
    我们内部的原因，不该多造一句同义的话。
    """

    code = "project_busy"


class RecordUpgradeFailed(RuntimeError):
    """迁移器拒绝了这个工作区的历史 —— 需要人来看，重发不会改变结果。"""

    code = "workspace_record_upgrade_failed"


def _has_envelopes(path: Path) -> bool:
    return (
        any(path.glob("*/artifacts/*.json"))
        or any(path.glob("*/artifacts/.frozen.jsonl"))
        or any(path.glob("*/artifacts/.versions/*.json"))
    )


def _migrate_worktree(repository, module, project_id: str, path: Path) -> tuple[dict, str]:
    """在仓库线程里、持项目锁跑。返回 (迁移器回执, 迁完的 HEAD)。

    迁移器自己的判决原样抛出（历史缺失、冻结校验不符、身份冲突 —— 永不伪造
    历史）；调用方决定那对这一轮意味着什么。
    """
    with repository._project_lock(project_id):
        report = module.migrate_records(path, checkpoint_pending=True)
        return report, repository._git(path, "rev-parse", "HEAD")


async def upgrade_records_before_use(db, session, *, project_id: str, owner_user_id: str) -> dict | None:
    """这一轮开跑前把工作区升到原生记录格式；升不了就抛，让这一轮失败在这里。

    工作区路径按 worker 自己那套算（`session_path`），不读 DB 里那份抄件 ——
    两处各算一次就是两个真相源，而 worker 用的是这一个。

    返回迁移器的回执；没有旧信封时返回 None，而那是绝大多数轮次走的路：一次
    glob 就返回，连契约模块都不加载。
    """
    repository = get_project_repository()
    path = repository.session_path(project_id, str(session.session_id))
    if not path.is_dir() or not _has_envelopes(path):
        return None
    module = record_migration_module()
    try:
        await harness_session_manager.prepare_workspace_upgrade(
            project_id, str(session.session_id), owner_user_id=owner_user_id)
    except HarnessSessionError as exc:
        # 攥着它的那个 worker 留着不杀（它可能正跑着几小时的研究）。
        raise RecordUpgradeBlocked(str(exc)) from exc
    try:
        report, head = await run_in_repository_thread(
            _migrate_worktree, repository, module, project_id, path)
    except Exception as exc:  # noqa: BLE001 - 迁移器的判决要带着原话到用户面前
        logger.exception("Research record upgrade refused for %s", path)
        raise RecordUpgradeFailed(f"{type(exc).__name__}: {exc}") from exc
    session.git_head_commit_sha = head
    # 这一句**立刻**落库，不搭这一轮后面的顺风车：ff-merge 是不可逆的，而这一轮
    # 随后任何一处失败都会 rollback。两者一起回滚不了，就必须一起提交 —— 否则
    # 磁盘上迁完了、DB 还记着迁移前的 head，而 checkpoint 权威拿它当 expected
    # 且 fail-closed，这个会话后面每一次落盘都会被拒。
    await db.commit()
    logger.info("Upgraded research records in %s (%d record(s))", path, len(report.get("records") or []))
    return report
