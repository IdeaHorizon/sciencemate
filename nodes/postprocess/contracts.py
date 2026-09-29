"""Figure-service contracts: hashing, request normalization, shared errors.

判决拆除 B 刀（docs/verdict_demolition/FIGURE_SUBSYSTEM_REBUILD.md）：八种
typed 产物收敛为一种 `figure` 记录之后，本模块只剩三类东西：

1. **hash 原语**（canonical_json / hash_json / hash_file / artifact_payload_hash）
   —— 出处绑定（不可压缩核 #2）的地基；
2. **调用方请求词表**（visual_request_schema / normalize_request /
   presentation_defaults）—— 调用方仍然用 visual_requests 提需求，词表和
   校验必须同源；
3. **共享错误类型与小工具**（VisualContractError / slug / region 几何）。

DSL 强制通道（chart registry / planner / renderer 词表）已整体删除；图型
知识活在 nodes/postprocess/skills/。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ASSET_KINDS = frozenset(
    {
        "auto",
        "quantitative",
        "scientific_image",
        "spatial_3d",
        "molecular_material",
        "particle_trajectory",
        "geospatial_field",
        "electronic_structure",
        "schematic",
        "generative_illustration",
        "composite",
    }
)
PURPOSES = frozenset(
    {"exploration", "diagnostic", "publication", "presentation", "interactive", "report"}
)

# 判决拆除 A 刀：quality_mode 三档身份已删除。调用方仍传时：忽略 + 记
# deprecation 见证（见 tools/figure.py）。
DEPRECATED_REQUEST_FIELDS = frozenset({"quality_mode"})

# 判决拆除 A/B 刀共同的防复发 B 墙：判决词表不许被铸进 figure 记录。
# 证据可持久化，判决不可以。
FORBIDDEN_VERDICT_FIELDS: tuple[str, ...] = ("status", "quality_mode", "verdict")


class VisualTruncationError(ValueError):
    """审图模型把输出预算用光了，一个字都没吐出来（finish_reason='length'）。

    单列一类是因为它的处理方式和别的失败**相反**：原样重试永远是同一个结果
    （预算是确定的），必须加预算再试。混在通用错误里就会退化成忙等 —— 三次
    一模一样的请求、三次一模一样的截断（E2E v20 实测）。
    """

    def __init__(self, message: str, *, finish_reason: str | None = None,
                 max_tokens: int | None = None) -> None:
        super().__init__(message)
        self.finish_reason = finish_reason
        self.max_tokens = max_tokens


class VisualContractError(ValueError):
    """Raised when a figure-service input or record violates the contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def artifact_payload_hash(record: dict[str, Any]) -> str:
    """Hash stable scientific content, excluding timestamps and receiver identity."""

    payload = {
        "type": record.get("type"),
        "name": record.get("name"),
        "content": record.get("content"),
        "metadata": record.get("metadata") or {},
        "provenance": record.get("provenance") or {},
    }
    return hash_json(payload)


def slug(value: str, *, fallback: str = "visual") -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-._")
    return (clean or fallback)[:80].lower()


def require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VisualContractError(f"{field} must be an object")
    return value


def record_check_finding(
    sink: dict[str, Any],
    *,
    collector: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    """检查照跑，判决取消：结果（含失败）如实写进产物自己的账。"""

    finding = {"collector": collector, "message": message, **details}
    findings = sink.setdefault("validation_findings", [])
    findings.append(finding)
    return finding


def normalize_region(
    value: Any, *, sink: dict[str, Any] | None = None, **details: Any
) -> dict[str, float]:
    """归一化坐标协议：x/y/w/h 须为 [0,1] 内的数（C，保留）。

    区域越出图像（x+w>1 或 y+h>1）不再让整份响应作废：clamp 到图像边界，
    并在 `sink` 里记 OB-REGION-CLAMPED（判决拆除三波，contracts:155 降格）。
    `details`（如 observation_index）原样并进 finding。
    """
    region = require_dict(value, "region")
    out: dict[str, float] = {}
    for key in ("x", "y", "w", "h"):
        try:
            number = float(region[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise VisualContractError(f"region.{key} must be a number") from exc
        if number < 0 or number > 1:
            raise VisualContractError(f"region.{key} must be between 0 and 1")
        out[key] = number
    if out["x"] + out["w"] > 1.001 or out["y"] + out["h"] > 1.001:
        original = dict(out)
        out["w"] = min(out["w"], 1.0 - out["x"])
        out["h"] = min(out["h"], 1.0 - out["y"])
        if sink is not None:
            record_check_finding(
                sink,
                collector="OB-REGION-CLAMPED",
                message="region extended outside the image; clamped to the image bounds",
                original_region=original,
                clamped_region=dict(out),
                **details,
            )
    return out


def region_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    a = normalize_region(left)
    b = normalize_region(right)
    x1 = max(a["x"], b["x"])
    y1 = max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a["w"] * a["h"] + b["w"] * b["h"] - intersection
    return intersection / union if union else 0.0


def visual_request_schema() -> dict[str, Any]:
    """`visual_request` 的 JSON Schema —— 直接由本模块的词表生成。

    词表和校验必须同源，否则迟早各说各话 —— 所以这里从常量生成，而不是再抄
    一遍（合法取值只在运行时报错 = 逼调用方猜，P5 实测 27 次同样失败）。
    """
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "这张图要说明什么"},
            "asset_kind": {"type": "string", "enum": sorted(ASSET_KINDS)},
            "purpose": {"type": "string", "enum": sorted(PURPOSES)},
            "source_artifact_ids": {"type": "array", "items": {"type": "string"}},
            # 调用方的**政策**（不是审美偏好）：这几项会绑定到 request_id 上，
            # 声明合同与铸记录时机械核 —— 模型改不掉。写作节点的 request_figures
            # 已经按这套键发（width / text_language / min_font_pt / forbid_text）；
            # 2026-09-18 iter11 实测它发了、这边一个字没读：七张示意图画成中文、
            # 4.17pt 的字、8 面板 —— 政策在场、判据不在场。
            "constraints": {
                "type": "object",
                "properties": {
                    "width": {
                        "type": "string",
                        "enum": sorted(CALLER_WIDTHS),
                        "description": "这张图印在哪个版心：single_column（84mm）/ double_column（170mm）/ slide / poster",
                    },
                    "text_language": {
                        "type": "string",
                        "description": "图内文字语言（ISO 639-1，如 en / zh）。en 时合同里出现中日韩文字即拒绝",
                    },
                    "min_font_pt": {
                        "type": "number",
                        "description": "印刷终尺寸下图内任何文字的字号下限（pt）；不写按版心默认（印刷 7pt）",
                    },
                    "forbid_text": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "图内文字不许命中的正则（用户的硬性要求，如 case[1-6]）",
                    },
                },
            },
            "style_intent": {"type": "string"},
            "spec": {"type": "object"},
            "generative_allowed": {"type": "boolean"},
        },
        "required": ["intent"],
    }


#: 调用方 `constraints.width` 的合法值 —— 与合同的 `medium` 词表**同一份**
#: （figure_contract.MEDIA）。两边各写一张表就会分叉；这里只是别名层。
CALLER_WIDTHS: frozenset[str] = frozenset(
    {"single_column", "double_column", "slide", "poster"}
)


def caller_policy(request: dict[str, Any] | None) -> dict[str, Any]:
    """把一条调用方请求里**有机械后果**的那几项抽成政策。

    返回的每一项要么是调用方明确给的，要么是 None（= 调用方没表态，由声明者
    自己定）。这里不推默认 —— 默认在用的那一端定（medium 按 purpose 推、字号
    按版心推），否则同一件事在两处各有一个默认值。
    """

    request = request if isinstance(request, dict) else {}
    constraints = request.get("constraints") if isinstance(request.get("constraints"), dict) else {}
    asset_kind = str(request.get("asset_kind") or "auto").strip().lower()
    width = str(constraints.get("width") or "").strip().lower()
    if width and width not in CALLER_WIDTHS:
        raise VisualContractError(
            f"constraints.width must be one of {sorted(CALLER_WIDTHS)}; got {width!r}"
        )
    language = str(constraints.get("text_language") or "").strip().lower() or None
    min_font = constraints.get("min_font_pt")
    try:
        min_font_pt = float(min_font) if min_font not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise VisualContractError("constraints.min_font_pt must be a number") from exc
    forbid = constraints.get("forbid_text") or []
    if isinstance(forbid, str):
        forbid = [forbid]
    if not isinstance(forbid, list) or not all(isinstance(item, str) for item in forbid):
        raise VisualContractError("constraints.forbid_text must be an array of regex strings")
    return {
        "asset_kind": None if asset_kind in ("", "auto") else asset_kind,
        "medium": width or None,
        "text_language": language,
        "min_font_pt": min_font_pt,
        "forbid_text": [item for item in forbid if item.strip()],
        "purpose": str(request.get("purpose") or "").strip().lower() or None,
    }


def validate_asset_kind(value: Any) -> str:
    kind = str(value or "auto").strip().lower()
    if kind not in ASSET_KINDS:
        raise VisualContractError(f"asset_kind must be one of {sorted(ASSET_KINDS)}")
    return kind


def infer_purpose(request: dict[str, Any]) -> str:
    """从意图/venue 推断用途 —— 只用于导出预设（尺寸/DPI/格式），不是身份。"""
    explicit = str(request.get("purpose") or "").strip().lower()
    if explicit:
        return explicit
    intent = str(request.get("intent") or "").lower()
    if request.get("venue") or any(
        token in intent for token in ("publication", "paper", "论文", "发表", "期刊", "主图")
    ):
        return "publication"
    if any(token in intent for token in ("报告", "汇报", "presentation", "正式")):
        return "report"
    return "exploration"


def normalize_request(request: dict[str, Any]) -> dict[str, Any]:
    """把一条 caller visual_request 归一成机械可读的事实集。

    B 刀后它不再铸 visual_brief —— 归一结果只用于开局 briefing、request_id
    对账与导出预设推断。图型/编码判断整体归 agent（读 skills 后自己写代码）。
    """
    if not isinstance(request, dict):
        raise VisualContractError("visual_request must be an object")
    intent = str(request.get("intent") or "").strip()
    source_ids = request.get("source_artifact_ids") or []
    if isinstance(source_ids, str):
        source_ids = [source_ids]
    if not isinstance(source_ids, list) or not all(isinstance(item, str) for item in source_ids):
        raise VisualContractError("source_artifact_ids must be an array of artifact IDs")
    spec = request.get("spec")
    if not intent and not source_ids and not spec:
        raise VisualContractError("request needs intent, source_artifact_ids, or a schematic spec")

    asset_kind = validate_asset_kind(request.get("asset_kind", "auto"))
    purpose = infer_purpose(request)
    constraints = request.get("constraints") if isinstance(request.get("constraints"), dict) else {}

    return {
        "request_id": str(request.get("request_id") or slug(intent or "visual-request")),
        "normalized_intent": intent or "Render the supplied scientific visual specification",
        "source_artifact_ids": list(dict.fromkeys(source_ids)),
        "purpose": purpose,
        "asset_kind": asset_kind,
        "claim_ref": request.get("claim_ref"),
        "preferred_form": request.get("preferred_form"),
        "venue": request.get("venue"),
        "constraints": constraints,
        "generative_allowed": bool(request.get("generative_allowed")),
        "spec": spec,
    }


def presentation_defaults(brief: dict[str, Any]) -> tuple[bool, bool]:
    """(publication_grade, exploratory) —— 导出预设的两个机械事实。

    判决拆除 A 刀落点：这不是身份、不是档位 —— 只决定默认尺寸/DPI/格式；
    没有任何检查、审查或铸记录读它当判决。
    """
    intent = str(brief.get("normalized_intent") or brief.get("intent") or "").lower()
    purpose = str(brief.get("purpose") or "").lower()
    publication_grade = bool(
        purpose == "publication"
        or brief.get("venue")
        or any(
            token in intent for token in ("publication", "paper", "论文", "发表", "期刊", "主图")
        )
    )
    formal = purpose in {"report", "presentation", "interactive"} or any(
        token in intent for token in ("报告", "汇报", "presentation", "正式")
    )
    exploratory = not publication_grade and not formal
    return publication_grade, exploratory
