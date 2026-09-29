"""双击之后，**首页真的出得来**。

## 病例（2026-09-07 真机）

刚装好的 `.app` 双击起来，`/` 返回 `{"detail":"Not Found"}`。API 全好、
`/health` 200、`doctor` 把界面和 harness 都报得清清楚楚 —— 只是**没有界面**。

真因是一个导入顺序：`app.main` 用 `if settings.static_ui_root:` 决定挂不挂静态
界面中间件，而 `Settings` 是**导入那一刻对环境的一次快照**。启动器在
`os.environ.setdefault("STATIC_UI_ROOT", …)` 之前就 `from app.config import …`
（PR#834 挪进来的），于是后端拿到的是一份"什么都没配"的快照。日志里同时写着
`HARNESS_ROOT 未配置`，也是同一个原因。

## 为什么现有判据一条都没红

`test_the_launcher_needs_nothing_configured` 那几条验的是**环境变量被设上了**，
而它们在同一个进程里、`app.config` 早被别的测试导入过 —— 顺序问题在那种视角
下不存在。验收脚本 `personal_smoke.py` 走的是 API，从不要一次首页。

于是"界面没了"这件事，在 1051 条测试 + 一整套验收里，**没有任何一处会发现**。

判据得落在用户会做的那件事上：**起一次，要一次首页，看看是不是 HTML。**
而且必须在一个**新进程**里 —— 导入顺序只在干净的导入状态下才成立，这也正是
生产里的状态。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

#: 仓库根（platform/backend/tests → 上溯三级）。
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "platform" / "backend"


def _a_package_shaped_like_the_real_one(tmp_path: Path) -> Path:
    """按装出来的包的真实布局摆一份 —— `app/` 旁边就是 `app/static_ui/`。

    必须真摆一份，不能靠 `STATIC_UI_ROOT` 替启动器把答案填好：真机上没人配这个
    变量，界面是**启动器自己找出来的**（`find_static_ui()` 从 `app/launcher.py`
    的同级目录找）。把答案预先塞进环境，测的就只剩"变量拼对了"，而
    2026-09-07 那次变量拼得完全正确，只是没人看它。

    3.4 MB 的目录复制一次，换一条真能拦住这类事故的判据。
    """
    stage = tmp_path / "site-packages"
    stage.mkdir(parents=True)
    shutil.copytree(BACKEND / "app", stage / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "static_ui"))
    static = stage / "app" / "static_ui"
    static.mkdir()
    (static / "index.html").write_text(
        "<!doctype html><title>ScienceMate</title><p>hello",
        encoding="utf-8")
    (static / "404.html").write_text("<!doctype html><title>没有这一页</title>",
                                     encoding="utf-8")
    # harness 也在 `app/` 旁边（打包脚本就是这么摆的）。判据用的是
    # `core/agent_loop.py` 在不在 —— 与 `find_the_harness()` 同一条，所以摆一个
    # 空文件就够，这条用例不跑 agent。
    loop = stage / "app" / "harness" / "core" / "agent_loop.py"
    loop.parent.mkdir(parents=True)
    loop.write_text("", encoding="utf-8")
    return stage


def _start_and_ask(tmp_path: Path, script: str) -> dict:
    """在**新进程**里跑一次 `launcher.main(["start"])`。

    新进程是判据的一部分：导入顺序只在干净的导入状态下才成立，而那正是生产
    里的状态 —— 同一个 pytest 进程里 `app.config` 早被别的用例导过，顺序问题
    在那种视角下根本不存在（这就是 1051 条测试一条都没红的原因）。
    """
    stage = _a_package_shaped_like_the_real_one(tmp_path)
    env = {
        **os.environ,
        "PYTHONPATH": str(stage),
        "PLATFORM_DATA_ROOT": str(tmp_path / "data"),
        "HARNESS_FRAMEWORK_HOME": str(tmp_path / "data"),
    }
    # ⚠️ 这两个必须**不在**环境里：真机上没人配它们，启动器自己找。预先塞进去
    # 就把被测的那件事绕过去了。
    env.pop("STATIC_UI_ROOT", None)
    env.pop("HARNESS_ROOT", None)
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=300,
    )
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, (
        f"子进程没给出结果（exit={done.returncode}）。\n"
        f"stdout:\n{done.stdout[-2000:]}\nstderr:\n{done.stderr[-2000:]}"
    )
    return json.loads(line[len("RESULT "):])


_ASK_FOR_THE_PAGE = textwrap.dedent(
    """
    import json, sys, types, asyncio
    answer = {}

    def fake_run(app_path, **kwargs):
        # 启动器已经把环境备齐了 —— 现在才第一次导入 app.main，与生产里
        # uvicorn 的时机一致。
        import httpx
        from app.main import app

        async def ask():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as client:
                page = await client.get("/")
                health = await client.get("/health")
                return {"page_status": page.status_code,
                        "page_head": page.text[:200],
                        "health_status": health.status_code}

        answer.update(asyncio.run(ask()))

    fake = types.ModuleType("uvicorn"); fake.run = fake_run
    sys.modules["uvicorn"] = fake
    assert "app.config" not in sys.modules, "启动器还没跑，config 就被导进来了"
    from app import launcher
    answer["exit_code"] = launcher.main(["start", "--no-browser"])
    print("RESULT " + json.dumps(answer))
    """
)

_ASK_WHAT_THE_SNAPSHOT_SAW = textwrap.dedent(
    """
    import json, sys, types
    answer = {}

    def fake_run(app_path, **kwargs):
        from app.config import settings
        answer["snapshot_static_ui"] = bool(settings.static_ui_root)
        answer["snapshot_harness"] = bool(settings.harness_root)

    fake = types.ModuleType("uvicorn"); fake.run = fake_run
    sys.modules["uvicorn"] = fake
    from app import launcher
    launcher.main(["start", "--no-browser"])
    print("RESULT " + json.dumps(answer))
    """
)


def test_the_home_page_is_html_not_a_json_error(tmp_path) -> None:
    """起一次，要一次首页 —— 必须是 HTML。

    这是双击图标之后发生的第一件事；它坏掉的时候，API 全好、health 200、
    `doctor` 把界面和 harness 都报得清清楚楚，只是**没有界面**。
    """
    result = _start_and_ask(tmp_path, _ASK_FOR_THE_PAGE)
    assert result["exit_code"] == 0
    assert result["health_status"] == 200, "后端自己都没起来，下面那条就无从谈起"
    assert result["page_status"] == 200, (
        f"首页返回 {result['page_status']}：{result['page_head']!r}\n"
        "—— 装出来的包双击之后就是这个样子（真机 2026-09-07）。"
    )
    assert "<" in result["page_head"], f"首页不是 HTML：{result['page_head']!r}"


def test_the_environment_is_ready_before_anything_reads_it(tmp_path) -> None:
    """启动器把环境备齐**在**任何人导入 `app.config` 之前。

    这条是上一条的机械版：`Settings` 是导入那一刻对环境的快照，晚一步就永远
    晚了。判据落在"快照里有没有"上，而不是"环境变量最终有没有" —— 后者在真机
    上是**成立的**，界面照样没了。
    """
    answer = _start_and_ask(tmp_path, _ASK_WHAT_THE_SNAPSHOT_SAW)
    assert answer.get("snapshot_static_ui") is True, (
        "uvicorn 拿到手的那份 Settings 快照里没有界面 —— app.main 于是不挂静态"
        "界面中间件，首页 404"
    )
    assert answer.get("snapshot_harness") is True, (
        "同上：快照里没有 harness，日志里会写「HARNESS_ROOT 未配置」，"
        "而发消息不会有回复"
    )
