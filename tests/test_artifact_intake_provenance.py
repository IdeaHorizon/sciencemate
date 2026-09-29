"""外部材料的合法入口，以及**来源不许被洗白**。

2026-08-04 e2e8 实测：用户给了一批做完的研究材料，只要求写成论文。调度器一路
走对了，最后撞在 writing 的机械输入门上 —— `required_input_artifact_types:
[experiment_log]`，而 experiment_log 只有 experiment 节点能产。它自己的原话：

    我需要先起 experiment 节点让它基于已有材料产出 experiment_log artifact。
    但这样会重复跑实验……或者我可以用 "replay" 或 "register_only" 模式

**它主动想避免重跑，框架没给它合法出口。**

这份测试锁两件事：
  ① 有出口 —— import_artifact 能让外部材料满足机械输入门
  ② 出口不是后门 —— imported 标记转发/回填多少次都洗不掉，且下游必须如实披露

②比①重要得多。只做①就是把防编数据的门禁开了个洞。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core import artifact_provenance as prov
from core.state import State


def _worktree(tmp_path):
    """P6（2026-08-08）：位置即来源 —— 导入源必须在 <worktree>/sources/ 下，
    所以这些测试需要一个真 worktree。规则本身在
    test_import_artifact_rejects_owned_outputs.py 里单测。"""
    import subprocess

    root = tmp_path / "wt"
    (root / "sources").mkdir(parents=True, exist_ok=True)
    if (root / ".git").exists():
        return root
    for args in (["init", "-b", "main"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@e.test"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "project.yaml").write_text("schema_version: 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"],
                   check=True, capture_output=True)
    return root


def _state(tmp_path, node_type: str, run_id: str = "run-1") -> State:
    from core.project_workspace import bind_project_workspace

    st = State.new(node_type=node_type, base_dir=tmp_path / node_type,
                   project_id="p1")
    bind_project_workspace(st, _worktree(tmp_path))
    return st


def _run_local_state(tmp_path, node_type: str) -> State:
    """没绑 worktree 的 run（CLI / fixture 回放）：产物落 run 本地账。

    executor 只在这种 run 里真正搬运上游产物（save_artifact + forwarded
    provenance）；绑了同一 worktree 的 run 直读共享账本，没有转发这一步 ——
    同名记录的普通覆盖会被 State 当成跨节点改写拒掉。
    """
    return State.new(node_type=node_type, base_dir=tmp_path / f"{node_type}-local",
                     project_id="p1")


@pytest.fixture()
def source_file(tmp_path):
    sources = tmp_path / "wt" / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    p = sources / "external_experiment_log.md"
    p.write_text("# 别人做完的实验\n\nn=50, seeds 10-13, 全部通过污染检测。\n")
    return p


def _import(state, **kw):
    from shared.tools.library.artifact_intake import _import_artifact
    return asyncio.run(_import_artifact(state, **kw))


# ── ① 出口存在 ──────────────────────────────────────────────────────────

def test_import_creates_artifact_that_satisfies_input_gate(tmp_path, source_file):
    """机械输入门看的是 list_artifacts 里有没有这个 type —— 导入的算数。"""
    st = _state(tmp_path, "_orchestrator")
    res = _import(st, artifact_type="experiment_log", name="external_log",
                  source_path=str(source_file))
    assert res["status"] == "success"
    types = {a["type"] for a in st.list_artifacts()}
    assert "experiment_log" in types


def test_import_records_source_hash_and_size(tmp_path, source_file):
    """事后可核：源在哪、内容是什么、多大。"""
    st = _state(tmp_path, "_orchestrator")
    res = _import(st, artifact_type="experiment_log", name="external_log",
                  source_path=str(source_file))
    rec = st.read_artifact(res["artifact_id"])
    p = prov.of(rec)
    assert p["kind"] == prov.KIND_IMPORTED
    assert p["source_path"].endswith("external_experiment_log.md")
    assert len(p["source_sha256"]) == 64
    assert p["source_size_bytes"] == source_file.stat().st_size


def test_import_does_not_impersonate_a_producing_node(tmp_path, source_file):
    """导入的工件不许冒充 experiment 产的 —— 任何"必须由 X 产出"的校验都该判否。"""
    st = _state(tmp_path, "_orchestrator")
    res = _import(st, artifact_type="experiment_log", name="external_log",
                  source_path=str(source_file))
    rec = st.read_artifact(res["artifact_id"])
    assert rec["produced_by_node_type"] == prov.IMPORT_NODE_TYPE
    assert rec["produced_by_node_type"] != "experiment"


# ── ② 出口不是后门 ──────────────────────────────────────────────────────

def test_imported_survives_forwarding_into_child_run(tmp_path, source_file):
    """executor 把上游工件注入子 run —— 此前接收方会被记成产出方。

    这是洗白路径 1：外部 experiment_log 转发进 writing 子 run，一次就变成
    "writing 产的"。转发只发生在**没绑 worktree** 的子 run（executor 真搬文件
    的那条路）；绑同一 worktree 的子 run 直读共享账本，没有这一步。
    """
    st = _state(tmp_path, "_orchestrator")
    res = _import(st, artifact_type="experiment_log", name="external_log",
                  source_path=str(source_file))
    src = st.read_artifact(res["artifact_id"])

    child = _run_local_state(tmp_path, "writing")
    child.save_artifact(
        artifact_type=src["type"], name=src["name"], content=src["content"],
        metadata={"_forwarded_input": True},
        provenance=prov.forwarded(prov.of(src), via_node_type="writing",
                                  via_run_id="run-child"),
    )
    got = child.read_artifact(res["artifact_id"])
    assert prov.is_imported(got), "转发一次就被洗成平台自产"
    assert got["produced_by_node_type"] != "writing"


def test_imported_survives_repeated_forwarding(tmp_path, source_file):
    """转发三次还是导入的 —— origin_kind 不许在链条中段被覆盖。"""
    st = _state(tmp_path, "_orchestrator")
    res = _import(st, artifact_type="experiment_log", name="external_log",
                  source_path=str(source_file))
    p = prov.of(st.read_artifact(res["artifact_id"]))
    for hop in ("writing", "_reviewer", "_curator"):
        p = prov.forwarded(p, via_node_type=hop, via_run_id=f"run-{hop}")
    assert prov.is_imported({"provenance": p})


def test_default_save_is_still_produced(tmp_path):
    """不传 provenance 的老调用方行为不变 —— 本 run 自己产的。"""
    st = _state(tmp_path, "experiment")
    r = st.save_artifact("experiment_log", "mine", "content")
    rec = st.read_artifact(r["id"])
    assert prov.of(rec)["kind"] == prov.KIND_PRODUCED
    assert rec["produced_by_node_type"] == "experiment"
    assert not prov.is_imported(rec)


def test_forwarding_a_produced_artifact_keeps_true_producer(tmp_path):
    """回填不是产出。父节点不许把子节点的活记到自己头上。

    回填（父 run 再 save 一次子 run 的产物）只在没绑 worktree 的 run 里存在；
    同一 worktree 里同 id 就是同一份记录，普通覆盖会被当成跨节点改写拒掉。
    """
    child = _state(tmp_path, "experiment", run_id="run-child")
    r = child.save_artifact("experiment_log", "real", "content")
    src = child.read_artifact(r["id"])

    parent = _run_local_state(tmp_path, "_orchestrator")
    parent.save_artifact(
        "experiment_log", "real", src["content"],
        provenance=prov.forwarded(prov.of(src), via_node_type="_orchestrator",
                                  via_run_id="run-1"))
    rec = parent.read_artifact(r["id"])
    assert rec["produced_by_node_type"] == "experiment"
    assert prov.true_producer(rec) == ("experiment", child.run_id)


def test_old_artifact_without_provenance_does_not_crash(tmp_path):
    """v3.2 之前的旧工件没有 provenance 块 —— 不许因此报错或被误判为导入。"""
    assert prov.is_imported({"type": "x", "produced_by_node_type": "experiment"}) is False
    assert prov.true_producer(
        {"produced_by_node_type": "experiment", "produced_by_run_id": "r"}
    ) == ("experiment", "r")


def test_forwarded_without_source_falls_back_to_produced(tmp_path):
    """拿不到源 provenance 就退回旧行为，不凭空编来源。"""
    p = prov.forwarded(None, via_node_type="writing", via_run_id="r")
    assert p["kind"] == prov.KIND_PRODUCED
    assert p["by_node_type"] == "writing"


# ── 边界 ────────────────────────────────────────────────────────────────

def test_only_orchestrator_may_import():
    """producing 节点自己导入 = 自产自销，正是要防的。

    角色归注册表（判决拆除·第三波）：ToolDefinition.allowed_node_types 让
    producing 节点的工具面上根本没有 import_artifact，函数体不再查角色。
    """
    from core.bootstrap import bootstrap
    from core.tool_registry import get_tool, list_tools_for_node

    bootstrap()
    assert get_tool("import_artifact").allowed_node_types == ["_orchestrator"]
    assert not list_tools_for_node("writing", ["import_artifact"])
    assert [t.name for t in list_tools_for_node("_orchestrator", ["import_artifact"])] == ["import_artifact"]


def test_missing_source_is_refused(tmp_path):
    """不许无源导入 —— 那就成了换个名字的伪造。"""
    st = _state(tmp_path, "_orchestrator")
    res = _import(st, artifact_type="experiment_log", name="x",
                  source_path=str(tmp_path / "nope.md"))
    assert res["status"] == "error"


def test_frozen_metadata_is_stripped(tmp_path):
    """导入一份"已冻结"的预注册不能解开 experiment 的冻结门禁。

    executor 的 tamper-evident 检查读 metadata.frozen；能自带 frozen 就等于
    "先做实验后补预注册"合法化了，防确认偏误的时序整个作废。
    """
    st = _state(tmp_path, "_orchestrator")
    src = tmp_path / "wt" / "sources" / "prereg.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps({
        "type": "pre_registration", "name": "ext",
        "content": "H1: ...",
        "metadata": {"frozen": True, "frozen_at": "2026-01-01"},
    }, ensure_ascii=False))
    res = _import(st, artifact_type="pre_registration", name="ext",
                  source_path=str(src))
    assert res["status"] == "success"
    rec = st.read_artifact(res["artifact_id"])
    assert not rec["metadata"].get("frozen")
    assert "frozen" in res["stripped_metadata"]


def test_cannot_inherit_produced_by_from_source_file(tmp_path):
    """源文件里写着 produced_by_node_type=experiment 也不作数。"""
    st = _state(tmp_path, "_orchestrator")
    src = tmp_path / "wt" / "sources" / "log.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps({
        "type": "experiment_log", "name": "ext", "content": "data",
        "metadata": {"produced_by_node_type": "experiment",
                     "produced_by_run_id": "fake-run"},
    }, ensure_ascii=False))
    res = _import(st, artifact_type="experiment_log", name="ext",
                  source_path=str(src))
    rec = st.read_artifact(res["artifact_id"])
    assert rec["metadata"].get("produced_by_node_type") is None
    assert rec["produced_by_node_type"] == prov.IMPORT_NODE_TYPE


def test_import_output_is_bounded(tmp_path):
    """导入不该成为绕过有界读的后门（今天刚有个 run 死在 2MB 单行文件上）。"""
    from shared.tools.library.artifact_intake import _MAX_IMPORT_BYTES
    st = _state(tmp_path, "_orchestrator")
    src = tmp_path / "wt" / "sources" / "huge.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x" * (_MAX_IMPORT_BYTES * 3))
    res = _import(st, artifact_type="dataset", name="huge", source_path=str(src))
    assert res["content_truncated"] is True
    rec = st.read_artifact(res["artifact_id"])
    assert len(rec["content"].encode()) <= _MAX_IMPORT_BYTES
    # sha256 记的是完整文件，不是截断后的
    assert prov.of(rec)["source_size_bytes"] == src.stat().st_size


def test_orchestrator_saving_a_producing_type_is_stamped_not_refused(tmp_path):
    """判决拆除第三波（builtin:312 降格）：调度器当初撞在「不许新建 experiment_log」
    这条报错上、打算重跑实验。墙删了：写得进去，但章是框架盖的 —— 一份
    `_orchestrator` 名下的 experiment_log 与 import_artifact 登记的外来材料一样，
    出处如实（前者是代笔，后者是 imported），referee 终审看得见。墙加回去这条转红。"""
    from shared.tools.builtin import _save_artifact
    st = _state(tmp_path, "_orchestrator")
    res = asyncio.run(_save_artifact(st, artifact_type="experiment_log",
                                     name="forged", content="x"))
    assert res["status"] == "success", res
    rec = st.read_artifact(res["id"])
    assert rec["produced_by_node_type"] == "_orchestrator"
    assert rec["produced_by_node_type"] != "experiment"
