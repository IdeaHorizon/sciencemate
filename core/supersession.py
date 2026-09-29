"""追加式否定：哪些产物已经被一条**合法的** supersession 记录排除掉了。

## 为什么这件事必须有一份公共实现

产物不可变，所以"这份是误建的"只能靠**追加**一条不可变的否定记录来表达，而不是删。
表达它的通道住在 experiment 节点里（``supersede_experiment_log``），但**读**它的人不止
一个：节点自己的 active view、scientific audit，以及 core 的 prereg 兑现账本。

2026-09-08 实测（#901）：一次真实 scientific run 里，早期 blocked 的 experiment_log
写了 8 条 ``not_run``；随后同一个生产 run 用合法、已冻结的 supersession 否定了那份未
冻结草稿，并在 canonical 冻结日志里写了 8 条 ``discharged``。节点自己的 active view
与 scientific audit **都读到了新日志**，而 ``core/prereg_commitments.py`` 仍返回 0/8
—— 它压根不消费这条否定。后果是冻结回执产生**假 closure debt**，decision card 错报
0/8，Writing 在 ``fulfilled == 0`` 时可能被错误硬拦。

一个问题两份实现，就会各自演化且分叉时两边都不报错
（[[feedback_one_truth_source_per_question]]）。所以判定放在这里，谁读都读同一份。

## 为什么不是"后写的覆盖先写的"

``_scan_result_metadata`` 用 ``setdefault``（最早的赢），配合 ``list_artifacts()`` 的
created_at 升序，于是最早那份 ``not_run`` 永久占位。看起来把它改成"后写覆盖"就完了
—— **不安全**：那等于允许任意一份较晚的、非 canonical 的产物改账。正解是把**被合法
否定的那份整个排除掉**，其余仍是最早的赢。

## 合法的判据（任何一条不满足就当否定不存在）

* 否定记录本身**已冻结** —— 没冻结的否定还能改，不能拿它藏证据；
* 正文 payload 与 metadata 里的 ``superseded_id`` 一致，且 ``reason`` 非空；
* ``payload.run_id == 否定记录.produced_by_run_id == 被否定产物.produced_by_run_id``
  —— 三者同一个生产 run。**不要求等于当前 run**：Writing 读 Experiment 的证据是跨
  消费者 run 的常态，要求等于当前 run 会把所有下游消费者挡在门外；
* 被否定的产物**未冻结** —— frozen 是已验证证据，一律不可否定（审计 fail-closed）。
"""

from __future__ import annotations

import json
from typing import Any

SUPERSESSION_TYPE = "experiment_log_supersession"
"""否定记录的产物类型。节点写它（``supersede_experiment_log``），这里读它。"""


def _payload(record: dict[str, Any]) -> dict[str, Any] | None:
    try:
        parsed = json.loads(str(record.get("content") or ""))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _frozen(state: Any, artifact_id: str) -> bool:
    """这个身份冻没冻 —— **问 State，不读 metadata**。

    2026-09-15 被判据抓到：这里原本读 ``metadata.frozen``。布局重构（记录是原生
    文件 + 一本账本）之后，冻结是**账本上的事实**，不再写在记录的 metadata 里。
    于是这个判定对每一条否定记录都答 False、整个函数静默返回空集 —— 而它
    "全程吞异常"，所以没有任何地方会报错，只是账本又开始产生假 closure debt。

    ``State.latest_frozen_artifact`` 是这件事在 main 上的唯一出处，调它。
    """
    try:
        return state.latest_frozen_artifact(artifact_id) is not None
    except Exception:
        return False


def superseded_artifact_ids(state: Any) -> frozenset[str]:
    """被合法否定、因此不该再计入任何账的产物 id。

    **全程吞异常**：这个判定被 commitment brief（全节点每轮都读）用到，绝不能让它崩。
    读不出来 = 当作没有否定 —— 那是保守方向：证据留在账上，不会凭空消失。
    """
    out: set[str] = set()
    try:
        items = state.list_artifacts(SUPERSESSION_TYPE) or []
    except Exception:
        return frozenset()
    for item in items:
        try:
            supersession_id = str(item.get("id") or "")
            if not supersession_id:
                continue
            record = state.read_artifact(supersession_id)
            if not isinstance(record, dict) or not _frozen(state, supersession_id):
                continue  # 未冻结的否定还能改 —— 不拿它藏证据
            payload = _payload(record)
            if not payload:
                continue
            target_id = str(payload.get("superseded_id") or "")
            declared = str((record.get("metadata") or {}).get("superseded_id") or "")
            if not target_id or declared != target_id:
                continue  # 正文与 metadata 分叉 —— 不作数
            if not str(payload.get("reason") or "").strip():
                continue  # 空口否定等于没有理由
            producer = str(record.get("produced_by_run_id") or "")
            if not producer or str(payload.get("run_id") or "") != producer:
                continue  # 否定记录声称的 run 与它自己的出身对不上
            target = state.read_artifact(target_id)
            if not isinstance(target, dict):
                continue
            if str(target.get("produced_by_run_id") or "") != producer:
                continue  # 只能否定**自己这一 run** 产出的东西，不能否定别人的
            if _frozen(state, target_id):
                continue  # frozen 是已验证证据，一律不可否定
            out.add(target_id)
        except Exception:
            continue
    return frozenset(out)


__all__ = ["SUPERSESSION_TYPE", "superseded_artifact_ids"]
