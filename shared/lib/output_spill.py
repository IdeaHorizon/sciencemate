"""工具输出超限时落盘，返回路径 + 头尾摘录。

**截断永远要给逃生通道。** `run_bash` 原本只返回 stdout 的最后 3000 字符，
更早的内容直接丢——模型看不到，也没有任何办法拿回来。科研场景的求解器日志
动辄上百 MB，出错信息往往在**开头**（参数校验、网格读取失败），而尾部只剩
"Segmentation fault"，于是模型只能反复重跑去猜。

三家外部框架是同一个做法：超限不丢，写进文件、把路径连同头尾摘录一起返回，
需要全文就自己去读。

只给"看得见的截断"是不够的——必须同时给**取回全文的具体办法**，否则等于把
"输出太长"换成"模型不知道还有别的内容"。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 单段输出进上下文的上限。超过就落盘。
DEFAULT_INLINE_LIMIT = 3000
# 落盘后回显的头 / 尾字符数 —— 报错常在开头，进度和结论常在结尾，两头都要。
HEAD_CHARS = 1200
TAIL_CHARS = 1800


def spill_root(state: Any) -> Path | None:
    """大输出保留到哪：优先 run-local，其次工作区，再次 project_root。"""
    for attr in ("run_dir", "workspace_root", "project_root"):
        base = getattr(state, attr, None)
        if base:
            try:
                d = Path(base)
                d.mkdir(parents=True, exist_ok=True)
                return d
            except Exception:
                continue
    return None


def _spill_dir(state: Any) -> Path | None:
    """落盘目录：优先 run-local，其次 project_root，都没有就不落盘。"""
    root = spill_root(state)
    if root is None:
        return None
    try:
        d = root / ".harness" / "tool_output"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def attach_stream(
    result: dict,
    key: str,
    text: str,
    full_path: str | None,
    *,
    inline_limit: int = DEFAULT_INLINE_LIMIT,
) -> dict:
    """子进程输出专用：`text` 已经是尾部截断过的，完整内容在 `full_path`。

    与 `attach` 的区别在于**不再重新落盘** —— 完整输出早就在文件里了，再写一遍
    只会把已经丢掉开头的那份存起来，看着像修好了其实没有。
    """
    text = text or ""
    if not full_path:
        # 没超限（或保留失败）：按普通截断处理
        if len(text) <= inline_limit:
            result[key] = text
            return result
        result[key] = text[-inline_limit:]
        result[f"{key}_full_chars"] = len(text)
        result[f"{key}_note"] = (
            f"输出 {len(text):,} 字符超过 {inline_limit:,} 上限，"
            "更早的内容未能保留。需要完整输出请把命令的 stdout 重定向到文件后再读。"
        )
        return result

    result[key] = text[-inline_limit:] if len(text) > inline_limit else text
    result[f"{key}_path"] = full_path
    try:
        size = Path(full_path).stat().st_size
        result[f"{key}_full_bytes"] = size
        size_txt = f"{size:,} 字节"
    except Exception:
        size_txt = "完整"
    result[f"{key}_note"] = (
        f"输出过长，上面只是结尾片段；{size_txt}的完整输出已保留在 `{full_path}`。"
        f"**开头和中间的内容都在那里，没有丢** —— 用 "
        f"`read_file('{full_path}')` 读（可带 offset/limit 分段）。"
        "求解器的报错常在开头，别只看结尾就下判断。"
    )
    return result


def spill_if_large(
    text: str,
    *,
    state: Any,
    label: str,
    inline_limit: int = DEFAULT_INLINE_LIMIT,
) -> dict:
    """返回一个描述这段输出的 dict。

    未超限 → `{"text": <全文>, "truncated": False}`
    超限且落盘成功 → 头尾摘录 + `path` + `full_chars` + 明确的取回指示
    超限但无处落盘 → 退回尾部截断，并**说明全文已丢失**（不假装它还在）
    """
    if text is None:
        text = ""
    n = len(text)
    if n <= inline_limit:
        return {"text": text, "truncated": False, "full_chars": n}

    d = _spill_dir(state)
    head, tail = text[:HEAD_CHARS], text[-TAIL_CHARS:]

    if d is None:
        return {
            "text": text[-inline_limit:],
            "truncated": True,
            "full_chars": n,
            "spilled": False,
            "note": (
                f"输出 {n:,} 字符超过 {inline_limit:,} 上限，且当前没有可写的落盘"
                f"目录，**更早的 {n - inline_limit:,} 字符已丢失**。"
                "需要完整输出请把命令的 stdout 重定向到文件后再读。"
            ),
        }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]
    path = d / f"{label}-{stamp}-{digest}.txt"
    try:
        path.write_text(text, encoding="utf-8", errors="replace")
    except Exception:
        return {
            "text": text[-inline_limit:],
            "truncated": True,
            "full_chars": n,
            "spilled": False,
            "note": f"输出 {n:,} 字符超限且落盘失败，更早的内容已丢失。",
        }

    return {
        "head": head,
        "tail": tail,
        "truncated": True,
        "spilled": True,
        "full_chars": n,
        "path": str(path),
        "note": (
            f"输出共 {n:,} 字符，已全文写入 `{path}`。上面只是开头 {len(head):,} "
            f"和结尾 {len(tail):,} 字符 —— **中间部分不在这里，但没有丢**。"
            f"需要完整内容用 `read_file('{path}')`（可带 offset/limit 分段读）。"
        ),
    }


def attach(result: dict, key: str, spilled: dict) -> dict:
    """把 spill 结果并进工具返回值。

    未超限 → `result[key] = 全文`
    超限 → `result[key]` 放尾部（保持老读者能用），另加 `<key>_head`、
           `<key>_path`、`<key>_full_chars`、`<key>_note`
    """
    if not spilled.get("truncated"):
        result[key] = spilled.get("text", "")
        return result

    if spilled.get("spilled"):
        result[key] = spilled["tail"]
        result[f"{key}_head"] = spilled["head"]
        result[f"{key}_path"] = spilled["path"]
    else:
        result[key] = spilled.get("text", "")
    result[f"{key}_full_chars"] = spilled["full_chars"]
    result[f"{key}_note"] = spilled["note"]
    return result
