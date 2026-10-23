"""开发者模式：桌面版从一个源码 checkout 跑（2026-10-23）。

同事改了代码要在桌面版里看效果。装好的那份 harness / 界面 / 后端 `app/` 是打包时拷进去的，
改自己的 checkout 对它没影响；以前只有「等重新打包」或「终端起后端 + 浏览器」两条路。

## 判据钉在哪

- 声明（`<数据根>/developer.json`）指着一个合格的 checkout 时，launcher 把 harness、界面、
  后端 `app/` 三个答案都指到它：`HARNESS_ROOT` / `STATIC_UI_ROOT` 是它的，启动整个交给它的
  `app.launcher.main`（同一进程，和接载荷那条路一样）。
- 源码胜过载荷：数据根里哪怕有更新的载荷，声明了 checkout 就不看载荷。
- 循环终止：checkout 里那份 launcher 再跑到这里，发现自己就住在 checkout 里 → 照常往下。
- 没构建过界面的 checkout：界面钉住随包 / 载荷那份，而不是没有界面。
- 声明了但目录不合格（删了、改名了）：不拦启动，照常走载荷 / 随包；关于页说明问题。
- 关于页的三个口：读 / 写（先验目录）/ 撤；只有个人档有；改了声明就允许重启。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import launcher
from app.services import developer_source as ds
from app.services import self_update as su
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401
from tests.test_the_backend_runs_from_the_payload import _applied_payload, _bundled, restore_the_app_modules  # noqa: F401

REPO = Path(__file__).resolve().parents[3]


# ───────────────────────────────────────────── 夹具

def _checkout(root: Path, *, with_ui: bool = True, with_backend: bool = True) -> Path:
    """一个长得像 harness-framework checkout 的目录；后端 launcher 会留痕迹。"""
    src = root / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    (src / ".git").mkdir()
    if with_backend:
        app = src / "platform" / "backend" / "app"
        app.mkdir(parents=True)
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "launcher.py").write_text(
            "import json, os, pathlib\n"
            "MARK = pathlib.Path(__file__).resolve().parent / 'ran.json'\n"
            "def main(argv=None):\n"
            "    MARK.write_text(json.dumps({'argv': argv, 'file': __file__,\n"
            "        'harness': os.environ.get('HARNESS_ROOT'), 'ui': os.environ.get('STATIC_UI_ROOT')}))\n"
            "    return 43\n", encoding="utf-8")
    if with_ui:
        out = src / "platform" / "frontend" / "out"
        out.mkdir(parents=True)
        (out / "index.html").write_text("<html></html>", encoding="utf-8")
    return src


def _declare(data_root: Path, source_root: Path | str) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / ds.DECLARATION).write_text(json.dumps({"source_root": str(source_root)}), encoding="utf-8")


@pytest.fixture
def clean_explicit_env(monkeypatch):
    """接管会设 HARNESS_ROOT / STATIC_UI_ROOT —— 进去前清掉、出来后放回。"""
    for name in ("HARNESS_ROOT", "STATIC_UI_ROOT"):
        monkeypatch.delenv(name, raising=False)


# ───────────────────────────────────────────── 目录合不合格

def test_a_checkout_is_recognised_by_what_the_launcher_itself_looks_for(tmp_path) -> None:
    src = _checkout(tmp_path)
    found = ds.inspect(src)
    assert found.harness == src.resolve()
    assert found.backend_app == (src / "platform" / "backend" / "app").resolve()
    assert found.static_ui == (src / "platform" / "frontend" / "out").resolve()


def test_an_unbuilt_interface_is_not_a_fault(tmp_path) -> None:
    found = ds.inspect(_checkout(tmp_path, with_ui=False))
    assert found.static_ui is None and found.has_static_ui is False


@pytest.mark.parametrize("make, says", [
    (lambda root: root / "nowhere", "目录不存在"),
    (lambda root: (root / "empty").mkdir() or root / "empty", "没有 core/agent_loop.py"),
    (lambda root: _checkout(root, with_backend=False), "没有后端"),
])
def test_a_directory_that_is_not_a_checkout_says_which_piece_is_missing(tmp_path, make, says) -> None:
    with pytest.raises(ds.DeveloperSourceError, match=says) as caught:
        ds.inspect(make(tmp_path))
    assert caught.value.en and caught.value.said_in("en") != str(caught.value), "英文那句没写"


def test_a_relative_path_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _checkout(tmp_path)
    with pytest.raises(ds.DeveloperSourceError, match="绝对路径"):
        ds.inspect("src")


def test_the_declaration_round_trips_and_can_be_withdrawn(tmp_path) -> None:
    assert ds.read_declared(tmp_path) is None
    ds.write_declared(tmp_path, "/some/where")
    assert ds.read_declared(tmp_path) == "/some/where"
    ds.write_declared(tmp_path, None)
    assert ds.read_declared(tmp_path) is None
    ds.write_declared(tmp_path, None)   # 撤两次不报错
    (tmp_path / ds.DECLARATION).write_text("not json", encoding="utf-8")
    assert ds.read_declared(tmp_path) is None, "写坏的声明当没有，不许拦启动"


# ───────────────────────────────────────────── 启动接过去

def test_the_launcher_hands_the_whole_start_to_the_checkout(tmp_path, monkeypatch, restore_the_app_modules, clean_explicit_env) -> None:
    src = _checkout(tmp_path)
    data_root = tmp_path / "data"
    _declare(data_root, src)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(data_root))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))

    assert launcher.main(["doctor"]) == 43, "声明了源码目录，启动没交给它的 app/"
    ran = json.loads((src / "platform" / "backend" / "app" / "ran.json").read_text(encoding="utf-8"))
    assert ran["argv"] == ["doctor"]
    assert Path(ran["harness"]) == src.resolve(), "HARNESS_ROOT 没指到 checkout —— worker 跑的还是随包那份"
    assert Path(ran["ui"]) == (src / "platform" / "frontend" / "out").resolve(), "STATIC_UI_ROOT 没指到 checkout 构建的界面"
    assert Path(launcher.sys.modules["app"].__file__).resolve().parent == (src / "platform" / "backend" / "app").resolve(), (
        "sys.modules 里的 app 还是随包那份 —— uvicorn 按字符串加载 app.main 时会拿到旧代码")
    assert launcher.sys.modules["app.launcher"].RUNNING_DECLARED_CHECKOUT is True, (
        "接过去的那份 launcher 没被打上「开发者模式起的」记号 —— 撤了声明之后没人知道要重启")


def test_the_checkout_wins_over_a_newer_payload(tmp_path, monkeypatch, restore_the_app_modules, clean_explicit_env) -> None:
    """开发者要看的是自己改的代码；载荷是别人发的。"""
    src = _checkout(tmp_path)
    data_root = tmp_path / "data"
    payload = _applied_payload(data_root, "9.0.0")
    _declare(data_root, src)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(data_root))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))

    assert launcher.main(["doctor"]) == 43
    assert not (payload / "extras" / "app" / "ran.json").exists(), "载荷的 app/ 被接了 —— 源码被别人发的代码换掉"
    assert Path(os.environ["HARNESS_ROOT"]) == src.resolve()


def test_an_unbuilt_interface_pins_the_installed_one(tmp_path, monkeypatch, restore_the_app_modules, clean_explicit_env) -> None:
    """checkout 没跑过 npm run build：界面用随包 / 载荷那份，而不是没有界面。
    不钉住的话，接过去的那份 launcher 按**它自己的位置**找随包界面 —— checkout 里没有。"""
    src = _checkout(tmp_path, with_ui=False)
    data_root = tmp_path / "data"
    payload = _applied_payload(data_root, "9.0.0")
    _declare(data_root, src)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(data_root))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))

    assert launcher.main(["doctor"]) == 43
    ran = json.loads((src / "platform" / "backend" / "app" / "ran.json").read_text(encoding="utf-8"))
    assert Path(ran["ui"]) == (payload / "static_ui").resolve()


def test_the_checkout_launcher_does_not_hand_over_again(tmp_path, monkeypatch, restore_the_app_modules, clean_explicit_env) -> None:
    """循环终止：checkout 里那份 launcher 跑到同一处，发现自己就住在这儿 —— 但环境已经指好。"""
    src = _checkout(tmp_path)
    data_root = tmp_path / "data"
    _declare(data_root, src)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(data_root))
    monkeypatch.setattr(launcher, "__file__", str(src / "platform" / "backend" / "app" / "launcher.py"))

    checkout = launcher.the_source_checkout_in_charge()
    assert checkout is not None
    assert launcher.hand_over_to_the_source_checkout(checkout, ["start"]) is None
    assert not (src / "platform" / "backend" / "app" / "ran.json").exists()
    assert Path(os.environ["HARNESS_ROOT"]) == src.resolve(), (
        "直接从 checkout 起后端时环境也得指到 checkout —— 否则 find_the_harness 会被载荷抢走")


def test_a_declaration_that_no_longer_points_at_a_checkout_does_not_block_the_start(tmp_path, monkeypatch, restore_the_app_modules, clean_explicit_env) -> None:
    data_root = tmp_path / "data"
    payload = _applied_payload(data_root, "2.0.0")
    _declare(data_root, tmp_path / "deleted-checkout")
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(data_root))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))

    assert launcher.the_source_checkout_in_charge() is None
    assert launcher.main(["doctor"]) == 42, "声明失效时应照常走载荷那条路"
    assert (payload / "extras" / "app" / "ran.json").exists()
    assert getattr(launcher.sys.modules["app.launcher"], "RUNNING_DECLARED_CHECKOUT", False) is False, "接载荷不是开发者模式"


def test_main_asks_for_the_checkout_before_it_looks_at_the_payload() -> None:
    """接线：先问开发者模式，声明了就不看载荷；两条路都排在备环境之前。"""
    source = (REPO / "platform/backend/app/launcher.py").read_text(encoding="utf-8")
    body = source[source.index("def main("):]
    assert body.index("switch_to_the_staged_update()") < body.index("the_source_checkout_in_charge()")
    assert body.index("the_source_checkout_in_charge()") < body.index("hand_over_to_the_payload_app(argv)")
    assert body.index("hand_over_to_the_payload_app(argv)") < body.index("prepare_the_environment()")
    assert "if checkout is not None:\n        handed = hand_over_to_the_source_checkout(checkout, argv)\n    else:\n        handed = hand_over_to_the_payload_app(argv)" in body, (
        "声明了 checkout 还去看载荷 —— checkout 里的 launcher 没有随包版本标记，任何载荷都会被当成更新")
    assert body.index("the_source_checkout_in_charge()") < body.index("from app.config import"), (
        "问开发者模式要在 import config 之前 —— Settings 是导入那一刻对环境的快照")


# ───────────────────────────────────────────── 关于页的口

def test_describe_says_whether_the_running_app_is_the_declared_one(tmp_path) -> None:
    src = _checkout(tmp_path)
    data_root = tmp_path / "data"
    bundled_app = tmp_path / "bundle" / "app"
    bundled_app.mkdir(parents=True)
    checkout_app = src / "platform" / "backend" / "app"

    told = ds.describe(data_root, bundled_app, "zh", started_by_developer_mode=False)
    assert told == {"declared": None, "active": False, "restart_needed": False, "problem": None,
                    "harness": None, "backend_app": None, "static_ui": None, "git": None, "running_from": None}

    _declare(data_root, src)
    told = ds.describe(data_root, bundled_app, "zh", started_by_developer_mode=False)
    assert told["declared"] == str(src) and told["active"] is False and told["restart_needed"] is True
    assert Path(told["harness"]) == src.resolve() and Path(told["static_ui"]) == (src / "platform" / "frontend" / "out").resolve()

    told = ds.describe(data_root, checkout_app, "zh", started_by_developer_mode=True)
    assert told["active"] is True and told["restart_needed"] is False, "跑的就是声明的那份，不该再要求重启"

    ds.write_declared(data_root, None)
    told = ds.describe(data_root, checkout_app, "zh", started_by_developer_mode=True)
    assert told["declared"] is None and told["restart_needed"] is True, "撤了声明、进程还是开发者模式起的 —— 重启才回到随包那份"
    assert Path(told["running_from"]) == src.resolve(), "「重启后才回到安装的版本」那句要说清现在是从哪跑的"

    told = ds.describe(data_root, checkout_app, "zh", started_by_developer_mode=False)
    assert told["restart_needed"] is False, "终端里从源码起的后端不归开发者模式管，不该被要求重启"

    _declare(data_root, tmp_path / "gone")
    told = ds.describe(data_root, bundled_app, "en", started_by_developer_mode=False)
    assert told["problem"] == f"Directory does not exist: {tmp_path / 'gone'}" and told["active"] is False
    assert told["restart_needed"] is False


@pytest.mark.asyncio
async def test_the_about_page_can_set_check_and_withdraw_it(runtime_client, monkeypatch, tmp_path) -> None:
    from app.config import settings

    src = _checkout(tmp_path)
    data_root = tmp_path / "data"
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "platform_data_root", str(data_root))
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    headers = {**_headers(token), "X-Interface-Language": "en"}

    told = (await client.get("/api/v1/update/developer", headers=headers)).json()
    assert told["declared"] is None and told["restart_needed"] is False

    refused = await client.put("/api/v1/update/developer", json={"source_root": str(tmp_path / "nope")}, headers=headers)
    assert refused.status_code == 400
    assert refused.json()["detail"] == f"Directory does not exist: {tmp_path / 'nope'}", "拒绝要说英文 —— 这一屏是英文"
    assert ds.read_declared(data_root) is None, "不合格的目录不许写进声明"

    set_ = await client.put("/api/v1/update/developer", json={"source_root": str(src)}, headers=headers)
    assert set_.status_code == 200
    assert set_.json()["declared"] == str(src.resolve()) and set_.json()["restart_needed"] is True
    assert ds.read_declared(data_root) == str(src.resolve())

    # 真重启是 os._exit(3)（壳认这个码重新拉起）—— 测试进程不能真退，记下来就行。
    asked: list[float] = []
    monkeypatch.setattr(su, "schedule_restart", lambda delay_s=0.5: asked.append(delay_s))
    restart = await client.post("/api/v1/update/restart", headers=headers)
    assert restart.status_code == 200 and restart.json()["restarting"] is True, "改了声明就该允许重启"
    assert asked, "答了 restarting 却没安排重启"

    gone = await client.delete("/api/v1/update/developer", headers=headers)
    assert gone.status_code == 200 and gone.json()["declared"] is None
    assert ds.read_declared(data_root) is None


@pytest.mark.asyncio
async def test_restart_without_anything_to_restart_for_is_still_refused(runtime_client, monkeypatch, tmp_path) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path / "data"))
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    refused = await client.post("/api/v1/update/restart", headers=_headers(token))
    assert refused.status_code == 409


@pytest.mark.asyncio
async def test_an_organisation_server_has_no_developer_mode(runtime_client, monkeypatch, tmp_path) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "profile", "org")
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path / "data"))
    client, _ = runtime_client
    token = await _token(client, "institution.admin@atrium.local")
    for method, kwargs in (("get", {}), ("put", {"json": {"source_root": "/x"}}), ("delete", {})):
        response = await getattr(client, method)("/api/v1/update/developer", headers=_headers(token), **kwargs)
        assert response.status_code == 403, method
