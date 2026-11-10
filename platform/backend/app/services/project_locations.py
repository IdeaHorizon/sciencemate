"""Installation-local addresses of project repositories.

Project identity is independent of its filesystem address. Old projects retain
their derived address; a chosen address is stored once in the installation's
location index, never in the portable Git tree or an in-process cache. All Git
and workspace callers resolve it through GitProjectRepository.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from app.services.say import SaidToAPerson


class ProjectLocationError(SaidToAPerson):
    """An unusable project address, with a message suitable for the creation form."""


class ProjectLocations:
    def __init__(self, default_parent: Path):
        self.default_parent = default_parent

    def _entry(self, project_id: str) -> Path:
        # IDs are checked by GitProjectRepository before reaching this boundary.
        return self.default_parent / ".locations" / f"{project_id}.json"

    def custom_root(self, project_id: str) -> Path | None:
        entry = self._entry(project_id)
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
            root = Path(record["root_folder"])
            if record["project_id"] != project_id or not root.is_absolute():
                raise ValueError("invalid project location")
            return root
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProjectLocationError(
                "无法读取项目的根文件夹记录，请检查应用数据目录。",
                en="Could not read the project's root folder record. "
                "Check the application data directory.",
            ) from exc

    def root(self, project_id: str) -> Path:
        return self.custom_root(project_id) or self.default_parent / project_id

    def custom_roots(self) -> dict[str, Path]:
        """Explicit addresses for maintenance; moving app data must preserve these roots."""
        roots = {}
        try:
            entries = list((self.default_parent / ".locations").iterdir())
        except FileNotFoundError:
            return roots
        except OSError as exc:
            raise ProjectLocationError(
                "无法读取项目根文件夹索引，请检查应用数据目录的访问权限。",
                en="Cannot read the project folder index. "
                "Check access to the application data directory.",
            ) from exc
        for entry in entries:
            if entry.suffix != ".json":
                continue
            root = self.custom_root(entry.stem)
            if root is not None:
                roots[entry.stem] = root
        return roots

    def prepare(self, project_id: str, root_folder: str | None) -> Path:
        existing = self.custom_root(project_id)
        if existing and not (existing / ".git").is_dir():
            raise ProjectLocationError(
                "项目的根文件夹暂时不可用，请连接对应磁盘或恢复该文件夹后重试。",
                en="The project's root folder is unavailable. "
                "Reconnect its drive or restore the folder, then try again.",
            )
        if not root_folder:
            return existing or self.default_parent / project_id
        candidate = Path(root_folder).expanduser()
        if not candidate.is_absolute():
            raise ProjectLocationError(
                "请输入根文件夹的完整路径。",
                en="Enter an absolute root folder path.",
            )
        try:
            candidate = candidate.resolve()
            if existing:
                if candidate != existing:
                    raise ProjectLocationError(
                        "这个项目已经有根文件夹，不能在创建时重新指定。",
                        en="This project already has a root folder "
                        "and cannot be created at another address.",
                    )
                return existing
            # Do not replace a user's files or adopt another Git repository.
            if candidate.exists() and (not candidate.is_dir() or any(candidate.iterdir())):
                raise ProjectLocationError(
                    "请选择空文件夹，或输入一个新的文件夹路径。已有文件不会被覆盖。",
                    en="Choose an empty folder or enter a new folder path. "
                    "Existing files will not be overwritten.",
                )
            # Nested projects cannot be isolated or backed up independently.
            if any((parent / ".git").exists() for parent in candidate.parents):
                raise ProjectLocationError(
                    "请选择 Git 仓库之外的文件夹，避免项目互相嵌套。",
                    en="Choose a folder outside an existing Git repository "
                    "to keep projects separate.",
                )
        except (OSError, ValueError) as exc:
            raise ProjectLocationError(
                "无法访问这个文件夹，请检查路径和访问权限。",
                en="Cannot access this folder. Check its path and permissions.",
            ) from exc
        return candidate

    def register(self, project_id: str, root: Path) -> None:
        if root == self.default_parent / project_id:
            return
        entry = self._entry(project_id)
        entry.parent.mkdir(parents=True, exist_ok=True)
        temporary = entry.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"project_id": project_id, "root_folder": str(root)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(entry)
        finally:
            temporary.unlink(missing_ok=True)

    def unregister(self, project_id: str) -> None:
        """Undo an address reservation if installing its genesis repository failed."""
        self._entry(project_id).unlink(missing_ok=True)


def list_project_folders(default_parent: Path, path: str | None = None) -> dict:
    """Directory-only navigation on the local machine; no browser File API paths."""
    try:
        current = Path(path).expanduser() if path else Path.home()
        if not current.is_absolute():
            raise ValueError("relative directory")
        current = current.resolve(strict=True)
        entries = list(current.iterdir())
        directories = sorted(
            (
                {"name": item.name, "path": str(item)}
                for item in entries
                if not item.name.startswith(".") and item.is_dir()
            ),
            key=lambda item: item["name"].casefold(),
        )
        roots = (
            [
                str(Path(f"{letter}:\\"))
                for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                if Path(f"{letter}:\\").is_dir()
            ]
            if os.name == "nt"
            else ["/"]
        )
        return {
            "available": True,
            "default_parent": str(default_parent),
            "path": str(current),
            "parent": str(current.parent) if current.parent != current else None,
            "directories": directories,
            "roots": roots,
            "empty": not entries,
        }
    except (OSError, ValueError) as exc:
        raise ProjectLocationError(
            "无法打开这个文件夹，请检查路径和访问权限。",
            en="Cannot open this folder. Check its path and permissions.",
        ) from exc
