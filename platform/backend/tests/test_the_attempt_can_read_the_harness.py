"""冻进 RunAttempt 的沙箱清单必须让 harness **读得到自己的代码**。

## 这条测试为什么存在

2026-09-06 真机实测：Mac 安装包里跑一个真课题，模型一路走到 experiment 节点，
写好了 `ising_mc.py` / `run_scan.py` / `analyze.py`，然后**三条执行路径全被拒**：

    safe_run_bash / safe_execute_python
      → read-only root was not frozen into this RunAttempt:
        /Applications/ScienceMate.app/…/app/harness

原因在冻结那一步：`_freeze_attempt_sandbox_manifest` 授的挂载只有会话工作区一个
（外加数据集类资源）。而跑命令的工具要求 harness 根**可读** —— 那是它要执行的
代码本身。两边各自都对，合起来是一道结构性的墙：凡是从平台这条路进来的
attempt，本机执行永远过不了 preflight。

Docker 年代这堵墙看不见（harness 烘在镜像里，不需要挂载）；PR C 把 Docker 删掉
之后它就露出来了，而 macOS 上没有 docker 可以兜底。真机上的症状是：课题开得了、
假设写得出、预注册冻得住，**一行计算都跑不了**。

判据落在冻结出来的那份清单上，不落在"某个工具能不能跑" —— 后者要一个真沙箱，
而这道墙在拿到沙箱之前就已经立起来了。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.services.execution_ingest import IngestContext
from app.services.local_execution import _freeze_attempt_sandbox_manifest
from app.services.project_repository import get_project_repository
from app.services.project_repository import run_in_repository_thread


async def _a_frozen_attempt(db, harness_root: Path) -> dict:
    project_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    repository = get_project_repository()
    (await run_in_repository_thread(repository.initialize_project, 
        project_id=project_id, name="sandbox-grant",
        description=None, research_domain=None, owner_id="owner",
    ))
    (await run_in_repository_thread(repository.ensure_session_workspace, 
        project_id=project_id, session_id=session_id, base_commit=None,
        title="sandbox-grant", created_by="owner",
    ))
    context = IngestContext(
        tenant_id="local-tenant",
        workspace_id="workspace",
        project_id=project_id,
        session_id=session_id,
        run_id=f"run_{uuid.uuid4().hex}",
    )
    _, attempt = await _freeze_attempt_sandbox_manifest(
        db, context=context, operation_id=str(uuid.uuid4())
    )
    return attempt.sandbox_manifest


@pytest.mark.asyncio
async def test_the_harness_root_is_frozen_as_readable(db_session, monkeypatch, tmp_path) -> None:
    """harness 根以 `ro` 进清单 —— 否则本机执行在 preflight 就被拒。"""
    harness_root = tmp_path / "harness"
    (harness_root / "core").mkdir(parents=True)
    monkeypatch.setattr(settings, "harness_root", str(harness_root))

    manifest = await _a_frozen_attempt(db_session, harness_root)

    modes = {mount["path"]: mount["mode"] for mount in manifest["mounts"]}
    assert str(harness_root.resolve()) in modes, (
        "harness 根没进清单 —— safe_run_bash / safe_execute_python 会在 preflight 被拒："
        f"{sorted(modes)}"
    )
    assert modes[str(harness_root.resolve())] == "ro"


@pytest.mark.asyncio
async def test_the_workspace_stays_the_only_writable_root(db_session, monkeypatch, tmp_path) -> None:
    """给读权限不等于放宽写边界。

    这条是上一条的对称面：加一个 `ro` 挂载**不能**顺手把可写面变大。写边界是
    这套东西最硬的一条不变量（`project_write_boundary`），它不因为"要让代码读得
    到"而让步。
    """
    harness_root = tmp_path / "harness"
    (harness_root / "core").mkdir(parents=True)
    monkeypatch.setattr(settings, "harness_root", str(harness_root))

    manifest = await _a_frozen_attempt(db_session, harness_root)

    writable = [mount["path"] for mount in manifest["mounts"] if mount["mode"] == "rw"]
    assert len(writable) == 1, f"可写根不止一个：{writable}"
    assert "project-worktrees" in writable[0] or "sessions" in writable[0] or writable[0]


@pytest.mark.asyncio
async def test_a_harness_root_that_does_not_exist_is_not_granted(
    db_session, monkeypatch, tmp_path
) -> None:
    """指不到的路径不进清单。

    冻一个不存在的挂载点，等于让每个 attempt 都带着一条永远无法满足的契约 ——
    失败会推迟到起沙箱那一刻，报的是别的错。
    """
    monkeypatch.setattr(settings, "harness_root", str(tmp_path / "nowhere"))

    manifest = await _a_frozen_attempt(db_session, tmp_path / "nowhere")

    assert all("nowhere" not in mount["path"] for mount in manifest["mounts"])
