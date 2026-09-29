"""前端的线类型必须是后端模型的**生成物**，不是手抄。

`ChatRequest` 曾经在前后端各有一份手写定义，`min_length=1` 只在后端那份 ——
"什么算一次合法提交"于是在两边各自演化，前端为此又长出三道各自的守卫，
分叉时谁都不报错（2026-09-03，cuib 会话 de4632cc：点了选项不写附言，请求
根本没出浏览器）。

这条测试在后端 CI 里跑生成器的 `--check`：改了模型没重新生成、或者有人手改
了生成文件，都在这里红。生成器本身就是契约的唯一出处，见
`platform/contracts/generate_wire_types.py`。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


def _generator():
    spec = importlib.util.spec_from_file_location(
        "generate_wire_types", CONTRACTS_DIR / "generate_wire_types.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_frontend_wire_types_equal_a_fresh_generation() -> None:
    generator = _generator()
    committed = generator.OUTPUT.read_text(encoding="utf-8")
    fresh = generator.emit(generator.models())
    assert committed == fresh, (
        "frontend/src/lib/generated/chat-request.ts 与后端模型不一致 —— "
        "在 platform/backend 下跑 `uv run python ../contracts/generate_wire_types.py` 重新生成"
    )


def test_generated_wire_types_are_the_ones_the_frontend_imports() -> None:
    """生成了没人用 = 契约仍然是手抄的。api.ts 必须从生成文件取 ChatRequest。"""
    generator = _generator()
    api_ts = (generator.PLATFORM_DIR / "frontend" / "src" / "lib" / "api.ts").read_text(
        encoding="utf-8"
    )
    assert 'from "./generated/chat-request"' in api_ts or (
        'from "@/lib/generated/chat-request"' in api_ts
    ), "api.ts 没有从生成文件导入 ChatRequest"
    assert "export interface ChatRequest" not in api_ts, "api.ts 里又长出了一份手写 ChatRequest"


def test_generator_refuses_shapes_it_does_not_understand() -> None:
    """猜出来的类型和手抄没有区别 —— 不认识的 schema 形状必须报错，不能静默产出。"""
    import pytest

    generator = _generator()
    with pytest.raises(ValueError):
        generator._ts_type({"type": "object", "additionalProperties": True}, aliases={})
