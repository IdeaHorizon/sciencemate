"""自更新：换的是载荷，落在数据根，签名不对整份拒收，切换只在启动时做。

每条判据都对着一种真实的坏法：签名被换 / 字节被改 / tar 里藏 `..` / 版本标记
与 manifest 不一致 / 暂存不完整 / 指针悬空 / 源码 checkout 被推更新 / 后端用错
误的退出码重启。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import tarfile
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import self_update as su
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401  fixture + 真登录

REPO = Path(__file__).resolve().parents[3]


def _keypair():
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return key, public


def _sign(key, data: bytes) -> str:
    return base64.b64encode(key.sign(data)).decode()


def _tar(version: str, *, marker: str | None = None, extra: dict[str, bytes] | None = None,
         symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    files = {
        "harness/core/agent_loop.py": b"# loop\n",
        f"harness/{su.VERSION_MARKER}": ((marker if marker is not None else version) + "\n").encode(),
        "static_ui/index.html": b"<html>v" + version.encode() + b"</html>",
    }
    files.update(extra or {})
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buffer.getvalue()


def _manifest(version: str, blob: bytes, **override) -> bytes:
    doc = {"schema": 1, "version": version, "archive": f"payload-{version}.tar.gz",
           "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob),
           "unit": list(su.UNIT), "notes": "n"}
    doc.update(override)
    return json.dumps(doc, sort_keys=True).encode()


def _stream_of(blob: bytes):
    def _stream(_url: str, sink) -> None:
        for i in range(0, len(blob), 7):
            sink(blob[i:i + 7])
    return _stream


# ───────────────────────────────────────────── 信任

def test_the_wire_format_matches_the_release_script(tmp_path, monkeypatch) -> None:
    """发布脚本签的，客户端验得过 —— 两边各自实现同一种线上格式，这条钉住它们没分叉。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("release_keys", REPO / "scripts" / "package" / "release_keys.py")
    rk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rk)
    public = rk.generate()
    data = b'{"schema": 1}'
    signature = rk.sign(data, rk.load_private())

    assert su.verify_signature(data, signature, public)
    assert not su.verify_signature(data + b" ", signature, public), "改一个字节还验得过"
    assert not su.verify_signature(data, signature, _keypair()[1]), "换一把公钥还验得过"


def test_a_tampered_manifest_is_rejected() -> None:
    key, public = _keypair()
    blob = _tar("1.0.0")
    doc = _manifest("1.0.0", blob)
    signature = _sign(key, doc)
    assert su.parse_manifest(doc, signature, public)["version"] == "1.0.0"

    forged = doc.replace(b'"version": "1.0.0"', b'"version": "9.9.9"')
    with pytest.raises(su.UpdateError, match="签名"):
        su.parse_manifest(forged, signature, public)


def test_a_manifest_signed_by_someone_else_is_rejected() -> None:
    other, _ = _keypair()
    _, public = _keypair()
    doc = _manifest("1.0.0", _tar("1.0.0"))
    with pytest.raises(su.UpdateError, match="签名"):
        su.parse_manifest(doc, _sign(other, doc), public)


@pytest.mark.parametrize("bad", [
    {"unit": ["harness"]},
    {"sha256": "abc"},
    {"size": 0},
    {"size": su.MAX_ARCHIVE_BYTES + 1},
    {"archive": "../x.tar.gz"},
    {"schema": 2},
])
def test_a_manifest_with_an_illegal_field_is_rejected(bad) -> None:
    """签名对、内容不合法 —— 也拒。签名只证明是谁发的，不证明它说得对。"""
    key, public = _keypair()
    doc = _manifest("1.0.0", _tar("1.0.0"), **bad)
    with pytest.raises(su.UpdateError):
        su.parse_manifest(doc, _sign(key, doc), public)


# ───────────────────────────────────────────── 下载 + 暂存

def _staged_ok(root: Path, version: str) -> bool:
    return su.staged_version(root) == version and (
        root / "payload" / "staged" / version / "harness" / su.VERSION_MARKER).is_file()


def test_a_good_payload_is_staged_and_the_archive_is_gone(tmp_path) -> None:
    blob = _tar("1.0.0")
    manifest = json.loads(_manifest("1.0.0", blob))
    staged = su.download_and_stage(tmp_path, manifest, "http://x/p.tar.gz", _stream_of(blob))

    assert _staged_ok(tmp_path, "1.0.0")
    assert staged == tmp_path / "payload" / "staged" / "1.0.0"
    assert not list((tmp_path / "updates").iterdir()), "下载的归档没清掉"
    assert not (tmp_path / "payload" / "staging").exists()


def test_a_download_whose_bytes_differ_stages_nothing(tmp_path) -> None:
    """manifest 签了名说 sha256 是 X，下来的不是 X —— 不管多接近，整份拒。"""
    blob = _tar("1.0.0")
    manifest = json.loads(_manifest("1.0.0", blob))
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])

    with pytest.raises(su.UpdateError, match="sha256"):
        su.download_and_stage(tmp_path, manifest, "http://x", _stream_of(tampered))
    assert su.staged_version(tmp_path) is None
    assert not (tmp_path / "payload" / "staged").exists()
    assert not list((tmp_path / "updates").iterdir())


def test_a_download_that_overruns_its_declared_size_is_cut_off(tmp_path) -> None:
    blob = _tar("1.0.0")
    manifest = json.loads(_manifest("1.0.0", blob, size=10))
    with pytest.raises(su.UpdateError, match="超过"):
        su.download_and_stage(tmp_path, manifest, "http://x", _stream_of(blob))


def test_an_archive_that_escapes_its_directory_is_rejected(tmp_path) -> None:
    blob = _tar("1.0.0", extra={"harness/../../evil.py": b"x"})
    manifest = json.loads(_manifest("1.0.0", blob))
    with pytest.raises(su.UpdateError, match="越界|顶层"):
        su.download_and_stage(tmp_path, manifest, "http://x", _stream_of(blob))
    assert not (tmp_path / "evil.py").exists()
    assert su.staged_version(tmp_path) is None


def test_an_archive_with_a_symlink_is_rejected(tmp_path) -> None:
    blob = _tar("1.0.0", symlink="harness/link")
    manifest = json.loads(_manifest("1.0.0", blob))
    with pytest.raises(su.UpdateError, match="链接"):
        su.download_and_stage(tmp_path, manifest, "http://x", _stream_of(blob))


def test_an_archive_with_a_stray_top_level_dir_is_rejected(tmp_path) -> None:
    blob = _tar("1.0.0", extra={"app/launcher.py": b"x"})
    manifest = json.loads(_manifest("1.0.0", blob))
    with pytest.raises(su.UpdateError, match="顶层"):
        su.download_and_stage(tmp_path, manifest, "http://x", _stream_of(blob))


def test_a_marker_that_disagrees_with_the_manifest_is_rejected(tmp_path) -> None:
    blob = _tar("1.0.0", marker="2.0.0")
    manifest = json.loads(_manifest("1.0.0", blob))
    with pytest.raises(su.UpdateError, match="版本标记"):
        su.download_and_stage(tmp_path, manifest, "http://x", _stream_of(blob))


# ───────────────────────────────────────────── 启动时切换

def _stage(root: Path, version: str) -> None:
    blob = _tar(version)
    su.download_and_stage(root, json.loads(_manifest(version, blob)), "http://x", _stream_of(blob))


def test_apply_at_launch_switches_the_pointer_and_keeps_the_previous_version(tmp_path) -> None:
    _stage(tmp_path, "1.0.0")
    assert su.apply_staged_at_launch(tmp_path) == "1.0.0"
    assert su.read_pointer(tmp_path) == su.Pointer("1.0.0", None, su.read_pointer(tmp_path).applied_at)
    assert su.active_payload_dir(tmp_path) == tmp_path / "payload" / "1.0.0"
    assert su.staged_version(tmp_path) is None, "切换完暂存指针还在 —— 下次启动会再切一遍"

    _stage(tmp_path, "1.1.0")
    assert su.apply_staged_at_launch(tmp_path) == "1.1.0"
    pointer = su.read_pointer(tmp_path)
    assert (pointer.version, pointer.previous) == ("1.1.0", "1.0.0")
    assert (tmp_path / "payload" / "1.0.0").is_dir(), "上一版被删了 —— 回滚无路"

    _stage(tmp_path, "1.2.0")
    su.apply_staged_at_launch(tmp_path)
    assert not (tmp_path / "payload" / "1.0.0").exists(), "只留当前 + 上一版，再早的要清"
    assert (tmp_path / "payload" / "1.1.0").is_dir()


def test_apply_is_a_noop_without_anything_staged(tmp_path) -> None:
    assert su.apply_staged_at_launch(tmp_path) is None
    assert su.read_pointer(tmp_path) is None


def test_a_corrupt_stage_is_not_applied_and_the_error_is_recorded(tmp_path) -> None:
    """暂存不完整 → 不切、不抛、留一条错误给 GET /update。launcher 不能因此起不来。"""
    _stage(tmp_path, "1.0.0")
    (tmp_path / "payload" / "staged" / "1.0.0" / "harness" / "core" / "agent_loop.py").unlink()

    assert su.apply_staged_at_launch(tmp_path) is None
    assert su.read_pointer(tmp_path) is None
    assert "不完整" in su.apply_error_path(tmp_path).read_text(encoding="utf-8")


def test_a_dangling_pointer_is_ignored(tmp_path) -> None:
    """指针指着一个不存在/不完整的目录 → 当没有，回落随包那份。"""
    su._write_json_atomically(su.pointer_path(tmp_path), {"version": "7.7.7"})
    assert su.active_payload_dir(tmp_path) is None


def test_rollback_points_at_the_previous_version(tmp_path) -> None:
    _stage(tmp_path, "1.0.0"); su.apply_staged_at_launch(tmp_path)
    _stage(tmp_path, "1.1.0"); su.apply_staged_at_launch(tmp_path)
    assert su.rollback(tmp_path) == "1.0.0"
    assert su.active_payload_dir(tmp_path) == tmp_path / "payload" / "1.0.0"


# ───────────────────────────────────────────── 谁能更新、版本、来源

def test_a_git_checkout_is_never_self_updatable(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    assert not su.is_self_updatable(tmp_path), "有 .git 的 checkout 被判成可自更新"
    bundled = tmp_path / "bundled"; bundled.mkdir()
    assert su.is_self_updatable(bundled), "没有 .git 的随包 harness 该可自更新"
    assert su.is_self_updatable(None) is False


@pytest.mark.parametrize("newer,older", [("0.3.1", "0.3.0"), ("0.10.0", "0.9.9"), ("1.0.0", "0.99.99"), ("0.3.1-rc1", "0.3.0")])
def test_version_ordering(newer, older) -> None:
    assert su.is_newer(newer, older)
    assert not su.is_newer(older, newer)
    assert not su.is_newer(newer, newer)


def test_an_install_without_a_marker_accepts_any_update() -> None:
    """打这套之前装的应用没有 PAYLOAD_VERSION —— 它就是该更新的那种。"""
    assert su.is_newer("0.0.1", None)


def test_a_direct_source_is_three_urls_in_one_directory(monkeypatch) -> None:
    key, public = _keypair()
    monkeypatch.setattr(su, "RELEASE_PUBLIC_KEY_B64", public)
    doc = _manifest("1", b"x"); table = {"https://h/x/manifest.json": doc,
                                         "https://h/x/manifest.json.sig": _sign(key, doc).encode()}
    found = su.find_the_newest_installable("https://h/x/manifest.json", lambda _u: None, table.__getitem__,
                                           su.what_a_desktop_installs)
    assert found.source.manifest_url == "https://h/x/manifest.json"
    assert found.source.signature_url == "https://h/x/manifest.json.sig"
    assert found.source.archive_url_for("payload-1.tar.gz", found.assets) == "https://h/x/payload-1.tar.gz"


def test_a_release_source_reads_the_release_assets(monkeypatch) -> None:
    key, public = _keypair()
    monkeypatch.setattr(su, "RELEASE_PUBLIC_KEY_B64", public)
    doc = _manifest("1", b"x"); table = {"https://h/dl/manifest.json": doc,
                                         "https://h/dl/manifest.json.sig": _sign(key, doc).encode()}
    seen = []

    def _get(url):
        seen.append(url)
        return [{"tag_name": "v1", "assets": [
            {"name": "manifest.json", "browser_download_url": "https://h/dl/manifest.json"},
            {"name": "manifest.json.sig", "browser_download_url": "https://h/dl/manifest.json.sig"},
            {"name": "payload-1.tar.gz", "browser_download_url": "https://h/dl/payload-1.tar.gz"},
        ]}]

    found = su.find_the_newest_installable("https://forge.example/wangd/harness-framework", _get,
                                           table.__getitem__, su.what_a_desktop_installs)
    assert seen == ["https://forge.example/api/v1/repos/wangd/harness-framework/releases"
                    f"?limit={su.LOOK_BACK}&draft=false&pre-release=false"]
    assert found.source.manifest_url == "https://h/dl/manifest.json"
    assert found.source.archive_url_for("payload-1.tar.gz", found.assets) == "https://h/dl/payload-1.tar.gz"


def test_a_github_repository_is_listed_through_its_own_api_host(monkeypatch) -> None:
    """个人版 0.5.6 起从 GitHub 发：仓库网址在 github.com，API 在 api.github.com，
    只认 per_page。Forgejo 那条路不变（上一条测试）。"""
    key, public = _keypair()
    monkeypatch.setattr(su, "RELEASE_PUBLIC_KEY_B64", public)
    doc = _manifest("1", b"x")
    seen: list[str] = []
    table = {"https://gh/dl/manifest.json": doc, "https://gh/dl/manifest.json.sig": _sign(key, doc).encode()}

    def _get(url):
        seen.append(url)
        return [{"tag_name": "v0.5.6", "html_url": "https://github.com/IdeaHorizon/sciencemate/releases/tag/v0.5.6",
                 "assets": [
            {"name": "manifest.json", "browser_download_url": "https://gh/dl/manifest.json"},
            {"name": "manifest.json.sig", "browser_download_url": "https://gh/dl/manifest.json.sig"},
            {"name": "payload-1.tar.gz", "browser_download_url": "https://gh/dl/payload-1.tar.gz"},
        ]}]

    found = su.find_the_newest_installable("https://github.com/IdeaHorizon/sciencemate", _get,
                                           table.__getitem__, su.what_a_desktop_installs)
    assert seen == [f"https://api.github.com/repos/IdeaHorizon/sciencemate/releases?per_page={su.LOOK_BACK}"]
    assert found.tag == "v0.5.6"
    assert found.source.page_url == "https://github.com/IdeaHorizon/sciencemate/releases/tag/v0.5.6"
    assert found.source.archive_url_for("payload-1.tar.gz", found.assets) == "https://gh/dl/payload-1.tar.gz"


def test_a_release_without_a_signature_asset_is_not_installable() -> None:
    with pytest.raises(su.NothingPublishedYet, match="manifest.json.sig"):
        su.find_the_newest_installable("https://h/o/r", lambda _u: [{"tag_name": "v1", "assets": [
            {"name": "manifest.json", "browser_download_url": "u"}]}], lambda _u: b"", su.what_a_desktop_installs)


def test_an_unreachable_source_is_reported_not_raised(tmp_path) -> None:
    harness = tmp_path / "h"; (harness / "core").mkdir(parents=True); (harness / "core" / "agent_loop.py").write_text("")

    def _boom(_u):
        raise ConnectionError("no route")

    status, manifest, *_ = su.check_for_update(tmp_path, harness, "https://h/x/manifest.json", _boom, _boom)
    assert manifest is None and status.reachable is False
    assert "拿不到更新信息" in (status.error or "")
    assert status.available_version is None


def test_check_offers_only_a_newer_signed_version(tmp_path, monkeypatch) -> None:
    key, public = _keypair()
    monkeypatch.setattr(su, "RELEASE_PUBLIC_KEY_B64", public)
    harness = tmp_path / "h"; (harness / "core").mkdir(parents=True)
    (harness / "core" / "agent_loop.py").write_text(""); (harness / su.VERSION_MARKER).write_text("1.0.0\n")
    blob = _tar("1.1.0"); doc = _manifest("1.1.0", blob); sig = _sign(key, doc)
    table = {"https://h/x/manifest.json": doc, "https://h/x/manifest.json.sig": sig.encode()}

    status, manifest, source, _ = su.check_for_update(tmp_path, harness, "https://h/x/manifest.json",
                                                      lambda _u: None, lambda u: table[u])
    assert status.reachable and status.available_version == "1.1.0" and manifest["version"] == "1.1.0"

    (harness / su.VERSION_MARKER).write_text("1.1.0\n")
    status, *_ = su.check_for_update(tmp_path, harness, "https://h/x/manifest.json", lambda _u: None, lambda u: table[u])
    assert status.available_version is None, "已经是这一版了还在推"


def test_schedule_restart_exits_with_the_relaunch_code() -> None:
    """壳只认一个数：3。用别的码退出，壳会把它当崩溃报给用户。"""
    got: list[int] = []
    done = threading.Event()

    def _exit(code: int) -> None:
        got.append(code); done.set()

    su.schedule_restart(delay_s=0.01, _exit=_exit)
    assert done.wait(2) and got == [su.RESTART_EXIT_CODE] == [3]


# ───────────────────────────────────────────── launcher 接线

def test_the_launcher_prefers_the_applied_payload_over_the_bundle(tmp_path, monkeypatch) -> None:
    """判据落在 launcher 真正的查找函数上 —— 机制存在但没接到路径是这仓库的常见死法。"""
    from app import launcher

    _stage(tmp_path, "2.0.0"); su.apply_staged_at_launch(tmp_path)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.delenv("STATIC_UI_ROOT", raising=False)

    assert launcher.find_the_harness() == tmp_path / "payload" / "2.0.0" / "harness"
    assert launcher.find_static_ui() == tmp_path / "payload" / "2.0.0" / "static_ui"

    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path / "nowhere"))
    assert launcher.find_the_harness() is None, "显式指定必须仍然最优先（即使它指错了）"


def test_the_finder_looks_where_the_packager_stages_the_harness() -> None:
    """「随包那份在哪」的找处，必须等于打包器的放处。

    这条闸是变异逼出来的：上面四条测试都把 `_the_bundled_harness` 替身掉了（不能
    往仓库的 app 目录里写东西），于是**那个函数指哪儿一个判据都没有** —— 把它改成
    指向 `nowhere`，四条全绿。同 git / tectonic 那两条「放处＝找处」对齐闸。
    """
    from pathlib import Path as _Path

    from app import launcher

    found = launcher._the_bundled_harness()
    assert found == _Path(launcher.__file__).resolve().parent / "harness"

    packager = (_Path(launcher.__file__).resolve().parents[3]
                / "scripts" / "package" / "build_windows_app.py").read_text(encoding="utf-8")
    assert 'staged_harness = BACKEND / "app" / "harness"' in packager, (
        "打包器把 harness 放到了别处，而启动器仍按 app/harness 去找 —— "
        "装是装进去了、它去别处找，两边都不报错")


def _bundled(tmp_path, version: str | None):
    """造一份「随包分发的 harness」。version=None ＝ 没有版本标记（老安装）。"""
    root = tmp_path / "app-bundle" / "harness"
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    if version is not None:
        (root / su.VERSION_MARKER).write_text(version + "\n", encoding="utf-8")
    return root


def test_a_reinstall_wins_over_a_payload_of_the_same_version(tmp_path, monkeypatch) -> None:
    """自更新过之后重装 —— 新装的那份必须真的生效。

    2026-09-10 真机：机器上先自更新到 0.4.4（数据根留下 `payload/current.json`），
    随后装了一个新打的包并起来跑 —— 应用照样加载数据根里那份旧载荷，**新包里的
    代码从头到尾没进过场**，而我差点把「修复没生效」当成「修复不管用」。

    这条闸把问题问对：不是「有没有自更新过」，而是**「哪一份更新」**。

    这里只把「随包那份在哪」替身掉（一个返回路径的纯函数，测试不能往仓库的 app
    目录里写东西）；被测的比较逻辑和接线都是真的。
    """
    from app import launcher

    _stage(tmp_path, "1.0.0"); su.apply_staged_at_launch(tmp_path)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.delenv("STATIC_UI_ROOT", raising=False)
    bundled = _bundled(tmp_path, "1.0.0")
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: bundled)

    assert launcher.find_the_harness() == bundled, (
        "载荷和随包一样新，却还在用载荷 —— 重装等于没装")


def test_an_older_payload_never_shadows_a_newer_bundle(tmp_path, monkeypatch) -> None:
    from app import launcher

    _stage(tmp_path, "1.0.0"); su.apply_staged_at_launch(tmp_path)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.delenv("STATIC_UI_ROOT", raising=False)
    bundled = _bundled(tmp_path, "2.0.0")
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: bundled)

    assert launcher.find_the_harness() == bundled
    assert launcher.find_static_ui() != tmp_path / "payload" / "1.0.0" / "static_ui", (
        "界面也不许被旧载荷遮住 —— 两处走同一个判断，就该一起成立")


def test_a_newer_payload_still_wins(tmp_path, monkeypatch) -> None:
    """自更新本身不许被这条闸误伤。"""
    from app import launcher

    _stage(tmp_path, "2.0.0"); su.apply_staged_at_launch(tmp_path)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.delenv("STATIC_UI_ROOT", raising=False)
    bundled = _bundled(tmp_path, "1.0.0")
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: bundled)

    assert launcher.find_the_harness() == tmp_path / "payload" / "2.0.0" / "harness"
    assert launcher.find_static_ui() == tmp_path / "payload" / "2.0.0" / "static_ui"


def test_a_bundle_without_a_version_marker_yields_to_any_payload(tmp_path, monkeypatch) -> None:
    """这套机制之前装的包没有版本标记 —— 那时候任何载荷都算新，这是对的。"""
    from app import launcher

    _stage(tmp_path, "0.0.1"); su.apply_staged_at_launch(tmp_path)
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.delenv("STATIC_UI_ROOT", raising=False)
    bundled = _bundled(tmp_path, None)
    monkeypatch.setattr(launcher, "_the_bundled_harness", lambda: bundled)

    assert launcher.find_the_harness() == tmp_path / "payload" / "0.0.1" / "harness"


def test_startup_applies_a_staged_update_before_it_prepares_the_environment(tmp_path, monkeypatch) -> None:
    """main() 的顺序：切换暂存 → 接管载荷里的 app/（#953 ⑤）→ 备环境。切换搬到了接管之前
    （装完更新的第一次重启要能接到新后端），备环境看见的仍是切换后的那一版。"""
    from app import launcher

    _stage(tmp_path, "3.0.0")
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    for name in ("HARNESS_ROOT", "STATIC_UI_ROOT", "HARNESS_BRIDGE_ENABLED"):
        monkeypatch.delenv(name, raising=False)

    launcher.switch_to_the_staged_update()
    _ui, harness = launcher.prepare_the_environment()

    assert su.read_pointer(tmp_path).version == "3.0.0", "启动没有切换暂存的更新"
    assert harness == tmp_path / "payload" / "3.0.0" / "harness"
    assert os.environ["HARNESS_ROOT"] == str(harness)


# ───────────────────────────────────────────── 端点

@pytest.mark.asyncio
async def test_update_status_is_readable_and_shaped(runtime_client, monkeypatch, tmp_path) -> None:
    """三个口都在 `authenticated` 后面 —— 用 fixture 里的真用户真登录，不绕鉴权。

    第一版用 app.dependency_overrides 顶掉 get_current_user：单跑绿，全量红（401）。
    覆盖挂在哪个 app 对象上、别的测试有没有清它，都不在这条测试的视野里 —— 顺序
    敏感的判据等于没判。真登录没有这个问题。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(su, "check_for_update", lambda *a, **k: (
        su.UpdateStatus("1.0.0", "bundled", True, available_version="1.1.0", reachable=True), None, None, {}))
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")

    response = await client.get("/api/v1/update", headers=_headers(token))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["installed_version"] == "1.0.0" and payload["available_version"] == "1.1.0"
    assert set(payload) >= {"installed_from", "self_updatable", "staged_version", "reachable", "error"}


@pytest.mark.asyncio
async def test_restart_without_a_staged_update_is_refused(runtime_client, monkeypatch, tmp_path) -> None:
    """个人档：任何本机用户都能重启（researcher 也行），但没暂存就没什么可重启的。"""
    from app.config import settings

    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")

    response = await client.post("/api/v1/update/restart", headers=_headers(token))
    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_an_org_server_only_lets_admins_install(runtime_client, monkeypatch, tmp_path) -> None:
    """组织档：装更新 = 换整台服务器的 harness。researcher 不行，institution_admin 行。"""
    from app.config import settings

    monkeypatch.setattr(settings, "profile", "org")
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    client, _ = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    admin = await _token(client, "institution.admin@atrium.local")

    assert (await client.post("/api/v1/update/restart", headers=_headers(researcher))).status_code == 403
    assert (await client.post("/api/v1/update/restart", headers=_headers(admin))).status_code == 409  # 过了权限，卡在"没暂存"


def test_a_private_update_source_gets_the_token_on_every_request(monkeypatch) -> None:
    """三条 HTTP 路（manifest / 签名 / 载荷流）都得带 —— 漏一条，私有仓库上那一步就 404。

    这几条路 2026-09-23 起住在 `services/release_feed.py` 一处：桌面自更新和组织服务器
    自升级问的是同一个发布页、带同一个凭据。
    """
    import inspect

    from app.api.v1 import update as desktop_update
    from app.config import settings
    from app.services import release_feed as u

    monkeypatch.setattr(settings, "update_source_token", "alice:s3cr3t")
    assert u.auth_headers()["Authorization"].startswith("Basic ")
    monkeypatch.setattr(settings, "update_source_token", "abc123")
    assert u.auth_headers() == {"Authorization": "token abc123"}
    monkeypatch.setattr(settings, "update_source_token", "")
    assert u.auth_headers() == {}

    for fn in (u.get_json, u.get_bytes, u.stream):
        assert "headers=auth_headers()" in inspect.getsource(fn), f"{fn.__name__} 没带凭据"
    # 两个用它的地方都真的走它，而不是各自又写了一份不带凭据的。
    users = [desktop_update]
    # 组织服务器自升级是专业版的：公开树里没有它，就只验桌面这一边。按名字找，不写 import 语句
    # （导出脚本的出门检查只认 import 语句）。
    import importlib
    import importlib.util

    # 先看有没有 app.pro 这个包：find_spec 对一个不存在的父包会直接抛 ModuleNotFoundError。
    if importlib.util.find_spec("app.pro") is not None and importlib.util.find_spec("app.pro.services.server_update") is not None:
        users.append(importlib.import_module("app.pro.services.server_update"))
    for user in users:
        source = inspect.getsource(user)
        assert "release_feed.get_json" in source and "release_feed.get_bytes" in source \
            and "release_feed.stream" in source, f"{user.__name__} 没走 release_feed"
        assert "httpx.Client(" not in source, f"{user.__name__} 自己又开了一个 httpx 客户端"


def test_the_default_update_source_is_a_public_repo() -> None:
    """默认更新源指公开发布仓库。

    指私有源码仓库的后果是隐形的：release 资产跟仓库同可见性 → 没 token 就 404 →
    而"更新拉不到"在界面上什么都不显示（更新是方便不是提醒）→ 没有人会发现自己
    再也收不到更新了。
    """
    assert su.DEFAULT_UPDATE_SOURCE.endswith("/wangd/sciencemate")
    assert "harness-framework" not in su.DEFAULT_UPDATE_SOURCE, "默认更新源指着私有源码仓库"
    assert "agent-for-science" not in su.DEFAULT_UPDATE_SOURCE, "还指着改名前的发布仓库"
