"""Ask the harness for knowledge — through the same bridge that runs the chat.

## Why the App Server has no KB of its own

Knowledge and memory are written by agents inside the harness (`create_claim`,
`kb_ingest`, `add_memory_candidate`) into `$HARNESS_FRAMEWORK_HOME`.  This
service's `kb_*` / `memory_*` tables were an earlier parallel domain model,
built before `platform_runtime` bridged execution onto the harness; on the
deployed instance with 15 real projects they held zero rows.  They are gone.

## Why this is a bridge call and not a file read

An earlier attempt had this module parse the harness JSONL directly.  That
quietly re-implemented `State.list_kb` — scope routing, project-shadows-org, id
dedup — a second copy of rules the harness owns, which would drift the first
time it changed one.  That split is exactly what this migration exists to end,
so re-creating it one layer down was the wrong shape.

The harness already publishes a read API for this caller: `core/api.py`, whose
module docstring names it outright — "平台只读 API —— `hf` CLI / 未来前端的统一
查询入口".  `platform_runtime` forwards to it under `op=kb_query`.  So the UI
asks the harness the same way the chat does, and there is one implementation.

Reads only.  Writes (resolving a proposal, flipping a claim) go through harness
tools with their own admission rules; a side channel from this service would
route around the gates that make the KB trustworthy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import data_root, settings, the_org_home, the_projects_home
from app.models.user import User
from app.services import harness_bridge_once
from app.services.harness_runtime import (
    harness_subprocess_env,
    the_interpreter_that_runs_the_harness,
)

_TIMEOUT_SECONDS = 30


class HarnessKBError(RuntimeError):
    """The harness could not answer this knowledge query."""


async def query(user: User, project_id: str, entity: str, *,
                search: str = "", limit: int = 50, offset: int = 0,
                scope: str = "project") -> list[dict[str, Any]]:
    """One read against the harness knowledge base.

    `entity` is one of concepts / claims / chunks / experiments / memory /
    proposals — the vocabulary `core.api` accepts, not a second one invented here.
    """
    payload = await _ask({
        "op": "kb_query",
        "request_id": f"kb-{uuid4().hex}",
        "project_id": project_id,
        # 读哪一层。`org` = 全组织共用的那一份（晋升上去的知识），和"某个项目的"
        # 是两件事，所以说出来而不是靠 project_id 空不空推。
        "scope": scope,
        "home_dir": str(_home_dir(user)),
        # org 层是**这个人所在组织**的，不是这个用户 home 底下推出来的（`config.the_org_home`）。
        # 显式送过去：环境变量分不清"App Server 说的"和"环境里剩下的"。
        "org_home": str(the_org_home(user.institution_id)),
        "entity": entity,
        "query": search,
        "limit": limit,
        "offset": offset,
    })
    records = payload.get("records")
    records = list(records) if isinstance(records, list) else []
    if scope == "org":
        # `core.api.kb_search` 在没给项目时是「跨项目搜」：org 层**加上这个 home 底下
        # 所有项目**。那是 CLI 要的；这里问的是组织的那一份 —— 不筛的话，提问者自己
        # 项目里的结论会当成「组织知识」列出来。
        records = [r for r in records if isinstance(r, dict) and r.get("scope") == "org"]
    return records


async def stats(user: User, project_id: str, *, scope: str = "project") -> dict[str, Any]:
    """Entity counts straight from `core.api.kb_stats`。`scope="org"` = 组织层那一份。"""
    payload = await _ask({
        "op": "kb_query",
        "request_id": f"kb-{uuid4().hex}",
        "project_id": project_id,
        "scope": scope,
        "home_dir": str(_home_dir(user)),
        "org_home": str(the_org_home(user.institution_id)),
        "entity": "stats",
    })
    got = payload.get("stats")
    return got if isinstance(got, dict) else {}


async def resolve_proposal(user: User, project_id: str, proposal_id: str, *,
                           decision: str, reasoning: str) -> dict[str, Any]:
    """Accept or reject a curator proposal through the harness's own tool.

    Writes go the same way reads do.  Resolving a proposal has harness-owned
    side effects (an accepted skill candidate writes `org/skills/<name>/SKILL.md`)
    and an auditable reasoning requirement — flipping a status field from here
    would skip both.
    """
    payload = await _ask({
        "op": "kb_resolve_proposal",
        "request_id": f"kb-{uuid4().hex}",
        "project_id": project_id,
        "home_dir": str(_home_dir(user)),
        "org_home": str(the_org_home(user.institution_id)),
        "proposal_id": proposal_id,
        "decision": decision,
        "reasoning": reasoning,
    }, expect="kb_resolve_proposal_result")
    return payload


# ── 组织的待审（晋升）──────────────────────────────────────────────────────
#
# 项目做完时，worker 把够格的结论交给**它所在的组织**（`core.kb_promotion`）：死路与
# 书目直落，验证结论与方法配方进这个组织的待审。这里是组织页上看它、裁它的那一半。
# 队列和落地都是 harness 的 —— 采纳要把提上来那张卡连同证据闭包写进 org 层，
# 这里不碰那些文件。


async def review_queue(user: User) -> list[dict[str, Any]]:
    """这个人所在组织的待审，全部（谁能看哪几条由调用方按项目筛）。"""
    payload = await _ask({
        "op": "kb_promotion", "action": "list",
        "request_id": f"kb-{uuid4().hex}",
        "home_dir": str(_home_dir(user)),
        "org_home": str(the_org_home(user.institution_id)),
    }, expect="kb_promotion_result")
    got = payload.get("proposals")
    return list(got) if isinstance(got, list) else []


async def adopt(user: User, proposal: dict[str, Any]) -> dict[str, Any]:
    """采纳：把提上来的那张卡落进这个组织的 org 层。

    原结论和证据在**那个项目的家**里（`config.the_projects_home`，`_ask` 每一问都说）——
    调用方核过那个项目是这个组织的之后才叫这里。
    """
    payload = await _ask({
        "op": "kb_promotion", "action": "adopt",
        "request_id": f"kb-{uuid4().hex}",
        "home_dir": str(_home_dir(user)),
        "org_home": str(the_org_home(user.institution_id)),
        "project_id": str(proposal.get("project_id") or ""),
        "proposal_id": str(proposal.get("id") or ""),
        "by": user.email,
    }, expect="kb_promotion_result")
    return dict(payload.get("proposal") or {})


async def decline(user: User, proposal_id: str, *, reason: str) -> dict[str, Any]:
    payload = await _ask({
        "op": "kb_promotion", "action": "decline",
        "request_id": f"kb-{uuid4().hex}",
        "home_dir": str(_home_dir(user)),
        "org_home": str(the_org_home(user.institution_id)),
        "proposal_id": proposal_id,
        "by": user.email,
        "reason": reason,
    }, expect="kb_promotion_result")
    return dict(payload.get("proposal") or {})


# ── 改正组织自己的一条知识（`core.org_corrections`）──────────────────────────
#
# 推翻 / 取代 / 撤回裁定是管理员的；提一条更正是组织里谁都可以。改的都是组织自己那一条，
# 不读任何项目 —— 所以这几问的 home 是提问者自己的，org 层是他所在组织的。


async def set_standing(user: User, org_id: str, *, verdict: str, reason: str,
                       superseded_by: str = "") -> dict[str, Any]:
    """组织对这一条的裁定：`refuted` / `superseded` 让它不再作数，`in_force` 撤回裁定。"""
    payload = await _ask({
        "op": "kb_promotion", "action": "reinstate" if verdict == "in_force" else "retire",
        "request_id": f"kb-{uuid4().hex}",
        "home_dir": str(_home_dir(user)),
        "org_home": str(the_org_home(user.institution_id)),
        "org_id": org_id, "verdict": verdict, "reason": reason,
        "superseded_by": superseded_by, "by": user.email,
    }, expect="kb_promotion_result")
    return dict(payload.get("entry") or {})


async def propose_correction(user: User, org_id: str, *, verdict: str, reason: str,
                             superseded_by: str = "") -> dict[str, Any]:
    """这个人对组织的一条知识提出更正，进组织的待审等管理员裁。"""
    payload = await _ask({
        "op": "kb_promotion", "action": "propose_correction",
        "request_id": f"kb-{uuid4().hex}",
        "home_dir": str(_home_dir(user)),
        "org_home": str(the_org_home(user.institution_id)),
        "org_id": org_id, "verdict": verdict, "reason": reason,
        "superseded_by": superseded_by, "by": user.email,
    }, expect="kb_promotion_result")
    return {"proposal": payload.get("proposal"), "already": bool(payload.get("already"))}


async def adopt_a_correction(user: User, proposal_id: str) -> dict[str, Any]:
    """认可一条更正 = 那一条不再作数。它改的是组织自己那一条，不去任何项目里读。"""
    payload = await _ask({
        "op": "kb_promotion", "action": "adopt",
        "request_id": f"kb-{uuid4().hex}",
        "home_dir": str(_home_dir(user)),
        "org_home": str(the_org_home(user.institution_id)),
        "proposal_id": proposal_id,
        "by": user.email,
    }, expect="kb_promotion_result")
    return dict(payload.get("proposal") or {})


def _home_dir(user: User) -> Path:
    """Same per-user harness home the execution bridge writes to.

    Kept identical to `harness_runtime` / `harness_sessions` on purpose: a
    knowledge query must look at the very directory the agents just wrote to.
    """
    state_root = data_root("state").resolve()
    return state_root / "users" / user.id


async def _ask(request: dict[str, Any], *,
               expect: str = "kb_query_result") -> dict[str, Any]:
    # 项目层在哪，每一问都说（`config.the_projects_home`）：项目的知识一个项目一份，不在提问者的
    # home 底下 —— 否则同一个项目，谁来看就读到谁攒的那一份。
    request = {**request, "projects_home": str(the_projects_home())}
    root = Path(settings.harness_root).expanduser().resolve()
    if not (root / "core" / "api.py").is_file():
        raise HarnessKBError("HARNESS_ROOT is not a valid current harness checkout")
    return await harness_bridge_once.ask_once(
        request,
        expect=expect,
        error=HarnessKBError,
        root=root,
        python=the_interpreter_that_runs_the_harness(),
        # 只读查询不碰模型 —— 故意不传任何 LLM_* 凭据。
        # org 层的位置在请求里（`org_home`），不从环境继承：组织档上进程环境里
        # 本来就没有它（`publish_the_data_root`），个人档上有也不该被它拽走。
        child_env=harness_subprocess_env(root),
        timeout_s=_TIMEOUT_SECONDS,
    )
