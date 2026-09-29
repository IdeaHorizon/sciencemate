"""起不来的载荷不许切过去 —— 否则一次更新就把应用砖掉。

## 病例（2026-09-16 实测，0.4.6 → 0.5.0）

载荷带的是**代码**，不带 `site-packages`（CPython 与依赖走整包重装）。0.5.0 的后端把
鉴权换成 PyJWT（`import jwt`），而 0.4.6 那份安装里只有 `jose`。于是：切换成功、指针
写下、`uvicorn` 起 `app.main` 时 `ModuleNotFoundError: No module named 'jwt'` ——
**应用从此起不来**，重启也没用（指针已经指着新载荷）。用户看到的只有「后端退出了」。

`_looks_like_a_payload` 答不了这个：文件全都在，是这台机器缺库。判据只能是真 import
一次，而且必须在一个可以整个丢掉的进程里 —— 失败之后要原样继续跑旧版。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import self_update as su


def _a_staged_payload(root: Path, version: str, *, app_body: str | None) -> None:
    """造一份形式上完整的暂存载荷；`app_body` 给了就带一个后端 `app/`。"""
    staged = su.payload_root(root) / "staged" / version
    (staged / "harness" / "core").mkdir(parents=True)
    (staged / "harness" / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    (staged / "harness" / su.VERSION_MARKER).write_text(version, encoding="utf-8")
    if app_body is not None:
        app_dir = staged / "extras" / "app"
        app_dir.mkdir(parents=True)
        (app_dir / "__init__.py").write_text("", encoding="utf-8")
        (app_dir / "launcher.py").write_text("", encoding="utf-8")
        (app_dir / "main.py").write_text(app_body, encoding="utf-8")
    su.staged_pointer_path(root).parent.mkdir(parents=True, exist_ok=True)
    su.staged_pointer_path(root).write_text(
        json.dumps({"version": version, "staged_at": "2026-09-16T00:00:00+00:00"}),
        encoding="utf-8")


def test_a_payload_whose_backend_imports_is_applied(tmp_path: Path) -> None:
    _a_staged_payload(tmp_path, "9.9.9", app_body="VALUE = 1\n")
    assert su.apply_staged_at_launch(tmp_path) == "9.9.9"
    pointer = su.read_pointer(tmp_path)
    assert pointer is not None and pointer.version == "9.9.9"


def test_a_payload_missing_a_dependency_is_refused_and_the_old_one_keeps_running(
    tmp_path: Path,
) -> None:
    """正是 0.4.6 → 0.5.0 那个形状：代码到了、库没到。"""
    _a_staged_payload(
        tmp_path, "9.9.9",
        app_body="import a_module_that_is_definitely_not_installed_anywhere\n")

    assert su.apply_staged_at_launch(tmp_path) is None, "起不来的载荷被切过去了 —— 应用会砖掉"
    assert su.read_pointer(tmp_path) is None, "指针被改了，下次启动还会走同一条死路"
    problem = su.apply_error_path(tmp_path).read_text(encoding="utf-8")
    assert "起不来" in problem and "重新下载安装包" in problem, (
        f"没有把「为什么没装上、用户该做什么」说出来：{problem!r}")


def test_a_payload_without_a_backend_is_not_probed(tmp_path: Path) -> None:
    """只换 harness / 界面的载荷不带 `extras/app`，没有「后端起不起得来」这回事。"""
    _a_staged_payload(tmp_path, "9.9.9", app_body=None)
    assert su.apply_staged_at_launch(tmp_path) == "9.9.9"


def test_the_probe_failing_is_not_the_payloads_fault(tmp_path: Path, monkeypatch) -> None:
    """探针自己跑不起来时不拒绝更新 —— 那是探针的问题，不该变成一次拒绝。"""
    def _boom(*_args, **_kwargs):
        raise OSError("no interpreter here")

    monkeypatch.setattr("subprocess.run", _boom)
    _a_staged_payload(tmp_path, "9.9.9", app_body="import nothing_at_all\n")
    assert su.apply_staged_at_launch(tmp_path) == "9.9.9"
