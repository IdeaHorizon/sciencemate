"""身份是「这是谁」，位置是「它在哪」——两者不能混。

## 现场（2026-08-11 凌晨）

macOS 的 `/tmp` 午夜清理把整个 checkout 削了。把部署搬到不会被清理的目录、
重启之后，第一条消息就报：

    state_dir is already bound to a different identity

而身份**一个字都没变** —— tenant / project / session / 指令快照全一样，变的
只是它住在哪。marker 里把 `home_dir` 的**绝对路径**算进了身份：

    {"home_dir": "/private/tmp/p0-integration/...",   ← 位置
     "project_id": "...", "session_id": "...", "tenant_id": "..."}

**报错指向的原因是假的。** 这类假原因最贵：它让人去查一个不存在的问题
（"谁动了我的 session？"），而真相是"我把目录挪了"。

## 同一晚的镜像

几小时前修过 decision id：它**缺**了平台 run 作用域，于是恢复出来的会话
撞上不可变判据。这次是反过来 —— 身份里**多**了位置。

两次都是同一个问题：**没分清「这是谁」和「这一次/这一处」。**

## 判据

    身份不符   硬拒（真的是别人的 state dir，继续用会串数据）
    仅位置变   记一笔并把 marker 更新到新位置（这是搬迁，不是冲突）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import platform_runtime


#: schema 1 的 marker：身份里还带着指令快照的两个字段（RFC X3 之前）。
_V1_MARKER = {
    "schema_version": 1,
    "tenant_id": "t-1",
    "project_id": "p-1",
    "session_id": "s-1",
    "instruction_snapshot_id": "snap-1",
    "instruction_snapshot_sha256": "a" * 64,
}


def _bind(base: Path, home: Path, *, session_id: str = "s-1") -> Path:
    return platform_runtime._bind_runtime_identity(
        base,
        tenant_id="t-1",
        project_id="p-1",
        session_id=session_id,
        home_dir=home,
    )


def test_a_relocation_is_not_an_identity_conflict(tmp_path: Path) -> None:
    """搬目录不该报"绑到了不同的身份"。"""
    base = tmp_path / "state"
    base.mkdir()
    _bind(base, tmp_path / "home-old")

    marker = _bind(base, tmp_path / "home-new")          # 只有位置变了

    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["home_dir"] == str((tmp_path / "home-new").resolve())
    assert recorded["session_id"] == "s-1"


def test_a_different_session_is_still_refused(tmp_path: Path) -> None:
    """放松的只是位置。真的是别人的 state dir 仍然硬拒 —— 继续用会串数据。"""
    base = tmp_path / "state"
    base.mkdir()
    _bind(base, tmp_path / "home")

    with pytest.raises(platform_runtime.RequestError) as excinfo:
        _bind(base, tmp_path / "home", session_id="s-2")
    assert excinfo.value.code == "runtime_identity_conflict"


def test_a_v1_marker_is_upgraded_not_refused(tmp_path: Path) -> None:
    """旧 marker 里多出来的指令字段不是"另一个身份"，是旧格式。

    指令曾经算进身份（改一个字 = 另一次运行）。RFC X3 之后它是每轮现读的
    文件，身份里没有它的位置。**这条判据管的是升级那一刻**：机器上已经存在
    的 state dir 全是 schema 1，如果新代码把它们判成
    `runtime_identity_conflict`，这次改动就把它要修的那件事原样再犯一次
    —— 所有存量会话一条消息都发不出去。
    """
    base = tmp_path / "state"
    base.mkdir()
    marker = base / ".platform-runtime-identity.json"
    marker.write_text(
        json.dumps({**_V1_MARKER, "home_dir": str((tmp_path / "home").resolve())}),
        encoding="utf-8",
    )

    _bind(base, tmp_path / "home")          # 不抛

    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["schema_version"] == 2
    assert "instruction_snapshot_id" not in recorded
    assert recorded["session_id"] == "s-1"


def test_a_v1_marker_of_another_session_is_still_refused(tmp_path: Path) -> None:
    """升级只放过"同一个会话的旧格式"。别人的 state dir 照旧硬拒。"""
    base = tmp_path / "state"
    base.mkdir()
    marker = base / ".platform-runtime-identity.json"
    marker.write_text(
        json.dumps({**_V1_MARKER, "session_id": "someone-else",
                    "home_dir": str((tmp_path / "home").resolve())}),
        encoding="utf-8",
    )

    with pytest.raises(platform_runtime.RequestError) as excinfo:
        _bind(base, tmp_path / "home")
    assert excinfo.value.code == "runtime_identity_conflict"


def test_the_first_bind_records_both(tmp_path: Path) -> None:
    """位置仍然记着 —— 取消的是"拿它当身份"，不是"不记它"。"""
    base = tmp_path / "state"
    base.mkdir()
    marker = _bind(base, tmp_path / "home")
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["home_dir"] == str((tmp_path / "home").resolve())
    assert recorded["tenant_id"] == "t-1"
