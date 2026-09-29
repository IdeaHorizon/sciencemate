"""把静态导出的 UI 交给浏览器 —— 包括那些构建时还不存在的地址。

## 为什么需要一个「解析器」而不是一个 StaticFiles

个人档的 UI 是 `next build --output export` 出来的一堆文件：运行期只剩 Python，
不再需要 Node。代价是 Next 要求每个动态段在**构建时**报出取值，而项目 id 和会话
id 是运行时才有的东西。于是我们只为每个动态段产出一份外壳：

    out/projects/_.html
    out/projects/_/sessions/_.html

真实地址 `/projects/9f3a…/sessions/67cd…` 在磁盘上不存在。它要落到
`projects/_/sessions/_.html` 这份外壳上 —— 页面在浏览器里用 `useParams()` 从地址
栏读真 id，所以外壳一旦送到，显示的就是真实那一个。

**顺序很重要**：先找字面量再找占位段。`/projects/settings` 这种"看起来像 id、其实
是真实路由"的地址必须落到 `projects/settings.html`，不能被占位段吃掉。
"""
from __future__ import annotations

from pathlib import Path

#: 静态导出里代表「运行时才知道」的那一段。与前端 `shared/routing/static-shell.ts`
#: 的 STATIC_SHELL_PARAM 是同一个约定 —— 改一边就要改另一边，所以两处都写了对方。
SHELL_SEGMENT = "_"


def _safe_segments(url_path: str) -> list[str] | None:
    segments = [s for s in url_path.strip("/").split("/") if s]
    for segment in segments:
        if segment in {".", ".."} or "\\" in segment or "\x00" in segment:
            return None
    return segments


def resolve_static_page(root: Path, url_path: str) -> Path | None:
    """这个地址该送哪份 HTML；送不出来返回 None（由调用方决定 404 还是别的）。

    先字面量后占位段，逐段回退。返回的路径保证在 ``root`` 之内。
    """
    segments = _safe_segments(url_path)
    if segments is None:
        return None

    def walk(directory: Path, rest: list[str]) -> Path | None:
        if not rest:
            index = directory / "index.html"
            return index if index.is_file() else None
        head, tail = rest[0], rest[1:]
        for candidate in (head, SHELL_SEGMENT):
            if tail:
                child = directory / candidate
                if child.is_dir():
                    found = walk(child, tail)
                    if found is not None:
                        return found
                continue
            page = directory / f"{candidate}.html"
            if page.is_file():
                return page
            child = directory / candidate
            if child.is_dir() and (child / "index.html").is_file():
                return child / "index.html"
        return None

    return walk(root, segments)


def resolve_static_asset(root: Path, url_path: str) -> Path | None:
    """构建产物里的真实文件（JS / CSS / 图片 / favicon）。字面量，不回退占位段。"""
    segments = _safe_segments(url_path)
    if not segments:
        return None
    candidate = root.joinpath(*segments)
    if not candidate.is_file():
        return None
    try:
        candidate.relative_to(root)
    except ValueError:  # pragma: no cover - _safe_segments 已挡掉 ..
        return None
    return candidate


class StaticUI:
    """把静态界面交给浏览器 —— 中间件，不是一条 catch-all 路由。

    ## 为什么不是路由

    catch-all 那版（`@app.get("/{full_path:path}")`）会**吃掉 API 的补斜杠
    重定向**。`POST /api/v1/projects` 的规范路径是 `/api/v1/projects/`：没挂
    界面时 Starlette 回 307 让客户端改道；挂上之后，这条 GET-only 的 catch-all
    在路径上匹配成功、方法对不上，同一个请求就变成 **405 Method Not Allowed**。

    2026-09-06 打 Mac 包时实测：同一份代码，从源码跑（没构建界面）一切正常，
    装成 `.app`（界面在包里）就 405 —— 而后者才是用户拿到的那一份。受影响的是
    全部 5 条规范路径带尾斜杠的 API 路由。

    中间件跑在路由**之前**：不是 API、且应用自己没有这条路由，才由它接管；
    其余原样放过去，该重定向重定向、该 405 405。
    """

    def __init__(self, app, *, root: Path, api_prefix: str, owner) -> None:
        self.app = app
        self.root = Path(root).expanduser().resolve()
        self.api_prefix = api_prefix
        # `app` 是中间件链里的**下一层**（异常处理，再下面才是路由表），它没有
        # `.routes`。要问"应用自己有没有这条路由"，必须拿着应用本身问。
        #
        # 2026-09-06 实测：从 `app` 上找 routes 永远找不到，于是 `/health` 被
        # 当成一个不存在的页面、回了一份 404.html。而我为这条写的测试当时是
        # **碰巧绿的** —— fixture 里还没造 404.html，送不出东西才落回应用。
        self.owner = owner

    async def __call__(self, scope, receive, send) -> None:
        if self._belongs_to_the_app(scope):
            await self.app(scope, receive, send)
            return
        response = self.answer(scope["path"])
        if response is None:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)

    def _belongs_to_the_app(self, scope) -> bool:
        """这条请求该交给应用自己吗。

        三种情况：不是 HTTP 的读请求、地址在 API 前缀下、或者应用**自己有**
        这条路由（哪怕方法对不上 —— 那时候该由它回 405，而不是由界面回一份
        HTML）。

        最后一条是问出来的，不是写死的名单：`/health`、`/docs`、以后新加的
        任何一条都自动算数。写名单的话，新加的路由默认会被界面吃掉，而那种
        失败长得像"这个接口不存在"。
        """
        if scope["type"] != "http" or scope.get("method") not in ("GET", "HEAD"):
            return True
        if scope["path"].startswith(self.api_prefix):
            return True
        from starlette.routing import Match

        for route in self.owner.routes:
            match, _ = route.matches(scope)
            if match is not Match.NONE:
                return True
        return False

    def answer(self, url_path: str):
        """这个地址该送什么。送不出来返回 None（由应用自己去答）。"""
        from fastapi.responses import FileResponse

        asset = resolve_static_asset(self.root, url_path)
        if asset is not None:
            return FileResponse(asset)
        page = resolve_static_page(self.root, url_path)
        if page is not None:
            return FileResponse(page, media_type="text/html")
        not_found = self.root / "404.html"
        if not_found.is_file():
            return FileResponse(not_found, media_type="text/html", status_code=404)
        return None
