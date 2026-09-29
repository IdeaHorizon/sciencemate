"""验证工具的注册表：**谁有资格在验证章上署名**。

## 为什么这件事必须现算，不能手写名单

2026-08-23 查出来的事故：`TRUSTED_VERIFIERS` 是一份硬编码的元组，里面有
`check_lean` —— 而这个工具**从来没有实现过**，节点工具表里也没有它。
`KNOWN_METHODS` 里的 `interval` 与 `formal` 同样是空位子。

后果不是"少了个功能"，是**门上多了个后门**：`_audit_steps` 先看
`verification.tool` 在不在白名单、再拿 probe 去账本反查。模型手写
`{tool: "check_lean", status: "verified", probe: "随便"}`，第一段**会放行**；
唯一拦住它的是账本反查，而账本读不到时那段整个跳过（那是刻意的降级：
取证手段失灵不等于伪造）。于是白名单里一个永远不会写账本的名字，
就成了一条只在降级路径上生效的近路。

这是「护栏要扫盘，不要写名单」的反向形态：手写名单的经典毛病是**新东西默认
漏过**，这里是**名单里的东西根本不存在**。两个方向的病根同一个 ——
名单是人手维护的一份抄件，而它描述的事实（哪些工具真的会验证并落账）
在别处演化。

## 判据

有资格署名 ⟺ **这个工具真的会往验证账本里写一条**。

所以注册这件事由验证工具自己做（import 时调 `register_verifier`），
白名单从注册表现算。没实现的工具不会注册，它的名字因此**不在**白名单里 ——
模型手写它会被当场拒掉，而不是溜过第一道判据。

## 能力缺席就不给工具

`interval`（需要 python-flint）和 `formal`（需要 Lean 工具链）都依赖外部
能力。能力不在时**连工具带 method 一起不注册** —— 不是注册一个会报错的工具，
也不是留一个"位子"。理由与平台既有的 runtime_capability 一条线：
引导里点名一个模型够不着的东西，模型会围着它空转（而这里更糟：它会被门当成
合法来源）。

参见 nodes/derivation/tools/derivation_contract.py 里两道门的分工。
"""
from __future__ import annotations

from typing import Any

#: {工具名: {"methods": (...), "description": str}}
#:
#: 只有真的会 `derivation_ledger.record(...)` 的工具才该在这里。
_VERIFIERS: dict[str, dict[str, Any]] = {}

#: 不依赖任何验证工具的合法 method。
#:
#: `none` 是合法取值 —— 一步可以诚实地没被机械验证过（cited_theorem / prose /
#: definition）。它不对应任何工具，所以由这里兜底，不进 _VERIFIERS。
_METHODLESS = ("none",)


def register_verifier(tool_name: str, methods: tuple[str, ...],
                      description: str = "") -> None:
    """声明「这个工具会往验证账本写章，它产出的 method 是这些」。

    在验证工具自己的模块里、`register_tool` 旁边调用 —— 两件事必须同生共死：
    工具没注册进 registry 模型就调不到，验证器没注册进这里它的章就不被认。
    分开写迟早分叉。
    """
    _VERIFIERS[tool_name] = {"methods": tuple(methods),
                             "description": description}


def trusted_verifiers() -> tuple[str, ...]:
    """有资格在验证章上署名的工具名（现算，排序稳定）。"""
    return tuple(sorted(_VERIFIERS))


def known_methods() -> tuple[str, ...]:
    """所有合法的 verification.method（现算）。

    = 已注册验证器声明的 method 并集 + 不需要工具的那些。
    """
    out: set[str] = set(_METHODLESS)
    for spec in _VERIFIERS.values():
        out.update(spec.get("methods") or ())
    return tuple(sorted(out))


def methods_of(tool_name: str) -> tuple[str, ...]:
    """某个工具声称能产出的 method。"""
    return tuple((_VERIFIERS.get(tool_name) or {}).get("methods") or ())


def method_is_available(method: str) -> bool:
    """这个 method 现在有没有工具能产出（能力探测的对外形态）。"""
    return str(method) in known_methods()


def describe() -> str:
    """给模型看的一行摘要 —— 契约必须送到调用方。"""
    if not _VERIFIERS:
        return "（本环境没有注册任何验证工具）"
    return "；".join(
        f"{name}（{'/'.join(spec['methods'])}）"
        for name, spec in sorted(_VERIFIERS.items()))
