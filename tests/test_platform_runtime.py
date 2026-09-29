"""Offline contract tests for the App Server harness bridge."""

from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.llm import LLMResponse
from core.pause import clear_all
from platform_runtime import (
    RequestError,
    SecretFilter,
    run_platform_request,
    serve_jsonl,
    validate_request,
)
def _write_instruction_files(
    home_dir: Path,
    project_id: str,
    *,
    canary_suffix: str = "V1",
) -> None:
    """把指令写成 worker 真正会读的那几个文件。

    从前这里造的是一份 `instruction_snapshot` 字典，随 init 请求下发、由
    `platform_runtime` 校验后落成三个只读文件。那条路是同一件事的第二套实现
    （RFC X3 删掉了它）—— 现在测试写的就是 `core.directives_loader` 读的那几个
    文件，中间没有第二份抄件可以和它分叉。
    """
    user_dir = home_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "PROFILE.md").write_text(
        f"# PROFILE.md\n\nPERSONAL-CANARY-{canary_suffix}\n", encoding="utf-8"
    )
    (user_dir / "RESEARCH_SETTINGS.md").write_text(
        f"SETTINGS-CANARY-{canary_suffix}\n", encoding="utf-8"
    )
    project_dir = home_dir / "projects" / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "PROJECT.md").write_text(
        f"# PROJECT.md\n\nPROJECT-CANARY-{canary_suffix}\n", encoding="utf-8"
    )


class _FakeLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.seen = []
        self.stream_display = None

    async def chat(self, messages, **kwargs):
        self.seen.append((list(messages), kwargs))
        response = self.responses.pop(0)
        if self.stream_display and response.content:
            midpoint = max(1, len(response.content) // 2)
            self.stream_display(response.content[:midpoint])
            self.stream_display(response.content[midpoint:])
            self.stream_display(None)
        return response


@pytest.fixture(autouse=True)
def _clear_runtime_registries():
    from core.pause_driver import set_auto_approve
    from shared.lib.dangerous_commands import set_bypass_mode

    clear_all()
    set_auto_approve(False)
    set_bypass_mode(False)
    yield
    clear_all()
    set_auto_approve(False)
    set_bypass_mode(False)


def _request(tmp_path: Path, **overrides):
    request = {
        "request_id": "req-test-1",
        "project_id": "project-alpha",
        "message": "请简短确认平台桥工作正常。",
        "home_dir": str(tmp_path / "isolated-home"),
    }
    request.update(overrides)
    return request


def _runtime_process(*, base_url: str = "http://provider.invalid") -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "LLM_BASE_URL": base_url,
            "LLM_MODEL": "offline-init-only",
            "LLM_API_KEY": "sk-offline-init-only",
            # 端点是**故意**连不通的（`provider.invalid`）。不关重试的话，
            # 子进程会对着它走完整条重试阶梯（默认 3 次 / 60s 预算）。
            #
            # 本机 DNS 对 `.invalid` 秒失败，看不出问题；容器里要走完 resolver
            # 超时，于是整条阶梯超过 30s —— CI 上约一半概率红在
            # `did not receive event 'result'`，而子进程其实一直在正常干活
            # （诊断里能看到它发过 started + 6 条 transcript，stderr 是空的）。
            #
            # 这些用例验的是"边界送不送得到三层指令"，不是重试策略。
            "LLM_MAX_RETRIES": "0",
            "LLM_RETRY_BUDGET_SECONDS": "0",
            # 子进程等一次 LLM 响应的上限。默认 300s —— 比这些用例自己的
            # 等待上限（30s）大一个量级，于是"对面不答话"的表现是**装死**：
            # 测试到点放弃，现场只有"发过 started 然后一片死寂"，看不出是谁
            # 的问题。给个小上限，卡住时子进程会报错并如实发出 result。
            "LLM_TIMEOUT": "15",
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", "platform_runtime", "--serve"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _send_rpc(process: subprocess.Popen, request: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()


#: 等一个真子进程发出某个事件最多等多久。
#
# 给得足是**故意**的：这些用例验的是"边界送不送得到"，不是"多快送到"。等到了
# 就立刻返回，宽限在正常情况下一秒都不花；等不到才是真失败。
#
# 原来是 10s，按空闲开发机定的；四个 job 并行时整套 pytest 从 18s 涨到 117s，
# 不够用。但也别给太大：真失败时这段是白等的 —— 90s 试过一次，一条红把整片
# 从 20s 拖到 104s。30s 是负载下够用、失败时不肉疼的折中。
#
# ⚠️ 超时放宽**不能**当成修复。同期那次红的真因是 CI 镜像里留了个悬空的
# editable 指针（见 .gitea/Dockerfile.ci），子进程 import 不到 core 就废在
# 那儿 —— 那是 rc=None 的真正含义，不是机器慢。
_EVENT_TIMEOUT_S = 30.0


def _read_event(
    process: subprocess.Popen,
    event_type: str,
    *,
    timeout: float = _EVENT_TIMEOUT_S,
) -> dict:
    assert process.stdout is not None
    events = getattr(process, "_test_events", None)
    if events is None:
        events = queue.Queue()
        process._test_events = events
        def read_lines():
            for line in process.stdout:
                events.put(line)
            events.put(None)
        threading.Thread(target=read_lines, daemon=True).start()
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = events.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            break
        event = json.loads(line)
        seen.append(json.dumps(event, ensure_ascii=False)[:400])
        if event.get("type") == event_type:
            return event
    # 进程还活着时原来打不出 stderr（`process.stderr.read()` 会阻塞），于是
    # 失败信息只剩一句 `rc=None`：进程活着、没发事件、不知道为什么。
    #
    # 2026-08-19 我为此猜了两次都错（先当负载超时把 10s 放宽到 90s，再当成 CI
    # 镜像里的悬空 editable 指针），两次都是因为现场只有 `rc=None`。所以这里
    # **先杀掉再读** —— 杀了 stderr 才读得到，而进程本来也要收掉。
    alive = process.poll() is None
    if alive:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    stderr = ""
    if process.stderr is not None:
        try:
            stderr = process.stderr.read() or ""
        except Exception as exc:      # noqa: BLE001 - 诊断路径不许再抛
            stderr = f"(读 stderr 失败: {exc})"
    raise AssertionError(
        f"did not receive event {event_type!r} within {timeout}s; "
        f"进程当时{'还活着（已杀）' if alive else '已退出'}，rc={process.poll()}；"
        "收到过的事件：\n" + ("\n".join(seen) if seen else "(一个都没有)")
        + f"\nstderr(尾部)=\n{stderr[-3000:]}"
    )


def _stop_runtime(process: subprocess.Popen, request_id: str) -> None:
    if process.poll() is None:
        _send_rpc(
            process,
            {"op": "terminate", "request_id": request_id},
        )
        _read_event(process, "terminated")
    process.wait(timeout=_EVENT_TIMEOUT_S)   # 同上：收尾也不该在负载下变判据


@pytest.mark.asyncio
async def test_request_runs_real_orchestrator_and_returns_structured_result(
    tmp_path: Path,
):
    llm = _FakeLLM(
        [
            LLMResponse(
                content="平台桥工作正常。",
                tool_calls=[],
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            ),
        ]
    )
    events: list[dict] = []

    def emit(event_type: str, **payload):
        events.append({"type": event_type, **payload})

    result = await run_platform_request(_request(tmp_path), emit, llm=llm)

    assert result["status"] == "completed"
    assert result["run_id"] == "orchestrator__project-alpha"
    assert result["final_text"] == "平台桥工作正常。"
    assert result["tokens_used"] == 14
    assert result["tokens_used_delta"] == 14
    assert result["artifact_paths"] == []

    transcript = Path(result["transcript_path"])
    conversation = Path(result["conversation_path"])
    assert transcript.is_file()
    assert conversation.is_file()
    native_events = [
        json.loads(line)["event"] for line in transcript.read_text(encoding="utf-8").splitlines()
    ]
    assert "platform_request_start" in native_events
    assert "llm_request" in native_events
    assert "llm_response" in native_events
    assert "platform_request_end" in native_events
    assert any(e["type"] == "started" for e in events)
    assert any(e["type"] == "transcript" for e in events)
    assert events[-1]["type"] == "result"

    saved = json.loads(conversation.read_text(encoding="utf-8"))
    assert any(
        m.get("role") == "user" and "平台桥工作正常" in (m.get("content") or "")
        for m in saved["messages"]
    )
    assert any(
        m.get("role") == "assistant" and m.get("content") == "平台桥工作正常。"
        for m in saved["messages"]
    )


@pytest.mark.asyncio
async def test_direct_result_delta_excludes_restored_session_usage(tmp_path: Path):
    first = await run_platform_request(
        _request(tmp_path, request_id="direct-first"),
        lambda *args, **kwargs: None,
        llm=_FakeLLM(
            [
                LLMResponse(
                    content="第一轮完成。",
                    tool_calls=[],
                    finish_reason="stop",
                    usage={"total_tokens": 11},
                )
            ]
        ),
    )
    second = await run_platform_request(
        _request(tmp_path, request_id="direct-second", message="继续第二轮"),
        lambda *args, **kwargs: None,
        llm=_FakeLLM(
            [
                LLMResponse(
                    content="第二轮完成。",
                    tool_calls=[],
                    finish_reason="stop",
                    usage={"total_tokens": 4},
                )
            ]
        ),
    )

    assert first["tokens_used"] == 11
    assert first["tokens_used_delta"] == 11
    assert second["tokens_used"] == 15
    assert second["tokens_used_delta"] == 4


@pytest.mark.asyncio
async def test_pause_is_returned_not_read_from_stdin(tmp_path: Path):
    pause_call = {
        "id": "call_pause_1",
        "type": "function",
        "function": {
            "name": "request_human_input",
            "arguments": json.dumps(
                {
                    "question": "选择研究范围",
                    "context": "需要人类确定边界",
                    "options": ["窄范围", "宽范围"],
                    # 新契约：给 options 必须给推荐项（无人值守按它作答）
                    "recommended_option_index": 0,
                },
                ensure_ascii=False,
            ),
        },
    }
    llm = _FakeLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[pause_call],
                finish_reason="tool_calls",
                usage={"total_tokens": 9},
            ),
        ]
    )
    events: list[dict] = []

    def emit(event_type: str, **payload):
        events.append({"type": event_type, **payload})

    result = await run_platform_request(_request(tmp_path), emit, llm=llm)

    assert result["status"] == "paused"
    assert result["final_text"] == ""
    assert result["pause_event"]["question"] == "选择研究范围"
    assert result["pause_event"]["options"] == ["窄范围", "宽范围"]
    assert Path(result["pause_pending_path"]).is_file()
    assert any(e["type"] == "pause_required" for e in events)
    assert events[-1]["type"] == "result"


def test_validation_requires_isolated_absolute_home(tmp_path: Path):
    with pytest.raises(RequestError, match="home_dir"):
        validate_request(_request(tmp_path, home_dir="relative/home"))
    with pytest.raises(RequestError, match="project_id"):
        validate_request(_request(tmp_path, project_id="../escape"))


def test_secret_filter_never_emits_provider_credentials():
    secret = "sk-test-super-secret"
    clean = SecretFilter([secret]).value(
        {
            "message": f"provider failed with {secret}",
            "api_key": secret,
            "nested": {"authorization": f"Bearer {secret}"},
        }
    )
    rendered = json.dumps(clean)
    assert secret not in rendered
    assert rendered.count("REDACTED") >= 3


@pytest.mark.asyncio
async def test_provider_key_is_process_local_not_in_tool_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "sk-runtime-only-secret"
    alternate_secret = "alternate-provider-secret"
    hook_secret = "run-end-hook-secret"
    monkeypatch.setenv("LLM_API_KEY", secret)
    monkeypatch.setenv("ALT_PROVIDER_API_KEY", alternate_secret)
    monkeypatch.setenv("HARNESS_RUN_END_HOOK_API_KEY", hook_secret)
    monkeypatch.setenv(
        "LLM_PROVIDERS_JSON",
        json.dumps(
            [
                {
                    "name": "alternate",
                    "model": "alternate-model",
                    "base_url": "http://provider.invalid",
                    "api_key_env": "ALT_PROVIDER_API_KEY",
                }
            ]
        ),
    )

    class _InspectingLLM:
        async def chat(self, messages, **kwargs):
            from core.runtime_secrets import get as runtime_secret

            from core.llm import LLMClient

            assert os.getenv("LLM_API_KEY") is None
            assert os.getenv("ALT_PROVIDER_API_KEY") is None
            assert os.getenv("HARNESS_RUN_END_HOOK_API_KEY") is None
            assert runtime_secret("ALT_PROVIDER_API_KEY") == alternate_secret
            assert runtime_secret("HARNESS_RUN_END_HOOK_API_KEY") == hook_secret
            child_client = LLMClient(model="m", base_url="http://provider.invalid")
            assert child_client.api_key == secret
            return LLMResponse(
                content="secret isolation ok",
                tool_calls=[],
                finish_reason="stop",
                usage={"total_tokens": 1},
            )

    result = await run_platform_request(
        _request(tmp_path), lambda *a, **k: None, llm=_InspectingLLM()
    )
    assert result["status"] == "completed"
    assert os.getenv("LLM_API_KEY") == secret
    assert os.getenv("ALT_PROVIDER_API_KEY") == alternate_secret
    assert os.getenv("HARNESS_RUN_END_HOOK_API_KEY") == hook_secret


@pytest.mark.asyncio
async def test_serve_resumes_pause_in_memory_then_accepts_second_turn(tmp_path: Path):
    pause_call = {
        "id": "call_pause_rpc_1",
        "type": "function",
        "function": {
            "name": "request_human_input",
            "arguments": json.dumps(
                {
                    "question": "选择研究范围",
                    "context": "需要人类确定边界",
                    "options": ["窄范围", "宽范围"],
                    # 新契约：给 options 必须给推荐项（无人值守按它作答）
                    "recommended_option_index": 0,
                },
                ensure_ascii=False,
            ),
        },
    }
    llm = _FakeLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[pause_call],
                finish_reason="tool_calls",
                usage={"total_tokens": 9},
            ),
            LLMResponse(
                content="已按窄范围继续并完成。",
                tool_calls=[],
                finish_reason="stop",
                usage={"total_tokens": 7},
            ),
            LLMResponse(
                content="第二轮也完成了。",
                tool_calls=[],
                finish_reason="stop",
                usage={"total_tokens": 5},
            ),
        ]
    )
    _write_instruction_files(tmp_path / "isolated-home", "project-rpc")
    requests = [
        {
            "op": "init",
            "request_id": "init-1",
            "tenant_id": "tenant-test",
            "project_id": "project-rpc",
            "session_id": "session-rpc",
            "home_dir": str(tmp_path / "isolated-home"),
        },
        {"op": "turn", "request_id": "turn-1", "message": "开始研究"},
        {
            "op": "answer",
            "request_id": "answer-1",
            "pause_id": "call_pause_rpc_1",
            "answer": "窄范围",
        },
        {"op": "turn", "request_id": "turn-2", "message": "再汇报一次"},
        {"op": "terminate", "request_id": "terminate-1"},
    ]
    stream = io.StringIO(
        "".join(json.dumps(request, ensure_ascii=False) + "\n" for request in requests)
    )
    events: list[dict] = []

    def emit(event_type: str, **payload):
        events.append({"type": event_type, **payload})

    await serve_jsonl(stream, emit, llm=llm)

    assert events[0]["type"] == "ready"
    assert events[0]["request_id"] == "init-1"
    assert Path(events[0]["runtime_root"]).as_posix().endswith("/projects/project-rpc/sessions/session-rpc/runs")
    default_marker = json.loads(
        (Path(events[0]["runtime_root"]) / ".platform-runtime-identity.json").read_text(
            encoding="utf-8"
        )
    )
    assert default_marker["session_id"] == "session-rpc"
    results = {event["request_id"]: event["data"] for event in events if event["type"] == "result"}
    assert results["turn-1"]["status"] == "paused"
    assert results["turn-1"]["tokens_used"] == 9
    assert results["turn-1"]["tokens_used_delta"] == 9
    assert results["turn-1"]["pause_id"] == "call_pause_rpc_1"
    assert results["turn-1"]["pause_event"]["question"] == "选择研究范围"
    assert results["answer-1"]["status"] == "completed"
    assert results["answer-1"]["final_text"] == "已按窄范围继续并完成。"
    assert results["answer-1"]["tokens_used"] == 16
    assert results["answer-1"]["tokens_used_delta"] == 7
    assert results["turn-2"]["status"] == "completed"
    assert results["turn-2"]["final_text"] == "第二轮也完成了。"
    assert results["turn-2"]["tokens_used"] == 21
    assert results["turn-2"]["tokens_used_delta"] == 5
    assert events[-1]["type"] == "terminated"
    assert events[-1]["request_id"] == "terminate-1"
    assert events[-1]["reason"] == "terminate"
    assert events[-1]["session_id"] == "session-rpc"
    assert all("request_id" in event for event in events)
    token_events = [event for event in events if event["type"] == "token_delta"]
    assert token_events
    assert all(event["request_id"] in {"turn-1", "answer-1", "turn-2"} for event in token_events)
    assert "".join(
        event["text"] for event in token_events if event["request_id"] == "turn-2"
    ) == "第二轮也完成了。"
    assert all("reasoning" not in event for event in token_events)
    for operation_request_id in ("turn-1", "answer-1", "turn-2"):
        end_index = next(
            index
            for index, event in enumerate(events)
            if event["type"] == "transcript"
            and event["event"].get("event") == "platform_request_end"
            and event["event"].get("request_id") == operation_request_id
        )
        result_index = next(
            index
            for index, event in enumerate(events)
            if event["type"] == "result" and event["request_id"] == operation_request_id
        )
        assert events[end_index]["request_id"] == operation_request_id
        assert end_index < result_index

    transcript = Path(results["turn-2"]["transcript_path"])
    native_events = [
        json.loads(line)["event"] for line in transcript.read_text(encoding="utf-8").splitlines()
    ]
    assert "loop_resume" in native_events
    assert "platform_session_end" in native_events
    conversation = json.loads(
        Path(results["turn-2"]["conversation_path"]).read_text(encoding="utf-8")
    )
    user_texts = [
        message.get("content")
        for message in conversation["messages"]
        if message.get("role") == "user"
    ]
    assert any("开始研究" in text for text in user_texts)
    assert any("再汇报一次" in text for text in user_texts)
    assert len(llm.seen) == 3
    system_message = next(message.content for message in llm.seen[0][0] if message.role == "system")
    # 指令**确实到达了模型**，而且到达的是文件里那几个字。组织层随它的零写
    # 入口一起删了（RFC X3），所以这里只剩两层 + 平台写的研究设置。
    for canary in (
        "PROJECT-CANARY-V1",
        "PERSONAL-CANARY-V1",
        "SETTINGS-CANARY-V1",
    ):
        assert canary in system_message
    assert "ORGANIZATION-CANARY" not in system_message


@pytest.mark.asyncio
async def test_serve_eof_saves_conversation_and_session_end(tmp_path: Path):
    llm = _FakeLLM(
        [
            LLMResponse(
                content="EOF 前已保存。",
                tool_calls=[],
                finish_reason="stop",
                usage={"total_tokens": 3},
            )
        ]
    )
    requests = [
        {
            "op": "init",
            "request_id": "init-eof",
            "tenant_id": "tenant-test",
            "project_id": "project-eof",
            "session_id": "session-eof",
            "home_dir": str(tmp_path / "isolated-home"),
        },
        {"op": "turn", "request_id": "turn-eof", "message": "保存后退出"},
    ]
    stream = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
    events: list[dict] = []

    await serve_jsonl(
        stream,
        lambda event_type, **payload: events.append({"type": event_type, **payload}),
        llm=llm,
    )

    result = next(event["data"] for event in events if event["type"] == "result")
    assert Path(result["conversation_path"]).is_file()
    native_records = [
        json.loads(line)
        for line in Path(result["transcript_path"]).read_text(encoding="utf-8").splitlines()
    ]
    session_end = next(
        record for record in native_records if record["event"] == "platform_session_end"
    )
    assert session_end["reason"] == "eof"


@pytest.mark.asyncio
async def test_serve_answer_uses_pause_cas_and_rejects_request_replay(tmp_path: Path):
    def pause_response(call_id: str, question: str) -> LLMResponse:
        return LLMResponse(
            content=None,
            tool_calls=[
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "request_human_input",
                        "arguments": json.dumps({"question": question}),
                    },
                }
            ],
            finish_reason="tool_calls",
            usage={"total_tokens": 2},
        )

    llm = _FakeLLM(
        [
            pause_response("pause-one", "第一个问题"),
            pause_response("pause-two", "第二个问题"),
            LLMResponse(
                content="两个问题都已处理。",
                tool_calls=[],
                finish_reason="stop",
                usage={"total_tokens": 2},
            ),
        ]
    )
    requests = [
        {
            "op": "init",
            "request_id": "cas-init",
            "tenant_id": "tenant-test",
            "project_id": "project-cas",
            "session_id": "session-cas",
            "home_dir": str(tmp_path / "isolated-home"),
        },
        {"op": "turn", "request_id": "cas-turn", "message": "开始"},
        {
            "op": "answer",
            "request_id": "wrong-answer",
            "pause_id": "not-current",
            "answer": "错误目标",
        },
        {
            "op": "answer",
            "request_id": "answer-one",
            "pause_id": "pause-one",
            "answer": "第一答复",
        },
        {
            "op": "answer",
            "request_id": "answer-one",
            "pause_id": "pause-one",
            "answer": "重放不得执行",
        },
        {
            "op": "answer",
            "request_id": "stale-answer",
            "pause_id": "pause-one",
            "answer": "旧问题答案",
        },
        {
            "op": "answer",
            "request_id": "answer-two",
            "pause_id": "pause-two",
            "answer": "第二答复",
        },
        {"op": "terminate", "request_id": "cas-terminate"},
    ]
    events: list[dict] = []

    await serve_jsonl(
        io.StringIO("".join(json.dumps(request) + "\n" for request in requests)),
        lambda event_type, **payload: events.append({"type": event_type, **payload}),
        llm=llm,
    )

    errors = {event["request_id"]: event for event in events if event["type"] == "error"}
    assert errors["wrong-answer"]["code"] == "pause_conflict"
    assert errors["wrong-answer"]["details"]["current_pause_id"] == "pause-one"
    assert errors["answer-one"]["code"] == "duplicate_request_id"
    assert errors["stale-answer"]["code"] == "pause_conflict"
    assert errors["stale-answer"]["details"]["current_pause_id"] == "pause-two"
    results = {event["request_id"]: event["data"] for event in events if event["type"] == "result"}
    assert results["answer-one"]["status"] == "paused"
    assert results["answer-one"]["pause_id"] == "pause-two"
    assert results["answer-two"]["status"] == "completed"
    assert results["answer-two"]["final_text"] == "两个问题都已处理。"
    assert len(llm.seen) == 3


@pytest.mark.asyncio
async def test_serve_drains_oversized_line_before_next_rpc():
    oversized = '{"op":"turn","request_id":"too-big","message":"' + ("x" * 1_048_576) + '"}\n'
    terminate = json.dumps({"op": "terminate", "request_id": "after-too-big"}) + "\n"
    events: list[dict] = []

    await serve_jsonl(
        io.StringIO(oversized + terminate),
        lambda event_type, **payload: events.append({"type": event_type, **payload}),
        llm=_FakeLLM([]),
    )

    assert events[0]["code"] == "request_too_large"
    assert events[-1] == {
        "type": "terminated",
        "request_id": "after-too-big",
        "reason": "terminate",
    }


@pytest.mark.asyncio
async def test_child_state_and_transcript_inherit_session_identity(tmp_path: Path):
    from core.executor import execute_node
    from core.harness import NodeHarness
    from core.state import State

    parent = State.new(
        node_type="_orchestrator",
        base_dir=tmp_path / "parent",
        tenant_id="tenant-child",
        session_id="session-child",
    )
    harness = NodeHarness(
        node_type="literature",
        system_prompt="test",
        tools=[],
        max_turns=2,
        required_outputs=[],
        required_output_artifact_types=[],
    )
    llm = _FakeLLM(
        [
            LLMResponse(
                content="child complete",
                tool_calls=[],
                finish_reason="stop",
                usage={"total_tokens": 1},
            )
        ]
    )

    summary = await execute_node(
        node_type="literature",
        state_dir=tmp_path / "children",
        harness_override=harness,
        parent_state=parent,
        llm=llm,
    )

    assert summary["tenant_id"] == "tenant-child"
    assert summary["session_id"] == "session-child"
    records = [
        json.loads(line)
        for line in (Path(summary["state_dir"]) / "transcript.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records
    assert {record["tenant_id"] for record in records} == {"tenant-child"}
    assert {record["session_id"] for record in records} == {"session-child"}


def test_two_sessions_in_same_project_run_in_parallel_without_state_leakage(
    tmp_path: Path,
):
    process_a = _runtime_process()
    process_b = _runtime_process()
    state_a = tmp_path / "runtime-a"
    state_b = tmp_path / "runtime-b"
    init_a = {
        "op": "init",
        "request_id": "init-a",
        "tenant_id": "tenant-shared",
        "project_id": "project-shared",
        "session_id": "session-a",
        "home_dir": str(tmp_path / "home-a"),
        "state_dir": str(state_a),
    }
    init_b = {
        **init_a,
        "request_id": "init-b",
        "session_id": "session-b",
        "home_dir": str(tmp_path / "home-b"),
        "state_dir": str(state_b),
    }
    try:
        _send_rpc(process_a, init_a)
        _send_rpc(process_b, init_b)
        ready_a = _read_event(process_a, "ready")
        ready_b = _read_event(process_b, "ready")

        assert process_a.poll() is None
        assert process_b.poll() is None
        assert ready_a["project_id"] == ready_b["project_id"] == "project-shared"
        assert ready_a["session_id"] == "session-a"
        assert ready_b["session_id"] == "session-b"
        assert ready_a["run_id"] != ready_b["run_id"]
        assert ready_a["runtime_root"] == str(state_a.resolve())
        assert ready_b["runtime_root"] == str(state_b.resolve())

        root_a = Path(ready_a["state_dir"])
        root_b = Path(ready_b["state_dir"])
        assert root_a.parent == state_a.resolve()
        assert root_b.parent == state_b.resolve()
        assert root_a != root_b
        assert (root_a / "artifacts").is_dir()
        assert (root_b / "artifacts").is_dir()
        assert root_a / "pause_pending.json" != root_b / "pause_pending.json"
        assert root_a / "conversation.json" != root_b / "conversation.json"

        marker_a = json.loads(
            (state_a / ".platform-runtime-identity.json").read_text(encoding="utf-8")
        )
        marker_b = json.loads(
            (state_b / ".platform-runtime-identity.json").read_text(encoding="utf-8")
        )
        assert marker_a["session_id"] == "session-a"
        assert marker_b["session_id"] == "session-b"
        assert (
            json.loads(
                (tmp_path / "home-a" / ".platform-tenant-identity.json").read_text(encoding="utf-8")
            )["tenant_id"]
            == "tenant-shared"
        )

        transcript_a = [
            json.loads(line)
            for line in Path(ready_a["transcript_path"]).read_text(encoding="utf-8").splitlines()
        ]
        transcript_b = [
            json.loads(line)
            for line in Path(ready_b["transcript_path"]).read_text(encoding="utf-8").splitlines()
        ]
        assert transcript_a and transcript_b
        assert {record["session_id"] for record in transcript_a} == {"session-a"}
        assert {record["session_id"] for record in transcript_b} == {"session-b"}
    finally:
        _stop_runtime(process_a, "terminate-a")
        _stop_runtime(process_b, "terminate-b")

    assert (root_a / "conversation.json").is_file()
    assert (root_b / "conversation.json").is_file()
    conversation_a = json.loads((root_a / "conversation.json").read_text(encoding="utf-8"))
    conversation_b = json.loads((root_b / "conversation.json").read_text(encoding="utf-8"))
    assert conversation_a["session_id"] == "session-a"
    assert conversation_b["session_id"] == "session-b"

    wrong_identity = _runtime_process()
    try:
        _send_rpc(
            wrong_identity,
            {
                **init_a,
                "request_id": "wrong-identity-init",
                "session_id": "session-c",
            },
        )
        conflict = _read_event(wrong_identity, "error")
        assert conflict["code"] == "runtime_identity_conflict"
    finally:
        _stop_runtime(wrong_identity, "wrong-identity-stop")


def test_real_subprocess_llm_boundary_receives_three_instruction_layers(
    tmp_path: Path,
) -> None:
    requests_seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            size = int(self.headers.get("Content-Length", "0"))
            requests_seen.append(json.loads(self.rfile.read(size)))
            payload = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "boundary ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 2,
                        "total_tokens": 9,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = _runtime_process(base_url=f"http://127.0.0.1:{server.server_address[1]}")
    _write_instruction_files(tmp_path / "home", "project-boundary")
    try:
        _send_rpc(
            process,
            {
                "op": "init",
                "request_id": "boundary-init",
                "tenant_id": "tenant-boundary",
                "project_id": "project-boundary",
                "session_id": "session-boundary",
                "home_dir": str(tmp_path / "home"),
                "state_dir": str(tmp_path / "runtime"),
            },
        )
        ready = _read_event(process, "ready")
        _send_rpc(
            process,
            {"op": "turn", "request_id": "boundary-turn", "message": "run"},
        )
        result = _read_event(process, "result")
        assert result["data"]["final_text"] == "boundary ok"
        assert requests_seen
        system_content = next(
            message["content"]
            for message in requests_seen[0]["messages"]
            if message["role"] == "system"
        )
        for canary in ("PROJECT-CANARY-V1", "PERSONAL-CANARY-V1", "SETTINGS-CANARY-V1"):
            assert canary in system_content
        # 指令住在这个用户的 harness home 里，**不再**被复制成一份
        # `<state_dir>/.platform-instructions/` 只读副本 —— 那份副本是同一段
        # 文本的第三个落点，而三个落点就有三个各自过期的机会。
        assert (tmp_path / "home" / "user" / "PROFILE.md").is_file()
        assert not (Path(ready["runtime_root"]) / ".platform-instructions").exists()
    finally:
        if process.poll() is None:
            _stop_runtime(process, "boundary-stop")
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_same_session_has_one_process_mutation_lane(tmp_path: Path):
    init = {
        "op": "init",
        "request_id": "lane-owner-init",
        "tenant_id": "tenant-lane",
        "project_id": "project-lane",
        "session_id": "session-lane",
        "home_dir": str(tmp_path / "home"),
        "state_dir": str(tmp_path / "runtime"),
    }
    owner = _runtime_process()
    contender = _runtime_process()
    replacement: subprocess.Popen | None = None
    try:
        _send_rpc(owner, init)
        ready = _read_event(owner, "ready")
        assert ready["session_id"] == "session-lane"

        _send_rpc(
            contender,
            {**init, "request_id": "lane-contender-init"},
        )
        error = _read_event(contender, "error")
        assert error["code"] == "project_busy"
        assert error["session_id"] == "session-lane"
        assert owner.poll() is None

        _stop_runtime(contender, "lane-contender-stop")
        _stop_runtime(owner, "lane-owner-stop")
        saved_conversation = json.loads(
            (Path(ready["state_dir"]) / "conversation.json").read_text(encoding="utf-8")
        )
        assert saved_conversation["tenant_id"] == "tenant-lane"
        assert saved_conversation["session_id"] == "session-lane"

        replacement = _runtime_process()
        _send_rpc(
            replacement,
            {**init, "request_id": "lane-replacement-init"},
        )
        replacement_ready = _read_event(replacement, "ready")
        assert replacement_ready["run_id"] == ready["run_id"]
        assert replacement_ready["state_dir"] == ready["state_dir"]
        restored_records = [
            json.loads(line)
            for line in Path(replacement_ready["transcript_path"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert restored_records
        assert {record["tenant_id"] for record in restored_records} == {"tenant-lane"}
        assert {record["session_id"] for record in restored_records} == {"session-lane"}
    finally:
        if contender.poll() is None:
            _stop_runtime(contender, "lane-contender-finally")
        if owner.poll() is None:
            _stop_runtime(owner, "lane-owner-finally")
        if replacement is not None and replacement.poll() is None:
            _stop_runtime(replacement, "lane-replacement-stop")
