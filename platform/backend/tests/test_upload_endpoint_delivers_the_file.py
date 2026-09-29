"""走真 HTTP：把文件交上去，然后它就在工作区里、树里、时间线里。

这是 2026-09-04 同事走的那条路 —— multipart 上传、能力校验、体积闸、落盘、
提交、回执。仓库测试覆盖了 `add_session_material`，但**端点**这一层还有一串
只在 HTTP 上才成立的东西：`UploadFile.file` 是不是真流进 `place`、413 的正文
里有没有那个数字、回执里给不给绝对路径。

老实现在这里静默了：会话附件 413 之后前端零反馈，用户以为传上去了。所以
"报错正文长什么样"在这里是被断言的对象，不是附带的。
"""
from __future__ import annotations

# `runtime_client` 是从 test_local_runtime_api 借来的 fixture（那套用户/项目/
# 会话的种子数据在那里，抄一份就是抄一份会各自演化的真相源）。ruff 把"参数名
# 遮住导入名"看成 F811 —— 那是 pytest fixture 注入的正常长相，不是重定义。
# ruff: noqa: F811
import hashlib
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.models.execution import ExecutionEvent
from app.services.harness_contract import materials_module

from .test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


async def _open_session(client: AsyncClient, token: str) -> tuple[str, str]:
    projects = await client.get("/api/v1/projects/", headers=_headers(token))
    assert projects.status_code == 200, projects.text
    project_id = projects.json()[0]["id"]
    created = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(token),
        json={"title": "交一份文件"},
    )
    assert created.status_code == 201, created.text
    return project_id, created.json()["id"]


@pytest.mark.asyncio
async def test_uploading_a_file_puts_it_in_the_session_workspace(runtime_client) -> None:
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)

    payload = b"podsys asc log line\n" * 5_000
    response = await client.post(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/files",
        headers=_headers(token),
        files={"file": ("podsys_asc_log.tar.gz", payload, "application/gzip")},
        data={"note": "第三方给的原始日志"},
    )
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["name"] == "podsys_asc_log.tar.gz"
    assert receipt["path"] == f"{materials_module().MATERIALS_RELATIVE}/podsys_asc_log.tar.gz"
    assert receipt["sizeBytes"] == len(payload)
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    assert receipt["committed"] is True

    # 回执里的绝对路径必须真的指到一个文件 —— 那正是模型要用的那条路径。
    absolute = Path(receipt["absolutePath"])
    assert absolute.is_file(), "回执给的绝对路径必须能直接打开"
    assert absolute.read_bytes() == payload

    tree = await client.get(
        f"/api/v1/projects/{project_id}/repository/tree",
        headers=_headers(token),
        params={
            "sessionId": session_id,
            "path": materials_module().MATERIALS_RELATIVE,
        },
    )
    assert tree.status_code == 200, tree.text
    rows = [
        row for row in tree.json()["entries"]
        if str(row["path"]).startswith(f"{materials_module().MATERIALS_RELATIVE}/")
    ]
    assert [row["path"] for row in rows] == [receipt["path"]], (
        f"界面上一份文件占一行、且是它自己那行，实际：{[r['path'] for r in rows]}"
    )
    assert rows[0]["sizeBytes"] == len(payload)
    assert rows[0]["note"] == "第三方给的原始日志"


@pytest.mark.asyncio
async def test_the_upload_lands_on_the_timeline_not_in_the_draft(runtime_client) -> None:
    """上传是时间线上的一件事。老实现把它塞进用户的输入草稿里，一删就没了。"""
    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)
    response = await client.post(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/files",
        headers=_headers(token),
        files={"file": ("table.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert response.status_code == 201, response.text

    async with factory() as db:
        query = select(ExecutionEvent).where(ExecutionEvent.kind == "material.added")
        events = (await db.execute(query)).scalars().all()
    assert len(events) == 1, "交一份文件应当留下**一条**事实"
    payload = events[0].payload
    assert payload["name"] == "table.csv"
    assert payload["sha256"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert Path(payload["absolutePath"]).is_file()


@pytest.mark.asyncio
async def test_too_large_says_the_limit_and_where_to_go_instead(runtime_client) -> None:
    """契约必须送到调用方：上限多少、超了走哪条路，都在这一句话里。

    2026-09-04 用户拿到的是**什么都没有** —— 后端 413 了，界面没说。这里钉
    住报错正文本身：数字在、去处在。
    """
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)

    original = settings.material_max_bytes
    settings.material_max_bytes = 1_024
    try:
        response = await client.post(
            f"/api/v1/projects/{project_id}/sessions/{session_id}/files",
            headers=_headers(token),
            files={"file": ("huge.bin", b"x" * 8_192, "application/octet-stream")},
        )
    finally:
        settings.material_max_bytes = original

    assert response.status_code == 413, response.text
    detail = response.json()["detail"]
    assert detail["maxBytes"] == 1_024, "报错要带上限那个数字"
    assert detail["sizeBytes"] >= 8_192, "也要说这份文件到底多大"
    assert "data 节点" in detail["message"], "还要说清超了该走哪条路"

    tree = await client.get(
        f"/api/v1/projects/{project_id}/repository/tree",
        headers=_headers(token),
        params={
            "sessionId": session_id,
            "path": materials_module().MATERIALS_RELATIVE,
        },
    )
    assert [
        row for row in tree.json()["entries"]
        if str(row["path"]).startswith(f"{materials_module().MATERIALS_RELATIVE}/")
    ] == [], "被拒的文件不许在树里留下任何痕迹"


@pytest.mark.asyncio
async def test_the_session_tells_the_client_what_the_limit_is(runtime_client) -> None:
    """前端的预检读这个数。它必须真的出现在会话响应里。"""
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)
    response = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{session_id}", headers=_headers(token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["materialMaxBytes"] == settings.material_max_bytes


@pytest.mark.asyncio
async def test_a_second_file_with_the_same_name_is_refused_with_a_next_step(
    runtime_client,
) -> None:
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)
    url = f"/api/v1/projects/{project_id}/sessions/{session_id}/files"
    first = await client.post(
        url, headers=_headers(token), files={"file": ("report.pdf", b"one", "application/pdf")}
    )
    assert first.status_code == 201, first.text

    replay = await client.post(
        url, headers=_headers(token), files={"file": ("report.pdf", b"one", "application/pdf")}
    )
    assert replay.status_code == 201, "同名同内容重传是幂等，不是错误"
    assert replay.json()["committed"] is False

    clash = await client.post(
        url, headers=_headers(token), files={"file": ("report.pdf", b"two", "application/pdf")}
    )
    assert clash.status_code == 409, clash.text
    assert "换个文件名" in clash.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_someone_without_drive_rights_cannot_add_files(runtime_client) -> None:
    client, _ = runtime_client
    lead = await _token(client, "researcher@atrium.local")
    outsider = await _token(client, "outsider@other.local")
    project_id, session_id = await _open_session(client, lead)

    response = await client.post(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/files",
        headers=_headers(outsider),
        files={"file": ("x.bin", b"x", "application/octet-stream")},
    )
    assert response.status_code in (403, 404), response.text


@pytest.mark.asyncio
async def test_the_old_upload_routes_are_gone(runtime_client) -> None:
    """删了就是删了：老路径不该还回 2xx。"""
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)
    for path in (
        f"/api/v1/projects/{project_id}/sessions/{session_id}/attachments",
        f"/api/v1/projects/{project_id}/sessions/{session_id}/attachments/promote",
        f"/api/v1/projects/{project_id}/materials",
    ):
        response = await client.post(
            path, headers=_headers(token),
            files={"file": ("x.bin", b"x", "application/octet-stream")},
        )
        assert response.status_code == 404, f"{path} 还活着：{response.status_code}"


@pytest.fixture(autouse=True)
def _harness_checkout(monkeypatch):
    """把 `HARNESS_ROOT` 指向本仓库自己的 harness checkout。

    材料这一层归 harness（`core/materials`），平台经 `harness_contract` 调它。
    测试进程默认没有这个变量 —— 与 `harness_domain_registry` 同一个理由和同一
    种做法：**给真路径、走真加载**，不 monkeypatch 掉那一层。替身遮住的正是
    "平台读得到 harness 的那份实现吗"这个接缝，而那是这条契约唯一值得验的东西。
    """
    from app.config import settings
    from app.services import harness_contract

    repository_root = Path(__file__).resolve().parents[3]
    assert (repository_root / "core" / "materials.py").is_file(), repository_root
    monkeypatch.setattr(settings, "harness_root", str(repository_root))
    harness_contract._harness_root.cache_clear()
    yield
    harness_contract._harness_root.cache_clear()
