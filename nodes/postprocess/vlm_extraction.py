"""按合同向审图模型提**封闭问题**，而不是让它开放找茬。

## 病（2026-09-16 实证）

现行审图 prompt 的任务是 "report only visible defects"。开放式找茬有两个后果，
八版实测全中：

1. **模型只报最显眼的那一类。** qwen3.8-27b 八次共报出 17 条观察，几乎全是
   重叠 / 裁切 / 遮挡 / 一致性 —— 因为「有没有压住」比「拓扑对不对」好看得多。
   两颗一条线都没有的 CPU，八次无人提。
2. **它说得对也没用。** 它确实报了最关键那条（「上联从机箱边框而不是网卡
   引出，且每台只画了一条」），agent 在 README 里把它改写成「设计选择（有意
   为之）」，下一版就不报了，`observation_count` 归零，宣布「干净」。

更深的一层：**审图的人手里没有需求。** prompt 只喂一句 `intent` 加一串抽象
checklist（"label and component correspondence"）。它没法核一份它没见过的规格。

## 药

合同在场时，问题由合同**机械生成**，且全是可数的封闭问题：

    「图里有几个红色的方块？」
    「标着 PCIe Switch 0 的方块上连着几条线？」
    「有没有哪个方块一条线都不连？」

- 小模型答可数问题远比答「有没有缺陷」准；
- 答案能**机械对账**（合同说 8 个，它数出 7 个 → 一条可核的分歧），不需要
  模型下判断；
- 每一版问的是**同一组问题**，所以某个数字从对变错一眼可见 —— 这才是
  「越改越差」看得见的前提。

## 权限没变：它仍然只是证人

抽取结果与合同不一致 → **finding**（模型会数错，尤其是密集小图）。
真正能拒绝铸记录的只有机械核查（合同 vs 渲染），因为那一侧不会看错。
证据归框架，判决归义务 —— 这条没动。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .contracts import VisualContractError, VisualTruncationError
from .figure_contract import COMPILED_FAMILIES, role_colors

#: 一次抽取最多问多少个节点的度数。问全部节点会把 prompt 撑爆，也超出小模型
#: 一次能可靠数清的量；按度数从高到低取 —— 度数高的那几个正是结构的关节。
MAX_DEGREE_QUESTIONS = 6

#: 抽取 prompt 的版本。进记录，换了版本下游能看出读数口径变了。
EXTRACTION_PROMPT_VERSION = "figure-extract-v1"

#: 送审图片长边上限（px）。模型端本来就会缩，把原图整张塞过去只是白烧
#: token 和时间 —— 2026-09-16 实测：一张 13058×2451 的图让 render_figure
#: 连续两次 provider ReadTimeout（150s），而渲染本身早就成功了。
MAX_REVIEW_PIXELS = 1600

#: 抽取一共最多占用多少墙钟秒。**证人不能扣住账本**：审图是证据不是判决，
#: 它慢或不可达都不该让一条已经渲染成功的记录铸不出来。2026-09-16 实测：
#: 一次 render_figure 在 4 次重试 × 150s 超时里卡了十几分钟，产物早就在盘上，
#: agent 连续三轮在原地等 —— 缺席本该是一行可读事实，不是一次挂起。
EXTRACTION_DEADLINE_S = 100.0


def _bounded_image(path: Path) -> tuple[Path, dict[str, Any]]:
    """把送审图片缩到有界尺寸，并**如实记下审图人看的是什么**。

    缩放这件事必须写进记录：说「审图人看过」而它看的是另一张分辨率的图，
    下游就没法判断一条「看不出来」到底是模型的问题还是分辨率的问题。
    """

    try:
        from PIL import Image
    except Exception:
        return path, {"downscaled": False, "reason": "PIL unavailable"}
    try:
        with Image.open(path) as image:
            width, height = image.size
            if max(width, height) <= MAX_REVIEW_PIXELS:
                return path, {"downscaled": False, "reviewed_px": [width, height]}
            copy = image.copy()
            copy.thumbnail((MAX_REVIEW_PIXELS, MAX_REVIEW_PIXELS))
            target = path.with_name(path.stem + ".review.png")
            copy.save(target)
            return target, {
                "downscaled": True,
                "source_px": [width, height],
                "reviewed_px": list(copy.size),
            }
    except Exception as exc:
        return path, {"downscaled": False, "reason": str(exc)[:120]}


def _colour_name(hex_colour: str) -> str:
    """把调色板颜色说成人话 —— 模型看图时认得的是颜色，不是 #4C72B0。"""

    table = {
        "#4C72B0": "blue",
        "#55A868": "green",
        "#C44E52": "red",
        "#8172B2": "purple",
        "#CCB974": "khaki/olive",
        "#64B5CD": "light blue",
        "#E69F00": "orange",
        "#009E73": "teal",
    }
    return table.get(hex_colour.upper(), hex_colour)


def build_questions(contract: dict[str, Any]) -> dict[str, Any]:
    """合同 → 封闭问题集。纯函数，可测，也可原样进记录备查。"""

    if contract["family"] in COMPILED_FAMILIES:
        colours = role_colors(contract)
        role_counts = {role: 0 for role in colours}
        for node in contract["nodes"]:
            role_counts[node["role"]] += 1
        degree: dict[str, int] = {node["id"]: 0 for node in contract["nodes"]}
        for edge in contract["edges"]:
            degree[edge["from"]] += 1
            degree[edge["to"]] += 1
        label_of = {node["id"]: node["label"] for node in contract["nodes"]}
        busiest = sorted(degree, key=lambda nid: (-degree[nid], nid))[:MAX_DEGREE_QUESTIONS]
        return {
            "family": "schematic",
            "roles": [
                {"role": role, "colour": _colour_name(colours[role]), "expected": role_counts[role]}
                for role in sorted(colours)
            ],
            "degrees": [
                {"label": label_of[nid], "expected": degree[nid]}
                for nid in busiest
                if label_of[nid]
            ],
            "expect_no_unconnected": True,
        }
    panels = contract.get("panels") or []
    return {
        "family": "asserted",
        "expected_panel_count": len(panels) or None,
        "expected_series_count": len(contract.get("series") or []) or None,
    }


def extraction_prompt(questions: dict[str, Any]) -> str:
    """只让它**数数和指认**，不让它评判。

    刻意不告诉它期望值：说了就是在诱导它复读（"expected 8" → 它答 8）。
    对账在框架这边做。
    """

    if questions["family"] == "schematic":
        roles = ", ".join(
            f"{item['colour']} boxes (these are labelled like \"{item['role']}\" in the legend)"
            for item in questions["roles"]
        )
        degree_labels = json.dumps(
            [item["label"] for item in questions["degrees"]], ensure_ascii=False
        )
        return (
            "You are reading a technical diagram. This is a counting and identification "
            "task, not a review: do not judge quality, do not suggest improvements, do "
            "not report defects. Report only what you can literally see.\n\n"
            "Count carefully, one category at a time:\n"
            f"1. For each of these box categories, how many such boxes are in the image: {roles}.\n"
            f"2. For each of these labelled boxes, how many separate lines touch it: "
            f"{degree_labels}.\n"
            "3. List the text label of every box that has NO line touching it at all.\n\n"
            "Count a line once even if it bends. If a category is absent, answer 0. "
            "If you genuinely cannot tell, use -1 for that number rather than guessing.\n\n"
            'Return ONLY compact JSON: {"role_counts":{"<role name>":<int>},'
            '"node_degrees":{"<box label>":<int>},"unconnected_labels":["<label>"]}'
        )
    return (
        "You are reading a scientific chart. This is a counting task, not a review: do "
        "not judge quality and do not report defects. Report only what you can see.\n\n"
        "1. How many separate plot panels (axes with their own frame) are in the image?\n"
        "2. For each panel, left to right then top to bottom, how many distinct data "
        "series are plotted (lines, marker groups, or bar groups)?\n"
        "3. Does the vertical axis use a linear or logarithmic scale? Answer "
        '"linear", "log", or "unclear".\n\n'
        "If you genuinely cannot tell a number, use -1 rather than guessing.\n\n"
        'Return ONLY compact JSON: {"panel_count":<int>,"series_per_panel":[<int>],'
        '"axis_scale_y":"linear|log|unclear"}'
    )


def _coerce_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    # -1 是协议里的「我看不出来」。把它和真实计数分开 —— 「读不出」和「读成
    # 0」是两件事，混在一起会让缺席看起来像不符。
    return None if number < 0 else number


def compare_extraction(
    questions: dict[str, Any], extracted: dict[str, Any]
) -> list[dict[str, Any]]:
    """抽取读数 vs 合同。分歧进 findings（不是拒绝：模型会数错）。"""

    findings: list[dict[str, Any]] = []
    unreadable: list[str] = []

    if questions["family"] == "schematic":
        counts = extracted.get("role_counts") if isinstance(extracted.get("role_counts"), dict) else {}
        mismatches = []
        for item in questions["roles"]:
            seen = _coerce_int(counts.get(item["role"]))
            if seen is None:
                unreadable.append(f"count of {item['role']}")
                continue
            if seen != item["expected"]:
                mismatches.append(
                    {"role": item["role"], "contract": item["expected"], "reviewer_saw": seen}
                )
        if mismatches:
            findings.append(
                {
                    "collector": "VLM-EXTRACTION",
                    "message": (
                        "the visual reviewer counted a different number of components "
                        "than the contract declares"
                    ),
                    "mismatches": mismatches,
                }
            )

        degrees = (
            extracted.get("node_degrees") if isinstance(extracted.get("node_degrees"), dict) else {}
        )
        degree_mismatches = []
        for item in questions["degrees"]:
            seen = _coerce_int(degrees.get(item["label"]))
            if seen is None:
                unreadable.append(f"lines touching {item['label']}")
                continue
            if seen != item["expected"]:
                degree_mismatches.append(
                    {
                        "label": item["label"],
                        "contract": item["expected"],
                        "reviewer_saw": seen,
                    }
                )
        if degree_mismatches:
            findings.append(
                {
                    "collector": "VLM-EXTRACTION",
                    "message": (
                        "the visual reviewer counted a different number of lines touching "
                        "these components than the contract declares"
                    ),
                    "mismatches": degree_mismatches,
                }
            )

        orphans = [
            str(label)
            for label in extracted.get("unconnected_labels") or []
            if str(label).strip()
        ]
        if orphans:
            findings.append(
                {
                    "collector": "VLM-EXTRACTION",
                    "message": (
                        "the visual reviewer sees components with no line touching them: "
                        + ", ".join(orphans[:10])
                    ),
                    "unconnected_labels": orphans[:10],
                }
            )
    else:
        expected_panels = questions.get("expected_panel_count")
        seen_panels = _coerce_int(extracted.get("panel_count"))
        if seen_panels is None:
            unreadable.append("panel count")
        elif expected_panels and seen_panels != expected_panels:
            findings.append(
                {
                    "collector": "VLM-EXTRACTION",
                    "message": "the visual reviewer counted a different number of panels",
                    "contract": expected_panels,
                    "reviewer_saw": seen_panels,
                }
            )
        expected_series = questions.get("expected_series_count")
        per_panel = [
            _coerce_int(value) for value in extracted.get("series_per_panel") or []
        ]
        readable = [value for value in per_panel if value is not None]
        if expected_series and readable and sum(readable) != expected_series:
            findings.append(
                {
                    "collector": "VLM-EXTRACTION",
                    "message": "the visual reviewer counted a different number of data series",
                    "contract": expected_series,
                    "reviewer_saw": sum(readable),
                }
            )

    if unreadable:
        # 「读不出来」必须自己说出来。v1 里一次 completed=false 被折算成
        # observation_count=0，agent 读成「干净」—— 缺席绝不能长得像通过。
        findings.append(
            {
                "collector": "OB-EXTRACTION-UNREADABLE",
                "message": (
                    "the visual reviewer could not read these quantities off the "
                    "rendering: " + ", ".join(unreadable[:10])
                    + "; they were neither confirmed nor contradicted"
                ),
                "unreadable": unreadable[:10],
            }
        )
    return findings


async def extract_against_contract(
    *,
    image_path: Path,
    contract: dict[str, Any],
    config: Any,
) -> dict[str, Any]:
    """跑一次抽取。返回 {status, questions, extracted, findings, attempts}。

    复用 vlm_witness 的调用原语（同一个 role 绑定、同一套错误分类），不新建
    第二条模型调用路径。
    """

    from . import vlm_witness

    from core import model_roles

    binding = model_roles.resolve(config.role)
    if binding is None:
        raise VisualContractError(
            "model role 'visual_review' is not bound; the caller must resolve the role "
            "before calling extract_against_contract"
        )
    questions = build_questions(contract)
    prompt = extraction_prompt(questions)
    review_path, image_note = _bounded_image(image_path)
    attempts: list[dict[str, Any]] = []
    budget = config.max_tokens
    deadline = time.monotonic() + EXTRACTION_DEADLINE_S
    for attempt in range(config.max_retries + 1):
        if time.monotonic() >= deadline:
            attempts.append({"attempt": attempt + 1, "error": "extraction deadline reached"})
            break
        try:
            content, finish_reason, usage = await vlm_witness._call_once(
                config=config,
                binding=binding,
                api_key=binding.api_key,
                image_path=review_path,
                prompt=prompt,
                max_tokens=budget,
                timeout_s=max(10.0, deadline - time.monotonic()),
            )
            payload = vlm_witness._extract_json(content)
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "finish_reason": finish_reason,
                    "usage": usage,
                    "response_valid": True,
                }
            )
            return {
                "status": "success",
                "questions": questions,
                "extracted": payload,
                "findings": compare_extraction(questions, payload),
                "attempts": attempts,
                "prompt_version": EXTRACTION_PROMPT_VERSION,
                "reviewed_image": image_note,
            }
        except VisualTruncationError as exc:
            # 原样重试永远是同一个结果（预算是确定的）—— 必须加预算再试。
            attempts.append(
                {"attempt": attempt + 1, "error": str(exc), "finish_reason": exc.finish_reason}
            )
            budget = min(int(budget * 2), config.max_tokens_ceiling)
        except Exception as exc:
            # 传输层错误（ReadTimeout/连不上）与契约错误一样，只是「这次没读成」。
            # 一律归类记账、不外抛 —— 外抛会让整次铸记录失败，那就是让证人当了判官。
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )
    return {
        "status": "extraction_incomplete",
        "questions": questions,
        "extracted": {},
        # 没跑完就是没跑完：交回一条**可读的缺席**，绝不返回空 findings 让
        # 调用方误以为读过且干净。
        "findings": [
            {
                "collector": "OB-EXTRACTION-INCOMPLETE",
                "message": (
                    "the contract-driven visual extraction did not complete; no "
                    "quantity in the rendering was confirmed by a reviewer. The "
                    "mechanical contract check still stands on its own — re-rendering "
                    "will not change this, the reviewer was simply not reachable in time"
                ),
                "attempts": len(attempts),
            }
        ],
        "attempts": attempts,
        "prompt_version": EXTRACTION_PROMPT_VERSION,
        "reviewed_image": image_note,
    }
