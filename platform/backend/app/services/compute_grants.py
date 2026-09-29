"""算力授权 —— 谁能用哪台机器、哪几张卡。

## 这一层为什么薄

`grants.yaml` 的格式、探针、合并规则全归 `core/capabilities`（harness 那边）：
它同时喂 agent 的提示注入和预注册冻结门禁，是那件事的唯一真相源。这里只是把
App Server 的请求转过去 —— 自己拼一份 YAML 就是第二份会各自演化的格式知识。

## 为什么此前"算力管理"不存在

读 `grants.yaml` 的有两处，**写它的一处都没有**。于是界面上能看到探针结果，
却没有任何一处能授权一台机器：一个组织装好服务器之后，唯一的办法是登到那台
机器上手写 YAML。09-17 盘点专业版时确认的。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings, the_org_home
from app.models.user import User
from app.services import harness_bridge_once
from app.services.instructions import harness_home_for
from app.services.harness_runtime import (
    harness_subprocess_env,
    the_interpreter_that_runs_the_harness,
)

_TIMEOUT_SECONDS = 60  # 探针要 ssh 出去，比一次 KB 读慢


class ComputeGrantsError(RuntimeError):
    """算力授权读不出来 / 写不进去，且说得出为什么。"""


async def read(user: User) -> dict[str, Any]:
    """整份授权 + 每条此刻探到的现状。"""
    return await _ask(user, {"op": "compute_grants"}, expect="compute_grants_result")


async def write(user: User, grants: dict[str, Any]) -> dict[str, Any]:
    """整份写回。格式不合规由 harness 那边拒（那里是格式的主人）。"""
    return await _ask(user, {"op": "compute_grants_set", "grants": grants},
                      expect="compute_grants_set_result")


async def machines(user: User, replace_with: list[dict] | None = None) -> dict[str, Any]:
    """这个组织登记的机器（`core.machines`）；给了 `replace_with` 就整份写回。"""
    request: dict[str, Any] = {"op": "compute_machines"}
    if replace_with is not None:
        request["machines"] = replace_with
    return await _ask(user, request, expect="compute_machines_result")


async def _ask(user: User, request: dict[str, Any], *, expect: str) -> dict[str, Any]:
    root = Path(settings.harness_root).expanduser().resolve()
    if not (root / "core" / "capabilities.py").is_file():
        raise ComputeGrantsError("HARNESS_ROOT 不是一份当前的 harness checkout")
    payload = {
        **request,
        "request_id": f"grants-{uuid4().hex}",
        # 与 worker 同一个 home（`instructions.harness_home_for`）—— 从前这里自己拼了一个
        # `<数据根>/state/users/<uid>`，和 worker 的 `harness-state/users/<uid>` 不是一个目录。
        "home_dir": str(harness_home_for(user.id)),
        # grants.yaml 住在 org 层 —— 那是**这个人所在组织**的，不是这个用户 home
        # 底下推出来的（同 KB 桥：环境变量分不清"App Server 说的"和"环境里剩的"）。
        "org_home": str(the_org_home(user.institution_id)),
    }
    return await harness_bridge_once.ask_once(
        payload,
        expect=expect,
        error=ComputeGrantsError,
        root=root,
        python=the_interpreter_that_runs_the_harness(),
        # 读授权 + 跑探针，不碰模型：故意不传任何 LLM_* 凭据。
        child_env=harness_subprocess_env(root),
        timeout_s=_TIMEOUT_SECONDS,
    )
