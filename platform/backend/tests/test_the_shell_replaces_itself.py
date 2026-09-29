"""壳启动时自替换（#953 ④）—— 自更新带来的新壳，由壳自己在起后端之前换上。

## 为什么是壳自己换、在启动那一刻换

运行中的 exe / 二进制在 Windows 和 macOS 上都**不能覆盖但能改名**。后端跑在壳之后、
又被壳的 Job 管着，它换不了壳；安装器是另一条路（要用户重下 375MB）。唯一能换壳的是
壳自己，唯一能换的时机是它刚起来、还没起后端、没开窗口、没锁任何东西的那一刻。

## 两道闸，缺一不可（判据钉在两道都在）

- **版本**：载荷版本必须比「我随哪一版装的」新。只比哈希，会把「重装了新包、数据根里
  还留着旧载荷」变成把新壳换成旧的 —— #958 那个坑的壳版。
- **哈希**：不是同一份字节才换。这是循环的终止条件：换完起来的新壳看到载荷里的壳就是自己，停。

真正的行为判据在真机：装 N 版 → 暂存带不同壳的 N+1 载荷 → 重启 → `shell.json` 报的哈希变成
载荷里那份、窗口照常出现、没有重装。这里钉的是「两个壳的代码里这些东西都在、顺序对、字段翻了」。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import self_update as su

REPO = Path(__file__).resolve().parents[3]


def _code(path: Path, markers: tuple[str, ...]) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith(markers))


WIN = _code(REPO / "platform/desktop/windows/Shell.cs", ("//", "///"))
MAC = _code(REPO / "platform/desktop/mac/Shell.swift", ("//", "///"))


# ───────────────────────────────────────────── 打包器：壳得知道自己随哪一版装的

def _the_windows_packager():
    import importlib.util
    spec = importlib.util.spec_from_file_location("afs_package_build_windows_app",
                                                  REPO / "scripts/package/build_windows_app.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_the_windows_package_tells_the_shell_which_version_it_shipped_with(tmp_path: Path) -> None:
    """版本闸的那一半事实来自 backend.json —— 打包器不写，壳就永远「不知道」、永远不换。"""
    mod = _the_windows_packager()
    app = tmp_path / "ScienceMate"; python = app / "Resources" / "python" / "python.exe"
    python.parent.mkdir(parents=True); python.write_bytes(b"MZ")
    mod.write_the_backend_json(app, python)
    doc = json.loads((app / "backend.json").read_text(encoding="utf-8"))
    assert doc["version"] == mod.payload_release.the_version(), "backend.json 没写版本，或写的不是 pyproject 那份"
    assert "LoadConfig" in WIN and 'key == "version"' in WIN, "壳没解析 backend.json 的 version"


def test_the_installer_check_tells_a_self_swap_apart_from_a_double_click_exit() -> None:
    """机器上真发生过：数据根里留着更新的测试载荷，装完自检起的壳换成载荷壳后 exit 0，
    自检把它报成「双击即退」—— 让人去查一个不存在的产品 bug。两种 exit 0 要说成两件事。"""
    mod = _the_windows_packager()
    swapped = mod.explain_the_shell_exit(0, "── pid=1 ──\nSHELL-SWAP done: 0.4.5 → 0.4.6，拉起新的自己\n")
    verdict = "这就是同事双击后看到的"
    assert verdict not in swapped and "载荷" in swapped.splitlines()[0] and "payload" in swapped, swapped
    plain = mod.explain_the_shell_exit(0, "── pid=1 ──\n")
    assert verdict in plain and "载荷" not in plain, plain
    assert "SHELL-SWAP done" in WIN, "自检认的那句话壳得真的说"


# ───────────────────────────────────────────── Windows 壳

def test_the_windows_shell_swaps_before_the_backend_and_again_after_a_restart() -> None:
    main = WIN[WIN.index("private static int Main("):]
    first = main.index("ReplaceMyselfIfANewerShellIsStaged(cfg, args)")
    assert first < main.index("CreateKillOnCloseJob()"), "自替换排在起后端之后 —— 那时 exe 已被 Job/窗口锁住"
    assert first > main.index("LoadConfig("), "自替换排在读配置之前 —— 那时还不知道自己哪一版"
    ready = main.index('"READY " + url')
    assert main.index("ReplaceMyselfIfANewerShellIsStaged(cfg, args)", ready) > ready, (
        "重启路径 READY 之后没再查一次 —— 后端刚 apply 完暂存的更新，新壳正好在这一刻到位")
    assert "CleanupOldShellFiles();" in main, "没收 .old"


def test_the_windows_shell_restarts_the_backend_instead_of_quitting_when_it_exits_to_update() -> None:
    """后端为装更新退 3 → 窗口关掉 → 这不能算「人要退出」。

    真机上就是这么坏的：`/update/restart` 后壳打了 STOPPING 而不是 RESTARTING，应用消失，
    更新（和新壳）要等下次手动启动才生效。「后端退了所以关窗」与「人关了窗」必须分开记。
    """
    fn = WIN[WIN.index("private static void ShowTheInterface"):WIN.index("private static void OpenInBrowser")]
    assert "backendGone = true" in fn and "!StillRunning(backend)" in fn, "后端退出没有单独记下来"
    assert "if (!backendGone) stop.Set()" in fn, "关窗一律置 stop —— 后端退 3 会被当成人要退出"
    main = WIN[WIN.index("private static int Main("):]
    assert main.index("if (stop.IsSet)") < main.index("exitCode == RESTART_EXIT_CODE"), (
        "重启判断得在「人要退出」之后，否则人关窗时也会重启后端")


def test_the_windows_shell_has_both_gates_and_a_rollback() -> None:
    fn = WIN[WIN.index("private static bool ReplaceMyselfIfANewerShellIsStaged"):WIN.index("[STAThread]")]
    assert "IsNewer(version, cfg.Version)" in fn, "没有版本闸 —— 重装的新壳会被旧载荷换掉"
    assert "Sha256Of(candidate) == Sha256Of(self)" in fn, "没有哈希闸 —— 没有循环终止条件"
    assert 'File.Move(mine, old)' in fn and 'File.Copy(incoming, mine)' in fn, "不是「改名再拷」"
    assert "rolled back" in fn and "File.Move(old, mine)" in fn, "换到一半失败没有回滚"
    assert "ArgumentList" not in WIN, ".NET Framework 4.x 没有 ArgumentList，in-box csc 编不过"
    assert '\\"self_replace\\": true' in WIN, "自报家门还说自己不会换"


# ───────────────────────────────────────────── Mac 壳

def test_the_mac_shell_swaps_before_anything_else() -> None:
    launch = MAC[MAC.index("func applicationDidFinishLaunching"):]
    assert launch.index("replaceMyselfIfANewerShellIsStaged()") < launch.index("WKWebViewConfiguration()"), (
        "自替换排在建窗口之后")
    assert launch.index("cleanupOldShellFiles()") < launch.index("replaceMyselfIfANewerShellIsStaged()")


def test_the_mac_shell_checks_again_after_the_backend_restarts() -> None:
    """「更新 → 重启」那条路窗口早就开着：后端退 3 → startBackend → 就绪 → 此刻数据根里
    才有新壳。只在 didFinishLaunching 查一遍，新壳要等下次手动启动。"""
    fn = MAC[MAC.index("private func startBackend"):MAC.index("func applicationShouldTerminateAfterLastWindowClosed")]
    assert fn.index("replaceMyselfIfANewerShellIsStaged()") < fn.index("webView.load("), (
        "后端就绪后没再查一次新壳（或查在指 webview 之后）")
    assert fn.index("self?.backend = backend") < fn.index("replaceMyselfIfANewerShellIsStaged()"), (
        "先记下后端再换壳 —— 不然 applicationWillTerminate 收不掉这个后端")


def test_the_mac_shell_has_both_gates_and_a_rollback() -> None:
    fn = MAC[MAC.index("private func replaceMyselfIfANewerShellIsStaged"):MAC.index("private func declareMyself")]
    assert "isNewer(version, than: mine)" in fn, "没有版本闸"
    assert "CFBundleShortVersionString" in fn, "版本不是从 Info.plist 读的"
    assert "theirs == ours" in fn, "没有哈希闸"
    assert "moveItem(at: exe, to: old)" in fn and "copyItem(at: candidate, to: exe)" in fn
    assert "posixPermissions" in fn, "载荷里的文件是 0644，没补可执行位起不来"
    assert "moveItem(at: old, to: exe)" in fn, "换到一半失败没有回滚"
    assert '"self_replace": true' in MAC


def test_the_data_root_is_computed_once_in_the_mac_shell() -> None:
    """三处要用（日志 / 自报 / 自替换）；各算一遍就是三个真相源。"""
    assert MAC.count("HARNESS_FRAMEWORK_HOME") == 1, "数据根的规则在 Mac 壳里出现了不止一次"


# ───────────────────────────────────────────── 两个壳与后端说的是同一件事

def test_a_shell_that_replaces_itself_never_asks_for_a_reinstall() -> None:
    """壳说 self_replace=true → 后端那一侧对壳的改动就不再说「需要重装」。"""
    declared = {"platform": "windows", "path": "x", "sha256": "a" * 64, "size": 1, "self_replace": True}
    verdict = su.describe_the_shell_update(declared, {"extras": {"shell": {"windows": "b" * 64}}})
    assert verdict == {"changed": True, "needs_reinstall": False}


def test_both_shells_read_the_pointer_the_backend_writes() -> None:
    """壳读的是后端写的那个文件、那个字段：`payload/current.json` 的 `version`。"""
    assert "current.json" in WIN and "current.json" in MAC
    assert su.pointer_path(Path("/r")).name == "current.json"
    assert '"version"' in WIN and 'doc["version"]' in MAC
