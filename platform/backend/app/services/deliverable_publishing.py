"""自主完成后：把 referee-approved 的冻结 deliverable 接进 changeset/publish 交付链。

## 为什么存在

harness 侧 `freeze` 只把产物写进会话分支 Git worktree；后端 Artifact 表（=Artifacts
面读的东西）只在 `stage_artifact_candidate`→`publish_change_set` 时才有行。两层之间
**没有任何自动接线** —— 于是 E2E v27/v28/v30 都是「run completed、manuscript 真冻结、
referee 真批」但 Artifacts 面永远空（[[project_e2e_v30_autonomy_stall]] §5）。

wangd 2026-08-23 定方向：**referee approve 即自动 publish**。本模块在 autonomous +
completed 的完成流程里，把每个冻结 deliverable：stage 成 changeset 资源 → 按 policy
issue 必需的 receipt（用 worktree 里的真实证据）→ 交给 publish 转成 durable Artifact 行。

## 哪些是 deliverable：读 harness 的目录模块，不在后端重算

「一个冻结产物算不算 permanent deliverable」的策略在 harness 侧
（`shared.lib.artifact_policy.is_permanent` + 账本上的冻结事实）。**后端进程
import 不到 shared**，也不该把那张策略表抄一份到后端（会各自演化、分叉不报错）。
本模块经契约桥调 `core.catalog.build(worktree)` 取 `is_deliverable` 的条目，正文
从工作区里那个原生文件读 —— 消费 harness 的分类决定（call it, don't reimplement
it）。曾经的 `<state>/…/deliverables/` 抄件已删：一份产物一处真身。

## 铁律：证据不足**不发**，绝不伪造 approval

`review_approval` 只在 worktree 里**确实**有一份 verdict=approve 的 referee critique
审这份 manuscript 时才发；找不到就跳过并如实登记（visible witness），**绝不**凭
“run 完成了”就造一张批准。负面/缺席的证据是证据，不是批准。
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.project_repository import run_in_repository_thread

_MANUSCRIPT_TYPES = {"manuscript", "paper_pdf"}


def read_frozen_deliverables(worktree_root: Path) -> list[dict[str, Any]]:
    """工作区里**冻结且永久**的记录 —— 交付物就是它们，没有第二份拷贝。

    「冻没冻 / 是不是永久 / 伴随文件在哪」全由 harness 的目录模块现算
    （`core.catalog`，经契约桥加载），后端只端出去。每项：
    {type, name, content(str), path, produced_by_node_type, created_at, version}。
    """
    from app.services.harness_contract import catalog_module

    root = Path(worktree_root)
    out: list[dict[str, Any]] = []
    for entry in catalog_module().build(root):
        if not entry.is_deliverable:
            continue
        try:
            content = (root / entry.record_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.append({
            "type": entry.kind, "name": entry.name, "content": content,
            "path": root / entry.record_path,
            "produced_by_node_type": entry.owner_node,
            "created_at": entry.created_at, "version": entry.version,
        })
    out.sort(key=lambda d: (d["type"], d["name"]))
    return out


def _verdict_of_critique(rec: dict) -> str:
    meta = rec.get("metadata") or {}
    v = meta.get("verdict") or meta.get("recommended_action")
    if isinstance(v, str) and v:
        return v.strip().lower()
    content = rec.get("content")
    if isinstance(content, str):
        try:
            cj = json.loads(content)
            if isinstance(cj, dict):
                vv = cj.get("verdict") or cj.get("decision")
                if isinstance(vv, str):
                    return vv.strip().lower()
        except Exception:
            pass
    return ""


def _workspace_records(worktree_root: Path):
    """工作区账本（core/ledger，经契约桥）—— 读记录只有这一条路。"""
    from app.services.harness_contract import ledger_module

    return ledger_module().workspace_store(Path(worktree_root))


def find_writing_approval(worktree_root: Path) -> dict | None:
    """找一份 verdict=approve、审 writing 产物的 referee critique。找不到返回 None。"""
    store = _workspace_records(worktree_root)
    for head in store.heads().values():
        if head.artifact_type != "review_critique" or head.produced_by_node_type != "_reviewer":
            continue
        rec = store.record(head.artifact_id)
        if not isinstance(rec, dict):
            continue
        content = rec.get("content")
        source_is_writing = False
        if isinstance(content, str):
            try:
                cj = json.loads(content)
                source_is_writing = isinstance(cj, dict) and cj.get("source_node_type") == "writing"
            except Exception:
                source_is_writing = "writing" in (rec.get("name") or "")
        if not source_is_writing and "writing" not in (rec.get("name") or ""):
            continue
        if _verdict_of_critique(rec) in {"approve", "approved", "accept", "accepted"}:
            return rec
    return None


def find_writing_validation(worktree_root: Path) -> dict | None:
    """找一份 writing_validation_report（存在即视为 writing 侧验证通过，freeze 前置）。"""
    store = _workspace_records(worktree_root)
    for head in store.heads().values():
        if head.artifact_type == "writing_validation_report":
            rec = store.record(head.artifact_id)
            if isinstance(rec, dict):
                return rec
    return None


def plan_receipts(
    deliverable: dict, worktree_root: Path
) -> tuple[list[tuple[str, dict]], list[str]]:
    """规划一个 deliverable 要发的 receipt。

    返回 (receipts, missing)。missing 非空 ⇒ 证据不足，**不该** publish（不伪造）。
    """
    atype = deliverable["type"]
    name = deliverable["name"]
    receipts: list[tuple[str, dict]] = []
    missing: list[str] = []

    if atype in _MANUSCRIPT_TYPES:
        wv = find_writing_validation(worktree_root)
        if wv is not None:
            receipts.append(("writing_validation", {
                "passed": True,
                "basis": f"writing_validation_report present ({wv.get('name')})",
                "artifact": name,
            }))
        else:
            missing.append("writing_validation: no writing_validation_report found")
        approval = find_writing_approval(worktree_root)
        if approval is not None:
            receipts.append(("review_approval", {
                "decision": "approve",
                "verdict": "approve",
                "basis": f"referee critique verdict=approve ({approval.get('name')})",
                "reviewer_run": approval.get("produced_by_run_id"),
                "artifact": name,
            }))
        else:
            missing.append("review_approval: no approving referee critique found")
    else:
        receipts.append(("producer_validation", {
            "passed": True,
            "basis": "frozen by producing node (promoted to deliverables)",
            "artifact": name,
        }))
    return receipts, missing


async def stage_and_publish_deliverables(
    db,
    *,
    project,
    session,
    user,
    state_root: Path,
    worktree_root: Path,
) -> dict[str, Any]:
    """scan（读 harness 已 promote 的集）→ 逐个 (stage + issue receipts)。

    **不在这里 publish**（publish 由调用方既有 auto-publish 段负责，避免双发）。
    证据不足的 deliverable 跳过并回报 skipped，调用方据此发 visible witness。
    """
    from app.services.session_changes import stage_artifact_candidate

    delivered: list[str] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    evidence: dict[str, list[str]] = {}

    for d in read_frozen_deliverables(worktree_root):
        receipts, missing = plan_receipts(d, worktree_root)
        if missing:
            skipped.append({"name": d["name"], "type": d["type"], "reason": "; ".join(missing)})
            continue
        try:
            version = await stage_artifact_candidate(
                db, project=project, session=session, user=user,
                artifact_id=None, name=d["name"], artifact_type=d["type"],
                resource_key=f"artifact:{d['type']}:{d['name']}",
                content=d["content"], mime_type="text/plain",
                description=(
                    f"auto-staged frozen deliverable ({d['type']}) on autonomous completion"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"name": d["name"], "stage_error": f"{type(exc).__name__}: {exc}"})
            continue
        # 收据随发布那次提交写成 commit trailer（见 `_evidence_trailers`）。
        # 从前它们是 `artifact_receipts` 表里的行 —— 而 `write_receipt` 早就
        # 同时写进 git 了，表是投影。证据跟着它证明的那次提交走，比跟着一张
        # 表走更难失散：仓库在，证据就在。
        evidence[f"{d['type']}:{d['name']}"] = [rtype for rtype, _ in receipts]
        delivered.append(f"{d['type']}:{d['name']}")

    return {"delivered": delivered, "skipped": skipped, "errors": errors, "evidence": evidence}


# ── ③ 触发对账：按 DB 完成状态补发，不靠 execute_local_turn 尾部 ──────────────
#
# 为什么不能挂在 execute_local_turn 尾部（原 #657 的挂法）：自主 run 的 root 完成
# 是**事件 ingestion** 确立的（transcript 里 harness 自己 emit 的 root run.completed
# 经 _apply_projection 投成 RunStatus.COMPLETED），而那趟 execute_local_turn 的尾部
# （run.summary / auto-publish 块）**对这次完成根本不在场** —— E2E v33 实证：root run
# status=completed 但 run.summary 全空（title/drive/projectRevisionId 皆 null）、
# project_revisions 只有初始两条。完成还可能由 session_event_replay / 孤儿 reconcile
# 等**后台路径** ingest，更不经过 execute_local_turn。
#
# 所以触发必须挂在**所有完成路径都汇入的那件事**上：DB 里 root run 到达 COMPLETED。
# 对账器按 DB 状态查「autonomous + root 已完成 + 尚未 publish」→ 幂等补发。天然自愈：
# 无论谁把 run 标成 COMPLETED，下一轮对账都接得住。判据落在 DB 事实上，不落在某条
# 到达路径上（[[feedback_mechanism_exists_but_unwired]] /
# [[feedback_guardrails_must_scan_not_list]]）。


def _memory_md_dirty(worktree_root: Path) -> bool:
    """worktree 里根 MEMORY.md 是否有未提交改动（tracked 改动 / 未跟踪新建）。"""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain=v1", "--", "MEMORY.md"],
            cwd=str(worktree_root), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.stdout.strip())


async def _flush_curator_memory_if_dirty(repo, *, project, session, run_id: str,
                                         worktree_root: Path) -> bool:
    """④ curator 所有制的 MEMORY.md flush。

    非 curator 节点（典型是 orchestrator 收尾写叙事段；access/nodes.yaml 里
    orchestrator.write=[notes]，**不含** MEMORY.md）能经 memory_write
    改 MEMORY.md，却**没有它的 checkpoint 权限**——只有 memory_curator.write=[MEMORY.md]。
    于是这类写留成未提交 → worktree 脏 → 挡 staging，且任何节点都提交不了它
    （[[project_e2e_v30_autonomy_stall]] ④）。这里以 **_curator 权限**（映射
    memory_curator，MEMORY.md 在其白名单内、过 _validate_checkpoint_paths）把它提交，
    并推进 expected HEAD（平台自己做 commit，不破坏 checkpoint 权威——裸 `git commit`
    会触发 session_git_head_untrusted，见 [[project_checkpoint_authority_selfheal]]）。

    返回是否真的 flush 了。session.git_head_commit_sha 就地更新，由调用方 flush 落库。

    checkpoint 失败（典型：该 session 的 worktree git head 与 DB 记录分歧 ——
    平台自己的旧提交没回写 git_head_commit_sha，checkpoint 权威 fail-closed 拒绝，
    见 [[project_checkpoint_authority_selfheal]]）**不往上抛**：那是这条 run 单独的
    权威账目问题，需另行自愈，绝不能带塌整条对账 / 交付。留 False，MEMORY.md 保持
    脏，后面 staging/publish 会因此如实失败并被记录（该 run 这次不交付、下次再试）。
    """
    from app.services.project_repository import ProjectRepositoryError

    if not await asyncio.to_thread(_memory_md_dirty, worktree_root):
        return False
    try:
        checkpoint = await run_in_repository_thread(
            repo.checkpoint_session_workspace,
            project_id=str(project.id),
            session_id=str(session.session_id),
            node_type="_curator",
            run_id=run_id,
            run_status="completed",
            workspace_prefix="MEMORY.md",
            paths=["MEMORY.md"],
            expected_head_commit=str(session.git_head_commit_sha or ""),
        )
    except ProjectRepositoryError:
        return False
    session.git_head_commit_sha = checkpoint.commit_sha
    return True


#: 「这一轮交付过没有」的权威标记 —— 写在 **git 提交的 trailer 上**。
#:
#: 从前它是 `project_revisions` 那一行的 message。可 manifest 里写着
#: `authority: git`，表是投影 —— 判据建在投影上，分叉时不报错，只会悄悄重复
#: 交付或永不交付。`publish_linear` 早就用 `Change-Set-ID:` trailer 判幂等，
#: 这里把交付这条路搬到同一把尺子上（RFC X1 第一步）。
DELIVERY_TRAILER = "Delivered-Run"


def _delivery_marker(run_id: str) -> str:
    """发布提交的 message —— 正文一行，加一条机器读的 trailer。"""
    return f"Continuous checkpoint for Run {run_id}\n\n{DELIVERY_TRAILER}: {run_id}"


def _publish_message(marker: str, evidence: dict[str, list[str]]) -> str:
    """发布提交的 message：给人的第一行 + 给机器的几条 trailer。

    证据（producer_validation / writing_validation / review_approval）从前是
    `artifact_receipts` 表里的行。它们跟着**它们证明的那次提交**走，比跟着一张
    表走更难失散：仓库还在，证据就在；换一个库、清一次数据，证据不跟着没。
    """
    lines = [marker]
    for name, kinds in sorted(evidence.items()):
        for kind in sorted(set(kinds)):
            lines.append(f"Deliverable-Evidence: {kind} {name}")
    return "\n".join(lines) if len(lines) > 1 else marker


async def already_delivered(repo, project_id: str, run_id: str) -> str | None:
    """交付过了吗 —— 问 git，不问表。返回那次提交的 sha。"""
    return await run_in_repository_thread(
        repo.commit_with_trailer, str(project_id), DELIVERY_TRAILER, run_id
    )


# ── ⑤ 永久性失败：不可达的工作区只吵一次 ──────────────────────────────────────
#
# 对账每 60s 重试一遍所有没交付的完成 run。绝大多数失败值得重试（锁竞争、
# 一次 checkpoint 权威分歧、publish 撞版本）。但有一类不会：run 的工作区**根本
# 不在磁盘上了**。它每分钟失败一次、每分钟一条 WARNING，直到永远。
#
# 实测（2026-08-24 本地库）：9 条历史集成测试写进真库的 run，数据根从
# `/private/tmp/p0-integration` 搬到 `~/harness-e2e/repo` 之后 worktree 的
# `.git` 链接还指着旧位置，`git rev-parse HEAD` 每次都是
# `fatal: not a git repository`。日志里 4968 条同一句告警 —— 真告警埋在里面
# 没人看得见，这才是代价。
#
# ## 判据是探测链，不是错误串的名单
#
# 不去 match 异常文本（"not a git repository" / "is not initialized" / …）——
# 那是名单式护栏，换一句话就漏（[[feedback_guardrails_must_scan_not_list]]）。
# 这里问的是一个正面的、机械的问题：**交付这条 run 必须存在的那串路径，现在
# 第一个不存在的是哪个**。仓库根 → worktree 目录 → worktree 的 `.git` →
# `.git` 指向的 gitdir，每一环都是"不在就交付不了"。
#
# 顺带记一句踩过的坑：这批 run 的**仓库根是好的**（它就是个合法 git 仓库，
# 还在原地），坏的是 worktree 里那个 `gitdir:` 指针。判据要是照第一直觉写成
# "仓库根不存在"，一次都不会触发，而测试会全绿（[[feedback_right_verdict_wrong_path]]）。


def _linked_gitdir(worktree: Path) -> Path | None:
    """worktree 的 `.git` 若是 `gitdir: <path>` 链接文件，返回它指向的路径。

    普通仓库的 `.git` 是目录（返回 None，上一环已验过它存在）；读不动也返回
    None —— 这个探测只负责回答"链接指向哪"，不负责判断别的失败。
    """
    dot_git = worktree / ".git"
    try:
        if not dot_git.is_file():
            return None
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:") :].strip()
    return Path(target) if target else None


#: 探测链每一环的名字，进证据里 —— 只有 `missing_path` 的话，读记录的人看不出
#: 当时问的是哪个问题（[[feedback_the_record_must_carry_the_whole_proposition]]）。
_PROBE_REPOSITORY_ROOT = "repository_root"
_PROBE_SESSION_WORKTREE = "session_worktree"
_PROBE_WORKTREE_GIT_LINK = "worktree_git_link"
_PROBE_LINKED_GITDIR = "linked_gitdir"


def unreachable_workspace(repo, project_id: str, session_id: str) -> dict[str, str] | None:
    """交付所需的路径链里，**此刻**第一个不存在的那环；全在则 None。

    返回 `{"probe": <环名>, "missing_path": <该环的路径>}`。None 的含义是"没有
    可复算的缺失路径" —— 失败另有原因（值得重试），或者这个问题本身问不出来。
    """
    try:
        project_root = repo.project_path(project_id)
        worktree = repo.session_path(project_id, session_id)
    except Exception:  # noqa: BLE001 - id 不合法之类：问不出路径，就别替它下结论
        return None

    chain = (
        (_PROBE_REPOSITORY_ROOT, project_root),
        (_PROBE_SESSION_WORKTREE, worktree),
        (_PROBE_WORKTREE_GIT_LINK, worktree / ".git"),
    )
    for probe, path in chain:
        if not path.exists():
            return {"probe": probe, "missing_path": str(path)}

    gitdir = _linked_gitdir(worktree)
    if gitdir is not None and not gitdir.exists():
        return {"probe": _PROBE_LINKED_GITDIR, "missing_path": str(gitdir)}
    return None


def delivery_block_holds(block: Any, repo, project_id: str, session_id: str) -> bool:
    """这条 run 此刻还该被静音吗 —— 这份记录的**唯一**解读处。

    记录本身只回答一件事：**这条 run 的不可达已经报过了**。还成不成立不看记录，
    每次现场重探一遍（`unreachable_workspace`）。

    这个分工是被一条红测试逼出来的：最早的写法是"当时缺的那个路径现在还缺不缺"。
    它对"数据根搬回来了"成立，但对**更可能发生的那种修复**是错的 ——
    `git worktree repair` 把 `.git` 重新指向活着的 gitdir，那个旧的悬空路径**永远**
    不会回来，于是这条 run 会被自己的历史证据永久关在门外。
    记录里存的是当时的事实（能查、能复算），判决从现在的世界里现算。
    """
    if not isinstance(block, dict) or not block.get("missing_path"):
        return False
    return unreachable_workspace(repo, project_id, session_id) is not None


async def _record_delivery_block(
    db, *, run_id: str, unreachable: dict[str, str], error: str
) -> None:
    """把这次观测到的不可达写进 run 行 —— 记录"已经报过了"，附上当时的现场。"""
    payload = {
        "probe": unreachable["probe"],
        "missing_path": unreachable["missing_path"],
        "error": error,
        "detected_at": datetime.now(UTC).isoformat(),
    }
    await _write_delivery_block(db, run_id=run_id, payload=payload)


async def _clear_delivery_block(db, *, run_id: str) -> None:
    """工作区又可达了 → 把这行记录抹掉。

    留着一行"当时不可达"的记录不影响静音判决（那是现算的），但会让任何按
    `delivery_block IS NOT NULL` 查"哪些 run 卡着"的人读到一个已经不成立的事实。
    没人维护的字段迟早会被当成事实读（[[feedback_never_updated_field_is_not_a_fact]]）。
    """
    await _write_delivery_block(db, run_id=run_id, payload=None)


async def _write_delivery_block(db, *, run_id: str, payload: dict | None) -> None:
    """单独 `update` + `commit`，且失败不许带塌对账。

    单独一个事务：调用方可能刚 rollback 过，这条记账不能挂在那个已经烂掉的事务上。
    必须落库：落不了的话下一轮又是一条 WARNING，等于没修。
    """
    from sqlalchemy import update

    from app.models.execution import Run

    try:
        await db.execute(update(Run).where(Run.id == run_id).values(delivery_block=payload))
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


async def deliver_completed_run(
    db,
    *,
    run_id: str,
    project,
    session,
    user,
    state_root: Path,
) -> dict[str, Any] | None:
    """幂等交付**一个**已完成 root run 的冻结 deliverable。

    已交付（存在 _delivery_marker 的 revision）→ 返回 None，不重复发。
    否则：④ flush 脏 MEMORY.md → stage（复用 #657，证据不足不发）→ publish。
    """
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.services.project_repository import get_project_repository
    from app.services.session_changes import PublishConflictsFoundError, publish_session

    marker = _delivery_marker(run_id)
    repo = get_project_repository()
    if await already_delivered(repo, str(project.id), run_id):
        return None

    worktree = Path(
        (await run_in_repository_thread(repo.session_status, str(project.id), str(session.session_id))).path
    )

    flushed = await _flush_curator_memory_if_dirty(
        repo, project=project, session=session, run_id=run_id, worktree_root=worktree
    )
    if flushed:
        # git commit 已发生且不可逆 → authority 记录（session.git_head_commit_sha）
        # 必须立刻落库。否则后续 stage/publish 若失败被 rollback，git 领先于 DB
        # 记录 → 下次 checkpoint 报 session_git_head_untrusted（fail-closed）。
        # 「不可撤销的事发生后，别把判据建在希望被保存下来的记录上」。
        await db.commit()

    delivery = await stage_and_publish_deliverables(
        db, project=project, session=session, user=user,
        state_root=state_root, worktree_root=worktree,
    )

    commit_sha = None
    publish_error = None
    # 总是发布：对已完成的自主 run，把这一轮的改动送上 main 就是交付本身。
    # demo / 单轮课题常常没有冻结产物、只有 checkpoint 的 git 提交 —— 那也要发。
    # 真的一点改动都没有时返回 None（不新建提交 → 不写 trailer），对账器会在
    # 时间窗内重试，无害。有内容发出 → 提交带 Delivered-Run trailer → 幂等守卫
    # 下次跳过。
    try:
        commit_sha = await publish_session(
            db, project=project, session=session, user=user,
            message=_publish_message(marker, delivery.get("evidence") or {}),
            change_key=f"delivery:{run_id}",
        )
    except PublishConflictsFoundError as exc:
        publish_error = str(exc)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
        publish_error = str(detail.get("message") or detail.get("code") or exc.detail)

    return {
        "run_id": run_id,
        "flushed_memory": flushed,
        "delivered": delivery["delivered"],
        "skipped": delivery["skipped"],
        "errors": delivery["errors"],
        "commit_sha": commit_sha,
        "publish_error": publish_error,
    }


async def reconcile_pending_deliveries(db, *, limit: int = 20) -> list[dict[str, Any]]:
    """自愈网：把「autonomous + root 已完成 + 未 publish」的 run 逐个幂等补发。

    覆盖所有把 run 标成 COMPLETED 的路径（turn 尾 / transcript replay / 孤儿
    reconcile）。每 run 独立 commit，一个失败不带塌其余。安全反复调用。

    工作区已不可达的 run（⑤）跳过且不出现在返回里 —— 调度器据此不再逐轮告警。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.config import data_root
    from app.models.execution import Run, RunStatus, SessionProjection
    from app.models.project import OperationMode, Project, ProjectConfig
    from app.models.user import User
    from app.services.project_repository import get_project_repository

    # 只看近 30 天完成的 run：够覆盖任何还相关的交付，又不让永久无法交付的
    # 古老 run（worktree 已随 release 迁移消失、或压根没冻结产物）每轮空转重试。
    since = datetime.now(UTC) - timedelta(days=30)
    candidates = (
        await db.scalars(
            select(Run)
            .where(
                Run.parent_run_id.is_(None),
                Run.status.in_(
                    [RunStatus.COMPLETED.value, RunStatus.COMPLETED_WITH_WARNING.value]
                ),
                Run.ended_at.is_not(None),
                Run.ended_at >= since,
            )
            .order_by(Run.ended_at.desc())
            .limit(200)
        )
    ).all()

    # 先把要用的纯量抠出来，循环里只碰这些 —— 循环内每 run 会 commit / rollback，
    # 之后这些 ORM 对象全部过期（expire_on_commit=False 只在没 rollback 时保命）；
    # 再取 `run.id` 会触发同步 lazy-load（`load_scalar_attributes`→`session.execute`），
    # 在 async 上下文里直接崩（greenlet），把整条对账带塌 —— 首次部署实测：一个
    # worktree 的 checkpoint 权威分歧让 deliver 抛错 → except 里取 `run.id` 二次崩。
    todo = [
        (str(r.id), str(r.project_id), str(r.session_id), r.delivery_block) for r in candidates
    ]

    state_root = data_root("state").resolve()
    repo = get_project_repository()
    results: list[dict[str, Any]] = []
    for run_id, project_id, session_id, block in todo:
        if len(results) >= limit:
            break
        # ⑤ 已经报过不可达的 run：现场再探一次。
        # 仍不可达 → 无声跳过（不进 results ⇒ 调度器这一轮不为它写任何日志；
        # 告警只在记录的那一刻发过一次）。已可达 → 抹掉记录，照常往下交付，
        # 不需要谁来解封。
        if block:
            if delivery_block_holds(block, repo, project_id, session_id):
                continue
            await _clear_delivery_block(db, run_id=run_id)
        if await already_delivered(repo, project_id, run_id):
            continue
        config = await db.scalar(
            select(ProjectConfig).where(ProjectConfig.project_id == project_id)
        )
        if not config or config.operation_mode != OperationMode.AUTONOMOUS:
            continue
        project = await db.get(Project, project_id)
        session = await db.scalar(
            select(SessionProjection).where(SessionProjection.session_id == session_id)
        )
        if not project or not session:
            continue
        uid = session.initiating_user_id or session.created_by_user_id
        user = await db.get(User, uid) if uid else None
        if not user:
            continue
        try:
            outcome = await deliver_completed_run(
                db, run_id=run_id, project=project, session=session,
                user=user, state_root=state_root,
            )
            if outcome is not None:
                await db.commit()
                results.append(outcome)
        except Exception as exc:  # noqa: BLE001 - 一个 run 失败不带塌对账循环
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001 - rollback 自身失败也不许带塌
                pass
            detail = f"{type(exc).__name__}: {exc}"
            # ⑤ 失败之后现场问一句：这条 run 的工作区路径链断在哪了？断了就把
            # 那一环记成证据，此后不再重试也不再告警；没断就是可重试的失败，
            # 照旧每轮如实报（不因为"看着眼熟"就静音）。
            unreachable = unreachable_workspace(repo, project_id, session_id)
            if unreachable is None:
                results.append({"run_id": run_id, "error": detail})
                continue
            await _record_delivery_block(
                db, run_id=run_id, unreachable=unreachable, error=detail
            )
            results.append({
                "run_id": run_id,
                "error": detail,
                "unreachable": unreachable["missing_path"],
                "probe": unreachable["probe"],
            })
    return results
