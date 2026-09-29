"""装进包里的 harness 必须带齐 worker 真正会 import 的东西（Mac、Windows、组织服务器三个包同一条）。

## 这条测试为什么存在

2026-09-06 第一次打出 `.app`：界面正常、项目建得出、会话开得了 —— 发第一条消息
时 worker 才倒在 `No module named 'chat'` 上。原因是打包清单是**手写**的
（`core/ shared/ nodes/ platform_runtime.py`），而仓库根上还有个 `chat.py`，被
`platform_runtime` 在函数体里 `import chat` 用着。

手写清单的失败形状永远是这个：漏掉的那一项不在任何一条构建期判据的视野里，
装出来的包一路正常，直到用户做那件真正的事。所以清单改成从 import 推出来，
而这条测试守着"推"这件事本身。

推导住在 `scripts/package/git_tracked.py`，三个打包器调同一个。它从前在两个桌面打包器里各
一份，组织服务器包没有 —— 于是从包装起来的组织服务器上，每个会话的第一条消息都倒在同一句
`No module named 'chat'` 上（2026-09-27 升级演练才看见）。服务器包那一半的判据在
`platform/backend/tests/test_the_org_server_bundle.py`。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "package" / "build_mac_app.py"
PACKAGERS = ("build_mac_app.py", "build_windows_app.py", "build_server_bundle.py")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def packaging():
    return _load(SCRIPT, "build_mac_app")


@pytest.fixture(scope="module")
def tracked():
    return _load(REPO / "scripts" / "package" / "git_tracked.py", "git_tracked_under_test")


def test_a_module_imported_inside_a_function_still_gets_packed(tracked, tmp_path) -> None:
    """函数体里的 import 也算数。

    `platform_runtime` 里那 5 处 `import chat` 全在函数体内 —— 只看文件顶部的
    import 会漏掉它们，而漏掉的代价要等到用户发第一条消息才现形。
    """
    staged = tmp_path / "harness"
    (staged / "core").mkdir(parents=True)
    (staged / "core" / "agent_loop.py").write_text(
        "def go():\n    import chat\n    return chat\n", encoding="utf-8")
    found = tracked.top_level_modules_reachable_from(staged, REPO)
    assert "chat" in found


def test_it_follows_the_chain(tracked, tmp_path) -> None:
    """带进来的模块自己 import 的，也要带。

    一层就停的话，清单看起来"推出来了"，其实只推了一步 —— 而这种半截的推导
    跟手写清单犯的是同一个错。
    """
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "first.py").write_text("import second\n", encoding="utf-8")
    (fake_repo / "second.py").write_text("x = 1\n", encoding="utf-8")
    staged = tmp_path / "harness"
    staged.mkdir()
    (staged / "entry.py").write_text("import first\n", encoding="utf-8")
    assert tracked.top_level_modules_reachable_from(staged, fake_repo) == {"first", "second"}


def test_third_party_imports_are_not_mistaken_for_our_modules(tracked, tmp_path) -> None:
    """只带仓库根上真有的那些 .py。

    `import numpy` 是依赖，不是我们的模块 —— 把它当模块拷贝会在打包时崩掉，
    而崩在这里比装出一个坏包好，但更好的是根本不认它。
    """
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "ours.py").write_text("x = 1\n", encoding="utf-8")
    staged = tmp_path / "harness"
    staged.mkdir()
    (staged / "entry.py").write_text("import numpy\nimport ours\n", encoding="utf-8")
    assert tracked.top_level_modules_reachable_from(staged, fake_repo) == {"ours"}


def test_the_real_worker_entry_needs_chat(packaging, tracked) -> None:
    """拿真的 harness 跑一遍推导：`chat` 必须在里面。

    上面三条验的是"推导这件事对不对"，这条验的是"对这个仓库推出来的结果对不
    对" —— 两者都要，因为推导可以完全正确却因为入口选错而漏掉东西。
    """
    entry = REPO / packaging.HARNESS_ENTRY
    if not entry.is_file():  # pragma: no cover - 仓库结构变了
        pytest.skip(f"{packaging.HARNESS_ENTRY} 不在仓库根上了")
    found = tracked.top_level_modules_reachable_from(REPO / "core", REPO)
    assert "chat" in found or "chat" in tracked.top_level_modules_reachable_from(entry.parent
                                                                                / "core", REPO)


def test_conftest_never_ships(tracked, tmp_path) -> None:
    """`conftest.py` 是 pytest 的东西，不进安装包。

    它在仓库根上、名字也对得上，但装机的人不跑测试；带上它等于让运行时多一个
    只在测试里成立的钩子。
    """
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "conftest.py").write_text("x = 1\n", encoding="utf-8")
    staged = tmp_path / "harness"
    staged.mkdir()
    (staged / "entry.py").write_text("import conftest\n", encoding="utf-8")
    assert tracked.top_level_modules_reachable_from(staged, fake_repo) == set()


def test_the_dmg_is_assembled_with_a_tool_that_keeps_hardlinks(packaging) -> None:
    """装 dmg 用的工具必须保留硬链接。

    git 的 libexec 里 143 个命令是同一个 inode。`cp -R` **不保留硬链接**，会把
    它们展开成 143 份实体副本 —— 实测 dmg 因此从 213 MB 涨到 499 MB，比它装的
    那个 .app（409 MB）还大。macOS 上保留硬链接的是 `ditto`。

    判据落在**用的是哪个工具**上，不落在"产出多大"：后者随内容变，而且要真打一次
    包才知道；前者是一句话的事，且它正是会被下一个人改错的那一句。
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assembly = source[source.index("def make_the_dmg"):]
    assembly = assembly[:assembly.index("\ndef ")] if "\ndef " in assembly else assembly
    code = "\n".join(line.split("#", 1)[0] for line in assembly.splitlines())
    assert '"ditto"' in code, "装 dmg 没用 ditto —— 硬链接会被展开"
    assert '"cp", "-R"' not in code, "cp -R 不保留硬链接"


@pytest.mark.parametrize("packager", PACKAGERS)
def test_every_packager_carries_them_through_the_one_derivation(packager) -> None:
    if not (REPO / "scripts" / "package" / packager).exists():
        pytest.skip(f"{packager} 是专业版的打包器：这棵树里没有它")
    """三个打包器都**调**同一个推导（判据落在调用上，不落在名字出现过）。

    少了哪一个，那个包就装得起来、开得了会话，然后在第一条消息上倒下 —— 组织服务器包
    就这么倒了三个版本。自己再实现一份也不行：几份抄件就有几个各自演化的答案。
    """
    import ast

    tree = ast.parse((REPO / "scripts" / "package" / packager).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "carry_the_top_level_modules"
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "git_tracked"]
    assert calls, f"{packager} 不带 worker 会 import 的仓库根模块 —— 装出来发第一条消息就倒"
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "top_level_modules_reachable_from" not in defined, f"{packager} 又自己实现了一份推导"
