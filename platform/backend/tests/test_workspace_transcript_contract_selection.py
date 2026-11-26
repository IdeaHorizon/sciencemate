"""Backend imports stand alone; workspace codecs come from the selected runtime."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _run(tmp_path, code, *args):
    return subprocess.run(
        [sys.executable, "-I", "-c", code, str(BACKEND), *map(str, args)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )


def test_backend_can_import_the_reader_without_a_harness_on_its_import_path(tmp_path):
    result = _run(tmp_path, """
import sys
sys.path.insert(0, sys.argv[1])
from app.config import settings
settings.harness_root = ''
assert 'core' not in sys.modules
from app.services import harness_transcript_ingest
assert 'core' not in sys.modules, 'importing the backend loaded a runtime before it was selected'
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_reader_budget_and_codec_use_the_selected_runtime_even_after_a_wrong_core_import(tmp_path):
    roots = []
    for name, budget in (("wrong", 10000), ("selected", 100)):
        root = tmp_path / name
        core = root / "core"
        core.mkdir(parents=True)
        (core / "__init__.py").write_text("")
        (core / "agent_loop.py").write_text("")
        (core / "workspace_transcript.py").write_text(
            f"MAX_TRANSCRIPT_RECORD_BYTES = {budget}\n"
            "def restore_workspace_record(record, path):\n"
            f"    return {{**record, 'codec': '{name}'}}\n"
        )
        roots.append(root)
    path = tmp_path / "transcript.jsonl"
    path.write_text("{}\n")
    result = _run(tmp_path, """
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
import core.workspace_transcript
assert core.workspace_transcript.MAX_TRANSCRIPT_RECORD_BYTES == 10000
sys.path.insert(0, sys.argv[1])
from app.config import settings
settings.harness_root = sys.argv[3]
from app.services import harness_contract, harness_transcript_ingest as reader
harness_contract._harness_root.cache_clear()
codec = harness_contract.workspace_transcript_module()
assert Path(codec.__file__).resolve() == Path(sys.argv[3], 'core/workspace_transcript.py').resolve()
assert codec.restore_workspace_record({}, None)['codec'] == 'selected'
reader._approved_runtime_root = lambda *args: Path(sys.argv[4]).parent
class Service:
    async def ingest_raw_record(self, db, **kwargs):
        assert kwargs['raw']['codec'] == 'selected'
async def adapter(*args, **kwargs):
    return {}
reader._adapter_state_for = adapter
native = asyncio.run(reader.ingest_transcript_wrapper(
    None, service=Service(), context=SimpleNamespace(project_id='p', session_id='s'),
    event={'transcript_path': sys.argv[4], 'byte_start': 0, 'byte_end': 3}, harness_states={}))
assert native['codec'] == 'selected'
try:
    reader.resolve_wrapper({'transcript_path': sys.argv[4], 'byte_start': 0, 'byte_end': 101},
                           project_id='p', session_id='s')
except RuntimeError as exc:
    assert 'maximum=100' in str(exc), str(exc)
else:
    raise AssertionError('reader used the wrong runtime budget')
""", *roots, path)
    assert result.returncode == 0, result.stdout + result.stderr
