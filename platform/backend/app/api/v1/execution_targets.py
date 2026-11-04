"""这台机器上，项目的模型命令可以在哪里跑（本机 / WSL 发行版）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.services import execution_targets

router = APIRouter()


@router.get("/execution-targets")
async def list_execution_targets() -> dict:
    """这台机器的可选项：``{"host": "win32", "targets": [{"value": "local", ...},
    {"value": "wsl:Ubuntu-24.04", ...}]}``。"""
    import asyncio

    return await asyncio.to_thread(execution_targets.available)
