"""`hash_files` —— 文件的 sha256 与字节数，由框架算给模型。

## 为什么要有这个工具

冻结一份 raw_results manifest 要求逐文件 `sha256` + `bytes`。**声明这两个数不需要
任何科学判断**，可模型手里没有算它们的路：只能 `safe_run_bash` 去 shell 出一趟。

2026-09-07 真机（二维 Ising，100 个 npz）：模型三次尝试用 `safe_run_bash` 算这
100 个文件的 sha256，三次都被 route gate 拒（同一动作同一原因达上限），于是
**手被彻底捆住** —— 它既不能算，又不肯编（"contract 要求真实 64-hex，伪造哈希会
违反 freeze contract"），最后只能诚实地交一份空 `source_hashes` 并 report_blocker。
一趟本已算完的科研，卡在"我够不到一个纯机械的数"上。

冻结时框架**本来就要**逐字节重算这批摘要来核对（`_validate_raw_results_manifest`
的 `verify_files`）。手里握着正确答案却只在对不上时才报出来，等于逼模型另找一条
算 hash 的路 —— 而那条路上有一道为别的目的设的闸。

> 那道闸本身没错：它管的是"这一轮的计算按声明的路线跑"。算摘要不是计算实验，
> 是记账。**机械可判的归框架，语义判断的归模型** —— `role` / `retention` 这类
> "这个文件算不算必留" 才是模型要答的，摘要不是。

## 边界

只读，只出摘要与字节数，**永不出内容**。路径必须落在本项目工作区或本 run 根下
（v2.1：读跨节点合法、写不跨节点）。越界一律拒，并说清越到哪去了。

## 出口有界

一次最多 `_MAX_FILES` 个。超了直接拒并说明怎么分批 —— 不静默截断：一份被截断的
摘要表会让下游覆盖检查报"漏了几个文件"，而真因是这里悄悄少给了。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from core import paths as _paths
from core.state import State
from core.tool_registry import ToolDefinition, register_tool

#: 一次调用最多算多少个文件。100 个 npz 是真实规模；留够余量，但不给无界。
_MAX_FILES = 512

#: 分块读，别把一个大文件整个吃进内存。
_BLOCK = 1024 * 1024


def _roots(state: State) -> list[Path]:
    """允许读的根。工作区（跨节点读合法）+ run 根（作业产物落这儿）。"""
    out: list[Path] = []
    for value in (getattr(state, "project_worktree", None), getattr(state, "root", None)):
        if not value:
            continue
        try:
            out.append(Path(value).resolve())
        except OSError:
            continue
    return out


def _inside(candidate: Path, roots: list[Path]) -> bool:
    return any(candidate == r or r in candidate.parents for r in roots)


async def _hash_files(state: State, paths: Any = None, **_: Any) -> dict:
    if not isinstance(paths, list) or not paths:
        return {"status": "error",
                "error": "paths 必须是非空的文件路径列表（相对本 run 或绝对都行）。"}
    if len(paths) > _MAX_FILES:
        return {"status": "error",
                "error": (f"一次最多 {_MAX_FILES} 个文件，收到 {len(paths)} 个。"
                          f"分批调用，把每批的结果合起来 —— 不要减少要声明的文件数。")}

    roots = _roots(state)
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"{raw!r} 不是路径字符串")
            continue
        p = _paths.resolve_display_relpath(state, raw.strip())
        try:
            resolved = p.resolve()
        except OSError as exc:
            errors.append(f"{raw}：解析失败（{type(exc).__name__}）")
            continue
        if roots and not _inside(resolved, roots):
            errors.append(
                f"{raw}：解析到 {resolved}，不在本项目工作区或 run 根下 —— "
                f"这个工具只读本流水线自己的文件")
            continue
        if not resolved.is_file():
            errors.append(f"{raw}：不是可读的普通文件（{resolved}）")
            continue
        digest = hashlib.sha256()
        try:
            with resolved.open("rb") as handle:
                for block in iter(lambda: handle.read(_BLOCK), b""):
                    digest.update(block)
            size = resolved.stat().st_size
        except OSError as exc:
            errors.append(f"{raw}：读不了（{type(exc).__name__}）")
            continue
        files.append({"path": str(resolved),
                      "sha256": digest.hexdigest(),
                      "bytes": size})

    if not files:
        return {"status": "error",
                "error": "一个文件都没算成：" + "；".join(errors[:10])}
    out: dict[str, Any] = {"status": "ok", "count": len(files), "files": files}
    if errors:
        # 半成功要说出来。悄悄少给几行，下游覆盖检查会报"漏文件"，真因在这儿。
        out["skipped"] = errors[:20]
        out["note"] = (f"{len(errors)} 个没算成（见 skipped）。声明 manifest 前先把它们"
                       f"解决掉 —— 少一行摘要，冻结时的覆盖检查就会拒。")
    return out


register_tool(
    ToolDefinition(
        name="hash_files",
        description=(
            "算一批文件的 sha256 与字节数（框架直接读盘算，不起 shell、不占执行路线）。\n"
            "用途：raw_results / replay_manifest 这类要求逐文件 `sha256` + `bytes` 的"
            "清单，摘要由这里取，**不要自己 shell 出去算**（那会撞执行路线闸）。\n"
            "返回 `files: [{path, sha256, bytes}]`，可直接填进 manifest。\n"
            "`role` / `retention` 这类判断不在这里 —— 那是你要答的。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (f"文件路径列表（相对本 run 或绝对）。一次最多 "
                                    f"{_MAX_FILES} 个；更多就分批。"),
                },
            },
            "required": ["paths"],
        },
        risk_level="low",
    ),
    _hash_files,
)
