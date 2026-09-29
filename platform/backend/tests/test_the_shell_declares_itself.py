"""壳启动时自报家门（`<数据根>/shell.json`），后端只读不猜。

## 为什么要有这一份（#953 第①步）

自更新只换 harness 与界面，换不了壳。后端要回答「这次更新含不含壳的改动、装着的壳会不会
自己换」，前提是知道**装着的壳到底是哪一份** —— 而只有壳自己最清楚它的路径、它的字节、
它有没有自替换能力。后端去猜（按版本号推断）就是第二个真相源，分叉时不报错。

判据落在三处：读取函数对「没报 / 报的读不懂 / 报全了」各自的答案；每一份 `UpdateStatus`
都带着它（写了没接线等于没有）；两个壳的**代码**里都真的写这个文件、且写在起后端之前。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import self_update as su

REPO = Path(__file__).resolve().parents[3]

GOOD = {
    "platform": "windows",
    "path": r"C:\Users\x\AppData\Local\Programs\ScienceMate\ScienceMate.exe",
    "sha256": "a" * 64,
    "size": 19456,
    "self_replace": False,
    "declared_at": "2026-09-12T04:00:00Z",
}


def test_no_declaration_means_none(tmp_path: Path) -> None:
    """0.4.5 及更早的壳不写这个文件 —— 那就是「不知道」，不是错误。"""
    assert su.read_shell_declaration(tmp_path) is None


def test_a_half_declaration_is_treated_as_none(tmp_path: Path) -> None:
    """字段少一个就当没报：`self_replace` 缺了却把 sha256 当真，上层会得出无法行动的结论。"""
    for missing in su.SHELL_DECLARATION_KEYS:
        half = {k: v for k, v in GOOD.items() if k != missing}
        su.shell_declaration_path(tmp_path).write_text(json.dumps(half), encoding="utf-8")
        assert su.read_shell_declaration(tmp_path) is None, f"缺 {missing} 还被当成有效"


def test_garbage_is_treated_as_none(tmp_path: Path) -> None:
    p = su.shell_declaration_path(tmp_path)
    for junk in ("", "not json", "[]", json.dumps({**GOOD, "sha256": "xyz"}),
                 json.dumps({**GOOD, "self_replace": "yes"})):
        p.write_text(junk, encoding="utf-8")
        assert su.read_shell_declaration(tmp_path) is None, f"读懂了不该读懂的：{junk!r}"


def test_a_full_declaration_is_returned_verbatim(tmp_path: Path) -> None:
    su.shell_declaration_path(tmp_path).write_text(json.dumps(GOOD), encoding="utf-8")
    assert su.read_shell_declaration(tmp_path) == GOOD


def test_every_update_status_carries_the_declaration(tmp_path: Path, monkeypatch) -> None:
    """接线：`check_for_update` 造的每一份状态都带着壳的家门 —— 不管更新源通不通。"""
    su.shell_declaration_path(tmp_path).write_text(json.dumps(GOOD), encoding="utf-8")
    harness = tmp_path / "h"; (harness / "core").mkdir(parents=True)
    (harness / su.VERSION_MARKER).write_text("1.0.0\n")

    def unreachable(_url):
        raise OSError("离线")

    status, *_ = su.check_for_update(tmp_path, harness, "https://h/x/manifest.json", unreachable, unreachable)
    assert status.shell == GOOD, "更新源不通时状态里也该有壳的家门 —— 它和更新源无关"
    assert status.as_dict()["shell"] == GOOD, "as_dict 漏了 shell —— 接口上看不见等于没有"


def _code_without_comments(path: Path, markers: tuple[str, ...]) -> str:
    text = path.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith(markers))


def test_the_windows_shell_declares_itself_before_starting_the_backend() -> None:
    """扫**代码**不扫注释（解释「为什么写 shell.json」的注释里一定会出现 shell.json）。"""
    code = _code_without_comments(REPO / "platform/desktop/windows/Shell.cs", ("//", "///"))
    assert '"shell.json"' in code, "Windows 壳不写 shell.json"
    main = code[code.index("private static int Main("):]
    assert main.index("DeclareMyself();") < main.index("LoadConfig("), (
        "自报家门得在读配置之前 —— 配置读失败那条早退路上也该留下家门")
    assert "SHA256" in code, "没算自己的 sha256 —— 后端认不出装着的是哪一份"
    # C# 里 JSON 是手拼的字符串字面量，源码里是转义形态 `\"self_replace\": false`。
    assert '\\"self_replace\\": true' in code, "没声明会不会自换（④ 落地后应为 true）"


def test_the_mac_shell_declares_itself_before_starting_the_backend() -> None:
    code = _code_without_comments(REPO / "platform/desktop/mac/Shell.swift", ("//", "///"))
    assert '"shell.json"' in code, "Mac 壳不写 shell.json"
    launch = code[code.index("func applicationDidFinishLaunching"):]
    assert launch.index("declareMyself()") < launch.index("startBackend()"), (
        "自报家门得在起后端之前 —— 后端一起来就可能去读")
    assert "SHA256.hash" in code and "import CryptoKit" in code
    assert '"self_replace": true' in code, "④ 落地后 Mac 壳也会自换，得说 true"


def test_both_shells_agree_on_the_fields_the_backend_requires() -> None:
    """三方（两个壳、后端）对字段名只能有一个答案 —— 这条把三份钉在一起。"""
    win = _code_without_comments(REPO / "platform/desktop/windows/Shell.cs", ("//", "///"))
    mac = _code_without_comments(REPO / "platform/desktop/mac/Shell.swift", ("//", "///"))
    for key in su.SHELL_DECLARATION_KEYS:
        assert f'\\"{key}\\"' in win or f'"{key}"' in win, f"Windows 壳没写 {key}"
        assert f'"{key}"' in mac, f"Mac 壳没写 {key}"
