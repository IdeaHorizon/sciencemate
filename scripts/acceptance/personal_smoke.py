"""个人版验收：一台干净机器上，从零到第一条回复。

## 它回答的问题

「下载、打开、开跑」这句话是不是真的。判据不是"服务起来了"——服务起来而
harness 没接上、模型没配上、鉴权把人挡在门外，界面照样打得开，只是发消息之后
什么都不会发生。所以这个脚本一路走到**收到一条 assistant 回复**为止，中途任何
一次 401 都算失败。

## 为什么用假模型

真模型会把两件事搅在一起：这套东西通没通，和那把 key 好不好使。假模型是一个
OpenAI 兼容的最小实现（就一个 `/v1/chat/completions`），跑在本机随机端口上。
它证明的是**管线**：消息进得去、worker 起得来、模型调得到、回复回得来、落得了
库。key 好不好使有它自己的探针（创建连接时那次 probe）。

## 怎么用

    bash scripts/acceptance/personal_smoke.sh

环境变量（都不是必须的）：

- `SMOKE_FAKE_TOKEN=1`  带一个假 token 再跑一遍。个人档不签发也不校验 token，
  所以结论必须**一模一样**——这是"个人档真的没有鉴权"的判据，不是"鉴权被绕
  过了"。
- `PLATFORM_PROFILE=org`  组织档。必须失败，且必须是 401 —— 同一套代码换个装配
  就该把人挡在门外。
- `SMOKE_KEEP=1`  跑完不删临时目录（要看落了什么盘时用）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "platform" / "backend"
REPLY_TEXT = "SMOKE-REPLY-OK"

#: 每一次 401 都被记下来。个人档的承诺是"没有登录这件事"，而一次 401 正是这条
#: 承诺破了的样子——它不该发生在任何一步，包括那些"失败了也不影响后面"的步骤。
unauthorized: list[str] = []


class Failed(RuntimeError):
    pass


# ---------------------------------------------------------------- 假模型


class _FakeModel(BaseHTTPRequestHandler):
    """OpenAI 兼容的最小实现：不管问什么，都回同一句话，从不调工具。

    从不调工具是有意的：这个脚本验的是管线通不通，不是 agent 聪不聪明。一旦让
    它调工具，脚本就会开始依赖工具面的具体形状，而那是另一件事的判据。
    """

    calls: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size) if size else b"{}"
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            request = {}
        type(self).calls.append(request)
        if request.get("stream"):
            self._stream()
            return
        self._json({
            "id": "smoke-1",
            "object": "chat.completion",
            "model": request.get("model", "smoke"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": REPLY_TEXT},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        })

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        # 有些探针先问一下 /v1/models。答得出来比 404 干净。
        self._json({"object": "list", "data": [{"id": "smoke", "object": "model"}]})

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta in ({"role": "assistant", "content": ""}, {"content": REPLY_TEXT}):
            chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}}
        self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, _format: str, *_args) -> None:
        return None


def start_fake_model() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ---------------------------------------------------------------- HTTP 客户端


class _KeepTheMethod(urllib.request.HTTPRedirectHandler):
    """跟随 307/308 时把方法带上。

    urllib 默认把重定向后的请求降级成 GET —— 一个 POST 撞上 FastAPI 的
    「补斜杠」重定向之后就变成了列表查询，读起来像是"建项目失败"，而真正的
    原因只是路径少了一个斜杠。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code in (307, 308):
            return urllib.request.Request(
                newurl, data=req.data, headers=dict(req.header_items()),
                method=req.get_method())
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    timeout: float = 60.0,
    stream: bool = False,
) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    # 307 要保持方法不变。urllib 自带的重定向处理会把 POST 变成 GET，于是
    # 「建项目」看起来像是"没建成"而不是"少了个斜杠"。
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if os.environ.get("SMOKE_FAKE_TOKEN"):
        req.add_header("Authorization", "Bearer smoke-not-a-real-token")
    opener = urllib.request.build_opener(_KeepTheMethod)
    try:
        with opener.open(req, timeout=timeout) as response:
            if stream:
                return response.status, _read_sse(response)
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        if exc.code == 401:
            unauthorized.append(f"{method} {url} -> 401 {text[:120]}")
        return exc.code, text
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return 0, str(exc)


def _read_sse(response) -> str:
    """读到 done 为止；读到 error 当场抬走。SSE 在这里只是传输：要的是"这一轮结束了"。"""
    lines = []
    for raw in response:
        line = raw.decode("utf-8", "replace")
        lines.append(line)
        if line.startswith("data: "):
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            # `error` 和 `done` 原来都只是"跳出循环"——于是模型那边真的报错时，验收
            # 拿到的是一段没答完的文本，然后照常往下走，最后判绿。失败要当场说出来。
            if isinstance(event, dict) and event.get("type") == "error":
                raise Failed(f"模型执行失败：{json.dumps(event, ensure_ascii=False)[:1200]}")
            if isinstance(event, dict) and event.get("type") == "done":
                break
    return "".join(lines)


def expect(status: int, text: str, wanted: tuple[int, ...], what: str) -> dict:
    if status not in wanted:
        raise Failed(f"{what}: HTTP {status} {text[:400]}")
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- 后端


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_backend(home: Path, port: int, log: Path) -> subprocess.Popen:
    """用**真的启动器**起后端。

    不是 `uvicorn app.main:app` —— 那样验的是一个用户永远不会走的入口。用户敲的
    是 `research-platform`，它自己去找数据根、静态界面和 harness；这三件事里任何
    一件它没找到，就是个人版的一个真缺口，应该在这里现形。
    """
    env = {
        k: v for k, v in os.environ.items()
        # 显式清掉所有"能替这套东西回答问题"的变量：这个脚本验的就是**没人回答
        # 时它自己答得出来**。留着它们的话，一台配过环境的开发机永远看不见
        # 一台干净机器上的失败。
        if k not in {
            "HARNESS_FRAMEWORK_HOME", "PLATFORM_DATA_ROOT", "DATABASE_URL",
            "HARNESS_ROOT", "HARNESS_BRIDGE_ENABLED", "STATIC_UI_ROOT",
            "LLM_PROVIDERS_JSON", "PLATFORM_PROFILE", "LOCAL_DEMO_MODE",
        }
    }
    env["HARNESS_FRAMEWORK_HOME"] = str(home)
    profile = os.environ.get("PLATFORM_PROFILE")
    if profile:
        env["PLATFORM_PROFILE"] = profile
        if profile == "org":
            # 组织档要一把真密钥、也要一个显式的数据根 —— 那正是它与个人档的
            # 差别。给全了它才起得来，才轮得到"它把人挡在门外"这件事被验到。
            env["SECRET_KEY"] = "smoke-org-secret-key-not-a-real-one"
            env["PLATFORM_DATA_ROOT"] = str(home)
            env["DATABASE_URL"] = f"sqlite+aiosqlite:///{home / 'db.sqlite'}"
    with log.open("wb") as handle:
        # 用 `with`：孩子拿到的是**它自己那份**（fork/exec 与 CreateProcess 都会复制），
        # 我们这份用完就该关。留着它在 Windows 上会让收尾段删不掉这个文件
        # （`[WinError 32] 另一个程序正在使用此文件`），于是验收在**成功之后**才炸。
        return subprocess.Popen(
            ["uv", "run", "--project", str(BACKEND), "research-platform",
             "start", "--port", str(port), "--no-browser"],
            cwd=str(REPO), env=env, stdout=handle, stderr=subprocess.STDOUT,
        )


def stop_backend(backend: subprocess.Popen) -> None:
    """收摊：连它起的那一串一起收。

    直接孩子是 `uv run`，真正的后端是它的孩子，worker 又是后端的孩子。只
    `terminate()` 直接孩子，在 Windows 上会留下一整棵还活着的树 —— 它攥着后端
    日志那个文件，于是收尾段 `log.unlink()` 报
    `[WinError 32] 另一个程序正在使用此文件`，而报出来的时候被测系统早就通过了。
    POSIX 上同样留过树，只是"删一个被打开着的文件"在那边不报错，所以一直没人看见。

    Windows 没有进程组信号，`taskkill /T` 是 in-box 的那把整棵树的刀
    （生产代码用的是 Job Object，见 `shared.lib.process_control` —— 但这份脚本
    只许用 stdlib：它要能在一台什么都没装的机器上跑）。
    """
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(backend.pid)],
                       capture_output=True, check=False)
    else:
        backend.terminate()
    try:
        backend.wait(timeout=20)
    except subprocess.TimeoutExpired:
        backend.kill()


def wait_until_ready(base: str, backend: subprocess.Popen | None, log: Path | None,
                     deadline: float) -> None:
    """等它就绪。**两种模式共用这一段** —— 自己起的和别人起的都要等。

    `--url` 那条路原来是"问一次 health/ready，不是 200 就判失败"。可"已经在跑"
    不等于"已经暖好"：打包脚本刚 spawn 出来的那个后端要几秒才起得来，于是
    2026-09-07 打包自检当场红在 `不是 200（是 0）` —— 连接还没建上。
    等待是这条路本来就该有的，不是照顾谁。
    """
    while time.monotonic() < deadline:
        if backend is not None and backend.poll() is not None:
            tail = log.read_text()[-2000:] if log and log.exists() else "(没有日志)"
            raise Failed(f"后端退了（exit {backend.returncode}）：\n{tail}")
        status, text = request("GET", f"{base}/health/ready", timeout=5)
        if status == 200:
            return
        time.sleep(0.5)
    tail = log.read_text()[-2000:] if log and log.exists() else "(没有日志)"
    raise Failed(f"/health/ready 一直不是 200：\n{tail}")


# ---------------------------------------------------------------- 主流程


def run(timeout: float, against: str | None = None,
        receipt: Path | None = None, for_installer: Path | None = None) -> int:
    """走完整条路。

    `against` 给了就**不自己起后端**，验一个已经在跑的实例 —— 装出来的 `.app`
    里那个后端就是这么验的：它由壳拉起来，不归这个脚本管，但要走的路一模一样。
    另写一份"给 app 用的验收"就等于两份判据，而分叉的时候两边都不报错。
    """
    started = time.monotonic()
    timings: list[tuple[str, float]] = []

    def done(name: str, since: float) -> None:
        timings.append((name, time.monotonic() - since))

    conversation = ""
    home = Path(tempfile.mkdtemp(prefix="smoke-home-"))
    log = home.parent / f"{home.name}.log"
    model_server, model_url = start_fake_model()
    if against:
        base = against.rstrip("/")
        backend = None
    else:
        base = f"http://127.0.0.1:{free_port()}"
        backend = start_backend(home, int(base.rsplit(":", 1)[1]), log)
    api = f"{base}/api/v1"
    try:
        mark = time.monotonic()
        wait_until_ready(base, backend, log if backend else None, started + timeout)
        done("后端就绪", mark)

        # 双击之后发生的第一件事就是这个 —— 在问任何 API 之前先问它。
        # 2026-09-07：装出来的包 `/` 返回 {"detail":"Not Found"}，而 API 全好、
        # health 200、doctor 报得清清楚楚。这份脚本当时**从不要首页**，于是
        # "界面没了"在整套验收里没有任何一处会发现。
        mark = time.monotonic()
        status, text = request("GET", f"{base}/")
        if status != 200:
            raise Failed(
                f"首页不是 200（是 {status}）：{text[:200]}\n"
                "  · 装出来的 .app：这是缺陷 —— 界面在包里，只是没接上"
                "（见 tests/test_the_page_must_actually_come_up.py）。\n"
                "  · 源码树：界面还没构建过。先 "
                "`cd platform/frontend && PLATFORM_STATIC_EXPORT=1 npm run build`。"
                "这一步不能跳 —— 用户装到手的东西里有界面，验收就得验它。")
        if "<" not in text[:200]:
            raise Failed(f"首页不是 HTML：{text[:200]!r}")
        done("首页出得来", mark)

        mark = time.monotonic()
        status, text = request("GET", f"{api}/capabilities")
        capabilities = expect(status, text, (200,), "问这台服务器是什么").get("features", [])
        done("能力清单", mark)

        mark = time.monotonic()
        status, text = request("POST", f"{api}/settings/model-backends", body={
            "provider": "openai_compatible",
            "display_name": "smoke",
            "model": "smoke",
            "base_url": model_url,
            "api_key": "smoke-key",
        })
        backend_id = expect(status, text, (201,), "配模型")["id"]
        status, text = request("POST", f"{api}/settings/model-backends/{backend_id}/default")
        expect(status, text, (200, 201, 204), "设成默认模型")
        done("配模型", mark)

        mark = time.monotonic()
        status, text = request("POST", f"{api}/projects", body={"name": "Smoke"})
        project_id = expect(status, text, (201,), "建项目")["id"]
        done("建项目", mark)

        mark = time.monotonic()
        status, text = request("POST", f"{api}/projects/{project_id}/sessions",
                               body={"title": "Smoke session"})
        session_id = expect(status, text, (201,), "建会话")["id"]
        done("建会话", mark)

        mark = time.monotonic()
        status, text = request(
            "POST", f"{api}/chat/projects/{project_id}/stream",
            body={"answer": {"kind": "text", "text": "Say hello."},
                  "conversation_id": session_id},
            timeout=max(30.0, started + timeout - time.monotonic()),
            stream=True,
        )
        expect(status, text, (200,), "发一条消息")
        conversation = text
        reply = wait_for_reply(api, project_id, session_id, started + timeout)
        done("收到回复", mark)

        if unauthorized:
            raise Failed("个人档出现了 401：\n  " + "\n  ".join(unauthorized))
        if "auth" in capabilities:
            # 走到这里还带着 auth 能力，说明这台服务器**声称**有登录、却让一个
            # 没登录的人一路做完了全部事情。这比"挡住了"严重得多，所以它是
            # 一条独立的判据，不是上面那条的反面。
            raise Failed(f"这台服务器声称有登录，却没拦住任何一步：{capabilities}")
        if not _FakeModel.calls:
            raise Failed("模型一次都没被调到 —— 回复不是这套管线产生的")

        for name, seconds in timings:
            print(f"  {name:<12} {seconds:6.1f}s")
        print(f"SMOKE OK: reply received in {time.monotonic() - started:.0f}s")
        print(f"  模型被调 {len(_FakeModel.calls)} 次；回复：{reply[:80]!r}")
        if receipt is not None:
            write_the_receipt(receipt, against, for_installer, time.monotonic() - started)
        return 0
    except Failed as exc:
        for name, seconds in timings:
            print(f"  {name:<12} {seconds:6.1f}s")
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        if unauthorized:
            print("401:\n  " + "\n  ".join(unauthorized), file=sys.stderr)
        # 失败时把两份现场一起给出来：这一轮的事件流，和后端自己说了什么。
        # 只报一句"等不到回复"等于让人从头再查一遍。
        if conversation:
            print("--- 这一轮的事件流（尾部）---", file=sys.stderr)
            print("".join(conversation.splitlines(keepends=True)[-40:]), file=sys.stderr)
        if log.exists() and log.stat().st_size:
            print("--- 后端日志（尾部）---", file=sys.stderr)
            print(log.read_text("utf-8", "replace")[-4000:], file=sys.stderr)
        return 1
    finally:
        if backend is not None:
            stop_backend(backend)
        model_server.shutdown()
        if os.environ.get("SMOKE_KEEP"):
            print(f"（留下了 {home} 和 {log}）")
        else:
            shutil.rmtree(home, ignore_errors=True)
            log.unlink(missing_ok=True)


def wait_for_reply(api: str, project_id: str, session_id: str, deadline: float) -> str:
    """等一条 assistant 消息落库。

    落库才算数：SSE 只说明"这次连接看见了什么"，而用户下次打开这个会话看到的
    是库里的那份。两者分叉过（PR#686），所以判据落在后者。
    """
    last = ""
    while time.monotonic() < deadline:
        status, text = request(
            "GET", f"{api}/projects/{project_id}/sessions/{session_id}/messages")
        if status == 200:
            for item in json.loads(text).get("items", []):
                if item.get("role") == "assistant" and (item.get("content") or "").strip():
                    return item["content"]
            last = text
        time.sleep(1.0)
    raise Failed(f"等不到 assistant 回复：{last[:400]}")


def write_the_receipt(path: Path, against: str | None, installer: Path | None,
                      seconds: float) -> None:
    """把"这份包被人在 Windows 上真跑到收到回复"写成一张收据。

    只在**成功**之后调 —— 收据的全部意思就是"这条路走通过"，失败时写一张就是撒谎。

    收据认的是**字节**（安装器的 sha256），不是版本号：发布那头
    （`scripts/package/publish_release.py`）拿它和 SHA256SUMS 里那条对，对不上就是
    验了另一个包。`against` 记下验的是哪一个跑着的实例 —— 源码树里现起的后端在
    Windows 上一直是通的，它证明不了装出来的包。
    """
    body = {
        "result": "SMOKE OK",
        "against": against,
        "platform": sys.platform,
        "host": socket.gethostname(),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seconds": round(seconds, 1),
    }
    if installer is not None:
        digest = hashlib.sha256()
        with installer.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        body["installer"] = installer.name
        body["sha256"] = digest.hexdigest()
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  收据写在 {path}（{body.get('installer', '没给安装器 —— 发布那头会拒')}）")


def main(argv: list[str] | None = None) -> int:
    # 这份脚本的每一句人话都是中文，而 Windows 解释器的默认文本编码是 cp1252/cp936：
    # 不管它的话，脚本会在**打印结论那一行**抛 UnicodeEncodeError —— 跑通了也看不见，
    # 看见的是一个与被测系统无关的栈。"Windows 上怎么变成 utf-8" 这个问题产品里
    # 已经有答案（`shared.lib.platform_env.ensure_utf8_mode`：带 PYTHONUTF8=1 re-exec
    # 一次，起不来就降级只掰 stdout/stderr），验收用同一个答案，不另起一份。
    sys.path.insert(0, str(REPO))
    from shared.lib.platform_env import ensure_utf8_mode

    ensure_utf8_mode()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="整条路的总预算（秒）。个人版的承诺是 5 分钟。")
    parser.add_argument("--url", default=None,
                        help="验一个已经在跑的实例（比如 .app 里那个），不自己起后端")
    parser.add_argument("--receipt", default=None, type=Path,
                        help="跑通之后把收据写在这儿（发布那头要它才肯发 Windows 安装器）")
    parser.add_argument("--for-installer", default=None, type=Path,
                        help="这次验的是哪个安装器装出来的 —— 收据记它的 sha256")
    args = parser.parse_args(argv)
    if args.for_installer is not None and args.receipt is None:
        parser.error("--for-installer 得配 --receipt，否则算出来的哈希没人收")
    return run(args.timeout, args.url, args.receipt, args.for_installer)


if __name__ == "__main__":
    raise SystemExit(main())
