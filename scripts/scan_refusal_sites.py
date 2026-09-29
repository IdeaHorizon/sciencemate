#!/usr/bin/env python3
"""扫盘统计拒绝点 —— 判决拆除战役的防复发闸数据源。

按 **AST** 判，不是正则：docstring / 注释里写的「raise SchemaValidationError」
不是拒绝点（kb_schema:448/855 曾被正则误计），而 `return "", [], {"status":
"error", …}` 这种 tuple 形态与 `return _error(...)` 助手形态是拒绝点却被正则漏掉。

拒绝点的两种到达形态：

  · `return` 节点：返回值是一个 dict 字面量，或 tuple / list 里含 dict 字面量，
    且该 dict 有键 `"status"`、值 ∈ {error, blocked, recoverable_blocked, needs_*}；
    或返回值是对拒绝信封工厂的一次调用 —— 名字为 `_error`，或以 `_err` /
    `_fail` / `_reject` 起头（memory_tools 的 `_err(`、latex 的 `_error(`）。
  · `raise` 节点：抛的是契约类异常（Call 或 Name），名单见 CONTRACT_EXCEPTIONS
    —— 这些类在派发口被收成 REJECTED / error 信封送到模型眼前。

明确**不计**：ValueError / RuntimeError / TypeError 等内置异常。它们是内部契约
（调用方是代码，不是模型），派发口把它们记成「工具崩了」而不是「工具说不」；
计进来会把「函数入参类型错」和「拒绝模型」混成一个数。

按文件计数输出 JSON。用法：
    .venv/bin/python scripts/scan_refusal_sites.py            # 打印
    .venv/bin/python scripts/scan_refusal_sites.py --write    # 收紧基线（**只降不升**）

## `--write` 只能降（#912）

基线是**一次性的历史欠账水位**，闸放行的额度是 `基线 + registry 条目数`。
`--write` 曾经是整份重生 —— 把所有文件的基线都抬到实测值，**包括那些"涨了、
但已被 registry 条目顶掉"的文件**。基线一抬，那些条目就从"已经付过账"变回
"还没花的额度"，同一批声明被重复计一次。

实测（#909 当时）：跑一次 `--write`，6 个文件凭空多出 **16 处**可以静默新增的
拒绝点 —— 而"静默新增拒绝点"正是这道闸存在的唯一理由。**闸自己在失败信息里
开的那条药方，会把闸的另一半松掉。**

所以：删墙 → 基线跟着降（这是 `--write` 的全部用途）；加墙 → 基线纹丝不动，
去 registry 逐条声明。新出现的文件也**不写进基线** —— 写进去就等于发一份
不用声明的额度。

局限（如实）：
  · 新造一个不在名单里的异常类可以绕过 raise 半边；
  · 先 `result = {"status": "error", …}` 再 `return result` 的两步写法、
    `dict(status="error")` 调用形态、以及在 except 里被替换成 error 信封的
    结果（`result = {...}` 赋值）都不在 return 半边视野内；
  · 助手按名字前缀认，起别的名字就漏；
  · 解析不了的文件（语法错）计 0 并在 stderr 报一行 —— 基线会因此「缩水」，
    棘轮测试转红，不会静默。
这是棘轮不是围墙 —— 它把「新增一堵墙」从静默变成必须留一行分类声明
（A 安全资源 / B 记录完整性 / C 物理协议 / collector）。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("core", "shared", "nodes")
BASELINE = ROOT / "docs" / "verdict_demolition" / "refusal_baseline.json"

#: raise 半边：到达模型的契约类异常。内置异常（ValueError / RuntimeError …）
#: 不在此列 —— 见模块 docstring「明确不计」。
CONTRACT_EXCEPTIONS = frozenset({
    "VisualContractError",
    "SchemaValidationError",
    "StateContractError",
    "AcquisitionError",
    "InstallError",
    "SandboxContractError",
    "PublicationFigureError",
    "VisualTruncationError",
    "ProjectWorkspaceError",
    "OfferContractError",
    "ModelRoleError",
    "TaskListError",
    "ToolRejection",
})

_REFUSAL_STATUSES = frozenset({"error", "blocked", "recoverable_blocked"})
_HELPER_EXACT = frozenset({"_error"})
_HELPER_PREFIXES = ("_err", "_fail", "_reject")


def _is_refusal_status(value: ast.expr) -> bool:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value in _REFUSAL_STATUSES or value.value.startswith("needs_")
    if isinstance(value, ast.IfExp):
        # `"needs_x" if cond else "error"`：两个分支都是拒绝 → 一处拒绝点
        return _is_refusal_status(value.body) or _is_refusal_status(value.orelse)
    return False


def _dict_is_refusal(node: ast.expr) -> bool:
    if not isinstance(node, ast.Dict):
        return False
    for key, value in zip(node.keys, node.values):
        if (isinstance(key, ast.Constant) and key.value == "status"
                and _is_refusal_status(value)):
            return True
    return False


def _callee_name(node: ast.expr) -> str | None:
    """`f(...)` / `mod.f(...)` / `f` / `mod.f` → 末段名字。"""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_refusal_helper(name: str | None) -> bool:
    return name is not None and (name in _HELPER_EXACT or name.startswith(_HELPER_PREFIXES))


def _return_is_refusal(node: ast.Return) -> bool:
    value = node.value
    if value is None:
        return False
    if _dict_is_refusal(value):
        return True
    if isinstance(value, ast.Tuple | ast.List):
        return any(_dict_is_refusal(elt) for elt in value.elts)
    return isinstance(value, ast.Call) and _is_refusal_helper(_callee_name(value))


def _raise_is_refusal(node: ast.Raise) -> bool:
    if node.exc is None:
        return False
    return _callee_name(node.exc) in CONTRACT_EXCEPTIONS


def count_refusal_sites(source: str) -> int:
    """一份源码里的拒绝点数。语法错交给调用方决定怎么报。"""
    tree = ast.parse(source)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and _return_is_refusal(node):
            n += 1
        elif isinstance(node, ast.Raise) and _raise_is_refusal(node):
            n += 1
    return n


def scan_file(path: Path) -> int:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    try:
        return count_refusal_sites(source)
    except SyntaxError as exc:
        print(f"scan_refusal_sites: cannot parse {path}: {exc}", file=sys.stderr)
        return 0


def scan() -> dict[str, int]:
    out: dict[str, int] = {}
    for d in SCAN_DIRS:
        for f in sorted((ROOT / d).rglob("*.py")):
            rel = str(f.relative_to(ROOT))
            if "/tests/" in rel or rel.startswith("tests/"):
                continue
            n = scan_file(f)
            if n:
                out[rel] = n
    return out


def tightened(baseline: dict[str, int], counts: dict[str, int]) -> dict[str, int]:
    """收紧后的基线：逐文件取 `min(现基线, 实测)`，实测为 0 的整条删掉。

    **不升、不新增**。理由见模块 docstring：抬基线 = 把已经付过账的 registry 条目
    退回成没花的额度。
    """
    out: dict[str, int] = {}
    for path, was in baseline.items():
        now = min(was, counts.get(path, 0))
        if now:
            out[path] = now
    return out


def grew(baseline: dict[str, int], counts: dict[str, int]) -> dict[str, tuple[int, int]]:
    """涨了的文件 —— `--write` 不碰它们，打印出来让人去 registry 声明。"""
    return {p: (n, baseline.get(p, 0)) for p, n in counts.items() if n > baseline.get(p, 0)}


if __name__ == "__main__":
    counts = scan()
    if "--write" in sys.argv:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
        new_baseline = tightened(baseline, counts)
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(new_baseline, indent=1, sort_keys=True,
                                       ensure_ascii=False) + "\n", encoding="utf-8")
        lowered = {p: (baseline[p], new_baseline.get(p, 0))
                   for p in baseline if new_baseline.get(p, 0) < baseline[p]}
        print(f"baseline tightened: {sum(new_baseline.values())} sites in "
              f"{len(new_baseline)} files（降了 {len(lowered)} 个）")
        for path, (was, now) in sorted(lowered.items()):
            print(f"  ↓ {path}: {was} → {now}")
        growth = grew(baseline, counts)
        if growth:
            print("\n以下文件涨了 —— 基线**没有**跟着抬（那会把已声明的条目退回成额度）。"
                  "\n逐条去 docs/verdict_demolition/refusal_registry.yaml 声明 "
                  "class ∈ {A,B,C,collector}：", file=sys.stderr)
            for path, (now, was) in sorted(growth.items()):
                print(f"  ↑ {path}: {was} → {now}", file=sys.stderr)
    else:
        print(json.dumps(counts, indent=1, sort_keys=True, ensure_ascii=False))
