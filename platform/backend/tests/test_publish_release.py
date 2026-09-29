"""发 release：tag 从 manifest 来、每个安装脚本指向自己那一版、同名资产先删再传、传完回读比对。

安装脚本从一个（`install.sh`）变成两个（加 `install.ps1`）之后，判据一律按
`publish_release.INSTALL_SCRIPTS` 那张表走 —— 加第三个平台时不必回来补用例，
漏掉的那一个会当场转红，而不是安静地一直指向上一版。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("afs_package_publish_release", REPO / "scripts" / "package" / "publish_release.py")
pr = importlib.util.module_from_spec(spec); spec.loader.exec_module(pr)


class _FakeForgejo:
    """记录每个请求；模拟 release 不存在 → 建 → 有一个同名旧资产。"""

    def __init__(self, sums: bytes):
        self.calls = []; self.sums = sums; self.deleted = []; self.uploaded = []

    def __call__(self, method, url, body, ctype):
        self.calls.append((method, url))
        if method == "GET" and url.endswith("/releases/tags/v1.2.3"):
            return 404, b"{}"
        if method == "POST" and url.endswith("/releases"):
            return 201, json.dumps({"id": 7, "assets": [{"id": 99, "name": "install.sh"}]}).encode()
        if method == "DELETE":
            self.deleted.append(url); return 204, b""
        if method == "POST" and "/releases/7/assets" in url:
            name = url.split("name=")[1]; self.uploaded.append((name, body))
            return 201, json.dumps({"browser_download_url": f"https://h/dl/{name}"}).encode()
        if method == "GET" and url.endswith("/SHA256SUMS"):
            return 200, self.sums
        return 500, b"unexpected"


@pytest.fixture
def release_dir(tmp_path):
    d = tmp_path / "release"; d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"version": "1.2.3", "notes": "n"}))
    (d / "SHA256SUMS").write_bytes(b"abc  x.dmg\n")
    (d / "x.dmg").write_bytes(b"dmg")
    (d / "install.sh").write_text('#!/bin/sh\nDEFAULT_URL="__RELEASE_URL__"\nDEFAULT_DMG="x.dmg"\n')
    return d


def test_publish_builds_the_tag_from_the_manifest_and_points_install_sh_at_itself(release_dir) -> None:
    fake = _FakeForgejo(sums=b"abc  x.dmg\n")
    api = pr.Forgejo("https://h", "o/r", "u:t", request=fake)

    result = pr.publish(release_dir, api, "https://h", "o/r")

    assert result["tag"] == "v1.2.3" and result["base"] == "https://h/o/r/releases/download/v1.2.3"
    baked = dict(fake.uploaded)["install.sh"].decode()
    assert 'DEFAULT_URL="https://h/o/r/releases/download/v1.2.3"' in baked, "install.sh 没指向自己这一版"
    assert "__RELEASE_URL__" not in baked


def test_a_same_named_old_asset_is_deleted_before_upload(release_dir) -> None:
    """重发同一版必须幂等：旧的 install.sh 先删，否则 Forgejo 会拒绝同名或留两份。"""
    fake = _FakeForgejo(sums=b"abc  x.dmg\n")
    pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake), "https://h", "o/r")
    assert any(u.endswith("/releases/7/assets/99") for u in fake.deleted), "同名旧资产没删"
    assert sorted(n for n, _ in fake.uploaded) == ["SHA256SUMS", "install.sh", "manifest.json", "x.dmg"]


def test_a_readback_that_differs_refuses_to_call_it_published(release_dir) -> None:
    """传完回读 SHA256SUMS 不一致 → 不许说"发好了"。"""
    fake = _FakeForgejo(sums=b"something else\n")
    with pytest.raises(SystemExit, match="回读"):
        pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake), "https://h", "o/r")


@pytest.mark.parametrize("filename", sorted(pr.INSTALL_SCRIPTS))
def test_bake_refuses_a_template_with_zero_or_two_assignment_lines(filename: str) -> None:
    """每个安装脚本都得有**恰好一行**默认地址。零行＝没烧进去，两行＝烧了一半。"""
    prefix, rendered = pr.INSTALL_SCRIPTS[filename]
    with pytest.raises(SystemExit, match="恰好 1 处"):
        pr.bake_the_release_url(filename, "# nothing to see here\n", "https://h")
    twice = rendered.format(base="a") + rendered.format(base="b")
    with pytest.raises(SystemExit, match="恰好 1 处"):
        pr.bake_the_release_url(filename, twice, "https://h")


@pytest.mark.parametrize("filename", sorted(pr.INSTALL_SCRIPTS))
def test_bake_puts_the_release_url_where_that_script_reads_it(filename: str) -> None:
    """烧进去的地址得落在**这个脚本自己**读的那一行上，不是另一个平台的写法。"""
    prefix, rendered = pr.INSTALL_SCRIPTS[filename]
    baked = pr.bake_the_release_url(filename, rendered.format(base="__RELEASE_URL__"), "https://h/x")
    assert baked == rendered.format(base="https://h/x")
    assert "__RELEASE_URL__" not in baked


def test_the_upload_error_names_the_usual_forgejo_limit(release_dir) -> None:
    """4 MB 默认上限是最常撞的墙 —— 报错得把它说出来，别让人去猜 413/422 是什么。"""
    class _Rejects(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            if method == "POST" and "/assets" in url:
                return 413, b"file too large"
            return super().__call__(method, url, body, ctype)
    with pytest.raises(SystemExit, match="MAX_SIZE"):
        pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=_Rejects(b"")), "https://h", "o/r")


def test_token_forms() -> None:
    assert pr.Forgejo("https://h", "o/r", "user:secret")._auth().startswith("Basic ")
    assert pr.Forgejo("https://h", "o/r", "abcdef")._auth() == "token abcdef"


def test_the_stable_install_script_is_updated_to_the_new_version(release_dir) -> None:
    """发完新版，git 树上那个**地址永不变**的 install.sh 必须指向新版。

    不做这一步的后果没人会发现：发布页上那条命令还在、还能跑，只是一直装旧版。
    """
    class _WithContents(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            if method == "GET" and "/contents/install.sh" in url:
                return 200, json.dumps({"sha": "oldsha"}).encode()
            if method == "PUT" and url.endswith("/contents/install.sh"):
                self.put = json.loads(body); return 200, b"{}"
            return super().__call__(method, url, body, ctype)

    fake = _WithContents(sums=b"abc  x.dmg\n")
    result = pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake), "https://h", "o/r")

    import base64
    written = base64.b64decode(fake.put["content"]).decode()
    assert 'DEFAULT_URL="https://h/o/r/releases/download/v1.2.3"' in written, "推上去的是没改过的模板"
    assert fake.put["sha"] == "oldsha" and fake.put["branch"] == "main"
    assert result["stable_install_url"] == {"install.sh": "https://h/o/r/raw/branch/main/install.sh"}


def test_a_failed_stable_update_is_loud_but_does_not_undo_the_release(release_dir, capsys) -> None:
    """改不动 main 不算发布失败（资产已经好了），但必须吵 —— 沉默的后果是装旧版。"""
    fake = _FakeForgejo(sums=b"abc  x.dmg\n")     # 没有 /contents 分支 → 500
    result = pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake), "https://h", "o/r")

    assert result["tag"] == "v1.2.3", "release 本身该照常成功"
    assert result["stable_install_url"] == {"install.sh": ""}, "改不动就该是空地址，不能假装改成了"
    out = capsys.readouterr().out
    assert "仍指向上一版" in out and "手工更新" in out


def test_the_default_repo_is_the_public_one() -> None:
    """发布件必须发到**公开**仓库：资产跟仓库同可见性，发私有的等于同事下不到。"""
    assert pr.DEFAULT_REPO == "wangd/sciencemate"
    assert "harness-framework" not in pr.DEFAULT_REPO, "默认发到了私有源码仓库"


def test_a_transient_upload_failure_is_retried_not_fatal(release_dir) -> None:
    """几百 MB 的发布件，网络噎一下不该让整趟发布断在半路。

    2026-09-10 真机：245MB 的 dmg 传到一半 `The write operation timed out`，一次失败
    整个发布就停了，release 上只躺着一个 SHA256SUMS —— 看起来像「发过了」。
    与 #892 给打包器下载补的是**同一条规则**：几百 MB 的网络搬运必须有超时、重试和
    完整性判据。那边补了这边没补，同一个形状就在发布这一头又来了一次。
    """
    publish = pr

    class _FlakyOnce(_FakeForgejo):
        def __init__(self, sums):
            super().__init__(sums)
            self.attempts = {}

        def __call__(self, method, url, body, ctype):
            if method == "POST" and "/releases/7/assets" in url:
                name = url.split("name=")[1]
                self.attempts[name] = self.attempts.get(name, 0) + 1
                if self.attempts[name] == 1:          # 第一次噎住
                    return 500, b"write timed out"
            return super().__call__(method, url, body, ctype)

    fake = _FlakyOnce(b"")
    api = publish.Forgejo("https://h", "o/r", "t", request=fake)
    release = {"id": 7, "assets": []}
    payload = b"x" * 1024
    asset = publish.replace_asset(api, release, "big.bin", payload)

    assert asset is not None, "第一次失败就放弃 —— 整趟发布会断在半路"
    assert fake.attempts["big.bin"] >= 2, "没有重试"


def test_an_upload_whose_readback_size_differs_is_refused(release_dir) -> None:
    """HTTP 201 只说明它收下了这次请求，不说明落盘的是**整份**文件。

    判据落在**回读的字节数**上：Forgejo 收下多少，问它自己要。同一形状的反面教材是
    scp 还没传完就去算 SHA256SUMS（记下半截文件的哈希）——那次靠 `shasum -c` 才逮住，
    发布这一头得有自己的判据，别指望下游。
    """
    publish = pr

    class _LosesBytes(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            if method == "POST" and "/releases/7/assets" in url:
                name = url.split("name=")[1]
                self.uploaded.append((name, body))
                return 201, json.dumps({"browser_download_url": "https://h/dl/x",
                                        "size": 7}).encode()   # ← 只收下 7 字节
            return super().__call__(method, url, body, ctype)

    api = publish.Forgejo("https://h", "o/r", "t", request=_LosesBytes(b""))
    with pytest.raises(SystemExit, match="大小对不上"):
        publish.replace_asset(api, {"id": 7, "assets": []}, "big.bin", b"y" * 4096)


class _RepoThatAlreadyPublished(_FakeForgejo):
    """同上，但这个仓库已经发过某一种发行 —— 它的 manifest.json 自己写着。"""

    def __init__(self, sums, edition: str):
        super().__init__(sums)
        self.edition = edition

    def __call__(self, method, url, body, ctype):
        if method == "GET" and "/releases?limit=" in url:
            # 资产里记的对外地址故意指向一台**走不通**的主机：闸必须走这次发布用的
            # 那个 host 去读，否则它把自己变成发布的单点故障（2026-09-20 真撞上）。
            return 200, json.dumps([{
                "tag_name": "v1.0.0",
                "assets": [{"name": "manifest.json",
                            "browser_download_url": "https://unreachable.invalid/manifest.json"}],
            }]).encode()
        if method == "GET" and url.endswith("/releases/download/v1.0.0/manifest.json"):
            assert url.startswith("https://h/"), f"闸没走发布用的 host：{url}"
            return 200, json.dumps({"edition": self.edition}).encode()
        if "unreachable.invalid" in url:
            raise AssertionError("闸去敲了资产里记的对外地址 —— 那条线发布机不一定通")
        return super().__call__(method, url, body, ctype)


def _mark_pro(release_dir: Path) -> None:
    manifest = json.loads((release_dir / "manifest.json").read_text())
    manifest["edition"] = "pro"
    (release_dir / "manifest.json").write_text(json.dumps(manifest))


def test_an_edition_never_lands_in_the_repo_of_the_other_one(release_dir) -> None:
    """一个发布仓库只发一种发行。

    两种发行的 `manifest.json` / `payload-<ver>.tar.gz` / `install.sh` **同名不同
    内容**，而自更新问这个仓库里最新发齐的 release 要 `manifest.json` —— 混进同一个仓库，
    后发的会把先发的顶掉。

    这条替掉的是「专业版只许进私有仓库」：那钉的是「专业版不开源」那个决定，
    2026-09-20 被推翻（两种发行都发公开仓库）。可见性不再是判据，**拿错目录**才是。
    """
    _mark_pro(release_dir)                       # 手里是专业版的产物
    fake = _RepoThatAlreadyPublished(b"abc  x.dmg\n", edition="personal")  # 目标发个人版
    with pytest.raises(SystemExit, match="一直在发 personal 版"):
        pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake),
                   "https://h", "o/r")
    assert not fake.uploaded, "拒绝之前就传上去了 —— 闸放在了上传之后"


def test_the_other_direction_is_refused_too(release_dir) -> None:
    """反过来一样：个人版的产物不许进一直发专业版的那个仓库。

    这正是 2026-09-20 差点发生的事 —— 一个 shell glob 没匹配上，`release-pro/` 里
    躺的是个人版的包。
    """
    fake = _RepoThatAlreadyPublished(b"abc  x.dmg\n", edition="pro")
    with pytest.raises(SystemExit, match="一直在发 pro 版"):
        pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake),
                   "https://h", "o/r")
    assert not fake.uploaded


def test_the_same_edition_goes_through(release_dir) -> None:
    _mark_pro(release_dir)
    fake = _RepoThatAlreadyPublished(b"abc  x.dmg\n", edition="pro")
    result = pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake),
                        "https://h", "o/r")
    assert result["tag"] == "v1.2.3" and fake.uploaded


def test_a_repo_that_never_published_is_not_blocked(release_dir) -> None:
    """第一次发：没有可矛盾的事实，别拦。问不到也一样 —— 这道闸防拿错目录，不防网络。"""
    fake = _FakeForgejo(sums=b"abc  x.dmg\n")    # 它对 /releases?limit= 答 500
    result = pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake),
                        "https://h", "o/r")
    assert result["tag"] == "v1.2.3" and fake.uploaded


def test_every_install_script_in_the_release_is_baked_and_pushed(release_dir) -> None:
    """发布目录里有几个安装脚本，就得烧几个、推几个。

    这条是「一张表」那个设计的真正判据：`install.ps1` 是随 Windows 安装器一起进来的，
    而漏掉一个平台的后果正是**那条命令还在、还能跑，只是一直装旧版**。
    """
    for filename, (_prefix, rendered) in pr.INSTALL_SCRIPTS.items():
        (release_dir / filename).write_text(rendered.format(base="__RELEASE_URL__"))

    pushed = {}

    class _WithContents(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            for name in pr.INSTALL_SCRIPTS:
                if method == "GET" and f"/contents/{name}?" in url:
                    return 200, json.dumps({"sha": f"old-{name}"}).encode()
                if method == "PUT" and url.endswith(f"/contents/{name}"):
                    pushed[name] = json.loads(body); return 200, b"{}"
            return super().__call__(method, url, body, ctype)

    fake = _WithContents(sums=b"abc  x.dmg\n")
    result = pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake), "https://h", "o/r")

    import base64
    uploaded = dict(fake.uploaded)
    for filename, (_prefix, rendered) in pr.INSTALL_SCRIPTS.items():
        want = rendered.format(base="https://h/o/r/releases/download/v1.2.3")
        # 传上去的是 multipart 表单，脚本正文夹在中间 —— 比对内容，不比对整个包封。
        assert want in uploaded[filename].decode(), f"{filename} 传上去的不是烧过的那份"
        assert "__RELEASE_URL__" not in uploaded[filename].decode(), f"{filename} 传的是没改过的模板"
        assert base64.b64decode(pushed[filename]["content"]).decode() == want, f"{filename} 没推到 main"
        assert pushed[filename]["sha"] == f"old-{filename}" and pushed[filename]["branch"] == "main"
        assert result["stable_install_url"][filename].endswith(f"/raw/branch/main/{filename}")


# ─────────────────────────────────────────────────────────────────────────────
# 下面五条对应的是一直只活在仓库外那份副本里的五个修复（2026-09-18/20 真发布时
# 一个个撞出来、就地改在 `~/Desktop/ScienceMate-0.5.0-发布/publish_release.py`）。
# 09-21 搬回仓库并补上判据 —— 拿一份没人测过的脚本对外发东西，是[[护栏在被绕过侧
# ＝没有]]那条的教科书形态。


def test_the_api_can_go_to_the_loopback_while_the_links_stay_public() -> None:
    """发布走回环，写给人看的地址仍是对外那个。

    tailscale 入口吞不下几百 MB 的写入（21 分钟 0 KB/s，从 PC 自己发也一样），
    所以 API 必须能指向 Forgejo 本机；但 manifest / install.sh 里要是也变成
    `http://127.0.0.1:3000`，同事拿到的就是一条谁都用不了的命令。
    """
    api = pr.Forgejo("http://127.0.0.1:3000", "o/r", "u:t", public_host="https://out.example")
    assert api.api.startswith("http://127.0.0.1:3000/api/"), "API 没走回环"
    assert api.public_host == "https://out.example"
    assert pr.update_the_stable_install_script.__doc__  # 只是点名下面查的是它
    fake = _FakeForgejo(sums=b"")

    class _WithContents(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            if method == "GET" and "/contents/install.sh" in url:
                return 200, json.dumps({"sha": "s"}).encode()
            if method == "PUT" and url.endswith("/contents/install.sh"):
                return 200, b"{}"
            return super().__call__(method, url, body, ctype)

    api = pr.Forgejo("http://127.0.0.1:3000", "o/r", "u:t",
                     request=_WithContents(sums=b""), public_host="https://out.example")
    url = pr.update_the_stable_install_script(api, "install.sh", "x", "v1")
    assert url.startswith("https://out.example/"), f"固定安装地址指到了内网：{url}"
    assert "127.0.0.1" not in url
    assert fake  # 让 linter 闭嘴


def test_a_brand_new_empty_repo_gets_a_first_commit_before_the_tag() -> None:
    """全新仓库一个 commit 都没有，Forgejo 拒绝在空仓库上打 tag —— 先种 README。"""
    seeded = []

    class _Empty:
        def __init__(self): self.made = False
        def __call__(self, method, url, body, ctype):
            if method == "GET" and url.endswith("/releases/tags/v1"):
                return 404, b"{}"
            if method == "POST" and url.endswith("/contents/README.md"):
                seeded.append(json.loads(body)); self.made = True; return 201, b"{}"
            if method == "POST" and url.endswith("/releases"):
                if not self.made:
                    return 422, b'{"message":"repo is empty"}'
                return 201, json.dumps({"id": 1, "assets": []}).encode()
            return 500, b"unexpected"

    api = pr.Forgejo("https://h", "o/r", "u:t", request=_Empty())
    release = pr.ensure_release(api, "v1", "n", "")
    assert release["id"] == 1, "空仓库上没能建出 release"
    assert seeded and "README" in seeded[0]["message"] or seeded, "没种 README"
    import base64 as b64
    assert b64.b64decode(seeded[0]["content"]).decode().startswith("# r")


def test_finder_droppings_are_not_release_assets(release_dir) -> None:
    """`._x` / `.DS_Store` 不是发布件 —— 2026-09-18 它们真的被传上去了。"""
    (release_dir / "._x.dmg").write_bytes(b"junk")
    (release_dir / ".DS_Store").write_bytes(b"junk")
    fake = _FakeForgejo(sums=b"abc  x.dmg\n")
    pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake), "https://h", "o/r")
    names = [n for n, _ in fake.uploaded]
    assert not [n for n in names if n.startswith(".")], f"点开头的文件被当成发布件传了：{names}"


def test_an_asset_that_is_not_in_this_release_gets_removed(release_dir) -> None:
    """发布页只许有这一版该有的东西：上一次传错的、改名前的，一律删掉。"""
    class _HasStale(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            if method == "GET" and url.endswith("/releases/7"):
                return 200, json.dumps({"id": 7, "assets": [
                    {"id": 41, "name": "._leftover"},
                    {"id": 42, "name": "x.dmg"},          # 这一版有，不许删
                ]}).encode()
            return super().__call__(method, url, body, ctype)

    fake = _HasStale(sums=b"abc  x.dmg\n")
    pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake), "https://h", "o/r")
    assert any(u.endswith("/assets/41") for u in fake.deleted), "多余资产没被删"
    assert not any(u.endswith("/assets/42") for u in fake.deleted), "把这一版该有的资产删了"


def test_the_readback_retries_the_public_entrance_then_says_it_fell_back(release_dir, capsys, monkeypatch) -> None:
    """回读先走对外地址（那是同事下载的那条路）；三次都不通才改走 API 地址，**并说出来**。

    「内容验了、入口没验」和「全验了」是两个结论，不许长一个样。
    """
    monkeypatch.setattr(pr, "RETRY_PAUSE_S", 0)
    tried = []

    class _EntranceDown(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            if method == "GET" and url.endswith("/SHA256SUMS"):
                tried.append(url)
                if url.startswith("https://out"):        # 对外入口：噎住
                    raise TimeoutError("the write operation timed out")
                return 200, self.sums                    # API 地址：读得到
            return super().__call__(method, url, body, ctype)

    api = pr.Forgejo("https://api.internal", "o/r", "u:t",
                     request=_EntranceDown(sums=b"abc  x.dmg\n"), public_host="https://out")
    result = pr.publish(release_dir, api, "https://out", "o/r")

    assert result["tag"] == "v1.2.3"
    assert sum(1 for u in tried if u.startswith("https://out")) == 3, f"没把对外入口试满三次：{tried}"
    assert tried[-1].startswith("https://api.internal"), "没回落到 API 地址"
    assert "入口没验" in capsys.readouterr().out, "回落了却没说，读的人会以为入口验过了"


# ── 发 Windows 安装器之前，得有人在 Windows 上真发过一条消息 ──────────────────
#
# 两次一模一样的事故：0.4.0 每轮 `[WinError 5]`（#908）、0.5.x 每轮 `[WinError 6]`
# （#1122）。两次都是改了 spawn、Windows 上一次都没真发过消息就发版，两次的"验证"
# 都只到 `/health/ready` 200 —— 而起 worker 那条路健康检查一步都不走。CI 只有
# Linux，所以这件事只能在**发布**那一刻拦。


WIN_SUMS = "abc  x.dmg\nbeef  ScienceMate-Setup.exe\n"


def _with_windows(release_dir, receipt: dict | None):
    (release_dir / "SHA256SUMS").write_text(WIN_SUMS, encoding="utf-8")
    if receipt is not None:
        (release_dir / pr.WINDOWS_ACCEPTANCE).write_text(json.dumps(receipt), encoding="utf-8")
    return _FakeForgejo(sums=WIN_SUMS.encode())


def test_a_windows_installer_without_a_receipt_is_refused(release_dir) -> None:
    fake = _with_windows(release_dir, None)
    api = pr.Forgejo("https://h", "o/r", "u:t", request=fake)

    with pytest.raises(SystemExit) as exc:
        pr.publish(release_dir, api, "https://h", "o/r")

    assert "ScienceMate-Setup.exe" in str(exc.value)
    assert fake.uploaded == [], "拒绝之前就不该传任何东西上去"


def test_a_receipt_for_other_bytes_is_refused(release_dir) -> None:
    """验过的和要发的必须是同一份字节 —— 不然收据只证明"某个包"跑通过。"""
    fake = _with_windows(release_dir, {"against": "http://127.0.0.1:1", "sha256": "d00d"})
    api = pr.Forgejo("https://h", "o/r", "u:t", request=fake)

    with pytest.raises(SystemExit) as exc:
        pr.publish(release_dir, api, "https://h", "o/r")

    assert "d00d" in str(exc.value) and "beef" in str(exc.value)
    assert fake.uploaded == []


def test_a_receipt_from_the_source_tree_is_refused(release_dir) -> None:
    """`against` 空 = 验的是源码树里现起的后端。那条路 Windows 上一直通，证明不了这个包。"""
    fake = _with_windows(release_dir, {"sha256": "beef"})
    api = pr.Forgejo("https://h", "o/r", "u:t", request=fake)

    with pytest.raises(SystemExit) as exc:
        pr.publish(release_dir, api, "https://h", "o/r")

    assert "against" in str(exc.value)
    assert fake.uploaded == []


def test_a_matching_receipt_lets_the_release_through(release_dir) -> None:
    fake = _with_windows(release_dir, {"against": "http://127.0.0.1:1", "sha256": "BEEF",
                                       "host": "pc", "ran_at": "2026-09-22T10:00:00+00:00"})
    api = pr.Forgejo("https://h", "o/r", "u:t", request=fake)

    result = pr.publish(release_dir, api, "https://h", "o/r")

    assert result["tag"] == "v1.2.3"
    assert pr.WINDOWS_ACCEPTANCE not in dict(fake.uploaded), (
        "收据是凭据不是发布件 —— 它不在 SHA256SUMS 里，传上去就多出一个没人核对的资产"
    )


def test_a_mac_only_release_needs_no_windows_receipt(release_dir) -> None:
    """对照组：清单里没有 Windows 安装器时这道闸必须闭嘴，否则它拦的是"发布"本身。"""
    fake = _FakeForgejo(sums=b"abc  x.dmg\n")
    api = pr.Forgejo("https://h", "o/r", "u:t", request=fake)

    assert pr.publish(release_dir, api, "https://h", "o/r")["tag"] == "v1.2.3"


def test_the_gate_reads_the_manifest_the_way_install_ps1_does() -> None:
    """认安装器的规则与 `install.ps1` 同一条（它挑 `*Setup.exe`）——两处各写一条就会分叉。"""
    script = (REPO / "scripts" / "package" / "install.ps1").read_text(encoding="utf-8")
    assert f"*{pr.INSTALLER_SUFFIX}" in script, (
        "install.ps1 认安装器的规则变了，而发布闸还按老规则挑 —— 改名那天没人会报错"
    )
    found = pr.the_windows_installers(
        "aa  x.dmg\nbb  ScienceMate-Setup.exe\ncc  ScienceMate-Pro-Setup.exe\ndd  install.ps1\n")
    assert found == {"ScienceMate-Setup.exe": "bb", "ScienceMate-Pro-Setup.exe": "cc"}


def test_a_stable_script_that_is_not_on_main_yet_gets_created(release_dir) -> None:
    """新加一个平台的安装脚本时 main 上还没有它 —— 只会 PUT 的话第一版永远更新不成，
    那个平台的「一句话安装」就一直带着版本号，而带版本号正是这一步要消灭的毛病。

    2026-09-22 发 0.5.2 真撞上：`install.ps1` 是新的，publish 打出
    「读不到 main 上的 install.ps1（HTTP 404）—— 请手工更新」。
    """
    made = {}

    class _NoScriptsOnMain(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            for name in pr.INSTALL_SCRIPTS:
                if method == "GET" and f"/contents/{name}?" in url:
                    return 404, b'{"message":"object does not exist"}'
                if method == "POST" and url.endswith(f"/contents/{name}"):
                    made[name] = json.loads(body); return 201, b"{}"
                if method == "PUT" and url.endswith(f"/contents/{name}"):
                    raise AssertionError(f"{name} 不在 main 上，却用了 PUT")
            return super().__call__(method, url, body, ctype)

    fake = _NoScriptsOnMain(sums=b"abc  x.dmg\n")
    result = pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=fake), "https://h", "o/r")

    assert "install.sh" in made, "main 上没有就该创建，而不是放弃"
    assert "sha" not in made["install.sh"], "创建时不该带 sha（哪怕是 null）"
    assert made["install.sh"]["branch"] == "main"
    import base64 as b64
    written = b64.b64decode(made["install.sh"]["content"]).decode()
    assert 'DEFAULT_URL="https://h/o/r/releases/download/v1.2.3"' in written, "创建的是没烧过的模板"
    assert result["stable_install_url"]["install.sh"].endswith("/raw/branch/main/install.sh"), \
        "创建成功了却没把那个永不变的地址报回来"


def test_a_repo_that_cannot_be_read_is_not_silently_created_over(release_dir, capsys) -> None:
    """读不动（500）和「还没有」（404）是两回事 —— 前者不许当成「那就建一个」。"""
    class _Broken(_FakeForgejo):
        def __call__(self, method, url, body, ctype):
            if method == "GET" and "/contents/install.sh?" in url:
                return 500, b"boom"
            if method in ("PUT", "POST") and url.endswith("/contents/install.sh"):
                raise AssertionError("读都没读明白就动 main 上的文件")
            return super().__call__(method, url, body, ctype)

    result = pr.publish(release_dir, pr.Forgejo("https://h", "o/r", "u:t", request=_Broken(sums=b"abc  x.dmg\n")),
                        "https://h", "o/r")
    assert result["stable_install_url"] == {"install.sh": ""}
    assert "请手工更新" in capsys.readouterr().out


# ── 默认兼容的最后一道：和上一次**发出去的**那一版比线（2026-09-23）─────────────
#
# 组织服务器夜里会自己升到这一版，而同事的桌面可能还是上一版。仓库里那张快照闸管开发中
# 的每个 PR；这里管的是装在别人机器上的那一版 —— 兼容承诺是对它许的。


class _RepoThatShippedAWire(_FakeForgejo):
    """上一次发布（v1.2.2）带着一份 org_wire.json。"""

    def __init__(self, sums, wire):
        super().__init__(sums)
        self.wire = wire

    def __call__(self, method, url, body, ctype):
        if method == "GET" and "/releases?limit=" in url:
            return 200, json.dumps([
                {"tag_name": "v1.2.3", "assets": [{"name": "org_wire.json"}]},     # 这一版自己（重发时）
                {"tag_name": "v1.2.2", "assets": [{"name": "org_wire.json",
                                                   "browser_download_url": "https://unreachable.invalid/w"}]},
            ]).encode()
        if method == "GET" and url.endswith("/releases/download/v1.2.2/org_wire.json"):
            assert url.startswith("https://h/"), f"闸没走发布用的 host：{url}"
            return 200, json.dumps(self.wire).encode()
        if method == "GET" and url.endswith("/releases/download/v1.2.3/org_wire.json"):
            raise AssertionError("拿这一版自己去比 —— 永远不会断")
        return super().__call__(method, url, body, ctype)


def _wire(protocol: int, **operations) -> dict:
    return {"org_protocol": protocol, "operations": operations}


_BEFORE = _wire(1, **{"POST /api/v1/projects/": {"params": {}, "body": {"name": True, "research_domain": False}}})


def _ship(release_dir: Path, now: dict, before: dict = _BEFORE):
    (release_dir / "org_wire.json").write_text(json.dumps(now))
    fake = _RepoThatShippedAWire((release_dir / "SHA256SUMS").read_bytes(), before)
    return fake, pr.Forgejo("https://h", "o/r", "t", request=fake)










def test_the_first_release_with_a_wire_has_nothing_to_compare(release_dir, capsys) -> None:
    (release_dir / "org_wire.json").write_text(json.dumps(_BEFORE))
    api = pr.Forgejo("https://h", "o/r", "t", request=_FakeForgejo((release_dir / "SHA256SUMS").read_bytes()))
    pr.publish(release_dir, api, "https://h", "o/r")
    assert "兼容承诺从这一版起算" in capsys.readouterr().out


def test_the_mac_packager_writes_the_wire_before_the_sums() -> None:
    """线要进 SHA256SUMS（回读时一并核对），所以得在装配发布目录之前写。"""
    mac = (REPO / "scripts" / "package" / "build_mac_app.py").read_text(encoding="utf-8")
    body = mac[mac.index("def main("):] if "def main(" in mac else mac
    assert body.index("write_the_wire(DIST / \"release\")") < body.index("assemble_the_release_dir("), \
        "线在发布目录装配之后才写 —— 它不会进 SHA256SUMS"
