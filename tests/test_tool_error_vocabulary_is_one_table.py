"""失败分类的词表只有一份 —— 上游加一个 code，下游必须跟上。

`core/tool_errors.py` 盖章，`tool-presentation.ts` 按章路由。两处各存一份名单
就是两个真相源：**重叠的部分永远看不出错，差集永远不报错**。上游新加一个 code、
前端没跟上，那个 code 会静默落进兜底分支——既不 Declined、也没有专门文案，
而且没有任何一层会抱怨。

这次就差点这样：`toolchain_missing` 加在 Python 侧，前端不认它就还是那句
"Build PDF did not complete"，等于白改。所以把对齐做成机械闸。
"""
from __future__ import annotations

import re
from pathlib import Path

from core import tool_errors as _errs

_TS = (Path(__file__).resolve().parents[1] / "platform" / "frontend" / "src"
       / "features" / "execution" / "lib" / "tool-presentation.ts")


def _ts_source() -> str:
    assert _TS.is_file(), f"显示层不在预期位置：{_TS}（挪了文件要同步改这道闸）"
    return _TS.read_text(encoding="utf-8")


def test_loop_level_sets_match_exactly() -> None:
    block = re.search(r"const LOOP_LEVEL_ERROR_CODES = new Set\(\[(.*?)\]\)",
                      _ts_source(), re.S)
    assert block, "前端的 LOOP_LEVEL_ERROR_CODES 不见了或换了写法"
    front = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert front == set(_errs.LOOP_LEVEL_CODES), (
        "循环级词表分叉了。差集里的 code 会静默走错分支：\n"
        f"  只在 Python：{sorted(set(_errs.LOOP_LEVEL_CODES) - front)}\n"
        f"  只在前端：  {sorted(front - set(_errs.LOOP_LEVEL_CODES))}"
    )


def test_every_code_is_known_to_the_display_layer() -> None:
    """每个 code 要么在循环级名单里，要么有一条自己的文案分支。

    判据落在"下游认不认得它"上，而不是"名字有没有出现过" —— 前者才是这道闸
    真正要防的事。
    """
    source = _ts_source()
    unknown = [
        code for code in sorted(_errs.ALL_CODES)
        if code not in _errs.LOOP_LEVEL_CODES and f'"{code}"' not in source
    ]
    assert not unknown, (
        f"这些 code 显示层一个字都不认识，会落进兜底文案：{unknown}。"
        "在 tool-presentation.ts 里给它们各写一条标题 + 下一步。"
    )


def test_toolchain_missing_is_not_swallowed_as_a_loop_step() -> None:
    """回归钉：这一类模型自己绕不过去，摆到人面前才有人去装 TeX。"""
    assert _errs.TOOLCHAIN_MISSING in _errs.ALL_CODES
    assert _errs.TOOLCHAIN_MISSING not in _errs.LOOP_LEVEL_CODES
