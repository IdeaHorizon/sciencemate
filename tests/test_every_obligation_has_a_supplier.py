"""无人值守最低集合里的每一条，都得**真有后端能给**（#798 / #841）。

## 两个方向的病，同一个根：词表与机制对不上

**方向一：有义务、没供给（#798）。** `DISK_CAP` 是容器年代由 `--storage-opt` 提供的；
Docker 拆除之后没有任何原生后端能给（linux 的 per-scope 磁盘配额要先给文件系统配
quota，darwin / win32 根本没有）。它却一直留在 `UNATTENDED_MINIMUM` 里，于是每台原生
机器的 `missing_for_unattended` **永远非空**：

    真机（2026-09-05）missing_for_unattended = [disk_cap, git_unwritable, net_deny]

后果是共享执行器上无人值守被**永久**拒绝，而 `unattended.py` 给出的补救是"让管理员
补上资源限制就能续轮" —— 一句谁都执行不了的祈使句。零供给方的义务不是"暂时缺"，
是一条谁都满足不了的最低要求。**删掉。**

**方向二：有机制、没名字（#841）。** linux 的
`systemd-run --user --scope -p CPUQuota=` 一直在真的限 CPU（探针跑的就是它），可
`Invariant` 里没有 CPU 这一项。于是 `sandbox_contract.cpus` **没有任何东西能对上**：

    真机第五轮：模型读到 `sandbox_contract.cpus = 1`，据此判断"16 进程争抢单核、
    需取消重提"——而宿主上 `ps` 显示那个作业正有 8 个以上进程各占 93–97% CPU。
    它按记录做了正确的推理，得到了错误的结论，因为记录说的不是实情。

**有机制就得有名字**，否则记账问不出这件事。

## 判据：扫盘，不写名单

不逐条列"哪些不变量有供给"（那是名单，新加一条默认漏过）。扫 `core/isolation/` 的
后端源码，看每条不变量**有没有任何一处真的会把它加进 capabilities**。
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.isolation import ATTENDED_MINIMUM, UNATTENDED_MINIMUM, Invariant

ISOLATION = Path(__file__).resolve().parents[1] / "core" / "isolation"


def invariants_some_backend_can_supply() -> set[str]:
    """源码里**真的会被声明**的不变量 —— `Invariant.X` 出现在 caps 的加法里，
    或出现在 win32 探针字段表的值里。"""
    supplied: set[str] = set()
    names = {member.name: member.value for member in Invariant}
    # 只扫**后端**模块。`__init__.py` 是词表与最低集合的所在地 —— 把它算进来，
    # `UNATTENDED_MINIMUM = {... Invariant.DISK_CAP ...}` 这个 set 字面量本身就会被
    # 当成一处供给，于是"义务没有供给方"这道闸永远绿。第一版就是这么写的，靠变异
    # 才抓出来（[[feedback_asserting_the_label_asserts_nothing]]）。
    for path in sorted(ISOLATION.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # caps.add(Invariant.X) / caps.update({Invariant.X, ...})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in {"add", "update"}:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in names \
                            and isinstance(sub.value, ast.Name) and sub.value.id == "Invariant":
                        supplied.add(names[sub.attr])
            # frozenset({Invariant.X, ...}) —— darwin 的探针一次性返回一整套
            if isinstance(node, ast.Set):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in names \
                            and isinstance(sub.value, ast.Name) and sub.value.id == "Invariant":
                        supplied.add(names[sub.attr])
            # win32：{"write_boundary": Invariant.WRITE_BOUNDARY, ...}
            if isinstance(node, ast.Dict):
                for value in node.values:
                    if isinstance(value, ast.Attribute) and value.attr in names \
                            and isinstance(value.value, ast.Name) and value.value.id == "Invariant":
                        supplied.add(names[value.attr])
    return supplied


def test_the_scan_is_not_vacuous():
    supplied = invariants_some_backend_can_supply()
    assert len(supplied) >= 6, f"只扫出 {sorted(supplied)} —— 判据在空跑"
    assert "write_boundary" in supplied


def test_no_minimum_asks_for_something_nobody_can_supply():
    """最低集合里的每一条都要有供给方 —— 否则那不是要求，是永久拒绝。"""
    supplied = invariants_some_backend_can_supply()
    for label, minimum in (("无人值守", UNATTENDED_MINIMUM), ("人在场", ATTENDED_MINIMUM)):
        orphans = sorted(item.value for item in minimum if item.value not in supplied)
        assert not orphans, (
            f"{label}最低集合里这几条**没有任何后端能给**：{orphans}。\n"
            "零供给方的义务不是「暂时缺」，是一条谁都满足不了的最低要求 —— 它让每台机器"
            "永远差着这一条，而给出的补救根本够不着（#798 的 disk_cap 就是这么来的）。\n"
            "要么连着供给一起加，要么把这条从最低集合里删掉。"
        )


def test_disk_cap_is_gone_from_the_vocabulary():
    """删干净：留一个没人能给的名字在词表里，迟早有人把它加回最低集合。"""
    assert not hasattr(Invariant, "DISK_CAP")
    assert "disk_cap" not in {item.value for item in Invariant}


def test_cpu_has_a_name_because_something_enforces_it():
    """#841：linux 的 CPUQuota 一直在限 CPU，词表里得有这个名字。

    没有名字，`sandbox_contract.cpus` 就没有任何东西能对上 —— 模型只能把"提交时填的
    那个数"读成契约。
    """
    assert Invariant.CPU_CAP.value == "cpu_cap"
    assert "cpu_cap" in invariants_some_backend_can_supply(), (
        "CPU_CAP 进了词表却没有任何后端声明它 —— 那就成了 disk_cap 的镜像"
    )


def test_cpu_cap_is_not_a_gate_for_unattended():
    """CPU 配额只记账、不当门。

    一条吃满 CPU 的命令是慢，不是毁；把它写进最低集合会让 macOS / Windows 全体失去
    无人值守，换来的只是"更慢"这一种后果被提前拦下。
    """
    assert Invariant.CPU_CAP not in UNATTENDED_MINIMUM
    assert Invariant.CPU_CAP not in ATTENDED_MINIMUM
