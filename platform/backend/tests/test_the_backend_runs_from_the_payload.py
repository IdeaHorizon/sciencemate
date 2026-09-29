"""后端 `app/` 走载荷（#953 ⑤）—— 自更新带来的后端由随包的启动器在同一进程里接过来。

## 这一步在整件事里的位置

载荷原来只有 harness + 界面；壳由壳自己换（④）；剩下后端 `app/`（API、自更新逻辑、
启动器）一改就得让人重装。现在第二个归档把 `app/` 也带上，随包的 launcher 在
import config / 开端口之前把启动整个交给载荷里那份 launcher —— **同一个 pid**，壳追踪的
进程没变（这就是为什么不 exec）。

## 判据钉在哪

- 真的接过去：载荷里的 `app.launcher.main` 被调、argv 原样、返回值原样、`app.*` 都指向载荷。
- 循环终止：载荷里那份 launcher 再跑到这里，发现自己就住在载荷里 → None。
- 版本闸：不比随包新的载荷，不接（走的是 `_the_applied_payload` 同一道闸）。
- 两端的「说带了 app 就得有 launcher.py」：发布端 `prove_the_payload_verifies`、客户端
  `download_and_stage` 各一道。
- 打包器把装配态 `app/` 收进 extras，且不带界面 / harness / 测试。
- 接线：`main()` 在 `prepare_the_environment()` 之前调 `hand_over_to_the_payload_app`。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from app import launcher
from app.services import self_update as su
from tests.test_self_update import _manifest, _tar

REPO = Path(__file__).resolve().parents[3]


def _by_path(name: str):
    spec = importlib.util.spec_from_file_location(f"afs_package_{name}", REPO / "scripts" / "package" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ───────────────────────────────────────────── 夹具

def _applied_payload(root: Path, version: str, *, with_app: bool = True) -> Path:
    """数据根里一份已切换的载荷；`with_app` 时 extras/app 是一个会留痕迹的假 app 包。"""
    payload = root / "payload" / version
    (payload / "harness" / "core").mkdir(parents=True)
    (payload / "harness" / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    (payload / "harness" / su.VERSION_MARKER).write_text(version + "\n", encoding="utf-8")
    (payload / "static_ui").mkdir()
    (root / "payload" / "current.json").write_text(json.dumps({"version": version, "previous": None,
                                                              "applied_at": "2026-09-12T00:00:00+00:00"}), encoding="utf-8")
    if with_app:
        app = payload / "extras" / "app"
        app.mkdir(parents=True)
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "launcher.py").write_text(
            "import json, pathlib\n"
            "MARK = pathlib.Path(__file__).resolve().parent / 'ran.json'\n"
            "def main(argv=None):\n"
            "    MARK.write_text(json.dumps({'argv': argv, 'file': __file__}))\n"
            "    return 42\n", encoding="utf-8")
    return payload


def _bundled(root: Path, version: str) -> Path:
    harness = root / "app-bundle" / "harness"
    (harness / "core").mkdir(parents=True)
    (harness / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    (harness / su.VERSION_MARKER).write_text(version + "\n", encoding="utf-8")
    return harness


@pytest.fixture
def restore_the_app_modules():
    """接管会改 sys.path / sys.modules —— 测完放回去，别让后面的测试跑在假 app 上。"""
    modules = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
    path = list(sys.path)
    yield
    for k in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
        del sys.modules[k]
    sys.modules.update(modules)
    sys.path[:] = path


# ───────────────────────────────────────────── 接过去

def test_the_launcher_hands_the_whole_start_to_the_payload_app(tmp_path, monkeypatch, restore_the_app_modules) -> None:
    payload = _applied_payload(tmp_path, "2.0.0")
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))

    assert launcher.hand_over_to_the_payload_app(["start", "--port", "1"]) == 42
    ran = json.loads((payload / "extras" / "app" / "ran.json").read_text(encoding="utf-8"))
    assert ran["argv"] == ["start", "--port", "1"], "argv 没原样交过去"
    assert Path(ran["file"]).resolve().parent == (payload / "extras" / "app").resolve()
    assert Path(sys.modules["app"].__file__).resolve().parent == (payload / "extras" / "app").resolve(), (
        "sys.modules 里的 app 还是随包那份 —— uvicorn 按字符串加载 app.main 时会拿到旧代码")
    assert sys.path[0] == str(payload / "extras")


def test_the_payload_launcher_does_not_hand_over_again(tmp_path, monkeypatch, restore_the_app_modules) -> None:
    """循环终止：接过来的那份 launcher 跑到同一处，发现自己就住在载荷里。"""
    payload = _applied_payload(tmp_path, "2.0.0")
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))
    # 把「我在哪」指到载荷里 —— 等价于载荷里那份 launcher 在问这个问题
    monkeypatch.setattr(launcher, "__file__", str(payload / "extras" / "app" / "launcher.py"))
    assert launcher.hand_over_to_the_payload_app(["start"]) is None
    assert not (payload / "extras" / "app" / "ran.json").exists()


def test_a_payload_no_newer_than_the_bundle_is_not_handed_over(tmp_path, monkeypatch, restore_the_app_modules) -> None:
    """版本闸与 harness 那道是同一道（#958）：重装了新包、数据根里留着旧载荷，不接。"""
    payload = _applied_payload(tmp_path, "1.0.0")
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))
    assert launcher.hand_over_to_the_payload_app(["start"]) is None
    assert not (payload / "extras" / "app" / "ran.json").exists()


def test_a_payload_without_an_app_starts_the_bundled_one(tmp_path, monkeypatch, restore_the_app_modules) -> None:
    _applied_payload(tmp_path, "2.0.0", with_app=False)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))
    assert launcher.hand_over_to_the_payload_app(["start"]) is None
    assert Path(sys.modules["app"].__file__).resolve().parent == Path(launcher.__file__).resolve().parent


def test_half_an_app_in_the_payload_is_not_handed_over(tmp_path, monkeypatch, restore_the_app_modules) -> None:
    """extras/app 在、launcher.py 不在（客户端闸漏了 / 有人手删）：接过去就是起不来的后端。"""
    payload = _applied_payload(tmp_path, "2.0.0")
    (payload / "extras" / "app" / "launcher.py").unlink()
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))
    assert launcher.the_payload_app(payload) is None
    assert launcher.hand_over_to_the_payload_app(["start"]) is None
    assert Path(sys.modules["app"].__file__).resolve().parent == Path(launcher.__file__).resolve().parent


def _staged_not_yet_applied(root: Path, version: str) -> Path:
    """装完更新、还没重启过的样子：载荷在 staged/、暂存指针在、current.json **不在**。"""
    payload = _applied_payload(root, version)
    staged = root / "payload" / "staged" / version
    staged.parent.mkdir(parents=True, exist_ok=True)
    payload.rename(staged)
    (root / "payload" / "current.json").unlink()
    (root / "payload" / "staged.json").write_text(json.dumps({"version": version, "staged_at": "2026-09-12T00:00:00+00:00"}),
                                                 encoding="utf-8")
    assert su.staged_version(root) == version and su.active_payload_dir(root) is None
    return root / "payload" / version


def test_the_first_restart_after_an_install_already_runs_the_payload_app(tmp_path, monkeypatch, restore_the_app_modules) -> None:
    """真机 2026-09-12 抓到的顺序 bug：接管跑在切换暂存之前 → 指针还没写 → 「没有载荷」→
    新后端要等下一次启动。判据走真的 `main()`：暂存态进去，载荷里的 main 出来。"""
    payload = _staged_not_yet_applied(tmp_path, "2.0.0")
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: _bundled(tmp_path, "1.0.0"))

    assert launcher.main(["doctor"]) == 42, "装完更新的第一次启动没接到载荷里的 app/"
    assert su.active_payload_dir(tmp_path) == payload, "暂存没切成当前载荷"
    ran = json.loads((payload / "extras" / "app" / "ran.json").read_text(encoding="utf-8"))
    assert ran["argv"] == ["doctor"]


def test_main_hands_over_before_it_prepares_anything() -> None:
    """接线：先切换暂存、再接管、再备环境 —— 之后随包这份代码就已经在场了。"""
    source = (REPO / "platform/backend/app/launcher.py").read_text(encoding="utf-8")
    body = source[source.index("def main("):]
    assert body.index("switch_to_the_staged_update()") < body.index("hand_over_to_the_payload_app(argv)"), (
        "接管排在切换暂存之前 —— 装完更新的第一次重启会漏接")
    assert body.index("hand_over_to_the_payload_app(argv)") < body.index("prepare_the_environment()")
    prepare = source[source.index("def prepare_the_environment("):source.index("def main(")]
    assert "apply_staged_at_launch" not in prepare, "切换暂存不能有两处 —— 会各自演化"
    assert "if handed is not None:\n        return handed" in body, "接过去了还往下跑 —— 两份后端同时起"
    assert "os.execv" not in body, "壳追踪的是原 pid；exec 在 Windows 上是 spawn+退出"


# ───────────────────────────────────────────── 客户端那道闸

def _extras_blob(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _both(files: dict[str, bytes]):
    main_blob = _tar("1.0.0")
    extras_blob = _extras_blob(files)
    units = sorted({name.split("/", 1)[0] if not name.startswith("shell/") else "/".join(name.split("/")[:2]) for name in files})
    doc = {"archive": "extras-1.0.0.tar.gz", "sha256": hashlib.sha256(extras_blob).hexdigest(),
           "size": len(extras_blob), "units": units, "files": {u: 1 for u in units}, "shell": {}}
    manifest = json.loads(_manifest("1.0.0", main_blob, extras=doc))
    streams = {"https://h/payload-1.0.0.tar.gz": main_blob, "https://h/extras-1.0.0.tar.gz": extras_blob}

    def _stream(url: str, sink) -> None:
        sink(streams[url])
    return manifest, _stream


def test_an_app_unit_is_staged_where_the_launcher_looks(tmp_path: Path) -> None:
    manifest, stream = _both({"app/__init__.py": b"", "app/launcher.py": b"def main(argv=None): return 0\n"})
    staged = su.download_and_stage(tmp_path, manifest, "https://h/payload-1.0.0.tar.gz", stream,
                                   extras_url="https://h/extras-1.0.0.tar.gz")
    su.apply_staged_at_launch(tmp_path)
    applied = su.active_payload_dir(tmp_path)
    assert launcher.the_payload_app(applied) == applied / "extras" / "app"
    assert staged.name == "1.0.0"


def test_an_app_unit_without_a_launcher_voids_the_staging(tmp_path: Path) -> None:
    manifest, stream = _both({"app/__init__.py": b"", "app/main.py": b""})
    with pytest.raises(su.UpdateError, match="launcher.py"):
        su.download_and_stage(tmp_path, manifest, "https://h/payload-1.0.0.tar.gz", stream,
                              extras_url="https://h/extras-1.0.0.tar.gz")
    assert not (tmp_path / "payload" / "staged").exists(), "半个 app 包被暂存了 —— 下次启动接过去就起不来"


# ───────────────────────────────────────────── 发布端

def test_the_mac_packager_ships_the_backend_app_without_the_things_that_are_not_it(tmp_path, monkeypatch) -> None:
    mac = _by_path("build_mac_app")
    monkeypatch.setattr(mac, "DIST", tmp_path / "dist")
    # 名单归 git（`scripts/package/git_tracked.py`）：后端代码和它的测试是提交过的；界面、
    # harness、字节码是装配/运行时才放进 `app/` 的 —— 和真打包时同一个局面。
    repo = tmp_path / "checkout"
    backend = repo / "platform" / "backend"
    for rel in ("app/__init__.py", "app/launcher.py", "app/main.py", "app/services/self_update.py",
                "app/tests/test_x.py"):
        (backend / rel).parent.mkdir(parents=True, exist_ok=True)
        (backend / rel).write_text("", encoding="utf-8")
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "--no-verify", "-m", "app")):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=repo, check=True, capture_output=True)
    for rel in ("app/static_ui/index.html", "app/harness/core/agent_loop.py",
                "app/__pycache__/launcher.cpython-314.pyc"):
        (backend / rel).parent.mkdir(parents=True, exist_ok=True)
        (backend / rel).write_text("", encoding="utf-8")
    monkeypatch.setattr(mac, "REPO", repo)
    monkeypatch.setattr(mac, "BACKEND", backend)
    app = tmp_path / "ScienceMate.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / mac.BINARY_NAME).write_bytes(b"\xcf\xfa\xed\xfe")

    extras = mac.the_extras_for_the_payload(app, None)
    assert "app" in extras
    shipped = sorted(str(p.relative_to(extras["app"])) for p in extras["app"].rglob("*") if p.is_file())
    assert shipped == ["__init__.py", "launcher.py", "main.py", "services/self_update.py"], shipped


def test_the_release_side_refuses_an_app_unit_without_a_launcher(tmp_path, monkeypatch) -> None:
    """发布端先替客户端核一遍 —— 发出去的就是收得下的。"""
    from tests.test_self_update import _keypair
    pr = _by_path("payload_release")
    private, public = _keypair()
    monkeypatch.setattr(pr, "baked_public_key", lambda: public)
    monkeypatch.setattr(pr.release_keys, "load_private", lambda: private)
    harness = tmp_path / "harness"; (harness / "core").mkdir(parents=True)
    (harness / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    ui = tmp_path / "ui"; ui.mkdir(); (ui / "index.html").write_text("", encoding="utf-8")
    half = tmp_path / "half-app"; half.mkdir(); (half / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="launcher.py"):
        pr.build_the_payload(harness, ui, tmp_path / "out", version="1.0.0", extras={"app": half})
    whole = tmp_path / "app"; whole.mkdir(); (whole / "launcher.py").write_text("", encoding="utf-8")
    manifest = pr.build_the_payload(harness, ui, tmp_path / "out2", version="1.0.0", extras={"app": whole})
    assert manifest["extras"]["units"] == ["app"] and manifest["_signed"]
