"""Cross-node contract for consuming rendered figure records.

判决拆除 B 刀：八种 typed 产物收敛为一种 `figure` 记录（figure_package 已
删除）。消费端只有一种形态：**引用图记录（id+hash）+ 披露其 findings**。

这里只验**账本真实性**（身份/指纹/血缘，B 类）—— 伪造记录被机械识别：

1. 记录必须由可视化属主节点铸出（produced_by_node_type 由框架盖章，模型
   写不了别人的章）；
2. ``metadata.figure_hash`` 必须等于对出处绑定核心（源数据 hash ↔ 渲染代码
   hash ↔ 输出文件 hash ↔ caption/alt_text）逐字节重算的值 —— 事后改写任何
   一环都会让指纹对不上；
3. deliverable 的字节指纹由 staging 侧（writing）逐文件重验。

记录**形状**（源绑定数组、渲染代码锚、输出文件清单）是一份声明式 schema
（``_FIGURE_METADATA_SCHEMA``），一次校验。判决词表（status/quality_mode/verdict）
不许复活这件事钉在**铸造侧**（nodes/postprocess/tools/figure.py 的
reject_verdict_fields）与测试上，不在消费端运行时因一个键名拒掉整张图 ——
无机器消费者读那些键，放行账不假（判决拆除第三波：publication_figures:100 删）。

充分性事实（findings、审计结果、「视觉检查未运行」这类缺席事实）一律进
``_adequacy_findings`` 随 payload 返回，由消费端披露进稿件局限节、referee
终审 —— 证据可持久化，判决不可以。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from shared.lib.schema_check import schema_errors

_SHA256 = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}

#: 图记录 metadata 的形状契约（C 类）。缺它 133 行的指纹对空数组也能对上，故必要。
_FIGURE_METADATA_SCHEMA: dict = {
    "type": "object",
    "required": ["source_artifact_ids", "source_hashes", "render_code", "files"],
    "properties": {
        "source_artifact_ids": {"type": "array"},
        "source_hashes": {"type": "array", "items": _SHA256},
        "render_code": {
            "type": "object",
            "required": ["path", "content_hash"],
            "properties": {"path": {"type": "string", "minLength": 1},
                           "content_hash": _SHA256},
        },
        "files": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["path", "content_hash"],
                "properties": {"path": {"type": "string", "minLength": 1},
                               "content_hash": _SHA256},
            },
        },
    },
}


class PublicationFigureError(ValueError):
    """A figure record cannot authorize publication use."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _hash_json(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def figure_binding_hash(metadata: dict[str, Any]) -> str:
    """出处绑定核心的指纹 —— 与铸造侧（nodes/postprocess/tools/figure.py）
    同一套字段口径；两边分叉即伪造检测失效，由测试钉住。"""

    payload: dict[str, Any] = {
        "source_artifact_ids": list(metadata.get("source_artifact_ids") or []),
        "source_hashes": list(metadata.get("source_hashes") or []),
        "render_code": {
            "path": (metadata.get("render_code") or {}).get("path"),
            "content_hash": (metadata.get("render_code") or {}).get("content_hash"),
        },
        "files": [
            {"format": item.get("format"), "content_hash": item.get("content_hash")}
            for item in metadata.get("files") or []
        ],
        "caption": metadata.get("caption"),
        "alt_text": metadata.get("alt_text"),
    }
    # 图合同也是这张图声称的一部分（它声明了图里必须成立的结构事实），所以
    # 必须被绑进指纹，否则事后改断言不留痕。
    #
    # **只在有合同时加这个键**：无条件加会让所有存量记录的 payload 多一个
    # `None`，指纹整体变化 —— 线上已铸的记录会在消费端集体验不过。加了键的
    # 记录与没加的记录各自自洽，互不影响。
    contract_hash = metadata.get("contract_hash")
    if contract_hash:
        payload["contract_hash"] = str(contract_hash)
    return _hash_json(payload)



# ── 图上印的数，必须等于它自己声称的出处（#439）──────────────────────────────


class FigureContradictsItsSource(PublicationFigureError):
    """图上印的数值与它自己声称的出处对不上 —— 不是充分性问题，是自相矛盾。"""


def _resolve_field_path(blob: Any, path: str) -> Any:
    """按 ``a/b/0/c`` 取值；取不到返回 :data:`_MISSING`。"""
    node = blob
    for part in str(path).split("/"):
        if part == "":
            continue
        if isinstance(node, dict):
            if part not in node:
                return _MISSING
            node = node[part]
        elif isinstance(node, (list, tuple)):
            if not part.lstrip("-").isdigit():
                return _MISSING
            try:
                node = node[int(part)]
            except IndexError:
                return _MISSING
        else:
            return _MISSING
    return node


_MISSING = object()


def annotated_value_conflicts(
    metadata: dict[str, Any],
    sources: Mapping[str, Any] | None = None,
) -> list[str]:
    """图上印的每个数值 vs 它声称的出处。返回不一致清单（空 = 没有矛盾）。

    E2E v26 现场：论文中心结论和它自己嵌的图**互相矛盾，而且矛盾正好跨过预注册
    的 2σ 门** —— 正文 1.17σ 判 supported，图上印的 2.8σ 该判 refuted。
    同一个头条数字被 experiment 和 postprocess 各算一次（不加权 vs 加权拟合），
    两边都不报错。**四轮 reviewer 全部没抓到，是人工审出来的。**

    源哈希校验抓不到这一类：两边读的**确实是同一份源**，分叉在算法里。
    图上那些数字此前只以像素存在，下游没有任何一方能把它和正文的数放在一起比。

    这道闸要求图记录声明 ``annotated_values``：图上印了哪些数、每个数声称来自
    哪份产物的哪个字段路径、容差是多少。声明了就逐条比对；**没声明就跳过**
    —— 见 :func:`validate_figure_record`，缺席本身会作为一条充分性事实被披露，
    而不是悄悄放行。

    ``sources`` 是 ``{artifact_id: 已解析的产物内容}``。拿不到某份出处**不算
    通过**，记一条"够不到出处"；够不到和对得上必须分开
    （相关：观测不到不许当成对得上）。
    """
    declared = metadata.get("annotated_values")
    if not isinstance(declared, list) or not declared:
        return []
    available = dict(sources or {})
    conflicts: list[str] = []
    for index, item in enumerate(declared):
        if not isinstance(item, dict):
            conflicts.append(f"annotated_values[{index}] 不是对象，无法对账")
            continue
        label = str(item.get("label") or f"#{index}").strip()
        artifact_id = str(item.get("source_artifact_id") or "").strip()
        field_path = str(item.get("source_field") or "").strip()
        if not artifact_id or not field_path:
            conflicts.append(
                f"{label}：声明了印在图上，却没说来自哪份产物的哪个字段 —— "
                "无出处的数值无法对账，等于没声明")
            continue
        try:
            printed = float(item.get("value"))
        except (TypeError, ValueError):
            conflicts.append(f"{label}：图上的值不是数字（{item.get('value')!r}）")
            continue
        try:
            tolerance = abs(float(item.get("tolerance", 0.0)))
        except (TypeError, ValueError):
            tolerance = 0.0
        if artifact_id not in available:
            conflicts.append(
                f"{label}：够不到出处 {artifact_id} —— 没有对上账，"
                "不是对上了（观测不到不许当成一致）")
            continue
        found = _resolve_field_path(available[artifact_id], field_path)
        if found is _MISSING:
            conflicts.append(f"{label}：出处 {artifact_id} 里没有字段 {field_path}")
            continue
        try:
            actual = float(found)
        except (TypeError, ValueError):
            conflicts.append(
                f"{label}：出处 {artifact_id}/{field_path} 不是数字（{found!r}）")
            continue
        if abs(printed - actual) > tolerance:
            conflicts.append(
                f"{label}：图上印的是 {printed!r}，而它声称的出处 "
                f"{artifact_id}/{field_path} 是 {actual!r}"
                f"（容差 {tolerance!r}，实差 {abs(printed - actual)!r}）")
    return conflicts


def validate_figure_record(
    record: dict[str, Any],
    sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return hash-verified figure metadata (raising on forgery), findings disclosed.

    返回的 dict 是记录的 metadata 加 ``_adequacy_findings``（消费端披露用）。
    """

    if record.get("type") != "figure":
        raise PublicationFigureError("artifact is not a figure record")
    if record.get("produced_by_node_type") not in {"postprocess", "scientific_visualization"}:
        raise PublicationFigureError("figure record was not produced by a visualization owner")
    if "content_hash" in record:
        from core.ledger import sha256_text

        if record.get("content_hash") != sha256_text(str(record.get("content") or "")):
            raise PublicationFigureError("figure record artifact content_hash is stale")
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        raise PublicationFigureError("figure record has no metadata")
    if metadata.get("record") != "rendered_figure":
        raise PublicationFigureError(
            "figure record was not minted by render_figure (no rendered_figure marker); "
            "stage it as asset_role=figure instead — the delivery will disclose that no "
            "mechanical audit ran on it"
        )

    # ── 记录形状（C）：一份 schema、一次校验 ────────────────────────────────
    shape_errors = schema_errors(_FIGURE_METADATA_SCHEMA, metadata, "metadata")
    if len(metadata.get("source_artifact_ids") or []) != len(metadata.get("source_hashes") or []):
        shape_errors.append("metadata.source_artifact_ids 与 source_hashes 长度不等")
    if shape_errors:
        raise PublicationFigureError("figure record shape is invalid: " + "；".join(shape_errors))

    # ── 出处绑定完整性（B）────────────────────────────────────────────────
    if metadata.get("figure_hash") != figure_binding_hash(metadata):
        raise PublicationFigureError("figure record figure_hash is stale or forged")

    # ── 充分性事实：披露，不判决 ──────────────────────────────────────────
    adequacy: list[str] = []
    audit = metadata.get("audit")
    if not (isinstance(audit, dict) and audit.get("ran") is True):
        adequacy.append("图像级机械审计未运行（记录缺 audit 账）")
    for finding in metadata.get("findings") or []:
        if isinstance(finding, dict):
            message = str(finding.get("message") or finding.get("observation") or "").strip()
            collector = str(finding.get("collector") or "").strip()
            if message:
                adequacy.append(f"[{collector or 'finding'}] {message}")
        elif isinstance(finding, str) and finding.strip():
            adequacy.append(finding.strip())
    # ── 图文对账（#439）：声明了就比，没声明就让缺席可见 ────────────────────
    #
    # 不是充分性问题：图上印的数和它自己声称的出处对不上，是**自相矛盾**，
    # 所以这一条抛而不是披露。没有声明的按缺席披露 —— postprocess 还没开始
    # 填这个字段，闸不该因此拦住所有图；但它的缺席必须**看得见**，
    # 否则和"查过了、没问题"长得一模一样。
    conflicts = annotated_value_conflicts(metadata, sources)
    if conflicts:
        raise FigureContradictsItsSource(
            "图上印的数值与它声称的出处对不上：" + "；".join(conflicts))
    if not metadata.get("annotated_values"):
        adequacy.append(
            "图文数值对账未运行（记录没有声明 annotated_values —— "
            "图上印了哪些数只以像素存在，下游无法与正文比对）")

    vlm_review = metadata.get("vlm_review")
    if not isinstance(vlm_review, dict):
        adequacy.append("视觉检查未运行（平台未配置 visual_review 角色）")
    elif not vlm_review.get("completed"):
        adequacy.append("视觉检查未完成（记录里有如实分类的失败条目）")

    payload = dict(metadata)
    payload["_adequacy_findings"] = adequacy
    return payload
