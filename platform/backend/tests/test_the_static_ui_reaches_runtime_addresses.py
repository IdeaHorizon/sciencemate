"""静态导出的 UI 必须能送到构建时不存在的地址上。

`next build --output export` 只为每个动态段产出一份外壳（`projects/_/sessions/_.html`），
而用户手里的地址是 `/projects/9f3a…/sessions/67cd…`。送不到那份外壳，硬刷新一个
会话链接就是 404 —— 一个「只能站内点进去、不能刷新、不能分享」的应用。

顺序是这里唯一的难点：字面量优先。`/projects/settings` 是真实路由，不能被占位段
吃掉。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.static_site import resolve_static_asset, resolve_static_page


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    for rel in (
        "index.html", "projects.html", "login.html", "settings.html",
        "projects/_.html", "projects/_/activity.html", "projects/_/settings.html",
        "projects/_/sessions/_.html", "projects/_/artifacts/_.html",
        # 与动态段**同级**的静态路由。Next 里 /projects/new 优先于 /projects/[id]，
        # 这里也必须。它是唯一能分辨「先字面量」和「先占位段」的形状 —— 没有它，
        # 把顺序调反测试照样全绿（2026-09-05 变异 P3 实测）。
        "projects/new.html",
    ):
        page = tmp_path / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"<!-- {rel} -->", encoding="utf-8")
    # 404 页从一开始就在。少了它，「应用自有路由不该被界面吃掉」那条会**碰巧
    # 绿**：界面送不出东西时本来就会落回应用，于是判据根本没被触发 ——
    # 2026-09-06 实测，装出来的包里 404.html 是真存在的，那条路一走就现形。
    (tmp_path / "404.html").write_text("<!-- 404 -->", encoding="utf-8")
    asset = tmp_path / "_next" / "static" / "app.js"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text("console.log(1)", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(("url", "expected"), [
    ("/", "index.html"),
    ("/projects", "projects.html"),
    ("/projects/9f3a-real-id", "projects/_.html"),
    ("/projects/9f3a-real-id/activity", "projects/_/activity.html"),
    ("/projects/9f3a-real-id/sessions/67cd-real-session", "projects/_/sessions/_.html"),
    ("/projects/9f3a-real-id/artifacts/art-1", "projects/_/artifacts/_.html"),
])
def test_a_runtime_address_lands_on_its_shell(site: Path, url: str, expected: str) -> None:
    assert resolve_static_page(site, url) == site / expected


def test_a_literal_route_beats_the_shell(site: Path) -> None:
    """与动态段同级的静态路由必须赢。

    `/projects/new` 是一个真实页面，`projects/_.html` 是「任意项目 id」的外壳。
    两者在同一层，顺序在这里是唯一的判据：先字面量，取不到才回退占位段。
    Next 的路由匹配就是这个顺序，我们不能在服务这一侧把它反过来 —— 那样
    「新建项目」会被当成一个 id 为 "new" 的项目。
    """
    assert resolve_static_page(site, "/projects/new") == site / "projects/new.html"
    assert resolve_static_page(site, "/projects/9f3a-real-id") == site / "projects/_.html"
    assert resolve_static_page(site, "/projects/x/settings") == site / "projects/_/settings.html"
    assert resolve_static_page(site, "/settings") == site / "settings.html"


def test_an_address_with_no_shell_is_not_invented(site: Path) -> None:
    assert resolve_static_page(site, "/nope") is None
    assert resolve_static_page(site, "/projects/x/nope/deeper") is None


def test_path_traversal_never_leaves_the_site(site: Path, tmp_path: Path) -> None:
    (tmp_path.parent / "secret.html").write_text("no", encoding="utf-8")
    assert resolve_static_page(site, "/../secret") is None
    assert resolve_static_asset(site, "/../secret.html") is None


def test_build_assets_are_served_literally(site: Path) -> None:
    assert resolve_static_asset(site, "/_next/static/app.js") == site / "_next/static/app.js"
    assert resolve_static_asset(site, "/_next/static/missing.js") is None
    # 资源不回退外壳：一个不存在的 chunk 要 404，不能送一份 HTML 回去，
    # 那会让浏览器报一个指不到病因的语法错误。
    assert resolve_static_asset(site, "/projects/x/sessions/y") is None


# ── 界面挂上之后，API 还得是原来那个 API ──────────────────────────────────
#
# 这一片验的不是"界面送得对"，是"界面**没有**顺手改变别的东西"。它抓到过一次
# 真的：catch-all 路由把 `POST /api/v1/projects` 从 307 变成 405，而且只在装出来
# 的包里发生（从源码跑时界面根本没构建，那条 catch-all 不存在）。


@pytest.fixture()
def app_with_ui(site: Path):
    """一个最小应用：两条 API、一条自有路由，外加挂上界面。

    不 reload `app.main`：那会把真应用的状态搅进来，而且测完还得再 reload 回去。
    这里要验的是**接线本身**，接线现在是一个类，可以单独装到任何应用上。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.static_site import StaticUI

    application = FastAPI()

    @application.post("/api/v1/projects/")
    async def _create() -> dict:
        return {"id": "p1"}

    @application.get("/api/v1/projects/")
    async def _list() -> list:
        return []

    @application.get("/health")
    async def _health() -> dict:
        return {"status": "ok"}

    application.add_middleware(
        StaticUI, root=site, api_prefix="/api/v1", owner=application)
    return TestClient(application, follow_redirects=False)


def test_a_write_without_the_trailing_slash_still_redirects(app_with_ui) -> None:
    """规范路径带尾斜杠的 API，少一个斜杠时要重定向，不能是 405。

    405 的意思是"这个地址不接受这个方法"，而真相是"这个地址就是它、只是写法
    差一个斜杠"。客户端拿到 405 只会去改自己的调用，改不动的就此卡住。
    """
    response = app_with_ui.post("/api/v1/projects", json={"name": "x"})
    assert response.status_code == 307, response.text
    assert response.headers["location"].endswith("/api/v1/projects/")


def test_a_read_without_the_trailing_slash_also_redirects(app_with_ui) -> None:
    """GET 少一个斜杠时也要重定向 —— 而且这一条比 POST 那条更容易漏。

    POST 不是 GET，中间件本来就整条放过去；GET 才真正走到"这地址归谁"的判断。
    2026-09-06 变异实测：把 API 前缀那道短路去掉，POST 那条判据**照样绿**
    （它压根没走到那里），只有这一条会转红 —— 少了它，那道短路等于没有判据
    守着，而它一旦失效，界面会拿一份 404.html 去答一个真实存在的接口。
    """
    response = app_with_ui.get("/api/v1/projects")
    assert response.status_code == 307, response.text
    assert response.headers["location"].endswith("/api/v1/projects/")


def test_the_api_still_answers_its_own_addresses(app_with_ui) -> None:
    assert app_with_ui.post("/api/v1/projects/", json={"name": "x"}).json() == {"id": "p1"}
    assert app_with_ui.get("/api/v1/projects/").json() == []


def test_an_address_the_app_owns_is_not_served_as_a_page(app_with_ui) -> None:
    """`/health` 这类应用自有路由不在 API 前缀下，但仍然归应用。

    判据是"应用有没有这条路由"，问出来的，不是一份名单 —— 名单会让以后新加的
    路由默认被界面吃掉，而那种失败长得像"这个接口不存在"。
    """
    assert app_with_ui.get("/health").json() == {"status": "ok"}


def test_pages_and_assets_still_reach_the_browser(app_with_ui, site: Path) -> None:
    assert app_with_ui.get("/").text == "<!-- index.html -->"
    assert app_with_ui.get("/projects/9f3a-real-id").text == "<!-- projects/_.html -->"
    assert app_with_ui.get("/_next/static/app.js").text == "console.log(1)"


def test_an_unknown_page_gets_the_sites_own_404(app_with_ui, site: Path) -> None:
    response = app_with_ui.get("/no/such/page")
    assert response.status_code == 404
    assert response.text == "<!-- 404 -->"
