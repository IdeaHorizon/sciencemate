"""「这个项目产出了什么」只有一个答案。

## 现场（wangd 2026-09-09）

「这个文件管理系统很混乱，比如每个节点一大堆没用的 log。」

在一个**真项目**上量过（`afs-local-deploy` 的 nonnormal_ews，204 件产物）：
12 件是交付物，而 `compression_log` 26 件、`latex_build_receipt` 18 件、
`visual_*` 42 件。用户要找的论文和这些平铺在一起。（compression_log 此后已随
run 生灭、不进研究记录；夹具里的框架内务用留在记录里的 `run_manifest`。）

## 判据的形状

分层必须是**推导出来的**，不是这里写一份"哪些类型算内务"的名单 —— 名单对
新类型默认失效，而失效的表现是"新的内务类型被当成研究产出摆到用户面前"，
没有任何报错。所以下面有一条测试专门盯这个：新造一个策略表里没有的类型，
它必须落进最保守那一档。

记录的形状取自真实产物（`literature_index` / `manuscript` 两个真样本），
不是我自己想象的 schema（[[feedback_test_data_must_look_real]]）：正文是节点
目录下的原生文件，类型 / 出处 / 版本 / 冻结在工作区账本（`core/ledger`）里。
"""
from __future__ import annotations

from pathlib import Path

from core import catalog
from core.ledger import workspace_store, write_record
from core.project_workspace import _NODE_WORKSPACES as _DIRS


def _record(root: Path, *, type: str, name: str, directory: str, content: str = "{}",
            metadata: dict | None = None, produced_by_node_type: str = "literature",
            produced_by_run_id: str = "1788160373-daa391",
            created_at: str = "2026-09-09T07:21:00.291900+00:00",
            version: int = 1, frozen: bool = False) -> str:
    """落一份研究记录 —— 字段照真样本的形状（provenance / produced_by_* / created_at）。

    `version` > 1：同一身份连续 save 到那一版（账本记版本）；`frozen`：再追一行
    freeze（形状照 `core.ledger.RecordStore.freeze`）。返回 artifact id。
    """
    rec: dict = {}
    for _ in range(version):
        rec = write_record(
            root, artifact_type=type, name=name, content=content, directory=directory,
            metadata=metadata or {}, produced_by_node_type=produced_by_node_type,
            produced_by_run_id=produced_by_run_id, created_at=created_at,
        )
    if frozen:
        workspace_store(root).freeze(
            rec["id"], metadata_patch={}, by_node=produced_by_node_type,
            by_run=produced_by_run_id, frozen_at="2026-09-09T08:00:00+00:00",
        )
    return rec["id"]


def _project(tmp_path: Path) -> Path:
    """一个够真的工作区：冻结的论文 + 它的 PDF、没冻的草稿、一堆框架内务。"""
    root = tmp_path / "wt"
    (root / "paper/latex_build/sci").mkdir(parents=True)
    (root / "paper/latex_build/sci/main.pdf").write_bytes(b"%PDF-1.7\n")
    _record(
        root, type="manuscript", name="podsys", directory=_DIRS["writing"],
        version=11, frozen=True, produced_by_node_type="writing",
        metadata={"pdf_path": str(root / "paper/latex_build/sci/main.pdf")},
    )

    _record(root, type="writing_preflight_plan", name="preflight",
            directory=_DIRS["writing"], produced_by_node_type="writing")
    for turn in range(3):
        _record(root, type="run_manifest", name=f"run_manifest_r{turn}",
                directory=_DIRS["experiment"], produced_by_node_type="experiment")
    _record(root, type="survey_report", name="WMLES_survey", directory=_DIRS["literature"])
    return root


def test_the_paper_is_the_first_thing_you_see(tmp_path: Path) -> None:
    """交付物排在最前 —— 用户要找的论文不该被 26 条压缩日志埋掉。"""
    entries = catalog.build(_project(tmp_path))

    assert entries[0].kind == "manuscript", [e.kind for e in entries[:3]]
    assert entries[0].is_deliverable
    assert entries[0].version == 11, "版本要报冻结的那一版"
    assert "paper/latex_build/sci/main.pdf" in entries[0].files, (
        "论文那一行必须带上用户点得开的 PDF —— 让人去点一份 .tex 才知道论文在"
        "哪，正是这次要修的毛病"
    )


def test_bookkeeping_is_sorted_out_by_a_rule_not_a_list(tmp_path: Path) -> None:
    grouped = catalog.by_tier(catalog.build(_project(tmp_path)))

    assert [e.kind for e in grouped[catalog.TIER_DELIVERABLE]] == ["manuscript"]
    assert {e.kind for e in grouped[catalog.TIER_OUTPUT]} == {"survey_report"}
    assert {e.kind for e in grouped[catalog.TIER_WORKING]} == {
        "run_manifest", "writing_preflight_plan",
    }


def test_an_unknown_type_lands_in_the_most_conservative_tier(tmp_path: Path) -> None:
    """策略表里没有的类型不许默认当成研究产出摆出来。

    这一条盯的是**判据的形状**：写名单的话，新类型默认漏进"研究产出"，而且
    没人会发现。
    """
    root = _project(tmp_path)
    new_id = _record(root, type="a_type_nobody_declared_yet", name="new",
                     directory=_DIRS["experiment"], produced_by_node_type="experiment")

    [entry] = [e for e in catalog.build(root) if e.artifact_id == new_id]
    assert entry.tier == catalog.TIER_WORKING
    assert entry.is_deliverable is False


def test_nothing_is_dropped_only_ordered(tmp_path: Path) -> None:
    """分层是排序不是过滤 —— 少列一条，用户就会以为那件东西不存在。"""
    root = _project(tmp_path)
    on_ledger = set(workspace_store(root).heads())
    listed = {e.artifact_id for e in catalog.build(root)}
    assert on_ledger <= listed, on_ledger - listed


def test_the_catalog_is_computed_not_stored(tmp_path: Path) -> None:
    """重建一次，结果必须逐字相同 —— 它是索引，不是第二套账。"""
    root = _project(tmp_path)
    first = [e.as_dict() for e in catalog.build(root)]
    second = [e.as_dict() for e in catalog.build(root)]
    assert first == second
    assert not list(root.rglob("*catalog*")), "目录本身绝不落盘"


def test_a_frozen_record_whose_file_vanished_is_not_a_deliverable(tmp_path: Path) -> None:
    """账本里有、盘上没有：说出来只会让用户去点一个 404。"""
    root = tmp_path / "wt"
    gone = _record(root, type="manuscript", name="gone", directory=_DIRS["writing"],
                   produced_by_node_type="writing", version=3, frozen=True)
    store = workspace_store(root)
    store.abs_path(store.head(gone)).unlink()

    assert catalog.build(root) == []


def test_the_paper_outranks_the_figures_and_the_data(tmp_path: Path) -> None:
    """同一档之内按**读者要什么**排，不是按类型名的字母序。

    2026-09-10 本机真机看出来的：`figure` 字母序在 `manuscript` 前面，于是
    "论文"在交付物里排第二。换成那个 264 件产物的真项目，交付物里有 8 份
    `clean_results` + 3 张图，论文会被压到第 12 行 —— 而用户来这一页就是找它的。
    """
    root = _project(tmp_path)
    for name in ("fig1_latency", "fig2_throughput"):
        _record(root, type="figure", name=name, directory=_DIRS["postprocess"],
                produced_by_node_type="postprocess", frozen=True)
    _record(root, type="clean_results", name="run1", directory=_DIRS["experiment"],
            produced_by_node_type="experiment", frozen=True)

    delivered = catalog.deliverables(catalog.build(root))
    assert [e.kind for e in delivered] == [
        "manuscript", "figure", "figure", "clean_results",
    ], [e.kind for e in delivered]


def test_an_unranked_deliverable_still_shows_up(tmp_path: Path) -> None:
    """排序不是排除 —— 优先级表里没有的类型排在最后，但仍在列表里。"""
    root = _project(tmp_path)
    unranked = _record(root, type="a_type_nobody_ranked", name="x",
                       directory=_DIRS["derivation"], produced_by_node_type="derivation")
    entries = catalog.build(root)
    assert unranked in {e.artifact_id for e in entries}
