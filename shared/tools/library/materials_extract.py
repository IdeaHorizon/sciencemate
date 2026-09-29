"""`extract_material` —— 把用户交来的压缩包解到**材料池**里，由框架做。

## 为什么这是一个工具而不是一句提示

用户交来的文件按内容寻址存在 `resources/materials/`（`core/materials`），那个目录
整个 gitignored，只有 `.ref` 指针进版本。可一份 `.tar.gz` 对模型没用，它要的是
解开的文件。此前没有解包的路，模型只能 `tar xzf` 到**自己的节点目录**——
2026-09-09 node20：4492 个日志文件、897MB、两份副本，全部被 checkpoint 收进会话
分支，之后每一次「这个会话改了什么」都要跟它成正比，整台后端以 35 秒为量子停摆。

解开的东西是材料的**派生物**，归属和材料一样：进池目录，不进 Git。这里就是那条
唯一的路：`resources/materials/<名字>.extracted/`，与材料同目录、同 gitignore、
同一个"只读输入"的地位。节点目录只放节点自己产出的东西。

## 边界

- 只解本工作区已登记的材料（名字来自 `list_files` / 材料清单），不接任意路径。
- 成员名做路径归一：绝对路径、`..`、链接一律跳过并计数，不让归档写到目录外。
- 幂等：已经解开就直接返回同一个路径，不重解。
- 只解 tar（含 gz/bz2/xz）与 zip；别的格式如实报不支持，不猜。
"""
from __future__ import annotations

import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from core import materials
from core.state import State
from core.tool_registry import ToolDefinition, register_tool

#: 解开的目录挂在材料名后面，与材料同目录（因此同一条 gitignore 兜着）。
EXTRACTED_SUFFIX = ".extracted"


def extracted_dir(worktree: Path | str, name: str) -> Path:
    return materials.materials_dir(worktree) / f"{materials.safe_name(name)}{EXTRACTED_SUFFIX}"


def _member_target(root: Path, member_name: str) -> Path | None:
    """归档成员落到哪；越界（绝对路径 / `..`）返回 None。"""
    parts = PurePosixPath(member_name.replace("\\", "/")).parts
    if not parts or parts[0] in ("/", "") or any(part in ("..", "") for part in parts):
        return None
    if PurePosixPath(member_name).is_absolute():
        return None
    return root.joinpath(*parts)


def _extract_tar(archive: Path, root: Path) -> tuple[int, int, int]:
    files = total = skipped = 0
    with tarfile.open(archive) as tar:
        for member in tar:
            target = _member_target(root, member.name)
            if target is None or member.issym() or member.islnk() or member.isdev():
                skipped += 1
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                skipped += 1
                continue
            with source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, 1024 * 1024)
            files += 1
            total += member.size
    return files, total, skipped


def _extract_zip(archive: Path, root: Path) -> tuple[int, int, int]:
    files = total = skipped = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = _member_target(root, info.filename)
            if target is None:
                skipped += 1
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # zip 里的符号链接靠 external_attr 表态；不还原链接
            if stat.S_ISLNK(info.external_attr >> 16):
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, 1024 * 1024)
            files += 1
            total += info.file_size
    return files, total, skipped


def _make_read_only(root: Path) -> None:
    """材料是输入，不是草稿：解开的文件与池对象同样只读。"""
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(0o444)


def _summary(root: Path) -> tuple[int, int]:
    files = total = 0
    for path in root.rglob("*"):
        if path.is_file():
            files += 1
            total += path.stat().st_size
    return files, total


async def extract_material(*, state: State, name: str, **_: Any) -> dict:
    worktree = getattr(state, "project_worktree", None)
    if not worktree:
        return {"status": "error", "error": "这个 run 没有绑定项目工作区，没有材料可解。"}
    root = Path(worktree)
    try:
        safe = materials.safe_name(name)
    except materials.MaterialError as exc:
        return {"status": "error", "error": str(exc)}
    source = materials.materials_dir(root) / safe
    if not source.is_file():
        known = sorted(ref.name for ref in materials.inventory(root))
        return {
            "status": "error",
            "error": (
                f"找不到材料 {safe!r}。已登记的材料："
                f"{', '.join(known) if known else '（无）'}"
            ),
        }
    target = extracted_dir(root, safe)
    relative = str(target.relative_to(root))
    if target.is_dir():
        files, total = _summary(target)
        return {
            "status": "success", "path": relative, "files": files, "bytes": total,
            "already_extracted": True,
            "note": f"{safe} 此前已解开，直接用 {relative}/ 下的文件。",
        }
    if tarfile.is_tarfile(source):
        extractor = _extract_tar
    elif zipfile.is_zipfile(source):
        extractor = _extract_zip
    else:
        return {
            "status": "error",
            "error": f"{safe} 不是 tar/zip 归档，这里解不开；如果它本来就是数据文件，直接读它。",
        }
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    try:
        files, total, skipped = extractor(source, partial)
        _make_read_only(partial)
        partial.rename(target)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(partial, ignore_errors=True)
        return {"status": "error", "error": f"解开 {safe} 失败：{type(exc).__name__}: {exc}"}
    return {
        "status": "success",
        "path": relative,
        "files": files,
        "bytes": total,
        "skipped_members": skipped,
        "note": (
            f"已解到 {relative}/（{files} 个文件，{materials.human_size(total)}）。"
            "这个目录和材料本身一样不进 Git、只读；分析结果和脚本写回你自己的节点目录。"
        ),
    }


register_tool(
    ToolDefinition(
        name="extract_material",
        description=(
            "把用户交来的压缩包（tar/tar.gz/tar.xz/zip）解到材料池 "
            "`resources/materials/<名字>.extracted/`，返回目录路径与文件数。\n"
            "**用户上传的归档只许这样解**：解到自己的节点目录会被 checkpoint 整目录"
            "排除（一份数据集几千个文件不该进版本库），而材料池目录本来就不进 Git。\n"
            "幂等：已解开就直接返回同一个路径。只读：解出来的文件是输入不是草稿。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "材料文件名（`resources/materials/` 下的名字，含后缀）。",
                },
            },
            "required": ["name"],
        },
        risk_level="low",
    ),
    extract_material,
)
