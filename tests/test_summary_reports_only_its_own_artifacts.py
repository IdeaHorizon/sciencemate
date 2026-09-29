"""run summary 里的 artifacts 是**归属**，不是可见范围。

`State.list_artifacts` 的 docstring 就是契约：

    「本 run 产出了什么」是**归属**问题 —— executor 的 produced_types、summary、
    交付判定都必须用 own_only，否则上游节点的产物会被算成本 run 的产出
    （完成度门禁被别人的劳动喂饱，是"自己给自己发通行证"的镜像变体）。

    返回顺序是契约的一部分：按 created_at 升序，**末位 = 最新**。

2026-09-01 本机 E2E 实拍，两条各自独立的违约叠在一起：

  1. core/executor.py 有 5 处构造 run summary，**只有 1 处**传了 own_only=True
     （而那一处的注释正好写着"summary 只报自己的"）。其余 4 处 —— 包括
     正常完成路径 —— 把整个项目的产物报成本 run 的产出。
     可见后果：writing 节点的决策包「📦 Produced」列出 literature_index、
     hypothesis_conclusion_audit、raw_results__q2… 共 95+ 份，还写着
     "另有 85 个产物未列出"。

  2. run_node 从 reviewer 子 run 的产出里挑 critique 用的是 `[0]` —— 按契约
     那是**最老**那份。reviewer 09:56 刚写出新的 critique，账本记下的却是
     8-30 那份基线件。

任一条单独存在都会挑到陈年 critique；两条叠加则必然。后果是
decision_package 的「账本权威」路径彻底空转（账本与传参被同一个错误来源
毒化成一致），只剩「critique 早于本 run 开跑」那道兜底闸在连拦三轮。

这道闸**扫盘**：按 AST 找 summary 字面量里的 `"artifacts": <call>`，
把合法那条路命名出来（`state.list_artifacts(own_only=True)`），其余一律违规。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXECUTOR = REPO / "core" / "executor.py"


def _artifact_values_in_dict_literals(tree: ast.AST) -> list[tuple[int, ast.expr]]:
    """所有字典字面量里键为 "artifacts" 的取值表达式（带行号）。"""
    found: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "artifacts":
                found.append((getattr(value, "lineno", node.lineno), value))
    return found


def _is_own_only_call(value: ast.expr) -> bool:
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if not (isinstance(func, ast.Attribute) and func.attr == "list_artifacts"):
        return False
    return any(kw.arg == "own_only"
               and isinstance(kw.value, ast.Constant) and kw.value.value is True
               for kw in value.keywords)


def test_every_summary_artifacts_list_is_own_only() -> None:
    tree = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    entries = _artifact_values_in_dict_literals(tree)
    # 防空转：闸必须真扫到东西
    assert len(entries) >= 5, f"只扫到 {len(entries)} 处 \"artifacts\" 键，闸可能没在扫真东西"

    offenders = []
    for lineno, value in entries:
        if isinstance(value, ast.List) and not value.elts:
            continue                      # 空列表字面量：没有归属问题
        if not _is_own_only_call(value):
            offenders.append(f"core/executor.py:{lineno}")
    assert not offenders, (
        "run summary 的 artifacts 必须是**本 run 自己的产出**：\n  "
        + "\n  ".join(offenders)
        + "\n合法写法只有 state.list_artifacts(own_only=True)（见 State.list_artifacts 契约）。"
          "不传 own_only 会把整个项目的产物报成本 run 的产出。")


def test_the_latest_critique_is_taken_from_the_end_not_the_front() -> None:
    """list_artifacts 契约：末位 = 最新。挑"这次审查刚写的"必须用 [-1]。"""
    src = (REPO / "shared" / "tools" / "run_node.py").read_text(encoding="utf-8")
    assert "review_artifacts[-1]" in src, "取 critique 没用末位（[-1]）"
    assert "review_artifacts[0]" not in src, (
        "review_artifacts[0] 取的是**最老**那份 —— 按 list_artifacts 契约，"
        "升序排列下 [0] 是最早的，[-1] 才是最新的")
