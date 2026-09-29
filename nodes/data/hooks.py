"""data 节点 loop hooks。

`structure_file_validation`（issue #166 第 2 条）
------------------------------------------------
背景：e2e 实测 data 交付的 POSCAR 格式错误、下游无法读取，却一路畅通到
reviewer —— reviewer 只能看 artifact preview，格式错误肉眼看不出来。

诊断出的架构问题不是"缺校验器"：`tools/atomic_structure_recovery.py`
里的 `_inspect_structure_file()` 已经是个像样的校验器（species 行、原子
数、行数 ≥ 8+sum(counts)、元素符号合法性、CIF atom-site loop）。问题是它
**只挂在 recover_atomic_structure 恢复路径上，不是交付物的门禁**；走其它
生成路径产出的结构文件一次都不过它。加上 data 节点原本 `quality_checks: []`，
坏文件没有任何一道机械关卡。

这跟 high_risk_command_audit 的老毛病是同一个模式：机械答案已经算得出来，
判定层却不用它。修法也一样 —— run 结束机械扫一遍本 run 产出的结构文件，
把确定结论写成 transcript 事件，QC 直接读事件做机械判定（pass/fail 双向
权威，不进 judge），reviewer 再拿它当红线。

判断权仍在人/reviewer：本 hook 只负责**让证据一定被看见**，不做语义裁决。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.loop_hooks import HookContext, LoopHook, register_loop_hook

log = logging.getLogger("data.hooks")

# 每个 run 最多校验多少个结构文件（防某些 package 生成上千帧轨迹时拖慢 run_end）
_MAX_FILES_SCANNED = 200


def _iter_candidate_structure_files(state) -> list[Path]:
    """本 run 目录下所有结构文件候选。

    只扫 state.root（run 自己的目录树）—— 上游转发进来的输入不该由本节点
    背锅，而 data 的正式产物（preprocessing package）就落在 run 目录下。
    """
    from .tools.atomic_structure_recovery import _STRUCTURE_SUFFIXES

    root = getattr(state, "root", None)
    if root is None or not Path(root).exists():
        return []
    found: list[Path] = []
    for path in sorted(Path(root).rglob("*")):
        if len(found) >= _MAX_FILES_SCANNED:
            break
        if not path.is_file():
            continue
        name = path.name.lower()
        # POSCAR / CONTCAR 是无后缀的 VASP 约定文件名，必须按文件名匹配
        if path.suffix.lower() in _STRUCTURE_SUFFIXES or name in (
            "poscar", "contcar",
        ):
            found.append(path)
    return found


def validate_structure_outputs(state) -> dict[str, Any]:
    """机械校验本 run 产出的结构文件，写 transcript 事件并返回统计。

    幂等：重复调用只是重算一遍并再写一条事件（QC 读最新一条）。data 节点
    的 custom loop 有多条早退路径，靠 run_loop 的 on_end 覆盖不全，所以
    data_agent_loop.run_loop 也会在 finally 里兜底调一次。
    """
    try:
        from .tools.atomic_structure_recovery import _inspect_structure_file
    except Exception as e:                                    # noqa: BLE001
        log.warning("结构校验器不可用，跳过（不阻断 run）：%s", e)
        return {"n_scanned": 0, "n_invalid": 0, "skipped": True}

    files = _iter_candidate_structure_files(state)
    invalid: list[dict[str, Any]] = []
    scanned = 0
    for path in files:
        try:
            result = _inspect_structure_file(path, None)
        except Exception as e:                                # noqa: BLE001
            # 连读都读不了 = 确定不可用，按 invalid 记（这正是 issue 报的症状）
            invalid.append({"path": str(path), "error": f"{type(e).__name__}: {e}"})
            scanned += 1
            continue
        scanned += 1
        if not result.get("valid"):
            invalid.append({
                "path": str(path),
                "format": result.get("format"),
                "error": result.get("error") or "结构文件未通过格式校验",
                "atom_count": result.get("atom_count"),
                "unknown_species": result.get("unknown_species") or [],
            })

    payload = {
        "n_scanned": scanned,
        "n_invalid": len(invalid),
        "invalid_files": invalid[:10],
    }
    try:
        state.hook_state["_structure_validation_done"] = True
        state.append_transcript("structure_file_validation", **payload)
        if invalid:
            state.save_memory(
                kind="observation",
                text=(
                    f"structure_file_validation: {len(invalid)}/{scanned} 个结构文件"
                    f"未通过格式校验 —— "
                    + "; ".join(
                        f"{Path(i['path']).name}: {i.get('error')}" for i in invalid[:5]
                    )
                ),
                tags=["structure_validation", "qc_evidence"],
            )
    except Exception as e:                                    # noqa: BLE001
        log.warning("structure_file_validation 写入失败：%s", e)
    return payload


def _structure_validation_on_end(ctx: HookContext, loop_result: Any) -> None:
    validate_structure_outputs(ctx.state)


structure_file_validation = LoopHook(
    name="structure_file_validation",
    description=(
        "run 结束机械校验本 run 产出的结构文件（POSCAR/CONTCAR/CIF/xyz/vasp），"
        "复用 tools/atomic_structure_recovery.py 的 _inspect_structure_file，"
        "结论写 `structure_file_validation` 事件供 QC 机械判定 + reviewer 红线。"
    ),
    on_end=_structure_validation_on_end,
    emits=("structure_file_validation",),
)
register_loop_hook(structure_file_validation)
