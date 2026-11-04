import base64
import hashlib
import json
import subprocess

import pytest

from core import remote_resources
from core.agent_loop import _persist_messages_checkpoint
from core.conversation_store import save_conversation
from core.llm import LLMMessage
from core.state import State
from shared.tools.library.remote_resources import remote_file


@pytest.mark.asyncio
async def test_download_preserves_project_write_layers(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
    (project / "project.yaml").write_text("schema_version: 2\n")
    (project / "notes").mkdir()
    (project / "paper").mkdir()
    subprocess.run(["git", "-C", str(project), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "Initialize",
        ],
        check=True,
        capture_output=True,
    )
    state = State.new(
        node_type="_orchestrator",
        project_id="p",
        project_worktree=project,
        base_dir=tmp_path / "runs",
    )
    content = b"downloaded bytes"
    calls = []

    async def handler(state, action, params):
        calls.append(action)
        return dict(
            status="success",
            resource_id="r",
            content_base64=base64.b64encode(content).decode(),
            sha256=hashlib.sha256(content).hexdigest(),
            bytes=len(content),
        )

    monkeypatch.setattr(remote_resources, "_handler", handler)
    result = await remote_file(
        state,
        action="download",
        resource_id="r",
        remote_path="result.bin",
        local_path="project/notes/result.bin",
    )
    assert result["status"] == "success", result
    assert (project / "notes/result.bin").read_bytes() == content
    for destination in (
        "project/paper/intrusion.bin",
        "project/.git/config",
        str(tmp_path / "outside.bin"),
    ):
        rejected = await remote_file(
            state,
            action="download",
            resource_id="r",
            remote_path="result.bin",
            local_path=destination,
        )
        assert rejected["status"] == "error", rejected
    assert calls == ["get"]


def test_password_arguments_are_redacted_before_execution_and_on_checkpoint(tmp_path):
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    password = "x"  # Typed fields must be redacted even for very short passwords.
    arguments = json.dumps({"action": "connect", "host": "example", "password": password})
    message = LLMMessage(
        role="assistant",
        tool_calls=[
            dict(id="1", type="function", function=dict(name="resource", arguments=arguments))
        ],
    )
    state.append_transcript(
        "llm_response",
        tool_calls_full=message.tool_calls,
        args_preview='{"password":"unfinished-secret',
    )
    save_conversation(state, [message])
    _persist_messages_checkpoint(state, [message], turn=1)
    for path in (
        state.transcript_path,
        state.root / "conversation.json",
        state.root / "messages_checkpoint.json",
    ):
        data = path.read_text()
        parsed = json.loads(data)
        call = (parsed.get("tool_calls_full") or parsed["messages"][0]["tool_calls"])[0]
        assert json.loads(call["function"]["arguments"])["password"] == "«REDACTED:PASSWORD»"
        assert "unfinished-secret" not in data
        assert "REDACTED:PASSWORD" in data
