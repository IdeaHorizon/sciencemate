"""Python 层自更新 —— 换的是**载荷**，不是壳。

## 更新单元

装好的应用里会变的那两块：`harness/`（core/shared/nodes/…）和 `static_ui/`。
CPython、随包 git、tectonic、壳、后端包 `app/` 都不在里面（见
`scripts/package/payload_release.py`）。一份载荷两个平台通用。

## 更新永远落数据根，不碰安装目录

公证过的 .app 一改就破签名；Windows 上正在跑的文件替换不掉。所以：

    <数据根>/payload/<版本>/{harness,static_ui}
    <数据根>/payload/current.json        {"version","previous","applied_at"}
    <数据根>/payload/staged.json         {"version"}   ← 下载验证后写，启动时消费
    <数据根>/updates/                    下载暂存

launcher 找载荷的顺序：显式指定 → **payload/current** → 随包 → 仓库。

## 切换只在下次启动时做

后端 `POST /update/install` 只做到「下载 → 验签 → 验摘要 → 安全解压 → 暂存」，
写一个 `staged.json` 就完了。**切换指针**由下一次 launcher 启动时做
（`apply_staged_at_launch`）：那时什么都没加载，原子 `os.replace` 一个指针文件，
旧版本留一份供回滚。Windows「文件被占用」在这条路上根本不出现。

## 重启 = 退出码 3

后端退出码 3 的意思只有一个：「请重新拉起我」。壳（Swift / C#）认这个数就够，
壳仍然是哑的。用 `os._exit`：不走 lifespan 收尾 —— socket 模式下 worker 本就
不跟后端死，新后端起来会接回它们（#784）。

## 信任

manifest 必须由发布私钥签（`app.release_pubkey`）；签名对不上 / 摘要对不上 /
tar 里有越界路径 / 顶层目录不是更新单元 / 版本标记不一致 —— 任何一条都**整份
拒收、不动现网、留一条错误**。没有"先跑着"这条路：自更新没有签名就是一条
对每台机器的远程执行通道。

## 谁不许自更新

harness 根下有 `.git` = 开发机的源码 checkout。它的"版本"由 git 管，不由载荷
管；给它推载荷等于把开发者的工作树换掉。机械判据，不看配置。

这个模块**不在导入期碰 `app.config`**：launcher 在 config 之前就要用它。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tarfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from app.release_pubkey import RELEASE_PUBLIC_KEY_B64

#: 后端用这个码退出 = 「壳，请重新拉起我」。Swift / C# 壳都认它。
RESTART_EXIT_CODE = 3

#: 内置的更新来源：**公开的发布件仓库**（只放 dmg/载荷/清单，不含源码）。
#:
#: 指公开仓库而不是源码仓库，是因为源码仓库是私有的 —— release 资产跟仓库同一个
#: 可见性，装了应用的同事没有 token 就 404，而"更新拉不到"这件事在界面上什么都不
#: 显示（更新是方便不是提醒），于是没有人会发现自己再也收不到更新了。
#: 用 settings.update_source 覆盖；私有源另配 update_source_token。
DEFAULT_UPDATE_SOURCE = "https://desktop-9el2944.taile9f15e.ts.net/wangd/sciencemate"

UNIT = ("harness", "static_ui")
#: 第二个归档（`manifest.extras`）里允许的顶层目录。和 `UNIT` 分开是**兼容**的需要：
#: 老客户端对第一个归档的顶层目录与 `manifest.unit` 都逐字从严，往那里加东西它会拒收；
#: 新东西走第二个归档 + 一个老客户端会忽略的可选键（见 #953）。
EXTRA_UNITS = ("shell", "app")
#: `manifest.extras.shell` 里允许的平台名 —— 和壳自报家门用的是同一套词。
SHELL_PLATFORMS = ("windows", "macos")
#: 附加归档里每个平台的壳目录下，哪个文件是「壳本身」（manifest.extras.shell 的 sha256 就是它的）。
#: 和 `scripts/package/payload_release.SHELL_BINARY` 是同一个答案，改一处必改另一处。
SHELL_BINARY = {"windows": "ScienceMate.exe", "macos": "ScienceMate"}
VERSION_MARKER = "PAYLOAD_VERSION"
MANIFEST_SCHEMA = 1
#: 载荷现在 ~10 MB。留一个数量级余量，但不给无界 —— 一个假 manifest 不该能把磁盘写满。
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """任何一步不成立都用它 —— 消息是给人看的一句话。"""


# ───────────────────────────────────────────────────────────── 布局

def payload_root(data_root: Path) -> Path:
    return Path(data_root) / "payload"


def pointer_path(data_root: Path) -> Path:
    return payload_root(data_root) / "current.json"


def staged_pointer_path(data_root: Path) -> Path:
    return payload_root(data_root) / "staged.json"


def apply_error_path(data_root: Path) -> Path:
    return payload_root(data_root) / "apply_error.txt"


@dataclass(frozen=True)
class Pointer:
    version: str
    previous: str | None = None
    applied_at: str = ""


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomically(path: Path, payload: dict) -> None:
    """写临时文件再 `os.replace` —— 崩在中间也不会留下半个指针。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_pointer(data_root: Path) -> Pointer | None:
    data = _read_json(pointer_path(data_root))
    if not data or not isinstance(data.get("version"), str) or not data["version"]:
        return None
    return Pointer(version=data["version"], previous=data.get("previous"),
                   applied_at=str(data.get("applied_at") or ""))


def _looks_like_a_payload(directory: Path) -> bool:
    return (directory / "harness" / "core" / "agent_loop.py").is_file() and \
        (directory / "harness" / VERSION_MARKER).is_file()


def active_payload_dir(data_root: Path) -> Path | None:
    """指针指着的那一版，且它真的完整 —— 否则当没有（回落随包那份）。"""
    pointer = read_pointer(data_root)
    if pointer is None:
        return None
    candidate = payload_root(data_root) / pointer.version
    return candidate if _looks_like_a_payload(candidate) else None


def version_of(harness_dir: Path | None) -> str | None:
    if harness_dir is None:
        return None
    try:
        text = (Path(harness_dir) / VERSION_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def is_self_updatable(harness_dir: Path | None) -> bool:
    """源码 checkout（有 .git）永远不自更新 —— 它的版本归 git 管。"""
    if harness_dir is None:
        return False
    return not (Path(harness_dir) / ".git").exists()


# ───────────────────────────────────────────────────────────── 版本

def parse_version(text: str) -> tuple[int, ...]:
    head = str(text).strip().split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for piece in head.split("."):
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group(0)) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, installed: str | None) -> bool:
    """没有版本标记的安装（打这套东西之前装的）—— 任何载荷都比它新，这是对的。"""
    if installed is None:
        return True
    return parse_version(candidate) > parse_version(installed)


# ───────────────────────────────────────────────────────────── 信任

def verify_signature(data: bytes, signature_b64: str,
                     public_b64: str | None = None) -> bool:
    """线上格式与 scripts/package/release_keys.py 一字不差（有交叉测试钉住）。

    公钥在**调用时**读模块属性，不绑在默认参数上 —— 绑定义时的值是"同一函数两
    采样时刻"：测试换不掉，将来换钥匙时热加载也换不掉。
    """
    public_b64 = public_b64 or RELEASE_PUBLIC_KEY_B64
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        public.verify(base64.b64decode(signature_b64.strip()), data)
        return True
    except Exception:
        return False


def parse_manifest(data: bytes, signature_b64: str,
                   public_b64: str | None = None) -> dict:
    """先验签，再解析。签名验的是**精确字节** —— 解析之后再验就给了改写的空间。"""
    if not verify_signature(data, signature_b64, public_b64):
        raise UpdateError("manifest 的签名对不上应用里的发布公钥 —— 拒收")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise UpdateError(f"manifest 不是合法 JSON：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise UpdateError(f"manifest schema 不是 {MANIFEST_SCHEMA}")
    for key, kind in (("version", str), ("archive", str), ("sha256", str), ("size", int)):
        if not isinstance(manifest.get(key), kind) or isinstance(manifest.get(key), bool):
            raise UpdateError(f"manifest.{key} 缺失或类型不对")
    if not _SHA256.fullmatch(manifest["sha256"]):
        raise UpdateError("manifest.sha256 不是 64 位十六进制")
    if manifest["size"] <= 0 or manifest["size"] > MAX_ARCHIVE_BYTES:
        raise UpdateError(f"manifest.size={manifest['size']} 超出允许范围")
    if list(manifest.get("unit") or []) != list(UNIT):
        raise UpdateError(f"manifest.unit={manifest.get('unit')} 不是更新单元 {list(UNIT)}")
    if "/" in manifest["archive"] or "\\" in manifest["archive"] or manifest["archive"].startswith("."):
        raise UpdateError("manifest.archive 必须是一个纯文件名")
    if "extras" in manifest:
        _validate_extras(manifest["extras"])
    if "server" in manifest:
        _validate_server(manifest["server"])
    return manifest


def _validate_server(server: object) -> None:
    """`manifest.server`：这一版的组织服务器包（`sciencemate-server-<ver>.tar.gz`）。

    **可选**键，老客户端不认识就忽略。组织服务器自己升级时只信它：包的摘要在签过名的
    manifest 里，下载下来对不上就整份拒收 —— 没有签名的自升级就是一条对每台服务器的
    远程执行通道（和桌面同一条理由）。
    """
    if not isinstance(server, dict):
        raise UpdateError("manifest.server 不是对象")
    for key, kind in (("archive", str), ("sha256", str), ("size", int), ("version", str),
                      ("org_protocol", int)):
        if not isinstance(server.get(key), kind) or isinstance(server.get(key), bool):
            raise UpdateError(f"manifest.server.{key} 缺失或类型不对")
    if not _SHA256.fullmatch(server["sha256"]):
        raise UpdateError("manifest.server.sha256 不是 64 位十六进制")
    if server["size"] <= 0 or server["size"] > MAX_ARCHIVE_BYTES:
        raise UpdateError(f"manifest.server.size={server['size']} 超出允许范围")
    if server["archive"] != f"sciencemate-server-{server['version']}.tar.gz":
        raise UpdateError(f"manifest.server.archive={server['archive']!r} 与版本 {server['version']} 对不上")


def _validate_extras(extras: object) -> None:
    """`manifest.extras` 的形状 —— 有就得完整、合法；坏的直接拒收整份 manifest。

    这是**可选**键：老客户端不认识它、直接忽略（所以这里的检查只影响新客户端）。
    形状与第一个归档同一套要求（纯文件名、64 位 hex、大小有上界），顶层目录只许
    `EXTRA_UNITS`，壳表只许 `SHELL_PLATFORMS`。manifest 整体签名，这块也在签名之内。
    """
    if not isinstance(extras, dict):
        raise UpdateError("manifest.extras 不是对象")
    for key, kind in (("archive", str), ("sha256", str), ("size", int)):
        if not isinstance(extras.get(key), kind) or isinstance(extras.get(key), bool):
            raise UpdateError(f"manifest.extras.{key} 缺失或类型不对")
    if not _SHA256.fullmatch(extras["sha256"]):
        raise UpdateError("manifest.extras.sha256 不是 64 位十六进制")
    if extras["size"] <= 0 or extras["size"] > MAX_ARCHIVE_BYTES:
        raise UpdateError(f"manifest.extras.size={extras['size']} 超出允许范围")
    if "/" in extras["archive"] or "\\" in extras["archive"] or extras["archive"].startswith("."):
        raise UpdateError("manifest.extras.archive 必须是一个纯文件名")
    units = extras.get("units")
    if not isinstance(units, list) or not units:
        raise UpdateError("manifest.extras.units 缺失或为空")
    for unit in units:
        if not isinstance(unit, str) or unit.split("/", 1)[0] not in EXTRA_UNITS:
            raise UpdateError(f"manifest.extras.units 里的 {unit!r} 顶层不在 {list(EXTRA_UNITS)}")
    shell = extras.get("shell", {})
    if not isinstance(shell, dict):
        raise UpdateError("manifest.extras.shell 不是对象")
    for platform, digest in shell.items():
        if platform not in SHELL_PLATFORMS or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise UpdateError(f"manifest.extras.shell[{platform!r}] 不合法")


# ───────────────────────────────────────────────────────────── 来源

@dataclass(frozen=True)
class Source:
    """三个 URL：manifest、它的签名、载荷。"""
    kind: str
    manifest_url: str
    signature_url: str
    archive_url: str | None   # 直接来源：由 manifest.archive 拼；release 来源：资产表里查
    #: 人能打开的那一页（release 的 html_url；直接来源就是 manifest 所在目录）。
    #: 「这次更新需要重新安装」时把它给用户 —— 不给地址的提示等于没提示。
    page_url: str | None = None

    def archive_url_for(self, name: str, assets: dict[str, str] | None = None) -> str:
        if self.archive_url:
            return self.archive_url.rsplit("/", 1)[0] + "/" + name
        if assets and name in assets:
            return assets[name]
        raise UpdateError(f"release 资产里没有 {name}")


class NothingPublishedYet(UpdateError):
    """发布页上最近几个 release 没有一个发完 —— 不是拒收，是还没有可装的。"""


#: 往回看几个 release。同一时刻最多一版在传；五个足够越过它，又不至于每次查更新都拖回
#: 一整页发布说明（实测五个 ≈ 20 KB，`releases/latest` ≈ 4.5 KB）。
LOOK_BACK = 5
MANIFEST, SIGNATURE = "manifest.json", "manifest.json.sig"


def what_a_desktop_installs(manifest: dict) -> list[str]:
    """桌面装一版要下的（`/update/install`）：载荷，带 extras 就连它一起。"""
    extras = manifest.get("extras")
    return [manifest["archive"], *([extras["archive"]] if extras else [])]


def what_a_server_installs(manifest: dict) -> list[str]:
    """组织服务器升一版要下的（`server_update.start`）。没有 server 项由调用方说。"""
    server = manifest.get("server")
    return [server["archive"]] if server else []


@dataclass(frozen=True)
class StillPublishing:
    """比找到的那版新、却还没传完的一个 release。"""
    tag: str
    missing: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.tag}（缺 {'、'.join(self.missing)}）"


@dataclass(frozen=True)
class Found:
    source: Source
    assets: dict[str, str]
    manifest: dict                  # 验过签的
    tag: str | None = None          # 直接来源没有 tag
    #: 比它新、还没传完的那几个，新的在前。
    still_publishing: tuple[StillPublishing, ...] = ()


def the_release_listing_url(host: str, owner: str, repo: str) -> str:
    """仓库网址 → 列 release 的 API 地址 —— 只在这里决定问谁。

    Forgejo/Gitea 的 API 在仓库同一个主机的 `/api/v1` 下；GitHub 的在另一个主机
    （api.github.com），响应形状一样（tag_name / draft / prerelease / assets[].browser_download_url
    / html_url），但它不认 `limit`、`draft`、`pre-release`，只认 `per_page`。
    个人版 0.5.6 起从 GitHub（IdeaHorizon/sciencemate）发，桌面查更新要问得到那儿。
    服务器端先滤一遍（省得拖回预发布的说明），客户端再滤一遍（老 Forgejo 不认这两个参数）。
    """
    if urlsplit(host).netloc.lower() in ("github.com", "www.github.com"):
        return f"https://api.github.com/repos/{owner}/{repo}/releases?per_page={LOOK_BACK}"
    return (f"{host}/api/v1/repos/{owner}/{repo}/releases"
            f"?limit={LOOK_BACK}&draft=false&pre-release=false")


def find_the_newest_installable(configured: str, get_json: Callable[[str], Any],
                                get_bytes: Callable[[str], bytes],
                                needs: Callable[[dict], list[str]], *,
                                via_this_host: bool = False) -> Found:
    """把一个更新源变成**这个调用方现在装得上的最新一版**。

    - 以 `manifest.json` 结尾 → 直接来源，签名与归档都在同一目录（列不了目录，不查齐不齐）；
    - 否则当作 Forgejo/Gitea 或 GitHub 仓库网址 → 列最近 LOOK_BACK 个正式 release，取第一个
      `manifest.json`、`.sig` 和 `needs(manifest)` 点名的归档都在的。

    ## 为什么不问 `releases/latest`

    发布不是一瞬间的事：release 先建出来，资产按文件名一个一个传（`publish_release.publish`），
    走 Funnel 可以传好几个小时、也会断在半路。2026-09-27 的 v0.5.5 建好时只有 SHA256SUMS 和
    Windows 安装器，`latest` 指着它，每台桌面查更新都报「最新 release 里没有 manifest.json」，
    直到有人手动把它改成预发布。而且 `manifest.json` 比载荷、服务器包都先到 —— 只查 manifest
    还会有一段「查得到、下不来」。没传完的那版还没到过任何人手里：照实说就是「现在能装的最新
    是上一版」，顺带说一句哪一版还在传（`still_publishing`）。

    ## 哪些跳过、哪些照旧报错

    只跳过**还没发完**的（草稿、预发布、缺文件）。文件都在、manifest 却验不过（签名对不上、
    这个客户端不认的格式）是这一版**本身**的事实，照旧抛出 —— 跳过它会把「有新版但你装不了」
    说成「已是最新」。

    `via_this_host`：下载地址用传进来的 host 拼，不用资产里记的对外地址（CI 走得通的
    host 和 Forgejo 的 ROOT_URL 不是一个）。
    """
    url = (configured or DEFAULT_UPDATE_SOURCE).strip().rstrip("/")
    if url.endswith(MANIFEST):
        source = Source("direct", url, url + ".sig", url, page_url=url.rsplit("/", 1)[0])
        manifest = parse_manifest(get_bytes(source.manifest_url),
                                  get_bytes(source.signature_url).decode("ascii", "replace"))
        return Found(source, {}, manifest)
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if not parts.scheme or not parts.netloc or len(segments) < 2:
        raise UpdateError(f"update_source={configured!r} 既不是 manifest.json 的 URL，也不是仓库网址")
    owner, repo = segments[-2], segments[-1]
    host = f"{parts.scheme}://{parts.netloc}"
    listed = get_json(the_release_listing_url(host, owner, repo))
    still_publishing: list[StillPublishing] = []
    for release in listed if isinstance(listed, list) else []:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name") or "?")
        assets = {a["name"]: (f"{host}/{owner}/{repo}/releases/download/{tag}/{a['name']}"
                              if via_this_host else a.get("browser_download_url"))
                  for a in release.get("assets") or []
                  if isinstance(a, dict) and a.get("name") and (via_this_host or a.get("browser_download_url"))}
        missing = [name for name in (MANIFEST, SIGNATURE) if name not in assets]
        if not missing:
            manifest = parse_manifest(get_bytes(assets[MANIFEST]),
                                      get_bytes(assets[SIGNATURE]).decode("ascii", "replace"))
            missing = [name for name in needs(manifest) if name not in assets]
            if not missing:
                page = release.get("html_url")
                return Found(Source("release", assets[MANIFEST], assets[SIGNATURE], None,
                                    page_url=page if isinstance(page, str) and page else None),
                             assets, manifest, tag, tuple(still_publishing))
        still_publishing.append(StillPublishing(tag, tuple(missing)))
    if not still_publishing:
        raise NothingPublishedYet(f"{host}/{owner}/{repo} 上还没有正式发布的 release")
    raise NothingPublishedYet(f"最近 {LOOK_BACK} 个 release 都还没传完："
                              + "；".join(str(s) for s in still_publishing))


def newer_than(still_publishing: tuple[StillPublishing, ...], installed: str | None) -> list[str]:
    """还在传的那几个里，比装着的新的（给人看的只有这些）。tag 是 `v<版本>`。"""
    return [s.tag for s in still_publishing if is_newer(s.tag.removeprefix("v"), installed)]


# ───────────────────────────────────────────────────────────── 状态

@dataclass
class UpdateStatus:
    installed_version: str | None
    installed_from: str            # payload / bundled / repo / unknown
    self_updatable: bool
    available_version: str | None = None
    staged_version: str | None = None
    source: str = ""
    reachable: bool = False
    error: str | None = None
    apply_error: str | None = None
    checked_at: str = ""
    notes: str = ""
    #: 装着的壳自报的家门（`<数据根>/shell.json`，壳启动时写）。None = 没报 / 报的读不懂。
    shell: dict | None = None
    #: 这次更新对壳意味着什么：`{"changed": bool | None, "needs_reinstall": bool}`。
    #: changed=None ＝ 判不了（壳没自报、或 manifest 没带这个平台的壳）。只在有新版本时给。
    shell_update: dict | None = None
    #: needs_reinstall 时，人该去哪下安装包 —— 不给地址的提示等于没提示。
    reinstall_url: str | None = None
    #: 比装着的新、还没传完的 release（tag，新的在前）。这时答的是「现在能装的最新一版」，
    #: 不说这一句，「已是最新」就把「更新的那版还在路上」藏掉了。
    still_publishing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


#: 壳自报家门时必须带的字段。少一个就当没报 —— 半份介绍比没介绍更容易被误读。
SHELL_DECLARATION_KEYS = ("platform", "path", "sha256", "size", "self_replace")


def shell_declaration_path(data_root: Path) -> Path:
    return Path(data_root) / "shell.json"


def read_shell_declaration(data_root: Path) -> dict | None:
    """壳启动时写下的「我是哪一份」；读不到或读不懂一律 None。

    ## 为什么这一份由壳写、后端只读

    自更新只换 harness 与界面，换不了壳（#953）。后端要回答「这次更新含不含壳的改动、
    装着的壳会不会自己换」，前提是知道装着的壳到底是哪一份 —— 而只有壳自己最清楚
    它的路径、它的字节、它有没有自替换能力。后端去猜（比如按版本号推断）就是第二个
    真相源，分叉时不报错。

    ## 为什么不信半份

    字段少一个就当没报：`self_replace` 缺了却把 `sha256` 当真，后端会得出「壳变了、
    但它会不会自己换我不知道」这种无法行动的结论。宁可整份 None，让上层按「不知道」走。
    """
    path = shell_declaration_path(data_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or any(key not in raw for key in SHELL_DECLARATION_KEYS):
        return None
    if not isinstance(raw["sha256"], str) or not _SHA256.fullmatch(raw["sha256"]):
        return None
    if not isinstance(raw["self_replace"], bool):
        return None
    return {key: raw[key] for key in (*SHELL_DECLARATION_KEYS, "declared_at") if key in raw}


def describe_installed(data_root: Path, harness_dir: Path | None) -> tuple[str | None, str]:
    if harness_dir is None:
        return None, "unknown"
    active = active_payload_dir(data_root)
    if active is not None and Path(harness_dir).resolve() == (active / "harness").resolve():
        return version_of(harness_dir), "payload"
    if (Path(harness_dir) / ".git").exists():
        return version_of(harness_dir), "repo"
    return version_of(harness_dir), "bundled"


def staged_version(data_root: Path) -> str | None:
    data = _read_json(staged_pointer_path(data_root))
    version = (data or {}).get("version")
    if not isinstance(version, str) or not version:
        return None
    return version if _looks_like_a_payload(payload_root(data_root) / "staged" / version) else None


def check_for_update(data_root: Path, harness_dir: Path | None, configured_source: str,
                     http_get_json: Callable[[str], Any],
                     http_get_bytes: Callable[[str], bytes]) -> tuple[UpdateStatus, dict | None, Source | None, dict[str, str]]:
    installed, installed_from = describe_installed(data_root, harness_dir)
    status = UpdateStatus(installed_version=installed, installed_from=installed_from,
                          self_updatable=is_self_updatable(harness_dir),
                          staged_version=staged_version(data_root),
                          checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          # 壳自报的家门跟着每一份状态走：没报就是 None，上层按「不知道」处理。
                          shell=read_shell_declaration(data_root))
    try:
        status.apply_error = apply_error_path(data_root).read_text(encoding="utf-8").strip() or None
    except OSError:
        status.apply_error = None
    if not status.self_updatable:
        status.error = "这份 harness 是源码 checkout（有 .git），版本归 git 管，不自更新"
        return status, None, None, {}
    try:
        found = find_the_newest_installable(configured_source, http_get_json, http_get_bytes,
                                            what_a_desktop_installs)
    except UpdateError as exc:
        status.error = str(exc)
        return status, None, None, {}
    except Exception as exc:  # 网络、超时、404 —— 拉不到就是拉不到，不影响使用
        status.error = f"拿不到更新信息：{type(exc).__name__}: {exc}"
        return status, None, None, {}
    source, assets, manifest = found.source, found.assets, found.manifest
    status.source = source.manifest_url
    status.reachable = True
    status.still_publishing = newer_than(found.still_publishing, installed)
    status.notes = str(manifest.get("notes") or "")
    if is_newer(manifest["version"], installed):
        status.available_version = manifest["version"]
        status.shell_update = describe_the_shell_update(status.shell, manifest)
        if status.shell_update["needs_reinstall"]:
            status.reinstall_url = source.page_url
    return status, manifest, source, assets


def describe_the_shell_update(declared: dict | None, manifest: dict) -> dict:
    """这次更新对壳意味着什么 —— 只用两份事实，不猜。

    两份事实：装着的壳自报的家门（`shell.json`，#953 ①）和新版 manifest 里带的壳哈希
    （`extras.shell`，#953 ②）。

    - 两份都有且哈希不同 → `changed=True`；壳不会自己换（`self_replace=False`）→
      `needs_reinstall=True`：**点「现在更新」拿不到这次的壳**，得重装，横幅得如实说并给地址。
    - 两份都有且哈希相同 → `changed=False`：照常更新。
    - 任何一份缺席 → `changed=None`：判不了。这时**不**说需要重装（说了可能是假警报），
      也不说不需要 —— 横幅按老样子走。0.4.5 及更早的壳不自报家门，就落在这一档；
      它们本来也换不了壳，这一档的诚实答案就是「不知道」。

    壳会自换（④ 落地、`self_replace=True`）时，changed=True 也不需要重装 —— 那时更新
    本身就会把壳换掉。
    """
    remote = (manifest.get("extras") or {}).get("shell") or {}
    if not declared or declared.get("platform") not in remote:
        return {"changed": None, "needs_reinstall": False}
    changed = remote[declared["platform"]] != declared.get("sha256")
    return {"changed": changed, "needs_reinstall": bool(changed and not declared.get("self_replace"))}


# ───────────────────────────────────────────────────────────── 下载 + 暂存

def _reject_unsafe_members(tar: tarfile.TarFile, allowed: tuple[str, ...] = UNIT) -> None:
    """顶层只许 `allowed`；`filter="data"` 另外挡绝对路径 / `..` / 越界链接 / 设备文件。

    第一个归档 `allowed=UNIT`（顶层必须**正好**是那两个）；第二个归档 `allowed=EXTRA_UNITS`
    （顶层是它的**子集**即可 —— 一个版本可以只带壳不带 app）。
    """
    tops: set[str] = set()
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
            raise UpdateError(f"载荷里有越界路径：{name}")
        if member.issym() or member.islnk():
            raise UpdateError(f"载荷里有链接：{name} —— 更新单元里不该有")
        if member.isdev() or member.isfifo():
            raise UpdateError(f"载荷里有设备/管道文件：{name}")
        tops.add(name.split("/", 1)[0])
    if allowed is UNIT:
        if tops != set(UNIT):
            raise UpdateError(f"载荷顶层目录 {sorted(tops)} ≠ 更新单元 {list(UNIT)}")
    elif not tops or not tops <= set(allowed):
        raise UpdateError(f"附加归档顶层目录 {sorted(tops)} 不在 {list(allowed)} 之内")


def fetch_verified(target: Path, url: str, expected: dict,
                    http_stream: Callable[[str, Callable[[bytes], None]], None], what: str) -> None:
    """流式下到 `target`，边下边算摘要；大小、sha256 都得和签过名的 manifest 一致。"""
    digest = hashlib.sha256()
    received = 0
    with target.open("wb") as out:
        def _sink(chunk: bytes) -> None:
            nonlocal received
            received += len(chunk)
            if received > expected["size"]:
                raise UpdateError(f"{what}下载超过 manifest 声明的大小 {expected['size']}")
            digest.update(chunk)
            out.write(chunk)
        http_stream(url, _sink)
    if received != expected["size"]:
        raise UpdateError(f"{what}下载 {received} 字节，manifest 说 {expected['size']}")
    if digest.hexdigest() != expected["sha256"]:
        raise UpdateError(f"{what}的 sha256 与签过名的 manifest 不一致 —— 拒收")


def download_and_stage(data_root: Path, manifest: dict, archive_url: str,
                       http_stream: Callable[[str, Callable[[bytes], None]], None],
                       extras_url: str | None = None) -> Path:
    """下载 → 摘要 → 安全解压 → 校验标记 → 暂存。任何一步失败：清干净、不动现网。

    manifest 带 `extras` 且给了 `extras_url` 时，第二个归档一起下、一起验、解到
    `<staging>/extras/`。**两个归档要么都到位、要么都不算**：主载荷已解好而附加归档
    坏了，整个暂存作废 —— 半份更新（新 harness + 旧壳）正是 #953 要消灭的那种状态。
    """
    version = manifest["version"]
    updates = Path(data_root) / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    archive = updates / manifest["archive"]
    extras = manifest.get("extras") if extras_url else None
    extras_archive = updates / extras["archive"] if extras else None
    try:
        fetch_verified(archive, archive_url, manifest, http_stream, "载荷")

        staging = payload_root(data_root) / "staging" / version
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with tarfile.open(archive, "r:gz") as tar:
            _reject_unsafe_members(tar)
            tar.extractall(staging, filter="data")
        if not _looks_like_a_payload(staging):
            raise UpdateError("解出来的载荷缺 core/agent_loop.py 或 PAYLOAD_VERSION")
        marked = version_of(staging / "harness")
        if marked != version:
            raise UpdateError(f"载荷里的版本标记 {marked!r} ≠ manifest 的 {version!r}")

        if extras is not None:
            fetch_verified(extras_archive, extras_url, extras, http_stream, "附加归档")
            extras_dir = staging / "extras"
            extras_dir.mkdir()
            with tarfile.open(extras_archive, "r:gz") as tar:
                _reject_unsafe_members(tar, EXTRA_UNITS)
                tar.extractall(extras_dir, filter="data")
            for platform, digest in (extras.get("shell") or {}).items():
                binary = extras_dir / "shell" / platform / SHELL_BINARY[platform]
                if not binary.is_file():
                    raise UpdateError(f"附加归档说带了 {platform} 的壳，解出来却没有 {binary.name}")
                if hashlib.sha256(binary.read_bytes()).hexdigest() != digest:
                    raise UpdateError(f"{platform} 壳的 sha256 与 manifest.extras.shell 不一致 —— 拒收")
            # 带了后端 app/ 就得是启动器接得过去的那种（launcher.hand_over_to_the_payload_app
            # 认 `app/launcher.py`）：半个 app 包比没有更糟 —— 起不来的后端没人能重装。
            if "app" in (extras.get("units") or []) and not (extras_dir / "app" / "launcher.py").is_file():
                raise UpdateError("附加归档说带了后端 app/，解出来却没有 app/launcher.py —— 拒收")

        staged = payload_root(data_root) / "staged" / version
        shutil.rmtree(staged, ignore_errors=True)
        staged.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, staged)
        _write_json_atomically(staged_pointer_path(data_root),
                               {"version": version, "staged_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return staged
    finally:
        archive.unlink(missing_ok=True)
        if extras_archive is not None:
            extras_archive.unlink(missing_ok=True)
        shutil.rmtree(payload_root(data_root) / "staging", ignore_errors=True)


# ───────────────────────────────────────────────────────────── 启动时切换

def apply_staged_at_launch(data_root: Path) -> str | None:
    """有暂存就切过去。只在启动时调 —— 那时什么都没加载。

    失败不抛：launcher 不能因为一次坏更新起不来。错误写进 `apply_error.txt`，
    由 `GET /update` 报出来；暂存目录留着供人查。
    """
    data_root = Path(data_root)
    data = _read_json(staged_pointer_path(data_root))
    version = (data or {}).get("version")
    if not isinstance(version, str) or not version:
        return None
    staged = payload_root(data_root) / "staged" / version
    try:
        if not _looks_like_a_payload(staged):
            raise UpdateError(f"暂存的 {version} 不完整（缺 agent_loop.py 或版本标记）")
        problem = why_the_payload_cannot_start(staged)
        if problem:
            raise UpdateError(
                f"{version} 的后端在这份安装上起不来（{problem}）—— 没有切过去，"
                "当前版本原样继续。这一版要重新下载安装包。")
        target = payload_root(data_root) / version
        if target.exists():
            shutil.rmtree(target)
        os.replace(staged, target)
        current = read_pointer(data_root)
        previous = current.version if current and current.version != version else (current.previous if current else None)
        _write_json_atomically(pointer_path(data_root), {
            "version": version, "previous": previous,
            "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        staged_pointer_path(data_root).unlink(missing_ok=True)
        apply_error_path(data_root).unlink(missing_ok=True)
        _prune(data_root, keep={version, previous})
        return version
    except Exception as exc:
        try:
            apply_error_path(data_root).write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        except OSError:
            pass
        return None



#: 探一次「载荷里的后端能不能被 import」要多久算超时。它只做 import，不开端口、不连库。
_START_PROBE_TIMEOUT_S = 90


def why_the_payload_cannot_start(payload_dir: Path) -> str | None:
    """载荷里的后端 `app/` 在**这份安装的解释器**上 import 得起来吗？起得来返 None。

    ## 为什么必须问

    载荷带的是**代码**，不带 `site-packages`（CPython 和依赖几个月不动一次，走整包
    重装）。所以一次改了第三方依赖的更新，代码到了、库没到：

        2026-09-16 实测 0.4.6 → 0.5.0 —— 0.5.0 的后端把鉴权换成 PyJWT（`import jwt`），
        而 0.4.6 那份安装里只有 `jose`。切换成功、指针写下、`uvicorn` 起 `app.main` 时
        `ModuleNotFoundError: No module named 'jwt'`。**应用从此起不来**：指针已经指向
        新载荷，下次启动还是同一条路。用户看到的是「后端退出了」，且重启无效。

    `_looks_like_a_payload` 问的是「文件齐不齐」，答不了这个 —— 文件全都在，是**这台
    机器**缺库。判据只能是真 import 一次。

    ## 为什么是子进程

    这里是启动最早期，`app.*` 只加载了 launcher。在本进程 import 载荷的 `app.main`
    会把半个后端拉进来且撤不回去；失败之后我们要**原样继续跑旧版**，所以探测必须发生
    在一个可以整个丢掉的进程里。

    探不动（超时 / 解释器找不到）不当失败：那是探针的问题，不是载荷的问题，按老规矩
    「守不住就如实说」交给下一道闸，别把一次抖动变成一次拒绝更新。
    """
    app_dir = payload_dir / "extras" / "app"
    if not (app_dir / "launcher.py").is_file():
        return None      # 这份载荷没带后端 app/，换不换后端这件事不存在
    import subprocess
    import sys

    # 探的是**接管之后真正会被加载的东西**：`app.launcher` 由 launcher 自己 import，
    # `app.main` 由 uvicorn 按字符串加载（`app.main:app`）—— 0.4.6 那次砖掉正是死在
    # 后者。`main.py` 不在就只探前者：它不在的载荷根本走不到 uvicorn，那是另一道闸的事。
    modules = ["app.launcher"] + (["app.main"] if (app_dir / "main.py").is_file() else [])
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c",
             "import importlib, sys\n"
             "sys.path.insert(0, sys.argv[1])\n"
             "for name in sys.argv[2:]: importlib.import_module(name)\n",
             str(app_dir.parent), *modules],
            capture_output=True, text=True, timeout=_START_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None      # 探针自己不行 —— 不替载荷背这口锅
    if done.returncode == 0:
        return None
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    return tail[-1][:200] if tail else f"exit={done.returncode}"

def _prune(data_root: Path, keep: set[str | None]) -> None:
    """只留当前 + 上一版。别的版本目录删掉 —— 每次更新 30 MB，不清会一直长。"""
    root = payload_root(data_root)
    for child in root.iterdir() if root.is_dir() else []:
        if child.is_dir() and child.name not in keep and child.name not in ("staged", "staging"):
            shutil.rmtree(child, ignore_errors=True)


def rollback(data_root: Path) -> str | None:
    """指回上一版（若还在）。下次启动生效。"""
    current = read_pointer(data_root)
    if current is None or not current.previous:
        return None
    if not _looks_like_a_payload(payload_root(data_root) / current.previous):
        return None
    _write_json_atomically(pointer_path(data_root), {
        "version": current.previous, "previous": None,
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return current.previous


# ───────────────────────────────────────────────────────────── 重启

def schedule_restart(delay_s: float = 0.5, _exit: Callable[[int], None] = os._exit) -> None:
    """回复送出去之后再退。`os._exit`：不走收尾 —— socket 模式下 worker 不跟后端死。"""
    timer = threading.Timer(delay_s, lambda: _exit(RESTART_EXIT_CODE))
    timer.daemon = True
    timer.start()
