"""随包的 TeX 工具链（tectonic + biber）：哪一版、从哪取、收货认哪个哈希 —— 两个打包器读这一份。

## 为什么是一对，而且必须一处写

平台模板（zh_article）的参考文献走 biblatex/biber。tectonic 自己**不带** biber，编到参考
文献那步从 PATH 调外部的（tectonic 源码 ``src/driver.rs``）；没有它：``Running external
tool biber ... error: program not found``（2026-09-23 Windows 真机）。

biber 的版本**必须**和 tectonic 宏包里的 biblatex 对上：0.15.0 的宏包（格式 33）是
biblatex 3.17（真机 .log：``biblatex 2022/02/02 v3.17``）→ biber 2.17。Mac 包与 Windows
包各写一份版本号，迟早有一边升了 tectonic 而 biber 没跟上 —— 那一边的每篇论文都卡在参考
文献，另一边一切正常。所以这一对只在这里写；对没对上由两个打包器的装完自检说（在安装
位置用平台自己的实测真编一次 PDF：``PDF_PROBE``）。

## 按哈希收货

SourceForge 走镜像分发，GitHub 这几版的资产也没有官方摘要，所以每一件都钉死 sha256：
哈希对不上就当场失败，而不是把一个不知道是什么的二进制签上名发给同事。哈希是
2026-09-24 在打包机上取的（biber Windows 版另有 box 上独立下载的同一哈希）。
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, NamedTuple

TECTONIC_VERSION = "0.15.0"
BIBER_VERSION = "2.17"

_TECTONIC = ("https://github.com/tectonic-typesetting/tectonic/releases/download/"
             f"tectonic%40{TECTONIC_VERSION}/tectonic-{TECTONIC_VERSION}-")
_BIBER = ("https://sourceforge.net/projects/biblatex-biber/files/biblatex-biber/"
          f"{BIBER_VERSION}/binaries/")


class Asset(NamedTuple):
    url: str
    sha256: str
    member: str          # 压缩包里要的那个文件（按文件名认，不按目录层级）


ASSETS: dict[tuple[str, str], Asset] = {
    ("windows", "tectonic"): Asset(
        _TECTONIC + "x86_64-pc-windows-msvc.zip",
        "1d6bb76f049c8a3774f6e9d66e4b04e1a8c3dcb37527b6b41b7e894328e7bf29", "tectonic.exe"),
    ("windows", "biber"): Asset(
        _BIBER + "Windows/biber-MSWIN64.zip/download",
        "c103bffc5ae0a7f513e7c26b6d394e9be6cf41952959c5d604ee2e6581b5dea2", "biber.exe"),
    # Mac 包是 arm64（包里的 CPython 就是 arm64），tectonic 取 aarch64 版；biber 只有 universal。
    ("macos", "tectonic"): Asset(
        _TECTONIC + "aarch64-apple-darwin.tar.gz",
        "24bd46566fa30d41101848405e9cbc4645edb92d8f857c9d21262174fb70cd33", "tectonic"),
    ("macos", "biber"): Asset(
        _BIBER + "MacOS/biber-darwin_universal.tar.gz/download",
        "182e1efa074d8a2a23a8893f2a22440d4e463cce55e4ed02076ac4c0ee0614b2", "biber"),
}


def place(platform: str, name: str, target: Path, download: Callable[[str, Path], None]) -> Path:
    """取 ``(platform, name)`` 那一件、按哈希收货、把里面那个文件放到 ``target``。

    ``download(url, dest)`` 由打包器给（各自已有带重试与截断校验的那个）。
    """
    asset = ASSETS[(platform, name)]
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "archive"
        print(f"  下载 {asset.url}")
        download(asset.url, archive)
        blob = archive.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if digest != asset.sha256:
            raise SystemExit(f"❌ {name}（{platform}）压缩包哈希不对：{digest}（应为 {asset.sha256}）")
        with target.open("wb") as out:
            out.write(_extract(blob, asset.member))
    target.chmod(0o755)
    print(f"  {target}（{target.stat().st_size // (1024 * 1024)} MB，sha256 已核）")
    return target


def _extract(blob: bytes, member: str) -> bytes:
    if zipfile.is_zipfile(io.BytesIO(blob)):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if Path(n).name.lower() == member.lower()]
            if len(names) != 1:
                raise SystemExit(f"❌ 压缩包里要一个 {member}，找到 {names}")
            return zf.read(names[0])
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        found = [m for m in tf.getmembers() if m.isfile() and Path(m.name).name == member]
        if len(found) != 1:
            raise SystemExit(f"❌ 压缩包里要一个 {member}，找到 {[m.name for m in found]}")
        stream = tf.extractfile(found[0])
        assert stream is not None
        with stream:
            return stream.read()


#: 装完自检的 PDF 实测探针：走**用户那条路**接环境（``launcher.prepare_the_environment`` 把
#: 随包 tectonic / biber 接上 PATH），再用平台自己的实测（``shared.lib.pdf_toolchain``）——
#: 同一份样本、同一堵墙、墙给的同一个家、同样断网（缺包由编译路径自己取）。不在打包器里
#: 另写编译命令：抄件会分叉。用法：``<包里的 python> probe.py <临时数据根>``，最后一行是记录。
PDF_PROBE = """
import asyncio, json, os, sys
from pathlib import Path

os.environ["HARNESS_FRAMEWORK_HOME"] = sys.argv[1]
from app import launcher

_ui, harness = launcher.prepare_the_environment()
sys.path.insert(0, str(harness))
from shared.lib import pdf_toolchain

print(json.dumps(asyncio.run(pdf_toolchain.measure()), ensure_ascii=False))
"""


def read_the_probe_record(stdout: str) -> dict:
    """探针的最后一行是实测记录；读不出来 = 探针自己没跑起来（先怀疑自检，别急着怪产品）。"""
    lines = (stdout or "").strip().splitlines()
    try:
        record = json.loads(lines[-1]) if lines else {}
    except ValueError:
        record = {}
    return record if isinstance(record, dict) else {}
