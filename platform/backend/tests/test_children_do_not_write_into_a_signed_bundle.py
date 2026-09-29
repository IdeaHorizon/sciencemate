"""被要求别写 .pyc 的进程，它起的 harness 子进程也不许写。

## 为什么这条要有判据

Mac 的 `.app` 是签好名的，往里写一个字节签名就不再成立 —— 用户看到的是"应用已
损坏"，而打包机上一切正常。壳用 `-B` 起后端正是为此；但 worker 的环境是由
`harness_subprocess_env` 从一份白名单**重建**的，`PYTHONDONTWRITEBYTECODE` 不在
里面，于是 worker import `site-packages` 时照写不误。

2026-09-16 打 0.5.0 时真机撞上：装完自检里 `personal_smoke` 跑完（回复都拿到了），
随后那次 `codesign --verify --deep --strict` 列出一串 `file added: …/__pycache__/…`。

判据落在**环境上**而不是"有没有 .pyc"：后者要跑一次真 worker 才看得见，而那正是
装完自检才做的事；这里要的是一条能在单测里转红的机械判据。
"""
from __future__ import annotations

from app.services.harness_runtime import harness_subprocess_env


def test_children_inherit_the_no_bytecode_flag(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("sys.dont_write_bytecode", True)
    env = harness_subprocess_env(tmp_path)
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1", (
        "后端被要求别写 .pyc，它起的 worker 却没被要求 —— 签好名的 bundle 会被写坏"
    )


def test_a_normal_run_leaves_bytecode_alone(monkeypatch, tmp_path) -> None:
    """开发机上不设它：那里写 .pyc 是有益的，而且没有签名可破。"""
    monkeypatch.setattr("sys.dont_write_bytecode", False)
    assert "PYTHONDONTWRITEBYTECODE" not in harness_subprocess_env(tmp_path)
