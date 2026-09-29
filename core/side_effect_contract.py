"""用户说了"只读"，收尾时要拿真实事件对一遍（#973）。

## 现场

用户明说「不要修改文件」。这次 run 实际写了 **24 个** Project 文件。而最终回复
和独立审稿**双双称「全程只读」**，审稿还给了 0.92 通过。

机制其实早就在场：`core.project_workspace.observe_after_tool` 每次工具改了
Project 文件都会产一条 `workspace_changed` 事件，`shared/tools/run_node.py`
也在消费它。缺的不是观测，是**没有任何地方把观测和用户那句承诺放在一起比**。

## 分类口径（2026-09-15 wangd 定）

**只算用户带进来的文件和源码。**框架自己的记录、证据、运行产物不算。

判据就是现成的 `.research/` 前缀 —— `project_workspace` 已经按它算
`internal_only`，这里复用同一份判定，不另起一套（一个问题一个真相源）。

不选"任何字节变化都算"：每次回复都要报一长串，真正该看见的那条会被淹掉。
不选"强制只读挂载"：很多正常任务会被误伤，而且它把"说错话"变成"做不成事"。

## 这一层做什么

* :func:`declare_read_only` —— 把用户那句承诺记下来（谁说的、原话）。
* :func:`reconcile` —— 拿本次 run 的 `workspace_changed` 事件对一遍，
  返回一份**事实**：承诺是什么、实际改了哪些属于用户的文件、有没有矛盾。

**返回事实，不下判决。**矛盾了要不要拦、怎么跟用户说，是调用方的事；
这里只保证「说的和做的」被放在了一起，而且**不可能悄悄对不上**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.project_workspace import _is_internal_bookkeeping

#: state 上挂承诺的属性名。只在内存里 —— 承诺属于这一次对话。
_ATTR = "_side_effect_promise"


@dataclass(frozen=True)
class ReadOnlyPromise:
    """用户说过的那句"别改文件"。"""

    said_by: str = "user"
    quote: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"said_by": self.said_by, "quote": self.quote}


@dataclass
class Reconciliation:
    """说的 vs 做的。**事实，不是判决。**"""

    promised_read_only: bool
    promise: dict[str, Any] | None
    user_paths: list[str] = field(default_factory=list)
    internal_paths: list[str] = field(default_factory=list)
    contradiction: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "promised_read_only": self.promised_read_only,
            "promise": self.promise,
            "user_paths": list(self.user_paths),
            "internal_paths": list(self.internal_paths),
            "user_files_changed": len(self.user_paths),
            "internal_files_changed": len(self.internal_paths),
            "contradiction": self.contradiction,
        }

    def sentence(self) -> str:
        """一句给人和模型读的话 —— 收尾陈述必须照抄这句，不许自己复述。"""
        if not self.promised_read_only:
            return ""
        if not self.contradiction:
            return (
                f"用户要求只读，本次确实没有改动用户的文件"
                f"（框架记账 {len(self.internal_paths)} 个文件不计）。"
            )
        shown = ", ".join(self.user_paths[:5])
        more = "" if len(self.user_paths) <= 5 else f" 等 {len(self.user_paths)} 个"
        return (
            f"⚠️ 用户要求只读，但本次改动了 {len(self.user_paths)} 个用户文件："
            f"{shown}{more}。这与最终陈述里的「未修改文件」直接冲突。"
        )


def declare_read_only(state: Any, quote: str = "", said_by: str = "user") -> None:
    """记下用户要求只读。重复声明不叠加，后一次覆盖。"""
    try:
        setattr(state, _ATTR, ReadOnlyPromise(said_by=said_by, quote=str(quote or "")))
    except Exception:
        pass


def promised_read_only(state: Any) -> ReadOnlyPromise | None:
    value = getattr(state, _ATTR, None)
    return value if isinstance(value, ReadOnlyPromise) else None


def _changed_paths(events: list[dict[str, Any]] | None) -> list[str]:
    """从 transcript 事件里取出所有被改过的 Project 路径，按首次出现去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if (event.get("event") or event.get("type")) != "workspace_changed":
            continue
        for path in event.get("paths") or []:
            text = str(path or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def reconcile(state: Any, events: list[dict[str, Any]] | None = None) -> Reconciliation:
    """把承诺和真实改动放在一起。

    ``events`` 不给就从 ``state.transcript`` 读。**读不到事件不等于没改过** ——
    那种情况下 ``user_paths`` 为空但也没有承诺可对，函数照样如实返回。
    """
    promise = promised_read_only(state)
    if events is None:
        events = list(getattr(state, "transcript", None) or [])
    changed = _changed_paths(events)
    user_paths = [p for p in changed if not _is_internal_bookkeeping(p)]
    internal_paths = [p for p in changed if _is_internal_bookkeeping(p)]
    return Reconciliation(
        promised_read_only=promise is not None,
        promise=promise.as_dict() if promise else None,
        user_paths=user_paths,
        internal_paths=internal_paths,
        contradiction=bool(promise is not None and user_paths),
    )
