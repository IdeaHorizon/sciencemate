"""自更新：查、装、重启。机制全在 `services/self_update.py`，这里只是三个口。

个人档：本机用户点了就装（接口只听 127.0.0.1）。组织档：装更新会换掉整台
服务器的 harness，只许 institution_admin。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import assembly
from app.auth import get_current_user
from app.config import settings, the_data_root
from app.services import developer_source as ds
from app.services import release_feed
from app.services import self_update as su
from app.services.user_interface import Language, language_on_screen, say

router = APIRouter()


def _harness_dir() -> Path | None:
    root = (settings.harness_root or "").strip()
    return Path(root).expanduser() if root else None


def _may_install(user) -> bool:
    if assembly.profile_name() == "personal":
        return True
    return getattr(user, "role", None) == "institution_admin"


def _check(lang: Language):
    # 更新源由装配层回答（显式配置 > 包里烧的发行 > 出厂值）：专业版的更新在
    # 私有仓库，这里若直读 settings 就永远只认公开仓库。
    return su.check_for_update(the_data_root(), _harness_dir(), assembly.update_source(),
                               release_feed.get_json, release_feed.get_bytes, lang=lang)


@router.get("/version")
async def get_installed_version() -> dict:
    """装着的是哪一版 —— 只读本机的版本标记，不碰网络（侧栏底部每次渲染都问它）。

    `GET /update` 会去更新源查一遍（几百毫秒到几秒，离线时要等超时），拿它来显示一个
    版本号太贵。两处说的版本出自同一个函数（`describe_installed`），不会分叉。
    """
    version, installed_from = await asyncio.to_thread(su.describe_installed, the_data_root(), _harness_dir())
    return {"version": version, "installed_from": installed_from}


@router.get("/update")
async def get_update_status(lang: Language = Depends(language_on_screen)) -> dict:
    status, _manifest, _source, _assets = await asyncio.to_thread(_check, lang)
    return status.as_dict()


@router.post("/update/install")
async def install_update(user=Depends(get_current_user),
                         lang: Language = Depends(language_on_screen)) -> dict:
    if not _may_install(user):
        raise HTTPException(status_code=403, detail=say(
            {"zh": "装更新会换掉这台服务器的 harness，只有机构管理员能做",
             "en": "Installing an update replaces this server's harness; only an institution administrator can"},
            lang))

    def _install():
        status, manifest, source, assets = _check(lang)
        if status.error:
            # status.error 已经是这一屏的语言了，两边写同一句。
            raise su.UpdateError(status.error, en=status.error)
        if manifest is None or not status.available_version:
            return status, None
        archive_url = source.archive_url_for(manifest["archive"], assets)
        # manifest 带 extras（壳等）就连第二个归档一起下 —— 两个要么都到位要么都不算。
        extras_url = (source.archive_url_for(manifest["extras"]["archive"], assets)
                      if manifest.get("extras") else None)
        su.download_and_stage(the_data_root(), manifest, archive_url, release_feed.stream, extras_url=extras_url,
                              from_version=status.installed_version, notes_url=source.page_url)
        return status, manifest["version"]

    try:
        status, staged = await asyncio.to_thread(_install)
    except su.UpdateError as exc:
        raise HTTPException(status_code=409, detail=exc.said_in(lang)) from exc
    return {**status.as_dict(), "staged_version": staged or status.staged_version,
            "restart_exit_code": su.RESTART_EXIT_CODE}


@router.post("/update/acknowledge")
async def acknowledge_update(user=Depends(get_current_user)) -> dict:
    """人看过「已更新到 X」了：之后不再提。"""
    return {"acknowledged": su.acknowledge_update(the_data_root())}


@router.post("/update/restart")
async def restart_for_update(user=Depends(get_current_user),
                             lang: Language = Depends(language_on_screen)) -> dict:
    if not _may_install(user):
        raise HTTPException(status_code=403, detail=say(
            {"zh": "只有机构管理员能重启服务器", "en": "Only an institution administrator can restart the server"},
            lang))
    staged = su.staged_version(the_data_root())
    # 两种「重启才生效」：暂存的更新，和开发者模式的声明改了（设了 / 撤了 / 换了目录）。
    if staged is None and not _describe(lang)["restart_needed"]:
        raise HTTPException(status_code=409, detail=say(
            {"zh": "没有暂存的更新，也没有改过开发者模式，不需要重启",
             "en": "No update is staged and developer mode has not changed, so there is nothing to restart for"},
            lang))
    su.schedule_restart()
    return {"restarting": True, "staged_version": staged, "exit_code": su.RESTART_EXIT_CODE}


# ───────────────────────────────────────────── 开发者模式：从源码 checkout 跑

def _the_running_app() -> Path:
    """现在这个进程的后端 `app/` 住在哪 —— 「开发者模式在不在跑」就是拿它和声明比。"""
    import app

    return Path(app.__file__).resolve().parent


def _describe(lang: Language) -> dict:
    from app import launcher

    return ds.describe(the_data_root(), _the_running_app(), lang,
                       started_by_developer_mode=launcher.RUNNING_DECLARED_CHECKOUT)


def _only_on_the_desktop(lang: Language) -> None:
    """组织服务器没有「我自己的 checkout」—— 它是部署出来的，版本归 server_update 管。"""
    if assembly.profile_name() != "personal":
        raise HTTPException(status_code=403, detail=say(
            {"zh": "开发者模式只有桌面版有；组织服务器的版本由升级流程管",
             "en": "Developer mode exists only on the desktop; an organisation server's version is managed by its upgrade flow"},
            lang))


class DeveloperSourceWrite(BaseModel):
    source_root: str


@router.get("/update/developer")
async def get_developer_source(lang: Language = Depends(language_on_screen)) -> dict:
    """声明了哪个源码目录、合不合格、现在跑的是不是它、要不要重启。"""
    _only_on_the_desktop(lang)
    return await asyncio.to_thread(_describe, lang)


@router.put("/update/developer")
async def set_developer_source(body: DeveloperSourceWrite, user=Depends(get_current_user),
                               lang: Language = Depends(language_on_screen)) -> dict:
    """把桌面版指到一个源码 checkout。先验目录合不合格再写 —— 写一个不合格的目录等于
    「设了但没生效」，关于页上看起来像开着、实际跑的还是随包那份。重启后生效。"""
    _only_on_the_desktop(lang)
    try:
        checkout = await asyncio.to_thread(ds.inspect, body.source_root)
    except ds.DeveloperSourceError as exc:
        raise HTTPException(status_code=400, detail=exc.said_in(lang)) from exc
    ds.write_declared(the_data_root(), str(checkout.root))
    return await asyncio.to_thread(_describe, lang)


@router.delete("/update/developer")
async def clear_developer_source(user=Depends(get_current_user),
                                 lang: Language = Depends(language_on_screen)) -> dict:
    """撤掉声明：下次启动回到随包 / 载荷那份。"""
    _only_on_the_desktop(lang)
    ds.write_declared(the_data_root(), None)
    return await asyncio.to_thread(_describe, lang)
