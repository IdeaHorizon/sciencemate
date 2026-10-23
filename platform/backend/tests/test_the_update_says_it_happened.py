"""更新之后要说一句「已更新到 X」，说一次（应用更新的视觉反馈第 3 条）。

重启之后窗口整个换掉（端口变了，壳把窗口指到新地址）。以前新窗口里什么都不说，人只看到
一次没头没尾的刷新（wangd 10-19 在本机从 0.5.6 升到 0.5.7）。

「从哪一版升上来」只有暂存那一刻知道：第一次从随包版本升级时 current.json 还不存在。
"""
from __future__ import annotations

import json

import pytest

from app.services import self_update as su
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401
from tests.test_self_update import _manifest, _stream_of, _tar


def _stage(root, version: str, **kwargs) -> None:
    blob = _tar(version)
    su.download_and_stage(root, json.loads(_manifest(version, blob)), "http://x", _stream_of(blob), **kwargs)


def test_the_first_update_from_the_bundled_version_remembers_where_it_came_from(tmp_path) -> None:
    _stage(tmp_path, "0.5.7", from_version="0.5.6",
           notes_url="https://github.com/IdeaHorizon/sciencemate/releases/tag/v0.5.7")
    assert su.read_pointer(tmp_path) is None, "随包版本没有指针 —— 这正是要在暂存时记下来的原因"
    assert su.apply_staged_at_launch(tmp_path) == "0.5.7"
    assert su.just_updated(tmp_path) == {
        "from": "0.5.6", "to": "0.5.7", "applied_at": su.read_pointer(tmp_path).applied_at,
        "notes_url": "https://github.com/IdeaHorizon/sciencemate/releases/tag/v0.5.7"}


def test_it_is_said_once(tmp_path) -> None:
    _stage(tmp_path, "0.5.7", from_version="0.5.6")
    su.apply_staged_at_launch(tmp_path)
    assert su.acknowledge_update(tmp_path) is True
    assert su.just_updated(tmp_path) is None
    assert su.acknowledge_update(tmp_path) is False, "没有要确认的就别说确认了"
    assert su.read_pointer(tmp_path).version == "0.5.7", "确认不许动指针本身"


def test_without_a_recorded_origin_the_previous_pointer_answers(tmp_path) -> None:
    _stage(tmp_path, "1.0.0")
    su.apply_staged_at_launch(tmp_path)
    su.acknowledge_update(tmp_path)
    _stage(tmp_path, "1.1.0")
    su.apply_staged_at_launch(tmp_path)
    assert su.just_updated(tmp_path)["from"] == "1.0.0"


def test_an_old_pointer_or_a_rollback_is_not_an_update(tmp_path) -> None:
    (tmp_path / "payload").mkdir()
    (tmp_path / "payload" / "current.json").write_text(json.dumps({"version": "0.5.7"}), encoding="utf-8")
    assert su.just_updated(tmp_path) is None, "老指针没有 announced 字段 —— 不是刚更新"
    _stage(tmp_path, "1.0.0", from_version="0.9.0")
    su.apply_staged_at_launch(tmp_path)
    su.acknowledge_update(tmp_path)
    _stage(tmp_path, "1.1.0", from_version="1.0.0")
    su.apply_staged_at_launch(tmp_path)
    su.rollback(tmp_path)
    assert su.just_updated(tmp_path) is None, "回滚不是「已更新到」"


@pytest.mark.asyncio
async def test_the_status_says_it_even_offline_and_the_window_can_dismiss_it(runtime_client, monkeypatch, tmp_path) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(settings, "update_source", "http://127.0.0.1:9/nowhere/repo")   # 够不着
    _stage(tmp_path, "0.5.7", from_version="0.5.6")
    su.apply_staged_at_launch(tmp_path)
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")

    status = (await client.get("/api/v1/update", headers=_headers(token))).json()
    assert status["just_updated"]["from"] == "0.5.6" and status["just_updated"]["to"] == "0.5.7"
    assert status["error"], "这条测试要的是够不着更新源的情形"

    done = await client.post("/api/v1/update/acknowledge", headers=_headers(token))
    assert done.status_code == 200 and done.json() == {"acknowledged": True}
    assert (await client.get("/api/v1/update", headers=_headers(token))).json()["just_updated"] is None


def test_install_hands_over_where_it_came_from_and_the_release_page() -> None:
    """接线：装更新那一处把装着的版本与 release 页交给暂存（AST，不靠名字出现过）。"""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "update.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "download_and_stage"]
    assert len(calls) == 1
    kws = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords}
    assert kws.get("from_version") == "status.installed_version" and kws.get("notes_url") == "source.page_url"
