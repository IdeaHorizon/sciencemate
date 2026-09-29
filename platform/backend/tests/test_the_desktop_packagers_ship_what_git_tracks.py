"""桌面包（Mac / Windows）里的 harness 和载荷的 `app/`：**git 跟踪的文件，别的一个都不带**。

服务器包在 PR#1144 换成了问 git（`scripts/package/git_tracked.py`），桌面的两个打包器还是整棵
`copytree` + 名字黑名单 —— 2026-09-23 主克隆上 `nodes/`、`shared/` 里 9 个 `.DS_Store` 会一起
装进包。这里锁住三处拷贝都走同一条规则。

判据是**等式**：包里的 == git 跟踪的 − 排除的。多一个文件都会出现在 diff 里，不需要为每种杂物
各写一条断言。在临时检出里造，不往本仓库的源码树里放东西（那正是 PR#1139 那次 flake 的形状）。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "scripts" / "package"

#: 临时检出里 git 跟踪的东西：三个 harness 包（带测试）、worker 入口和它 import 的根模块、
#: 后端 app（带测试）、不随包走的别处，外加一个中文名文件（Windows 构建机的 locale 是 cp1252）。
TRACKED = {
    ".gitignore": ".DS_Store\n__pycache__/\n",
    "core/agent_loop.py": "x = 1\n",
    "core/tests/test_loop.py": "x = 1\n",
    "shared/lib/io.py": "x = 1\n",
    "shared/tools/spec.yaml": "x: 1\n",
    "nodes/experiment/spec.yaml": "x: 1\n",
    "nodes/experiment/gone.py": "x = 1\n",
    "nodes/experiment/tests/test_node.py": "x = 1\n",
    "nodes/literature/data/reference/一二级学科.pdf": "%PDF\n",
    "platform_runtime.py": "def go():\n    import chat\n    return chat\n",
    "chat.py": "x = 1\n",
    "unimported.py": "x = 1\n",
    "docs/readme.md": "x\n",
    "platform/backend/app/launcher.py": "x = 1\n",
    "platform/backend/app/api/routes.py": "x = 1\n",
    "platform/backend/app/tests/test_app.py": "x = 1\n",
}

#: 跟踪着、但工作树里删掉了 —— 删它的人的意思是"不要了"。
DELETED = {"nodes/experiment/gone.py"}

#: 开发机的工作树里会有、git 不跟踪的东西：被 ignore 的（`.DS_Store`、字节码）和没被 ignore 的
#: （测试库日志、草稿模块）两种都有 —— 打包的口径是 tracked，两种都不该进包。
JUNK = (
    "nodes/.DS_Store",
    "shared/tools/.DS_Store",
    "core/__pycache__/agent_loop.cpython-314.pyc",
    "core/test-gw0.db-journal",
    "nodes/experiment/scratch_probe.py",
    "nodes/node_modules/x/index.js",
    "stray.py",
    "platform/backend/app/.DS_Store",
    "platform/backend/app/scratch.py",
    "platform/backend/app/api/__pycache__/routes.cpython-314.pyc",
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", PACKAGE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True)


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _shipped_by_the_rule(names, under: str) -> set[str]:
    """git 跟踪的 − 删掉的 − 路径里有 `tests` 的，相对 `under`。"""
    return {str(PurePosixPath(name).relative_to(under)) for name in set(names) - DELETED
            if name.startswith(f"{under}/") and "tests" not in PurePosixPath(name).parts}


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    for name, text in TRACKED.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", "tracked")
    for name in JUNK:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(b"junk")
    for name in DELETED:
        (repo / name).unlink()
    return repo


def _point_at(packager, repo: Path, tmp_path: Path) -> None:
    """`_load` 每次都是一份新的模块实例，直接改它的全局不会漏到别的测试里。"""
    packager.REPO = repo
    packager.BACKEND = repo / "platform" / "backend"
    packager.DIST = tmp_path / "dist"


@pytest.mark.parametrize("name, built", [
    ("build_mac_app", set()),
    # Windows 不打载荷，版本标记在装配这一步自己写（见 build_windows_app.stage_into_the_package）。
    ("build_windows_app", {"PAYLOAD_VERSION"}),
])
def test_the_harness_in_the_desktop_package_is_what_git_tracks(
        checkout: Path, tmp_path: Path, name: str, built: set[str]) -> None:
    packager = _load(name)
    _point_at(packager, checkout, tmp_path)
    interface = tmp_path / "ui"
    interface.mkdir()
    (interface / "index.html").write_text("<html/>", encoding="utf-8")

    packager.stage_into_the_package(interface)

    shipped = _files(checkout / "platform/backend/app/harness") - built
    # harness = 三个包 + worker 入口 + 它 import 的根模块（`chat`；没人 import 的 `unimported`
    # 不带）—— 各自都是 git 跟踪的那份。
    expected = {name for name in set(TRACKED) - DELETED
                if name.split("/")[0] in packager.HARNESS_PACKAGES
                and "tests" not in PurePosixPath(name).parts} | {"platform_runtime.py", "chat.py"}
    assert shipped == expected, (
        f"多出来的：{sorted(shipped - expected)}；少了的：{sorted(expected - shipped)}")


def test_the_backend_app_in_the_update_is_what_git_tracks_plus_the_pro_things(
        checkout: Path, tmp_path: Path) -> None:
    """载荷第二个归档里的 `app/`：跑在**装配好的**树上 —— 界面、harness、专业版的 ssh 库与
    服务器包都已经放进 `BACKEND/app` 了。后两样是构建产物、不在 git 里，但自更新上来的安装
    只能从这里拿到它们，所以它们要走；界面和 harness 走第一个归档，不在这里。"""
    packager = _load("build_mac_app")
    _point_at(packager, checkout, tmp_path)
    interface = tmp_path / "ui"
    interface.mkdir()
    (interface / "index.html").write_text("<html/>", encoding="utf-8")
    packager.stage_into_the_package(interface)
    app = checkout / "platform/backend/app"
    vendor, bundle = packager.payload_release.VENDOR_DIR, packager.payload_release.SERVER_BUNDLE_DIR
    pro = {f"{vendor}/asyncssh/__init__.py", f"{bundle}/sciencemate-server-7.7.7.tar.gz"}
    for name in [*pro, f"{vendor}/asyncssh/__pycache__/__init__.cpython-314.pyc"]:
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        (app / name).write_bytes(b"built")

    shipped = _files(packager.the_backend_app_for_the_payload())

    expected = _shipped_by_the_rule(TRACKED, "platform/backend/app") | pro
    assert shipped == expected, (
        f"多出来的：{sorted(shipped - expected)}；少了的：{sorted(expected - shipped)}")


def test_a_root_module_the_worker_imports_but_git_does_not_track_is_refused(
        checkout: Path, tmp_path: Path) -> None:
    """worker 要 import 的根模块没进过提交：带上它 = 发出去一个没提交的文件；不带 = worker
    在用户发第一条消息时 `No module named`。两样都不对，所以当场拒绝。"""
    packager = _load("build_mac_app")
    _point_at(packager, checkout, tmp_path)
    (checkout / "core" / "agent_loop.py").write_text("import stray\n", encoding="utf-8")
    interface = tmp_path / "ui"
    interface.mkdir()
    with pytest.raises(SystemExit, match=r"\['stray\.py'\].*git 没跟踪"):
        packager.stage_into_the_package(interface)


def test_the_file_list_does_not_depend_on_the_locale(checkout: Path) -> None:
    """名单按 UTF-8 解码，不按本机 locale。

    Windows 构建机的 locale 是 cp1252（2026-09-24 实测）：按 locale 解码时，`nodes/literature/`
    下的中文名被解成 `2025�\\xad科院…`，名单里多出三个磁盘上不存在的路径，拷的那一刻才炸。
    这里的 CI 是 UTF-8 locale，照原样跑看不出来 —— 所以在子进程里打开
    `-X warn_default_encoding` 并把 `EncodingWarning` 升成错误：**任何**按 locale 解码的地方都
    当场失败，不管这台机器的 locale 碰巧是什么。
    """
    probe = (
        "import importlib.util, json, sys\n"
        "from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('git_tracked', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
        "repo = Path(sys.argv[2])\n"
        "print(json.dumps({'nodes': module.the_tracked_files(repo, ['nodes']),\n"
        "                  'nothing': module.the_tracked_files(repo, [])}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-X", "warn_default_encoding", "-W", "error::EncodingWarning",
         "-c", probe, str(PACKAGE / "git_tracked.py"), str(checkout)],
        capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode("utf-8", errors="replace")[-2000:]
    listed = json.loads(done.stdout)
    assert listed["nodes"] == sorted({name for name in set(TRACKED) - DELETED
                                      if name.startswith("nodes/")
                                      and "tests" not in PurePosixPath(name).parts})
    # 不带路径的 `git ls-files --` 是整个仓库 —— 空名单必须还是空名单。
    assert listed["nothing"] == []
