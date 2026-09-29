"""流程图上谁是"车站"，必须等于节点自己声明的角色。

## 现场（wangd 2026-08-21）

> 「observation 这种节点也算是生产节点了，为啥在流程图里是小的呢？」

`observation` 是治理型产出节点（`post_run_flow: full`，跑完走 review → decision），
但前端 `research-map.ts` 里那张 `STATION_ORDER` 名单是 observation 加进来**之前**
写的，于是它被画成了服务型小卫星。

判据本身没错 —— 车站 = 治理型产出节点，卫星 = 服务型（literature / data /
postprocess，被消费方随时调用）+ 架构节点（`_` 开头）。错的是这张名单是**抄件**，
而抄件分叉时两边都不报错（[[一个问题一个真相源]]）。

## 为什么不干脆让前端别抄

前端读不到 `harness.yaml`。真正的消除是把角色随事件流带到前端（nodeType 旁边
再带一个 role），那是 API 契约的改动，值得做但不在这一刀里。

在那之前，抄件可以存在 —— **分叉不可以静默**。这个测试直接读两边：一边是
`nodes/*/harness.yaml` 的 `post_run_flow`，一边是 TS 源码里的 `STATION_ORDER`。
新增一个产出节点而忘了改前端，CI 当场红。
"""
from __future__ import annotations

import pathlib
import re

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
MAP_SOURCE = REPO / "platform/frontend/src/features/execution/lib/research-map.ts"

#: `core/harness.py` 的 `is_service`：`post_run_flow == "none"` 才是服务型。
#: 不写这个字段 = 默认 `full` = 治理型产出节点。
_DEFAULT_FLOW = "full"


def _declared_station_types() -> set[str]:
    stations: set[str] = set()
    for harness in sorted((REPO / "nodes").glob("*/harness.yaml")):
        node_type = harness.parent.name
        if node_type.startswith("_"):
            continue  # 架构节点（_reviewer / _curator）永远是卫星
        try:
            spec = yaml.safe_load(harness.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if str(spec.get("post_run_flow") or _DEFAULT_FLOW) != "none":
            stations.add(node_type)
    return stations


def _frontend_station_types() -> set[str]:
    source = MAP_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"const STATION_ORDER = \[(.*?)\] as const;", source, re.S)
    assert match, f"{MAP_SOURCE} 里找不到 STATION_ORDER —— 判据的锚点没了"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_flowchart_stations_are_exactly_the_governed_producing_nodes() -> None:
    declared = _declared_station_types()
    frontend = _frontend_station_types()
    missing = declared - frontend
    extra = frontend - declared
    assert not missing, (
        f"这些节点声明了走治理链（post_run_flow != none），但流程图把它们画成小卫星："
        f"{sorted(missing)}\n  → 在 {MAP_SOURCE.relative_to(REPO)} 的 STATION_ORDER 里加上"
    )
    assert not extra, (
        f"流程图把这些当车站，但它们的 harness.yaml 说是服务型：{sorted(extra)}"
    )


def test_the_criterion_has_something_to_check() -> None:
    """两边都非空 —— 空集合上一切断言都成立，护栏最常见的死法。"""
    declared = _declared_station_types()
    assert len(declared) >= 3, f"只认出 {sorted(declared)} 个产出节点，判据大概率读错了"
    assert "observation" in declared, "observation 应当被认成治理型产出节点"
