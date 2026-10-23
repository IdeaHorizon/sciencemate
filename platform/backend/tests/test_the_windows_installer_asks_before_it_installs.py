"""Windows 安装器得先问人三件事，而且问的时候不能把自己卡死。

## 为什么有这一份

2026-09-21 wangd：「默认装到 AppData\\Local\\Programs 里面，这是一个隐藏文件夹……
要有桌面快捷方式，或者开始菜单栏快捷方式。另外，现在安装会出现一个 Terminal，就在那里
安装，什么选项都没有。能不能做一个可视化界面，让用户点点点那种的？」

向导本身跑在 Windows 上，CI 跑不了 —— 但**把向导接进安装流程**这件事是机械可判的，
而且真机上已经撞出两个只有真跑才看得见的缺陷，两个都落在这里：

1. `InstallerStub.Main` 没有 `[STAThread]`。`浏览…` 背后是 `SHBrowseForFolder`
   （OLE/shell COM），线程不是 STA 时它**建出对话框却永远不显示**，安装器整个卡住。
   真机窗口列表里躺着一个 `vis=False` 的 "Browse For Folder"，人看到的只是点了没反应。
2. 版本号靠「exe 旁边有个 VERSION 文件」读 —— 用户手里只有一个 .exe，旁边什么都没有，
   于是标题永远是「ScienceMate 」带个尾空格。改成编译那一刻烧进去。

所以判据是三条：**这个目录里每个 `Main` 都得是 STA**（扫，不是列名单）；
**向导的答案真的被用掉**（三问各自接到安装的哪一步）；
**版本号那份生成源码真的被编进去**（生成了不编＝还是空的）。

## 2026-09-21 下午：向导换了一版，判据跟着搬

同一天有两份并行的实现（两个会话，同一句话下达的）。留下来的是三页那一版
（多出进度页 —— 解压 800MB 期间窗口不能一动不动 —— 和卸载器），快捷方式从
`WScript.Shell` 换成了 `IShellLinkW`：WSH 走 ANSI，**目标路径含中文时直接失败**
（`Value does not fall within the expected range.`），而默认安装位置是
`C:\\Users\\<用户名>\\AppData\\Local\\Programs\\ScienceMate` —— 中文 Windows 上
用户名常常就是中文，那台机器上一个快捷方式都建不出来。先前「真机上验过能出 .lnk」
是真的，只是那台机器的用户名是 `fcbay`，纯 ASCII。

上面三条判据一条没丢，只是问的对象换成了新实现；「每个 Main 都得是 STA」那条是
扫出来的，新加的卸载器自动被它守着。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WINDOWS_SHELL = REPO / "platform" / "desktop" / "windows"
STUB = WINDOWS_SHELL / "InstallerStub.cs"
WIZARD = WINDOWS_SHELL / "InstallWizard.cs"
FOOTPRINT = WINDOWS_SHELL / "InstallFootprint.cs"
ICON_SOURCE = WINDOWS_SHELL / "MakeIcon.cs"
BUILD = REPO / "scripts" / "package" / "build_windows_app.py"

#: `private static int Main(...)` / `static void Main(...)`，前面允许有特性与注释。
A_MAIN = re.compile(r"^[ \t]*(?:(?:private|public|internal|static|partial)[ \t]+)*"
                    r"static[ \t]+(?:int|void)[ \t]+Main[ \t]*\(", re.MULTILINE)


def _the_compile_units() -> dict[str, list[Path]]:
    """构建脚本里每一条 `csc` 命令就是一个编译单元 —— 谁和谁编在一起，由它说了算。

    判「要不要 STA」不能按文件名，得按**这一堆源码合起来干什么**：`InstallerStub.cs`
    自己一行 WinForms 都没有，窗口在 `InstallWizard.cs` 里；反过来画图标那个只用
    `System.Drawing`，逼它 STA 是无缘无故。
    """
    tree = ast.parse(BUILD.read_text(encoding="utf-8"))
    units: dict[str, list[Path]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "run" and call.args):
                continue
            first = call.args[0]
            if not (isinstance(first, ast.List) and first.elts
                    and isinstance(first.elts[0], ast.Name) and first.elts[0].id == "CSC"):
                continue
            sources = [WINDOWS_SHELL / n.value
                       for n in ast.walk(call)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)
                       and n.value.endswith(".cs")]
            if sources:
                units[node.name] = sources
    return units


#: 会画窗口或开 shell 对话框的东西。碰上任何一个，这个单元的入口就必须是 STA。
DRAWS_A_WINDOW = ("System.Windows.Forms", "GetTypeFromProgID", "ShowDialog", "MessageBox")


def test_there_is_something_to_scan() -> None:
    """扫不到编译单元就等于没有这道闸 —— 先证明扫得到。"""
    units = _the_compile_units()
    assert units, "构建脚本里一条 csc 命令都没扫到，这道闸是空的"
    assert "make_the_installer" in units, "安装器的编译单元没被扫到"
    for sources in units.values():
        for source in sources:
            assert source.is_file(), f"编译单元里点了一个不存在的文件：{source}"


@pytest.mark.parametrize("unit", sorted(_the_compile_units()))
def test_an_entry_point_that_draws_a_window_is_single_threaded_apartment(unit: str) -> None:
    """WinForms 与 shell 对话框只在 STA 上活着；MTA 上它们不报错，只是永远不出现。"""
    sources = _the_compile_units()[unit]
    texts = {source: source.read_text(encoding="utf-8") for source in sources}
    if not any(any(mark in text for mark in DRAWS_A_WINDOW) for text in texts.values()):
        pytest.skip(f"{unit} 这一单元不画窗口")
    entries = [(source, text) for source, text in texts.items() if A_MAIN.search(text)]
    assert entries, f"{unit} 这一单元没有入口"
    for source, text in entries:
        for match in A_MAIN.finditer(text):
            # 只看紧挨着的那几行：`[STAThread]` 必须贴在这个 Main 上，不是文件里别处有就算。
            nearby = [line.strip() for line in text[:match.start()].splitlines()[-8:]]
            assert "[STAThread]" in nearby, (
                f"{source.name} 的 Main 没带 [STAThread]。WinForms 入口在 MTA 上，"
                f"`浏览…` 那类 shell 对话框会建出来却不显示，整个进程卡住。"
            )


def test_the_wizard_answers_are_the_ones_the_install_uses() -> None:
    """问了三件事就得用三件事：装到哪、桌面图标、开始菜单，各自接到具体一步。

    2026-09-21 向导从「一个对话框」换成了三页（多出进度页与完成页，解压 800MB 期间
    窗口不能是一动不动的），三个答案改从 `InstallOptions` 走。判据问的还是同一句话：
    **问到的和用到的是不是同一个东西**。
    """
    stub = STUB.read_text(encoding="utf-8")
    wizard = WIZARD.read_text(encoding="utf-8")
    assert "InstallWizard.Run(options.ForAPerson())" in stub, "安装器从来没问过 —— 向导没接进来"
    for asked, answer, complaint in [
        (r"_options\.InstallDir\s*=", "InstallDir", "选的目录没被拿去装"),
        (r"_options\.DesktopShortcut\s*=\s*_desktop\.Checked", "DesktopShortcut", "桌面那个勾没被读走"),
        (r"_options\.StartMenuShortcut\s*=\s*_startMenu\.Checked", "StartMenuShortcut", "开始菜单那个勾没被读走"),
    ]:
        assert re.search(asked, wizard), complaint
        assert f"options.{answer}" in stub or f"options.{answer}" in FOOTPRINT.read_text(encoding="utf-8"), \
            f"向导收了 {answer}，安装那一步没人读它"
    assert re.search(r"if\s*\(options\.DesktopShortcut\)", stub), "勾了桌面图标却没往桌面放"
    assert re.search(r"if\s*\(options\.StartMenuShortcut\)", stub), "勾了开始菜单却没往开始菜单放"


def test_a_missing_shortcut_is_not_a_failed_install() -> None:
    """快捷方式造不出来，应用照样能用 —— 不许因此改退出码、走 Fail 或抛出去。

    文件已经全部就位、应用已经能起来了，因为一个图标没建成就把整次安装报成失败，
    是拿最轻的事去否定最重的事。失败收进 `Warnings`，向导在完成页照原样说出来。
    """
    stub = STUB.read_text(encoding="utf-8")
    start = stub.index("private static void LeaveTheFootprint")
    body = stub[start:]
    end = body.find("\n    private static ", 1)
    body = body[:end] if end > 0 else body
    assert "return Fail(" not in body, "快捷方式失败被当成安装失败了"
    assert "throw" not in body, "快捷方式那一步把异常抛出去了 —— 它会一路冒到「安装失败」"
    assert body.count("Warnings.Add(") >= 3, "失败没被收进 Warnings，人看不到哪一件没做成"
    assert re.search(r"private static void LeaveTheFootprint", stub), "这一步不该有返回码"


#: 只有人才该看见窗口。脚本走 `--no-launch` / `--silent`，被一个模态框挂住就是打包卡死。
A_PERSON = "if (!options.Silent)"


def test_only_a_person_is_shown_a_window() -> None:
    """向导**紧挨着**那道判断起 —— 文件里别处有一句同样的话不算数。

    这条判据第一版是「源码里出现过这句 if」，而当时另一处弹窗正好也是同一句 —— 于是
    把向导那一处的守卫整个删掉，测试照样绿。判据必须盯住**这一处**。
    """
    stub = STUB.read_text(encoding="utf-8")
    call = "InstallWizard.Run(options.ForAPerson())"
    assert call in stub, f"{call} 根本没被调用"
    for index in (i for i in range(len(stub)) if stub.startswith(call, i)):
        before = stub[:index].splitlines()[-2:]
        assert any(A_PERSON in line for line in before), (
            f"{call} 前面两行里没有 `{A_PERSON}` —— 非交互跑到这里会被窗口挂死"
        )


def test_an_error_box_only_pops_for_a_person() -> None:
    """出错也一样：脚本要的是退出码和 stderr，一个等人点「确定」的框会把它挂死。

    所以 `Fail()` 里那个 MessageBox 必须在 `interactive` 守卫下，而静默那条路
    （`--no-launch` / `--silent`）传进去的就是 false。
    """
    stub = STUB.read_text(encoding="utf-8")
    body = stub[stub.index("private static int Fail("):]
    body = body[:body.index("\n    private static", 1)]
    box = body.index("MessageBoxW(")
    assert "if (interactive)" in body[:box], "错误框不在 interactive 守卫下 —— 脚本会被它挂住"
    assert re.search(r"return Fail\(refused\.Message, refused\.Code, false\)", stub), \
        "静默那条路把 interactive 传成了 true"


def _the_csc_call_in(function: str) -> ast.Call:
    tree = ast.parse(BUILD.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "run"):
                    return call
    raise AssertionError(f"{function} 里没有编译那一步")


def _the_csc_call_for_the_installer() -> ast.Call:
    return _the_csc_call_in("make_the_installer")


def _every_string_in(call: ast.Call) -> set[str]:
    """把这条命令里所有字面量拿出来 —— f-string 的片段也算（`/win32icon:{...}`）。"""
    found = set()
    for node in ast.walk(call):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def test_the_installer_is_compiled_with_the_wizard_and_its_version() -> None:
    """三样东西必须在同一条 csc 命令里：桩、向导、生成的版本号。少一个都不报错，只是没有。"""
    call = _the_csc_call_for_the_installer()
    literals = {n.value for n in ast.walk(call) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
    assert "InstallerStub.cs" in literals
    assert "InstallWizard.cs" in literals, "向导没被编进安装器 —— 窗口根本不会出现"
    assert "InstallFootprint.cs" in literals, "快捷方式/卸载登记那一份没编进去"
    assert "version_source" in names, "生成的版本号源码没编进去，标题里就是空的"
    assert "/target:winexe" in literals, "还会先弹一个黑窗口"
    assert "/r:System.Windows.Forms.dll" in literals


def test_the_generated_version_is_what_the_stub_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """生成的类名/字段名要和 C# 里写的那一处对得上，否则编译才发现。"""
    sys.path.insert(0, str(REPO / "scripts" / "package"))
    import build_windows_app as build

    monkeypatch.setattr(build, "DIST", tmp_path)
    source = build.write_the_installer_version("9.9.9")
    text = source.read_text(encoding="utf-8")
    assert '"9.9.9"' in text
    reference = f"{build.INSTALLER_VERSION_CLASS}.{build.INSTALLER_VERSION_FIELD}"
    # 读它的是向导第一页（「版本 x.y.z」那一行）——生成的名字和用的名字必须是同一个，
    # 对不上要到编译那一刻才发现。
    used_in = STUB.read_text(encoding="utf-8") + WIZARD.read_text(encoding="utf-8")
    assert reference in used_in, f"安装器那几份源码里没有引用 {reference}"
    size_reference = f"{build.INSTALLER_VERSION_CLASS}.{build.INSTALLER_SIZE_FIELD}"
    assert size_reference in used_in, f"没人读 {size_reference} —— 向导说不出「需要多大地方」"
    assert f"long {build.INSTALLER_SIZE_FIELD}" in text
    assert f"class {build.INSTALLER_VERSION_CLASS}" in text
    assert f"string {build.INSTALLER_VERSION_FIELD}" in text


@pytest.mark.parametrize("unknown", ["", "(unknown)"])
def test_a_package_that_does_not_know_its_version_is_not_built(
    unknown: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """版本号读不出来就别出包 —— 向导上一个空标题比报错更难查。"""
    sys.path.insert(0, str(REPO / "scripts" / "package"))
    import build_windows_app as build

    monkeypatch.setattr(build, "DIST", tmp_path)
    with pytest.raises(SystemExit):
        build.write_the_installer_version(unknown)


def test_the_wizard_asks_exactly_the_three_things() -> None:
    """第一页只问这三样。多问一句，装个软件就又变成一张表。

    判据落在**页面上能改的控件**上，不落在文案：文案会改，「这一页只让人做三个决定」
    不会。三个 = 一个路径输入框（带「浏览…」）+ 两个复选框。
    """
    wizard = WIZARD.read_text(encoding="utf-8")
    assert "安装到：" in wizard and "在桌面创建快捷方式" in wizard and "添加到开始菜单" in wizard
    # 只看第一页那个函数 —— 后面两页也有控件（进度条、完成页那个只读的说明框），
    # 它们不是在问人任何事。
    page = wizard[wizard.index("private void BuildChoosePage()"):]
    page = page[:page.index("\n    private void Build", 1)]
    controls = set(re.findall(r"_pageChoose\.Controls\.Add\((_\w+)\)", page))
    # `_useDefault` 不是第四个问题：它平时**不显示**（`Visible = false`），只在选中的
    # 位置写不进去时冒出来，点一下就换回不用权限的那个。给出路不等于多问一句 ——
    # 让人对着一句"装不进去"自己想办法，才是把负担推给他。
    assert controls == {"_dir", "_desktop", "_startMenu", "_space", "_complaint",
                        "_useDefault", "_install", "_cancel"}, (
        f"第一页上的控件变了：{sorted(controls)}。装个软件不该变成填一张表 —— "
        f"能改的只该有三样（目录、桌面图标、开始菜单），其余是说明与按钮。")


@pytest.mark.parametrize("function", ["build_the_shell", "make_the_installer"])
def test_what_lands_on_the_desktop_has_an_icon(function: str) -> None:
    """快捷方式的全部意义是一眼认出来 —— 顶着 .NET 默认图标就等于没做。

    mac 那边早就现画一个（`MakeIcon.swift`，理由写着「Dock 里是一张白纸，看起来像
    装坏了」）。Windows 这边一直没有，直到要往桌面放图标才成为问题。
    """
    literals = _every_string_in(_the_csc_call_in(function))
    assert any(text.startswith("/win32icon:") for text in literals), \
        f"{function} 编出来的 exe 没有图标"


def test_the_icon_is_drawn_not_committed() -> None:
    """不往仓库里塞二进制：图标是构建时画的，源码是一份 .cs。"""
    assert ICON_SOURCE.is_file(), "没有画图标的程序"
    assert not list(WINDOWS_SHELL.glob("*.ico")), "仓库里躺着一个 .ico —— 该现画"
    text = ICON_SOURCE.read_text(encoding="utf-8")
    sizes = re.search(r"Sizes\s*=\s*\{([^}]*)\}", text)
    assert sizes, "画图标的程序没说它画哪几档尺寸"
    drawn = {int(n) for n in re.findall(r"\d+", sizes.group(1))}
    # 16/32 是 Explorer 列表与任务栏取的档，256 是大图标视图取的档。少一档就会被拉伸。
    assert {16, 32, 256} <= drawn, f"少了关键尺寸：{drawn}"


# ── 装不进去这件事，要在人**选完路径那一刻**说 ──────────────────────────────
#
# 2026-09-23 wangd 真机：安装位置改成 `C:\Program Files\SM\ScienceMate`，向导一句话
# 不说，点了安装、等过一轮，才弹出一句 .NET 原文：
#
#     安装失败: 对路径"\\?\C:\Program Files\SM\ScienceMate.new-25e68b40a66d…"的访问被拒绝。
#
# 那句话里对用户有意义的东西是零：`\\?\` 是我们自己加的长路径前缀，`.new-<事务id>`
# 是我们内部的 staging 名字，而真正的事实 ——**那个位置要管理员权限**—— 一个字没说，
# 更没说下一步该干什么。
#
# 同一个形状此前被指出过一次（建立组织那张表单：「密码不合格你当场说啊」）。答案都在
# 手边，却被推到一次长操作的末尾。
#
# 判据两条：这一问**问了**（而且是靠真去写一次来答，不是看路径长什么样），以及它
# **在打字那一刻就问**。向导本身跑在 Windows 上 CI 跑不了，但这两件事都是机械可判的。

#: 名字别撞上面那两个（它们是 Path，这两个是正文）。撞了的话，前面每一条
#: `STUB.read_text()` 都会在一个字符串上炸 —— 2026-09-23 写这几条时真撞了一次。
WIZARD_SOURCE = WIZARD.read_text(encoding="utf-8")
STUB_SOURCE = STUB.read_text(encoding="utf-8")


def _body_of(source: str, signature: str) -> str:
    """一个方法**自己**的正文 —— 按大括号配对切，不按"下一个方法叫什么"切。

    按下一个方法的名字切会把中间的东西一起圈进来：`WhyNotInstallable` 和
    `IsAnInstallation` 之间正好隔着 `WhyThisFolderRefusesUs` 的**定义**，于是
    "调用方里有没有提到它"这一问，被它自己的定义无条件答成了"有"。
    2026-09-23 变异实测：把那次调用整个删掉，判据纹丝不动。
    """
    start = source.index(signature)
    opened = source.index("{", start)
    depth, i = 0, opened
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[opened:i + 1]
        i += 1
    raise AssertionError(f"{signature} 的大括号没配上")


def test_the_pre_install_check_asks_whether_we_can_write_there() -> None:
    """`WhyNotInstallable` 里得有「写得进去吗」这一问。"""
    body = _body_of(STUB_SOURCE, "public static string WhyNotInstallable")
    assert "WhyThisFolderRefusesUs" in body, (
        "装之前不问写不写得进去 —— 于是只能等解压到一半，用一句 .NET 原文告诉人")


def test_it_answers_by_writing_not_by_looking_at_the_path() -> None:
    """答法是真去建一个目录再删掉，不是按路径前缀猜。

    按路径判（"以 C:\Program Files 开头就拒"）是一条会漂的规则：**提权跑的安装器
    写得进去**，于是它会挡掉一次本来能成的安装；而写不进去的地方也远不止那一个。
    """
    body = _body_of(STUB_SOURCE, "private static string WhyThisFolderRefusesUs")
    assert "Directory.CreateDirectory" in body and "Directory.Delete" in body, (
        "没真去写一次 —— 那这个答案就是猜的")
    assert "UnauthorizedAccessException" in body, "没认出「要管理员权限」这一种"
    assert "Program Files" not in body, (
        "按路径前缀判了 —— 提权跑的安装器写得进 Program Files，这条规则会挡掉它")


def test_the_person_hears_it_while_they_are_still_choosing() -> None:
    """打字那一刻就问，不是等他点安装。"""
    # 查的是**那一根线**，不是两个名字都还在：两头都在、中间断了，是这类判据
    # 最常见的假绿（2026-09-23 变异实测漏过一次）。
    # 只看那个 delegate 的**体内**。前两版都栽在"窗口比要判的东西大"上：
    # `[^;]*` 在 `ReportSpace();` 的分号处就停了（好线被判成断的）；换成
    # `[\s\S]{0,200}?` 又越过那一行，匹配到下面独立的那次 `AskAgainShortly();`
    # （断线被判成好的）。两次都是判据读了它不该读的地方。
    hooked = re.search(r"_dir\.TextChanged\s*\+=\s*delegate\s*\{([^}]*)\}", WIZARD_SOURCE)
    assert hooked, "路径框上没挂 TextChanged"
    assert "AskAgainShortly()" in hooked.group(1), (
        "改了路径不重新问 —— 答案还是只能在点了安装之后到")
    asks = _body_of(WIZARD_SOURCE, "private void RecheckTheFolder")
    assert "WhyNotInstallable" in asks, "重新问的时候没去问那个唯一的判据"
    assert "_install.Enabled" in asks, "装不进去还让人点「安装」"


def test_there_is_a_way_out_not_just_a_complaint() -> None:
    """说了装不进去，就得给一条出路 —— 否则人卡在那儿。"""
    handler = re.search(r"_useDefault\.LinkClicked\s*\+=\s*delegate\s*\{([^}]*)\}", WIZARD_SOURCE)
    assert handler, "那条出路没接处理函数"
    assert "_dir.Text" in handler.group(1) and "DefaultInstallDir()" in handler.group(1), (
        "「换成不用权限的位置」点下去什么都不做")
    assert "_useDefault.Visible = false" in WIZARD_SOURCE, (
        "那条出路一直摆着 —— 它该只在真的写不进去时出现")


def test_no_bare_Timer_where_two_namespaces_define_one() -> None:
    """同时 using 了 System.Threading 和 System.Windows.Forms 就不许裸写 Timer。

    这一条只挡一个名字，挡不住这一类的全部 —— 真正完整的判官是 csc，而 CI 只有
    Linux。写它是因为它**真的咬过**：2026-09-23 加防呆那一版裸写了 `Timer`，在真
    csc 上是 CS0104（`System.Threading.Timer` vs `System.Windows.Forms.Timer`），
    而 Mac 这边编的是 Swift，什么都看不见 —— 和 2026-09-18 那次 CS0246 同一个形状。
    """
    for path in sorted((REPO / "platform/desktop/windows").glob("*.cs")):
        source = path.read_text(encoding="utf-8")
        if "using System.Threading;" not in source:
            continue
        if "using System.Windows.Forms;" not in source:
            continue
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith(("//", "///")))
        assert not re.search(r"(?<![.\w])Timer\b", code), (
            f"{path.name} 里裸写了 Timer，而这个文件同时 using 了 System.Threading 和 "
            "System.Windows.Forms —— 真 csc 上是 CS0104。写全名。")



# ── 2026-10-19：高 DPI 屏上挤成一堆 ───────────────────────────────────────────
#
# wangd 转来一台 150% 缩放的 Windows 上的截图：标题压着副标题、「浏览…」只剩半截、
# 「需要约 0.9 GB」压在输入框上。病根是一句许了没兑现的诺：`PrepareUi` 调了
# `SetProcessDPIAware`（「别替我拉伸，我自己按 DPI 画」），字号是磅、自然放大了 1.5 倍；
# 而每个坐标是按 96 DPI 写的像素，窗口也没开自动缩放 —— 格子还是原来那么大。
#
# 真机上量出来的三条（9800x3d，DPI 144，见 Footprint.BeginLayoutAt96Dpi 的注释）：
# 不挂起布局就开自动缩放 = 什么都不放大；界面字体在放大之前设 = 固定尺寸的 Label
# 被放大两次；字体在放大之后设 = 全部恰好一次。顺序收在 Footprint 那一对函数里，
# 这里查两件事：**许了诺的编译单元里，每个窗口都走那一对**；**那一对里的顺序没被换掉**。
# 布局本身（有没有叠、有没有截）CI 上量不了 —— 那要真 DPI，见 PR 里的真机探针。

FOOTPRINT_SOURCE = FOOTPRINT.read_text(encoding="utf-8")

#: 声明「我自己按 DPI 画」的写法。这一单元里谁都别想让系统替它拉伸。
PROMISES_DPI = ("SetProcessDPIAware", "SetProcessDpiAwareness", "SetProcessDpiAwarenessContext")

#: `class X : Form` —— 每一个窗口，不是名单。
A_FORM = re.compile(r"\bclass[ \t]+(\w+)[ \t]*:[ \t]*Form\b")


def _code_only(source: str) -> str:
    """去掉 `//` 注释：注释里提一句 `Footprint.BeginLayoutAt96Dpi` 不算调用了它。"""
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.splitlines())


def _forms_that_promised_to_scale() -> list[tuple[str, str, str]]:
    """(单元, 窗口类名, 构造函数正文)，只收许了 DPI 诺的单元里的窗口。"""
    found: list[tuple[str, str, str]] = []
    for unit, sources in sorted(_the_compile_units().items()):
        texts = [source.read_text(encoding="utf-8") for source in sources]
        if not any(mark in text for text in texts for mark in PROMISES_DPI):
            continue
        for text in texts:
            code = _code_only(text)
            for match in A_FORM.finditer(code):
                name = match.group(1)
                ctor = _body_of(code[match.start():], f"public {name}(")
                found.append((unit, name, ctor))
    return found


def test_the_dpi_scan_finds_the_windows_it_is_meant_to_guard() -> None:
    """扫不到就等于没有这道闸 —— 安装向导和卸载确认框都得在里面。"""
    names = {name for _, name, _ in _forms_that_promised_to_scale()}
    assert {"WizardForm", "ConfirmUninstall"} <= names, (
        f"许了 DPI 诺的单元里只扫到 {sorted(names)} —— 扫法塌了，这道闸是空的")


@pytest.mark.parametrize(
    "unit,form,ctor", _forms_that_promised_to_scale(),
    ids=[f"{unit}:{name}" for unit, name, _ in _forms_that_promised_to_scale()])
def test_a_window_in_a_dpi_aware_process_scales_its_pixels(unit: str, form: str, ctor: str) -> None:
    """进程说了「我自己按 DPI 画」，窗口就得把 96 DPI 写的坐标交给 WinForms 放大。"""
    begin = ctor.find("Footprint.BeginLayoutAt96Dpi(this)")
    end = ctor.find("Footprint.EndLayoutAt96Dpi(this")
    assert begin >= 0 and end >= 0, (
        f"{unit} 里的 {form} 没走 Footprint.BeginLayoutAt96Dpi / EndLayoutAt96Dpi —— "
        "进程是 DPI 感知的，150% 的屏上字放大了、格子没放大，就是那张挤成一堆的截图")
    adds = [m.start() for m in re.finditer(r"\bControls\.Add(?:Range)?\(", ctor)]
    sized = [m.start() for m in re.finditer(r"\bClientSize\s*=", ctor)]
    assert adds, f"{form} 的构造函数里一个控件都没加 —— 扫法塌了"
    assert begin < min(adds + sized), (
        f"{form} 先加了控件 / 定了窗口大小才开始按 96 DPI 摆 —— 挂起布局之前的东西放大不到")
    assert end > max(adds), (
        f"{form} 放大完了还在往窗口里加控件 —— 那些控件按原像素摆，150% 下照样挤")
    # 窗口自己的字体只许在 EndLayoutAt96Dpi 里设。语句形式（以分号结尾）才是窗口的；
    # 对象初始化器里的 `Font = …,` 是某个 Label 自己的，不算。
    own_font = re.search(r"^[ \t]*(?:this\.)?Font[ \t]*=[^;\n]*;", ctor, re.MULTILINE)
    assert not own_font, (
        f"{form} 在构造函数里自己设了窗口字体 —— 设在放大之前的话，固定尺寸的 Label "
        "会被放大两次（真机量过：100 → 225）。字体交给 EndLayoutAt96Dpi")


def test_the_layout_pair_keeps_the_measured_order() -> None:
    """Begin：先挂起再开自动缩放；End：先放大再换字体。两条都是真机上量出来的。"""
    begin = _code_only(_body_of(FOOTPRINT_SOURCE, "public static void BeginLayoutAt96Dpi"))
    assert "new SizeF(96F, 96F)" in begin and "AutoScaleMode.Dpi" in begin, (
        "没声明「坐标是按 96 DPI 写的、按 DPI 放大」")
    suspend = begin.find("SuspendLayout()")
    assert 0 <= suspend < begin.find("AutoScaleMode"), (
        "没先挂起布局就开了自动缩放 —— WinForms 当场放大一个空窗口，之后的控件全按原像素摆")
    end = _code_only(_body_of(FOOTPRINT_SOURCE, "public static void EndLayoutAt96Dpi"))
    resume = end.find("ResumeLayout(")
    font = end.find(".Font = ")
    assert 0 <= resume < font, (
        "字体在放大之前设了（或者没设）—— 固定尺寸的 Label 会被放大两次")


# ── 2026-10-19：安装器两种语言，默认英文 ──────────────────────────────────────
#
# wangd：「安装器也可以选语言，默认英文。」安装器是一个不懂中文的人看到的第一个画面 ——
# 从前它只有中文。现在每一句写成 `Speech.T("中文", "English")`，向导第一页顶上放「中文 |
# English」。判据扫的是"编进安装器 / 卸载器的源码里出现了中文"这件事本身：每一个带中文的
# 字面量都必须是 `Speech.T` / `Speech.In` 的中文那个参数，紧跟着的英文参数里不许有中文。
# 哪个单元算"安装器家族"不列名单：编进了定义 `Speech` 的那份源码的单元都算。

_HAN = re.compile(r"[一-鿿]")
_STRING_START = re.compile(r'(\$@|@\$|\$|@)?"')


def _csharp_strings_and_mask(source: str) -> tuple[list[tuple[int, int]], str]:
    """(每个字符串字面量的 [起, 止)，以及一份把注释和字符串内容都换成空格的源码)。

    换成空格是为了配括号、拆参数时不被字符串里的括号和逗号骗 —— 位置一个都不挪。
    """
    spans: list[tuple[int, int]] = []
    masked = list(source)
    i, n = 0, len(source)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if masked[k] != "\n":
                masked[k] = " "

    while i < n:
        if source.startswith("//", i):
            j = source.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if source[i] == "'":
            j = i + 1
            while j < n and source[j] != "'":
                j += 2 if source[j] == "\\" else 1
            blank(i, j + 1)
            i = j + 1
            continue
        start = _STRING_START.match(source, i)
        if start:
            verbatim = "@" in (start.group(1) or "")
            j = start.end()
            while j < n:
                if verbatim and source[j] == '"' and source[j + 1:j + 2] == '"':
                    j += 2
                    continue
                if not verbatim and source[j] == "\\":
                    j += 2
                    continue
                if source[j] == '"':
                    break
                j += 1
            spans.append((i, j + 1))
            blank(i, j + 1)
            i = j + 1
            continue
        i += 1
    return spans, "".join(masked)


def _arguments(masked: str, opened: int) -> list[tuple[int, int]]:
    """从 `(` 开始，按顶层逗号切出每个参数的 [起, 止)。"""
    depth, start, out = 0, opened + 1, []
    for k in range(opened, len(masked)):
        ch = masked[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                out.append((start, k))
                return out
        elif ch == "," and depth == 1:
            out.append((start, k))
            start = k + 1
    raise AssertionError("括号没配上")


def _units_that_speak() -> dict[str, list[Path]]:
    units = {}
    for unit, sources in _the_compile_units().items():
        if any("static class Speech" in s.read_text(encoding="utf-8") for s in sources):
            units[unit] = sources
    return units


def test_the_installer_family_is_found() -> None:
    units = _units_that_speak()
    assert "make_the_installer" in units, f"安装器那个单元没编进 Speech：{sorted(units)}"
    assert any(any(s.name == "Uninstaller.cs" for s in sources) for sources in units.values()), (
        "卸载器那个单元没编进 Speech —— 卸载时又是一屏只有中文")
    assert any(any(s.name == "Shell.cs" for s in sources) for sources in units.values()), (
        "壳那个单元没编进 Speech —— 后端起不来时弹的对话框又只有中文")


def test_every_csharp_source_that_draws_a_window_is_in_a_unit_that_speaks() -> None:
    """新加一个画窗口的单元而忘了编进 Speech.cs，上面那条判据就看不见它 —— 这一条看得见。"""
    speaking = {s for sources in _units_that_speak().values() for s in sources}
    for unit, sources in _the_compile_units().items():
        texts = {s: s.read_text(encoding="utf-8") for s in sources}
        draws = any(mark in t for t in texts.values() for mark in DRAWS_A_WINDOW)
        chinese = any(_HAN.search(t) for t in texts.values())
        if draws and chinese:
            assert all(s in speaking for s in sources), f"{unit} 画窗口、带中文，却没编进 Speech.cs"


@pytest.mark.parametrize("unit", sorted(_units_that_speak()))
def test_every_chinese_line_in_the_installer_also_says_it_in_english(unit: str) -> None:
    offenders: list[str] = []
    for source_path in _units_that_speak()[unit]:
        source = source_path.read_text(encoding="utf-8")
        spans, masked = _csharp_strings_and_mask(source)
        allowed: list[tuple[int, int]] = []
        # 写日志的 Console 输出不是界面：整句放行（壳的 stderr 进 shell.log，给找问题的人看）。
        for call in re.finditer(r"\bConsole\.(?:Error\.)?Write(?:Line)?\s*\(", masked):
            allowed.append(_arguments(masked, call.end() - 1)[0])
        for call in re.finditer(r"\bSpeech\.(T|In|Both|Pair)\s*\(", masked):
            args = _arguments(masked, call.end() - 1)
            zh = 1 if call.group(1) == "In" else 0
            if len(args) < zh + 2:
                offenders.append(f"{source_path.name}:{source.count(chr(10), 0, call.start()) + 1} Speech.{call.group(1)} 少了一个参数")
                continue
            allowed.append(args[zh])
            en_from, en_to = args[zh + 1]
            if _HAN.search(source[en_from:en_to]):
                offenders.append(f"{source_path.name}:{source.count(chr(10), 0, en_from) + 1} 英文那一侧写着中文")
        # 语言自己的名字用它自己的文字写（「中文」）—— 这是唯一一处有意不翻的。
        name_of = re.search(r"static string NameOf\s*\(", masked)
        if name_of:
            body = masked.index("{", name_of.end())
            allowed.append(_arguments(masked.replace("{", "(").replace("}", ")"), body)[0])
        for a, b in spans:
            if _HAN.search(source[a:b]) and not any(x <= a and b <= y for x, y in allowed):
                offenders.append(f"{source_path.name}:{source.count(chr(10), 0, a) + 1} → {source[a:b][:50]}")
    assert not offenders, (
        f"{unit} 里这些话只有中文，英文安装界面上会原样露出来（{len(offenders)} 处）。"
        "写成 Speech.T(\"中文\", \"English\")：\n" + "\n".join(offenders))


def test_the_installer_speaks_english_unless_someone_picks_chinese() -> None:
    """默认英文（wangd 2026-10-19），第一页顶上能换，换了算这个人选的、交给应用。"""
    speech = (WINDOWS_SHELL / "Speech.cs").read_text(encoding="utf-8")
    assert re.search(r"public static string Language\s*=\s*English\s*;", speech), "安装器默认不是英文"
    switch = _body_of(WIZARD_SOURCE, "private void SwitchTo")
    assert "Speech.ChosenByThePerson = true" in switch, "向导上换了语言却不算这个人选的 —— 应用第一次打开不会照它"
    assert "foreach (Action say in _speech) say();" in switch, "换了语言，已经摆出来的字不跟着换"
    footprint_of_install = _body_of(STUB_SOURCE, "private static void LeaveTheFootprint")
    assert "language = Speech.Language" in footprint_of_install, "清单没记装的时候的语言 —— 卸载器不知道该说哪种"
    assert "Footprint.TellTheAppTheLanguage(" in footprint_of_install, "选过的语言没交给应用"
