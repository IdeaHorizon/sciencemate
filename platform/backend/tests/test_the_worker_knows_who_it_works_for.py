"""worker 眼里「我是谁」由平台说 —— 不是这台服务器的 git 身份。

`core.identity.current_user_id()` 没人说时按 `git config user.email` 自己生成。平台上那是
**服务器**的 git 身份：一台组织服务器上每个人写下的 KB 记录都署同一个名字，按人写的算力授权
（grants.yaml 里 `<用户 id>` 那一节）也永远对不上任何人（2026-09-24 读出来的）。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from app.config import settings
from app.services.instructions import harness_home_for, publish_identity

REPO = Path(__file__).resolve().parents[3]


def test_the_harness_reads_back_the_platforms_answer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    publish_identity("5b92bbce-774b-413e-a99b-2aeb6d2d6090", display_name="李明", email="liming@lab.test")
    home = harness_home_for("5b92bbce-774b-413e-a99b-2aeb6d2d6090")

    said = subprocess.run(
        [sys.executable, "-c", "from core.identity import current_user_id; print(current_user_id())"],
        capture_output=True, text=True, cwd=str(REPO), timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO), "HARNESS_FRAMEWORK_HOME": str(home)})

    assert said.returncode == 0, said.stderr
    assert said.stdout.strip() == "5b92bbce-774b-413e-a99b-2aeb6d2d6090"


def test_publishing_twice_is_quiet_and_a_rename_is_heard(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    path = publish_identity("u1", display_name="李明", email="liming@lab.test")
    first = path.stat().st_mtime_ns
    publish_identity("u1", display_name="李明", email="liming@lab.test")
    assert path.stat().st_mtime_ns == first, "没变也重写了一遍"
    publish_identity("u1", display_name="李明（组长）", email="liming@lab.test")
    assert json.loads(path.read_text(encoding="utf-8"))["display_name"] == "李明（组长）"


def test_every_worker_spawn_publishes_it() -> None:
    """起 worker 的路径（`harness_sessions._paths`）每次都写 —— 读调用本身（AST），不是字符串。"""
    source = (Path(__file__).resolve().parents[1] / "app/services/harness_sessions.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == "_paths")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "publish_identity"]
    assert calls, "起 worker 时没告诉它自己是谁"
    assert ast.unparse(calls[0].args[0]) == "str(user.id)"
