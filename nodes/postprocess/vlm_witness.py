"""Independent minimax-m3 visual reviewer gateway.

The VLM reports *where* and *what is visibly wrong*.  Deterministic code maps
those observations to rubric rules and required actions.  The model never
creates approval artifacts directly and never sees the producer transcript.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from .contracts import (
    VisualContractError,
    VisualTruncationError,
    hash_json,
    normalize_region,
    record_check_finding,
    region_iou,
)

_RUBRIC_DIR = Path(__file__).resolve().parent / "reviewer" / "rubrics"


@dataclass(frozen=True)
class ReviewerConfig:
    """审图**协议**的配置。后端身份不在这里。

    provider / base_url / model / api_key_env 四项已经移出去了：谁来当审图
    模型是**平台配置**（模型角色 `visual_review`），不是节点里的常量。此前
    它们写死在这里、key 只从一个写死的环境变量名读，后果是这条能力对用户
    完全不可见 —— 看不见是谁、换不了、也没法在缺失时提前知道。

    留在这里的都是 postprocess 自己的方法学：prompt/rubric 版本、复现协议、
    重试预算、标定 id。那些确实该由节点 owner 说了算。

    `calibrated_model` 是**标定针对的模型**。平台可以把这个角色指给别的
    模型，那时标定不再适用 —— 代码不替 owner 决定该拒绝还是该放行，但会
    如实记进 review 结果，不假装标定仍然成立。
    """

    role: str = "visual_review"
    calibrated_model: str = "minimax-m3"
    temperature: float = 0.0
    timeout_s: float = 150.0
    max_retries: int = 4
    retry_backoff_s: float = 0.75
    max_tokens: int = 4800
    #: 被 finish_reason='length' 截断时，输出预算翻倍重试的上限。
    #: minimax-m3 会把预算全花在推理上、content 一个字不吐 —— 那时原样重试
    #: 是忙等，加预算才是唯一有效的动作。
    max_tokens_ceiling: int = 19200
    confirm_empty_once: bool = True
    confirm_findings_once: bool = True
    supplemental_detail_view: bool = True
    prompt_version: str = "figure-review-v9"
    rubric_version: str = "scientific-visual-v4-domain-adapters"
    deployment_role: str = "supplementary_fail_closed"
    calibration_id: str = "minimax-m3-v9-full-v5-2026-08-05"


def _rubric_checks(rubric_id: str) -> list[str]:
    path = (_RUBRIC_DIR / f"{rubric_id}.yaml").resolve()
    if _RUBRIC_DIR.resolve() not in path.parents or not path.exists():
        raise VisualContractError(f"review rubric {rubric_id!r} is not installed")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VisualContractError(f"review rubric {rubric_id!r} is unreadable") from exc
    if not isinstance(value, dict) or value.get("rubric_id") != rubric_id:
        raise VisualContractError(f"review rubric {rubric_id!r} has an invalid identity")
    checks = value.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or not all(isinstance(item, str) for item in checks)
    ):
        raise VisualContractError(f"review rubric {rubric_id!r} has no valid checks")
    return checks


def review_checklist(
    visual_kind: str,
    *,
    composition: bool,
    publication: bool = False,
) -> list[str]:
    """Load the versioned rubric files used for the actual VLM call."""

    family = "composite" if composition else visual_kind
    if family not in {
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
    }:
        family = "base"
    checks = _rubric_checks("base")
    if family != "base":
        checks.extend(_rubric_checks(family))
    if publication:
        checks.extend(_rubric_checks("publication_design"))
    return list(dict.fromkeys(checks))


def _mime_type(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/png")


def _image_data_url(path: Path) -> str:
    return f"data:{_mime_type(path)};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _detail_montage_data_url(path: Path) -> str | None:
    """Return non-contrast-adjusted enlarged crops without altering the source artifact.

    The first API image always remains the authoritative full rendering.  This
    supplemental board only makes edge text, annotations, dense centers, and
    lower-right scale bars easier for a VLM to inspect after provider-side image
    resizing.  It is never persisted or reviewed as a separate artifact.
    """

    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps

        with Image.open(path) as opened:
            source = opened.convert("RGB")
        width, height = source.size
        if width < 8 or height < 8:
            return None
        boxes = [
            ("TOP", (0, 0, width, max(1, round(height * 0.32)))),
            ("BOTTOM", (0, round(height * 0.68), width, height)),
            ("LEFT", (0, 0, max(1, round(width * 0.32)), height)),
            ("RIGHT", (round(width * 0.68), 0, width, height)),
            (
                "CENTER",
                (
                    round(width * 0.22),
                    round(height * 0.18),
                    round(width * 0.78),
                    round(height * 0.82),
                ),
            ),
            (
                "LOWER RIGHT",
                (round(width * 0.48), round(height * 0.48), width, height),
            ),
        ]
        cell_width, cell_height, header_height = 480, 340, 24
        board = Image.new("RGB", (cell_width * 3, cell_height * 2), "white")
        draw = ImageDraw.Draw(board)
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 15)
        except OSError:
            font = ImageFont.load_default()
        for index, (label, box) in enumerate(boxes):
            column, row = index % 3, index // 3
            x0, y0 = column * cell_width, row * cell_height
            crop = source.crop(box)
            fitted = ImageOps.contain(
                crop,
                (cell_width - 12, cell_height - header_height - 12),
                method=Image.Resampling.LANCZOS,
            )
            paste_x = x0 + (cell_width - fitted.width) // 2
            paste_y = y0 + header_height + (cell_height - header_height - fitted.height) // 2
            board.paste(fitted, (paste_x, paste_y))
            draw.text((x0 + 6, y0 + 3), label, fill="#202124", font=font)
            draw.rectangle(
                (x0, y0, x0 + cell_width - 1, y0 + cell_height - 1),
                outline="#B0B0B0",
                width=1,
            )
        buffer = io.BytesIO()
        board.save(buffer, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except (OSError, ValueError):
        return None


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise VisualContractError("reviewer response contains no JSON object")
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise VisualContractError("reviewer response JSON is truncated or invalid") from exc
    if not isinstance(value, dict):
        raise VisualContractError("reviewer response must be a JSON object")
    return value


def _validate_observations(
    value: dict[str, Any],
    expected_panel: str,
    allowed_panel_ids: list[str] | None = None,
    *,
    sink: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    # 判决拆除批 2（verdicts_postprocess.md reviewer 节）：检查照跑，判决取消。
    # 越界内容剥离 + 记账，不再拒绝整份响应；findings 落进 `sink`
    # （调用方把它并进该次 review 的 attempt/结果账）。
    sink = sink if sink is not None else {}
    observations = value.get("observations")
    if not isinstance(observations, list):
        raise VisualContractError("reviewer response needs an observations array")
    if len(observations) > 8:
        dropped = len(observations) - 8
        record_check_finding(
            sink,
            collector="OB-CAPACITY",
            message=f"已截断 {dropped} 条：观察超出 8 条容量上限，保留前 8 条",
            truncated=dropped,
            kept=8,
        )
        observations = observations[:8]
    out = []
    for index, raw in enumerate(observations):
        if not isinstance(raw, dict):
            raise VisualContractError("each reviewer observation must be an object")
        forbidden = {
            "confidence",
            "severity",
            "verdict",
            "rule_ref",
            "required_fix",
            "required_action",
        } & set(raw)
        if forbidden:
            # 观察员越权判决字段：剥离（不进最终观察记录）+ 记账，不拒绝
            # （wangd 拍板，verdicts_postprocess.md reviewer:249）。
            record_check_finding(
                sink,
                collector="OB-REVIEWER-ROLE",
                message=(
                    "observation carried judgment fields outside the reviewer's "
                    "observation role; the fields were stripped and never entered "
                    "the recorded observation"
                ),
                stripped_fields=sorted(forbidden),
                observation_index=index,
            )
        panel_id = str(raw.get("panel_id") or expected_panel)
        allowed = set(allowed_panel_ids or [expected_panel])
        if panel_id not in allowed:
            # 观察指向不在本次审查范围内的 panel：剥离该条 + 记账，不作废整份
            # 响应（判决拆除三波，vlm_witness:280 降格，与 forbidden 字段同形）。
            record_check_finding(
                sink,
                collector="OB-REVIEWER-ROLE",
                message=(
                    f"observation addressed panel_id={panel_id!r} outside the reviewed "
                    f"set {sorted(allowed)!r}; the observation was stripped"
                ),
                observation_index=index,
                panel_id=panel_id,
            )
            continue
        observation = str(raw.get("observation") or "").strip()
        if not observation:
            # 空文本的观察什么也没说：跳过 + 记账（计入调用方的丢弃数），
            # 不作废整份响应（vlm_witness:285 降格）。
            record_check_finding(
                sink,
                collector="OB-MALFORMED-OBSERVATION",
                message="observation text is empty; the observation was skipped",
                observation_index=index,
            )
            continue
        absence_claim = re.search(
            r"\b(?:not|isn't|is not|no)\s+(?:visibly\s+|being\s+)?"
            r"(?:clipped|cropped|cut off|overlapping|occluded|illegible)\b|"
            r"\bfully rendered and legible\b|\brather than being (?:clipped|cropped)\b",
            observation,
            re.IGNORECASE,
        )
        if absence_claim:
            # "无缺陷"式观察走既有的静默丢弃路径（与下方 clean_claim 同一条），
            # 不拒绝：正则判词会误伤真实缺陷描述（reviewer:269 删）。
            # 丢弃量由调用方按响应长度差如实记进
            # discarded_nondefect_observation_count。
            continue
        tonal_preference = re.search(
            r"compressed (?:luminance|tonal|dynamic) range|"
            r"(?:luminance|tonal|dynamic) range (?:is|appears) compressed|"
            r"(?:no|without|lacks?) (?:true |deep )?(?:blacks?|whites?)|"
            r"no use of.*(?:black|white).*(?:tonal|dynamic|scale)|"
            r"(?:not|does not) (?:use|span).*(?:full|complete).*(?:tonal|dynamic|0.{0,3}255)",
            observation,
            re.IGNORECASE,
        )
        material_loss = re.search(
            r"lost|erased|obscur|indistinguish|invisible|unread|clipp|saturat|crush",
            observation,
            re.IGNORECASE,
        )
        if tonal_preference and not material_loss:
            # Scientific images are not required to occupy the full 0..255
            # range. Without visible loss of structure this is a display-style
            # preference, not a defect.
            continue
        clean_claim = re.search(
            r"\b(?:is|are)\s+(?:present(?:,|\s+and)?\s*)?"
            r"(?:clearly\s+)?(?:visible|legible|readable)\b|"
            r"\bgood (?:local )?contrast\b|\bas expected\b|"
            r"\bclearly rendered\b",
            observation,
            re.IGNORECASE,
        )
        defect_signal = re.search(
            r"clip|crop|cut off|overlap|occlu|illegib|unread|low contrast|"
            r"barely|indistinguish|saturat|washed|crushed|missing|misalign|"
            r"cross|intersect|crowd|small|domin|unbalanc|inconsisten|"
            r"截断|遮挡|不可读|低对比|过曝|缺失|错位|交叉|拥挤",
            observation,
            re.IGNORECASE,
        )
        if clean_claim and not defect_signal:
            continue
        if re.search(
            r"rather than (?:a )?(?:uniform|random).*(?:scatter|distribution)|"
            r"expected (?:scatter|distribution|data) (?:shape|pattern)",
            observation,
            re.IGNORECASE,
        ):
            # 评判科学数据分布越出观察员职权：剥离该观察 + 记账，不拒绝
            # （reviewer:318，同 249 的剥离形态）。
            record_check_finding(
                sink,
                collector="OB-REVIEWER-ROLE",
                message=(
                    "observation judges the scientific data distribution rather "
                    "than a rendering defect; the observation was stripped"
                ),
                observation_index=index,
            )
            continue
        region = normalize_region(raw.get("region"), sink=sink, observation_index=index)
        visible = raw.get("visible_elements") or []
        if not isinstance(visible, list):
            # 非数组置空 + 记账，不作废整份响应（vlm_witness:357 降格）。
            record_check_finding(
                sink,
                collector="OB-MALFORMED-OBSERVATION",
                message="visible_elements was not an array; recorded as empty",
                observation_index=index,
                supplied=str(visible)[:200],
            )
            visible = []
        out.append(
            {
                "panel_id": panel_id,
                "region": region,
                "observation": observation,
                "visible_elements": [str(item) for item in visible[:12]],
            }
        )
    return out


def _review_prompt(
    *,
    panel_id: str,
    intent: str,
    checklist: list[str],
    composition: bool,
    review_context: dict[str, Any] | None = None,
    allowed_panel_ids: list[str] | None = None,
) -> str:
    scope = "the entire multi-panel composition" if composition else f"panel {panel_id}"
    context = review_context or {}
    pass_instruction = str(context.get("instruction") or "").strip()
    serialized_context = {key: value for key, value in context.items() if key != "instruction"}
    instruction_text = (
        f"Pass-specific instruction: {pass_instruction} " if pass_instruction else ""
    )
    return (
        "You are an independent scientific-figure visual inspector. Inspect "
        f"{scope} at the supplied final rendering. The intended message is: {intent!r}. "
        "Report only visible defects; do not infer statistical correctness, source fidelity, "
        "or hidden causes. Describe the visible phenomenon rather than prescribing a fix. "
        "Do not report clusters, bands, gaps, outliers, trends, or other data-distribution "
        "patterns as defects; those may be genuine observations. "
        "Do not require a scientific image to use the full 0–255 tonal range or contain true "
        "black and white; report contrast only when visible scientific structure or an expected "
        "annotation is materially indistinguishable. "
        "This is direct visual extraction, not an open-ended reasoning task. Do not provide "
        "analysis or chain-of-thought. Return at most eight high-value observations and keep the "
        "JSON under 500 tokens whenever possible. "
        "Do not demand a title, legend, scale bar, colorbar, or annotation unless the supplied "
        "annotation contract marks it as expected. Do not treat intentional whitespace as a "
        "defect; "
        "report it only when the visible balance or reading order is materially impaired. "
        "If nothing is visibly wrong, return an empty observations array. "
        "Before returning an empty array, compare every declared expected annotation with the "
        "visible rendering and perform each relevant checklist item once; publication-design "
        "defects count as visible defects even when no glyph is clipped. "
        "The first supplied image is the authoritative full rendering. If a second image is "
        "present, it is a non-contrast-adjusted inspection board of enlarged TOP, BOTTOM, LEFT, "
        "RIGHT, CENTER, "
        "and LOWER RIGHT crops from the same rendering; use it only to inspect small details and "
        "report normalized coordinates relative to the first image. Do not report the inspection "
        "board labels or crop boundaries as defects. "
        f"{instruction_text}"
        f"Allowed panel_id values: {json.dumps(allowed_panel_ids or [panel_id])}. "
        f"Review context: {json.dumps(serialized_context, ensure_ascii=False)}. "
        f"Checklist: {json.dumps(checklist, ensure_ascii=False)}. "
        'Return ONLY compact JSON: {"observations":[{"panel_id":"'
        f'{panel_id}","region":{{"x":0.0,"y":0.0,"w":0.1,"h":0.1}},'
        '"observation":"visible phenomenon","visible_elements":["element"]}]} . '
        "Coordinates are normalized to the supplied image."
    )


def _empty_confirmation_instruction(
    checklist: list[str], review_context: dict[str, Any] | None
) -> str:
    context = review_context or {}
    kind = str(context.get("visual_kind") or "")
    shared = (
        "This is a mandatory falsification pass after a primary empty result. "
        "Report only a defect that is visibly present; never emit an observation to say an "
        "element is clean. "
    )
    if kind == "quantitative":
        return shared + (
            "Inspect the quantitative figure in this order: trace all four canvas edges for cut "
            "glyphs; read the title, both axis labels, every tick-label region, and legend at "
            "final "
            "size; check whether the legend or annotations cover marks, **and whether any "
            "annotation, legend, or text box overlaps another so that either becomes partly "
            "unreadable** — a verdict/statistic annotation hidden behind the legend is a defect "
            "even when no data mark is covered; compare every line, point, "
            "and uncertainty band against the background; look for severe overplotting; then "
            "ignore "
            "hue and verify that grouped identities also differ by marker, line style, or hatch. "
            "Use the annotation contract to decide which labels are required."
        )
    if kind == "scientific_image":
        return shared + (
            "Inspect the scientific image for visibly saturated white regions or crushed dark "
            "regions that erase internal structure; compare the scale bar and its text with the "
            "local background; check channel/LUT legends and annotations for low contrast or "
            "occlusion; and scan all canvas edges for cropping."
        )
    if kind == "composite":
        return shared + (
            "Compare panel top, bottom, and plot-area edges; verify expected panel labels in "
            "reading order; compare type size, typeface, and stroke weight across panels; inspect "
            "gutters for "
            "collision or excessive compression; then assess hierarchy and whitespace balance at "
            "the declared final size."
        )
    if kind == "schematic":
        return shared + (
            "Trace every connector from source to target. Report intersections between unrelated "
            "edges when there is no junction marker, labels covering connectors, ambiguous arrow "
            "directions, or node/label collisions. Distinguish these from intentional junctions."
        )
    return (
        shared
        + "Perform a fresh, item-by-item scan of this checklist: "
        + json.dumps(checklist, ensure_ascii=False)
    )


def _finding_confirmation_instruction(checklist: list[str]) -> str:
    return (
        "This is a mandatory independent replication pass. Inspect the image from scratch; "
        "you are not given the prior pass and must not assume it found anything. Report only "
        "defects directly visible in this image, using the same strict JSON contract. Re-scan "
        "the supplied checklist once and keep the result compact."
    )


def _replicated_observations(
    reference: list[dict[str, Any]], candidate: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return only reference findings independently reproduced by candidate."""

    if not reference or not candidate:
        return []
    replicated: list[dict[str, Any]] = []
    confirmed_findings = map_observations(candidate)
    for observation, finding in zip(reference, map_observations(reference)):
        matched = False
        primary_elements = set(observation.get("visible_elements") or [])
        for candidate_observation, candidate_finding in zip(candidate, confirmed_findings):
            if candidate_observation.get("panel_id") != observation.get("panel_id"):
                continue
            if candidate_finding.get("rule_ref") != finding.get("rule_ref"):
                continue
            candidate_elements = set(candidate_observation.get("visible_elements") or [])
            region_match = _regions_correspond(
                observation["region"], candidate_observation["region"]
            )
            # Every replicated finding must be localized consistently. Shared
            # element names alone are too weak (e.g. two different axis labels).
            elements_correspond = (
                bool(primary_elements & candidate_elements)
                if primary_elements and candidate_elements
                else region_iou(observation["region"], candidate_observation["region"])
                >= 0.10
            )
            if region_match and elements_correspond:
                matched = True
                break
        if matched:
            replicated.append(observation)
    return replicated


def _observations_replicate(
    primary: list[dict[str, Any]], confirmation: list[dict[str, Any]]
) -> bool:
    if not primary or not confirmation:
        return not primary and not confirmation
    return bool(_replicated_observations(primary, confirmation))


def _regions_correspond(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if region_iou(left, right) >= 0.02:
        return True

    def contains(region: dict[str, Any], point: tuple[float, float]) -> bool:
        return (
            float(region["x"]) <= point[0] <= float(region["x"]) + float(region["w"])
            and float(region["y"])
            <= point[1]
            <= float(region["y"]) + float(region["h"])
        )

    left_center = (
        float(left["x"]) + float(left["w"]) / 2,
        float(left["y"]) + float(left["h"]) / 2,
    )
    right_center = (
        float(right["x"]) + float(right["w"]) / 2,
        float(right["y"]) + float(right["h"]) / 2,
    )
    return contains(left, right_center) or contains(right, left_center)


def _merge_replicated_observations(
    primary: list[dict[str, Any]], secondary: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union adjudicated findings while collapsing the same localized defect."""

    merged: list[dict[str, Any]] = []
    merged_findings: list[dict[str, Any]] = []
    for observation, finding in zip(
        [*primary, *secondary], map_observations([*primary, *secondary])
    ):
        duplicate = any(
            existing_observation.get("panel_id") == observation.get("panel_id")
            and existing_finding.get("rule_ref") == finding.get("rule_ref")
            and _regions_correspond(existing_observation["region"], observation["region"])
            for existing_observation, existing_finding in zip(merged, merged_findings)
        )
        if not duplicate:
            merged.append(observation)
            merged_findings.append(finding)
    return merged


async def _call_once(
    *,
    config: ReviewerConfig,
    binding: object,
    api_key: str,
    image_path: Path,
    prompt: str,
    include_detail_view: bool = False,
    max_tokens: int | None = None,
    timeout_s: float | None = None,
) -> tuple[str, str | None, dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
    ]
    if include_detail_view and config.supplemental_detail_view:
        detail_view = _detail_montage_data_url(image_path)
        if detail_view:
            content.append({"type": "image_url", "image_url": {"url": detail_view}})
    payload = {
        "model": binding.model,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "temperature": config.temperature,
        "max_tokens": int(max_tokens or config.max_tokens),
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=timeout_s or config.timeout_s) as client:
        response = await client.post(
            binding.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    response.raise_for_status()
    body = response.json()
    choice = (body.get("choices") or [{}])[0]
    content = choice.get("message", {}).get("content")
    finish = choice.get("finish_reason")
    if not isinstance(content, str) or not content.strip():
        budget = int(max_tokens or config.max_tokens)
        if finish == "length":
            raise VisualTruncationError(
                f"reviewer spent all {budget} output tokens without emitting content",
                finish_reason=finish, max_tokens=budget,
            )
        raise VisualContractError(
            f"reviewer returned HTTP 200 with empty content (finish_reason={finish!r})"
        )
    return content, choice.get("finish_reason"), body.get("usage") or {}


async def review_image(
    *,
    image_path: Path,
    panel_id: str,
    intent: str,
    checklist: list[str],
    composition: bool = False,
    review_context: dict[str, Any] | None = None,
    allowed_panel_ids: list[str] | None = None,
    config: ReviewerConfig | None = None,
) -> dict[str, Any]:
    config = config or ReviewerConfig()
    from core import model_roles

    # 判决拆除 A 刀：角色在场由**调用方**保证（lifecycle 先 resolve 判断，
    # 缺席时根本不会走到这里）。model_roles.require 在审图路径不再被调用；
    # 这里 resolve 拿绑定，拿不到说明调用方违反了契约 —— 如实抛错。
    binding = model_roles.resolve(config.role)
    if binding is None:
        raise VisualContractError(
            "model role 'visual_review' is not bound; the caller must resolve the role "
            "before calling review_image"
        )
    api_key = binding.api_key
    # 标定是针对某个具体模型做的。换了模型不等于不能用，但"标定仍然成立"
    # 这句话就不再为真 —— 如实带出去，别让下游以为它成立。
    calibration_applies = binding.model == config.calibrated_model
    attempts = []
    confirmation_mode: str | None = None
    primary_observations: list[dict[str, Any]] | None = None
    secondary_observations: list[dict[str, Any]] | None = None
    prompt = _review_prompt(
        panel_id=panel_id,
        intent=intent,
        checklist=checklist,
        composition=composition,
        review_context=review_context,
        allowed_panel_ids=allowed_panel_ids,
    )
    budget = config.max_tokens
    truncated_at_ceiling = False
    for attempt in range(config.max_retries + 1):
        try:
            include_detail_view = confirmation_mode in {
                "empty_confirmation",
                "finding_confirmation",
            }
            content, finish_reason, usage = await _call_once(
                config=config,
                binding=binding,
                api_key=api_key,
                image_path=image_path,
                prompt=prompt,
                include_detail_view=include_detail_view,
                max_tokens=budget,
            )
            if finish_reason == "length":
                raise VisualTruncationError(
                    f"reviewer response was truncated at {budget} output tokens",
                    finish_reason=finish_reason, max_tokens=budget,
                )
            parsed = _extract_json(content)
            check_sink: dict[str, Any] = {}
            observations = _validate_observations(
                parsed, panel_id, allowed_panel_ids, sink=check_sink
            )
            discarded_nondefect_count = max(
                0, len(parsed.get("observations") or []) - len(observations)
            )
            attempt_record = {
                "attempt": attempt + 1,
                "finish_reason": finish_reason,
                "response_valid": True,
                "usage": usage,
                "inspection_pass": confirmation_mode or "primary",
                "inspection_input": (
                    "full_rendering_plus_detail_montage"
                    if include_detail_view and config.supplemental_detail_view
                    else "full_rendering"
                ),
                "response_hash": hash_json(parsed),
                "observations": observations,
            }
            if discarded_nondefect_count:
                attempt_record["discarded_nondefect_observation_count"] = (
                    discarded_nondefect_count
                )
            if check_sink.get("validation_findings"):
                attempt_record["validation_findings"] = check_sink["validation_findings"]
            needs_confirmation = (
                primary_observations is None
                and attempt < config.max_retries
                and (
                    (not observations and config.confirm_empty_once)
                    or (bool(observations) and config.confirm_findings_once)
                )
            )
            if needs_confirmation:
                confirmation_mode = (
                    "empty_confirmation" if not observations else "finding_confirmation"
                )
                attempt_record[f"{confirmation_mode}_triggered"] = True
                attempts.append(attempt_record)
                primary_observations = observations
                instruction = (
                    _empty_confirmation_instruction(checklist, review_context)
                    if confirmation_mode == "empty_confirmation"
                    else _finding_confirmation_instruction(checklist)
                )
                prompt = _review_prompt(
                    panel_id=panel_id,
                    intent=intent,
                    checklist=checklist,
                    composition=composition,
                    review_context={
                        **(review_context or {}),
                        "inspection_pass": f"independent_{confirmation_mode}",
                        "instruction": instruction,
                    },
                    allowed_panel_ids=allowed_panel_ids,
                )
                continue
            attempts.append(attempt_record)
            if primary_observations is not None:
                if confirmation_mode == "adjudication" and secondary_observations is not None:
                    primary_matches = _replicated_observations(
                        primary_observations, observations
                    )
                    secondary_matches = _replicated_observations(
                        secondary_observations, observations
                    )
                    primary_replicated = bool(primary_matches)
                    secondary_replicated = bool(secondary_matches)
                    consistent = bool(primary_replicated or secondary_replicated)
                    accepted_matches = _merge_replicated_observations(
                        primary_matches, secondary_matches
                    )
                    attempts[-1]["independent_replication_consistent"] = consistent
                    attempts[-1]["replicated_observation_count"] = len(accepted_matches)
                    attempts[-1]["adjudication_consensus"] = (
                        "positive_both"
                        if primary_replicated and secondary_replicated
                        else "positive_primary"
                        if primary_replicated
                        else ("positive_secondary" if secondary_replicated else "none")
                    )
                    if consistent:
                        return {
                            "status": "success",
                            "observations": accepted_matches,
                            "attempts": attempts,
                        }
                    clean_pass_count = sum(
                        not pass_observations
                        for pass_observations in (
                            primary_observations,
                            secondary_observations,
                            observations,
                        )
                    )
                    attempts[-1]["clean_pass_count"] = clean_pass_count
                    if clean_pass_count >= 2:
                        attempts[-1]["adjudication_consensus"] = "negative_two_pass"
                        attempts[-1]["discarded_unreplicated_primary_count"] = len(
                            primary_observations
                        )
                        attempts[-1]["discarded_unreplicated_secondary_count"] = len(
                            secondary_observations
                        )
                        attempts[-1]["discarded_unreplicated_adjudication_count"] = len(
                            observations
                        )
                        return {
                            "status": "success",
                            "observations": [],
                            "attempts": attempts,
                        }
                    return {
                        "status": "review_inconclusive",
                        "observations": [],
                        "attempts": attempts,
                        "error": (
                            "three independent visual inspection passes neither replicated a "
                            "reported defect nor established two independent clean passes"
                        ),
                    }
                replicated = _replicated_observations(primary_observations, observations)
                consistent = (
                    not primary_observations and not observations
                ) or bool(replicated)
                attempts[-1]["independent_replication_consistent"] = consistent
                attempts[-1]["replicated_observation_count"] = len(replicated)
                if primary_observations:
                    attempts[-1]["discarded_unreplicated_primary_count"] = (
                        len(primary_observations) - len(replicated)
                    )
                if not consistent:
                    if attempt < config.max_retries:
                        secondary_observations = observations
                        confirmation_mode = "adjudication"
                        attempts[-1]["adjudication_triggered"] = True
                        prompt = _review_prompt(
                            panel_id=panel_id,
                            intent=intent,
                            checklist=checklist,
                            composition=composition,
                            review_context={
                                **(review_context or {}),
                                "inspection_pass": "independent_adjudication",
                                "instruction": (
                                    "This is a third independent inspection because prior "
                                    "passes disagreed. Inspect the authoritative full rendering "
                                    "from scratch without assuming either result."
                                ),
                            },
                            allowed_panel_ids=allowed_panel_ids,
                        )
                        continue
                    return {
                        "status": "review_inconclusive",
                        "observations": [],
                        "attempts": attempts,
                        "error": (
                            "independent visual inspection passes did not replicate the same "
                            "visible result"
                        ),
                    }
                return {
                    "status": "success",
                    "observations": replicated,
                    "attempts": attempts,
                }
            return {
                "status": "success",
                "observations": observations,
                "attempts": attempts,
                # 这次到底是谁审的、标定还算不算数 —— 判决可以现算，
                # **证据必须落下来**。
                "reviewer_model": binding.model,
                "reviewer_provider": binding.provider,
                "calibration_applies": calibration_applies,
            }
        except VisualTruncationError as exc:
            # 预算不够是**机械可判定**的：原样重试必然是同一个结果。加预算是
            # 唯一有效的动作，框架自己就能做，不该退化成三次一样的忙等。
            raised = min(budget * 2, config.max_tokens_ceiling)
            escalated = raised > budget
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "response_valid": False,
                    "inspection_pass": confirmation_mode or "primary",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "max_tokens": budget,
                    "next_max_tokens": raised if escalated else None,
                }
            )
            if not escalated:
                truncated_at_ceiling = True
                break
            budget = raised
            if attempt < config.max_retries and config.retry_backoff_s > 0:
                await asyncio.sleep(config.retry_backoff_s * (attempt + 1))
        except Exception as exc:
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "response_valid": False,
                    "inspection_pass": confirmation_mode or "primary",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            )
            if attempt < config.max_retries and config.retry_backoff_s > 0:
                await asyncio.sleep(config.retry_backoff_s * (attempt + 1))
    # 失败原因必须**如实分类**。都叫 review_unavailable，调用方就会照着
    # "缺 API key 或上游故障" 去查凭据 —— 而这里 key 是好的、上游返回的是
    # HTTP 200，只是模型没在预算内说完话（今晚为此白查了一轮凭据）。
    if truncated_at_ceiling:
        return {
            "status": "review_truncated",
            "observations": [],
            "attempts": attempts,
            "reason": (
                f"审图模型连续把输出预算用光（已加到上限 "
                f"{config.max_tokens_ceiling} tokens 仍未吐出内容）。"
                "凭据和上游都是好的；请换审图模型或简化 checklist。"
            ),
        }
    return {
        "status": "review_unavailable",
        "observations": [],
        "attempts": attempts,
        "reviewer_model": binding.model,
        "reviewer_provider": binding.provider,
        "calibration_applies": calibration_applies,
    }


_RULES = [
    (
        re.compile(
            r"overplot|overcrowd|too many points|dense points|"
            r"dense (?:cloud|field|mass) of (?:points|marks)|"
            r"points? (?:merge|merging|form(?:s|ing)? (?:an? )?(?:solid )?blob)|"
            r"点.*(拥挤|重叠)",
            re.I,
        ),
        "base.overplotting",
        "major",
        "reduce display-level point occlusion without changing data meaning",
    ),
    (
        re.compile(
            r"saturat|washed[- ]out|blown[- ]out|whiteout|near[- ]white|"
            r"uniform(?:ly)? (?:bright|white)|crushed.*contrast|crushed highlight|"
            r"high[- ]end.*clipp|luminance clipp|clipped highlight|"
            r"lost (?:visible )?(?:detail|structure).*(?:bright|white)|过曝|高光.*丢失",
            re.I,
        ),
        "scientific_image.saturation",
        "major",
        "restore visible image structure using a declared display window",
    ),
    (
        re.compile(
            r"\boverlap\w*|\bcover(?:s|ed|ing)?\b|\bocclu\w*|遮挡|盖住|重叠",
            re.I,
        ),
        "base.visual_occlusion",
        "major",
        "restore visibility of the obscured scientific content",
    ),
    (
        re.compile(
            r"clip|cut off|cropp|truncat|cut (?:at|by).*(?:edge|bound)|截断|裁掉|切掉",
            re.I,
        ),
        "base.clipping",
        "major",
        "keep all labels and marks inside the final canvas",
    ),
    (
        re.compile(
            r"unreadable|illegible|too small|small font|difficult to read|hard to read|"
            r"reducing legibility|看不清|不可读|太小",
            re.I,
        ),
        "base.legibility",
        "major",
        "make the affected content legible at final size",
    ),
    (
        re.compile(
            r"contrast|indistinguishable|barely distinguishable|pale|nearly invisible|"
            r"color|only by hue|hue alone|颜色|仅.*色相|对比度|分不清",
            re.I,
        ),
        "base.accessibility",
        "major",
        "restore distinguishability without changing data meaning",
    ),
    (
        re.compile(
            r"misalign|alignment|not .*aligned|uneven|unequal panel (?:height|width)|错位|未对齐",
            re.I,
        ),
        "base.alignment",
        "minor",
        "align the affected visual elements",
    ),
    (
        re.compile(
            r"hierarchy|compete.*(attention|title)|dominates?.*(figure|plot)|视觉层级|喧宾夺主",
            re.I,
        ),
        "base.hierarchy",
        "minor",
        "restore a restrained hierarchy that prioritizes scientific content",
    ),
    (
        re.compile(
            r"heavy grid|dense grid|thick grid|gridlines?.*(?:dominant|heavy|thick|dense|"
            r"compete|distract)|(?:decorative|ornamental).*(?:frame|title|element)|"
            r"网格.*(?:过重|密集|抢眼)|装饰.*(?:过多|干扰)",
            re.I,
        ),
        "publication.excessive_ornament",
        "minor",
        "reduce non-data ornament and keep grid lines subordinate to scientific marks",
    ),
    (
        re.compile(
            r"white ?space|empty space|unbalance|breathing room|gutter|spacing|留白|间距",
            re.I,
        ),
        "base.spacing_balance",
        "minor",
        "rebalance spacing without compressing or decorating scientific content",
    ),
    (
        re.compile(
            r"missing.*(label|legend|title)|"
            r"(?:no|without|lacks?|absent|not visible).*(label|legend|title)|"
            r"(?:label|legend|title).*(?:missing|absent|not visible)|"
            r"缺少.*(标签|图例|标题)",
            re.I,
        ),
        "base.annotation_missing",
        "major",
        "add the missing explanatory annotation from the plan",
    ),
    (
        re.compile(
            r"inconsistent typography|typograph(?:y|ic).*(?:inconsisten|mismatch)|"
            r"different typeface|font mismatch|inconsistent naming|"
            r"naming convention.*inconsisten|字体.*(不一致|不同)",
            re.I,
        ),
        "base.typography_consistency",
        "minor",
        "use a consistent type system across the final composition",
    ),
    (
        re.compile(r"break|discontin|断裂|中断", re.I),
        "base.visible_discontinuity",
        "major",
        "inspect the affected region and restore continuous visibility",
    ),
    (
        re.compile(
            r"(?:edge|connector|arrow|line).*(?:cross|intersect)|"
            r"(?:cross|intersect).*(?:edge|connector|arrow|line)|边.*交叉|连线.*交叉",
            re.I,
        ),
        "schematic.edge_crossing",
        "major",
        "reroute unrelated connectors so their correspondence is unambiguous",
    ),
]


def map_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for observation in observations:
        text = observation["observation"]
        rule_ref, severity, action = (
            "base.visible_issue",
            "minor",
            "resolve the visible issue without changing scientific meaning",
        )
        for pattern, candidate_rule, candidate_severity, candidate_action in _RULES:
            if pattern.search(text):
                rule_ref, severity, action = candidate_rule, candidate_severity, candidate_action
                break
        finding_id = (
            "finding_"
            + hash_json(
                {
                    "panel_id": observation["panel_id"],
                    "region": observation["region"],
                    "rule_ref": rule_ref,
                }
            ).split(":", 1)[1][:10]
        )
        findings.append(
            {
                "finding_id": finding_id,
                "panel_id": observation["panel_id"],
                "region": observation["region"],
                "observation": text,
                "severity": severity,
                "rule_ref": rule_ref,
                "required_action": action,
                "mapping_source": "deterministic_rubric",
            }
        )
    return findings


# 判决拆除 A 刀：verdict_for（approve/minor_revision/major_revision 词表）已
# 删除 —— VLM 是证人不是盖章岗，观察进 findings，改不改归 agent。
