"""项目仓库的节点目录名单，必须和 harness 认识的节点对得上。

## 现场（2026-08-18）

接入 observation 时，harness 侧的名单改了（`core/project_bootstrap`、
`core/project_workspace`），后端这份没改。后果不是报错，是**仓库里根本没有
`observation/` 目录** —— 节点一写盘就被边界守卫判越界，而症状离病因隔着好几层。

更糟的是这份名单在后端还被手抄了两遍（建仓一份、修复一份），三份都回答同一个
问题："这个项目有哪些节点目录"。三份谁也不会因为对不上而报错。

## 这组测试守什么

守**跨仓一致**：后端铺的目录 ⊇ harness 的 producing 节点。不是守具体有哪几个
（那会随业务变），是守"两边不许各自演化"。

新增节点时这条会红，且红得说得清：它会点名哪个节点在 harness 里有、在后端
没有。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services.project_repository import (
    NODE_WORKSPACES,
    SYSTEM_WORKSPACES,
    _node_write_rules,
)

HARNESS_ROOT = Path(__file__).resolve().parents[3]


def _harness_workspace_table() -> dict[str, str]:
    """harness 侧的 `_NODE_WORKSPACES` —— 读源码文本，不 import。

    后端这份是镜像。镜像分叉的后果是静默的：节点写盘被判越界，报错指向"越界"
    而不是"两张表对不上"。所以对着真相源逐项比。
    """
    source = (HARNESS_ROOT / "core" / "project_workspace.py").read_text(encoding="utf-8")
    body = source[source.index("_NODE_WORKSPACES = {"):]
    body = body[: body.index("}") + 1]
    return dict(re.findall(r'"([A-Za-z_]+)":\s*"([^"]+)"', body))


def _harness_producing_nodes() -> set[str]:
    """harness 侧声明的 producing 节点 —— 读 nodes/ 目录，不写名单。

    刻意不 import harness 代码：后端测试不该依赖 harness 能被 import（CI 里
    两边是分开的 job）。目录约定本身就是 harness 的节点注册机制。
    """
    nodes_dir = HARNESS_ROOT / "nodes"
    if not nodes_dir.is_dir():
        return set()
    return {
        entry.name
        for entry in nodes_dir.iterdir()
        if entry.is_dir()
        and not entry.name.startswith((".", "_"))
        and (entry / "harness.yaml").is_file()
    }


def test_every_harness_producing_node_has_a_project_directory():
    """harness 里有的节点，仓库里必须有它的目录。

    没有目录 = 节点一写盘就被边界守卫判越界，而报错指向"越界"而不是"没建目录"。
    """
    harness_nodes = _harness_producing_nodes()
    if not harness_nodes:
        return          # 单独部署后端时 harness 不在盘上，跳过
    missing = sorted(harness_nodes - set(NODE_WORKSPACES))
    assert not missing, (
        f"这些节点在 harness 里有、后端没给它们建目录：{missing}\n"
        f"改 app/services/project_repository.NODE_WORKSPACES（access/nodes.yaml "
        f"的写权限段由它派生，不用另改）。"
    )


def test_write_rules_are_derived_from_the_single_manifest():
    """写权限段从名单派生，不手抄。

    本文件曾手抄两遍。抄件的问题不是重复，是**分叉时两边都不报错**。
    """
    rules = _node_write_rules()
    for node, directory in (*NODE_WORKSPACES.items(), *SYSTEM_WORKSPACES.items()):
        assert f"  {node}: {{write: [{directory}]}}" in rules
    assert len(re.findall(r"write: \[", rules)) == len(NODE_WORKSPACES) + len(SYSTEM_WORKSPACES)


def test_the_mirror_matches_the_harness_table_directory_by_directory():
    """节点 → 目录，两边逐项相等。改一边忘另一边时这条红，且点名哪个节点。"""
    harness = _harness_workspace_table()
    if not harness:
        return
    mirror = {**NODE_WORKSPACES, **SYSTEM_WORKSPACES}
    for node, directory in harness.items():
        if node.startswith("_") or node == "memory_curator":
            continue
        assert mirror.get(node) == directory, (
            f"{node}: harness 说它的目录是 {directory!r}，后端镜像说 {mirror.get(node)!r}"
        )
    # 调度器没有私人抽屉：它的目录不在 .research/ 下。
    assert not SYSTEM_WORKSPACES["orchestrator"].startswith(".research")


def test_the_manifest_is_not_hand_copied_anywhere_in_the_module():
    """名单不许长回来 —— 删掉抄件是一次性的，防复发才是重点。"""
    source = Path(
        __import__("app.services.project_repository", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    body = source[source.index("class ") :] if "class " in source else source
    for name in ("literature", "hypothesis", "experiment"):
        literal = f'"  {name}: {{write: [{name}]}}'
        assert literal not in body, (
            f"{name} 的写权限规则又被手抄进代码了；应当由 _node_write_rules() 派生"
        )
