"""后端发出的每一种事件，前端的名单都得认识 —— 词表只在一处长，抄件要被逼着跟上。

2026-09-09 node20：后端加了 `autonomy.downgraded`，前端 `EXECUTION_EVENT_KINDS` 没跟上。
前端解析器对它抛错 → 事件流从同一游标反复重开 → 每次漏一条连接 → 六条占满 → 页签
失聪。解析器现在不再因未知种类报废整条流（那是失败模式的修法）；这条闸管的是另一半：
名单必须包含后端会发的每一种，界面才会按正确的受众和形状画它。
"""
from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BACKEND_APP = _ROOT / "platform" / "backend" / "app"
_FRONTEND_KINDS = (
    _ROOT / "platform" / "frontend" / "src" / "features" / "execution" / "lib" / "execution-event.ts"
)



def _string_values(node: ast.AST) -> set[str]:
    """一个表达式里所有的字符串常量（`"a"` / `"a" if x else "b"`）。"""
    return {
        item.value for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _backend_kinds() -> set[str]:
    """后端会送到事件流里的种类：`EventDraft("…")` 的第一个参数，以及
    `record_app_event(kind=…)` 的 kind。命令的 kind（`create_command(kind=…)`）不是事件。"""
    kinds: set[str] = set()
    for source in _BACKEND_APP.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "EventDraft" and node.args:
                kinds |= _string_values(node.args[0])
            elif name == "record_app_event":
                for keyword in node.keywords:
                    if keyword.arg == "kind":
                        kinds |= _string_values(keyword.value)
    return {kind for kind in kinds if re.fullmatch(r"[a-z_]+\.[a-z_]+", kind)}


def _frontend_kinds() -> set[str]:
    text = _FRONTEND_KINDS.read_text(encoding="utf-8")
    block = text[text.index("EXECUTION_EVENT_KINDS = ["): text.index("] as const")]
    return set(re.findall(r'"([a-z_]+\.[a-z_]+)"', block))


def test_every_backend_event_kind_is_in_the_frontend_vocabulary() -> None:
    backend = _backend_kinds()
    frontend = _frontend_kinds()
    assert {"run.paused", "decision.required", "autonomy.downgraded"} <= backend, backend
    missing = sorted(backend - frontend)
    assert not missing, (
        "后端会发、前端名单里没有的事件种类：" + ", ".join(missing)
        + " → 加进 platform/frontend/.../execution-event.ts 的 EXECUTION_EVENT_KINDS"
        "（以及 event-audience.ts 的受众表）。"
    )
