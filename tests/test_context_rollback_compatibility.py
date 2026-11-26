"""Use the frozen deployed reader, not a reimplementation of its format.

Fixture: ec142279ea0f1c92ee0431e58b5bab6281a6c7f3:core/context_view.py.
Keep it immutable while this release remains a supported rollback target.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types

import pytest

from core import context_view as cv, summarizer as sm
from core.context_lifecycle import target_tokens
from core.conversation_store import (load_conversation, msg_to_jsonable,
                                     render_context_change, save_conversation)
from core.state import State
from tests.test_context_minimal_regressions import Summary, harness, history, read_all


FIXTURE = Path(__file__).parent / "fixtures/context_view_v1_ec142279.py.txt"


def deployed_reader():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "32c6fef569aa301d8c3928b386e4af0abf7e99d3704efd25dd642f6197ceeabb"
    module = types.ModuleType("deployed_context_view")
    exec(compile(raw, str(FIXTURE), "exec"), module.__dict__)
    return module.ContextView


@pytest.mark.asyncio
async def test_upgrade_compact_restart_and_rollback_read_exact_history(tmp_path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    old = deployed_reader()(state)
    receipt = '{"job_id":"existing-job", "pid":12345, "submission_count":1}'
    old.note_result(tool_name="submit_job", args={}, tool_call_id="job",
                    content=receipt, turn=0, replayable=False)
    old_ref = old.entries["job"]["result_ref"]
    assert old_ref.startswith("v1:")
    assert cv.ContextView(state).read_result(old_ref)["content"] == receipt
    references = {old_ref: receipt}
    messages = history()
    for turn in (21, 42, 63):
        exact = json.dumps([msg_to_jsonable(m) for m in messages], ensure_ascii=False)
        result = await sm.run_summarizer(harness(), state, messages, Summary(),
                                         turn, sm.estimate_tokens(messages))
        outcome = state.hook_state["_context_compaction"]
        assert outcome["target_met"]
        references[outcome["history_ref"]] = exact
        render_context_change(state, messages, result, turn=turn, source="rollback_test")
        save_conversation(state, messages)
        state = State.reopen("literature", state.root.parent, state.run_id)
        restored = load_conversation(state)
        assert restored is not None
        view = cv.ContextView(state)
        index_ref = view.history_index_ref()
        index_body = read_all(view, index_ref)
        assert len(json.loads(index_body)) == len(references) - 1
        assert any(index_ref in (m.content or "") for m in restored)
        references[index_ref] = index_body
        # Restart does not append a second catalogue for the same archives.
        before = view._log().stat().st_size
        assert cv.ContextView(state).history_index_ref() == index_ref
        assert view._log().stat().st_size == before
        # Each catalogue itself is a normal v1 receipt, including its entries.
        for ref, content in references.items():
            assert ref.startswith("v1:")
            assert read_all(deployed_reader()(state), ref) == content
        references.pop(index_ref)
        messages = restored + history(f"next-{turn}-")[2:]

    # A separate process running the actual old reader can recover every byte.
    payload = {"root":str(state.root), "refs":references}
    check = subprocess.run([sys.executable, "-c", """
import json, sys, types
from pathlib import Path
data=json.load(sys.stdin)
module=types.ModuleType('rollback_reader')
exec(compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec'), module.__dict__)
view=module.ContextView(types.SimpleNamespace(root=Path(data['root'])))
for ref, expected in data['refs'].items():
    parts=[]; offset=0
    while True:
        page=view.read_result(ref, offset=offset, limit=701)
        parts.append(page['content'])
        if page['next_offset'] is None: break
        offset=page['next_offset']
    assert ''.join(parts)==expected
print(len(data['refs']))
""", str(FIXTURE)], input=json.dumps(payload), text=True,
        capture_output=True, timeout=20, check=True)
    assert int(check.stdout) == 4
    # Reading archives never submits or dispatches the original effectful tool.
    records = [json.loads(line) for line in cv.ContextView(state)._log().read_text().splitlines()]
    assert sum(r["key"] == "submit_job|{}" for r in records) == 1


def test_prompt_degradation_ceiling_does_not_reserve_output_twice(tmp_path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    state.hook_state[sm.OBSERVED_CEILING_KEY] = 5000
    h = harness(window=120000)
    assert target_tokens(h, state) == int(3500 / sm.effective_calibration(state))


@pytest.mark.asyncio
async def test_catalogue_write_failure_keeps_the_direct_archive_readable(tmp_path, monkeypatch):
    state = State.new(node_type="literature", base_dir=tmp_path)
    original = cv.ContextView._append_log

    def append(self, **kwargs):
        if kwargs["key"] == "context_history_index|{}":
            return None
        return original(self, **kwargs)

    monkeypatch.setattr(cv.ContextView, "_append_log", append)
    messages = history()
    result = await sm.run_summarizer(harness(), state, messages, Summary(),
                                     21, sm.estimate_tokens(messages))
    reference = state.hook_state["_context_compaction"]["history_ref"]
    assert read_all(deployed_reader()(state), reference)
    assert any(reference in (m.content or "") for m in result)
    assert not any('result_ref="None"' in (m.content or "") for m in result)
