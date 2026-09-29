"""不买证书也能发：发布目录一个地址、install.sh 不经浏览器、收尾文案说真话。

## 为什么要有这些判据

- 「右键 →「打开」」在 macOS 15 之后不成立。写在收尾文案里，照着做的人会以为包坏了。
- Gatekeeper 只看带隔离标记的文件；标记是浏览器打的，curl 不打。install.sh 的价值全在
  "不经浏览器"这一件事上 —— 所以它必须用 curl 拉、必须核校验和（没有签名了，校验和是
  唯一的完整性证据）、必须在失败时停。
- 安装与自更新共用一个目录：目录里每个会被下载的文件都得在 SHA256SUMS 里。
"""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "scripts" / "package"


def _by_path(name: str):
    spec = importlib.util.spec_from_file_location(f"afs_package_{name}", PACKAGE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def packager(tmp_path, monkeypatch):
    b = _by_path("build_mac_app")
    monkeypatch.setattr(b, "DIST", tmp_path / "dist")
    return b


def test_the_closing_copy_no_longer_tells_people_to_right_click(packager) -> None:
    """macOS 15 起那条捷径没了。收尾文案得说真实的两条路。"""
    text = packager.HOW_TO_INSTALL
    assert "右键" not in text, "还在教人右键打开 —— 新系统上这一步不存在"
    assert "curl -fsSL" in text and "install.sh" in text, "没给不经浏览器那条路"
    assert "隐私与安全性" in text and "仍要打开" in text, "经浏览器那条路的四步没写清"
    assert "同一个地址" in text, "没说安装与自更新共用一个地址"


def test_the_release_dir_is_one_servable_thing(packager, tmp_path) -> None:
    """dmg + SHA256SUMS + install.sh 在一个目录里；每个会被下载的文件都在校验表里。"""
    dmg = tmp_path / "Agent-for-Science-9.9.9-arm64.dmg"
    dmg.write_bytes(b"not really a dmg")
    (packager.DIST / "release").mkdir(parents=True)
    (packager.DIST / "release" / "payload-9.9.9.tar.gz").write_bytes(b"payload")
    (packager.DIST / "release" / "manifest.json").write_text("{}")

    release = packager.assemble_the_release_dir(dmg, "http://10.0.0.1/afs/")

    assert (release / dmg.name).read_bytes() == b"not really a dmg"
    sums = dict(line.split("  ", 1)[::-1] for line in (release / "SHA256SUMS").read_text().splitlines())
    for name in (dmg.name, "payload-9.9.9.tar.gz", "manifest.json"):
        assert name in sums, f"{name} 不在 SHA256SUMS 里 —— 装的人没法核对它"
        assert sums[name] == hashlib.sha256((release / name).read_bytes()).hexdigest()
    assert "install.sh" not in sums and "SHA256SUMS" not in sums


def test_install_sh_is_baked_with_the_url_and_the_dmg_name(packager, tmp_path) -> None:
    dmg = tmp_path / "Agent-for-Science-9.9.9-arm64.dmg"; dmg.write_bytes(b"x")
    release = packager.assemble_the_release_dir(dmg, "http://10.0.0.1/afs/")
    script = (release / "install.sh").read_text(encoding="utf-8")
    assert 'DEFAULT_URL="http://10.0.0.1/afs"' in script, "地址没烧进去（或没去掉尾斜杠）"
    assert 'DEFAULT_DMG="Agent-for-Science-9.9.9-arm64.dmg"' in script
    assert script.count("http://10.0.0.1/afs") == 1, "地址应只出现在赋值那一行 —— 出现在别处就是模板又把占位符写到判断里了"
    assert "__RELEASE_URL__" not in script and "__DMG_NAME__" not in script
    assert (release / "install.sh").stat().st_mode & 0o111, "install.sh 不可执行"


def test_install_sh_without_a_url_refuses_to_guess(packager, tmp_path) -> None:
    """没传 --release-url：脚本里留空，跑起来得明说要 export 什么，不能默默拉一个猜的地址。"""
    dmg = tmp_path / "x.dmg"; dmg.write_bytes(b"x")
    release = packager.assemble_the_release_dir(dmg, "")
    result = subprocess.run(["sh", str(release / "install.sh")], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "AFS_DRY_RUN": "1"})
    assert result.returncode != 0
    assert "AFS_RELEASE_URL" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="脚本本身只在 macOS 上有意义；Linux 只做语法检查")
def test_install_sh_plans_exactly_the_safe_steps(packager, tmp_path) -> None:
    """dry-run 打印的计划：curl 拉 → 核校验和 → 挂载/复制/卸载 → 去标记 → 打开。顺序不能乱。"""
    dmg = tmp_path / "Agent-for-Science-9.9.9-arm64.dmg"; dmg.write_bytes(b"x")
    release = packager.assemble_the_release_dir(dmg, "http://10.0.0.1/afs")
    result = subprocess.run(["sh", str(release / "install.sh")], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "AFS_DRY_RUN": "1", "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    plan = result.stdout
    order = [plan.index(k) for k in ("curl -fsSL", "shasum -a 256", "hdiutil attach", "ditto", "hdiutil detach", "xattr -dr com.apple.quarantine", "open -a")]
    assert order == sorted(order), f"步骤顺序不对：\n{plan}"
    assert "http://10.0.0.1/afs/Agent-for-Science-9.9.9-arm64.dmg" in plan
    assert "http://10.0.0.1/afs/SHA256SUMS" in plan


def test_install_sh_is_valid_shell() -> None:
    assert subprocess.run(["sh", "-n", str(PACKAGE / "install.sh")]).returncode == 0


# ── 别处打好的发布件：只收哈希 ─────────────────────────────────────────────────
#
# Windows 的 Setup.exe 在 Windows 构建机上生、在 Forgejo 所在的那台 PC 上传，从来
# 不需要经过 Mac。2026-09-21 真把它拖过来过一次：scp 退出 0，369 MB 只到 49 MB ——
# 而截断的文件会被原样哈希进清单发出去。清单需要的只是哈希，那就只收哈希。

def test_a_foreign_installer_is_listed_by_hash_alone(packager, tmp_path) -> None:
    dmg = tmp_path / "ScienceMate-9.9.9-arm64.dmg"; dmg.write_bytes(b"dmg")
    digest = "a" * 64
    release = packager.assemble_the_release_dir(dmg, "http://h/x", {"ScienceMate-Setup.exe": digest})
    sums = dict(line.split("  ", 1)[::-1] for line in (release / "SHA256SUMS").read_text().splitlines())
    assert sums["ScienceMate-Setup.exe"] == digest, "别处打好的安装器没进清单 —— install.ps1 会找不到它"
    assert not (release / "ScienceMate-Setup.exe").exists(), "本来就不该把字节搬过来"


def test_a_foreign_installer_that_is_actually_here_must_agree(packager, tmp_path) -> None:
    """文件碰巧在本地时，给的哈希得和它一致 —— 两边有一个是错的就别发。"""
    dmg = tmp_path / "ScienceMate-9.9.9-arm64.dmg"; dmg.write_bytes(b"dmg")
    release_dir = packager.DIST / "release"; release_dir.mkdir(parents=True, exist_ok=True)
    (release_dir / "ScienceMate-Setup.exe").write_bytes(b"real bytes")
    with pytest.raises(SystemExit, match="别发"):
        packager.assemble_the_release_dir(dmg, "http://h/x", {"ScienceMate-Setup.exe": "b" * 64})
    real = hashlib.sha256(b"real bytes").hexdigest()
    release = packager.assemble_the_release_dir(dmg, "http://h/x", {"ScienceMate-Setup.exe": real})
    lines = (release / "SHA256SUMS").read_text().splitlines()
    assert sum(1 for line in lines if line.endswith("  ScienceMate-Setup.exe")) == 1, "同一个文件列了两遍"


@pytest.mark.parametrize("bad", ["ScienceMate-Setup.exe", "ScienceMate-Setup.exe=abc",
                                 "ScienceMate-Setup.exe=" + "A" * 64, "dir/x.exe=" + "a" * 64, "=" + "a" * 64])
def test_a_malformed_foreign_entry_is_refused(packager, bad) -> None:
    """抄错的哈希写进清单，装的人核对时才发现 —— 那时已经发出去了。"""
    with pytest.raises(SystemExit, match="名字=64位"):
        packager.a_foreign_asset(bad)


def test_the_command_line_hash_actually_reaches_the_manifest() -> None:
    """`--windows-installer` 收了不接进装配，等于没有 —— 判接线，不判有没有这个参数。"""
    import ast
    tree = ast.parse((PACKAGE / "build_mac_app.py").read_text(encoding="utf-8"))
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "assemble_the_release_dir"]
    assert calls, "main 里没有装配发布目录那一步"
    assert len(calls[0].args) >= 3, "装配时没把外来件的哈希传进去 —— 参数收了、清单里没有"
    reads = {(n.value.id, n.attr) for n in ast.walk(main)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert ("args", "windows_installer") in reads, "--windows-installer 从来没被读过"
    parsed = [n for n in ast.walk(main) if isinstance(n, ast.Call)
              and getattr(n.func, "id", "") == "a_foreign_asset"]
    assert parsed, "命令行那串没经过 a_foreign_asset 校验就用了"
