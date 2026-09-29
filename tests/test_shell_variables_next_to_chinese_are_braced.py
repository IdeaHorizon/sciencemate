"""shell 脚本里，`$变量` 后面紧跟中文时必须写成 `${变量}`。

## 病例（2026-09-23，真跑服务器安装脚本时撞到）

    install-server.sh: line 541: ENV_FILE\\xef: unbound variable

那一行是 `…必须是 org（检查 $ENV_FILE）`。macOS 自带的 bash 3.2 在 **UTF-8 locale**
（也就是正常的终端：en_US.UTF-8 / zh_CN.UTF-8）下把全角括号的第一个字节当成变量名的
一部分；脚本开着 `set -u`，于是报"变量未定义"退出 —— 本该给人看的那句话被一句乱码顶掉。
`LANG=C` 反而没事，所以写的人在自己的 shell 里多半试不出来。

同一个写法在 Mac 一行安装器（`scripts/package/install.sh`）的**第①步**：
`say "① 下载 $DMG（curl，…）"` —— 在正常的 Mac 终端里 `curl … | bash`，第一步就崩。

判据扫**每一个**被跟踪的 shell 脚本，不挑名单：新脚本默认也在里面。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_BARE_BEFORE_NON_ASCII = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])")


def _shell_scripts() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "*.sh", "*.bash"], cwd=REPO,
                            capture_output=True, text=True).stdout.split()
    return [REPO / name for name in listed] or sorted(
        p for p in REPO.rglob("*.sh") if ".venv" not in p.parts and "node_modules" not in p.parts)


def test_there_are_scripts_to_check() -> None:
    names = {p.relative_to(REPO).as_posix() for p in _shell_scripts()}
    assert "scripts/package/install.sh" in names
    # 组织服务器的安装脚本是专业版的：在这棵树里就必须被扫到，不在（公开树）就没有可扫的。
    if (REPO / "deploy/org/install-server.sh").exists():
        assert "deploy/org/install-server.sh" in names


def test_no_variable_runs_into_a_chinese_character() -> None:
    found = []
    for script in _shell_scripts():
        for number, line in enumerate(script.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for match in _BARE_BEFORE_NON_ASCII.finditer(line):
                found.append(f"{script.relative_to(REPO)}:{number}  {match.group(0)}…")
    assert not found, (
        "这些 `$变量` 后面紧跟着非 ASCII 字符。macOS 的 bash 3.2 在 UTF-8 终端里会把那个字的"
        "第一个字节读进变量名，`set -u` 下直接报「unbound variable」退出。写成 ${变量}：\n  "
        + "\n  ".join(found))
