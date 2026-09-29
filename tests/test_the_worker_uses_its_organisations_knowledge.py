"""会话 worker 读写的 org 层，是平台在 init 里说的那一个 —— 不是它自己 home 底下的 `org/`。

## 病例（2026-09-24 读代码核实）

09-16 把 org 层改成「由平台说，不从环境里嗅」：`_temporary_home(home_dir, org_home)`
没人说时退回 `home_dir/org`。KB 桥每一问都说了；**会话 worker 没有** —— 它
`_temporary_home(self.home_dir)`。平台给每个用户一个 harness home（`state/users/<uid>`），
于是真正在跑研究的 agent 一直各自一个私有 org 层：界面上的「组织知识」和 agent 读写的
不是同一份，晋升上去的东西组里别人的项目也读不到。

判据：在模型被调用的那一刻（agent 正在干活），harness 眼里的 org 层是哪个目录。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from core.llm import LLMResponse
from core.pause import clear_all
from platform_runtime import serve_jsonl
from tests.test_platform_runtime import _write_instruction_files


class _Watching:
    """一个只答一句的模型，答之前记下这一刻 harness 眼里的 org 层。"""

    def __init__(self) -> None:
        self.org_layer_while_working: list[Path] = []
        self.stream_display = None

    async def chat(self, messages, **kwargs):
        from core.paths import org_root

        self.org_layer_while_working.append(org_root())
        return LLMResponse(content="好。", tool_calls=[], finish_reason="stop",
                           usage={"total_tokens": 3})


async def _one_turn(home: Path, **init) -> list[Path]:
    clear_all()
    _write_instruction_files(home, "project-org")
    requests = [
        {"op": "init", "request_id": "init-1", "tenant_id": "tenant-test",
         "project_id": "project-org", "session_id": "session-org",
         "home_dir": str(home), **init},
        {"op": "turn", "request_id": "turn-1", "message": "开始"},
        {"op": "terminate", "request_id": "terminate-1"},
    ]
    stream = io.StringIO("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests))
    events: list[dict] = []
    llm = _Watching()
    await serve_jsonl(stream, lambda kind, **payload: events.append({"type": kind, **payload}),
                      llm=llm)
    assert any(e["type"] == "result" for e in events), events
    clear_all()
    return llm.org_layer_while_working


@pytest.mark.asyncio
async def test_the_worker_works_in_the_organisation_it_was_told(tmp_path: Path) -> None:
    home = tmp_path / "users" / "u1"
    organisation = tmp_path / "organisations" / "A"

    seen = await _one_turn(home, org_home=str(organisation))

    assert seen and all(p == organisation for p in seen), (
        f"agent 干活时读写的是 {seen}，不是平台说的那个组织的 {organisation}")


@pytest.mark.asyncio
async def test_without_being_told_it_stays_in_its_own_home(tmp_path: Path) -> None:
    """CLI / 夹具不说 —— 那就是这个 home 自己的 org/（不去环境里捡一个）。"""
    home = tmp_path / "users" / "u1"

    seen = await _one_turn(home)

    assert seen and all(p == home / "org" for p in seen)


class _Reading(_Watching):
    """同上，另外记下模型收到的全部正文。"""

    def __init__(self) -> None:
        super().__init__()
        self.read: list[str] = []

    async def chat(self, messages, **kwargs):
        self.read.extend(str(getattr(m, "content", "") or "") for m in messages)
        return await super().chat(messages, **kwargs)


@pytest.mark.asyncio
async def test_a_new_project_opens_with_what_the_organisation_knows(tmp_path: Path) -> None:
    """复利的另一半：组织采纳过的东西，别的项目开局就摆在 agent 面前。

    `org_orientation` 这个 hook 早就写好了（判据里点名 `_orchestrator`），却没挂在任何
    节点上 —— 2026-09-24 真跑：管理员采纳了一条结论，组里新开的项目第一轮，模型收到的
    八条消息里一个字都没有。知识只在 agent 自己想起来 search_kb 时才读得到。
    """
    import os

    from core import kb_promotion as kp
    from core.state import State
    from tests.test_kb_promotion import _good_card, _make_terminal, _seed_claim, _seed_evidence

    organisation = tmp_path / "organisations" / "A"
    elsewhere = tmp_path / "users" / "someone-else"
    saved = {k: os.environ.get(k) for k in ("HARNESS_FRAMEWORK_HOME", "HARNESS_FRAMEWORK_ORG_HOME")}
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(elsewhere)
    os.environ["HARNESS_FRAMEWORK_ORG_HOME"] = str(organisation)
    try:
        # 别人的项目做完了，交上来，管理员采纳了。
        done = State.new(node_type="_orchestrator", base_dir=elsewhere / "runs", project_id="p_done")
        _make_terminal(done)
        finding = _seed_claim(done, _seed_evidence(done))
        kp.offer_to_the_organisation(done, project_id="p_done", at="2026-09-24T00:00:00Z",
                                     drafts={finding: _good_card()})
        [waiting] = kp.review_queue()
        assert kp.adopt(done, waiting["id"], approved_by="admin", at="2026-09-24T00:00:00Z")["status"] == "success"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    clear_all()
    home = tmp_path / "users" / "u1"
    _write_instruction_files(home, "project-org")
    requests = [
        {"op": "init", "request_id": "init-1", "tenant_id": "tenant-test",
         "project_id": "project-org", "session_id": "session-org",
         "home_dir": str(home), "org_home": str(organisation)},
        {"op": "turn", "request_id": "turn-1", "message": "开题：高压相变用哪种势函数？"},
        {"op": "terminate", "request_id": "terminate-1"},
    ]
    stream = io.StringIO("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests))
    llm = _Reading()
    await serve_jsonl(stream, lambda *_a, **_k: None, llm=llm)
    clear_all()

    assert any(_good_card()["statement"] in text for text in llm.read), (
        "组织采纳过的结论，新项目开局时模型一个字都没看到")
