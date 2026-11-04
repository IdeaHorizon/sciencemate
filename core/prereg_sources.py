"""冻结预注册里**显式声明**的数据源主机 —— 一个出处（#1244，RFC_CAPABILITY_GRANTS_LAYERING D7）。

两个读者，同一份答案：

* 派发时（`core.executor.execute_node`）：这一趟绑定了哪一份冻结预注册（`task_contract` 上的
  exact 绑定：id + 版本 + 内容哈希），它的「数据源 / 许可与前提」小节里写了哪些主机 —— 批准派发
  就把这些主机一并作为出网授权落下，无人值守的长课题不必等到撞墙再问一次「没人答 = 没批」。
* experiment 第一轮的可达性预检（`nodes/experiment/hooks.py`）：还有哪些声明了的主机够不着。

解析规则原样搬自 experiment 的预检（lujy，#1037 起），只是换了住处：两处各写一份，就是两个会
悄悄分叉的答案。**宁可漏报不可误报**：认不准的不算主机 —— 漏掉的照旧走 `request_network_access`
问人，误认的会变成一条没人批过的授权。
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["data_source_section", "declared_hosts", "declared_hosts_for_run", "looks_like_host"]

# 显式排除的"长得像域名"的文件扩展名与数据格式后缀。注意 .nc/.sh/.in/.md 同时
# 是真实 ccTLD，不排除就会把 sst.mnmean.nc 误判成主机 —— 宁可漏报不可误报。
_NON_HOST_SUFFIXES = frozenset("""
nc nc4 cdf cdl netcdf grb grb2 grib grib2 hdf hdf5 h5 zarr npy npz mat sav
csv tsv txt dat bin log out err json yaml yml toml ini cfg conf nml namelist
md rst html htm css js xml pdf png jpg jpeg tif tiff svg gif mp4 avi
py sh bash csh zsh pl rb jl f f90 f77 c cc cpp cxx h hpp o so a exe whl
zip tar gz tgz xz bz2 zst rar iso img deb rpm lock bak tmp swp orig rej
db sql sqlite parquet feather pt pth ckpt pkl safetensors ipynb docx xlsx pptx
""".split())

# 裸 token 扫描：至少两段、字符集 [a-z0-9.-]；左侧 lookbehind 含 "/" —— URL 路径
# 段（.../ncep.reanalysis/...）不算主机，主机由 _URL_HOST_RE 单独负责。
_HOST_TOKEN_RE = re.compile(
    r"(?<![0-9a-z._/-])([a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)"
)
_URL_HOST_RE = re.compile(r"https?://([^\s/?#\"'<>）)\]，、；]+)", re.I)


def looks_like_host(host: str) -> bool:
    """Conservative hostname test: prefer missing a host over inventing one."""
    if not host or len(host) < 4 or ".." in host:
        return False
    labels = host.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return False
    tld = labels[-1]
    if not tld.isalpha() or not 2 <= len(tld) <= 24:
        return False          # 版本号 v1.2.3 / 分辨率 0.25 的尾段不是字母
    if tld in _NON_HOST_SUFFIXES:
        return False          # sst.mnmean.nc、requirements.txt 之类
    if all(label.isdigit() for label in labels[:-1]):
        return False
    return True


def data_source_section(text: str) -> str:
    """定位「数据源/资源 + 许可/前提」小节正文（标题大小写与全半角宽容）。"""
    lines = text.splitlines()
    start = -1
    level = 0
    for index, line in enumerate(lines):
        matched = re.match(r"^(#{1,6})\s*(.+?)\s*$", line)
        if not matched:
            continue
        title = (matched.group(2)
                 .replace("（", "(").replace("）", ")")
                 .replace("　", "").replace(" ", "").lower())
        subject = any(word in title for word in ("数据源", "数据来源", "资源", "datasource", "datasources"))
        premise = any(word in title for word in ("许可", "前提", "licen"))
        if subject and premise:
            start = index + 1
            level = len(matched.group(1))
            break
    if start < 0:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        heading = re.match(r"^(#{1,6})\s", line)
        if heading and len(heading.group(1)) <= level:
            break
        body.append(line)
    return "\n".join(body)


def declared_hosts(section: str) -> list[str]:
    """一段正文里能认准的主机名（去重、保序、小写）。传整份预注册时先取小节：`data_source_section`。"""
    candidates: list[str] = []
    for matched in _URL_HOST_RE.finditer(section):
        raw = matched.group(1).lower().split("@")[-1].split(":")[0]
        candidates.append(raw)
    for matched in _HOST_TOKEN_RE.finditer(section.lower()):
        candidates.append(matched.group(1))
    hosts: list[str] = []
    for candidate in candidates:
        host = candidate.strip().strip(".").lower()
        if looks_like_host(host) and host not in hosts:
            hosts.append(host)
    return hosts


def declared_hosts_for_run(state: Any) -> tuple[list[str], dict[str, Any]]:
    """这一趟**绑定**的那份冻结预注册里声明的主机，以及依据。

    只认 `task_contract` 上的 exact 绑定（id + 版本 + 内容哈希三件一起，#1099）：项目里此刻
    「最新那份冻结的」不算 —— experiment 自己的预检也明说那只是观察、不构成授权。版本找不到、
    哈希对不上、正文读不到，都返回空列表并在依据里写明为什么（没授权和查过但不给是两件事）。
    """
    from core.task_contract import ASSIGNMENT_EXACT, assignment_for_state

    revision, kind = assignment_for_state(state)
    if revision is None or kind != ASSIGNMENT_EXACT:
        return [], {"bound": False, "assignment": kind}
    assignment = revision.assignment
    basis: dict[str, Any] = {"bound": True, "artifact_id": assignment.artifact_id,
                             "version": assignment.version}
    try:
        versions = state.artifact_versions(assignment.artifact_id) or []
    except Exception as exc:  # 观察层：读不到不是授权
        basis["unreadable"] = f"{type(exc).__name__}: {exc}"[:300]
        return [], basis
    match = next((v for v in versions if str(v.get("version")) == str(assignment.version)), None)
    if match is None:
        basis["unreadable"] = "bound version not found"
        return [], basis
    if assignment.content_hash and str(match.get("content_hash") or "") != assignment.content_hash:
        basis["unreadable"] = "content hash does not match the binding"
        return [], basis
    content = str(match.get("content") or "")
    if not content:
        basis["unreadable"] = "bound version has no readable content"
        return [], basis
    section = data_source_section(content)
    return (declared_hosts(section) if section.strip() else []), basis
