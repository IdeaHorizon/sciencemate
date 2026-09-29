"""这次更新含不含壳的改动、装着的壳会不会自己换 —— 后端只拿两份事实答，不猜（#953 ③）。

两份事实：装着的壳自报的家门（`shell.json`，①）与新版 manifest 里带的壳哈希（`extras.shell`，②）。
两份都有才下判断；缺一份就是「不知道」，**不**说需要重装（假警报比不说更糟），也不说不需要。

为什么要有这一层：0.4.4 的全部价值都在壳里，装了 0.4.3 的同事点「现在更新」拿到的是新流程
代码和界面，**窗口还是没有** —— 点完发现没变。横幅得在他点之前就说清楚，并给地址。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import self_update as su
from tests.test_self_update import _keypair, _manifest, _sign, _tar

DECLARED = {"platform": "windows", "path": "C:\\x\\ScienceMate.exe", "sha256": "a" * 64,
            "size": 1, "self_replace": False}


def test_a_different_shell_that_cannot_replace_itself_needs_a_reinstall() -> None:
    manifest = {"extras": {"shell": {"windows": "b" * 64, "macos": "c" * 64}}}
    assert su.describe_the_shell_update(DECLARED, manifest) == {"changed": True, "needs_reinstall": True}


def test_a_different_shell_that_replaces_itself_does_not() -> None:
    """④ 落地后壳会自换：changed 仍是 True，但不用重装 —— 更新本身会把壳换掉。"""
    manifest = {"extras": {"shell": {"windows": "b" * 64}}}
    assert su.describe_the_shell_update({**DECLARED, "self_replace": True}, manifest) == {
        "changed": True, "needs_reinstall": False}


def test_the_same_shell_is_not_a_change() -> None:
    manifest = {"extras": {"shell": {"windows": "a" * 64}}}
    assert su.describe_the_shell_update(DECLARED, manifest) == {"changed": False, "needs_reinstall": False}


def test_missing_either_fact_means_unknown_not_reinstall() -> None:
    """判不了就说判不了：老壳不自报、或 manifest 没带这个平台的壳、或老格式 manifest。"""
    assert su.describe_the_shell_update(None, {"extras": {"shell": {"windows": "b" * 64}}}) == {
        "changed": None, "needs_reinstall": False}
    assert su.describe_the_shell_update(DECLARED, {"extras": {"shell": {"macos": "b" * 64}}}) == {
        "changed": None, "needs_reinstall": False}
    assert su.describe_the_shell_update(DECLARED, {}) == {"changed": None, "needs_reinstall": False}


def _reachable(tmp_path: Path, manifest_doc: bytes, signature: str, release_page: str | None):
    """一个可达的、发齐了的 release 来源：manifest + 签名 + 它点名的归档 + 发布页地址。"""
    named = su.what_a_desktop_installs(json.loads(manifest_doc))
    table = {
        f"https://h/api/v1/repos/o/r/releases?limit={su.LOOK_BACK}&draft=false&pre-release=false": [{
            "tag_name": "v2.0.0", "html_url": release_page,
            "assets": [{"name": "manifest.json", "browser_download_url": "https://h/m"},
                       {"name": "manifest.json.sig", "browser_download_url": "https://h/s"},
                       *({"name": n, "browser_download_url": f"https://h/{n}"} for n in named)],
        }],
        "https://h/m": manifest_doc, "https://h/s": signature.encode(),
    }
    return (lambda url: table[url]), (lambda url: table[url])


def test_the_status_carries_the_verdict_and_the_reinstall_url(tmp_path: Path, monkeypatch) -> None:
    """接线：`check_for_update` 造的状态里有判断，需要重装时还有地址（发布页的 html_url）。"""
    key, public = _keypair()
    monkeypatch.setattr(su, "RELEASE_PUBLIC_KEY_B64", public)   # 应用里烧的公钥 —— 既有测试同一写法
    blob = _tar("2.0.0")
    doc = _manifest("2.0.0", blob, extras={
        "archive": "extras-2.0.0.tar.gz", "sha256": "d" * 64, "size": 10, "units": ["shell/windows"],
        "files": {"shell/windows": 1}, "shell": {"windows": "b" * 64}})
    signature = _sign(key, doc)
    su.shell_declaration_path(tmp_path).write_text(json.dumps(DECLARED), encoding="utf-8")
    harness = tmp_path / "h"; (harness / "core").mkdir(parents=True)
    (harness / su.VERSION_MARKER).write_text("1.0.0\n")
    get_json, get_bytes = _reachable(tmp_path, doc, signature, "https://h/o/r/releases/tag/v2.0.0")

    status, *_ = su.check_for_update(tmp_path, harness, "https://h/o/r", get_json, get_bytes)
    assert status.available_version == "2.0.0", status.error
    assert status.shell_update == {"changed": True, "needs_reinstall": True}
    assert status.reinstall_url == "https://h/o/r/releases/tag/v2.0.0", "需要重装却没给地址 —— 等于没提示"
    assert status.as_dict()["shell_update"]["needs_reinstall"] is True


def test_an_old_format_manifest_yields_unknown_and_no_url(tmp_path: Path, monkeypatch) -> None:
    """老格式（没 extras）：判不了，也不给地址。"""
    key, public = _keypair()
    monkeypatch.setattr(su, "RELEASE_PUBLIC_KEY_B64", public)
    blob = _tar("2.0.0")
    doc = _manifest("2.0.0", blob); signature = _sign(key, doc)
    su.shell_declaration_path(tmp_path).write_text(json.dumps(DECLARED), encoding="utf-8")
    harness = tmp_path / "h"; (harness / "core").mkdir(parents=True)
    (harness / su.VERSION_MARKER).write_text("1.0.0\n")
    get_json, get_bytes = _reachable(tmp_path, doc, signature, "https://h/o/r/releases/tag/v2.0.0")
    status, *_ = su.check_for_update(tmp_path, harness, "https://h/o/r", get_json, get_bytes)
    assert status.available_version == "2.0.0", status.error
    assert status.shell_update == {"changed": None, "needs_reinstall": False}
    assert status.reinstall_url is None


def test_the_release_page_url_comes_from_the_source(tmp_path: Path, monkeypatch) -> None:
    """direct 来源：manifest 所在目录；release 来源：release 的 html_url。"""
    key, public = _keypair()
    monkeypatch.setattr(su, "RELEASE_PUBLIC_KEY_B64", public)
    doc = _manifest("1.0.0", b"x"); signature = _sign(key, doc)
    direct = {"https://h/afs/manifest.json": doc, "https://h/afs/manifest.json.sig": signature.encode()}
    found = su.find_the_newest_installable("https://h/afs/manifest.json", lambda _u: None, direct.__getitem__,
                                           su.what_a_desktop_installs)
    assert found.source.page_url == "https://h/afs"
    get_json, get_bytes = _reachable(tmp_path, doc, signature, "https://h/o/r/releases/tag/v1")
    found = su.find_the_newest_installable("https://h/o/r", get_json, get_bytes, su.what_a_desktop_installs)
    assert found.source.page_url == "https://h/o/r/releases/tag/v1"
