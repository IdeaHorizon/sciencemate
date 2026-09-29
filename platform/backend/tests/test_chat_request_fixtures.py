"""前端组装出来的请求体，后端必须逐字节认 —— 两边钉在同一批 fixture 上。

`platform/contracts/fixtures/chat-request/accepted/*.json` 由前端测试
（`features/chat/lib/answer.test.ts`）用生产同一条路 `buildChatRequest` 生成并
断言相等；这里把同一批文件喂进真实的 Pydantic 模型。任何一边改了形状而另一边
没跟上，两个测试里必有一个红。

`rejected/*.json` 是必须 422 的形状：旧线格式、空白文本、没有 id 的选项、
不认识的 kind。它们要**响亮地**被拒，而不是被某一层重新解释成别的东西。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.v1.chat import ChatRequest, ChoiceAnswer, TextAnswer, turn_input_for

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "chat-request"
ACCEPTED = sorted((FIXTURES / "accepted").glob("*.json"))
REJECTED = sorted((FIXTURES / "rejected").glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ACCEPTED, ids=[p.stem for p in ACCEPTED])
def test_every_accepted_fixture_parses_and_keeps_its_kind(path: Path) -> None:
    raw = _load(path)
    request = ChatRequest.model_validate(raw)
    assert request.answer.kind == raw["answer"]["kind"]
    assert request.answer.kind == path.stem.split("-", 1)[0], (
        "fixture 文件名的前缀就是它声明的 kind，两者必须一致"
    )


def test_a_choice_without_a_note_is_a_complete_submission() -> None:
    """cuib 09-03 的那一次：选了 REVISE、附言空。它必须是合法提交。"""
    request = ChatRequest.model_validate(_load(FIXTURES / "accepted" / "choice-without-note.json"))
    assert isinstance(request.answer, ChoiceAnswer)
    message, choice = turn_input_for(request.answer)
    assert message == ""
    assert choice == {
        "offer_id": "1788332859-ecd718:pecb8eac6:o3b1cc632",
        "choice_id": "revise",
    }


def test_text_is_stripped_once_at_the_door() -> None:
    request = ChatRequest.model_validate(
        {"answer": {"kind": "text", "text": "  跑的怎么样了？\n"}}
    )
    assert isinstance(request.answer, TextAnswer)
    assert request.answer.text == "跑的怎么样了？"
    assert turn_input_for(request.answer) == ("跑的怎么样了？", None)


@pytest.mark.parametrize("path", REJECTED, ids=[p.stem for p in REJECTED])
def test_every_rejected_fixture_is_refused_loudly(path: Path) -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(_load(path))


def test_accepted_and_rejected_fixtures_both_exist() -> None:
    """目录空了测试会"全过"—— 这条防止 fixture 被搬走之后闸静默失效。"""
    assert len(ACCEPTED) >= 4
    assert len(REJECTED) >= 3
