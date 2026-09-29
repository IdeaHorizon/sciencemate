"""给每一条资讯配一张图 —— 能拿到真图就用真图，拿不到才画一张明显是生成的。

## 三级供给，顺序即诚实度

1. **feed 自带的图**（采集时存进 `image_url`）：APS 的 key image、Phys.org
   的 media:thumbnail、Nature 的落地页 og:image。这是出版商自己选的配图。
2. **论文 PDF 里的插图**：arXiv / bioRxiv 的 feed 与落地页都没有图
   （arXiv 的 og:image 是它自己的 logo），但论文本身有。从 PDF 里裁一张
   出来 —— 那是这篇论文**自己的**图，不是别处借来的。裁不出来就**放弃**，
   不退而求其次渲染正文页（见 `figure_from_pdf` 里那段实测记录）。
3. **生成的封面**：上面两条都拿不到时，画一张带标题与来源的排版卡。

## 为什么第 3 级不是"随便配一张科学插画"

配一张不属于这篇论文的漂亮图片，是**拿装饰冒充内容** —— 读者会以为那是这项
工作的图。生成的封面不会被误认（它就是标题排在色块上），它只负责让版面完整，
不声称任何东西。同理，第 2 级绝不退化成"渲染首页"以外的编造。

## 打真源查过的事（2026-08-24），省得下一个人再查一遍

- **Nature Communications 的文章页没有 `og:image`，也拿不到插图**：我们收到的
  HTML 里只有广告位、期刊页眉横幅和几个 logo，正文插图是后续由脚本加载的。
  拿那张页眉横幅当配图，等于每张 Nature 卡片都是同一条色带 —— 那是装饰冒充
  内容。所以 Nature 系走第 3 级。
- **bioRxiv 在 Cloudflare 后面**（对我们回 429），落地页和 PDF 都取不到，同样
  走第 3 级；`.miss` 记号保证不会每次刷到都再撞一次墙。
- **arXiv 的插图是矢量的**：`get_images()` 对它返回空，得按绘图指令的密度找。
  实测有插图的论文一页能有 350–560 条绘图指令，纯理论文章则一条都没有。

## 为什么按需，不在采集时做

一次采集 300+ 条，每条要下载 0.4–1 MB 的 PDF —— 那是几百 MB 的出站流量，
而其中绝大多数条目根本不会被谁看到。所以在**有人真的要看这张图**的时候才做，
并落盘缓存：同一条只做一次，重启也不用重做（对 arXiv 也礼貌）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

import httpx

from app.config import data_root
from app.services.feed.collectors import USER_AGENT

logger = logging.getLogger(__name__)

#: PDF 最大下载体积。综述类论文可以很大，而我们只要一张图。
MAX_PDF_BYTES = 12 * 1024 * 1024

#: 只看前面这些页。插图通常在正文前半段；扫到末尾徒增耗时。
MAX_PAGES_SCANNED = 12

#: 一页要有这么多绘图指令才算"有插图"。arXiv 的图基本是矢量的
#: （matplotlib / TikZ 画出来的绘图指令），不是嵌入位图 —— 所以判据取
#: 绘图指令的密度，而不是 `get_images()`（实测那个对 arXiv 返回空）。
MIN_DRAWINGS_FOR_FIGURE = 60

#: 裁出来的图至少要这么大，否则多半是个公式或页眉线。
MIN_FIGURE_WIDTH = 180
MIN_FIGURE_HEIGHT = 90

#: 但也不能大到几乎铺满整页。
#:
#: 铺满一页的"图"通常不是图，是页眉线 + 表格框 + 脚注线的并集 —— 按它裁剪
#: 等于把一整页正文渲染出来。实测（2026-08-24，真部署）那样的产物是一张
#: 小到读不了的文字页：既没有信息量，看起来还像坏了。
MAX_FIGURE_PAGE_FRACTION = 0.75

#: 同时最多几个 PDF 在下载。
#:
#: 首页一屏 24 张卡，浏览器会**同时**发 24 个图片请求。不设闸就是同时向
#: arXiv 拉 24 个 PDF —— 对它是一次小型压测，对我们是把工作进程全占住。
#: 排队等一会儿的代价只是图片晚几秒出来，而那期间卡片本来就是占位状态。
_PDF_SLOTS = asyncio.Semaphore(3)

_ARXIV_ABS = re.compile(r"arxiv\.org/abs/([^/?#]+)", re.IGNORECASE)
_BIORXIV = re.compile(r"biorxiv\.org/content/(10\.[^/?#]+/[^/?#v]+)", re.IGNORECASE)


def cache_dir() -> Path:
    path = data_root("state") / "feed-thumbnails"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(item_id: str) -> Path:
    return cache_dir() / f"{item_id}.png"


def miss_path(item_id: str) -> Path:
    """"这一条derive不出图"的记号。

    没有它的话，一篇纯理论文章（或者一个 404 的 PDF）会在**每次**有人刷到
    这张卡时重下一遍 PDF —— 缓存只记住成功，就等于只对成功的那部分生效。
    """
    return cache_dir() / f"{item_id}.miss"


def pdf_url_for(url: str | None) -> str | None:
    """这条内容的 PDF 在哪。拿不到就返回 None —— **不猜**。

    只认能机械推出 PDF 地址的两家。别的出版商的 PDF 多半在付费墙后，
    猜一个地址过去只会拿到登录页，然后我们把登录页渲染成"插图"。
    """
    if not url:
        return None
    match = _ARXIV_ABS.search(url)
    if match:
        # 去掉版本后缀：`2401.01234v2` 与 `v1` 的图基本一样，共用缓存。
        return f"https://arxiv.org/pdf/{match.group(1).split('v')[0]}"
    match = _BIORXIV.search(url)
    if match:
        return f"https://www.biorxiv.org/content/{match.group(1)}v1.full.pdf"
    return None


async def figure_from_pdf(pdf_url: str) -> bytes | None:
    """下载 PDF，裁一张插图出来。拿不到返回 None。

    找"绘图指令最密的那一页"，按那些指令的包围盒裁剪 —— 直接渲染整页会得到
    一屏正文，那在卡片上既不好看也没有信息量。
    """
    try:
        import fitz  # PyMuPDF，已是本服务的依赖
    except ImportError:  # pragma: no cover - 依赖缺失是部署问题
        logger.warning("PyMuPDF unavailable; cannot derive figures from PDFs")
        return None

    # **边下边看上限**，不是下完再量：`MAX_PDF_BYTES` 写在下载之后的话，
    # 一个 300 MB 的附件会先进内存、再被判超限 —— 上限没有拦住任何东西。
    payload = bytearray()
    try:
        async with _PDF_SLOTS:
            async with httpx.AsyncClient(
                timeout=45.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            ) as client:
                async with client.stream("GET", pdf_url) as response:
                    if response.status_code >= 400:
                        return None
                    async for chunk in response.aiter_bytes():
                        payload += chunk
                        if len(payload) > MAX_PDF_BYTES:
                            logger.info("PDF too large, abandoning %s", pdf_url)
                            return None
    except httpx.HTTPError as exc:
        logger.info("PDF fetch failed for %s: %s", pdf_url, exc)
        return None
    if not payload.startswith(b"%PDF"):
        # 拿到的不是 PDF（登录页、验证码页）。把它渲染成"插图"就是把一张
        # 登录截图当成论文配图 —— 宁可没有。
        return None

    try:
        doc = fitz.open(stream=bytes(payload), filetype="pdf")
    except Exception:  # noqa: BLE001 - 损坏的 PDF 不该带走这次请求
        return None

    try:
        best_page, best_bbox, best_score = None, None, 0
        for page_number in range(min(doc.page_count, MAX_PAGES_SCANNED)):
            page = doc[page_number]
            drawings = page.get_drawings()
            if len(drawings) < MIN_DRAWINGS_FOR_FIGURE or len(drawings) <= best_score:
                continue
            xs: list[float] = []
            ys: list[float] = []
            for drawing in drawings:
                rect = drawing.get("rect")
                if rect and rect.width > 3 and rect.height > 3:
                    xs += [rect.x0, rect.x1]
                    ys += [rect.y0, rect.y1]
            if not xs:
                continue
            bbox = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
            if bbox.width < MIN_FIGURE_WIDTH or bbox.height < MIN_FIGURE_HEIGHT:
                continue
            page_area = page.rect.width * page.rect.height
            if page_area and (bbox.width * bbox.height) / page_area > MAX_FIGURE_PAGE_FRACTION:
                continue
            best_page, best_bbox, best_score = page, bbox, len(drawings)

        if best_page is None:
            # 一张矢量图都没有 —— **不退化成"渲染首页"**。
            #
            # 第一版是那么做的，实测（2026-08-24，真部署）结果是一张 300KB 的
            # 整页正文缩略图：字小到读不了，版面上看着像加载坏了。它比没有图
            # 更糟，因为读者会以为那就是这篇论文的图。
            #
            # 纯理论文章确实存在（实测一篇 31 页的没有任何矢量图），这条路
            # 交给第三级的生成封面 —— 那个明显是生成的，不冒充任何东西。
            return None

        pixmap = best_page.get_pixmap(clip=best_bbox, dpi=110)
        return bytes(pixmap.tobytes("png"))
    except Exception:  # noqa: BLE001
        logger.info("Figure extraction failed for %s", pdf_url, exc_info=True)
        return None
    finally:
        doc.close()


#: 生成封面的配色。按域取，同一个域永远同一套 —— 一眼能认出"这是同一类"。
_PALETTES = (
    ("#1e3a5f", "#2d6a9f"), ("#3d2b56", "#6b4d8f"), ("#1f4d3d", "#357a5b"),
    ("#5a3320", "#96593a"), ("#42304f", "#7a5c8e"), ("#1d3f4d", "#2f7288"),
    ("#4a2b3d", "#84506b"), ("#2b3d1f", "#587a35"),
)


def generated_cover(*, title: str, venue: str, domain: str) -> bytes:
    """画一张排版封面 —— **明显是生成的**，不冒充任何科学插图。

    只放标题与来源。同一个域配色固定，所以同类内容在版面上认得出来。

    返回 SVG 字节。矢量、几百字节、任何缩放都清晰，而且不需要字体文件
    （用系统字体族名）—— 服务端画位图要引一整套字体渲染栈。
    """
    palette = _PALETTES[
        int(hashlib.sha256((domain or venue or "x").encode()).hexdigest()[:8], 16)
        % len(_PALETTES)
    ]
    # 标题按字数硬折行。SVG 没有自动换行，`<foreignObject>` 在部分渲染器里
    # 不生效，所以自己切。
    words = (title or "").strip()
    lines: list[str] = []
    line = ""
    for char in words:
        line += char
        if len(line) >= 26:
            lines.append(line)
            line = ""
        if len(lines) >= 4:
            break
    if line and len(lines) < 4:
        lines.append(line)
    if not lines:
        lines = ["（无标题）"]
    if len("".join(lines)) < len(words):
        lines[-1] = lines[-1][:24] + "…"

    def escape(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    spans = "".join(
        f'<tspan x="40" dy="{0 if i == 0 else 30}">{escape(text)}</tspan>'
        for i, text in enumerate(lines)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" '
        f'width="640" height="360" role="img">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{palette[0]}"/>'
        f'<stop offset="1" stop-color="{palette[1]}"/></linearGradient></defs>'
        f'<rect width="640" height="360" fill="url(#g)"/>'
        f'<text x="40" y="130" fill="#ffffff" font-size="25" font-weight="600" '
        f'font-family="ui-sans-serif, system-ui, -apple-system, sans-serif">{spans}</text>'
        f'<text x="40" y="310" fill="#ffffff" fill-opacity="0.72" font-size="16" '
        f'font-family="ui-sans-serif, system-ui, sans-serif">{escape(venue or "")}</text>'
        f"</svg>"
    ).encode("utf-8")


# ── 后台补缓存（2026-09-01 生产止血）────────────────────────────────────────
# 图片接口的契约是「立刻给一张图」。此前 cache-miss 时在用户请求里排队下载
# PDF（_PDF_SLOTS 队列 + 45s 超时串行），实测单请求挂 394 秒，把浏览器对本站
# 的 6 条连接全部占死 —— 用户点任何导航都没反应。现在：请求立即回生成封面，
# 真图由后台任务补进缓存，下次刷到即是。同一条目并发去重；失败照记 miss
# （「两边都要记」的既有语义原样保留）。
_INFLIGHT: set[str] = set()


def ensure_cached_in_background(item_id: str, pdf_url: str) -> None:
    if item_id in _INFLIGHT:
        return
    _INFLIGHT.add(item_id)

    async def _job() -> None:
        try:
            figure = await figure_from_pdf(pdf_url)
            try:
                (cache_path(item_id) if figure else miss_path(item_id)).write_bytes(
                    figure or b"")
            except OSError:
                logger.warning("Could not cache derived figure for %s", item_id)
        finally:
            _INFLIGHT.discard(item_id)

    asyncio.create_task(_job())
