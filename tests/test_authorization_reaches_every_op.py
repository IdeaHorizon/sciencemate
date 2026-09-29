"""授权范围是**会话属性**，每一次派发都带着它当下的值。

## 现场（2026-08-13）

一轮无人值守 E2E 停在「真实外部作业提交」上。我在 UI 上把项目模式拨到
「连续」（库里已是 `["*"]`），然后答复那个挂起的询问 —— **它下一个高危点
还是停**。

原因：只有 `op=run_unattended` 带 `authorized_risk_classes`，`op=answer`
不带。于是授权是**出发时的快照**，一趟之内改不了。一个空授权起跑的 run，
事后给它多少授权都进不去，只能一次次手动点批准 —— UI 上白纸黑字写着
「连续」，而这个承诺在这条路径上永远兑现不了。

## 为什么修在收发口而不是给 answer 补个字段

给 `answer` 补一个参数能让这次好使，但那是**名单式护栏**：明天加第四个
op，它默认又漏，而且漏的时候不报错。所以两侧都挪到唯一的必经点 ——
App Server 的 `_rpc_locked`（每条请求都挂上），worker 的请求分发点
（处理任何 op 之前先应用）。新增的 op 自动就带上，漏不掉。

## 为什么这些测试要真跑 serve_jsonl

原来这三条是**源码检视**：`inspect.getsource` 出来做字符串匹配，断言
`"unattended_authorization_declared" in run_unattended` 之类。那种判据把
实现的写法冻在测试里 —— 保持行为不变的重构会打红它，而写法相似、语义
损坏的改动照样绿。同事在 `04552d7` 点过这个毛病：复刻判据等于自己给
自己打分。

这里改成真起 `serve_jsonl`、真发 JSONL 请求、真让模型调一条 `sudo`
命令，看**它到底停不停**。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.llm import LLMResponse
from platform_runtime import serve_jsonl

from tests.test_platform_runtime import _FakeLLM

#: 一条真会命中 `_SHELL_PATTERNS` 的命令（提权）。用真词表里的东西，
#: 而不是自己造一个假类别 —— 造假类别就测不到"真实的高危路径"。
HIGH_RISK_COMMAND = "sudo systemctl restart nginx"
#: 另一条同样命中词表的命令。测"档位变了"时必须换一条 —— 高危门对同一条
#: 命令留有已批准的残留标记，复用它会让测试假绿。
ANOTHER_HIGH_RISK_COMMAND = "sudo systemctl restart postgresql"


@pytest.fixture(autouse=True)
def _restore_authorization_scope():
    """授权范围是模块级全局 —— 谁改谁还。

    这里的每条测试都会通过真实 JSONL 请求改到它。不还原的话，同一进程后面
    所有测试都处于"已预授权"状态：实测污染了
    `test_highrisk_approval_retry_contract` 的 5 条，而且它们**单跑全绿、
    全量才红** —— 全局可变状态最难查的那种症状。
    """
    from shared.lib import dangerous_commands as dc

    saved = set(dc.PREAUTHORIZED_CATEGORIES)
    try:
        yield
    finally:
        dc.set_preauthorized_categories(saved)


def _bash_call(cmd: str) -> dict:
    return {
        "id": f"call_{abs(hash(cmd)) % 10**8}",
        "type": "function",
        "function": {
            "name": "run_bash",
            "arguments": json.dumps({"cmd": cmd}, ensure_ascii=False),
        },
    }


def _calls_bash(cmd: str) -> LLMResponse:
    return LLMResponse(
        content=None, tool_calls=[_bash_call(cmd)], finish_reason="tool_calls",
        usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )


def _says(text: str) -> LLMResponse:
    return LLMResponse(
        content=text, tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )


class _LiveStream:
    """按需生成下一条请求的输入流。

    `answer` 必须带**真实的** `pause_id` —— 上一条请求跑完才知道它是什么。
    第一版我在请求里写了个占位符：`answer` 于是被 `pause_conflict` 挡在门外，
    "后面没有再暂停"就成了必然，测试稳稳地绿，却什么也没测到。

    所以这里在读第 N 行的时候，才拿已经收到的事件把这一条填完整。
    """

    def __init__(self, requests: list[dict], events: list[dict]) -> None:
        self._requests = list(requests)
        self._events = events
        self._index = 0

    def _latest_pause_id(self) -> str:
        for event in reversed(self._events):
            pause = (event.get("data") or {}).get("pause_event") or {}
            if pause.get("pause_id"):
                return str(pause["pause_id"])
            if event.get("type") == "pause_required" and event.get("pause_id"):
                return str(event["pause_id"])
        raise AssertionError(
            "要答复一个暂停，但事件流里根本没有暂停 —— 前置条件没成立，"
            "后面断言『没再暂停』就毫无意义"
        )

    def readline(self, _limit: int | None = None) -> str:
        if self._index >= len(self._requests):
            return ""
        request = dict(self._requests[self._index])
        self._index += 1
        if request.get("op") == "answer":
            request["pause_id"] = self._latest_pause_id()
        return json.dumps(request, ensure_ascii=False) + "\n"


async def _drive(tmp_path: Path, requests: list[dict], responses: list[LLMResponse]) -> list[dict]:
    """真跑一次 serve_jsonl，返回它发出的全部事件。"""
    events: list[dict] = []
    await serve_jsonl(
        _LiveStream(requests, events),
        lambda event_type, **payload: events.append({"type": event_type, **payload}),
        llm=_FakeLLM(list(responses)),
    )
    return events


def _init(tmp_path: Path) -> dict:
    return {
        "op": "init",
        "request_id": "init-auth",
        "tenant_id": "tenant-test",
        "project_id": "project-auth",
        "session_id": "session-auth",
        "home_dir": str(tmp_path / "isolated-home"),
    }


def _pauses(events: list[dict], request_id: str) -> list[dict]:
    """这次调用停下来问人了几次。"""
    return [
        event for event in events
        if event.get("request_id") == request_id
        and (event.get("data") or {}).get("status") == "paused"
    ]


@pytest.mark.asyncio
async def test_an_unauthorized_run_stops_at_the_high_risk_point(tmp_path: Path) -> None:
    """基线：没声明授权，高危点必须停 —— 默认永远是"停下问人"。"""
    events = await _drive(
        tmp_path,
        [
            _init(tmp_path),
            {
                "op": "run_unattended", "request_id": "un-1",
                "message": "去重启服务", "max_turns": 4,
                "authorized_risk_classes": [],
            },
        ],
        [_calls_bash(HIGH_RISK_COMMAND), _says("做完了")],
    )
    assert _pauses(events, "un-1"), "空授权居然没停 —— 默认必须是停下问人"


@pytest.mark.asyncio
async def test_authorization_granted_while_answering_takes_effect(tmp_path: Path) -> None:
    """核心：空授权起跑、停在高危点，**答复时**给授权 —— 后续高危点不再停。

    这是 2026-08-13 那个死循环的直接判据。`op=answer` 不带授权时，这条会红：
    答复之后模型再调一次 `sudo`，它会第二次停下来问人。
    """
    events = await _drive(
        tmp_path,
        [
            _init(tmp_path),
            {
                "op": "run_unattended", "request_id": "un-1",
                "message": "去重启服务", "max_turns": 4,
                "authorized_risk_classes": [],
            },
            {
                "op": "answer", "request_id": "ans-1",
                # pause_id 由 `_LiveStream` 在读到这一行时按真实事件填入
                "answer": "批准",
                "authorized_risk_classes": ["*"],   # ← 人在这一刻给了授权
            },
        ],
        [
            _calls_bash(HIGH_RISK_COMMAND),         # 第 1 次：停下问人
            _calls_bash(HIGH_RISK_COMMAND),         # 拿到通行证后重调（框架契约）
            _calls_bash("sudo systemctl restart redis"),  # 第 2 个高危点
            _says("都做完了"),
        ],
    )
    assert not _pauses(events, "ans-1"), (
        "答复时给了全类别授权，后面的高危点还是停下来问人了 —— "
        "「连续」在 answer 这条路径上没有生效"
    )


@pytest.mark.asyncio
async def test_a_declaration_reaches_the_session_whatever_the_op_is(tmp_path: Path) -> None:
    """声明落在**请求分发点**，不是某个 op 里 —— 这才是"漏不掉"的理由。

    判据不看源码写法，看事实：随便挑一个 op（这里用 `turn`）带上声明，
    会话的授权范围就该变。任何将来新增的 op 同样成立，因为它们都过同一个点。
    """
    from shared.lib import dangerous_commands as dc

    saved = set(dc.PREAUTHORIZED_CATEGORIES)
    try:
        await _drive(
            tmp_path,
            [
                _init(tmp_path),
                {
                    "op": "turn", "request_id": "turn-1", "message": "说句话",
                    "authorized_risk_classes": ["真实外部作业提交"],
                },
            ],
            [_says("好的")],
        )
        assert dc.preauthorized("真实外部作业提交"), (
            "`turn` 带的声明没生效 —— 说明接收还长在某个 op 里，"
            "而不是在所有 op 的必经点上"
        )
    finally:
        dc.set_preauthorized_categories(saved)


@pytest.mark.asyncio
async def test_a_malformed_declaration_is_rejected_by_name(tmp_path: Path) -> None:
    """契约必须送到调用方：报错要给出合法取值从哪来。"""
    events = await _drive(
        tmp_path,
        [
            _init(tmp_path),
            {
                "op": "turn", "request_id": "turn-bad", "message": "说句话",
                "authorized_risk_classes": "真实外部作业提交",   # 应该是 list
            },
        ],
        [_says("好的")],
    )
    errors = [e for e in events if e.get("type") == "error" and e.get("request_id") == "turn-bad"]
    assert errors, "畸形声明被静默接受了 —— 静默无效正是最难查的那种失败"
    assert errors[0].get("code") == "invalid_authorized_risk_classes"
    assert "match_high_risk" in str(errors[0].get("message") or ""), (
        "报错没说合法类别从哪来 —— 逼调用方猜"
    )


@pytest.mark.asyncio
async def test_omitting_the_field_keeps_the_current_scope(tmp_path: Path) -> None:
    """省略 = "这次不谈授权"，沿用本趟已声明的范围。

    否则任何一条不带该字段的请求都会**悄悄收回**授权 —— 收回授权的正规
    路径是 reset（换届），不是"某次请求恰好没写这个字段"。
    """
    from shared.lib import dangerous_commands as dc

    saved = set(dc.PREAUTHORIZED_CATEGORIES)
    try:
        await _drive(
            tmp_path,
            [
                _init(tmp_path),
                {
                    "op": "turn", "request_id": "turn-1", "message": "一",
                    "authorized_risk_classes": ["*"],
                },
                {"op": "turn", "request_id": "turn-2", "message": "二"},   # 不带
            ],
            [_says("好"), _says("好")],
        )
        assert dc.preauthorized("真实外部作业提交"), (
            "一条不带该字段的请求把授权收回去了 —— 换届只能走 reset"
        )
    finally:
        dc.set_preauthorized_categories(saved)


# ── 中途改档：两个方向都要真的送达（2026-08-23）────────────────────────────


def _accepted(events: list[dict], request_id: str) -> dict | None:
    for event in events:
        if event.get("type") == "accepted" and event.get("request_id") == request_id:
            return event
    return None


@pytest.mark.asyncio
async def test_a_sync_op_upgrades_the_scope_without_a_conversation(tmp_path: Path) -> None:
    """档位可以在**没有任何对话请求**的情况下改掉，并且立刻生效。

    这是 2026-08-23 那个 bug 的核心：UI 上改设置只写库行，worker 手里是派发时
    的快照，而一轮无人值守可以跑几十分钟不产生任何派发。管理面的 `sync` 让
    "改设置"自己成为一次派发 —— 它不做任何事，只负责走一次必经点。

    先空授权跑一轮（必停），再 sync 成全授权，同一个会话再跑一轮（必不停）。
    两轮之间没有任何东西重启，也没有人手动答复过。
    """
    events = await _drive(
        tmp_path,
        [
            _init(tmp_path),
            {
                "op": "run_unattended", "request_id": "before",
                "message": "去重启服务", "max_turns": 4,
                "authorized_risk_classes": [],
            },
            {
                "op": "sync", "request_id": "switch",
                "authorized_risk_classes": ["*"],
            },
            {
                "op": "turn", "request_id": "after",
                "message": "接着重启",
                "authorized_risk_classes": ["*"],
            },
        ],
        [
            _calls_bash(HIGH_RISK_COMMAND), _says("做完了"),
            _calls_bash(HIGH_RISK_COMMAND), _says("做完了"),
        ],
    )
    assert _pauses(events, "before"), "前置条件不成立：空授权那一轮居然没停"

    ack = _accepted(events, "switch")
    assert ack is not None, "sync 没有回执 —— 收下 ≠ 黑洞，两句都要说"
    assert ack.get("authorized_risk_classes") == ["*"], (
        f"sync 回执里的档位不是刚送到的那份：{ack}"
    )
    assert not _pauses(events, "after"), (
        "改档之后那一轮还是停下来问人了 —— 声明送到了但没有被施加"
    )


@pytest.mark.asyncio
async def test_revoking_the_scope_takes_effect_too(tmp_path: Path) -> None:
    """降级方向：连续 → 协作。这个方向 2026-08-23 之前**完全不生效**。

    接收端写着 `if declared and …`，空列表被当成"这次不谈授权"沿用旧值；
    而"协作"档在后端算出来就是空列表。于是档位只能变大，收回授权唯一的出口
    是 reset —— 而用户在 UI 上明明把开关拨回去了。
    """
    events = await _drive(
        tmp_path,
        [
            _init(tmp_path),
            {
                # 用 turn 而不是 run_unattended：后者会自己续轮，把假 LLM 的
                # 回答提前吃光，第二轮直接 IndexError —— 那时"没停下来"是因为
                # 它根本没跑到高危点，测试会假绿（实测栽过一次）。
                "op": "turn", "request_id": "wide-open",
                "message": "去重启服务",
                "authorized_risk_classes": ["*"],
            },
            {
                "op": "sync", "request_id": "revoke",
                "authorized_risk_classes": [],
            },
            {
                "op": "turn", "request_id": "after-revoke",
                "message": "再重启一次",
                "authorized_risk_classes": [],
            },
        ],
        [
            _calls_bash(HIGH_RISK_COMMAND), _says("做完了"),
            # 第二轮换一条命令：高危门对**同一条命令**留有已批准的残留标记，
            # 复用它就测不到"档位变了没有"（会稳稳地绿，而且是假绿）。
            _calls_bash(ANOTHER_HIGH_RISK_COMMAND), _says("做完了"),
        ],
    )
    assert not _pauses(events, "wide-open"), "前置条件不成立：全授权那一轮居然停了"

    ack = _accepted(events, "revoke")
    assert ack is not None and ack.get("authorized_risk_classes") == [], (
        f"收回授权没有被接收端认下：{ack}"
    )
    assert _pauses(events, "after-revoke"), (
        "切回协作之后高危点还是自动放行 —— 降级根本没生效，"
        "而这正是「切回协作应该立刻开始问」那半边"
    )
