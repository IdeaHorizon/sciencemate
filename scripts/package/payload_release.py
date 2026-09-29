"""打一份**可自更新的载荷** —— 应用里会变的那部分，签名后发出去。

## 更新单元是什么

不是整个 .app / 安装目录，是它里面**会变的那两块**：

    harness/     core/ shared/ nodes/ platform_runtime.py …   （约 29 MB）
    static_ui/   Next.js 静态导出                              （约 3 MB）

CPython、随包 git、tectonic、壳 —— 不在更新单元里。它们几个月不动一次，动了走
整包重装。后端包 `app/` 也不在（v1）：它是**执行更新的那个进程**，自己换自己是
另一个问题，等有了不可变引导层再做。

**一份载荷两个平台通用**：里面全是 Python 源码和静态文件，Mac 上打一次，Windows
装的应用拉的是同一份。

## 从哪来

从打包器**已经装配好的那两个目录**打包（`stage_into_the_package` 之后）。这样更新
里的内容与一份全新安装逐字节一致 —— 不另写一份"哪些文件算 harness"的清单，
那份清单分叉的时候两边都不报错。

## 版本

唯一来源：仓库根 `pyproject.toml` 的 `version`。打包器的 Info.plist、dmg 文件名、
这里的 manifest、装好的应用自报的版本 —— 全从这一处读。2026-09-08 之前是三份手写
（0.1.0 / 0.1.0 / 0.3.0），一题三答。

载荷里写一个 `harness/PAYLOAD_VERSION` 标记：装好的应用靠它知道自己是哪一版
（没有它 = 老版本 = 任何更新都比它新，这是对的）。

## 签名

`manifest.json` 由发布机上的 ed25519 私钥签（见 `release_keys.py`）。**没有私钥
时如实降级**：照样出载荷，但明说客户端会拒收 —— 不假装签了。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def _sibling(name: str):
    """按文件路径装载同目录的模块，**不碰 sys.path、不依赖包名**。

    仓库根和 platform/backend 各有一个叫 `scripts` 的包。谁先被 import，`sys.modules`
    里的 `scripts` 就是谁 —— 之后用包名 import 兄弟模块会解析到**错的那个包**。按路径
    装载绕开整件事。

    历史注记：这里原本还有第二条理由 —— 后端测试的扫盘 helper 会把打包器当模块
    `exec` 一遍来问"构建产物写在哪"，于是打包器任何导入期副作用都会让**每一条扫盘闸
    一起报错**（2026-09-08 实测：全量 8 红、单跑全绿）。那条路已随 #878 删除：扫盘语料
    改成问 `git ls-files`，`dist/` 由 `.gitignore` 排除，没人再 exec 打包器。
    """
    import importlib.util

    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"afs_package_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

release_keys = _sibling("release_keys")

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "platform" / "backend"

#: 更新单元里的顶层目录。客户端解压后**只认这几个**，多出来的当篡改拒收。
UNIT = ("harness", "static_ui")
MANIFEST_SCHEMA = 1
VERSION_MARKER = "PAYLOAD_VERSION"


def the_version() -> str:
    """仓库根 pyproject.toml 的 version —— 版本号只在这里回答。"""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(data["project"]["version"]).strip()
    if not version:
        raise SystemExit("pyproject.toml 里没有 project.version")
    return version


def the_build_id() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=REPO,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def write_the_version_marker(staged_harness: Path, version: str) -> Path:
    marker = staged_harness / VERSION_MARKER
    marker.write_text(version + "\n", encoding="utf-8")
    return marker


#: 归档里的权限位。**写死，不取磁盘上的** —— 见 `_add_tree`。
_FILE_MODE, _DIR_MODE = 0o644, 0o755


#: 每个平台的壳目录里，哪个文件是「壳本身」。后端拿它的 sha256 和壳自报的比。
SHELL_BINARY = {"windows": "ScienceMate.exe", "macos": "ScienceMate"}


def _digest_of_the_shell(shell_dir: Path) -> str:
    """壳目录里主二进制的 sha256 —— 和壳启动时自报的 `shell.json.sha256` 是同一把尺。"""
    for name in SHELL_BINARY.values():
        candidate = shell_dir / name
        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
    raise SystemExit(f"{shell_dir} 里没有壳本身（{sorted(SHELL_BINARY.values())} 之一）")


def _add_tree(tar: tarfile.TarFile, root: Path, arcname: str) -> int:
    """按排好序的路径加进去 —— 同样的输入出同样的字节。

    uid/gid/uname/gname/mtime **和 mode** 全部归一化。mode 那一项是 2026-09-08
    补的：磁盘上的权限位由构建进程的 umask 决定（后端 `assembly.install()` 就设
    `umask(0o077)`），于是同一份代码在不同 umask 下出不同的 sha256 —— 而那个
    sha256 正是 manifest 里被签名保护的完整性凭据。"可复现"这个保证当时是假的。
    """
    count = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in ("__pycache__",) or part.endswith(".pyc") for part in rel.parts):
            continue
        info = tar.gettarinfo(str(path), arcname=f"{arcname}/{rel.as_posix()}")
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        info.mode = _FILE_MODE if path.is_file() else _DIR_MODE
        if path.is_file():
            with path.open("rb") as handle:
                tar.addfile(info, handle)
            count += 1
        else:
            tar.addfile(info)
    return count


#: 第二个归档里允许的顶层目录。老客户端只认 `UNIT`，所以这些**不能**混进第一个归档里。
EXTRA_UNITS = ("shell", "app")


def _tar_trees(trees: dict[str, Path]) -> tuple[bytes, dict[str, int]]:
    """把若干棵树按给定的归档名打成一个 tar.gz，返回字节和每棵树的文件数。

    **gzip 那层自己写死**，不交给 `mode="w:gz"`。tar 里每一条的 mtime 由 `_add_tree`
    归一化，但 gzip 容器头（第 4..7 字节）另有一个 mtime，`w:gz` 会填**当时的墙钟**
    —— 于是同一棵树，两次构建只要跨过一个整秒就出两个 sha256。而那个 sha256 正是
    manifest 里被签名保护的完整性凭据："发的就是审过的那份"当时核对不了。

    2026-09-14 实测：同一秒内两次构建字节相同，隔 1.1 秒再构建就不同，差的正好是
    头里那 4 个字节（`...7c7fa76a...` / `...7d7fa76a...`）。全量并行跑时
    `test_the_archive_is_reproducible` 偶发转红，那不是测试脆，是这个 bug 透出来了。

    `filename=""` 一并把 gzip 头里的 FNAME 字段关死：那是第二个跟构建环境有关的
    输入（`GzipFile` 默认取 fileobj 的 `.name`）。
    """
    buffer = io.BytesIO()
    counts: dict[str, int] = {}
    with gzip.GzipFile(filename="", fileobj=buffer, mode="wb", compresslevel=6, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for arcname, root in trees.items():
                counts[arcname] = _add_tree(tar, root, arcname)
    return buffer.getvalue(), counts


def _the_org_protocol() -> int:
    """按路径读 `platform/backend/app/pro/org_protocol.py` 的协议号 —— 只在那一处定义（专业版才有服务器包）。"""
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[2] / "platform" / "backend" / "app" / "pro" / "org_protocol.py"
    if not path.is_file():
        raise SystemExit("服务器包是专业版的：这棵树里没有 app/pro/org_protocol.py，打不了带服务器包的载荷")
    spec = importlib.util.spec_from_file_location("afs_org_protocol", path)
    module = importlib.util.module_from_spec(spec)
    # 先登记再执行：它里面有 `@dataclass`（见 `_the_edition_module` 那段 09-16 的教训）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return int(module.ORG_PROTOCOL)


def _the_edition_module():
    """按路径装载 `platform/backend/app/edition.py`：发行文件的格式只在那一个文件里定，
    打包器不抄第二份。它没有任何 app 内部 import，所以在系统 python3 上也装得起来。"""
    import importlib.util
    import sys

    cached = sys.modules.get("afs_edition")
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parents[2] / "platform" / "backend" / "app" / "edition.py"
    spec = importlib.util.spec_from_file_location("afs_edition", path)
    module = importlib.util.module_from_spec(spec)
    # 先登记再执行：模块里有 `@dataclass`，而 dataclasses 解析字符串注解时会
    # `sys.modules.get(cls.__module__).__dict__` —— 没登记就是 None，CI 上 11 条
    # 打载荷的测试因此一起红（2026-09-16）。这是按路径装载模块的标准写法，
    # `_sibling` 里没这一步只是因为它装的模块里没有 dataclass。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_the_edition(directory: Path, edition: str, update_source: str = "") -> Path:
    """把发行写进 bundle（`<Resources>/edition.json`）—— 两个打包器共用这一处。

    写的位置是 `launcher` 那个解释器的 `sys.prefix.parent`，读它的是 `app/edition.py`；
    放错一层就静默变成个人版（EXEC_PLAN_TWO_EDITIONS §1），所以装完自检要问一次
    `/api/v1/capabilities` 核对。
    """
    target = _the_edition_module().write_edition(directory, edition, update_source)
    print(f"  {target}（{edition}{'，更新源 ' + update_source if update_source else ''}）")
    return target


#: 个人版是开源发行：包里的字节必须等于公开树里的字节。这两个目录在，就还是内部树。
PRO_EDITION_DIRS = ("platform/backend/app/pro", "platform/frontend/src/pro")
#: 装出来的包里不该有的东西（相对包根的后缀）—— 后端专业版包、专业版页面的静态导出。
PRO_EDITION_TRACES = ("app/pro/__init__.py", "static_ui/organisation.html")
HOW_TO_EXPORT = "python3 scripts/package/export_public_tree.py <空目录> --git-init"


def the_personal_edition_is_built_from_the_public_tree(repo: Path, edition: str) -> None:
    """个人版只从导出的公开树打 —— 两个打包器动手前都先问这一句。

    2026-09-29 挂载刚打好的个人版 dmg：`edition.json` 写着 personal，site-packages 里却有
    完整的 `app/pro/`（28 个 .py，明文），静态界面里编进了组织页与登录页；自更新的 extras
    归档同样带着。两个打包器都是「`platform/backend/app` 里 git 跟踪的文件」照拷，发行只差
    `edition.json` 与自更新源（EXEC_PLAN_TWO_EDITIONS §1 当年就是这么定的）。分野
    （PR #1169–#1172）守住了源码树与开源导出，没守住装出来的包 —— 任何人下载免费的个人版
    就拿到了专业版的后端源码。

    正解不是在打包器里再排除一遍（那是第二份「什么算专业版」，与 `export_public_tree.py`
    的排除表分叉时两边都不报错），而是**个人版包 = 公开树**：先导出，再在导出树里打。
    从含专业版的内部树打个人版一律拒绝；专业版照旧从内部树打。
    """
    if edition != "personal":
        return
    present = [d for d in PRO_EDITION_DIRS if (repo / d).is_dir()]
    if present:
        raise SystemExit(
            "❌ 个人版不从内部树打：这棵树里还有专业版（" + "、".join(present) + "）。\n"
            "   个人版是开源发行，包里的字节必须等于公开树里的字节 —— 先导出公开树，再在里面打：\n"
            f"   {HOW_TO_EXPORT}")


def the_package_carries_no_pro_edition(package_root: Path, edition: str) -> None:
    """装完自检的第二道：个人版包里不得有专业版的痕迹。

    入口那道拒绝的是「从哪棵树打」；这一道问的是「打出来的东西里有没有」—— 排除表漏了
    一个目录、或者装配时从别处混进来，只有翻包才看得见。
    """
    if edition != "personal":
        return
    found = sorted({str(path.relative_to(package_root))
                    for trace in PRO_EDITION_TRACES
                    for path in package_root.rglob(trace)})
    if found:
        raise SystemExit("❌ 个人版包里有专业版的痕迹：" + "；".join(found[:6])
                         + "\n   这个包别发。个人版只从公开树打：" + HOW_TO_EXPORT)
    print("  ✅ 包里没有专业版的痕迹（app/pro、组织页）")


#: 专业版才要的两样，和界面/harness 一样装进 `app/` 里。
#:
#: 为什么是 `app/` 而不是 `Resources/`：自更新的第二个归档只允许 `shell` 和 `app`
#: 两个顶层单元（`self_update.EXTRA_UNITS`），**老客户端对别的单元会拒收整份
#: manifest**。放进 `Resources/` 的东西只有重装才拿得到；放进 `app/` 的随自更新
#: 一起到。2026-09-20 发现：0.5.1 新加的 asyncssh 与服务器包都在 `Resources/`，
#: 于是从 0.5.0 自更新上来的那份点「建立组织」必然失败。
SERVER_BUNDLE_DIR = "server_bundle"
VENDOR_DIR = "_vendor"
#: 随 `app/` 一起分发的第三方包。载荷不带 site-packages，所以**新加的依赖只能
#: 这样送到自更新上来的安装里**。只收纯 Python 的：带原生扩展的要按平台分发，
#: 而一份载荷同时服务 Mac 与 Windows。
VENDORED = ("asyncssh",)


def stage_the_pro_only_things(backend: Path, edition: str,
                              server_bundle: Path | None) -> None:
    """专业版多的两样：远程安装用的 ssh 库，和它要送上去的服务器包。"""
    if edition != "pro":
        return
    print("── 把 ssh 库与服务器包装进 app/（自更新才带得走）")
    vendor = backend / "app" / VENDOR_DIR
    shutil.rmtree(vendor, ignore_errors=True)
    vendor.mkdir(parents=True)
    for name in VENDORED:
        _vendor_one(backend, vendor, name)
    if server_bundle is not None:
        target = backend / "app" / SERVER_BUNDLE_DIR
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        shutil.copy2(server_bundle, target / server_bundle.name)
        print(f"  {server_bundle.name} → app/{SERVER_BUNDLE_DIR}/"
              f"（{server_bundle.stat().st_size // 1024 // 1024} MB）")


def the_uv_executable() -> str:
    """uv 在哪 —— **这个问题只在这里回答一次**。

    uv 是构建机的工具，不进包。它在 PATH 上不是理所当然的：Windows 构建机把它装在
    `%USERPROFILE%\\.local\\bin\\uv.exe`，而从 ssh 进去的非登录 shell 里 PATH 没有它
    —— 2026-09-21 打专业版 Windows 包时，`subprocess.run(["uv", ...])` 就在这一下
    `WinError 2` 了。Mac 上 uv 一直在 PATH 里，所以这个缺陷在 Mac 那条路上永远看不见。
    """
    found = shutil.which("uv")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    for candidate in (home / ".local" / "bin" / "uv.exe", home / ".local" / "bin" / "uv"):
        if candidate.is_file():
            return str(candidate)
    raise SystemExit("找不到 uv —— 构建机要先装 uv（它只用于打包，不进产物）")


def _vendor_one(backend: Path, vendor: Path, name: str) -> None:
    """按锁文件里的版本，把一个包装进 `vendor/`。

    用 `uv pip install --target`，不去某个 `.venv` 里拷：venv 的布局按平台分家
    （POSIX 是 `lib/pythonX.Y/site-packages`，Windows 是 `Lib/site-packages`），
    而且**打 Windows 包的机器上后端 venv 根本不存在** —— 2026-09-20 真打一次才
    撞上。版本取自 `backend/uv.lock`，所以随包走的那份与装进应用的是同一版。
    """
    version = _locked_version(backend, name)
    subprocess.run([the_uv_executable(), "pip", "install", "--no-deps", "--quiet",
                    "--target", str(vendor), f"{name}=={version}"], check=True)
    landed = vendor / name
    if not landed.is_dir():
        raise SystemExit(f"uv 把 {name} 装进 {vendor} 了，但没有 {name}/ —— 布局不是预期的那样")
    native = [*landed.rglob("*.so"), *landed.rglob("*.pyd"), *landed.rglob("*.dylib")]
    if native:
        raise SystemExit(
            f"{name} 带原生扩展（{native[0].name}），不能这样分发 —— "
            "一份载荷要同时服务 Mac 与 Windows")
    for junk in vendor.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)
    files = sum(1 for _ in landed.rglob("*") if _.is_file())
    print(f"  {name}=={version} → app/{VENDOR_DIR}/{name}（{files} 个文件）")


def _locked_version(backend: Path, name: str) -> str:
    """`backend/uv.lock` 里锁的是哪一版 —— 和装进应用的那份同一个出处。"""
    lock = backend / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    marker = f'name = "{name}"'
    at = text.find(marker)
    if at == -1:
        raise SystemExit(f"{lock} 里没有 {name} —— 它还不是后端的依赖？")
    for line in text[at:].splitlines()[1:6]:
        if line.startswith("version = "):
            return line.split('"')[1]
    raise SystemExit(f"{lock} 里 {name} 那一段读不出 version")


def build_the_payload(staged_harness: Path, staged_ui: Path, out_dir: Path,
                      *, version: str | None = None, notes: str = "",
                      extras: dict[str, Path] | None = None,
                      edition: str = "personal",
                      server_bundle: Path | None = None) -> dict:
    """出三个文件：`payload-<ver>.tar.gz`、`manifest.json`、`manifest.json.sig`。
    给了 `extras` 再多出一个 `extras-<ver>.tar.gz`。

    返回 manifest（dict）外加 `_paths`。没有私钥时不出 .sig，并把 `signed=False`
    印出来 —— 客户端会拒收这份载荷，这里不假装。

    ## 为什么壳和后端 `app/` 走**第二个**归档，而不是并进第一个

    已装客户端有两道故意从严的闸：`manifest.unit` 必须逐字等于 `("harness","static_ui")`，
    第一个归档的顶层目录也必须逐字相等（`self_update.parse_manifest` /
    `_reject_unsafe_members`）。往里加新单元，0.4.4 / 0.4.5 的客户端会当场拒收 ——
    而且 `check_for_update` 把拒收吞成 `status.error`，横幅静默消失，老用户从此收不到
    更新提示。闸是对的（安全），不放松。

    所以新东西走 `extras-<ver>.tar.gz`，manifest 只加一个**可选**键 `extras`：老客户端
    不认识就忽略、照旧只下第一个归档；新客户端两个都下、都验、都装。manifest 整体签名，
    新键天然被签到。`extras` 的 key 形如 `shell/windows`、`shell/macos`、`app` ——
    顶层目录只许 `EXTRA_UNITS` 里的那几个。
    """
    version = version or the_version()
    out_dir.mkdir(parents=True, exist_ok=True)
    write_the_version_marker(staged_harness, version)

    archive = out_dir / f"payload-{version}.tar.gz"
    blob, counts = _tar_trees({"harness": staged_harness, "static_ui": staged_ui})
    archive.write_bytes(blob)

    if edition not in _the_edition_module().EDITIONS:
        raise SystemExit(f"edition 必须是 personal / pro，不是 {edition!r}")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        # 发布件自己说清楚是哪种发行：publish_release 据此拒绝把专业版发进公开仓库。
        # 老客户端不认识这个键，忽略（parse_manifest 只核 schema/unit/size，不核未知键）。
        "edition": edition,
        "build": the_build_id(),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive": archive.name,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
        "unit": list(UNIT),
        "files": {"harness": counts["harness"], "static_ui": counts["static_ui"]},
        "notes": notes,
    }
    if extras:
        for arcname, root in extras.items():
            top = arcname.split("/", 1)[0]
            if top not in EXTRA_UNITS:
                raise SystemExit(f"extras 的 {arcname!r} 顶层不在 {EXTRA_UNITS} 里 —— 老客户端的闸会拒收")
            if not root.is_dir():
                raise SystemExit(f"extras 的 {arcname!r} 指向的不是目录：{root}")
        extras_archive = out_dir / f"extras-{version}.tar.gz"
        extras_blob, extras_counts = _tar_trees(dict(sorted(extras.items())))
        extras_archive.write_bytes(extras_blob)
        manifest["extras"] = {
            "archive": extras_archive.name,
            "sha256": hashlib.sha256(extras_blob).hexdigest(),
            "size": len(extras_blob),
            "units": sorted(extras),
            "files": extras_counts,
            # 每个壳单独一个 sha256：后端拿它和装着的壳自报的 sha256 比，回答「这次更新
            # 含不含壳的改动」—— 不用先把整个归档下回来。
            "shell": {
                arcname.split("/", 1)[1]: _digest_of_the_shell(root)
                for arcname, root in extras.items() if arcname.startswith("shell/")
            },
        }
    if server_bundle is not None:
        # 组织服务器自己升级时只信这一项：包的摘要在签过名的 manifest 里（可选键，老客户端
        # 忽略）。协议号一并写上 —— 服务器升级之前就知道新版会不会让还没更新的桌面对不上。
        if server_bundle.name != f"sciencemate-server-{version}.tar.gz":
            raise SystemExit(f"服务器包 {server_bundle.name} 和载荷版本 {version} 对不上")
        blob_of_the_server = server_bundle.read_bytes()
        manifest["server"] = {
            "archive": server_bundle.name,
            "sha256": hashlib.sha256(blob_of_the_server).hexdigest(),
            "size": len(blob_of_the_server),
            "version": version,
            "org_protocol": _the_org_protocol(),
        }
    manifest_path = out_dir / "manifest.json"
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)

    key = release_keys.load_private()
    sig_path = out_dir / "manifest.json.sig"
    sig_path.unlink(missing_ok=True)
    if key is None:
        print(f"  ⚠️ 没有发布私钥（{release_keys.private_key_path()}）—— 载荷**未签名**，"
              f"装好的应用会拒收。要发布先跑：python3 scripts/package/release_keys.py generate")
        manifest["_signed"] = False
    else:
        sig_path.write_text(release_keys.sign(manifest_bytes, key), encoding="ascii")
        manifest["_signed"] = True
        prove_the_payload_verifies(manifest_path, sig_path, archive,
                                   out_dir / manifest["extras"]["archive"] if "extras" in manifest else None)

    manifest["_paths"] = {"archive": str(archive), "manifest": str(manifest_path),
                          "signature": str(sig_path) if key else "",
                          "extras": str(out_dir / manifest["extras"]["archive"]) if "extras" in manifest else ""}
    print(f"  载荷 {archive.name}（{len(blob) // 1024 // 1024} MB，harness {counts['harness']} 文件"
          f" + 界面 {counts['static_ui']} 文件），{'已签名' if key else '未签名'}")
    if "extras" in manifest:
        ex = manifest["extras"]
        print(f"  附加 {ex['archive']}（{ex['size'] // 1024} KB：{', '.join(ex['units'])}；壳 {sorted(ex['shell'])}）")
    return manifest


def baked_public_key() -> str:
    """应用里烧的那把公钥 —— 从源码文件读，不抄第二份。"""
    text = (BACKEND / "app" / "release_pubkey.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("RELEASE_PUBLIC_KEY_B64"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("release_pubkey.py 里没有 RELEASE_PUBLIC_KEY_B64")


def prove_the_payload_verifies(manifest_path: Path, sig_path: Path, archive: Path,
                               extras_archive: Path | None = None) -> None:
    """发布前用**应用里烧的那把公钥**验一次 —— 签的钥匙和烧的钥匙对不上，
    发出去的每一份更新都会被拒，而发布机这边什么都不报错。

    带附加归档时一并验：manifest.extras 的 sha256/size 对得上文件、顶层目录只在
    `EXTRA_UNITS` 里、每个平台的壳的 sha256 等于 manifest.extras.shell 里写的 —— 客户端
    收到后会做同样的核对，这里先替它做一遍，发出去的就是收得下的。
    """
    public = baked_public_key()
    data = manifest_path.read_bytes()
    if not release_keys.verify(public, data, sig_path.read_text(encoding="ascii").strip()):
        raise SystemExit("签名用的私钥与 release_pubkey.py 里烧的公钥**不配对** —— "
                         "装好的应用会拒收这份载荷。别发。")
    manifest = json.loads(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["sha256"]:
        raise SystemExit("载荷的 sha256 与 manifest 不一致")
    if ("extras" in manifest) != (extras_archive is not None):
        raise SystemExit("manifest 里的 extras 和手上的附加归档对不上（一个有一个没有）")
    if extras_archive is not None:
        ex = manifest["extras"]
        blob = extras_archive.read_bytes()
        if hashlib.sha256(blob).hexdigest() != ex["sha256"] or len(blob) != ex["size"]:
            raise SystemExit("附加归档的 sha256/size 与 manifest.extras 不一致")
        with tempfile.TemporaryDirectory(prefix="extras-check-") as tmp:
            with tarfile.open(extras_archive, "r:gz") as tar:
                tops = {m.name.split("/", 1)[0] for m in tar.getmembers()}
                if not tops or not tops <= set(EXTRA_UNITS):
                    raise SystemExit(f"附加归档顶层目录 {sorted(tops)} 不在 {list(EXTRA_UNITS)} 之内")
                tar.extractall(tmp, filter="data")
            for platform, digest in ex["shell"].items():
                binary = Path(tmp) / "shell" / platform / SHELL_BINARY[platform]
                if not binary.is_file():
                    raise SystemExit(f"附加归档说带了 {platform} 的壳，解出来却没有 {binary.name}")
                if hashlib.sha256(binary.read_bytes()).hexdigest() != digest:
                    raise SystemExit(f"{platform} 壳的 sha256 与 manifest.extras.shell 不一致")
            # 带了后端 app/ 就得是客户端接得过去的那种：启动器认的是 `app/launcher.py`。
            if "app" in ex["units"] and not (Path(tmp) / "app" / "launcher.py").is_file():
                raise SystemExit("附加归档说带了后端 app/，解出来却没有 app/launcher.py —— 客户端接不过去")
    with tempfile.TemporaryDirectory(prefix="payload-check-") as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            tops = {m.name.split("/", 1)[0] for m in tar.getmembers()}
            if tops != set(UNIT):
                raise SystemExit(f"载荷顶层目录 {sorted(tops)} ≠ 更新单元 {list(UNIT)}")
            tar.extractall(tmp, filter="data")
        marker = Path(tmp) / "harness" / VERSION_MARKER
        if not marker.is_file() or marker.read_text().strip() != manifest["version"]:
            raise SystemExit("载荷里的 PAYLOAD_VERSION 标记缺失或与 manifest 不一致")
        if not (Path(tmp) / "harness" / "core" / "agent_loop.py").is_file():
            raise SystemExit("载荷里没有 core/agent_loop.py —— 启动器认不出它")
    print("  ✓ 用应用里烧的公钥验过：签名、摘要、目录、版本标记都对")
