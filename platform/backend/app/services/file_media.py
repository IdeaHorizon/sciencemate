"""把工作区文件的扩展名映射成一个 HTTP media type。

## 为什么不直接用 `mimetypes.guess_type`

`mimetypes` 在首次调用时会读 `/etc/mime.types`、`/usr/local/etc/mime.types` 等
**宿主机上的文件**。同一个 `.md` 在开发机上是 `text/markdown`、在精简的容器镜像
里可能是 `None`。判据依赖运行环境 = 本机永远绿、部署了才出问题，而这类问题的
症状（"PDF 在服务器上变成下载了"）看起来完全不像环境问题。

所以研究产出真正会出现的那些扩展名在这里**写死**，`guess_type` 只做补充。

## 未知扩展名一律 `application/octet-stream`

这个方向是刻意的：表里没有的东西降级成"惰性字节"，而不是让浏览器去猜。猜错的
代价是不对称的 —— 猜成 `text/html` 意味着 agent 写出来的任意文件都能在 API 源
上执行脚本。新增一个扩展名要显式加进来，成本是一行；反过来（默认放行、危险的
再拉黑）漏掉一个的成本是一个 XSS。
"""

from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

DEFAULT_MEDIA_TYPE = "application/octet-stream"

#: 研究产出实际会出现的扩展名。值必须与宿主机无关。
_EXPLICIT: dict[str, str] = {
    # 图 —— postprocess / writing 出的图基本都是 png，少数是矢量。
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
    # 论文
    ".pdf": "application/pdf",
    # 网页产出（sandbox 的责任在响应头，见 repository/raw 路由）
    ".html": "text/html",
    ".htm": "text/html",
    # 文本 / 数据
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".xml": "text/xml",
    ".toml": "text/plain",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".log": "text/plain",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".tex": "text/x-tex",
    ".bib": "text/x-bibtex",
    ".r": "text/plain",
    ".jl": "text/plain",
    ".ipynb": "application/json",
}

#: 前端按这个分流渲染方式。文本一类不在这里 —— 它们走既有的 JSON 文件端点，
#: 那条路已经处理好截断与编码回退。
_IMAGE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/tiff",
        "image/svg+xml",
    }
)


def media_type_for(path: str) -> str:
    """这个路径应该以什么 media type 送出去。未知 → 惰性字节。"""
    suffix = PurePosixPath(path).suffix.lower()
    explicit = _EXPLICIT.get(suffix)
    if explicit:
        return explicit
    guessed, _ = mimetypes.guess_type(path)
    return guessed or DEFAULT_MEDIA_TYPE


def is_image_media_type(media_type: str) -> bool:
    return media_type in _IMAGE_TYPES
