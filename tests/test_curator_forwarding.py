"""curator integration 的 artifact 必须真搬进子 run（issue #254 / #260）。

jicq 两次独立复现同一现象：
  read_artifact(literature_index…) → 找不到
  read_artifact(survey_report…)    → 找不到
  scan_artifact_disagreements(...) → status=success 但 scanned_artifacts=[]
  read_external_artifact(run_id=…) → 两个都读成功
  最终 receipt: artifacts_read=[两个]，artifacts_scanned=[]，n_unintegrated=2
  → 机械 QC integration_targets_actually_scanned 挂 → curator incomplete

根因链：
  1. orchestrator 起 curator 只在 node_inputs 传 `artifact_ids`（一串**字符串**），
     没传 `forward_artifact_ids` → artifact 实体没进 curator 的 run；
  2. `scan_artifact_disagreements` 只扫本地 `state.list_artifacts()`
     （shared/tools/library/disagreement_scan.py），跨 run 的读不算；
  3. 于是我 #229 加的整合收据记 scanned=[] → 门禁必挂。

**那是我造的门**：把"必须被 scan 扫到"当成整合的机械证据时，没验证 curator
拿不拿得到本地 artifact —— 跟 #151 一样的错误形状（造出谁都过不去的门）。

修法：forward 机制本来就有（executor 会把 upstream_artifacts 用 save_artifact
写进子 run 并标 `_forwarded_input`，`list_artifacts()` 看得见），缺的只是没接到
这条路径。现在框架在起 curator integration 时机械补齐 —— 光靠 prompt 指引不够，
模型漏传一次就又是一轮白跑。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from core.state import State


def _state() -> State:
    return State.new(node_type="_orchestrator", base_dir=Path(tempfile.mkdtemp()),
                     project_id="p1")


def _events(st: State, name: str) -> list[dict]:
    if not st.transcript_path.exists():
        return []
    out = []
    for line in st.transcript_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") == name:
            out.append(e)
    return out


# ── forward 机制本身：确认 forwarded artifact 进得了子 run 的 list_artifacts ──


def test_forwarded_artifact_is_visible_to_local_scan():
    """这是整条修复的前提：forward 进来的 artifact 必须能被 `list_artifacts()`
    看到 —— `scan_artifact_disagreements` 正是靠它取扫描目标的。"""
    child = State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()),
                      project_id="p1")
    # executor 对 upstream_artifacts 做的事（core/executor.py:153 起）
    child.save_artifact("survey_report", "x", "内容", metadata={"_forwarded_input": True})

    ids = [a["id"] for a in child.list_artifacts()]
    assert "survey_report__x" in ids

    # 扫描器取的就是 list_artifacts()（disagreement_scan.py:92），能看见即可被扫到
    import inspect

    from shared.tools.library import disagreement_scan
    assert "state.list_artifacts()" in inspect.getsource(disagreement_scan)


# ── 框架自动补 forward（核心修复）──────────────────────────────────────────


def test_source_declares_autoforward_for_curator():
    """接线存在性：起 _curator integration 时框架会自动补 forward_artifact_ids。

    用源码断言而非跑真实子 run —— 真跑要起 LLM。行为层由下面的
    receipt 测试 + 既有 test_curator_integration_receipt 覆盖。
    """
    import inspect

    import shared.tools.run_node as rn
    src = inspect.getsource(rn._run_node_tool)
    assert "curator_integration_autoforward" in src
    assert "forward_artifact_ids = _target_ids" in src
    # producer_run_id 也不指望模型传（实测是 null），从账本反查
    assert "producer_run_id" in src
    assert "pending_curator_integrations" in src


def test_orchestrator_guidance_tells_to_forward():
    """prompt 指引也要同步（机械兜底 + 指引双保险）。"""
    from core.loader import load_harness
    sp = load_harness("_orchestrator").system_prompt
    assert "forward_artifact_ids" in sp
    assert "#254" in sp or "254" in sp


# ── 修好之后：receipt 应判整合成功 ─────────────────────────────────────────


def test_receipt_passes_when_scan_sees_forwarded_targets():
    """forward 到位后，scan 扫得到 → n_unintegrated=0 → 门禁放行。"""
    from nodes._curator.hooks import build_integration_receipt

    st = State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()))
    targets = ["literature_index__x", "survey_report__x"]
    ni = {"mode": "integration", "trigger_node": "literature",
          "artifact_ids": targets, "producer_run_id": "run-lit-1"}
    records: list[dict] = [
        {"name": "read_artifact", "args": {"artifact_id": t},
         "result": {"status": "success"}} for t in targets]
    records.append({"name": "scan_artifact_disagreements", "args": {},
                    "result": {"status": "success", "scanned_artifacts": targets}})
    records.append({"name": "create_claim", "args": {},
                    "result": {"status": "success"}})
    r = build_integration_receipt(st, ni, tool_records=records)
    assert r["n_unintegrated"] == 0
    assert r["verdict"] == "ok"
    assert r["producer_run_id"] == "run-lit-1"      # 不再是 null


def test_receipt_reproduces_the_bug_without_forwarding():
    """回归锚点：没 forward 时的原始故障形态 —— 读到了但没扫到。

    这条**期望失败**（n_unintegrated=2），锁住"外部读取 ≠ 扫描成功"这个语义：
    将来若有人把判据放宽成"读到就算整合"，这条会红。
    """
    from nodes._curator.hooks import build_integration_receipt

    st = State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()))
    targets = ["literature_index__x", "survey_report__x"]
    ni = {"mode": "integration", "trigger_node": "literature",
          "artifact_ids": targets}
    records: list[dict] = []
    # 本地读失败（artifact 不在本 run）
    records += [{"name": "read_artifact", "args": {"artifact_id": t},
                 "result": {"status": "error", "error": "not found"}}
                for t in targets]
    # scan 只扫本地 → 空
    records.append({"name": "scan_artifact_disagreements", "args": {},
                    "result": {"status": "success", "scanned_artifacts": []}})
    # 跨 run 读成功（读得到，但不算扫到）
    records += [{"name": "read_external_artifact", "args": {"artifact_id": t},
                 "result": {"status": "success"}}
                for t in targets]
    r = build_integration_receipt(st, ni, tool_records=records)
    assert r["n_unintegrated"] == 2
    assert r["artifacts_scanned"] == []
    assert sorted(r["artifacts_read"]) == sorted(targets)   # 读到了
