"""User identity —— 让 KB 记录知道是谁写的。

每个 v3 KB record 写入时填 `created_by_user_id`，从 `~/.harness-framework/user/identity.json` 读。
没找到就启动时自动生成（取 `$USER` 或 `git config user.email`），写一份。

identity.json 格式：
    {
      "user_id": "wangd",                 # 短稳定 id
      "display_name": "Di Wang",
      "email": "wangd@example.com",
      "created_at": "2026-05-15T..."
    }

环境变量覆盖：HARNESS_FRAMEWORK_USER_ID
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from core.paths import identity_path, user_root


def _read_git_email() -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _read_git_name() -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _auto_default() -> dict:
    """生成一份默认 identity（首次启动）。"""
    email = _read_git_email() or ""
    user_id = email.split("@")[0] if email else os.getenv("USER", "anonymous")
    return {
        "user_id": user_id,
        "display_name": _read_git_name() or user_id,
        "email": email,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def load_identity() -> dict:
    """读 identity.json。不存在就 auto 生成并写盘。"""
    p = identity_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # auto-init
    user_root().mkdir(parents=True, exist_ok=True)
    rec = _auto_default()
    p.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return rec


def current_user_id() -> str:
    """供 KB 记录 `created_by_user_id` 用。环境变量优先。"""
    override = os.getenv("HARNESS_FRAMEWORK_USER_ID")
    if override:
        return override
    return load_identity()["user_id"]
