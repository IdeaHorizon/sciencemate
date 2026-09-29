"""呈现层学术搜索：只交付 literature index，不启动研究产物流程。"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pathlib import Path
import sqlite3
from urllib.parse import unquote

from app.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.literature import (
    LiteratureSearchIn,
    LiteratureSearchOut,
    LiteratureTranslateIn,
    LiteratureTranslateOut,
)
from app.services.feed import bridge
from app.services.feed.literature_projection import literature_papers_root
from app.services.model_backends import select_effective_backend
from app.services.sse import sse_response
import os

router = APIRouter()


@router.post("/search", response_model=LiteratureSearchOut)
async def search_literature(payload: LiteratureSearchIn, user: User = Depends(get_current_user), db=Depends(get_db)) -> LiteratureSearchOut:
    backend = await select_effective_backend(db, user, role=bridge.REASONING_ROLE)
    if backend is None:
        raise HTTPException(status_code=503, detail="没有可用的 reasoning 模型后端")
    try:
        result = await bridge.call(op="literature_search", payload={"query": payload.query, "limit": payload.limit, "remote_refresh": payload.remote_refresh}, result_type="literature_search_result", backend=backend)
    except (bridge.BridgeUnavailable, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    result.pop("type", None)
    result.pop("request_id", None)
    # JSONL emitter 为每个事件统一附加时间戳；它属于传输元数据，不是
    # literature 搜索响应契约的一部分。
    result.pop("at", None)
    return LiteratureSearchOut.model_validate(result)


@router.post("/translate", response_model=LiteratureTranslateOut)
async def translate_literature_page(
    payload: LiteratureTranslateIn,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
) -> LiteratureTranslateOut:
    """按需翻译当前可见页；独立请求，不阻塞学术索引搜索结果。"""
    backend = await select_effective_backend(db, user, role=bridge.REASONING_ROLE)
    if backend is None:
        raise HTTPException(status_code=503, detail="没有可用的 reasoning 模型后端")
    try:
        result = await bridge.call(
            op="literature_translate_page",
            payload={"papers": [paper.model_dump() for paper in payload.papers]},
            result_type="literature_translation_result",
            backend=backend,
        )
    except (bridge.BridgeUnavailable, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    for key in ("type", "request_id", "at"):
        result.pop(key, None)
    return LiteratureTranslateOut.model_validate(result)


@router.get("/asset")
async def literature_asset(
    doi: str = Query(..., min_length=1, max_length=300),
    kind: str = Query(..., pattern="^(pdf|figure)$"),
    name: str | None = Query(default=None, max_length=255),
):
    """安全地提供共享 literature 目录中的 PDF/figure 文件。"""
    # 「论文文件在哪」问 literature_projection —— 投影层和下载接口读的必须是
    # 同一个目录，否则 Feed 里点得开的条目在这里 404。
    root = literature_papers_root()
    catalog = root / "literature_catalog.sqlite3"
    if not catalog.is_file():
        raise HTTPException(status_code=404, detail="本地文献 catalog 不存在")
    normalized = unquote(doi).strip().lower()
    try:
        with sqlite3.connect(catalog) as conn:
            row = conn.execute(
                "SELECT article_dir, pdf_path, figure_path FROM papers WHERE lower(doi)=?",
                (normalized,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="读取本地文献 catalog 失败") from exc
    if not row:
        raise HTTPException(status_code=404, detail="本地没有这篇论文的归档记录")
    article_dir, pdf_path, figure_path = row
    try:
        article = Path(article_dir).resolve()
        if not article.is_relative_to(root):
            raise ValueError
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="本地归档路径无效") from None

    candidates: list[Path] = []
    if kind == "pdf":
        if pdf_path:
            candidates.append(Path(pdf_path))
        candidates.extend((article / "paper").glob("*.pdf"))
    else:
        if name:
            candidates.append(article / "figure" / Path(unquote(name)).name)
        elif figure_path:
            candidates.append(Path(figure_path))
        candidates.extend(
            p for p in (article / "figure").iterdir()
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ) if (article / "figure").is_dir() else None
    target = next((p.resolve() for p in candidates if p.is_file()), None)
    if target is None or not target.is_relative_to(root):
        raise HTTPException(status_code=404, detail="本地没有可用的该资产")
    media = "application/pdf" if kind == "pdf" else None
    return FileResponse(target, media_type=media, filename=target.name)


@router.post("/search/stream")
async def search_literature_stream(payload: LiteratureSearchIn, user: User = Depends(get_current_user), db=Depends(get_db)):
    """流式返回学术搜索阶段，最后发送完整 index 结果。"""
    backend = await select_effective_backend(db, user, role=bridge.REASONING_ROLE)
    if backend is None:
        raise HTTPException(status_code=503, detail="没有可用的 reasoning 模型后端")

    async def events():
        queue: asyncio.Queue[dict] = asyncio.Queue()

        started = time.monotonic()

        # 连接建立后立即通知前端
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "progress",
                    "stage": "strategy",
                    "detail": "正在启动检索策略生成",
                    "progress_percent": 3,
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

        async def on_progress(event: dict) -> None:
            await queue.put(
                {
                    "type": "progress",
                    **{k: v for k, v in event.items() if k != "type"},
                }
            )

        task = asyncio.create_task(
            bridge.call(
                op="literature_search",
                payload={
                    "query": payload.query,
                    "limit": payload.limit,
                    "remote_refresh": payload.remote_refresh,
                },
                result_type="literature_search_result",
                backend=backend,
                on_progress=on_progress,
            )
        )

        last_heartbeat = started
        current_progress = {
            "stage": "strategy",
            "detail": "正在启动检索策略生成",
            "progress_percent": 3,
        }

        while not task.done() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)

                if event.get("type") == "progress":
                    current_progress = {
                        "stage": str(event.get("stage") or "search"),
                        "detail": str(event.get("detail") or "正在检索"),
                        "progress_percent": int(
                            event.get("progress_percent")
                            or current_progress["progress_percent"]
                        ),
                    }

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            except asyncio.TimeoutError:
                now = time.monotonic()

                # 模型等待期间每 5 秒发送一次心跳
                if not task.done() and now - last_heartbeat >= 5.0:
                    elapsed = int(now - started)

                    heartbeat = {
                        "type": "progress",
                        "stage": current_progress["stage"],
                        "detail": f"{current_progress['detail']}（已等待 {elapsed} 秒）",
                        "elapsed_seconds": elapsed,
                        "progress_percent": current_progress["progress_percent"],
                    }

                    yield (
                        "data: "
                        + json.dumps(heartbeat, ensure_ascii=False)
                        + "\n\n"
                    )

                    last_heartbeat = now

        try:
            result = task.result()

            for key in ("type", "request_id", "at"):
                result.pop(key, None)

            validated = LiteratureSearchOut.model_validate(result)

            done_event = {
                "type": "done",
                **validated.model_dump(),
            }

            yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"

        except Exception as exc:
            # SSE 响应头已经发出后，异常不能再由 FastAPI 改成普通 5xx；必须
            # 变成流内 error 事件，否则浏览器只会永远停在最后一条 progress。
            error_event = {
                "type": "error",
                "message": f"检索结果处理失败：{type(exc).__name__}",
            }

            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"


    return sse_response(events(), release=db)
