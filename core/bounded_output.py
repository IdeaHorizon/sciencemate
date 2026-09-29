"""工具结果的出口有界闸 —— 太大的结果落盘，进上下文的只是一个指针。

## 为什么在**咽喉**做，不在每个工具里做

"有界读"这条原则项目里反复得出过：KB 的 find/read 分离（单条 search_kb 83k
tokens 顶爆 context 之后定的）、`read_file` 的 `_MAX_READ_BYTES`。但它一直是
**逐个工具各实现一遍**的 —— 于是谁没实现谁就漏，而漏掉的那个没人会发现。

2026-08-22 在 632 个真实 checkpoint 上实测，漏了两个：

    read_artifact            8 条 > 20 万字符，最大 **1,865,703 字符**
    read_producer_transcript 2 条 > 20 万字符

那条 186 万字符是一张 `clean_results` 的 JSON 平表。按 3.2 字符/token 折算约
58 万 tokens —— **是默认上下文窗口（256k）的 2.3 倍**。它根本装不进去；那 8 个
撑爆窗口的 checkpoint 全部是被这一条撑爆的（不是"读了太多次"，是单条就超）。

工作集的「至多一份活副本」对它无效 —— 它本来就只有一份。压缩也救不了：压缩是
在 context 里腾地方，而这一条自己就比 context 大。**唯一的解是不让它进来。**

所以这道闸放在 `tool_registry.execute` 的唯一出口上：新工具默认被覆盖，工具作者
不需要记得这件事。

## 语义

- 超过 `MAX_RESULT_CHARS` → 完整结果**原样落盘**，返回一个带路径的指针。
- **`status` 原样继承**，本层不盖章（同 core/context_view 的宪法：框架只做
  逐字呈现或显式墓碑，不做有损转述占原件的位）。
- 顶层**标量字段全部保留** —— `total_matched: 0`、`passed: true` 这类往往就是
  答案本身，砍掉它们等于让这次调用白跑。
- 给出**可执行的下一步**：分段读 / 直接在文件上 grep、jq。对一张 186 万字符的
  平表，后者几乎总是比整份读回来更有用。

## 落盘失败也必须截断

写不进盘（无 run 目录、磁盘满）时**不放行原文** —— 宁可只给 preview 并如实说明
完整内容没能保存。放行一次就是一轮必然的上下文崩溃。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#: 工具结果进入上下文的字符上限。
#:
#: 取值比 `read_file` 的单次读上限（`_MAX_READ_BYTES = 256_000`）**大一档**：
#: read_file 满页返回时，正文 256k 再加 envelope 就在 26 万上下，不该被这道闸
#: 再截一次（它自己有 offset/limit 分页，已经是有界的）。300k 字符按 3.2 字符
#: /token 约合 94k tokens —— 仍然只占默认窗口的三分之一。
MAX_RESULT_CHARS = 300_000

#: 指针里带多少开头正文。够模型认出"这是什么、结构长什么样"，据此决定怎么筛。
PREVIEW_CHARS = 2_000

OVERSIZED_DIRNAME = "oversized"


def _dump(result: Any) -> str:
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return repr(result)


def _save(state: Any, tool_name: str, blob: str) -> Path | None:
    """把完整结果落盘。拿不到目录或写失败返回 None（调用方仍然截断）。"""
    base = getattr(state, "root", None)
    if base is None:
        tp = getattr(state, "transcript_path", None)
        base = Path(tp).parent if tp else None
    if base is None:
        return None
    try:
        d = Path(base) / OVERSIZED_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:12]
        p = d / f"{tool_name}__{digest}.json"
        if not p.exists():
            p.write_text(blob, encoding="utf-8")
        return p
    except OSError:
        return None


def bound(state: Any, tool_name: str, result: Any) -> Any:
    """结果过大 → 落盘并换成指针；否则原样返回。"""
    if not isinstance(result, dict):
        return result
    blob = _dump(result)
    if len(blob) <= MAX_RESULT_CHARS:
        return result

    path = _save(state, tool_name, blob)
    # 顶层标量往往就是答案本身（total_matched / passed / count …），一律留下。
    out: dict[str, Any] = {
        k: v for k, v in result.items()
        if v is None or isinstance(v, bool | int | float)
    }
    out["status"] = result.get("status", "success")   # 继承，不盖章
    out["oversized"] = True
    out["result_chars"] = len(blob)
    out["preview"] = blob[:PREVIEW_CHARS]

    if path is not None:
        out["saved_to"] = str(path)
        out["note"] = (
            f"⚠️ 这次 {tool_name} 的结果有 {len(blob):,} 字符，**放不进上下文**"
            f"（上限 {MAX_RESULT_CHARS:,}），所以没有整份给你。\n"
            f"完整内容原样存在：{path}\n"
            f"上面 `preview` 是它的开头 {PREVIEW_CHARS:,} 字符，够你看出结构。\n"
            f"接下来怎么拿你要的那部分：\n"
            f"  • 只要其中几行/几个字段 —— 用 run_bash 在文件上筛："
            f"`grep`、`jq`、`head`。**对大表这几乎总是比整份读回来更有用。**\n"
            f"  • 确实要通读 —— `read_file(path=\"{path}\", offset=N, limit=M)` 分段读。\n"
            f"别再原样重调这个工具：结果一样大，一样进不来。"
        )
    else:
        out["note"] = (
            f"⚠️ 这次 {tool_name} 的结果有 {len(blob):,} 字符，放不进上下文，"
            f"而且**完整内容也没能存盘**（拿不到 run 目录或写入失败）。\n"
            f"上面 `preview` 是开头 {PREVIEW_CHARS:,} 字符。\n"
            f"请换一个更窄的调用方式（加过滤条件 / 分页参数 / 只取需要的字段），"
            f"别原样重调。"
        )
    return out
