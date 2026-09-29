"""上下文窗口是**模型的属性**，必须传到 worker。

## 现场（2026-08-18，会话 c9deb4f2）

窗口此前只有一个全局 env `LLM_CONTEXT_WINDOW`，而 App Server 建 worker 环境
时**根本没传它** —— 于是不管用户选了哪个模型，harness 一律吃 120000 的兜底
默认值。摘要器按窗口的 70% 触发压缩，于是一个百万窗口的模型也在 84k 处开始
反复压：一个会话压了 15 次，最后调度器自己要求换 session。

"设置里有这一项"和"这一项真的生效"是两件事，这条测试守的是后者。
"""
from __future__ import annotations

import inspect


def test_the_worker_environment_carries_the_model_window() -> None:
    from app.services import harness_sessions

    src = inspect.getsource(harness_sessions)
    assert 'child_env["LLM_CONTEXT_WINDOW"]' in src, (
        "worker 环境没带模型窗口 —— 设置里填了也不会生效"
    )
    assert "backend.context_window_tokens" in src, "窗口没有取自这个模型的配置"


def test_an_unset_window_is_not_guessed() -> None:
    """没配就不传，让 harness 用它自己的默认 —— 别替用户编一个数。

    编一个数的代价不是"差不多"：它直接决定何时压缩上下文，猜大了撞
    provider 硬上限，猜小了反复丢上下文。
    """
    from app.services import harness_sessions

    src = inspect.getsource(harness_sessions)
    assert "if backend.context_window_tokens:" in src


def test_the_column_is_nullable_so_existing_backends_keep_working() -> None:
    from app.models.model_backend import ModelBackendConfig

    column = ModelBackendConfig.__table__.c.context_window_tokens
    assert column.nullable, "存量配置不该被逼着填一个它不知道的数"
