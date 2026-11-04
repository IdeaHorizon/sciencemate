"""Managed Chrome with private per-user profiles; metadata only, no extensions.

Uses the same reviewed DOM/export function as the existing collector. DevTools
listens on loopback only. No cookies, signed URLs or browser controls are exposed
through the public API. Human challenges are detected, never solved here.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import websockets

from app.config import settings, the_data_root
from shared.lib.private_files import make_private, mkdir_private

HOME_URL = "https://www.cnki.net/"
SEARCH_URL = "https://kns.cnki.net/kns8s/search"
SCRIPT = (Path(__file__).resolve().parents[1] / "assets/cnki-extension/cnki-page.js").read_text().replace("export async function", "async function", 1)


class CollectionError(RuntimeError):
    pass


def executable() -> str | None:
    if settings.cnki_browser_executable:
        return shutil.which(settings.cnki_browser_executable)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        if path := shutil.which(name):
            return path
    candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    if sys.platform == "win32":
        for root in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if base := os.environ.get(root):
                candidates += [str(Path(base) / name) for name in (
                    "Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe")]
    return next((p for p in candidates if Path(p).is_file()), None)


def availability() -> dict:
    enabled = settings.cnki_server_enabled
    available = enabled and bool(executable())
    return {"mode": "server", "available": available,
            "detail": "平台自动采集，无需安装扩展" if available else
            "平台知网采集未启用" if not enabled else "采集服务器未找到 Chrome 或 Edge，请联系管理员"}


def headless() -> bool:
    if settings.cnki_browser_headless is not None:
        return settings.cnki_browser_headless
    return sys.platform not in {"win32", "darwin"} and not bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


async def command(ws_url: str, method: str, **params) -> dict:
    parsed = urlsplit(ws_url)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CollectionError("browser_endpoint_invalid")
    async with websockets.connect(ws_url, open_timeout=5, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        async with asyncio.timeout(30):
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == 1:
                    if message.get("error"):
                        raise CollectionError("browser_command_failed")
                    return message.get("result", {})


class Browser:
    def __init__(self, root: Path | None = None):
        self.root = root or the_data_root() / "cnki/browser"
        self.process = None
        self.user_id = None
        self.client = None
        self.ws = None
        self.tab = None
        self.detail_tab = None
        self.detail_lock = asyncio.Lock()

    async def ensure(self, user_id: str):
        user_id = str(UUID(user_id))
        if self.user_id == user_id and self.process and self.process.returncode is None:
            return
        await self.close()
        binary = executable()
        if not binary:
            raise CollectionError("browser_not_installed")
        profile = self.root / user_id
        mkdir_private(profile)
        make_private(profile)
        port_file = profile / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        args = [binary, f"--user-data-dir={profile}", "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                "--disable-dev-shm-usage"]
        virtual_display = settings.cnki_browser_virtual_display and sys.platform == "linux"
        if virtual_display:
            launcher = shutil.which("xvfb-run")
            if not launcher:
                raise CollectionError("virtual_display_unavailable")
            args = [launcher, "-a", "-s", "-screen 0 1280x900x24 -nolisten tcp", *args,
                    "--window-size=1280,900"]
        elif headless():
            args.append("--headless=new")
        else:
            args.append("--start-minimized")
        self.process = await asyncio.create_subprocess_exec(*args, "about:blank",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self.user_id = user_id
        for _ in range(80):
            if self.process.returncode is not None:
                raise CollectionError("browser_start_failed")
            if port_file.exists():
                lines = port_file.read_text().splitlines()
                if len(lines) >= 2 and lines[0].isdigit():
                    self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{lines[0]}", timeout=5, trust_env=False)
                    version = (await self.client.get("/json/version")).json()
                    self.ws = version["webSocketDebuggerUrl"]
                    self.tab = await self.new_tab()
                    # Keep collection unobtrusive; verification is surfaced in the platform.
                    if not virtual_display and not headless():
                        try:
                            window = await command(self.ws, "Browser.getWindowForTarget", targetId=self.tab["id"])
                            await command(self.ws, "Browser.setWindowBounds", windowId=window["windowId"],
                                          bounds={"windowState": "minimized"})
                        except (CollectionError, KeyError):
                            pass  # Some window managers only support the launch flag.
                    # Start at the public home page to establish a normal browser session.
                    await self.navigate(self.tab, HOME_URL)
                    return
            await asyncio.sleep(.15)
        raise CollectionError("browser_start_timeout")

    async def new_tab(self) -> dict:
        target = await command(self.ws, "Target.createTarget", url="about:blank", background=True)
        for _ in range(20):
            tabs = (await self.client.get("/json/list")).json()
            if tab := next((t for t in tabs if t["id"] == target["targetId"]), None):
                return tab
            await asyncio.sleep(.1)
        raise CollectionError("browser_tab_not_created")

    async def navigate(self, tab: dict, url: str):
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in {"kns.cnki.net", "www.cnki.net"} or (p.hostname == "www.cnki.net" and (p.path != "/" or p.query)) or p.username or p.password or p.port not in {None, 443}:
            raise CollectionError("invalid_cnki_url")
        previous = await command(tab["webSocketDebuggerUrl"], "Runtime.evaluate",
                                 expression="performance.timeOrigin", returnByValue=True)
        origin = previous.get("result", {}).get("value")
        result = await command(tab["webSocketDebuggerUrl"], "Page.navigate", url=url)
        # The browser's error document can finish loading after Page.navigate
        # returns. Inspect it in the same bounded readiness loop below.
        if result.get("errorText") and "ERR_HTTP_RESPONSE_CODE_FAILURE" not in result["errorText"]:
            raise CollectionError("browser_navigation_failed")
        for _ in range(100):
            value = await command(tab["webSocketDebuggerUrl"], "Runtime.evaluate",
                expression="({host:location.hostname,path:location.pathname,ready:document.readyState,origin:performance.timeOrigin,error:document.querySelector('.error-code')?.innerText})", returnByValue=True)
            page = value.get("result", {}).get("value", {})
            if page.get("host") == "chromewebdata" and "403" in (page.get("error") or ""):
                raise CollectionError("access_denied")
            if page.get("host") == "chromewebdata" and page.get("error") and page.get("ready") == "complete":
                raise CollectionError("browser_navigation_failed")
            if page.get("origin") != origin and page.get("host") == p.hostname:
                if "/verify/" in page.get("path", ""):
                    raise CollectionError("waiting_for_verification")
                if page.get("ready") == "complete":
                    return
            await asyncio.sleep(.2)
        raise CollectionError("browser_navigation_timeout")

    async def execute(self, action: str, args: dict | None = None, *, tab=None) -> dict:
        expression = "(async()=>{" + SCRIPT + ";return await cnkiPage(" + json.dumps(action) + "," + json.dumps(args or {}, ensure_ascii=False) + ");})()"
        response = await command((tab or self.tab)["webSocketDebuggerUrl"], "Runtime.evaluate",
                                 expression=expression, awaitPromise=True, returnByValue=True)
        if response.get("exceptionDetails"):
            raise CollectionError("browser_script_failed")
        value = response.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise CollectionError("browser_script_failed")
        if value.get("error"):
            raise CollectionError(value["error"])
        return value

    async def search(self, query: str, field: str) -> dict:
        # Keep the challenge page available for a human instead of navigating
        # it away on every queue retry. Only inspect DOM while it is blocked.
        for tab in (self.tab, self.detail_tab):
            if tab and (await self.execute("state", tab=tab)).get("captcha"):
                raise CollectionError("waiting_for_verification")
        # Reuse the loaded search page for subsequent query groups.
        location = await command(self.tab["webSocketDebuggerUrl"], "Runtime.evaluate",
            expression="({host:location.hostname,path:location.pathname,ready:!!document.querySelector('input.search-input')})", returnByValue=True)
        page = location.get("result", {}).get("value", {})
        if page.get("host") != "kns.cnki.net" or not page.get("ready"):
            await self.navigate(self.tab, SEARCH_URL)
        return await self.execute("search", {"query":query, "field":field})

    async def prepare_metadata(self):
        state = await self.execute("state")
        if state.get("captcha"):
            raise CollectionError("waiting_for_verification")
        location = await command(self.tab["webSocketDebuggerUrl"], "Runtime.evaluate",
            expression="location.hostname", returnByValue=True)
        if location.get("result", {}).get("value") != "kns.cnki.net":
            await self.navigate(self.tab, SEARCH_URL)

    async def full_abstract(self, url: str) -> dict:
        async with self.detail_lock:
            if self.detail_tab is None:
                self.detail_tab = await self.new_tab()
            await self.navigate(self.detail_tab, url)
            return await self.execute("abstract", tab=self.detail_tab)

    async def close(self):
        if self.ws:
            try:
                await asyncio.wait_for(command(self.ws, "Browser.close"), 3)
            except Exception:
                pass
        if self.process and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        if self.client:
            await self.client.aclose()
        self.process = self.client = self.ws = self.user_id = self.tab = self.detail_tab = None
