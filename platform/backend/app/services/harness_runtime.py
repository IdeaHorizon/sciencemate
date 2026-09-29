"""Isolated subprocess client for the latest harness-framework platform bridge."""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path

from app.config import settings
from app.models.model_backend import ModelBackendConfig
from app.services.model_backends import HARNESS_COMPATIBLE_PROVIDERS

ProtocolCallback = Callable[[dict], Awaitable[None]]


class HarnessRuntimeError(RuntimeError):
    pass



def the_interpreter_that_runs_the_harness() -> str:
    """哪个 Python 跑 harness —— **这个问题只在这里回答一次**。

    显式配了就用配的（node20 那类部署会指到一个特定的 venv）；没配就是**跑着
    这个后端的那一个**：harness 与后端装在同一份运行时里（Mac 安装包就是这么
    打的），所以那个答案永远成立，而"没配"不该是一种失败。

    2026-09-06 真机实测：装好的 `.app` 里 `HARNESS_PYTHON` 是空的。spawn 这条
    路有 `or sys.executable` 兜底，跑得好好的；而启动探针那条路直接
    `Path(settings.harness_python)`，空串经 `os.path.abspath("")` 变成**当前
    工作目录** —— Launch Services 起的应用 cwd 是 `/`，于是探针去执行 `/`，
    拿回 `[Errno 13] Permission denied`，日志里写着"这台机器没有执行边界"。

    实际上边界好好的（同一份 harness 直接问，答的是 darwin / write_boundary 等
    五项全在）。也就是说平台在**谎报**，而且是往"更不安全"的方向报：一个真有
    写边界的机器被记成没有。同一个问题两处各答一次，其中一处忘了兜底 —— 判据
    因此收成一处。
    """
    return settings.harness_python or sys.executable


_HARNESS_SUBPROCESS_PASSTHROUGH = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "XDG_CACHE_HOME",
})


def harness_subprocess_env(
    root: Path | str,
    *,
    passthrough: Iterable[str] = (),
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """起一个 harness 子进程（python.exe import core/shared）的环境 —— **一处回答**。

    和上面的解释器一样，"给 harness 子进程什么环境"这个问题也只该答一次。这些子进程都会
    import 到 asyncio / socket / TLS，因此**必须带 Windows 系统变量**（SYSTEMROOT 等）：
    否则连 ws2_32 / 加密 DLL 都加载不了，``import asyncio`` 就 ``OSError WinError 10106``
    （Winsock 起不来），启动探针崩掉、后端又一次**谎报「没有执行边界」**（Windows 版，与
    上面 Mac 版同形）。系统变量走 ``platform_env.system_env_passthrough`` 一处回答 —— P0-5
    只把 worker 的 ``_child_environment`` 收了，naming/kb/feed/runtime/启动探针各抄了一份
    allowlist、都漏了系统变量，这里一并收口。

    同族的第二件事 **UTF-8 模式** 也一并带上（``utf8_mode_env`` → Windows 上 PYTHONUTF8=1）：
    这些子进程都 import core、一路写读中文的 transcript/memory，cp1252 默认会崩或乱码。
    这样 naming/kb/feed/runtime/启动探针全都起在 utf-8，不只 worker（#862 当时只收了 worker）。

    ``passthrough``：调用方额外要放行的**名字**（如 ``HARNESS_EXECUTOR`` / 文献 env keys）；
    ``extra``：要强制塞进去的**键值**（如 ``LLM_API_KEY``）。``PYTHONPATH`` 一律指 harness 根。
    POSIX 上 ``system_env_passthrough()`` / ``utf8_mode_env()`` 都是空集，逐字无副作用。
    """
    from app.services.harness_imports import harness_module

    platform_env = harness_module("shared.lib.platform_env")
    names = _HARNESS_SUBPROCESS_PASSTHROUGH | set(passthrough)
    env = {key: value for key, value in os.environ.items() if key in names}
    env.update(platform_env.system_env_passthrough())
    env.update(platform_env.utf8_mode_env())
    env["PYTHONPATH"] = str(root)
    if sys.dont_write_bytecode:
        # 这个进程被要求别写 .pyc，它起的子进程也一样 —— 子进程 import 的是**同一批
        # 文件**，写不写得下取决于那些文件在哪，而不取决于谁在 import。
        #
        # 真机现场（2026-09-16 打 0.5.0）：Mac 的 .app 是签好名的，壳用 `-B` 起后端，
        # 而 worker 的环境是从这份白名单重建的 —— `PYTHONDONTWRITEBYTECODE` 不在里面，
        # 于是 worker 往 `Resources/python/.../site-packages/**/__pycache__/` 里写了一
        # 堆 .pyc，**签名当场失效**（`codesign --verify` 列出 "file added: …"）。
        # 用户看到的会是"应用已损坏"，而打包机上一切正常 —— 幸好装完自检在验收之后
        # 又验了一次签名，把它按住了。
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env

def _provider_base_url(config: ModelBackendConfig) -> str | None:
    if config.base_url:
        return config.base_url
    return {
        "deepseek": "https://api.deepseek.com",
        "kimi": "https://api.moonshot.cn/v1",
        "openai": "https://api.openai.com/v1",
    }.get(config.provider)


def harness_bridge_supported(config: ModelBackendConfig) -> bool:
    return settings.harness_bridge_enabled and config.provider in HARNESS_COMPATIBLE_PROVIDERS


async def _drain_stderr(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if total < 16_384:
            chunks.append(chunk[: 16_384 - total])
            total += len(chunks[-1])
    return b"".join(chunks).decode("utf-8", errors="replace")
