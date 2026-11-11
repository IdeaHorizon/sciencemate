"""Build the headless personal-runtime source archive in either edition's desktop."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tarfile
import tomllib
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]


def build() -> Path:
    spec = importlib.util.spec_from_file_location(
        "runtime_git_tracked", Path(__file__).with_name("git_tracked.py")
    )
    tracked = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tracked)
    version = str(tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"])
    paths = [
        "platform/backend",
        "core",
        "shared",
        "nodes",
        "deploy/personal",
        "pyproject.toml",
        "uv.lock",
        "framework_exemptions.yaml",
    ]
    paths.extend(path.name for path in REPO.glob("*.py"))
    files = tracked.the_tracked_files(REPO, paths)
    # Personal runtime is core-only, even when assembled from the internal edition.
    files = [name for name in files if not name.startswith("platform/backend/app/pro/")]
    required = "deploy/personal/install-runtime.sh"
    if required not in files:
        raise RuntimeError("The personal runtime installer must be tracked by git before packaging")
    output = REPO / "dist/release" / f"sciencemate-runtime-{version}.tar.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = output.with_suffix("." + uuid4().hex + ".building")
    digest = hashlib.sha256()
    try:
        with tarfile.open(scratch, "w:gz") as archive:
            for name in files:
                source = REPO / name
                # Refuse symlinks: source archives must not dereference private host files.
                if source.is_symlink() or not source.is_file():
                    continue
                data = source.read_bytes()
                digest.update(name.encode() + b"\0" + data)
                destination = "install-runtime.sh" if name == required else "harness/" + name
                entry = tarfile.TarInfo(destination)
                entry.size = len(data)
                entry.mode = 0o755 if name.endswith(".sh") else 0o644
                archive.addfile(entry, io.BytesIO(data))
            for name, data in (
                ("VERSION", version.encode()),
                ("harness/PAYLOAD_VERSION", version.encode()),
                ("BUILD_ID", digest.hexdigest()[:16].encode()),
            ):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(data))
        scratch.replace(output)
    finally:
        scratch.unlink(missing_ok=True)
    return output


if __name__ == "__main__":
    print(build())
