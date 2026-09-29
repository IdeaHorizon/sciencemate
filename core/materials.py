"""用户交给平台的文件 —— 唯一落点、字节池、指针。

## 不变量

**用户给的文件，就是会话 worktree 里 `sources/<name>` 这个真实文件。
路径即身份。**

模型不需要任何新工具、新协议、新前缀：`read_file` 读它、`run_bash` 里 `tar`
解它、`import_artifact` 收它（`sources/` 就是"位置即来源"认定的外来材料位：
用户带进来的原件一个门进来、永不改，角色靠引用而不是靠搬家）。沙箱把 worktree 以**同一路径** bind 进容器，所以工作区里的路径在
容器里也是那条路径 —— 反过来说，worktree 之外的任何落点（对象存储、runtime
目录、临时目录）在沙箱里根本不存在，那正是 2026-09-04 "上传了 agent 找不到"
的结构性原因。

## 字节与指针分离

    <git-common-dir>/harness-materials/<aa>/<sha256>   字节池，0444，只增
    <worktree>/sources/<name>                          hardlink 到池对象（gitignored）
    <worktree>/sources/<name>.ref                      跟踪的指针（JSON）
    <worktree>/sources/.gitignore                      由本模块维护

为什么不把字节直接放进 Git：用户文件是**输入**，天然按内容寻址；Git 的职责
是记"哪个 sha 在哪个时刻在场"，不是存字节。这样 2KB 的 yaml 和 800MB 的
tarball 走**同一条规则**，不再有"附件 64 MiB / 材料 10 MB / checkpoint 静默
剔除"这三套互不相干的尺寸判决。代价如实说：`git show main:<材料路径>` 看到
的是指针不是内容，跟 git-lfs 一样。

为什么池在 `<git-common-dir>` 下而不是一个配置项：一个项目的全部 worktree
（canonical 主干 + 每个会话）共用同一个 git common dir，所以池的位置**可以
从 worktree 自己推出来**，不需要第二个真相源，也不会出现"平台进程和 CLI 进程
各算出一个池"。它同时保证同文件系统（hardlink 的前提），跨设备时按 EXDEV
回退成拷贝并如实记账。

为什么 hardlink 不是 symlink：checkpoint 硬拒 symlink；容器里 symlink 会悬空；
hardlink 在 bind mount 里就是一个普通文件。

## 一个文件一份指针，不要清单

早先的实现用 append-only 的 `MANIFEST.jsonl` 记全部材料。它在 publish 时是
隐藏冲突源：publish 按路径整文件复制，两个会话各自往同一份 JSONL 追加，后
发布的那次会把先发布的那些行整份覆盖掉，而且两边都不报错。一份文件一份
`.ref` 之后，publish 的路径集天然无交叉。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

#: 材料在 worktree 里的位置（相对 worktree 根，POSIX 写法）。
#: 2026-09-12 从 `resources/materials` 改名：`resources/` 同时装着算力注册表和
#: 密钥引用（project_governance 写的），用户的材料和 HPC 资源不该共用一个名字。
MATERIALS_RELATIVE = "sources"
#: 指针文件后缀。
REF_SUFFIX = ".ref"
#: 字节池在 git common dir 下的名字。
POOL_DIRNAME = "harness-materials"
#: 池对象与 worktree 里的实体一律只读 —— 材料是输入，不是草稿。
_OBJECT_MODE = 0o444
#: `.gitignore`：目录里除了它自己和指针，什么都不进版本。
_GITIGNORE_BODY = (
    "# 字节由 core/materials 的内容寻址池持有，Git 只跟踪 .ref 指针。\n"
    "# 这条忽略是承重的：没有它，一个 800MB 的未跟踪文件会让\n"
    "# `_worktree_has_blocking_changes` 永久为真，产物写入与 checkpoint 全部停摆。\n"
    "*\n"
    "!.gitignore\n"
    "!*" + REF_SUFFIX + "\n"
)
#: 一次读多少字节做 hash。
_CHUNK = 1024 * 1024

_REF_SCHEMA_VERSION = 1


class MaterialError(RuntimeError):
    """材料落盘失败。消息面向用户，说清哪里错了、该怎么办。"""


class MaterialTooLargeError(MaterialError):
    def __init__(self, message: str, *, size_bytes: int, max_bytes: int):
        super().__init__(message)
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes


class MaterialNameConflictError(MaterialError):
    """同名但内容不同。改名是用户的决定，框架不替他做。"""


@dataclass(frozen=True, slots=True)
class MaterialRef:
    """一份材料的完整命题 —— 谁、何时、多大、什么内容、哪来的。

    只有记录的人能凭它把这份材料重新认出来：sha256 是身份，path 是位置，
    两者都在。
    """

    name: str
    #: `sources/<name>`
    path: str
    #: `sources/<name>.ref`
    ref_path: str
    sha256: str
    size_bytes: int
    uploaded_by: str
    uploaded_at: str
    source: str
    note: str
    #: 字节此刻是否真的在这个 worktree 里躺着。
    present: bool
    #: 实体文件的绝对路径 —— 注入给模型的就是这个（相对路径锚在节点目录上，
    #: 给相对路径等于让模型去猜锚点）。
    absolute_path: Path

    def as_ref_document(self) -> dict:
        return {
            "schema_version": _REF_SCHEMA_VERSION,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "uploaded_by": self.uploaded_by,
            "uploaded_at": self.uploaded_at,
            "source": self.source,
            "note": self.note,
        }


# ── 位置 ────────────────────────────────────────────────────────────────────


def _git_common_dir(worktree: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    if result.returncode != 0:
        raise MaterialError(
            f"{worktree} 不是一个 Git 工作区，材料没有可共用的字节池："
            f"{result.stderr.strip()[:200]}"
        )
    raw = result.stdout.strip()
    if not raw:
        raise MaterialError(f"{worktree} 的 git common dir 为空")
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else (worktree / candidate).resolve()


def pool_root(worktree: Path | str) -> Path:
    """字节池的根。一个项目的全部 worktree 共用它。"""
    return _git_common_dir(Path(worktree)) / POOL_DIRNAME


def materials_dir(worktree: Path | str) -> Path:
    return Path(worktree) / MATERIALS_RELATIVE


def _object_path(pool: Path, digest: str) -> Path:
    return pool / digest[:2] / digest


def safe_name(filename: str) -> str:
    """上传的文件名是不可信输入：只取 basename、剥分隔符、禁隐藏文件。

    中文等非 ASCII 名字**保留** —— 仓库与 raw 出口（RFC 5987）都撑得住，
    改名反而让用户在自己的文件树里认不出自己的文件。
    """
    name = Path(str(filename)).name.replace("/", "_").replace("\\", "_").strip()
    name = name.lstrip(".")
    if not name:
        raise MaterialError("材料需要一个非空文件名")
    if name == ".gitignore":
        raise MaterialError("`.gitignore` 由框架维护，不能作为材料名")
    if name.endswith(REF_SUFFIX):
        raise MaterialError(
            f"材料名不能以 `{REF_SUFFIX}` 结尾 —— 那是指针文件的后缀。"
            f"把文件改个名再传。"
        )
    return name


# ── 写入 ────────────────────────────────────────────────────────────────────


def ensure_gitignore(worktree: Path | str) -> str:
    """确保材料目录存在且带着那条承重的 `.gitignore`。返回它的相对路径。"""
    directory = materials_dir(worktree)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ".gitignore"
    if not target.is_file() or target.read_text(encoding="utf-8") != _GITIGNORE_BODY:
        target.write_text(_GITIGNORE_BODY, encoding="utf-8")
    return f"{MATERIALS_RELATIVE}/.gitignore"


def _link_or_copy(object_path: Path, target: Path) -> bool:
    """把池对象接进 worktree。返回 True = hardlink，False = 跨设备回退成拷贝。"""
    if target.exists():
        if target.samefile(object_path):
            return True
        with target.open("rb") as existing, object_path.open("rb") as original:
            if hashlib.file_digest(existing, "sha256").digest() == hashlib.file_digest(original, "sha256").digest():
                return False
        raise MaterialError(f"材料内容与已登记对象不一致，不能覆盖：{target}")
    try:
        os.link(object_path, target)
        return True
    except OSError:
        # 跨文件系统（EXDEV）或不支持硬链接：拷一份，如实告诉调用方。
        shutil.copy2(object_path, target)
        os.chmod(target, _OBJECT_MODE)
        return False


def _absorb(pool: Path, stream: IO[bytes], *, max_bytes: int | None) -> tuple[str, int, Path]:
    """流式把字节收进池，返回 (sha256, size, object_path)。

    先落临时文件再按内容改名：中途失败不会在池里留下一个名字正确、内容不全的
    对象（那种对象事后无法与正确的区分开）。
    """
    pool.mkdir(parents=True, exist_ok=True)
    incoming = pool / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    handle = tempfile.NamedTemporaryFile(dir=incoming, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise MaterialTooLargeError(
                        f"文件超过上限 {max_bytes} 字节",
                        size_bytes=size, max_bytes=max_bytes,
                    )
                digest.update(chunk)
                handle.write(chunk)
        sha = digest.hexdigest()
        object_path = _object_path(pool, sha)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        if object_path.exists():
            temporary.unlink(missing_ok=True)      # 同内容已在池里，幂等
        else:
            os.chmod(temporary, _OBJECT_MODE)
            os.replace(temporary, object_path)
        return sha, size, object_path
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def place(
    worktree: Path | str,
    filename: str,
    source: IO[bytes] | Path | str,
    *,
    uploaded_by: str,
    uploaded_at: str | None = None,
    note: str = "",
    material_source: str = "upload",
    max_bytes: int | None = None,
) -> tuple[MaterialRef, tuple[str, ...]]:
    """把一份用户文件放进 worktree。**只写盘，不碰 Git。**

    返回 `(材料, 需要提交的相对路径)`。谁有 Git 权限谁去提交 —— 平台走
    `_commit_platform_write`，CLI 走 `project_bootstrap._git`。本模块不持有
    提交权限，也就不可能在两个入口各长出一套提交逻辑。

    - 同名同内容重放 → 幂等（补齐可能缺失的实体与指针），不报错。
    - 同名不同内容   → `MaterialNameConflictError`。自动加后缀是替用户做决定，
                       而他此刻就在屏幕前，能自己改名。
    """
    root = Path(worktree)
    name = safe_name(filename)
    pool = pool_root(root)
    gitignore_path = ensure_gitignore(root)

    relative = f"{MATERIALS_RELATIVE}/{name}"
    ref_relative = f"{relative}{REF_SUFFIX}"
    target = root / relative
    ref_target = root / ref_relative

    existing = _read_ref(ref_target)
    if isinstance(source, (str, Path)):
        source_path = Path(source)
        if not source_path.is_file():
            raise MaterialError(f"找不到文件：{source_path}")
        with source_path.open("rb") as handle:
            sha, size, object_path = _absorb(pool, handle, max_bytes=max_bytes)
    else:
        sha, size, object_path = _absorb(pool, source, max_bytes=max_bytes)

    if existing is not None and existing.get("sha256") != sha:
        raise MaterialNameConflictError(
            f"`{name}` 已经存在，而且内容不同"
            f"（在册 sha256 {str(existing.get('sha256'))[:12]}，这次 {sha[:12]}）。"
            f"换个文件名再传 —— 覆盖别人的材料、或者悄悄给你加个后缀，"
            f"都不该由框架替你决定。"
        )
    if existing is None and target.exists():
        raise MaterialNameConflictError(
            f"`{relative}` 已经有一个不受管理的同名文件（没有 {REF_SUFFIX} 指针）。"
            f"换个文件名，或先把那个文件挪走。"
        )

    linked = _link_or_copy(object_path, target)
    reference = MaterialRef(
        name=name,
        path=relative,
        ref_path=ref_relative,
        sha256=sha,
        size_bytes=size,
        uploaded_by=str(uploaded_by),
        uploaded_at=uploaded_at or datetime.now(UTC).isoformat(),
        source=str(material_source),
        note=str(note),
        present=True,
        absolute_path=target.resolve(),
    )
    if existing is not None:
        # 幂等命中：指针已经在册，内容一致 —— 不改写它的上传者/时间戳，
        # 那是第一次上传时的事实，不是这次的。
        reference = _ref_from_document(root, name, existing)
    else:
        _write_ref(ref_target, reference)
    del linked   # 跨设备回退成拷贝仍是合法形状（闸 G2 认 nlink≥2 **或** sha 与指针一致）
    return reference, (ref_relative, gitignore_path)


# ── 读取 ────────────────────────────────────────────────────────────────────


def _read_ref(ref_path: Path) -> dict | None:
    if not ref_path.is_file():
        return None
    try:
        parsed = json.loads(ref_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 吵，不吞：一份坏指针不该让整个清单消失，也不该被当成"没有这份材料"。
        return {"sha256": "", "size_bytes": 0, "uploaded_by": "", "uploaded_at": "",
                "source": "", "note": f"指针文件无法解析：{ref_path.name}"}
    return parsed if isinstance(parsed, dict) else None


def _write_ref(ref_path: Path, reference: MaterialRef) -> None:
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(
        json.dumps(reference.as_ref_document(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _ref_from_document(worktree: Path, name: str, document: dict) -> MaterialRef:
    relative = f"{MATERIALS_RELATIVE}/{name}"
    entity = worktree / relative
    return MaterialRef(
        name=name,
        path=relative,
        ref_path=f"{relative}{REF_SUFFIX}",
        sha256=str(document.get("sha256") or ""),
        size_bytes=int(document.get("size_bytes") or 0),
        uploaded_by=str(document.get("uploaded_by") or ""),
        uploaded_at=str(document.get("uploaded_at") or ""),
        source=str(document.get("source") or ""),
        note=str(document.get("note") or ""),
        present=entity.is_file(),
        absolute_path=entity.resolve() if entity.exists() else entity,
    )


def inventory(worktree: Path | str) -> list[MaterialRef]:
    """这个 worktree 里用户交来的全部文件，按上传时间倒序（最新在前）。"""
    root = Path(worktree)
    directory = materials_dir(root)
    if not directory.is_dir():
        return []
    found: list[MaterialRef] = []
    for ref_path in sorted(directory.glob(f"*{REF_SUFFIX}")):
        document = _read_ref(ref_path)
        if document is None:
            continue
        found.append(_ref_from_document(root, ref_path.name[: -len(REF_SUFFIX)], document))
    found.sort(key=lambda item: (item.uploaded_at, item.name), reverse=True)
    return found


def fingerprint(worktree: Path | str) -> tuple[tuple[str, str], ...]:
    """"用户给了什么"的机械指纹 —— 给每轮上下文注入做差分用。"""
    return tuple((item.name, item.sha256) for item in inventory(worktree))


def materialize(worktree: Path | str) -> tuple[list[MaterialRef], list[MaterialRef]]:
    """按指针把字节补回这个 worktree。返回 (补齐的, 池里没有的)。

    新会话从 main 分出来时，`.ref` 随 Git 过来，字节不会 —— 这个函数就是那半步。
    幂等，可以在任何时刻重复调用。
    """
    root = Path(worktree)
    directory = materials_dir(root)
    if not directory.is_dir():
        return [], []
    try:
        pool = pool_root(root)
    except MaterialError:
        return [], []
    restored: list[MaterialRef] = []
    missing: list[MaterialRef] = []
    for reference in inventory(root):
        entity = root / reference.path
        if entity.is_file():
            continue
        if not reference.sha256:
            missing.append(reference)
            continue
        object_path = _object_path(pool, reference.sha256)
        if not object_path.is_file():
            missing.append(reference)
            continue
        _link_or_copy(object_path, entity)
        restored.append(reference)
    return restored, missing


# ── 渲染 ────────────────────────────────────────────────────────────────────


def human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def describe(references: Iterable[MaterialRef]) -> list[str]:
    """给模型看的行。**给绝对路径** —— 相对路径锚在节点目录上，给它等于让
    模型去猜锚点，而那正是"上传了却读不到"的另一半原因。"""
    lines: list[str] = []
    for reference in references:
        detail = f"- `{reference.absolute_path}`（{human_size(reference.size_bytes)}"
        if reference.sha256:
            detail += f"，sha256 {reference.sha256[:12]}"
        detail += "）"
        if reference.note:
            detail += f" —— 用户备注：{reference.note}"
        if not reference.present:
            detail += " ⚠️ 字节不在本工作区（池里找不到这个 sha）"
        lines.append(detail)
    return lines
