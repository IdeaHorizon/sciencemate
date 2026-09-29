"""当前承诺了什么 —— 读预注册 head，不回放历史、不读 KB。

## 承诺的真相源只有一个：预注册 artifact

预注册就是「我承诺检验什么、什么结果算我错了」的权威冻结件：
  - 路径即身份，head 即当前（PR#500 三原语）
  - 冻结后修订走 `save_artifact(amendment_reason=...)` —— 信封里的
    `amendment` / `prev_content_hash` 就是修订链
  - 入库咽喉按哈希链账本拒绝未走修订的改动

因此「现在承诺了什么」= 读 head 一次。**没有去重、没有取代猜测** ——
那正是被本模块删掉的东西。

## 被删掉的两条错路（2026-08-19 实测代价）

1. 回放 transcript：四个审计各自扫历次 create_claim 调用，
   各自猜"哪次取代哪次"。修一次承诺内容变一次 → 被当成新承诺 → 旧版永远在。
   英国饮食 run 五次修不掉、blocked 收场。
2. 读 KB：provisional 断言进共享知识库，违反准入原则（被采纳才进 KB，
   docs/v21-collaboration-model.md §7），且 KB claim 无修订语义。

如果将来有人在这里加回 identity/去重函数，说明预注册的修订链坏了 ——
去修修订链，不要在消费端再猜一次。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State


def latest_prereg_content(state: State) -> str:
    """预注册 head 的正文。没有预注册时返回空串（早期阶段，审计各自判 applicable）。"""
    arts = state.list_artifacts("pre_registration")
    if not arts:
        return ""
    rec = state.read_artifact(arts[-1]["id"]) or {}
    return str(rec.get("content") or "")


def committed_falsifiers(state: State) -> list[dict[str, Any]]:
    """当前承诺的证伪判据 —— 从预注册 head 解析。"""
    content = latest_prereg_content(state)
    if not content:
        return []
    return _extract_prereg_falsifiers(content) or _extract_yaml_block_falsifiers(content)


def committed_claim_texts(state: State) -> list[str]:
    """当前承诺的命题正文（给按文本扫的审计当补充料；主料是 prereg 全文）。"""
    content = latest_prereg_content(state)
    if not content:
        return []
    texts: list[str] = []
    for m in re.finditer(
        r"(?:proposition|命题|hypothesis_text|claim_text)\s*[:：]\s*(.+)", content
    ):
        line = m.group(1).strip().strip('"').strip()
        if line and line not in texts:
            texts.append(line)
    return texts


_CRITERION_KEYS = ("threshold", "threshold_rationale", "comparison", "criterion",
                   "metric", "statement")


def _criteria_from_structured(fs: Any, label: str = "") -> list[dict[str, Any]]:
    """`falsification_criteria_structured` 的两种合法形状 → 判据列表。

    一个字段，两种形状（#409 / #453-1）：

      · 一条判据：`{"metric": …, "comparison": …, "threshold": …}`
      · label → 判据 的映射：`{"F1.1": {...}, "F1.2": {...}, "F1.3": {...}}`
        —— 人文社科课题一个假说拆成多条定性判据时模型只能这么写（契约里没有
        "一个假说多条判据"的位置）。

    两个都是 dict，`isinstance` 都放行；原实现把整个映射当成**一条**判据，取
    comparison 得 None，报告里就是那行 `✗ claim (comparison=None, threshold=None,
    source=None)`，hypothesis 从第 79 轮磕到第 83 轮出不来。判据是"值长什么样"：
    值全是 dict 就是映射，按 key 当 label 展开。
    """
    if isinstance(fs, list):
        return [x for x in fs if isinstance(x, dict)]
    if not isinstance(fs, dict) or not fs:
        return []
    values = list(fs.values())
    if all(isinstance(v, dict) for v in values) and not any(k in fs for k in _CRITERION_KEYS):
        out: list[dict[str, Any]] = []
        for key, val in fs.items():
            item = dict(val)
            item.setdefault("label", str(key))
            out.append(item)
        return out
    item = dict(fs)
    if label:
        item.setdefault("label", label)
    return [item]


def _extract_prereg_falsifiers(content: str) -> list[dict[str, Any]]:
    """Best-effort: JSON blocks or threshold_rationale sections in prereg markdown."""
    found: list[dict[str, Any]] = []
    if not content:
        return found
    for m in re.finditer(r"```json\s*(\{.*?\}|\[.*?\])\s*```", content, flags=re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            if data.get("falsification_criteria_structured"):
                found.extend(_criteria_from_structured(
                    data["falsification_criteria_structured"],
                    label=str(data.get("label") or "")))
            elif (
                "threshold" in data
                or "threshold_rationale" in data
                or "comparison" in data
                or "criterion" in data
            ):
                found.append(data)
            for key in ("hypotheses", "falsifiers", "assessments"):
                val = data.get(key)
                if isinstance(val, list):
                    for h in val:
                        if not isinstance(h, dict):
                            continue
                        fs = h.get("falsification_criteria_structured")
                        if fs is not None:
                            found.extend(_criteria_from_structured(
                                fs, label=str(h.get("label") or "")))
                        elif any(k in h for k in _CRITERION_KEYS[:4]):
                            item = dict(h)
                            item.setdefault("label", h.get("label") or "")
                            found.append(item)
        elif isinstance(data, list):
            for h in data:
                if isinstance(h, dict) and (
                    "threshold" in h
                    or "falsification_criteria_structured" in h
                    or "comparison" in h
                ):
                    fs = h.get("falsification_criteria_structured")
                    if fs is not None:
                        found.extend(_criteria_from_structured(fs, label=str(h.get("label") or "")))
                    else:
                        found.append(h)
    return found


def _extract_yaml_block_falsifiers(content: str) -> list[dict[str, Any]]:
    """协议正文里的**数值闭合条件** —— 走 core 的解析器，不自己认标题。

    v0.5：只取数值条。陈述条没有阈值可审 —— 它的诚实性判据是"兑现记录挂没挂
    证据"，那道门在 `core.prereg_commitments` 的关闭门禁里，不在这里。

    2026-08-16：本模块此前只解析 ```json``` 块。而 `freeze_artifact` 的
    prereg 门禁走 `core.prereg_commitments`，要求判据写成 yaml 三元组
    （`metric:` / `comparison:` / `threshold:`）—— 两边认的是不同的格式。
    在**冻结当轮**这层分歧被 transcript 掩盖了（create_claim 的参数里有结构化
    falsifier）；到了后续轮 transcript 是空的，回落到正文就一条也解析不出来，
    于是 `assess_threshold_grounding([])` 直接判 "未找到 structured falsifier"。
    协议已冻结、改不动，节点在这道门上没有出口（issue #409 的同一个病灶）。

    同一个问题不能有两份互不认识的解析器：这里直接复用冻结门禁那一份。
    """
    try:
        from core.prereg_commitments import NUMERIC, parse_questions
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for qid, question in parse_questions(content or "").items():
        for item in question.closure:
            if item.kind != NUMERIC:
                continue
            out.append({
                "label": qid,
                "hypothesis_id": qid,
                "metric": item.metric,
                "comparison": item.comparison,
                "threshold": item.threshold,
                # 走富接口而不是 `parse_commitments` 兼容视图，就是为了这个字段：
                # 兼容视图的形状要跟 v3.8 逐字一致，加不得键。
                "threshold_rationale": dict(item.rationale),
            })
    return out


