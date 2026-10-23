"""内存墙不许有 swap 这条后门（#1207）。

linux 后端用 ``systemd-run --user --scope -p MemoryMax=…`` 给每条命令一片 cgroup。只设
MemoryMax 时，cgroup v2 的 ``memory.swap.max`` 默认是 max：超过上限的内存被**换出**而不是被杀。
node20 有 160 GB swapfile —— 一条失控命令在那上面不会 OOM，只会慢慢换页，直到 swap 用完或
墙钟超时，顺带拖慢同一台机器上的别人。声明了上限就该当场 OOM 并记原因
（``_memory_wall`` 读 memory.events），所以 MemoryMax 永远和 ``MemorySwapMax=0`` 成对出现。

两条判据：

1. 翻译限额的那一处（``_cgroup_properties``）：有内存上限就一定带 ``MemorySwapMax=0``，
   没内存上限就一个 Memory 属性都不带（别给没声明的作业凭空加墙）。
2. 每条命令会设的每一种属性，探针（``_PROBE_PROPERTIES``）都跑过 —— 探针成功才证明这台
   机器的 systemd 吃得下它们。属性名只在探针列表里出现过的才许用：这条把"加了一个新属性
   却没进探针、在老 systemd 上每条命令都起不来"的形状挡在仓库外。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from core.isolation.linux import _PROBE_PROPERTIES, _cgroup_properties


@dataclass
class _Limits:
    memory_bytes: int = 0
    pids: int = 0
    cpus: float = 0.0


def _names(props: list[str]) -> list[str]:
    assert props[::2] == ["-p"] * (len(props) // 2), props
    return [value.split("=", 1)[0] for value in props[1::2]]


def test_a_memory_limit_always_disables_swap() -> None:
    props = _cgroup_properties(_Limits(memory_bytes=512 * 1024 * 1024))
    assert "MemoryMax=536870912" in props
    assert "MemorySwapMax=0" in props, "只有 MemoryMax 没有 MemorySwapMax=0 = 有 swap 的机器上没有内存墙"
    assert props.index("MemorySwapMax=0") == props.index("MemoryMax=536870912") + 2, "两条成对、紧挨着"


def test_no_memory_limit_means_no_memory_properties_at_all() -> None:
    props = _cgroup_properties(_Limits(pids=64, cpus=2))
    assert not any(name.startswith("Memory") for name in _names(props)), props
    assert "TasksMax=64" in props and "CPUQuota=200%" in props


def test_every_property_a_command_may_set_was_probed() -> None:
    probed = {prop.split("=", 1)[0] for prop in _PROBE_PROPERTIES}
    used = set(_names(_cgroup_properties(_Limits(memory_bytes=1, pids=1, cpus=1.0))))
    assert used <= probed, (
        f"这些属性每条命令会设、探针却没跑过：{sorted(used - probed)} —— "
        "老 systemd 不认它时，探针照样成功、每条命令却起不来"
    )
    assert "MemorySwapMax" in probed


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd 是 Linux 的")
def test_the_probe_still_passes_with_the_swap_property_on_this_host() -> None:
    """真机：带 MemorySwapMax=0 的探针在有用户级 systemd 的机器上必须照样成功。
    没有用户级 systemd（CI 容器、无 delegation）就跳过——那不是这条测试要问的事。"""
    from core.isolation.linux import _probe_systemd

    exe, why = _probe_systemd()
    if exe is None and ("not installed" in why or "no user session" in why):
        pytest.skip(why)
    assert exe is not None, why
