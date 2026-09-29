"""更新载荷能带上壳（#953 ②）—— 走第二个归档，老客户端照旧、新客户端两个都收。

## 为什么是第二个归档，而不是往第一个里加

已装客户端有两道故意从严的闸：`manifest.unit` 必须逐字等于 `("harness","static_ui")`，
第一个归档的顶层目录也必须逐字相等。往那里加新单元，0.4.4 / 0.4.5 客户端当场拒收 ——
而且 `check_for_update` 把拒收吞成 `status.error`，横幅静默消失。闸是对的，不放松。
所以壳走 `extras-<ver>.tar.gz` + 一个老客户端会忽略的可选键。

判据落在四处：发布端造得出并自验得过；客户端认得可选键、坏的拒收；两个归档**要么都
到位要么都不算**；发布端与客户端对常量只有一个答案。
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from app.services import self_update as su
from tests.test_self_update import _keypair, _manifest, _sign, _stream_of, _tar

REPO = Path(__file__).resolve().parents[3]


def _by_path(name: str):
    spec = importlib.util.spec_from_file_location(f"afs_package_{name}", REPO / "scripts" / "package" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pr, rk = _by_path("payload_release"), _by_path("release_keys")


# ───────────────────────────────────────────── 发布端与客户端只能有一个答案

def test_the_two_sides_agree_on_the_constants() -> None:
    """常量各一份是为了打包器不 import 后端；这条钉住它们没分叉。"""
    assert tuple(pr.EXTRA_UNITS) == tuple(su.EXTRA_UNITS)
    assert dict(pr.SHELL_BINARY) == dict(su.SHELL_BINARY)
    assert set(pr.SHELL_BINARY) == set(su.SHELL_PLATFORMS)


# ───────────────────────────────────────────── 发布端

@pytest.fixture
def staged(tmp_path):
    h = tmp_path / "harness"; (h / "core").mkdir(parents=True)
    (h / "core" / "agent_loop.py").write_text("# loop\n")
    u = tmp_path / "static_ui"; u.mkdir(); (u / "index.html").write_text("<html>")
    return h, u


def _shell_dirs(tmp_path: Path) -> dict[str, Path]:
    win = tmp_path / "shell-windows"; win.mkdir()
    (win / "ScienceMate.exe").write_bytes(b"MZ-windows-shell")
    (win / "WebView2Loader.dll").write_bytes(b"dll")
    mac = tmp_path / "shell-macos"; mac.mkdir()
    (mac / "ScienceMate").write_bytes(b"\xcf\xfa\xed\xfe-mac-shell")
    return {"shell/windows": win, "shell/macos": mac}


def test_a_payload_with_shells_ships_a_second_archive_and_leaves_the_first_untouched(staged, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    public = rk.generate()
    monkeypatch.setattr(pr, "baked_public_key", lambda: public)
    h, u = staged
    out = tmp_path / "out"
    manifest = pr.build_the_payload(h, u, out, version="1.2.3", extras=_shell_dirs(tmp_path))

    doc = json.loads((out / "manifest.json").read_bytes())
    # 老客户端读的那几个键一个字都没变
    assert doc["unit"] == ["harness", "static_ui"] and doc["schema"] == 1
    with tarfile.open(out / "payload-1.2.3.tar.gz") as tar:
        assert {n.split("/", 1)[0] for n in tar.getnames()} == {"harness", "static_ui"}, "壳混进第一个归档了 —— 老客户端会拒收"
    # 新键
    ex = doc["extras"]
    assert ex["archive"] == "extras-1.2.3.tar.gz" and (out / ex["archive"]).is_file()
    assert ex["units"] == ["shell/macos", "shell/windows"]
    assert ex["sha256"] == hashlib.sha256((out / ex["archive"]).read_bytes()).hexdigest()
    assert ex["size"] == (out / ex["archive"]).stat().st_size
    assert ex["shell"]["windows"] == hashlib.sha256(b"MZ-windows-shell").hexdigest()
    assert ex["shell"]["macos"] == hashlib.sha256(b"\xcf\xfa\xed\xfe-mac-shell").hexdigest()
    with tarfile.open(out / ex["archive"]) as tar:
        names = tar.getnames()
    assert "shell/windows/ScienceMate.exe" in names and "shell/macos/ScienceMate" in names
    assert manifest["_signed"] is True, "带 extras 的 manifest 也得签得过、自验得过"


def test_no_extras_means_no_second_archive_and_no_key(staged, tmp_path, monkeypatch) -> None:
    """不给 extras 就和从前一模一样 —— 这是老格式的定义。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    public = rk.generate()
    monkeypatch.setattr(pr, "baked_public_key", lambda: public)
    h, u = staged
    pr.build_the_payload(h, u, tmp_path / "out", version="1.0.0")
    doc = json.loads((tmp_path / "out" / "manifest.json").read_bytes())
    assert "extras" not in doc
    assert not list((tmp_path / "out").glob("extras-*"))


def test_an_extra_outside_the_allowed_tops_is_refused_at_build_time(staged, tmp_path, monkeypatch) -> None:
    """发布端就拦：顶层不在 EXTRA_UNITS 里的东西，客户端那一侧也一定拒收。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    h, u = staged
    bad = tmp_path / "runtime"; bad.mkdir(); (bad / "python.exe").write_bytes(b"x")
    with pytest.raises(SystemExit, match="顶层"):
        pr.build_the_payload(h, u, tmp_path / "out", version="1.0.0", extras={"runtime": bad})


# ───────────────────────────────────────────── 客户端：认可选键

def _extras_blob(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _extras_doc(blob: bytes, shell_bytes: bytes = b"MZ-shell", **override) -> dict:
    doc = {"archive": "extras-1.0.0.tar.gz", "sha256": hashlib.sha256(blob).hexdigest(),
           "size": len(blob), "units": ["shell/windows"], "files": {"shell/windows": 1},
           "shell": {"windows": hashlib.sha256(shell_bytes).hexdigest()}}
    doc.update(override)
    return doc


def test_an_old_manifest_without_extras_still_parses() -> None:
    """兼容的另一半：新客户端收老格式照样过 —— 只发 Mac 不发壳的版本会长这样。"""
    key, public = _keypair()
    doc = _manifest("1.0.0", _tar("1.0.0"))
    assert "extras" not in su.parse_manifest(doc, _sign(key, doc), public)


def test_a_manifest_with_valid_extras_parses() -> None:
    key, public = _keypair()
    blob = _extras_blob({"shell/windows/ScienceMate.exe": b"MZ-shell"})
    doc = _manifest("1.0.0", _tar("1.0.0"), extras=_extras_doc(blob))
    assert su.parse_manifest(doc, _sign(key, doc), public)["extras"]["shell"]["windows"]


@pytest.mark.parametrize("bad", [
    {"sha256": "abc"},
    {"size": 0},
    {"archive": "../extras.tar.gz"},
    {"units": []},
    {"units": ["runtime"]},
    {"shell": {"linux": "a" * 64}},
    {"shell": {"windows": "nothex"}},
    "not-an-object",
])
def test_malformed_extras_reject_the_whole_manifest(bad) -> None:
    key, public = _keypair()
    blob = _extras_blob({"shell/windows/ScienceMate.exe": b"MZ-shell"})
    extras = bad if isinstance(bad, str) else _extras_doc(blob, **bad)
    doc = _manifest("1.0.0", _tar("1.0.0"), extras=extras)
    with pytest.raises(su.UpdateError, match="extras"):
        su.parse_manifest(doc, _sign(key, doc), public)


# ───────────────────────────────────────────── 客户端：两个归档要么都到位要么都不算

def _both(tmp_path: Path, shell_bytes: bytes = b"MZ-shell", *, corrupt_extras: bool = False):
    main_blob = _tar("1.0.0")
    extras_blob = _extras_blob({"shell/windows/ScienceMate.exe": shell_bytes,
                                "shell/windows/WebView2Loader.dll": b"dll"})
    manifest = json.loads(_manifest("1.0.0", main_blob, extras=_extras_doc(extras_blob, shell_bytes)))
    streams = {"https://h/payload-1.0.0.tar.gz": main_blob,
               "https://h/extras-1.0.0.tar.gz": (extras_blob[:-3] + b"xxx") if corrupt_extras else extras_blob}

    def _stream(url: str, sink) -> None:
        blob = streams[url]
        for i in range(0, len(blob), 7):
            sink(blob[i:i + 7])
    return manifest, _stream


def test_extras_are_staged_next_to_the_payload(tmp_path: Path) -> None:
    manifest, stream = _both(tmp_path)
    staged = su.download_and_stage(tmp_path, manifest, "https://h/payload-1.0.0.tar.gz", stream,
                                   extras_url="https://h/extras-1.0.0.tar.gz")
    assert (staged / "harness" / "core" / "agent_loop.py").is_file()
    assert (staged / "extras" / "shell" / "windows" / "ScienceMate.exe").read_bytes() == b"MZ-shell"
    assert not list((tmp_path / "updates").glob("*.tar.gz")), "归档下完没清"


def test_a_bad_extras_archive_voids_the_whole_staging(tmp_path: Path) -> None:
    """主载荷已经解好、附加归档坏了 → 整个暂存作废。半份更新（新 harness + 旧壳）正是要消灭的。"""
    manifest, stream = _both(tmp_path, corrupt_extras=True)
    with pytest.raises(su.UpdateError, match="附加归档"):
        su.download_and_stage(tmp_path, manifest, "https://h/payload-1.0.0.tar.gz", stream,
                              extras_url="https://h/extras-1.0.0.tar.gz")
    assert not (su.payload_root(tmp_path) / "staged").exists(), "主载荷单独留下了 —— 半份更新"
    assert su.staged_version(tmp_path) is None


def test_a_shell_whose_hash_disagrees_with_the_manifest_is_refused(tmp_path: Path) -> None:
    """归档整体哈希对、壳单独的哈希不对 —— 说明 manifest.extras.shell 在撒谎，也拒。"""
    manifest, stream = _both(tmp_path)
    manifest["extras"]["shell"]["windows"] = "0" * 64
    with pytest.raises(su.UpdateError, match="壳"):
        su.download_and_stage(tmp_path, manifest, "https://h/payload-1.0.0.tar.gz", stream,
                              extras_url="https://h/extras-1.0.0.tar.gz")
    assert not (su.payload_root(tmp_path) / "staged").exists()


def test_a_stray_top_in_the_extras_archive_is_refused_during_staging(tmp_path: Path) -> None:
    """接线：暂存那条路上真的过了成员检查。

    变异逼出来的：把 `_reject_unsafe_members(tar, EXTRA_UNITS)` 从 `download_and_stage` 里删掉，
    原来的 22 条全绿 —— 成员检查只被直接测过，没被从暂存这条路上测过。这条喂一个 sha 对得上
    manifest、但顶层多了 `runtime/` 的附加归档：签名、摘要都过，**只有**成员检查能拦。
    """
    main_blob = _tar("1.0.0")
    stray = _extras_blob({"shell/windows/ScienceMate.exe": b"MZ-shell", "runtime/python.exe": b"x"})
    manifest = json.loads(_manifest("1.0.0", main_blob, extras=_extras_doc(stray)))
    streams = {"https://h/payload-1.0.0.tar.gz": main_blob, "https://h/extras-1.0.0.tar.gz": stray}

    def _stream(url: str, sink) -> None:
        sink(streams[url])

    with pytest.raises(su.UpdateError, match="附加归档顶层"):
        su.download_and_stage(tmp_path, manifest, "https://h/payload-1.0.0.tar.gz", _stream,
                              extras_url="https://h/extras-1.0.0.tar.gz")
    assert not (su.payload_root(tmp_path) / "staged").exists(), "带私货的附加归档进了暂存"


def test_without_an_extras_url_the_old_path_is_unchanged(tmp_path: Path) -> None:
    manifest, stream = _both(tmp_path)
    staged = su.download_and_stage(tmp_path, manifest, "https://h/payload-1.0.0.tar.gz", stream)
    assert (staged / "harness").is_dir() and not (staged / "extras").exists()


def test_extras_tops_are_a_subset_but_the_main_archive_must_match_exactly(tmp_path: Path) -> None:
    only_shell = _extras_blob({"shell/macos/ScienceMate": b"x"})
    with tarfile.open(fileobj=io.BytesIO(only_shell), mode="r:gz") as tar:
        su._reject_unsafe_members(tar, su.EXTRA_UNITS)          # 子集即可
    stray = _extras_blob({"runtime/python": b"x"})
    with tarfile.open(fileobj=io.BytesIO(stray), mode="r:gz") as tar:
        with pytest.raises(su.UpdateError, match="附加归档"):
            su._reject_unsafe_members(tar, su.EXTRA_UNITS)
    with tarfile.open(fileobj=io.BytesIO(only_shell), mode="r:gz") as tar:
        with pytest.raises(su.UpdateError, match="更新单元"):
            su._reject_unsafe_members(tar)                       # 主归档：必须正好是 UNIT


# ───────────────────────────────────────────── 接线

def test_the_install_endpoint_hands_over_the_extras_url() -> None:
    """写了没接线等于没有：判据落在真调用上（AST）。"""
    source = (REPO / "platform/backend/app/api/v1/update.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "install_update")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("download_and_stage")]
    assert calls, "install 没调 download_and_stage"
    assert any(k.arg == "extras_url" for k in calls[0].keywords), "install 没把 extras_url 递给 download_and_stage"
    assert "manifest['extras']['archive']" in ast.unparse(fn), "extras_url 不是从 manifest.extras.archive 算的"


def test_the_windows_packager_exports_the_shell_after_building_it() -> None:
    src = (REPO / "scripts/package/build_windows_app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    main = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"))
    assert "export_the_shell_for_the_payload(app)" in main, "Windows 打包器没把壳导出来 —— Mac 装配时无壳可收"
    assert main.index("build_the_shell(app)") < main.index("export_the_shell_for_the_payload(app)")
    fn = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "export_the_shell_for_the_payload"))
    assert "SHA256SUMS" in fn, "导出的壳目录没带核对单 —— 传坏了 Mac 那边看不出来"


def test_the_exported_checksums_are_lf_only_and_the_mac_side_tolerates_crlf_anyway(tmp_path, monkeypatch) -> None:
    """真机第一次传到 Mac 就撞上的：Windows 上 `write_text` 把 \\n 换成 \\r\\n，`shasum -c`
    四个文件全「找不到」（名字尾巴挂着 \\r）。两道防线各守各的：导出端写 LF；装配端就算收到
    CRLF 也不被骗成「缺文件」。
    """
    # ① 导出端：写核对单那一句必须带 newline="\\n"（AST，扫的是那个调用不是措辞）
    src = (REPO / "scripts/package/build_windows_app.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "export_the_shell_for_the_payload")
    writes = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("write_text")]
    assert writes and all(any(k.arg == "newline" and ast.unparse(k.value) == "'\\n'" for k in w.keywords) for w in writes), (
        "SHA256SUMS 没按 LF 写 —— Windows 上会写成 CRLF，Mac 那边一个文件都找不到")

    # ② 装配端：收到 CRLF 的核对单也认
    mac = _by_path("build_mac_app")
    monkeypatch.setattr(mac, "DIST", tmp_path / "dist")
    app = tmp_path / "ScienceMate.app"; (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / mac.BINARY_NAME).write_bytes(b"mac-shell")
    win = tmp_path / "shell-windows"; win.mkdir()
    (win / "ScienceMate.exe").write_bytes(b"MZ")
    digest = hashlib.sha256(b"MZ").hexdigest()
    (win / "SHA256SUMS").write_bytes(f"{digest}  ScienceMate.exe\r\n".encode())
    extras = mac.the_extras_for_the_payload(app, str(win))
    assert (extras["shell/windows"] / "ScienceMate.exe").read_bytes() == b"MZ"
    assert not (extras["shell/windows"] / "SHA256SUMS").exists(), "核对单混进了归档目录"
    assert (extras["shell/macos"] / mac.BINARY_NAME).read_bytes() == b"mac-shell"


def test_the_mac_packager_builds_the_shell_before_the_payload_and_passes_extras() -> None:
    src = (REPO / "scripts/package/build_mac_app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    main = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"))
    assert main.index("build_the_shell(app)") < main.index("build_the_payload("), "壳在载荷之后才编 —— 载荷里带不上它"
    assert main.count("build_the_shell(app)") == 1, "壳编了两遍"
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("build_the_payload"))
    assert any(k.arg == "extras" for k in call.keywords), "打载荷时没把壳递进去"
    assert "--windows-shell-dir" in src
