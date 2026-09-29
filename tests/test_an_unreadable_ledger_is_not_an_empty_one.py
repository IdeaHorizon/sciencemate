"""作业观测：读不出来 ≠ 没有作业（#941）。

观测面此前答不出「受管作业的真实终态、退出状态、调度器作业号」。于是评测只能把
这些维度记成 NOT_OBSERVABLE，或者更糟 —— 从 chat 文案、catalog 里有没有产物、
退出码摘要去**推断** PASS。

这组判据钉的是那条最容易被悄悄违反的：**把读失败降级成空清单，看起来和
「这个项目一个作业都没跑过」一模一样。**`core.jobs.load` 正是这么做的
（它给 agent 循环内部用，那里降级可以接受）；观测面必须抛。
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from core import jobs


def _write_ledger(root, *rows):
    path = root / jobs._JOBS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_no_ledger_is_honestly_empty(tmp_path):
    """登记表不存在 = 确实还没有作业。这个空是真的。"""
    assert jobs.observe(tmp_path) == []


def test_an_unreadable_ledger_raises_instead_of_looking_empty(tmp_path):
    """登记表在、但读不动 —— 必须抛，不许返回 []。"""
    path = _write_ledger(tmp_path, {"job_id": "j1", "status": "done"})
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):          # root 无视权限位
            pytest.skip("以 root 跑，权限位挡不住读取")
        with pytest.raises(jobs.JobsLedgerUnreadable):
            jobs.observe(tmp_path)
    finally:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def test_load_still_degrades_and_that_is_the_difference(tmp_path):
    """对照：agent 循环内部那条路读不动仍然返回 [] —— 两个函数就差这一件事。

    没有这一条，上面那条可以靠「把 load 也改成抛」通过，而那会改变
    agent 循环的行为（那里降级是有意的）。
    """
    path = _write_ledger(tmp_path, {"job_id": "j1", "status": "done"})
    os.chmod(path, 0)

    class _S:
        project_root = str(tmp_path)

    try:
        if os.access(path, os.R_OK):
            pytest.skip("以 root 跑，权限位挡不住读取")
        assert jobs.load(_S()) == []
    finally:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def test_terminal_state_and_scheduler_identity_come_through(tmp_path):
    """观测面要答得出的那几样：终态、调度器作业号、谁起的。"""
    _write_ledger(
        tmp_path,
        {"job_id": "j1", "purpose": "LAMMPS NVE", "node_type": "experiment",
         "run_id": "r-7", "scheduler_job_id": "slurm-90210", "started_at": 1.0},
        {"job_id": "j1", "status": "failed", "note": "exit 7"},
    )
    (rec,) = jobs.observe(tmp_path)
    assert rec.status == "failed", "终态没合进来 —— append-only 要取最后一条"
    assert rec.scheduler_job_id == "slurm-90210"
    assert rec.run_id == "r-7" and rec.purpose == "LAMMPS NVE"
    assert rec.note == "exit 7"


def test_a_corrupt_line_does_not_hide_the_rest(tmp_path):
    """一行坏了不该让整张表消失 —— 那又是一次「看起来没有作业」。"""
    path = tmp_path / jobs._JOBS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"job_id": "j1", "status": "done"}\n{ not json\n'
                    '{"job_id": "j2", "status": "running"}\n', encoding="utf-8")
    assert {r.job_id for r in jobs.observe(tmp_path)} == {"j1", "j2"}


def test_the_endpoint_says_unreadable_not_empty():
    """端点把 JobsLedgerUnreadable 翻成 503，不是 200 + 空清单。"""
    import ast
    import pathlib

    src = pathlib.Path("platform/backend/app/api/v1/projects.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_project_jobs")
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    caught = " ".join(ast.unparse(h.type) for h in handlers if h.type is not None)
    assert "JobsLedgerUnreadable" in caught, (
        "端点没有接住「读不出来」—— 那个异常会变成 500，或者更糟：有人加个 "
        "except Exception 把它变成空清单")
    bodies = " ".join(ast.unparse(h) for h in handlers)
    assert "503" in bodies
