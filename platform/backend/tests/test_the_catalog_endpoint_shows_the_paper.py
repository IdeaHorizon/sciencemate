"""走真 HTTP 问「这个项目产出了什么」，论文必须排在第一条、且带得开的 PDF。

这条测试刻意走**端点**而不是直接调 `core.catalog`：这一层还有一串只在真
HTTP 上才成立的东西 —— 鉴权、harness 契约模块真的加载得进来、工作区取的是
会话那个而不是项目主干。契约那一步尤其要真走一遍：后端进程的 cwd 是
`platform/backend`，`core` 不在 import path 上，顶层 import 会在测试里全绿、
在真部署上起不来（2026-09-04 一次真起进程照出来的）。

夹具写的是**真记录**（RFC 2026-09-12 §6）：正文是原生文件
`paper/manuscript__podsys.tex`，事实（类型 / 版本 / 冻结 / metadata）在
`.research/ledger/records.jsonl`。同一份夹具也喂 `/repository/records` —— 界面
读事实走那条端点，不再按文件名猜 head 在哪。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from app.services.harness_contract import ledger_module

# `runtime_client` 是从 test_local_runtime_api 借来的 fixture。
# ruff: noqa: F811
from .test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


async def _open_session(client: AsyncClient, token: str) -> tuple[str, str]:
    projects = await client.get("/api/v1/projects/", headers=_headers(token))
    assert projects.status_code == 200, projects.text
    project_id = projects.json()[0]["id"]
    created = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(token),
        json={"title": "看看产出了什么"},
    )
    assert created.status_code == 201, created.text
    return project_id, created.json()["id"]


async def _worktree(project_id: str, session_id: str) -> Path:
    """会话工作区的盘上位置。

    走 `run_in_repository_thread` 不是讲究：仓库那一层有一道闸，任何 git 调用
    落在事件循环线程上都当场报错（PR#905）。测试也归它管 —— 在测试里绕过去，
    等于让这道闸对"测试写的新调用点"失效。
    """
    from app.services.project_repository import (
        get_project_repository,
        run_in_repository_thread,
    )

    status = await run_in_repository_thread(
        get_project_repository().session_status, project_id, session_id
    )
    return Path(status.path)


def _write_manuscript(root: Path, *, extra_metadata: dict | None = None) -> None:
    """一份冻结的论文（第 5 版）+ 它编出来的 PDF —— 形状照真产物。

    存五版再冻第五版：账本里 head 是 version=5、freeze 行钉住它，文件一个字节
    不动。`extra_metadata` 是 agent 写进 metadata 的东西（不可信输入）。
    """
    ledger = ledger_module()
    pdf = root / "paper" / "latex_build" / "sci_manuscript" / "main.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    metadata = {"pdf_path": str(pdf), **(extra_metadata or {})}
    for version in range(1, 6):
        ledger.write_record(
            root, artifact_type="manuscript", name="podsys",
            content=f"\\documentclass{{article}} % v{version}\n", directory="paper",
            metadata=metadata, produced_by_node_type="writing",
            produced_by_run_id="1788160373-daa391",
            created_at="2026-09-09T08:00:00+00:00", frozen=version == 5,
        )
    # 同一个工作区里再放一堆框架内务 —— 用户抱怨的"一大堆没用的 log"。
    # 压缩日志如今随 run 生灭、不进工作区；每次编译铸的收据还在这里。
    for build in range(4):
        ledger.write_record(
            root, artifact_type="latex_build_receipt", name=f"build_{build}",
            content="{}", directory="paper", produced_by_node_type="writing",
            produced_by_run_id="1788160373-daa391",
            created_at="2026-09-09T08:02:00+00:00",
        )


@pytest.mark.asyncio
async def test_the_paper_is_the_first_row_and_its_pdf_opens(runtime_client) -> None:
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)
    _write_manuscript(await _worktree(project_id, session_id))

    response = await client.get(
        f"/api/v1/projects/{project_id}/catalog",
        params={"sessionId": session_id},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()

    first = body["entries"][0]
    assert first["kind"] == "manuscript", [e["kind"] for e in body["entries"][:3]]
    assert first["isDeliverable"] is True
    assert first["version"] == 5
    assert first["recordPath"] == "paper/manuscript__podsys.tex"
    assert body["counts"]["deliverable"] == 1
    assert body["counts"]["working"] == 4, "4 张编译收据属于工作过程，不该混进产出"

    # 判据落在**用户点得开**上：目录给的路径必须真的能取到字节。
    [pdf] = [p for p in first["files"] if p.endswith(".pdf")]
    raw = await client.get(
        f"/api/v1/projects/{project_id}/repository/raw",
        params={"sessionId": session_id, "path": pdf},
        headers=_headers(token),
    )
    assert raw.status_code == 200, (
        f"目录把论文指到 {pdf}，而那条路径取不到字节 —— 列出来点不开，"
        f"和没列出来一样：{raw.text[:200]}"
    )
    assert raw.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_a_path_outside_the_workspace_never_reaches_the_client(
    runtime_client, tmp_path: Path,
) -> None:
    """agent 写进 metadata 的路径是不可信输入 —— 工作区外的一律不端出去。

    渲染层对外部路径/URL 是零点击外泄，所以这条不是格式讲究。
    """
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)
    root = await _worktree(project_id, session_id)

    outside = tmp_path / "somebody_elses_secret.pdf"
    outside.write_bytes(b"%PDF-1.7\n")
    _write_manuscript(root, extra_metadata={"leak_path": str(outside)})

    response = await client.get(
        f"/api/v1/projects/{project_id}/catalog",
        params={"sessionId": session_id},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    assert entries, "夹具没写进目录 —— 空目录上这条断言什么都证明不了"
    every_path = [p for entry in entries for p in entry["files"]]
    assert not any("somebody_elses_secret" in p for p in every_path), every_path
    assert not any(p.startswith("/") for p in every_path), every_path


@pytest.mark.asyncio
async def test_the_records_endpoint_serves_the_ledger_head(runtime_client) -> None:
    """`/repository/records` 端出账本的 head：id / 类型 / 路径 / 版本 / 冻结。

    正文文件本身不带类型、版本、出处、metadata —— 这些只在账本上。界面此前按
    文件名正则猜 head、再拆 JSON 信封拿 metadata；现在事实走这条端点，正文
    走 `/repository/raw?path=`，两者对得上才算端出来了。
    """
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _open_session(client, token)
    _write_manuscript(await _worktree(project_id, session_id))

    response = await client.get(
        f"/api/v1/projects/{project_id}/repository/records",
        params={"sessionId": session_id},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["legacyLayout"] is False
    [row] = [r for r in body["records"] if r["type"] == "manuscript"]
    assert row["id"] == "manuscript__podsys"
    assert row["path"] == "paper/manuscript__podsys.tex"
    assert row["version"] == 5
    assert row["frozen"] is True
    assert row["frozenVersion"] == 5
    assert row["producedByNodeType"] == "writing"
    assert row["metadata"]["frozen"] is True, "冻结补丁要折进有效 metadata"
    assert len(body["records"]) == 5, "四张编译收据也是记录，照样在账本上"

    # 按类型过滤是同一条端点的事，不是界面自己再筛一遍。
    filtered = await client.get(
        f"/api/v1/projects/{project_id}/repository/records",
        params={"sessionId": session_id, "type": "manuscript"},
        headers=_headers(token),
    )
    assert [r["id"] for r in filtered.json()["records"]] == ["manuscript__podsys"]

    # 账本给的 path 必须真的取得到正文 —— 事实和正文是同一份东西的两面。
    raw = await client.get(
        f"/api/v1/projects/{project_id}/repository/raw",
        params={"sessionId": session_id, "path": row["path"]},
        headers=_headers(token),
    )
    assert raw.status_code == 200, raw.text
    assert raw.content.startswith(b"\\documentclass"), raw.content[:80]
