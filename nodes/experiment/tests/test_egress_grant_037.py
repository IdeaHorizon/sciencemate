"""037 反驳复核钉子：判定按 curl/git 真正连的主机；「申请→答复→重试」不绕回同一张卡。

与复审稿的差别：
* 循环用例不再断言「Core 没记下授权」（``assert not is_granted``）——那是把 #1068 的缺陷
  写成节点测试的前提，Core 一修（resume 时调 grant_from_answer），节点测试就在别人的 PR 里变红。
  这里用 monkeypatch 把「授权未生效」钉住，或者用「拒绝」这条 Core 修不修都一样的路。
* 另加一条防过度修：本 run 申请过、且授权确实生效了的主机，必须照常联网。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import core.capability_grants as capability_grants
from core.bootstrap import bootstrap
from core.capability_grants import grant_from_answer
from core.llm import LLMMessage, LLMResponse
from core.loader import load_harness
from core.pause import clear_all, get_paused_run
from nodes.experiment.tests.test_resource_fetch import (
    _clone_with,
    _network_calls,
    _offline_git_is_real,
    _redirecting_spawn,
    _run,
    _state,
)
from nodes.experiment.tools import resource_fetch as fetch


def _must_not_spawn(spawned: list):
    async def spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError(f"a request left the node for {args[-1]!r}")

    return spawn


# ── 判定口径：按下载进程真正要连的主机算 ────────────────────────────────────


@pytest.mark.parametrize("kind", ["file", "git"])
def test_query_at_sign_cannot_launder_an_unlisted_source_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)  # 内置默认含 github.com
    url = "https://evil.example?@github.com/o/r.git"
    assert urlsplit(url).hostname == "evil.example"  # curl / git 真正连的主机
    spawned: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait", _must_not_spawn(spawned))

    result = _run(state, url=url, kind=kind, destination=f"laundered-{kind}")

    assert spawned == [], "名单外主机 evil.example 被当成 github.com 放行"
    assert result["status"] == "error", result
    request = result.get("request_network_access")
    if request is not None:
        assert request["arguments"]["host"] == "evil.example", request


def test_query_at_sign_cannot_borrow_an_exact_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "mirror.example.net")
    assert grant_from_answer(state, "data.example.org", "允许", reason="declared input")
    spawned: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait", _must_not_spawn(spawned))

    result = _run(state, url="https://evil.example?@data.example.org/archive.nc",
                  kind="file", destination="archive.nc")

    assert spawned == [], "对 data.example.org 的精确授权被借给了 evil.example"
    assert result["status"] == "error", result


def test_a_submodule_cannot_launder_its_host_through_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    calls: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait", _offline_git_is_real(
        calls, _clone_with({"m": "https://evil.example?@github.com/x.git"})))

    result = _run(state, url="https://github.com/mom-ocean/MOM6.git",
                  kind="git", recursive=True, destination="mom6-laundered-sub")

    assert result["status"] == "error", result
    assert len(_network_calls(calls)) == 1, "只允许 clone 联网，submodule update 不得发出"


def test_a_redirect_hop_is_judged_by_its_real_host_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纵深防御：curl 8.x 会把 Location 规范成 https://evil.example/?@…，真实 curl 下这一跳
    本来就拦得住；这里钉的是判定函数本身不依赖 curl 替它规范化。"""
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    source = "https://github.com/o/r/releases/download/v1/x.tar.gz"
    calls: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait", _redirecting_spawn(
        calls, {source: "https://evil.example?@github.com/x.tar.gz"}, b"never"))

    result = _run(state, url=source, kind="file", destination="x.tar.gz")

    assert result["status"] == "error", result
    assert len(calls) == 1, [call[-1] for call in calls]


# ── 撞墙循环：照指引走必须走得出去（不依赖 Core #1068 修没修）──────────────────


class _FollowsTheGuidance:
    """完全照工具返回办事：给了申请调用就申请，否则记下这句话、换下一个 URL。"""

    def __init__(self, urls: list[str], limit: int = 12) -> None:
        self.urls = list(urls)
        self.limit = limit
        self.calls = 0
        self.final_errors: list[str] = []

    @staticmethod
    def _call(call_id: str, name: str, arguments: dict) -> LLMResponse:
        return LLMResponse(
            content="",
            tool_calls=[{
                "id": call_id,
                "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(arguments, ensure_ascii=False)},
            }],
            finish_reason="tool_calls",
            usage={"total_tokens": 1},
        )

    async def chat(self, messages, **_kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls > self.limit or not self.urls:
            return LLMResponse(content="停", tool_calls=[], finish_reason="stop",
                               usage={"total_tokens": 1})
        last = messages[-1]
        if last.role == "tool" and last.name == "fetch_resource":
            body = json.loads(last.content)
            request = body.get("request_network_access")
            if request:
                return self._call(f"req{self.calls}", "request_network_access",
                                  request["arguments"])
            self.final_errors.append(str(body.get("error") or ""))
            self.urls.pop(0)
            if not self.urls:
                return LLMResponse(content="停", tool_calls=[], finish_reason="stop",
                                   usage={"total_tokens": 1})
        return self._call(f"fetch{self.calls}", "fetch_resource", {"url": self.urls[0]})


@pytest.fixture
def _pause_registry():
    clear_all()
    yield
    clear_all()


def _drive(state, llm, answer: str) -> int:
    from core.agent_loop import resume_loop, run_loop

    async def drive() -> int:
        result = await run_loop(load_harness("experiment"), state,
                                [LLMMessage(role="user", content="取数据")], llm)
        cards = 0
        while result.status == "paused" and cards < 4:
            cards += 1
            ctx = get_paused_run(state.run_id)
            text = answer
            if answer == "__unattended__":
                # 自主/连续档与孤儿 pause 收尾用的同一个 Core 自动作答
                from core.pause_driver import auto_approve_answer
                text = auto_approve_answer(ctx.pause_event)
            result = await resume_loop(ctx, text)
        return cards

    return asyncio.run(drive())


@pytest.mark.parametrize("answer", ["允许", "拒绝", "__unattended__"])
def test_an_answer_that_did_not_take_effect_does_not_bring_the_same_card_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _pause_registry, answer: str,
) -> None:
    """「允许」但授权没生效（今天的 #1068；以后也可能是进程重启、无人值守自动作答），
    或者「拒绝」：原样重试都不能再叫模型申请同一主机，要给 report_blocker 出口。"""
    bootstrap(force=True)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    # 钉住「授权未生效」，不依赖 Core 修没修 #1068。
    monkeypatch.setattr(capability_grants, "is_granted", lambda *_a, **_k: False)
    state = _state(tmp_path)
    spawned: list = []
    # bootstrap 之后工具注册表执行的是 `tools.resource_fetch`（同一文件的第二个模块对象），
    # 只桩 nodes.experiment.tools.resource_fetch 挡不住真实 run_loop 里的 curl。
    live = importlib.import_module("tools.resource_fetch")
    assert Path(live.__file__).resolve() == Path(fetch.__file__).resolve()
    for module in {fetch, live}:
        monkeypatch.setattr(module, "spawn_and_wait", _must_not_spawn(spawned))
    host = "downloads.psl.noaa.gov"
    llm = _FollowsTheGuidance([f"https://{host}/Datasets/x.nc"])

    cards = _drive(state, llm, answer)

    assert spawned == []
    assert cards == 1, f"同一主机的授权卡弹了 {cards} 次：照指引重试又被叫去申请"
    assert llm.final_errors, "重试之后工具没有给出不再申请的出口"
    final = llm.final_errors[-1]
    assert "request_network_access(" not in final, final
    assert "report_blocker" in final, final


def test_a_grant_that_did_take_effect_is_not_blocked_by_the_earlier_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防过度修：Core 修好 #1068 以后，申请过且批下来的主机必须照常联网。"""
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    state = _state(tmp_path)
    host = "downloads.psl.noaa.gov"
    state.append_transcript("tool_call", turn=2, name="request_network_access",
                            args={"host": host, "reason": "x"})
    assert grant_from_answer(state, host, "允许", reason="x")
    calls: list = []
    payload = b"netcdf\n"
    monkeypatch.setattr(fetch, "spawn_and_wait", _redirecting_spawn(calls, {}, payload))

    result = _run(state, url=f"https://{host}/Datasets/x.nc", kind="file", destination="x.nc")

    assert result["status"] == "success", result
    assert len(calls) == 1


def test_a_prior_request_for_one_host_does_not_silence_the_request_for_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    state = _state(tmp_path)
    spawned: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait", _must_not_spawn(spawned))
    state.append_transcript("tool_call", turn=2, name="request_network_access",
                            args={"host": "downloads.psl.noaa.gov", "reason": "x"})

    result = _run(state, url="https://www.ncei.noaa.gov/data/x.nc", kind="file")

    assert spawned == []
    assert result["error_code"] == "network_access_required", result
    assert result["request_network_access"]["arguments"]["host"] == "www.ncei.noaa.gov"
