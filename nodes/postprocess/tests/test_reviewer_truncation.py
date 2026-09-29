"""审图模型把输出预算用光时，框架自己加预算 —— 不要原样重试。

E2E v20 实测：minimax-m3 把 4800 tokens 全花在推理上、content 一个字没吐
（HTTP 200 + finish_reason='length'）。重试三次，三次一模一样的请求、一模一样
的截断，然后报"缺 API key 或上游故障" —— 凭据其实完全正常，害人白查一轮。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nodes.postprocess import vlm_witness as rv
from nodes.postprocess.contracts import VisualTruncationError


@pytest.fixture(autouse=True)
def _visual_review_role(monkeypatch: pytest.MonkeyPatch):
    """审图角色已配置。

    从前这里 setenv 一个 ICOMPIFY_API_KEY 就算"配好了"。审图后端搬进平台的
    模型角色之后，那个变量不再是任何东西的判据 —— 照旧 setenv 只会让测试
    看起来配好了，而 review_image 第一步就返回 review_unavailable。
    """
    from core import model_roles

    monkeypatch.setenv("HARNESS_MODEL_ROLES", json.dumps({
        "visual_review": {
            "provider": "icompify",
            "model": "minimax-m3",
            "base_url": "https://reviewer.invalid",
            "api_key": "test-key",
        },
    }))
    model_roles.install_from_environment(force=True)
    yield
    model_roles.install_from_environment(force=True)


def _review(monkeypatch, responder) -> dict:
    calls: list[int] = []

    async def fake_call_once(*, config, binding, api_key, image_path, prompt,
                             include_detail_view=False, max_tokens=None):
        budget = int(max_tokens or config.max_tokens)
        calls.append(budget)
        return responder(budget)

    monkeypatch.setattr(rv, "_call_once", fake_call_once)
    monkeypatch.setattr(rv.ReviewerConfig, "retry_backoff_s", 0.0)
    result = asyncio.run(
        rv.review_image(
            image_path=Path(__file__),
            panel_id="p1",
            intent="check",
            checklist=["axis labels present"],
        )
    )
    result["_budgets"] = calls
    return result


def test_budget_doubles_instead_of_repeating_the_same_request(monkeypatch) -> None:
    def responder(budget: int):
        if budget < 9600:
            raise VisualTruncationError("all budget spent", finish_reason="length",
                                        max_tokens=budget)
        return ('{"observations": []}', "stop", {})

    result = _review(monkeypatch, responder)

    # 前两次是"截断 → 加预算"；之后的调用是 confirm_empty_once 的复核轮，
    # 沿用已经够用的预算，属于正常流程。
    assert result["_budgets"][:2] == [4800, 9600], "预算必须翻倍，不能原样重试"
    assert result["status"] != "review_unavailable"


def test_ceiling_reports_truncation_not_missing_credentials(monkeypatch) -> None:
    def responder(budget: int):
        raise VisualTruncationError("all budget spent", finish_reason="length",
                                    max_tokens=budget)

    result = _review(monkeypatch, responder)

    assert result["status"] == "review_truncated", "凭据没问题就不该说凭据有问题"
    assert result["_budgets"][-1] == rv.ReviewerConfig.max_tokens_ceiling
    assert len(set(result["_budgets"])) == len(result["_budgets"]), "每次预算都该不同"
    assert "预算" in result["reason"]


def test_an_unassigned_role_raises_instead_of_faking_a_review(monkeypatch) -> None:
    """A 刀（FIGURE_SUBSYSTEM_REBUILD.md）：角色在场由**调用方**保证。

    lifecycle 先 `model_roles.resolve("visual_review")` 判断，角色缺席时根本
    不发起审图（findings 里自然没有 VLM 条目）。review_image 被违反契约地
    直接调用时如实抛错，不返回一份 review_unavailable 的空壳 ——
    `model_roles.require` 在审图路径不再被调用。
    """
    from core import model_roles

    monkeypatch.delenv("HARNESS_MODEL_ROLES", raising=False)
    model_roles.install_from_environment(force=True)
    with pytest.raises(rv.VisualContractError) as excinfo:
        asyncio.run(
            rv.review_image(image_path=Path(__file__), panel_id="p1", intent="i",
                            checklist=["c"])
        )
    message = str(excinfo.value)
    assert "visual_review" in message and "caller must resolve" in message
    assert "ICOMPIFY_API_KEY" not in message
