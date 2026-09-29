"""工作区文件的字节出口 —— 图和 PDF 能不能被真的取出来。

这一层之前整个不存在：`read_worktree_file` 把内容解码成 UTF-8，二进制一律
`content=None`。节点跑完出的 `figures/*.png` 和 `latex_build/*.pdf` 在 API 上
取不到，界面上只剩一个文件名。
"""

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import StaticPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import hash_password
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.project import Project, ProjectMembership
from app.models.user import User
from tests._authentication_tables import AUTHENTICATION_TABLES
from app.services.file_media import DEFAULT_MEDIA_TYPE, media_type_for
from app.services.project_repository import (
    GitProjectRepository,
    ProjectFileTooLargeError,
    ProjectRepositoryError,
    get_project_repository,
)
from app.services.project_repository import run_in_repository_thread

#: 真的 PNG 头。用随便一串字节测"能读出二进制"会漏掉一整类问题 —— 编码回退
#: 把不可解码的字节替换成 U+FFFD 时，长度对不上但内容看起来"有东西"。
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def _project(tmp_path: Path) -> tuple[GitProjectRepository, str]:
    repository = GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")
    repository.initialize_project(
        project_id="project-preview",
        name="Preview",
        description=None,
        research_domain=None,
        owner_id="user-1",
    )
    return repository, "project-preview"


def test_binary_bytes_survive_the_round_trip(tmp_path: Path) -> None:
    repository, project_id = _project(tmp_path)
    root = repository.project_path(project_id)
    (root / "figures").mkdir(parents=True, exist_ok=True)
    (root / "figures/fig1.png").write_bytes(PNG_HEADER)

    safe, body = repository.read_worktree_bytes(
        project_id, "figures/fig1.png", max_bytes=1_000_000
    )
    assert safe == "figures/fig1.png"
    # 逐字节相等 —— 这正是既有 JSON 端点做不到的那一步。
    assert body == PNG_HEADER

    # 对照：老路径在同一个文件上依然只能回 content=None。这条断言是为了钉住
    # "为什么需要第二条读取路径"，将来有人想合并两者时会先撞到它。
    text_view = repository.read_worktree_file(project_id, "figures/fig1.png")
    assert text_view["binary"] is True
    assert text_view["content"] is None


def test_the_size_cap_names_the_limit_instead_of_pretending_the_file_is_missing(
    tmp_path: Path,
) -> None:
    repository, project_id = _project(tmp_path)
    root = repository.project_path(project_id)
    (root / "big.bin").write_bytes(b"x" * 5_000)

    with pytest.raises(ProjectFileTooLargeError) as excinfo:
        repository.read_worktree_bytes(project_id, "big.bin", max_bytes=1_000)
    # 界面要能说"文件 5000 字节，上限 1000"——只说"打不开"的话，用户不知道
    # 下一步该干什么。
    assert excinfo.value.size_bytes == 5_000
    assert excinfo.value.max_bytes == 1_000
    # 仍然是 ProjectRepositoryError 的子类：既有 except 分支不会漏掉它。
    assert isinstance(excinfo.value, ProjectRepositoryError)


def test_the_byte_path_refuses_every_escape_the_text_path_refuses(tmp_path: Path) -> None:
    """两条读取路径共用一份路径判据，所以这里逐条比对，不另写一套预期。"""
    repository, project_id = _project(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours")

    for attempt in (
        "../secret.txt",
        "../../secret.txt",
        "figures/../../secret.txt",
        "/etc/passwd",
        ".git/config",
        "",
        ".",
    ):
        with pytest.raises(ProjectRepositoryError):
            repository.read_worktree_bytes(project_id, attempt, max_bytes=1_000_000)
        with pytest.raises(ProjectRepositoryError):
            repository.read_worktree_file(project_id, attempt)


def test_a_symlink_pointing_outside_the_worktree_is_refused(tmp_path: Path) -> None:
    """`..` 只是最直白的一种越界。软链接不含 `..`，靠 resolve 之后的归属判。"""
    repository, project_id = _project(tmp_path)
    root = repository.project_path(project_id)
    outside = tmp_path / "outside.txt"
    outside.write_text("not yours")
    (root / "escape.txt").symlink_to(outside)

    with pytest.raises(ProjectRepositoryError):
        repository.read_worktree_bytes(project_id, "escape.txt", max_bytes=1_000_000)


def test_a_directory_is_not_a_file(tmp_path: Path) -> None:
    repository, project_id = _project(tmp_path)
    with pytest.raises(ProjectRepositoryError):
        repository.read_worktree_bytes(project_id, "postprocess", max_bytes=1_000_000)


def test_media_types_do_not_depend_on_the_host(tmp_path: Path) -> None:
    """研究产出真正会出现的那些扩展名必须是确定的。

    `mimetypes.guess_type` 读宿主机上的 mime 表 —— 开发机上对、精简镜像里
    可能回 None。那样的差异只有部署之后才看得见，且症状（"PDF 变成下载了"）
    完全不像环境问题。
    """
    assert media_type_for("outputs/figures/fig1.png") == "image/png"
    assert media_type_for("paper/latex_build/main.pdf") == "application/pdf"
    assert media_type_for("figures/schematic.svg") == "image/svg+xml"
    assert media_type_for("report.html") == "text/html"
    assert media_type_for("MEMORY.md") == "text/markdown"
    assert media_type_for("results.json") == "application/json"
    assert media_type_for("FIG1.PNG") == "image/png", "扩展名大小写不该改变结论"


def test_an_unknown_extension_degrades_to_inert_bytes() -> None:
    """未知 → octet-stream。方向是刻意的：新东西默认惰性，不默认可执行。"""
    assert media_type_for("experiments/checkpoint.qqqq") == DEFAULT_MEDIA_TYPE
    assert media_type_for("no_extension_at_all") == DEFAULT_MEDIA_TYPE


# ── 路由层 ──────────────────────────────────────────────────────────────
#
# 上面那些是服务层。下面这些必须走真路由：except 分支的**顺序**、安全响应头、
# media type 有没有真的接到响应上 —— 这三样在服务层测不到，而它们恰恰是这条
# 路由最容易出错的部分。

PASSWORD = "PreviewTest2026!"

_TABLES = [
    *AUTHENTICATION_TABLES,
    Project.__table__,
    ProjectMembership.__table__,
]


@pytest_asyncio.fixture
async def preview_client():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=_TABLES)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:

        async def override_get_db():
            yield db

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, db
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_project(db: AsyncSession):
    """库里的 Project 行 + 磁盘上真实的 Git 仓库，两边用同一个 id。"""
    owner = User(
        email="preview.owner@atrium.local",
        hashed_password=hash_password(PASSWORD),
        display_name="Owner",
        role="researcher",
        institution_id="inst",
        group_id="grp",
    )
    outsider = User(
        email="preview.outsider@other.local",
        hashed_password=hash_password(PASSWORD),
        display_name="Outsider",
        role="researcher",
        institution_id="other-inst",
        group_id="other-grp",
    )
    db.add_all([owner, outsider])
    await db.flush()
    project = Project(owner_id=owner.id, name="Preview project")
    db.add(project)
    await db.flush()

    (await run_in_repository_thread(get_project_repository().initialize_project, 
        project_id=str(project.id),
        name="Preview project",
        description=None,
        research_domain=None,
        owner_id=str(owner.id),
    ))
    await db.commit()
    return owner, outsider, project


async def _headers(client: AsyncClient, email: str) -> dict[str, str]:
    """这个人的一张 token —— 直接签，不走登录端点（登录那扇门是专业版的，公开树里没有）。
    用户从这个夹具接给 app 的那条 get_db 里查：夹具怎么装配库，这里就怎么问。"""
    from sqlalchemy import select

    from app.auth import create_access_token
    from app.database import get_db
    from app.main import app as the_app
    from app.models.user import User

    provide = the_app.dependency_overrides.get(get_db) or get_db
    sessions = provide()
    db = await sessions.__anext__()
    try:
        user = await db.scalar(select(User).where(User.email == email))
    finally:
        try:
            await sessions.aclose()
        except Exception:  # noqa: BLE001 - 夹具的生成器怎么收尾是它的事
            pass
    assert user is not None, f"没有 {email} 这个用户"
    return {"Authorization": f"Bearer {create_access_token(user)}"}


def _write(project, relative: str, body: bytes) -> None:
    root = get_project_repository().project_path(str(project.id))
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


@pytest.mark.asyncio
async def test_a_figure_reaches_the_browser_as_an_image(preview_client):
    client, db = preview_client
    owner, _, project = await _seed_project(db)
    _write(project, "figures/figures/fig1.png", PNG_HEADER)

    response = await client.get(
        f"/api/v1/projects/{project.id}/repository/raw",
        params={"path": "figures/figures/fig1.png"},
        headers=await _headers(client, owner.email),
    )
    assert response.status_code == 200
    assert response.content == PNG_HEADER
    assert response.headers["content-type"].startswith("image/png")
    # 目的是"打开看"，不是"存下来"。
    assert response.headers["content-disposition"].startswith("inline")
    assert "fig1.png" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_agent_written_files_are_served_sandboxed_and_unsniffable(preview_client):
    """这里送出的是 **agent 写的文件**，包括 HTML。

    生产环境前后端同源。少了 sandbox，一个节点产出的 HTML 直接访问就是在应用
    自己的源上执行脚本；少了 nosniff，浏览器会无视我们给的类型自己猜。
    """
    client, db = preview_client
    owner, _, project = await _seed_project(db)
    _write(project, "paper/report.html", b"<script>alert(1)</script>")

    response = await client.get(
        f"/api/v1/projects/{project.id}/repository/raw",
        params={"path": "paper/report.html"},
        headers=await _headers(client, owner.email),
    )
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_too_large_is_not_reported_as_missing(preview_client, monkeypatch):
    """except 分支的顺序 —— TooLarge 是 RepositoryError 的子类。

    写反了的话"文件太大"会被当成"文件不存在"回 404，而这两件事用户的下一步
    完全不同（一个是去下载，一个是文件真没了）。这条测试是那个顺序的锚。
    """
    client, db = preview_client
    owner, _, project = await _seed_project(db)
    _write(project, "paper/paper.pdf", b"%PDF-1.7" + b"0" * 5_000)
    monkeypatch.setattr(settings, "project_file_preview_max_bytes", 1_000)

    response = await client.get(
        f"/api/v1/projects/{project.id}/repository/raw",
        params={"path": "paper/paper.pdf"},
        headers=await _headers(client, owner.email),
    )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["code"] == "project_file_too_large"
    # 界面要能把上限说出来。
    assert detail["maxBytes"] == 1_000
    assert detail["sizeBytes"] > 1_000


@pytest.mark.asyncio
async def test_the_raw_route_refuses_what_the_json_route_refuses(preview_client):
    """新开一条读取路径，最容易漏的就是把准入抄少一道。逐条比对，不各写预期。"""
    client, db = preview_client
    owner, outsider, project = await _seed_project(db)
    _write(project, "data/secret.txt", b"in-project")

    owner_headers = await _headers(client, owner.email)
    outsider_headers = await _headers(client, outsider.email)

    for route in ("raw", "file"):
        url = f"/api/v1/projects/{project.id}/repository/{route}"
        # 越界路径
        for attempt in ("../../../etc/passwd", ".git/config", "data/../../escape"):
            escaped = await client.get(
                url, params={"path": attempt}, headers=owner_headers
            )
            assert escaped.status_code == 404, (route, attempt)
        # 看不见这个项目的人
        forbidden = await client.get(
            url, params={"path": "data/secret.txt"}, headers=outsider_headers
        )
        assert forbidden.status_code == 403, route
        # 没有凭据
        anonymous = await client.get(url, params={"path": "data/secret.txt"})
        assert anonymous.status_code in (401, 403), route
        # 不存在的项目
        missing = await client.get(
            f"/api/v1/projects/00000000-0000-0000-0000-000000000000/repository/{route}",
            params={"path": "data/secret.txt"},
            headers=owner_headers,
        )
        assert missing.status_code == 404, route


@pytest.mark.asyncio
async def test_text_types_carry_utf8_or_chinese_reports_render_as_mojibake(preview_client):
    """`text/*` 必须带上 `charset=utf-8`。

    2026-08-23 在真 Chrome 里做的对照：同一份中文 HTML，blob 的类型是
    `text/html` 时 iframe 按传统编码解码，标题变成 `èŠ,ç,¹äº§å‡º…`；带
    `charset=utf-8` 才是「节点产出的网页报告」。

    blob 的类型直接来自这个响应头，所以这条不变量的**唯一**落点在这里。
    Starlette 目前对 `text/*` 会自动补 —— 正因为是自动的，改 media_type 的
    写法（比如为了控制别的头改成手写 Content-Type）会静默把它弄丢，而症状
    是"中文报告花了"，看起来完全不像响应头的问题。

    markdown/纯文本那条路不受影响：前端走 `blob.text()`，那个恒按 UTF-8 解。
    受影响的是交给浏览器自己解码的 iframe（HTML）。
    """
    client, db = preview_client
    owner, _, project = await _seed_project(db)
    _write(project, "paper/report.html", "<h3>节点产出的网页报告</h3>".encode())
    _write(project, "notes.md", "# 中文标题".encode())
    headers = await _headers(client, owner.email)

    for path in ("paper/report.html", "notes.md"):
        response = await client.get(
            f"/api/v1/projects/{project.id}/repository/raw",
            params={"path": path},
            headers=headers,
        )
        assert response.status_code == 200
        assert "charset=utf-8" in response.headers["content-type"].lower(), path
        # 字节本身也要原样过去。
        assert "节点产出的网页报告" in response.text or "中文标题" in response.text

    # 二进制不该被塞 charset —— 那是给文本用的。
    _write(project, "figures/fig.png", PNG_HEADER)
    binary = await client.get(
        f"/api/v1/projects/{project.id}/repository/raw",
        params={"path": "figures/fig.png"},
        headers=headers,
    )
    assert binary.headers["content-type"] == "image/png"
