"""Cross-process record replacement must be readable and must not lose updates."""
import json
import os
import subprocess
import sys
from pathlib import Path

from shared.lib.json_file import read_object, write_object


def test_concurrent_readers_and_writers_keep_every_property(tmp_path):
    record = tmp_path / "record.json"
    write_object(record, {"initial": True})
    root = Path(__file__).resolve().parents[1]
    code = """
import sys
from pathlib import Path
from shared.lib.json_file import read_object, write_object
path, worker = Path(sys.argv[1]), sys.argv[2]
for n in range(60):
    write_object(path, {worker: n}, merge=True)
    assert read_object(path)["initial"] is True
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(record), str(worker)],
                                  cwd=root, env={**os.environ, "PYTHONPATH": str(root)},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for worker in range(4)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=45)
        assert process.returncode == 0, (stdout + stderr).decode("utf-8", "replace")
    assert read_object(record) == {"initial": True, **{str(n): 59 for n in range(4)}}
    assert not list(tmp_path.glob("*.tmp"))
