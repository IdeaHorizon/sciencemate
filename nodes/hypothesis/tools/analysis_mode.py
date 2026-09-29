"""Analysis 的两档请求模式 —— 出计划 vs 改计划。

## 事故

v2.1 把 hypothesis 升格成 Analysis（研究计划的持续维护者 + 实验证据的裁决者）。
prompt 里写得很清楚："后续轮不要从零重做假设生成"。但节点每一轮照样把
generate → cluster → audit → evolve → score → freeze 全套重跑一遍。

不是模型不听话，是**门禁逼它重跑**。`validate_hypothesis_outputs` 的三条
blocking 判据（prereg_frozen_and_registered / structured_falsification_present /
not_conclusion_restatement）都从 `state.transcript_path` 取料，而 transcript 是
**per-run** 的（`State.transcript_path = root/transcript.jsonl`，root 是本次 run
目录），artifacts 才是跨 run 持久的（v2 下落在节点 Git 目录）。

于是第 2 轮开局：prereg 明明冻结在盘上，transcript 却是空的 → 三条判据全 fail
→ 节点 incomplete。要过门，只能重新 freeze 一份新 prereg；要 freeze 就得重新
生成、重新打分、重新审计。**prompt 说"别重做"，机制说"不重做就判死"** ——
同一件事上文案与 API 不一致时，赢的永远是 API。

## 修法

两件事分开：

1. **判据取料改成"看承诺，不看这一趟说过什么话"**（在 output_validator 里）。
   "协议冻没冻、假说登没登记"是项目的持久事实，不是本轮的对话记录。

2. **请求模式分流**（本模块）。第一次出计划和拿实验结果回来改计划，本来就
   不是同一件工作，不该有同一份交付契约：
     - `plan`  —— 还没有生效协议：走完整工作流，冻结 prereg，建 research_state。
     - `revise`—— 已有冻结协议 + research_state：读实验结果 → 裁决假说 →
       该改计划就出修订版（必要时新增假设 + 新冻结）→ 出新一版 research_state。

## 模式由**状态机械推导**，不靠调用方申报

literature 那套 mode 是调用方声明的，因为"我要查一个参数还是铺一个领域"只有
调用方知道。Analysis 不一样：这一趟是第一次出计划还是回来改计划，是**项目状态
的事实** —— 盘上有没有冻结的 prereg、有没有 research_state，一查便知。可机械
回答的问题不要交给调用方去猜（猜错的代价是隐性的：orchestrator 忘了传 mode，
节点就又跑一遍最重的流程，而且没人会发现）。

调用方仍可显式传 `mode=plan` 强制走重流程（pivot / 重做计划）；但**不能**把
`revise` 说进存在 —— 没有生效协议时申报 revise 一律降级回 plan 并说明原因。
否则 revise 就成了"跳过整套预注册"的绕行路径。
"""
from __future__ import annotations

import json
from typing import Any

from core.state import State

MODE_PLAN = "plan"
MODE_REVISE = "revise"
MODES = (MODE_PLAN, MODE_REVISE)

#: 本轮"新增了科学承诺"的信号 —— 出现任一条，假说类判据就对本轮的产出生效。
#: 扫声明而不是写名单：新工具只要落在这三类语义里就自动被覆盖。
_COMMITMENT_TOOLS = frozenset({
    "freeze_artifact",
})


def _meta_of(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    meta = record.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            return {}
    return meta if isinstance(meta, dict) else {}


# ── 持久事实 ──────────────────────────────────────────────────────────


def frozen_prereg_entries(state: State) -> list[dict[str, Any]]:
    """盘上已冻结的 pre_registration（跨 run 可见 —— 它们在节点 Git 目录里）。"""
    out: list[dict[str, Any]] = []
    for entry in state.list_artifacts("pre_registration"):
        record = state.read_artifact(str(entry["id"]))
        if _meta_of(record).get("frozen"):
            out.append({"id": str(entry["id"]), "record": record})
    return out


def has_frozen_prereg(state: State) -> bool:
    return bool(frozen_prereg_entries(state))


#: 承载实验**结果**的产物类型。中间件、脚本、图不进"必须交代"的账 ——
#: 逼 Analysis 逐一点名每个文件只会催生另一种表演（把 id 抄一遍就过门）。
RESULT_ARTIFACT_TYPES = frozenset({"experiment_log", "clean_results"})


def experiment_artifact_ids(state: State, *, results_only: bool = False) -> list[str]:
    """experiment 节点交付的产物 id。

    走 `list_artifacts` 的 owner_node 而不是自己拼 `<worktree>/experiment/
    artifacts/*.json`：产物目录归属由框架单点决定，自己拼路径就是第二个真相源。
    """
    return [
        str(entry["id"])
        for entry in state.list_artifacts()
        if entry.get("owner_node") == "experiment"
        and (not results_only or entry.get("type") in RESULT_ARTIFACT_TYPES)
    ]


def experiment_run_ids(state: State) -> set[str]:
    """experiment 产物里记录的 run id —— 裁决证据也可以填 run id。"""
    out: set[str] = set()
    for entry in state.list_artifacts():
        if entry.get("owner_node") != "experiment":
            continue
        meta = _meta_of(state.read_artifact(str(entry["id"])))
        for key in ("run_id", "origin_run_id", "created_by_run_id"):
            value = str(meta.get(key) or "").strip()
            if value:
                out.add(value)
    return out


# ── 本轮做了什么 ──────────────────────────────────────────────────────


def _tool_calls(state: State) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if not state.transcript_path.exists():
        return calls
    for line in state.transcript_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "tool_call":
            calls.append({"name": event.get("name"), "args": event.get("args") or {}})
    return calls


def run_authored_commitments(state: State) -> bool:
    """本轮是否**新增了科学承诺**（新假说 / 新冻结 / 新 prereg 草稿）。

    这是假说类判据的适用开关。判"本轮做了什么"用 transcript 是对的 ——
    错的是拿它去判"项目现在有什么"。
    """
    for call in _tool_calls(state):
        name = str(call.get("name") or "")
        args = call.get("args") or {}
        if name in _COMMITMENT_TOOLS:
            return True
        if name == "create_claim" and args.get("claim_type") == "hypothesis":
            return True
        if name in ("save_artifact", "stage_hypothesis_draft"):
            kind = str(
                args.get("artifact_type") or args.get("type") or args.get("name") or ""
            ).lower()
            if "pre_registration" in kind or "prereg" in kind:
                return True
    return False


# ── 模式解析 ──────────────────────────────────────────────────────────


def resolve_mode(state: State, node_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """本趟是 plan 还是 revise。返回 {mode, reason, declared, downgraded}。"""
    from .research_state import current_state

    declared = str((node_inputs or {}).get("mode") or "").strip().lower() or None
    has_prereg = has_frozen_prereg(state)
    has_state = current_state(state) is not None
    eligible = has_prereg and has_state

    if declared == MODE_PLAN:
        return {
            "mode": MODE_PLAN,
            "declared": declared,
            "downgraded": False,
            "reason": "调用方显式要求重出计划（pivot / 重做）",
        }
    if declared == MODE_REVISE and not eligible:
        missing = []
        if not has_prereg:
            missing.append("没有已冻结的 pre_registration")
        if not has_state:
            missing.append("没有 research_state")
        return {
            "mode": MODE_PLAN,
            "declared": declared,
            "downgraded": True,
            "reason": (
                "调用方申报 revise，但本项目" + "、".join(missing) + " —— "
                "revise 是「已有生效协议之后回来改」，不能用来跳过整套预注册。"
                "已降级为 plan。"
            ),
        }
    if eligible:
        return {
            "mode": MODE_REVISE,
            "declared": declared,
            "downgraded": False,
            "reason": "盘上已有冻结的 pre_registration + research_state —— 这是后续轮",
        }
    return {
        "mode": MODE_PLAN,
        "declared": declared,
        "downgraded": False,
        "reason": (
            "本项目还没有生效的研究协议"
            + ("（缺 research_state）" if has_prereg else "（缺冻结的 pre_registration）")
        ),
    }


def current_mode(state: State) -> str:
    """已解析并记在 state 上的模式；没解析过就现算（不写回）。"""
    mode = str(state.hook_state.get("_request_mode") or "").strip().lower()
    if mode in MODES:
        return mode
    return str(resolve_mode(state, state.hook_state.get("node_inputs"))["mode"])


# ── revise 的兑现判据 ─────────────────────────────────────────────────


def evidence_index(state: State) -> set[str]:
    """裁决证据可以指向的东西：任何 artifact id + experiment 的 run id。"""
    index = {str(entry["id"]) for entry in state.list_artifacts()}
    index |= experiment_run_ids(state)
    return index


def unresolvable_adjudication_evidence(state: State) -> list[dict[str, Any]]:
    """research_state 里判了 supported/refuted、但 evidence 指向不存在东西的行。

    `update_research_state` 已经拦了"裁决不给证据"；这里拦的是下一层 ——
    **给了证据但指不到任何真实产物**。两者是同一条红线的两半：没有证据的裁决
    不算裁决，指不到东西的证据同样不算证据。
    """
    from .research_state import RESOLVED_STATUSES, current_state

    meta = _meta_of(current_state(state))
    rows = meta.get("hypotheses")
    if not isinstance(rows, list):
        return []
    index = evidence_index(state)
    bad: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") not in RESOLVED_STATUSES:
            continue
        refs = row.get("evidence")
        refs = [str(x).strip() for x in refs] if isinstance(refs, list) else []
        refs = [x for x in refs if x]
        # 允许证据带路径/后缀写法（experiment/artifacts/foo.json → foo）
        missing = [
            ref for ref in refs
            if ref not in index
            and ref.rsplit("/", 1)[-1].removesuffix(".json") not in index
        ]
        if missing:
            bad.append({
                "id": str(row.get("id") or "?"),
                "status": str(row.get("status")),
                "unresolvable": missing,
            })
    return bad


def unaccounted_experiments(state: State) -> list[str]:
    """experiment 交了**结果**产物、research_state 却只字未提的那些。

    "不该分析的实验被强制送来"是另一个问题（issue #413）；这里管的是反面：
    **实验跑了、Analysis 收尾时对它只字不提**。裁决层的义务是给出说法
    （裁决 / 记为 inconclusive / 写进 gaps），不是当它没发生。

    说法可以写在四个地方之一 —— completed_experiments / 某条假说的 evidence /
    gaps / next_steps。不规定写在哪，只要求"这份结果在账上出现过"。

    **扫全部版本，不只扫最新那一版。** `update_research_state` 不会把父版本的
    completed_experiments 自动继承下来；只看最新版就等于要求每一轮把历史上所有
    实验重抄一遍 —— 那是又一种为过门而生的表演，而且轮数越多抄得越长。
    不变量应该是"这份结果**曾经**被交代过"，不是"这一版里也写着"。
    """
    from .research_state import load_versions

    produced = experiment_artifact_ids(state, results_only=True)
    if not produced:
        return []
    blob_parts: list[str] = []
    for _version, record in load_versions(state):
        meta = _meta_of(record)
        for row in meta.get("completed_experiments") or []:
            if isinstance(row, dict):
                blob_parts.extend(str(v) for v in row.values())
        for row in meta.get("hypotheses") or []:
            if isinstance(row, dict):
                refs = row.get("evidence")
                if isinstance(refs, list):
                    blob_parts.extend(str(x) for x in refs)
                blob_parts.append(str(row.get("note") or ""))
        blob_parts.extend(str(x) for x in (meta.get("gaps") or []))
        blob_parts.extend(str(x) for x in (meta.get("next_steps") or []))
    blob = "\n".join(blob_parts)
    return [aid for aid in produced if aid not in blob]
