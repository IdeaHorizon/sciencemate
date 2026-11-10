"""Managed Chrome with private per-user profiles; metadata only, no extensions.

Uses the same reviewed DOM/export function as the existing collector. DevTools
listens on loopback only. No cookies, signed URLs or browser controls are exposed
through the public API. Human challenges are detected, never solved here.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sys
import time
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
    def __init__(self, code, *, endpoint=None, http_status=None):
        super().__init__(code)
        self.endpoint = endpoint
        self.http_status = http_status


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


async def activate_control(ws_url: str, point: dict):
    parsed = urlsplit(ws_url)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CollectionError("browser_endpoint_invalid")
    if (not isinstance(point, dict) or set(point) != {"x", "y"}
            or any(type(v) not in {float, int} or not math.isfinite(v) or not 0 <= v <= 100000
                   for v in point.values())):
        raise CollectionError("browser_activation_failed")
    # DOM preparation has already returned. Dispatch outside the page evaluation
    # and wait for both acknowledgements before observing the result.
    async with websockets.connect(ws_url, open_timeout=5, max_size=8*1024*1024) as ws:
        async with asyncio.timeout(10):
            for identifier, kind in ((1, "mouseMoved"), (2, "mousePressed"), (3, "mouseReleased")):
                await ws.send(json.dumps({"id": identifier, "method": "Input.dispatchMouseEvent",
                    "params": {"type": kind, **point, "button": "none" if kind == "mouseMoved" else "left",
                               "clickCount": 0 if kind == "mouseMoved" else 1}}))
                while True:
                    response = json.loads(await ws.recv())
                    if response.get("id") == identifier:
                        if response.get("error"):
                            raise CollectionError("browser_activation_failed")
                        break


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
        self.ensure_lock = asyncio.Lock()
        self.input_lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.recovery_lock = asyncio.Lock()
        self.next_request_at = 0.0
        self.last_recovery_at = None
        self.lanes = {}
        self.denied = False
        self.denied_at = None
        self.complete_result = None

    async def request_turn(self):
        """Pace active browser operations across both lanes, never DOM reads.

        The lock covers only the start reservation. Source responses can still
        arrive concurrently; no burst of accumulated tokens is allowed.
        """
        async with self.request_lock:
            if self.denied:
                raise CollectionError("session_paused")
            delay = self.next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            # A previously started request can be denied while this one waits.
            if self.denied:
                raise CollectionError("session_paused")
            self.next_request_at = time.monotonic() + settings.cnki_request_interval_seconds

    async def ensure(self, user_id: str):
        async with self.ensure_lock:
            user_id = str(UUID(user_id))
            if self.user_id == user_id and self.process and self.process.returncode is None:
                # A live Chrome process does not imply our collection tab survived.
                # Recreate closed tabs in the SAME private, verified session.
                tabs = (await self.client.get("/json/list")).json()
                if not self.tab or not any(t.get("id") == self.tab.get("id") for t in tabs):
                    self.tab = await self.new_tab()
                    if not self.denied:
                        await self.navigate(self.tab, HOME_URL)
                if self.detail_tab and not any(t.get("id") == self.detail_tab.get("id") for t in tabs):
                    self.detail_tab = None
                return
            previous_user = self.user_id
            await self.close()
            if previous_user is not None and previous_user != user_id:
                self.denied = False
                self.denied_at = None
                self.last_recovery_at = None
            profile = self.root / user_id
            mkdir_private(profile)
            make_private(profile)
            binary = executable()
            if not binary:
                raise CollectionError("browser_not_installed")
            port_file = profile / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            args = [binary, f"--user-data-dir={profile}", "--remote-debugging-port=0",
                    "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                    "--disable-dev-shm-usage", "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"]
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
                        # The private Chrome already owns a launch tab. Reuse it:
                        # a never-activated extra target can discard native input
                        # while hidden, although its DOM reports a valid hit target.
                        tabs = (await self.client.get("/json/list")).json()
                        self.tab = next((t for t in tabs if t.get("type") == "page"
                                         and t.get("url") == "about:blank"), None)
                        if self.tab is None:
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
                        if not self.denied:
                            await self.navigate(self.tab, HOME_URL)
                        return
                await asyncio.sleep(.15)
            raise CollectionError("browser_start_timeout")

    async def new_tab(self) -> dict:
        target = await command(self.ws, "Target.createTarget", url="about:blank", background=True)
        for _ in range(20):
            tabs = (await self.client.get("/json/list")).json()
            if tab := next((t for t in tabs if t["id"] == target["targetId"]), None):
                # Chrome can discard native input on a target which has never
                # been activated. Activate once, then keep the window minimized.
                async with self.input_lock:
                    await command(self.ws, "Target.activateTarget", targetId=tab["id"])
                if not settings.cnki_browser_virtual_display and not headless():
                    try:
                        window = await command(self.ws, "Browser.getWindowForTarget", targetId=tab["id"])
                        await command(self.ws, "Browser.setWindowBounds", windowId=window["windowId"],
                                      bounds={"windowState":"minimized"})
                    except (CollectionError, KeyError):
                        pass
                return tab
            await asyncio.sleep(.1)
        raise CollectionError("browser_tab_not_created")

    async def check_session(self, *, tab=None):
        """A newly submitted search can check a previously paused session once.

        Healthy sessions are untouched. This is consumed with the user's job,
        never invoked by a status poll or recurring collection.
        """
        if self.denied:
            await self.recover_session(tab=tab)

    async def recover_session(self, *, tab=None):
        """One explicit retry re-enters via home, keeping profile and challenges.

        No cookie replacement, challenge solving or repeated 403 refresh loop.
        """
        async with self.recovery_lock:
            # One UI retry can enqueue all five categories. They share the same
            # recovery attempt; a sibling must not clear a fresh 403 again.
            recent = max(self.last_recovery_at or 0, self.denied_at or 0)
            if recent and time.monotonic()-recent < 60:
                if self.denied:
                    raise CollectionError("session_paused")
                return
            for page_tab in (tab or self.tab, self.detail_tab, *self.lanes.values()):
                if page_tab and (await self.execute("state", tab=page_tab)).get("captcha"):
                    raise CollectionError("waiting_for_verification")
            # Only an explicit user retry clears the shared denial. A sibling
            # job or the next metadata row cannot start its own retry loop.
            self.last_recovery_at = time.monotonic()
            self.denied = False
            self.denied_at = None
            await self.navigate(tab or self.tab, HOME_URL)

    async def navigate(self, tab: dict, url: str):
        if self.denied:
            raise CollectionError("session_paused")
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in {"kns.cnki.net", "www.cnki.net"} or (p.hostname == "www.cnki.net" and (p.path != "/" or p.query)) or p.username or p.password or p.port not in {None, 443}:
            raise CollectionError("invalid_cnki_url")
        previous = await command(tab["webSocketDebuggerUrl"], "Runtime.evaluate",
                                 expression="performance.timeOrigin", returnByValue=True)
        origin = previous.get("result", {}).get("value")
        await self.request_turn()
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
                self.denied = True
                self.denied_at = time.monotonic()
                raise CollectionError("access_denied", endpoint=p.path, http_status=403)
            if page.get("host") == "chromewebdata" and page.get("error") and page.get("ready") == "complete":
                raise CollectionError("browser_navigation_failed")
            if page.get("origin") != origin and page.get("host") == p.hostname:
                if "/verify/" in page.get("path", ""):
                    raise CollectionError("waiting_for_verification")
                if page.get("ready") == "complete":
                    return
            await asyncio.sleep(.2)
        raise CollectionError("browser_navigation_timeout")

    async def _page(self, action: str, args: dict | None = None, *, tab=None) -> dict:
        if self.denied and action not in {"state", "cancel", "wait"}:
            raise CollectionError("session_paused")
        if action == "export":
            await self.request_turn()
        expression = "(async()=>{" + SCRIPT + ";return await cnkiPage(" + json.dumps(action) + "," + json.dumps(args or {}, ensure_ascii=False) + ");})()"
        response = await command((tab or self.tab)["webSocketDebuggerUrl"], "Runtime.evaluate",
                                 expression=expression, awaitPromise=True, returnByValue=True)
        if response.get("exceptionDetails"):
            raise CollectionError("browser_script_failed")
        value = response.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise CollectionError("browser_script_failed")
        if value.get("error"):
            if value["error"] == "access_denied":
                self.denied = True
                self.denied_at = time.monotonic()
            raise CollectionError(value["error"], endpoint=value.get("endpoint"), http_status=value.get("httpStatus"))
        return value

    async def execute(self, action: str, args: dict | None = None, *, tab=None) -> dict:
        if action not in {"search", "next", "sort", "resource"}:
            return await self._page(action, args, tab=tab)
        for attempt in range(3):
            dispatched = False
            prepared = False
            try:
                # Only focus + native input are serialized. Each tab then waits
                # for its own response concurrently, without stealing clicks.
                async with self.input_lock:
                    # Wait before measuring click coordinates; delayed dispatch
                    # after measurement can hit a different control after scroll.
                    await self.request_turn()
                    target = tab or self.tab
                    if target.get("id") and self.ws:
                        if (await self._page("state", tab=tab)).get("captcha"):
                            raise CollectionError("waiting_for_verification")
                        # Activating a background tab can restore its scroll.
                        # Focus before measuring the control, never afterwards.
                        await command(self.ws, "Target.activateTarget", targetId=target["id"])
                    result = await self._page(action, {**(args or {}), "prepareOnly": True}, tab=tab)
                    if "activation" not in result:
                        break
                    prepared = True
                    if self.denied:
                        raise CollectionError("session_paused")
                    dispatched = True
                    await activate_control(target["webSocketDebuggerUrl"], result["activation"])
                result = await self._page("wait", tab=tab)
                break
            except BaseException as exc:
                if prepared:
                    try:
                        await self._page("cancel", tab=tab)
                    except Exception:
                        pass
                # The page guard rejected the click before the site's handler
                # saw it. Reacquire the control; never replay an accepted click
                # on a timeout, HTTP error or human verification request.
                if isinstance(exc, CollectionError) and (str(exc) == "browser_activation_moved" or
                        (str(exc) == "browser_activation_failed" and not dispatched)) and attempt < 2:
                    await asyncio.sleep(.25)
                    continue
                raise
        if action in {"search", "resource"} and not (args or {}).get("skipSort") and not result.get("dateSorted"):
            result = await self.execute("sort", tab=tab)
        return result

    def empty_category(self, query, field, document_kind):
        """Only a complete, recent total-library result can prove a category empty.

        A generic 'try again later' page or missing category count cannot.
        This avoids selecting empty CNKI tabs which return an ambiguous error.
        """
        cached = self.complete_result
        if (not document_kind or not cached or cached[0] != (self.user_id, query.strip(), field)
                or time.monotonic()-cached[1] > 60):
            return None
        from app.services.cnki import as_literature
        category = {"journal":"中文期刊", "thesis":"学位论文", "conference":"会议文章",
                    "book":"书籍", "other":"其他"}[document_kind]
        state = cached[2]
        if any(as_literature(row).publication_category == category for row in state["rows"]):
            return None
        return {**state, "rows":[], "total":0, "page":1, "hasNext":False,
                "emptyConfirmed":True, "emptyEvidence":"complete_total_library_result",
                "resource":{"journal":"JOURNAL", "thesis":"DISSERTATION", "conference":"CONFERENCE",
                            "book":"BOOK"}.get(document_kind, "CROSSDB")}

    async def search(self, query: str, field: str, *, resume_verification: bool = False, document_kind=None, tab=None) -> dict:
        target = tab or self.tab
        resource = {"journal":"JOURNAL", "thesis":"DISSERTATION", "conference":"CONFERENCE", "book":"BOOK"}.get(document_kind, "CROSSDB")
        # Keep the challenge page available for a human instead of navigating
        # it away on every queue retry. Only inspect DOM while it is blocked.
        for page_tab in (target, self.detail_tab):
            if page_tab and (await self.execute("state", tab=page_tab)).get("captcha"):
                raise CollectionError("waiting_for_verification")
        if empty := self.empty_category(query, field, document_kind):
            return empty
        if resume_verification:
            # CNKI submits the pending query after a human completes its dialog.
            # Submitting it again here can trigger another challenge indefinitely.
            for _ in range(12):
                state = await self.execute("state", tab=target)
                if state.get("captcha"):
                    raise CollectionError("waiting_for_verification")
                if state.get("query", "").strip() != query.strip() or state.get("field") != field or state.get("resource", "CROSSDB") != resource:
                    break  # The user changed the query: never import unrelated results.
                if state.get("rows") or state.get("emptyConfirmed"):
                    return state
                await asyncio.sleep(.25)
            else:
                raise CollectionError("verification_result_pending")
        # Reuse the loaded search page for subsequent query groups.
        location = await command(target["webSocketDebuggerUrl"], "Runtime.evaluate",
            expression="({host:location.hostname,path:location.pathname,ready:!!document.querySelector('input.search-input,input[data-tipid=gradetxt-1]')})", returnByValue=True)
        page = location.get("result", {}).get("value", {})
        if page.get("host") != "kns.cnki.net" or not page.get("ready"):
            await self.navigate(target, SEARCH_URL)
        # A previous category must not constrain the next query. CNKI can use
        # the same generic error page for a selected category with no matches.
        state = await self.execute("state", tab=target)
        if state.get("resource", "CROSSDB") != "CROSSDB":
            try:
                await self.execute("resource", {"resource":"CROSSDB", "skipSort":True}, tab=target)
            except CollectionError as exc:
                if str(exc) != "results_unavailable":
                    raise
                # Changing the old query's filter can itself return an empty
                # page. Proceed only if the total-library tab actually changed.
                if (await self.execute("state", tab=target)).get("resource") != "CROSSDB":
                    raise
        result = await self.execute("search", {"query":query, "field":field, "skipSort":resource != "CROSSDB"}, tab=target)
        if (result.get("resource") == "CROSSDB" and result.get("query", "").strip() == query.strip()
                and result.get("field") == field and result.get("hasNext") is False
                and result.get("total") == len(result.get("rows", []))
                and (result.get("rows") or result.get("emptyConfirmed"))):
            self.complete_result = ((self.user_id, query.strip(), field), time.monotonic(), result)
            if empty := self.empty_category(query, field, document_kind):
                return empty
        if result.get("resource", "CROSSDB") != resource:
            result = await self.execute("resource", {"resource":resource}, tab=target)
        if result.get("resource", "CROSSDB") != resource:
            raise CollectionError("resource_selection_failed")
        return result

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
            if self.denied:
                raise CollectionError("session_paused")
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
        self.lanes.clear()

    def lane(self, slot):
        return BrowserLane(self, slot)


class BrowserLane:
    """Independent search tab, sharing only the owner's browser session."""
    def __init__(self, browser, slot):
        self.browser, self.slot, self.tab = browser, slot, None

    async def ensure(self, user_id):
        await self.browser.ensure(user_id)
        if self.slot == 0:
            self.tab = self.browser.tab
        else:
            tabs = (await self.browser.client.get("/json/list")).json()
            tab = self.browser.lanes.get(self.slot)
            if not tab or not any(t.get("id")==tab.get("id") for t in tabs):
                tab = await self.browser.new_tab()
                self.browser.lanes[self.slot] = tab
                if not self.browser.denied:
                    await self.browser.navigate(tab, HOME_URL)
            self.tab = tab

    async def execute(self, action, args=None):
        return await self.browser.execute(action, args, tab=self.tab)

    async def search(self, query, field, **kwargs):
        return await self.browser.search(query, field, tab=self.tab, **kwargs)

    async def prepare_metadata(self):
        if (await self.execute("state")).get("captcha"):
            raise CollectionError("waiting_for_verification")
        await self.browser.navigate(self.tab, SEARCH_URL)

    async def full_abstract(self, url):
        return await self.browser.full_abstract(url)

    async def recover_session(self):
        await self.browser.recover_session(tab=self.tab)

    async def check_session(self):
        await self.browser.check_session(tab=self.tab)

    async def close(self):
        # A broken lane must not close the sibling's live collection.
        if self.tab:
            try:
                await command(self.browser.ws, "Target.closeTarget", targetId=self.tab["id"])
            except Exception:
                pass
        self.tab = None
