"""把 `dist/release/` 发成 Forgejo release —— 安装脚本和自更新从此有了地址。

    FORGEJO_TOKEN=<user:token 或 token> python3 scripts/package/publish_release.py \\
        --repo wangd/harness-framework --host https://desktop-9el2944.taile9f15e.ts.net

## 做什么

0. **这一版要发 Windows 安装器的话，先核验收收据**（`windows-acceptance.json`）：有人
   在 Windows 上拿**这些字节**装出来的应用真发过一条消息、真收到过回复。没有就不发。
   收据由 `scripts/acceptance/personal_smoke.py --url … --receipt … --for-installer …`
   在跑通之后写。这一步不可跳过的理由见 `refuse_a_windows_build_nobody_ran`：
   0.4.0 与 0.5.x 两次发出去的 Windows 包都是**每一轮必崩**，两次的"验证"都只到
   `/health/ready` 200，而起 worker 那条路健康检查一步都不走。CI 只有 Linux。
1. 读 `dist/release/manifest.json` 拿版本 → tag `v<版本>`；release 不存在就建，存在就复用。
2. 把 `install.sh` 里的 DEFAULT_URL 改写成**这个 release 的下载地址**
   （`<host>/<owner>/<repo>/releases/download/v<版本>`），装这份就装这一版。
3. 目录里每个文件上传为 release 资产；同名旧资产先删 —— 重发同一版是幂等的。
4. 传完把 SHA256SUMS 下回来逐字节比对，确认 Forgejo 存的就是发出去的。

## 私有仓库

release 资产跟仓库同一个可见性。仓库私有 → 没登录的人下不到（install.sh 与应用的
自更新都会 404/401）。两条路：给同事一个只读 token（install.sh 认 AFS_AUTH）、或者
把 release 放一个公开的、只放发布件的仓库。脚本不替你选，只把事实印出来。

只用标准库：打包器跑在系统 python3 上，那台机器不一定有 requests。
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "https://desktop-9el2944.taile9f15e.ts.net"

#: 对外入口读不到就等一会儿再试。入口是 tailscale，断续是常态。
RETRY_PAUSE_S = 10
#: 发布件仓库（公开）。源码仓库是私有的，资产跟仓库同可见性 —— 发那儿等于
#: 同事没 token 就下不到。
DEFAULT_REPO = "wangd/sciencemate"


class Forgejo:
    """够用的一层：GET / POST JSON / POST 文件 / DELETE。`request` 可注入，测试不打网。"""

    def __init__(self, host: str, repo: str, token: str, request=None,
                 public_host: str | None = None):
        # `host` = API 请求发往哪儿；`public_host` = 写给人看的地址。
        # 2026-09-18 实测：往 tailscale 入口 POST 几百 MB 的资产会写超时、零进度
        # （从 Mac 21 分钟 0 KB/s，从 PC 自己发也一样），而小的 API 请求都正常 ——
        # 入口吞不下大写入。发布因此得在 Forgejo 本机走回环，可 manifest 与
        # install.sh 里仍必须是同事能用的那个对外地址。两件事，两个字段。
        self.host = host.rstrip("/")
        self.public_host = (public_host or host).rstrip("/")
        self.owner, self.name = repo.split("/", 1)
        self.api = f"{self.host}/api/v1/repos/{self.owner}/{self.name}"
        self.token = token
        self._request = request or self._urllib

    #: 单次 socket 操作的超时（不是整趟的）。发布件几百 MB，慢但在动的不该被误杀；
    #: 真噎住（这么久一个字节都没动）才算失败，交给上面的重试。
    SOCKET_TIMEOUT_S = 1800

    def _auth(self) -> str:
        if ":" in self.token:
            return "Basic " + base64.b64encode(self.token.encode()).decode()
        return "token " + self.token

    def _urllib(self, method: str, url: str, body: bytes | None, content_type: str | None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self._auth())
        req.add_header("Accept", "application/json")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self.SOCKET_TIMEOUT_S) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get(self, path: str):
        status, body = self._request("GET", self.api + path, None, None)
        return status, _maybe_json(body)

    def post_json(self, path: str, payload: dict):
        status, body = self._request("POST", self.api + path, json.dumps(payload).encode(), "application/json")
        return status, _maybe_json(body)

    def delete(self, path: str) -> int:
        status, _ = self._request("DELETE", self.api + path, None, None)
        return status

    #: 传大资产失败几次就放弃。发布件是几百 MB，网络噎一下不该让整趟白跑
    #: （2026-09-10 实测：245MB 的 dmg 传到一半 `The write operation timed out`，
    #: 一次失败整个发布就断在半路，release 里只躺着一个 SHA256SUMS）。
    UPLOAD_ATTEMPTS = 4

    def upload(self, path: str, filename: str, data: bytes, *, sleep=time.sleep):
        """传一个资产。**失败要重试，传完要回读核对大小。**

        与 `build_windows_app.download()` 同一条规则（#892 给下载补的那条）——
        「几百 MB 的网络搬运必须有超时、重试和完整性判据」。那边补了，这边没有，
        于是同一个形状的失败在发布这一头又来了一次：一次写超时 → 整趟发布断在半路，
        而**已经传上去的部分还留在 release 上**，看起来像"发过了"。

        回读核对是这里的完整性判据：Forgejo 收下多少字节，问它自己要。
        """
        boundary = "----afs" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"attachment\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        url = self.api + path + "?" + urllib.parse.urlencode({"name": filename})
        last = None
        for attempt in range(1, self.UPLOAD_ATTEMPTS + 1):
            status, resp = self._request(
                "POST", url, body, f"multipart/form-data; boundary={boundary}")
            if status in (200, 201):
                return status, _maybe_json(resp)
            last = (status, _maybe_json(resp))
            if attempt < self.UPLOAD_ATTEMPTS:
                print(f"  ↻ {filename} 第 {attempt} 次没传上去（HTTP {status}），重试")
                sleep(2 * attempt)
                # 重试前先把可能落下的半截资产删掉，免得 Forgejo 拒绝同名
                self._drop_asset_named(path, filename)
        return last

    def _drop_asset_named(self, assets_path: str, filename: str) -> None:
        """删掉同名资产（重试前的清场）。删不掉不抛 —— 它只是让重试更可能成功。"""
        release_path = assets_path.rsplit("/assets", 1)[0]
        status, release = self.get(release_path)
        if status != 200 or not isinstance(release, dict):
            return
        for asset in release.get("assets") or []:
            if asset.get("name") == filename:
                self.delete(f"{release_path}/assets/{asset['id']}")


def _maybe_json(body: bytes):
    """错误体不一定是 JSON（反代的 413 是 HTML）—— 解不开就原样当文本，别在报错路上再抛一次。"""
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")[:300]


def download_base(host: str, repo: str, tag: str) -> str:
    return f"{host.rstrip('/')}/{repo}/releases/download/{tag}"


#: 两个安装脚本各自把发布地址写在哪一行。**一张表**——加第三个平台时只动这里，
#: 而不是在三处 if 里各写一遍（那时漏掉的那一处会安静地一直指向上一版）。
INSTALL_SCRIPTS = {
    "install.sh": ('DEFAULT_URL="', 'DEFAULT_URL="{base}"\n'),
    "install.ps1": ("$DefaultUrl = '", "$DefaultUrl = '{base}'\n"),
}


#: Windows 验收收据的文件名。它**不上传** —— 它是"这份包被人在 Windows 上真跑过"
#: 的凭据，留在发布机上；发布页上该有的是发布件。
WINDOWS_ACCEPTANCE = "windows-acceptance.json"

#: 清单里哪一条是 Windows 安装器。**与 `install.ps1` 认安装器用的是同一条规则**
#: （它下 SHA256SUMS、挑 `*Setup.exe`）—— 两处各写一条规则就会在改名那天分叉，
#: 而分叉的那一天谁都不会报错。
INSTALLER_SUFFIX = "Setup.exe"


def the_windows_installers(sums: str) -> dict[str, str]:
    """SHA256SUMS 里的 Windows 安装器：``{名字: sha256}``。"""
    found = {}
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*").endswith(INSTALLER_SUFFIX):
            found[parts[1].lstrip("*")] = parts[0]
    return found


def refuse_a_windows_build_nobody_ran(release_dir: Path) -> None:
    """发 Windows 安装器之前，必须有人拿**这些字节**装出来的应用真发过一条消息。

    这道闸的由来是两次一模一样的事故：0.4.0 每一轮 `[WinError 5]`（#908）、
    0.5.x 每一轮 `[WinError 6]`（#1122）。两次都是**改了 spawn，Windows 上一次都
    没真发过消息就发版**；两次的"验证"都只到 `/health/ready` 200 —— 而起 worker
    那条路，健康检查一步都不走。CI 只有 Linux，所以这件事不可能由 CI 来答。

    收据由 `scripts/acceptance/personal_smoke.py --url … --receipt … --for-installer …`
    写，且只在**跑到收到一条回复**之后写。这里核三件：收据在不在、它认的 sha256 是不是
    清单里这个安装器、它验的是不是一个**跑着的实例**（`against`）。哈希对得上，
    "验过的"和"发出去的"就是同一份字节；对不上就是有人验了另一个包。
    """
    sums = (release_dir / "SHA256SUMS").read_text(encoding="utf-8")
    installers = the_windows_installers(sums)
    if not installers:
        return                                  # 这一版没有 Windows 件，没什么要验的
    receipt_path = release_dir / WINDOWS_ACCEPTANCE
    how = (
        "\n  在 Windows 上装好这个包，后端地址见 "
        "%LOCALAPPDATA%\\afs\\logs\\shell.log 的 READY 那行，然后：\n"
        f"    python scripts\\acceptance\\personal_smoke.py --url http://127.0.0.1:<端口> \\\n"
        f"        --receipt {WINDOWS_ACCEPTANCE} --for-installer <这个 Setup.exe 的路径>\n"
        f"  跑到「收到回复」它才会写收据。把收据拷进发布目录再发。"
    )
    if not receipt_path.is_file():
        raise SystemExit(
            f"这一版要发 Windows 安装器（{', '.join(sorted(installers))}），"
            f"但发布目录里没有 {WINDOWS_ACCEPTANCE} —— "
            f"没有任何人在 Windows 上拿它真发过一条消息。" + how)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not receipt.get("against"):
        raise SystemExit(
            f"{WINDOWS_ACCEPTANCE} 没有 against —— 它验的不是一个装好跑着的实例，"
            f"而是源码树里现起的后端。那条路 Windows 上一直是通的，证明不了这个包。" + how)
    verified = str(receipt.get("sha256", "")).lower()
    for name, sha256 in sorted(installers.items()):
        if verified == sha256.lower():
            print(f"  ✓ Windows 验收：{name} 在 {receipt.get('host', '?')} "
                  f"上跑到收到回复（{receipt.get('ran_at', '?')}）")
            return
    raise SystemExit(
        f"{WINDOWS_ACCEPTANCE} 认的是 {verified or '(空)'}，而这一版要发的是 "
        + "、".join(f"{n}={h}" for n, h in sorted(installers.items()))
        + " —— 验过的和要发的不是同一份字节，别发。" + how)


def bake_the_release_url(filename: str, script: str, base: str) -> str:
    """把安装脚本里的默认地址指向这个 release。只动赋值那一行。"""
    prefix, rendered = INSTALL_SCRIPTS[filename]
    out, hit = [], 0
    for line in script.splitlines(keepends=True):
        if line.startswith(prefix):
            out.append(rendered.format(base=base)); hit += 1
        else:
            out.append(line)
    if hit != 1:
        raise SystemExit(f"{filename} 里默认地址的赋值行有 {hit} 处，应恰好 1 处")
    return "".join(out)


def ensure_release(api: Forgejo, tag: str, name: str, notes: str) -> dict:
    status, release = api.get(f"/releases/tags/{tag}")
    if status == 200 and release:
        print(f"  release {tag} 已存在（id {release['id']}），复用")
        return release
    payload = {"tag_name": tag, "name": name, "body": notes, "draft": False, "prerelease": False}
    status, release = api.post_json("/releases", payload)
    if status == 422 and "empty" in str(release):
        # 2026-09-18 实测：新建的仓库一个 commit 都没有，Forgejo 拒绝在空仓库上打 tag。
        # 种一个 README 就有 main 了 —— 第一次往新仓库发布必撞这一下。
        st, body = api._request("POST", api.api + "/contents/README.md", json.dumps({
            "message": "初始化：这个仓库只放发布件",
            "content": base64.b64encode(
                f"# {api.name}\n\n发布件在 Releases 页；安装命令见各版本说明。\n".encode()).decode(),
        }).encode(), "application/json")
        if st not in (200, 201):
            raise SystemExit(f"仓库是空的，种 README 也失败：HTTP {st} {_maybe_json(body)}")
        print("  仓库原本是空的，种了 README.md 作第一个 commit")
        status, release = api.post_json("/releases", payload)
    if status not in (200, 201):
        raise SystemExit(f"建 release 失败：HTTP {status} {release}")
    print(f"  建了 release {tag}（id {release['id']}）")
    return release


def replace_asset(api: Forgejo, release: dict, filename: str, data: bytes) -> dict:
    for asset in release.get("assets") or []:
        if asset.get("name") == filename:
            code = api.delete(f"/releases/{release['id']}/assets/{asset['id']}")
            if code not in (200, 204):
                raise SystemExit(f"删旧资产 {filename} 失败：HTTP {code}")
    status, asset = api.upload(f"/releases/{release['id']}/assets", filename, data)
    if status not in (200, 201):
        raise SystemExit(
            f"上传 {filename}（{len(data) // 1024} KB）失败：HTTP {status} {asset}\n"
            "  常见原因：Forgejo app.ini 的 [attachment] MAX_SIZE（默认 4 MB）或 ALLOWED_TYPES 不放行。"
        )
    # 传完**回读核对字节数**：HTTP 201 只说明它收下了这次请求，不说明落盘的就是整份文件。
    # 2026-09-10 实测过同一形状的反面教材：scp 还没传完就去算 SHA256SUMS，记下了半截
    # 文件的哈希；那次是 `shasum -c` 逮住的。发布这一头也得有自己的判据，别指望下游。
    got = asset.get("size") if isinstance(asset, dict) else None
    if got is not None and int(got) != len(data):
        raise SystemExit(
            f"上传 {filename} 后大小对不上：本地 {len(data)} 字节，Forgejo 说 {got} 字节。\n"
            "  发出去的将是半截文件 —— 拒绝继续。"
        )
    return asset


def update_the_stable_install_script(api: Forgejo, filename: str, script: str, tag: str) -> str:
    """把仓库 main 分支上的 `install.sh` / `install.ps1` 换成指向这一版的那份。

    ## 为什么要有这一步

    release 资产的 URL 带版本号（`releases/download/v0.3.0/…`），而 Forgejo 7.0 没有
    `releases/latest/download/`。所以"一句话安装"如果用资产 URL，**每发一版就得让所有人
    换一条命令** —— 发布页上的那行会一直装旧版，而且没有任何东西会报错。

    git 树上的 `raw/branch/main/install.sh` 这个地址永不变。每发一版只改它的内容。
    于是发布页写一次，之后一直对。

    改不动不算发布失败（release 已经好了）—— 但要吵出来，因为不改的后果正是"命令还在、
    装的是旧版"这种没人会发现的错。

    ## 还不在 main 上的那一版

    新加一个平台的安装脚本时（2026-09-22 的 `install.ps1`），main 上还没有这个文件。
    只会 PUT 的话，第一版永远更新不成 —— 于是那个平台的「一句话安装」一直带着版本号，
    而带版本号那条命令的毛病正是这个函数要消灭的。没有就**创建**它。
    """
    status, existing = api.get(f"/contents/{filename}?ref=main")
    known = existing.get("sha") if isinstance(existing, dict) else None
    if status not in (200, 404) or (status == 200 and not known):
        print(f"  ⚠️ 读不到 main 上的 {filename}（HTTP {status}）——"
              f" 发布页那条固定命令仍指向上一版，请手工更新")
        return ""
    payload = {"content": base64.b64encode(script.encode()).decode(), "branch": "main",
               "message": f"{filename} → {tag}" if known else f"加上 {filename}（{tag}）"}
    if known:
        payload["sha"] = known
    status, body = api._request("PUT" if known else "POST", api.api + f"/contents/{filename}",
                                json.dumps(payload).encode(), "application/json")
    if status not in (200, 201):
        print(f"  ⚠️ {'更新' if known else '创建'} main 上的 {filename} 失败"
              f"（HTTP {status} {_maybe_json(body)}）——"
              f" 发布页那条固定命令仍指向上一版，请手工更新")
        return ""
    print(f"  ✓ main 上的 {filename} {'已指向' if known else '新建好并指向'} {tag}"
          f"（发布页那条固定命令自动跟上）")
    return f"{api.public_host}/{api.owner}/{api.name}/raw/branch/main/{filename}"


def the_edition_this_repo_has_been_publishing(api: Forgejo) -> str | None:
    """这个仓库一直在发哪种发行 —— **问它已经发出去的东西**。

    不看仓库叫什么（名字里带不带 pro 都可能骗人），也不看它公不公开
    （2026-09-20 起两种发行都发公开仓库，可见性不再区分它们）。
    读最近几个 release 里的 `manifest.json`，它自己写着 `edition`。

    空仓库 / 问不到 → `None`：第一次发没有可矛盾的事实，而这道闸防的是**拿错
    目录**，不是防网络。
    """
    status, releases = api.get("/releases?limit=5")
    if status != 200 or not isinstance(releases, list):
        return None
    for release in releases:
        for asset in release.get("assets") or []:
            if asset.get("name") != "manifest.json":
                continue
            # 走**这次发布用的那个 host**，不用资产里记的 browser_download_url：
            # 后者是 Forgejo 按自己的 ROOT_URL 生成的对外地址（这里是 ts.net），
            # 而发布机不一定走得通那条线（2026-09-20：TLS 握手超时/EOF）。
            # 闸不该把自己变成发布的单点故障 —— 它只是要读一个 800 字节的 JSON。
            tag = release.get("tag_name")
            if not tag:
                continue
            url = f"{api.host}/{api.owner}/{api.name}/releases/download/{tag}/manifest.json"
            status, body = api._request("GET", url, None, None)
            if status != 200:
                continue
            try:
                return str(json.loads(body).get("edition", "personal"))
            except Exception:  # noqa: BLE001 —— 读不懂就当没读到
                continue
    return None


def refuse_if_this_repo_publishes_another_edition(api: Forgejo, repo: str, edition: str) -> None:
    """一个发布仓库只发一种发行。

    两种发行的 `manifest.json` / `payload-<ver>.tar.gz` / `install.sh` **同名不同
    内容**，而自更新是问这个仓库里最新发齐的 release 要 `manifest.json` 的 —— 把两种混进同一个
    仓库，后发的那种会把先发的顶掉，另一半用户的自更新从此拿到不属于自己的载荷。

    这道闸原本写的是「专业版只许进私有仓库」。那钉的是「专业版不开源」那个决定，
    2026-09-20 wangd 推翻了它（「专业版不要放私有仓库呀，先全面公开」）。可见性不再
    是判据，**拿错目录**才是：今天就差点把个人版的产物当成专业版发出去（一个 shell
    glob 没匹配上，release-pro/ 里躺的是个人版的包）。
    """
    already = the_edition_this_repo_has_been_publishing(api)
    if already is not None and already != edition:
        raise SystemExit(
            f"{repo} 一直在发 {already} 版，而这份发布件是 {edition} 版 —— 拒绝发布。\n"
            f"  两种发行的 manifest.json / payload / install.sh 同名不同内容：混进同一个仓库，"
            f"后发的会把先发的顶掉，另一半用户的自更新会拿到不属于自己的载荷。\n"
            f"  要么换一个仓库，要么确认 --release-dir 指对了（{edition} 版的产物在哪个目录）。")


#: 组织服务器对外的"线"的快照（`app/pro/org_wire.py`）。专业版每次发布都带一份。
WIRE = "org_wire.json"


def _org_wire_module():
    """按路径装载 `platform/backend/app/pro/org_wire.py`：比较规则只在那一处定义。
    模块级只 import 标准库（`the_wire_now` 才碰 app），系统 python3 也装得起来。"""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "platform" / "backend" / "app" / "pro" / "org_wire.py"
    if not path.is_file():
        return None   # 个人版的树（公开树）：没有组织服务器，也就没有线
    spec = importlib.util.spec_from_file_location("afs_org_wire", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def the_wire_the_last_release_shipped(api: Forgejo, tag_now: str) -> tuple[str, dict] | None:
    """上一次**真发出去**的那一版的线（它的 `org_wire.json` 资产）。没有 → None。

    和 `the_edition_this_repo_has_been_publishing` 一样走发布用的 host，不走资产里记的对外地址。
    """
    status, releases = api.get("/releases?limit=10")
    if status != 200 or not isinstance(releases, list):
        return None
    for release in releases:
        tag = release.get("tag_name")
        if not tag or tag == tag_now:
            continue
        if not any(asset.get("name") == WIRE for asset in release.get("assets") or []):
            continue
        status, body = api._request("GET", f"{api.host}/{api.owner}/{api.name}/releases/download/{tag}/{WIRE}", None, None)
        if status != 200:
            continue
        try:
            return tag, json.loads(body)
        except ValueError:
            continue
    return None


def refuse_if_the_wire_broke(api: Forgejo, release_dir: Path, manifest: dict, tag: str) -> None:
    """默认兼容的最后一道：这一版的线和**上一次发出去的**那一版比，断了而协议号没动 → 不发。

    为什么在这里还要一道（仓库里的快照闸之外）：那张快照是开发中确认过的线，可以被人
    一起重拍；这里比的是**装在别人机器上的**那一版 —— 服务器夜里会自己升到这一版，而
    同事的桌面可能还停在上一版。兼容承诺是对它许的。
    """
    now_path = release_dir / WIRE
    if "server" in manifest and not now_path.is_file():
        raise SystemExit(f"这一版带着组织服务器包，却没带 {WIRE} —— 打包没跑到写线的那一步，"
                         "下一次发布就没有东西可比。重打一次专业版。")
    if not now_path.is_file():
        return
    now = json.loads(now_path.read_text(encoding="utf-8"))
    shipped = the_wire_the_last_release_shipped(api, tag)
    if shipped is None:
        print(f"  上一次发布没带 {WIRE}（0.5.4 之前）—— 兼容承诺从这一版起算")
        return
    before_tag, before = shipped
    wire = _org_wire_module()
    if wire is None:
        print("  这棵树里没有组织服务器的线（个人版）—— 不比")
        return
    broke = wire.what_broke(before, now)
    if broke and now.get("org_protocol", 0) <= before.get("org_protocol", 0):
        raise SystemExit(
            f"和上一次发布（{before_tag}）比，组织服务器的线断了 {len(broke)} 处，而协议号没动"
            f"（{now.get('org_protocol')}）—— 拒绝发布：\n    " + "\n    ".join(broke) +
            "\n  服务器会夜里自己升到这一版，而同事的桌面可能还是上一版 —— 这几处一断，他们就坏了。"
            "\n  要么改回兼容的写法，要么 app/pro/org_protocol.py 的 ORG_PROTOCOL 加一（桌面继续认上一版服务器）。")
    if broke:
        print(f"  线断了 {len(broke)} 处，协议号 {before.get('org_protocol')} → {now.get('org_protocol')}："
              "还没更新的桌面会被要求更新")
    else:
        print(f"  线和 {before_tag} 兼容（{len(wire.what_was_added(before, now))} 处只加不减）")


def publish(release_dir: Path, api: Forgejo, host: str, repo: str) -> dict:
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]; tag = f"v{version}"
    refuse_if_this_repo_publishes_another_edition(api, repo, manifest.get("edition", "personal"))
    refuse_a_windows_build_nobody_ran(release_dir)
    refuse_if_the_wire_broke(api, release_dir, manifest, tag)
    base = download_base(host, repo, tag)
    print(f"  版本 {version} → tag {tag}\n  下载地址 {base}/")
    release = ensure_release(api, tag, f"ScienceMate {version}", manifest.get("notes") or "")

    uploaded, baked = {}, {}
    for path in sorted(release_dir.iterdir()):
        # `._x` / `.DS_Store`：发布目录经手过 U 盘或 Finder 就会长出来，
        # 2026-09-18 它们真的被当成资产传上去了。点开头的一律不是发布件。
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name == WINDOWS_ACCEPTANCE:
            continue            # 凭据，不是发布件（它也不在 SHA256SUMS 里）
        data = path.read_bytes()
        if path.name in INSTALL_SCRIPTS:
            data = bake_the_release_url(path.name, data.decode("utf-8"), base).encode("utf-8")
            baked[path.name] = data.decode("utf-8")
        asset = replace_asset(api, release, path.name, data)
        uploaded[path.name] = asset.get("browser_download_url")
        print(f"  ↑ {path.name}（{len(data) // 1024} KB）")

    # release 上不在本地发布目录里的资产（上一次传错的、改名前的）一律删掉：
    # 发布页只许有这一版该有的东西。2026-09-18 一批 `._*` 垃圾就是这么留在上面的。
    status, fresh = api.get(f"/releases/{release['id']}")
    for asset in (fresh.get("assets") or []) if status == 200 and isinstance(fresh, dict) else []:
        if asset.get("name") not in uploaded:
            code = api.delete(f"/releases/{release['id']}/assets/{asset['id']}")
            print(f"  ✂ 删掉 release 上多余的 {asset.get('name')}（HTTP {code}）")

    stable = {name: update_the_stable_install_script(api, name, script, tag)
              for name, script in sorted(baked.items())}

    # 传完读回来比对：Forgejo 存的就是我发的。**先走对外地址** —— 那是同事下载走的
    # 那条路，顺带验了入口。入口时断时续就多试几次；实在读不到再走 API 地址读同一个
    # 文件（内容一样，只是没验到入口，这种情况要说出来，别让人以为验过了）。
    local = (release_dir / "SHA256SUMS").read_bytes()
    status, body = -1, b""
    attempts = [f"{base}/SHA256SUMS"] * 3 + [f"{api.host}/{repo}/releases/download/{tag}/SHA256SUMS"]
    for attempt, url in enumerate(attempts):
        try:
            status, body = api._request("GET", url, None, None)
        except Exception as exc:                       # noqa: BLE001 —— 入口断了也算一次失败
            status, body = -1, str(exc).encode()
        if status == 200:
            if attempt == len(attempts) - 1:
                print("  ⚠️ 对外入口三次没读到，回读改走 API 地址 —— 内容验了，入口没验")
            break
        if attempt < len(attempts) - 1:
            time.sleep(RETRY_PAUSE_S)
    if status != 200 or body != local:
        raise SystemExit(f"回读 SHA256SUMS 不一致（HTTP {status}）—— 发布件与本地不同，别信这份 release")
    print("  ✓ 回读 SHA256SUMS 逐字节一致")
    return {"tag": tag, "base": base, "assets": uploaded, "stable_install_url": stable}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--release-dir", default=str(REPO / "dist" / "release"))
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="发布件对外的地址（写进 manifest / install.sh）")
    parser.add_argument("--api-host", default=None,
                        help="API 请求发往哪儿；默认同 --host。在 Forgejo 本机发布时给回环地址，"
                             "绕开吞不下大写入的对外入口")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)
    token = os.environ.get("FORGEJO_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺 FORGEJO_TOKEN（user:token 或 token）")
    release_dir = Path(args.release_dir)
    if not (release_dir / "manifest.json").is_file():
        raise SystemExit(f"{release_dir} 里没有 manifest.json —— 先打包")
    api = Forgejo(args.api_host or args.host, args.repo, token, public_host=args.host)
    result = publish(release_dir, api, args.host, args.repo)
    print("\n发好了。")
    stable = result.get("stable_install_url") or {}
    for platform, filename, command in (("macOS", "install.sh", "curl -fsSL {url} | sh"),
                                        ("Windows", "install.ps1", "irm {url} | iex")):
        fixed = stable.get(filename)
        url = fixed or f"{result['base']}/{filename}"
        print(f"  同事装（{platform}）：  " + command.format(url=url)
              + ("" if fixed else "   ⚠️ 这条带版本号，下一版要换"))
    print(f"  应用的更新源：      {args.host}/{args.repo}   （settings.update_source；默认值已是它）")
    status, repo = api.get("")
    if status == 200 and repo and repo.get("private"):
        print("  ⚠️ 这个仓库是私有的：没登录的人下不到资产。给同事只读 token（install.sh 认 AFS_AUTH=user:token），"
              "或把 release 放一个公开的只放发布件的仓库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
