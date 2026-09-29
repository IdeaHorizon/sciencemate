"""自更新：查、装、重启。机制全在 `services/self_update.py`，这里只是三个口。

个人档：本机用户点了就装（接口只听 127.0.0.1）。组织档：装更新会换掉整台
服务器的 harness，只许 institution_admin。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app import assembly
from app.auth import get_current_user
from app.config import settings, the_data_root
from app.services import release_feed
from app.services import self_update as su

router = APIRouter()


def _harness_dir() -> Path | None:
    root = (settings.harness_root or "").strip()
    return Path(root).expanduser() if root else None


def _may_install(user) -> bool:
    if assembly.profile_name() == "personal":
        return True
    return getattr(user, "role", None) == "institution_admin"


def _check():
    # 更新源由装配层回答（显式配置 > 包里烧的发行 > 出厂值）：专业版的更新在
    # 私有仓库，这里若直读 settings 就永远只认公开仓库。
    return su.check_for_update(the_data_root(), _harness_dir(), assembly.update_source(),
                               release_feed.get_json, release_feed.get_bytes)


@router.get("/update")
async def get_update_status() -> dict:
    status, _manifest, _source, _assets = await asyncio.to_thread(_check)
    return status.as_dict()


@router.post("/update/install")
async def install_update(user=Depends(get_current_user)) -> dict:
    if not _may_install(user):
        raise HTTPException(status_code=403, detail="装更新会换掉这台服务器的 harness，只有机构管理员能做")

    def _install():
        status, manifest, source, assets = _check()
        if status.error:
            raise su.UpdateError(status.error)
        if manifest is None or not status.available_version:
            return status, None
        archive_url = source.archive_url_for(manifest["archive"], assets)
        # manifest 带 extras（壳等）就连第二个归档一起下 —— 两个要么都到位要么都不算。
        extras_url = (source.archive_url_for(manifest["extras"]["archive"], assets)
                      if manifest.get("extras") else None)
        su.download_and_stage(the_data_root(), manifest, archive_url, release_feed.stream, extras_url=extras_url)
        return status, manifest["version"]

    try:
        status, staged = await asyncio.to_thread(_install)
    except su.UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**status.as_dict(), "staged_version": staged or status.staged_version,
            "restart_exit_code": su.RESTART_EXIT_CODE}


@router.post("/update/restart")
async def restart_for_update(user=Depends(get_current_user)) -> dict:
    if not _may_install(user):
        raise HTTPException(status_code=403, detail="只有机构管理员能重启服务器")
    staged = su.staged_version(the_data_root())
    if staged is None:
        raise HTTPException(status_code=409, detail="没有暂存的更新，不需要重启")
    su.schedule_restart()
    return {"restarting": True, "staged_version": staged, "exit_code": su.RESTART_EXIT_CODE}
