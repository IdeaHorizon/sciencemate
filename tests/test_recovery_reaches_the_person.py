"""工具写的"下一步该干什么"，必须真的到得了人面前。

## 读端有、写端零

`payload-view.ts` 一直在读 `error.recovery`，`tool-presentation.ts` 一直在用
`safeRecovery(error?.recovery, 兜底)`。中间**没有任何一层写过这个字段** ——
于是每一次失败都走兜底，而兜底是按"这个工具大概在干什么"猜的：compile_latex
属于 pdf 类，兜底句是 "Review the document source, then ask the agent to rebuild
the PDF."。2026-09-09 那次缺 latexmk，界面照这句话说了三遍，源码一个字都没错。

这不是文案问题，是**一条没有写者的读路径**：读得再对，也永远读到 undefined。

本文件守 harness 这一侧（截断）与**跨进程的字段名对齐**；ingest 那一跳的真实
行为在 `platform/backend/tests/test_tool_failure_carries_the_next_step.py`
（backend 不在 harness 的 venv 里，在这边只能 skip，而 skip 不是红色）。
"""
from __future__ import annotations

import json
from pathlib import Path

from core import tool_errors as _errs

_ROOT = Path(__file__).resolve().parents[1]


def _failure(**extra) -> dict:
    return {
        "status": "error",
        "error_code": _errs.TOOLCHAIN_MISSING,
        "error": "这台机器上没有 LaTeX 工具链",
        "recovery": "在跑 harness 的机器上装 texlive，或把 tectonic 放进 PATH。",
        **extra,
    }


def test_truncation_keeps_the_next_step() -> None:
    """大结果被截断时，recovery 不能跟着 body 一起被截走。

    编译失败的返回值天然很大（stdout_tail 3000 字 + 版式审计），**必然**走截断
    这条路 —— 只在小结果上验等于没验。
    """
    from core.agent_loop import _brief

    big = _failure(stdout_tail="x" * 4000, layout_audit={"passed": False})
    assert len(json.dumps(big)) > 500, "样本没超过截断阈值，这条测试等于没跑"

    kept = _brief(big)
    assert isinstance(kept, dict), "失败结果被压成字符串了（envelope 必须保形）"
    assert kept["recovery"] == big["recovery"], "recovery 被截进了 _body_truncated"
    assert kept["error_code"] == _errs.TOOLCHAIN_MISSING


def test_the_wire_field_names_line_up_across_the_process_boundary() -> None:
    """backend 是独立进程、不 import harness 包，这条链上三处各写一次字面量：

        工具返回 `recovery`  →  事件写 `errorRecovery`  →  前端读 `payload.errorRecovery`

    名字对不上**不会报错**，只会静默退回兜底文案 —— 正是这次要修的病。所以把
    "各写一次"钉成"必须相等"。
    """
    ingest = (_ROOT / "platform/backend/app/services/execution_ingest.py"
              ).read_text(encoding="utf-8")
    view = (_ROOT / "platform/frontend/src/features/execution/lib/payload-view.ts"
            ).read_text(encoding="utf-8")

    assert '"errorRecovery"' in ingest, "事件里没人写 recovery —— 界面只能猜"
    assert 'result.get("recovery")' in ingest, "ingest 没从工具返回值里取 recovery"
    assert "payload.errorRecovery" in view, "扁平失败事件里的 recovery 没人读"
    assert "recovery" in _ROOT.joinpath(
        "core/agent_loop.py").read_text(encoding="utf-8").split(
        "_ENVELOPE_KEYS = ")[1].split("\n")[0], \
        "recovery 不在 envelope 白名单里 —— 一超 500 字节就被截进 _body_truncated"
