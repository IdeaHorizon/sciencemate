"""写墙含不含元数据，是一个能被问出来的问题（#1095）。

Landlock 的访问位里**没有、也不可能有**管元数据的那一类 —— 内核文档明写
chmod/chown/utime/setxattr 这些"文件相关动作"限制不了。于是同一个
`write_boundary`：bwrap 下含内容+元数据（`--ro-bind / /`，连 chmod 都是 EROFS），
Landlock 下只含内容与目录项，可写根**之外**任意自有文件的权限位、时间戳、xattr
照样改得动（实测 chmod 0644→0600 rc=0）。

内容改不了，所以这不是内容篡改；能做的是把当前用户拥有的任何文件 `chmod 000`，
让之后的 run 或服务读不了 —— 可恢复的拒绝服务。**最结构性的一条是记账**：两种
后端此前声明同一个能力，strict 策略、UI 与节点从账上看不出差别，
「能力声明比兑现的宽」而没人看得见。

这条判据和 `net_scope` 同形：机制守到的口径不同，账上就得是两个词。
"""
from __future__ import annotations

from core.isolation import Invariant, enforcement_record


class _Backend:
    """最小后端替身：只回答能力集与口径。"""

    def __init__(self, caps, **extras):
        self.name = "fake"
        self._caps = frozenset(caps)
        for k, v in extras.items():
            setattr(self, k, v)

    def capabilities(self):
        return self._caps


def test_the_record_carries_the_write_boundary_scope() -> None:
    record = enforcement_record(
        _Backend({Invariant.WRITE_BOUNDARY}, write_boundary_scope="content_only"))
    assert record.as_event()["write_boundary_scope"] == "content_only"


def test_a_backend_that_cannot_answer_is_recorded_as_the_wide_one() -> None:
    """答不上来时按旧语义（含元数据）记 —— 只有真的只管内容的后端才报窄口径。

    反过来（默认记成窄）会把 seatbelt / win32 这些真的管住元数据的后端也说成
    有缺口，那是另一种不实。
    """
    record = enforcement_record(_Backend({Invariant.WRITE_BOUNDARY}))
    assert record.as_event()["write_boundary_scope"] == "content_and_metadata"


def test_no_write_boundary_means_no_scope() -> None:
    assert enforcement_record(_Backend(set())).as_event()["write_boundary_scope"] == "none"


def test_the_linux_backend_answers_per_probe(monkeypatch) -> None:
    """bwrap 与 Landlock 在同一台机器上必须答出不同的口径。

    这条才是缺陷本身：此前两条路都声明同一个 `write_boundary`。
    """
    from core.isolation.linux import LinuxBackend

    backend = LinuxBackend()
    monkeypatch.setattr(backend, "_probe", lambda: None)

    backend._bwrap, backend._landlock = "/usr/bin/bwrap", None
    assert backend.write_boundary_scope == "content_and_metadata"

    backend._bwrap, backend._landlock = None, True
    assert backend.write_boundary_scope == "content_only", (
        "Landlock 报了和 bwrap 一样的口径 —— 账上看不出元数据这块缺口")

    backend._bwrap, backend._landlock = None, None
    assert backend.write_boundary_scope == "none"
