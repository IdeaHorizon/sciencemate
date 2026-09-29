"""worker 自己那道 provider 闸不许再把"没有 API key"当成"没配好"。

PR#1019 按「谁提供端点」重判了平台侧三处和 `core/llm.py`，**漏了这个文件里的
两份**（`run_platform_request` 与 `Session.start` 各抄了一遍同样的八行）。于是
yuankk 的自建 vLLM 照旧 `provider_not_configured: missing provider environment
variables: LLM_API_KEY` —— 探针说 Ready、真跑还是起不来。

一个问题抄两份，改对一份就只剩另一份继续错，而两边都不报错。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import platform_runtime as pr

RUNTIME = Path(pr.__file__)


def test_a_self_hosted_endpoint_without_a_key_passes_the_gate(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://10.128.7.30:8000")
    monkeypatch.setenv("LLM_MODEL", "qwen3.8-27b")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    pr._require_provider_environment()   # 不抛 = 通过


def test_being_unable_to_reach_anything_is_still_refused(monkeypatch):
    """闸没被拆掉，只是不再拿 key 说事：够不到端点仍然当场停。"""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "m")

    with pytest.raises(pr.RequestError) as caught:
        pr._require_provider_environment()
    assert "LLM_BASE_URL" in str(caught.value)
    assert "LLM_API_KEY" not in str(caught.value)


def test_this_file_asks_the_question_in_exactly_one_place():
    """判据扫的是「这个问题被答了几次」，不是某两行长什么样。

    抄第二份不会有任何东西报错 —— 上一次就是这么漏的。
    """
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    lists_of_provider_vars = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set))
        and {getattr(e, "value", None) for e in node.elts if isinstance(e, ast.Constant)}
        >= {"LLM_BASE_URL", "LLM_MODEL"}
    ]
    assert len(lists_of_provider_vars) == 1, (
        f"「够到端点要哪些环境变量」在这个文件里被答了 {len(lists_of_provider_vars)} 次")

    callers = [node for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "_require_provider_environment"]
    assert len(callers) == 2, f"两个入口都得过这道闸，实际 {len(callers)} 处"
