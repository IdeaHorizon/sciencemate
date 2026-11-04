"""CI 的失败报告要说出「这台 runner 的墙钟在本 job 期间往回跳过」。

runner 的墙钟曾每 ~32 秒往回跳 ~1 秒（见 `.gitea/clock_watch.py` 模块文档）。PR#1276 上那次
偶发红查了两天才查到钟上 —— 这件事应该写在报告里，而不是留给下一个人再猜一遍。
三件事各一条：采样器认得出回跳；报告在跳过时说、没跳时不说；每个会贴报告的 job 都起了
采样器，而且写的就是报告读的那份文件（扫 workflow，不列名单）。
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WATCH = REPO / ".gitea" / "clock_watch.py"
REPORTER = REPO / ".gitea" / "report_pytest_failure.py"
WORKFLOWS = sorted((REPO / ".gitea" / "workflows").glob("*.yaml"))


def _load_watch():
    spec = importlib.util.spec_from_file_location("clock_watch_under_test", WATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTime:
    """墙钟在第 3 次采样时往回拨 1 秒，单调钟照常走 —— 就是那台 runner 的样子。"""

    def __init__(self) -> None:
        self.mono = 0.0
        self.wall = 1_800_000_000.0
        self.ticks = 0

    def sleep(self, seconds: float) -> None:
        self.ticks += 1
        self.mono += seconds
        self.wall += seconds - (1.0 if self.ticks == 3 else 0.0)

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


def test_the_watcher_records_a_backward_step(tmp_path) -> None:
    mod = _load_watch()
    fake = _FakeTime()
    mod.time = fake
    log = tmp_path / "clock.jsonl"
    mod.watch(str(log), interval=0.05, until=0.05 * 6)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    assert "-1000" in lines[0]
    assert "往回跳了 **1 次**" in mod.summarize(str(log))


def test_a_steady_clock_records_nothing(tmp_path) -> None:
    mod = _load_watch()
    fake = _FakeTime()
    fake.sleep = lambda s: (setattr(fake, "mono", fake.mono + s), setattr(fake, "wall", fake.wall + s))
    mod.time = fake
    log = tmp_path / "clock.jsonl"
    mod.watch(str(log), interval=0.05, until=0.3)
    assert log.read_text(encoding="utf-8") == ""
    assert mod.summarize(str(log)) is None


def test_the_probe_gives_a_verdict_either_way(tmp_path) -> None:
    """修钟之后的验收命令：跳过就报次数，没跳就明说没跳 —— 不许不吭声。"""
    mod = _load_watch()
    mod.time = _FakeTime()
    assert "往回跳了 **1 次**" in mod.probe(0.3, str(tmp_path / "a.jsonl"))
    steady = _FakeTime()
    steady.sleep = lambda s: (setattr(steady, "mono", steady.mono + s), setattr(steady, "wall", steady.wall + s))
    mod.time = steady
    assert "一次都没有往回跳" in mod.probe(0.3, str(tmp_path / "b.jsonl"))


def _report(clock_log: Path) -> str:
    env = {**os.environ, "CLOCK_WATCH_LOG": str(clock_log)}
    return subprocess.run(
        [sys.executable, str(REPORTER), "--to", "tracking-issue", "--dry-run", "--log", f"x={clock_log}.absent"],
        capture_output=True, text=True, env=env, timeout=60, check=True,
    ).stdout


def test_the_report_says_so_when_the_clock_jumped(tmp_path) -> None:
    log = tmp_path / "clock.jsonl"
    log.write_text('{"at": 1, "step_ms": -995.0}\n{"at": 2, "step_ms": -1159.0}\n', encoding="utf-8")
    out = _report(log)
    assert "往回跳了 **2 次**" in out and "-1159" in out


def test_the_report_stays_quiet_when_it_did_not(tmp_path) -> None:
    empty = tmp_path / "clock.jsonl"
    empty.write_text("", encoding="utf-8")
    assert "墙钟" not in _report(empty)
    assert "墙钟" not in _report(tmp_path / "never-started.jsonl")


def test_every_reporting_job_starts_the_watcher_on_the_file_the_report_reads() -> None:
    default = _load_watch().DEFAULT_PATH
    reporter = REPORTER.read_text(encoding="utf-8")
    assert f'"{default}"' in reporter, "报告读的默认路径和采样器写的不是同一份"

    problems, seen = [], 0
    for wf in WORKFLOWS:
        for name, job in (yaml.safe_load(wf.read_text(encoding="utf-8")).get("jobs") or {}).items():
            runs = [step.get("run") or "" for step in job.get("steps") or []]
            if not any("report_pytest_failure.py" in run for run in runs):
                continue
            seen += 1
            starts = [i for i, run in enumerate(runs) if "clock_watch.py" in run]
            tests = [i for i, run in enumerate(runs) if re.search(r"\bpytest\b", run)
                     and "report_pytest_failure" not in run]
            if not starts:
                problems.append(f"{wf.name}:{name} 会贴失败报告，却没起采样器")
                continue
            if tests and starts[0] > tests[0]:
                problems.append(f"{wf.name}:{name} 采样器起在测试之后")
            if default not in runs[starts[0]] or "&" not in runs[starts[0]]:
                problems.append(f"{wf.name}:{name} 采样器没在后台写 {default}")
    assert seen >= 3, f"只扫到 {seen} 个会贴报告的 job —— 扫描本身坏了？"
    assert not problems, "\n".join(problems)
