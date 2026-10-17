"""每个运行时会声明的失败 code 都有去处（RFC_WORKER_START_IS_NOT_A_GATE §3.7，窄单 E）。

lujy 10-15 现场：worker 报 `tenant_home_conflict_corrupt`，文案表里没有，退到兜底的「这一轮没能完成 /
再发一条」—— 六次。没有任何东西在新 code 出现时提醒我们写文案。

闸按 AST 扫，不按名单：
- worker（`platform_runtime.py`）：`RequestError(...)` 及其子类第一个字面量参数、`conflict_code="..."`、
  `_emit_error(_, "...")`、`_error_code_for` 返回的字面量；
- 后端（`app/`）：`HarnessSessionError(..., code="...")` 一族的字面量。
每一个 code 要么在 `_COPY` 有文案，要么在 `_FALLS_TO_GENERIC` 登记了为什么兜底够用。
反向也查：登记了却没人声明的 code 一样红（表不许长成坟场）。两张表不许重叠。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.services import run_failures

HARNESS_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = HARNESS_ROOT / "platform_runtime.py"
APP = HARNESS_ROOT / "platform" / "backend" / "app"

_WORKER_ERRORS = {"RequestError", "HardStartFailure", "ProviderNotConfigured", "IdentityConflict"}
_BACKEND_ERRORS = {"HarnessSessionError", "HarnessSessionStaleError", "HarnessSessionProcessError",
                   "HarnessRuntimeError"}


def _callee(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literal(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def declared_codes() -> dict[str, set[str]]:
    """code → 声明它的位置。"""
    found: dict[str, set[str]] = {}

    def add(code: str | None, where: str) -> None:
        if code:
            found.setdefault(code, set()).add(where)

    source = RUNTIME.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node)
        where = f"platform_runtime.py:{node.lineno}"
        if name in _WORKER_ERRORS and node.args:
            add(_literal(node.args[0]), where)
        if name == "_emit_error" and len(node.args) > 1:
            add(_literal(node.args[1]), where)
        for keyword in node.keywords:
            if keyword.arg == "conflict_code":
                add(_literal(keyword.value), where)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_error_code_for":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return):
                    add(_literal(inner.value), f"platform_runtime.py:{inner.lineno}")
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) in _BACKEND_ERRORS:
                for keyword in node.keywords:
                    if keyword.arg == "code":
                        add(_literal(keyword.value), f"{path.relative_to(HARNESS_ROOT)}:{node.lineno}")
    return found


def test_every_declared_code_has_copy_or_is_registered_as_generic():
    declared = declared_codes()
    assert len(declared) > 30, "扫描器没扫到东西 —— 闸空转"
    homeless = {
        code: sorted(where)[:2]
        for code, where in declared.items()
        if code not in run_failures._COPY and code not in run_failures._FALLS_TO_GENERIC
    }
    assert not homeless, (
        "这些 code 既没有文案也没登记为什么兜底够用（run_failures._COPY / _FALLS_TO_GENERIC）：\n"
        + "\n".join(f"  {code}  ← {where}" for code, where in sorted(homeless.items()))
    )


def test_the_generic_registry_only_names_codes_somebody_declares():
    declared = declared_codes()
    stale = sorted(code for code in run_failures._FALLS_TO_GENERIC if code not in declared)
    assert not stale, f"登记了却没人声明的 code（表不许长成坟场）：{stale}"


def test_copy_and_registry_do_not_overlap():
    both = sorted(set(run_failures._COPY) & set(run_failures._FALLS_TO_GENERIC))
    assert not both, f"既有文案又登记兜底，一个 code 两个答案：{both}"


def test_every_registered_code_says_why():
    empty = [code for code, why in run_failures._FALLS_TO_GENERIC.items() if not str(why).strip()]
    assert not empty, f"登记要说为什么兜底够用：{empty}"


def test_the_three_hard_start_failures_each_say_their_own_thing():
    """RFC §3.1：模型不可达说模型，身份冲突说「是别人的」，不说「读不到」。"""
    provider = run_failures.copy_for("provider_not_configured")
    tenant = run_failures.copy_for("tenant_home_conflict")
    state = run_failures.copy_for("runtime_identity_conflict")
    assert "模型" in provider["title"]
    assert "别人" in tenant["title"] and "读不到" not in tenant["body"]
    assert "会话" in state["title"]
    assert provider["retryable"] is False and tenant["retryable"] is False


def test_registered_generic_codes_do_not_log_a_missing_copy_warning(caplog):
    import logging

    class _Declared(Exception):
        code = "invalid_request"

    with caplog.at_level(logging.WARNING, logger=run_failures.logger.name):
        assert run_failures.describe(_Declared("x")).code == "invalid_request"
    assert not [r for r in caplog.records if "has no user-facing copy" in r.getMessage()]

    class _Unknown(Exception):
        code = "something_nobody_registered"

    with caplog.at_level(logging.WARNING, logger=run_failures.logger.name):
        run_failures.describe(_Unknown("x"))
    assert [r for r in caplog.records if "has no user-facing copy" in r.getMessage()]


def test_the_scanner_sees_each_declaration_shape():
    """扫描器认的四种形状各一例（否则闸只是看起来在扫）。"""
    sample = ast.parse(
        'raise RequestError("shape_a", "x")\n'
        'raise IdentityConflict(conflict_code, "y")\n'
        'def f():\n    _bind("m", {}, conflict_code="shape_b", label="l")\n'
        '_emit_error(rid, "shape_c", "T", "m", {})\n'
        'def _error_code_for(exc):\n    return "shape_d"\n'
    )
    seen: set[str] = set()
    for node in ast.walk(sample):
        if isinstance(node, ast.Call):
            name = _callee(node)
            if name in _WORKER_ERRORS and node.args:
                seen.add(_literal(node.args[0]) or "")
            if name == "_emit_error" and len(node.args) > 1:
                seen.add(_literal(node.args[1]) or "")
            for keyword in node.keywords:
                if keyword.arg == "conflict_code":
                    seen.add(_literal(keyword.value) or "")
        if isinstance(node, ast.FunctionDef) and node.name == "_error_code_for":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return):
                    seen.add(_literal(inner.value) or "")
    assert {"shape_a", "shape_b", "shape_c", "shape_d"} <= seen
    assert re.fullmatch(r"[a-z_]+", "shape_a")
