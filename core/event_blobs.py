"""事件日志里的大 payload 只存**引用**（RFC 异步运行时 P0-7）。

## 为什么（Codex 的 1.4GB 事故）

对标时在 `~/.codex/recovery/` 里翻到一个被内联 base64 图片撑到
1,476,355,395 字节的会话日志。修复靠的是把 payload 外置 + 逐条 SHA-256 核对。

我们这边的事件文件有完全相同的形状：`transcript` 包装事件里带着整条原生
记录，而原生记录里可以有一张图、一份 200 万字符的工具输出。一条这样的记录
会同时干掉三件事 —— 事件文件、协议行长度上限、以及后端恢复时的内存。

这与 KB 的「有界读取」是同一条不变量：**一次读取的规模不能由被读对象的
规模决定**。

## 判据是"这件事"，不是"这个字段"

按字段名列黑名单必然漏新字段（`image_b64` 挡住了，明天来一个 `figure_data`
就没挡住，而且没人会发现）。这里的判据是**值的大小** —— 递归扫整个 payload，
任何超过阈值的字符串都外置。新字段默认被覆盖，不是默认漏过。

## 外置不成怎么办：截断 + 见证，绝不静默内联

拿不到 blob 目录（CLI、一次性执行）时不能"那就原样写吧" —— 那正是事故本身。
改成留一段可读的开头 + 一条说明为什么被截。事实变少了，但它自己说得出
自己变少了。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

#: 单个值的外置阈值。64 KiB 足够放下任何**人要读的**文本；再大的一定是
#: 数据（图片、原始输出、序列化产物），而数据属于文件，不属于日志行。
MAX_INLINE_VALUE_BYTES = 64 * 1024

#: 引用里保留的开头。让人在不打开 blob 的情况下也能认出这是什么东西。
PREVIEW_CHARS = 200

#: 引用对象的标记键。读侧靠它认出"这不是内容，这是一张提货单"。
REF_KEY = "__event_blob__"

#: blob 目录名，与事件文件同级。
BLOB_DIRNAME = "event_blobs"


def blob_dir_for(events_path: Path | str) -> Path:
    """事件文件对应的 blob 目录。"""
    return Path(events_path).parent / BLOB_DIRNAME


def _reference(
    value: str, *, blob_dir: Path | None, reason: str = ""
) -> dict[str, Any]:
    raw = value.encode("utf-8", "replace")
    digest = hashlib.sha256(raw).hexdigest()
    ref: dict[str, Any] = {
        REF_KEY: True,
        "sha256": digest,
        "bytes": len(raw),
        "preview": value[:PREVIEW_CHARS],
    }
    if blob_dir is None:
        # 落不了盘就如实说：这段内容**没有**在任何地方保存下来。
        ref["dropped"] = reason or "no_blob_directory"
        return ref
    try:
        blob_dir.mkdir(parents=True, exist_ok=True)
        target = blob_dir / f"{digest}.bin"
        if not target.exists():
            # 同名即同内容（sha256 是文件名），所以已存在就不重写 ——
            # 重复的大 payload 天然去重。
            tmp = target.with_suffix(".bin.part")
            tmp.write_bytes(raw)
            tmp.replace(target)
        ref["path"] = str(target)
    except OSError as exc:
        ref["dropped"] = f"{type(exc).__name__}"
    return ref


def externalize(value: Any, *, blob_dir: Path | None) -> Any:
    """递归把超阈值的字符串换成引用。其余原样返回。

    只处理**字符串**：数字、布尔撑不大一个日志行，而容器本身的大小由它装的
    字符串决定 —— 换掉叶子，容器自然就小了。真正病态的容器（几十万个小元素）
    是另一个问题，不在这条防线的射程内，别顺手在这里加第二种判据。
    """
    if isinstance(value, str):
        if len(value.encode("utf-8", "replace")) <= MAX_INLINE_VALUE_BYTES:
            return value
        return _reference(value, blob_dir=blob_dir)
    if isinstance(value, dict):
        return {key: externalize(item, blob_dir=blob_dir) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [externalize(item, blob_dir=blob_dir) for item in value]
    return value


def is_reference(value: Any) -> bool:
    """这是一张提货单吗。"""
    return isinstance(value, dict) and value.get(REF_KEY) is True
