"""Windows 启动器每打一个完整性标签都要记账（RFC_WORKER_START_IS_NOT_A_GATE §3.9，窄单 G）。

我们是整个系统里唯一会改用户目录权限的人，而 `_icacls` 从前 `check=False, capture_output` 连输出都扔了：
lujy 10-15 现场「权限怎么变了」三种可能没有一种能靠日志排除。现在每次调用记路径、改前、改后、
返回码、spawn token、pid；`run()` 从 spec["journal"] 拿账本位置，probe（临时目录）不记。

这里不跑真 icacls（CI 是 Linux）：把 `subprocess.run` 换成替身，验的是**账上有什么**。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.isolation import _win32_exec as launcher


class _FakeIcacls:
    """`icacls <path>` 回当前标签；`/setintegritylevel` 改标签并回 rc。"""

    def __init__(self, rc: int = 0) -> None:
        self.labels: dict[str, str] = {}
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        path = argv[1]
        if "/setintegritylevel" in argv:
            self.labels[path] = argv[-1]
            return subprocess.CompletedProcess(argv, self.rc, stdout=b"", stderr=b"" if self.rc == 0 else b"Access is denied.")
        label = self.labels.get(path)
        text = f"{path} Mandatory Label\\{'Low' if label and label.endswith('L') else 'Medium'} Mandatory Level:(OI)(CI)(NW)" if label else f"{path} BUILTIN\\Users:(F)"
        return subprocess.CompletedProcess(argv, 0, stdout=text.encode(), stderr=b"")


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "permissions.jsonl"
    monkeypatch.setattr(launcher, "_JOURNAL", str(path))
    monkeypatch.setenv("HARNESS_SPAWN_TOKEN", "deadbeefcafe")
    monkeypatch.setenv("HARNESS_RUN_ID", "run_journal_1")
    return path


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_every_label_change_is_journaled_with_before_and_after(journal, tmp_path, monkeypatch):
    fake = _FakeIcacls()
    monkeypatch.setattr(launcher.subprocess, "run", fake)
    target = str(tmp_path / "worktree")
    launcher._icacls(target, "L")
    launcher._icacls(target + "\\.git", "M")
    records = _lines(journal)
    assert [r["level"] for r in records] == ["L", "M"]
    first = records[0]
    assert first["path"] == target and first["op"] == "setintegritylevel"
    assert "BUILTIN\\Users" in first["before"] and "Mandatory Label\\Low" in first["after"]
    assert first["returncode"] == 0 and first["spawn_token"] == "deadbeef" and first["run_id"] == "run_journal_1"
    assert first["at"] and first["pid"]
    # 三次 icacls 调用一组：改前 / 改 / 改后 —— 账本要的是事实，不是「我试过了」
    assert [c[1:] for c in fake.calls[:3]] == [[target], [target, "/setintegritylevel", "(OI)(CI)L"], [target]]


def test_a_refused_label_is_still_journaled_with_its_error(journal, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", _FakeIcacls(rc=5))
    launcher._icacls(str(tmp_path / "x"), "M")
    [record] = _lines(journal)
    assert record["returncode"] == 5 and "Access is denied" in record["stderr"]


def test_without_a_journal_nothing_is_written_and_icacls_runs_once(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "_JOURNAL", None)
    fake = _FakeIcacls()
    monkeypatch.setattr(launcher.subprocess, "run", fake)
    launcher._icacls(str(tmp_path / "x"), "L")
    assert len(fake.calls) == 1, "没有账本就别多跑两次 icacls（probe 走这条）"
    assert not list(tmp_path.rglob("*.jsonl"))


def test_run_takes_the_journal_from_the_spec(monkeypatch, tmp_path):
    """`run()` 的环境是裁过的，账本位置只能走 spec。这里只验第一行：读 spec、设全局，再碰 Win32 之前就停。"""
    seen = {}

    def stop_here():
        seen["journal"] = launcher._JOURNAL
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher, "_win32", stop_here)
    with pytest.raises(RuntimeError, match="stop"):
        launcher.run({"journal": str(tmp_path / "j.jsonl"), "writable": []}, ["cmd"])
    assert seen["journal"] == str(tmp_path / "j.jsonl")
    with pytest.raises(RuntimeError, match="stop"):
        launcher.run({"writable": []}, ["cmd"])
    assert seen["journal"] is None


def test_the_win32_backend_puts_the_journal_into_the_spec(monkeypatch, tmp_path):
    from core.isolation import win32 as backend

    monkeypatch.setenv("HARNESS_PERMISSIONS_JOURNAL", str(tmp_path / "logs" / "permissions.jsonl"))
    source = Path(backend.__file__).read_text(encoding="utf-8")
    assert '"journal": os.environ.get("HARNESS_PERMISSIONS_JOURNAL")' in source
