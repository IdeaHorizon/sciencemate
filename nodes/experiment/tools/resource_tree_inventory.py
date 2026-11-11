"""Read-only inventory in the filesystem namespace that created the checkout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


_MAX_TREE_ENTRIES = 100_000


class TreeInventoryError(RuntimeError):
    """A checkout cannot be safely imported."""


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _tree_inventory(root: Path) -> tuple[str, int, int]:
    """Hash a downloaded tree without following any downloaded symlink."""
    digest = hashlib.sha256()
    size = 0
    entries = 0
    resolved_root = root.resolve(strict=True)

    def visit(directory: Path) -> None:
        nonlocal size, entries
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            # Git's commit is the reproducibility identity.  Administrative
            # files contain clone-local reflog timestamps and would make a
            # checkout content hash nondeterministic.
            if entry.name == ".git":
                continue
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            info = entry.stat(follow_symlinks=False)
            entries += 1
            if entries > _MAX_TREE_ENTRIES:
                raise TreeInventoryError(
                    f"下载树超过 {_MAX_TREE_ENTRIES} 个条目；已丢弃，未导入 destination"
                )
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                target_path = Path(target)
                if target_path.anchor:
                    raise TreeInventoryError(f"下载树含绝对 symlink：{relative} -> {target}")
                # Reject links that leave the root and re-enter using its
                # staging name. They would point elsewhere after import.
                depth = len(path.parent.relative_to(root).parts)
                for component in target_path.parts:
                    depth += -1 if component == ".." else 1
                    if depth < 0:
                        break
                if depth < 0 or not (path.parent / target_path).resolve(
                    strict=False
                ).is_relative_to(resolved_root):
                    raise TreeInventoryError(f"下载树含越界 symlink：{relative} -> {target}")
                digest.update(f"L\0{relative}\0{mode:o}\0{target}\0".encode())
            elif stat.S_ISDIR(info.st_mode):
                digest.update(f"D\0{relative}\0{mode:o}\0".encode())
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                file_hash, file_size = _sha256_file(path)
                size += file_size
                digest.update(f"F\0{relative}\0{mode:o}\0{file_size}\0{file_hash}\0".encode())
            else:
                raise TreeInventoryError(f"下载树含不支持的特殊文件：{relative}")

    visit(root)
    return digest.hexdigest(), size, entries


if __name__ == "__main__":
    try:
        print(json.dumps(_tree_inventory(Path(sys.argv[1]))))
    except (OSError, RuntimeError, ValueError) as exc:
        sys.exit(str(exc))
