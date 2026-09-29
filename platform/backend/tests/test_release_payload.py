"""发布端：版本只有一个来源、载荷带标记、签名对不上就别发、没钥匙就说没钥匙。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tarfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _by_path(name: str):
    """按路径装载，与打包器自己找兄弟模块的方式一致；sys.path 一字不动。"""
    spec = importlib.util.spec_from_file_location(f"afs_package_{name}", REPO / "scripts" / "package" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pr, rk = _by_path("payload_release"), _by_path("release_keys")


@pytest.fixture
def staged(tmp_path):
    h = tmp_path / "harness"; (h / "core").mkdir(parents=True)
    (h / "core" / "agent_loop.py").write_text("# loop\n"); (h / "core" / "__pycache__").mkdir()
    (h / "core" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    u = tmp_path / "static_ui"; u.mkdir(); (u / "index.html").write_text("<html>")
    return h, u


def test_the_version_has_exactly_one_source() -> None:
    """根 pyproject.toml。打包器的 Info.plist / dmg 名 / manifest / 应用自报全从这读。"""
    version = pr.the_version()
    assert version and version[0].isdigit()
    mac = (REPO / "scripts" / "package" / "build_mac_app.py").read_text(encoding="utf-8")
    assert '"0.1.0"' not in mac and "0.1.0-arm64" not in mac, "打包器里还有手写的版本号"
    assert "payload_release.the_version()" in mac


def test_a_signed_payload_carries_marker_manifest_and_signature(staged, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    public = rk.generate()
    monkeypatch.setattr(pr, "baked_public_key", lambda: public)
    h, u = staged

    manifest = pr.build_the_payload(h, u, tmp_path / "out", version="1.2.3")

    assert manifest["_signed"] is True and manifest["version"] == "1.2.3"
    assert (h / pr.VERSION_MARKER).read_text().strip() == "1.2.3", "载荷里没写版本标记"
    out = tmp_path / "out"
    assert (out / "manifest.json.sig").is_file()
    doc = json.loads((out / "manifest.json").read_bytes())
    assert doc["unit"] == ["harness", "static_ui"] and doc["schema"] == 1
    with tarfile.open(out / "payload-1.2.3.tar.gz") as tar:
        names = tar.getnames()
    assert "harness/PAYLOAD_VERSION" in names and "static_ui/index.html" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names), "字节码进了载荷"


#: 差分闸用的树 —— 多层目录、每层多个文件，让"层级"和"顺序"也真的进到字节里。
_CONTENT = {
    "harness/core/agent_loop.py": "# loop\n",
    "harness/core/zzz_last.py": "# z\n",
    "harness/shared/aaa_first.py": "# a\n",
    "static_ui/index.html": "<html>",
    "static_ui/assets/app.js": "console.log(1)\n",
}


def _write_the_tree(root: Path) -> tuple[Path, Path]:
    for rel, body in _CONTENT.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root / "harness", root / "static_ui"


def _let_the_wall_clock_tick() -> None:
    """睡过一个整秒边界再构建下一次。

    gzip 容器头里的 mtime 是**秒**精度的墙钟。两次构建落在同一秒里，即使谁都不管
    这个字段，字节也照样相同 —— 所以"不跨秒的可复现"什么都没问。2026-09-14 之前
    判据就是这么绿的：单跑 0.2 秒必然同秒，全量并行偶尔排到跨秒才红一次（真的红
    过），于是被当成 flaky。判据不许依赖机器多快：这里强行跨过去。
    """
    time.sleep(1.1)


def _build_on_a_machine(root: Path, out_dir: Path, *, umask: int, mtime: int, tz: str) -> str:
    """在一台"机器"上建树、打载荷，返回载荷的 sha256。

    umask 和时区罩住**整个构建**，不只是建树那一段：`build_the_payload` 自己还要写
    一个 `harness/PAYLOAD_VERSION`，那个文件的权限位是在写它的那一刻定下的。
    """
    previous_umask = os.umask(umask)
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        harness, ui = _write_the_tree(root)
        for path in root.rglob("*"):            # 盘上的时间戳：checkout 什么时候做的
            os.utime(path, (mtime, mtime))
        return pr.build_the_payload(harness, ui, out_dir, version="1.0.0")["sha256"]
    finally:
        os.umask(previous_umask)
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


def test_two_machines_building_the_same_tree_get_the_same_bytes(tmp_path, monkeypatch) -> None:
    """同一份内容，两台哪儿都不一样的机器，必须出同一个 sha256。

    **这一条变的是环境，不是字段名。** 归一化写成"这个字段也归零、那个字段也归零"
    的清单，就只挡得住清单上已经有的那几项：2026-09-08 补 mode（umask 漏的）、
    2026-09-14 补 gzip 头里的 mtime（墙钟漏的）—— 两次都是先出事、再往清单上加一行，
    而每一条判据都只点名它自己那一个字段，下一个漏的字段没有任何判据在等它。

    这里反过来问："这台机器和那台机器差在哪？"—— umask、盘上的时间戳、树在哪个路径
    下、时区、输出目录名、以及两次构建之间跨过的那个整秒，全都不一样。出来的字节必须
    一样。哪个字段漏了不需要谁先想到它。

    实测这一条能抓住历史上那两个真 bug：把 `info.mode` 或 gzip 的 `mtime=0` 改坏，
    它都红。抓不住的那几项在下面两条里 —— 抓不住的原因也写在那儿。
    """
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    a_root = tmp_path / "machine-a"; a_root.mkdir()
    b_root = tmp_path / "a-rather-longer-path-for-machine-b"; b_root.mkdir()

    a = _build_on_a_machine(a_root, tmp_path / "out-a", umask=0o022, mtime=1_000_000_000, tz="UTC")
    _let_the_wall_clock_tick()
    b = _build_on_a_machine(b_root, tmp_path / "out-b", umask=0o077, mtime=1_700_000_000,
                            tz="Asia/Tokyo")

    assert a == b, "同一份内容在两台机器上出了两个 sha256 ——「发的就是审过的那份」核对不了"


def test_every_entry_is_normalised_away_from_whoever_built_it(tmp_path, monkeypatch) -> None:
    """归档里每一条的属主/权限/时间戳都写死 —— 上一条**证不到**的那几项在这里挡。

    差分闸变得了 umask、时区、路径、时间戳，变不了**跑构建的是谁**：一个进程里
    uid/gid/uname/gname 只有一个值，两次构建自然相同，把归一化删掉它也照样绿（实测：
    删掉 `info.uid = info.gid = 0`，差分闸不红）。所以这几项只能直接看归档里写了什么。

    源文件故意建成 0600/0700 —— 默认 umask 下会是 0644/0755，**正好等于写死的值**，
    那样这条判据不管有没有归一化都绿（第一版就是这么假绿的，变异照出来的）。
    """
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    root = tmp_path / "src"; root.mkdir()
    previous = os.umask(0o077)
    try:
        harness, ui = _write_the_tree(root)                 # → 文件 0600、目录 0700
        manifest = pr.build_the_payload(harness, ui, tmp_path / "out", version="1.0.0")
    finally:
        os.umask(previous)

    with tarfile.open(tmp_path / "out" / manifest["archive"]) as tar:
        members = tar.getmembers()

    assert members, "归档是空的"
    for m in members:
        assert (m.uid, m.gid) == (0, 0), f"{m.name} 带着构建机的 uid/gid（{m.uid}:{m.gid}）"
        assert (m.uname, m.gname) == ("", ""), f"{m.name} 带着构建机的用户名（{m.uname!r}/{m.gname!r}）"
        assert m.mtime == 0, f"{m.name} 的 mtime 是 {m.mtime}（跟着磁盘走了）"
        assert m.mode in (pr._FILE_MODE, pr._DIR_MODE), f"{m.name} 的 mode 是 {oct(m.mode)}（跟着 umask 走了）"


def test_the_bytes_do_not_follow_the_filesystems_order(tmp_path, monkeypatch) -> None:
    """文件系统按什么顺序吐目录项，归档就不能按什么顺序排。

    这一项差分闸也证不到，而且**证不到的原因本身就跟机器有关**：APFS 回来的目录项
    本来就是有序的，`sorted()` 删掉在 Mac 上一切照旧（实测差分闸绿）；ext4 是哈希序，
    同一份代码在 CI 上就会次次出不同的字节 —— 正是"本机永远绿、CI 偶发红"那种判据。

    所以不等文件系统发善心：把目录项**倒着**递给构建器，再要求出来的字节和正着递
    完全相同。跟跑在哪个文件系统上无关。
    """
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    root = tmp_path / "src"; root.mkdir()
    harness, ui = _write_the_tree(root)

    forwards = pr.build_the_payload(harness, ui, tmp_path / "fwd", version="1.0.0")["sha256"]
    plain_rglob = Path.rglob
    monkeypatch.setattr(Path, "rglob",
                        lambda self, pattern: iter(sorted(plain_rglob(self, pattern), reverse=True)))
    backwards = pr.build_the_payload(harness, ui, tmp_path / "bwd", version="1.0.0")["sha256"]

    assert forwards == backwards, "归档的字节跟着文件系统吐目录项的顺序走了"


def test_the_archive_header_carries_no_reading_of_the_clock(staged, tmp_path, monkeypatch) -> None:
    """gzip 容器头里不许有墙钟 —— 差分闸能抓这一项，这一条说的是它**为什么**成立。

    tar 里每一条的 mtime `_add_tree` 归一化了，gzip 容器头第 4..7 字节还另有一个，
    `mode="w:gz"` 往里填当时的时间（2026-09-14 的真 bug）。差分闸靠跨过一个整秒边界
    照出它；这一条直接看那 4 个字节，跟两次构建隔多久无关。

    第 3 字节的 FNAME 位一并盯住：`GzipFile` 默认拿 fileobj 的 `.name` 写进头里，
    那是另一个跟构建环境有关的输入，只是当下这条路上的 fileobj 恰好没有名字。
    """
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    h, u = staged

    manifest = pr.build_the_payload(h, u, tmp_path / "out", version="1.0.0")
    header = (tmp_path / "out" / manifest["archive"]).read_bytes()[:10]

    assert header[:2] == b"\x1f\x8b", "出来的不是 gzip"
    assert header[4:8] == b"\x00\x00\x00\x00", f"gzip 头里写了墙钟：{header[4:8].hex()}"
    assert not header[3] & 0x08, "gzip 头里带了 FNAME —— 归档字节跟着构建时的文件名走"


def test_an_unsigned_build_says_so_and_ships_no_signature(staged, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "empty-home"))
    h, u = staged
    manifest = pr.build_the_payload(h, u, tmp_path / "out", version="1.0.0")
    assert manifest["_signed"] is False
    assert not (tmp_path / "out" / "manifest.json.sig").exists(), "没钥匙却出了个签名文件"
    out = capsys.readouterr().out
    assert "未签名" in out, "没钥匙却没说载荷未签名"
    assert "拒收" in out, "没说清后果 —— 装好的应用会拒收这份载荷"


def test_a_key_that_does_not_match_the_baked_public_key_refuses_to_ship(staged, tmp_path, monkeypatch) -> None:
    """签的钥匙和烧进应用的不配对 —— 发出去每一份都会被拒，而发布机这边什么都不报错。这里就要报。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    rk.generate()
    monkeypatch.setattr(pr, "baked_public_key", lambda: rk.public_key_b64(__import__("cryptography.hazmat.primitives.asymmetric.ed25519", fromlist=["Ed25519PrivateKey"]).Ed25519PrivateKey.generate()))
    h, u = staged
    with pytest.raises(SystemExit, match="不配对"):
        pr.build_the_payload(h, u, tmp_path / "out", version="1.0.0")


def test_generate_refuses_to_overwrite_an_existing_key(tmp_path, monkeypatch) -> None:
    """覆盖私钥 = 已装出去的应用再也收不到更新。默认拒绝。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path))
    first = rk.generate()
    with pytest.raises(FileExistsError, match="重装"):
        rk.generate()
    assert rk.public_key_b64(rk.load_private()) == first
    assert oct(rk.private_key_path().stat().st_mode & 0o777) == "0o600"


def test_executing_the_packager_never_touches_sys_path_even_with_another_scripts_package_loaded() -> None:
    """扫盘 helper 会把打包器 exec 一遍。那一刻 `scripts` 这个名字已经是后端的包。

    2026-09-08 真实故障：打包器顶层 `sys.path.insert(0, 仓库根)` + `from scripts.package
    import …` → 全量时（test_seed_local_demo 先被收集）解析到后端的 scripts → 抛 →
    每一条扫盘闸一起报错；单跑全绿。判据：塞一个假 scripts 进 sys.modules，再 exec
    打包器 —— 不许抛、不许改 sys.path。
    """
    fake = sys.modules.get("scripts")
    before = list(sys.path)
    sys.modules["scripts"] = __import__("types").ModuleType("scripts")   # 没有 .package
    try:
        spec = importlib.util.spec_from_file_location("afs_package_build_mac_app_probe",
                                                      REPO / "scripts" / "package" / "build_mac_app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)          # 不许抛
        assert hasattr(module, "payload_release"), "打包器没装到兄弟模块"
    finally:
        if fake is not None:
            sys.modules["scripts"] = fake
        else:
            sys.modules.pop("scripts", None)
    assert sys.path == before, "打包器在导入期改了 sys.path —— 它会遮住后端的 scripts 包"


# ── 组织服务器包进签过名的 manifest（2026-09-23）─────────────────────────────
#
# 组织服务器自己升级时只信 manifest 里 `server` 那一项：包的文件名、大小、sha256、协议号。
# 这里打一次真的载荷，再用**客户端那一侧**的 parse_manifest 验一遍 —— 两边各自实现同一种
# 线上格式，钉住它们没分叉。


def test_the_server_bundle_is_signed_into_the_manifest(staged, tmp_path, monkeypatch) -> None:
    org_protocol = pytest.importorskip("app.pro.org_protocol", reason="服务器包是专业版的：公开树里没有它")
    from app.services import self_update as su

    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    public = rk.generate()
    monkeypatch.setattr(pr, "baked_public_key", lambda: public)
    h, u = staged
    bundle = tmp_path / "sciencemate-server-1.2.3.tar.gz"
    bundle.write_bytes(b"a server bundle")

    pr.build_the_payload(h, u, tmp_path / "out", version="1.2.3", server_bundle=bundle)

    raw = (tmp_path / "out" / "manifest.json").read_bytes()
    sig = (tmp_path / "out" / "manifest.json.sig").read_text()
    manifest = su.parse_manifest(raw, sig, public_b64=public)
    assert manifest["server"] == {
        "archive": "sciencemate-server-1.2.3.tar.gz",
        "sha256": hashlib.sha256(b"a server bundle").hexdigest(),
        "size": len(b"a server bundle"),
        "version": "1.2.3",
        "org_protocol": org_protocol.ORG_PROTOCOL,
    }


def test_a_server_bundle_of_another_version_is_refused(staged, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    h, u = staged
    stale = tmp_path / "sciencemate-server-1.2.2.tar.gz"
    stale.write_bytes(b"old")
    with pytest.raises(SystemExit, match="对不上"):
        pr.build_the_payload(h, u, tmp_path / "out", version="1.2.3", server_bundle=stale)
