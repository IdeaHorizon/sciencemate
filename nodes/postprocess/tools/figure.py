"""figure 服务的工具面（判决拆除 B 刀）：render_figure + inspect_figure。

渲染主路径 = agent 在沙箱里写 matplotlib（复用 execute_python 的强制沙箱，
不自建第二套执行器）。框架的职责收缩为**机械录入出处绑定**（不可压缩核 #2）：

    figure 记录 ↔ 源数据 artifact hash ↔ 渲染代码 hash ↔ 输出文件 hash

- **可复现性是像素诚信的锚点**：渲染代码逐字节冻结在 run 目录里，记录携带
  replay 命令；referee 可按记录重跑。
- **图像级机械审计恒跑**（figure_audit，findings 形态，无拒绝分支）。
- **VLM 证人**：平台配置了 visual_review 角色就跑一次，观察进 findings；
  角色缺席时 findings 里自然没有该类条目（记录里 vlm_review 说明是哪一种缺席 —— 缺席
  是可读事实，不是异常）。
- **判决不落盘**：status/quality_mode/verdict 试图进记录即被机械拒绝
  （A 刀防复发 B 墙）。
- **生成式像素不得充当证据图**：generative 声明必须 evidence_bearing=false，
  否则拒绝录入（保留的 B 检查）。

八种 typed 产物已收敛为这一种 `figure` 记录；工作笔记（构思、草稿、中间
数据）归 agent 工作区的自由文件，不 typed、不 hash 链、不 schema。
"""

from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path
from typing import Any

from core import paths
from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from ..contracts import (
    ASSET_KINDS,
    DEPRECATED_REQUEST_FIELDS,
    FORBIDDEN_VERDICT_FIELDS,
    PURPOSES,
    VisualContractError,
    artifact_payload_hash,
    caller_policy,
    hash_bytes,
    hash_file,
    normalize_request,
    presentation_defaults,
    slug,
)
from ..layout_quality import layout_digest
from ..figure_audit import (
    audit_figure_outputs,
    audit_preamble,
    observed_object_model,
    read_sidecar,
)
from ..figure_contract import (
    COMPILED_FAMILIES,
    FAMILY_CHECKLIST,
    check_result_diff,
    contract_diff,
    contract_hash,
    contract_schema,
    contract_summary,
    coverage_findings,
    evaluate_assertions,
    failed_assertions,
    family_for_asset_kind,
    normalize_contract,
    dropped_findings,
    redeclaration_losses,
    empty_row_findings,
    medium_of,
    unexpressed_findings,
    redrawn_component_findings,
    unroled_edge_findings,
    ASSERTED_FAMILIES,
    asserted_design_failures,
    contract_strings,
    font_floor_pt,
    render_guidance,
    style_preamble,
    text_policy_failures,
)

_ALLOWED_NODES = ["postprocess", "scientific_visualization"]

#: 声明过的合同活在 run 的 hook_state 里（agent 写不到），键是 contract_id。
#: 「先声明、后渲染」要成立，渲染读的那份就不能是渲染时再交一遍的那份。
_CONTRACT_STORE_KEY = "figure_contracts"

#: figure 记录的 schema 版本（B 刀起：单一 figure 记录承载全部科学必需）。
FIGURE_SCHEMA_VERSION = "3.0"

#: render_figure 铸的记录带这个标记；消费端据此走出处绑定校验
#: （shared/lib/publication_figures.py）。
RENDERED_FIGURE_RECORD = "rendered_figure"


def reject_verdict_fields(metadata: dict[str, Any]) -> None:
    """A 刀防复发 B 墙：判决词表不许被铸进 figure 记录。"""

    present = [field for field in FORBIDDEN_VERDICT_FIELDS if field in metadata]
    if present:
        raise VisualContractError(
            f"figure record may not carry verdict fields {present}: the status/"
            "quality_mode/verdict vocabulary was removed (verdict demolition); "
            "record evidence as findings instead"
        )


# 出处绑定核心的指纹：数据 hash ↔ 代码 hash ↔ 输出 hash ↔ 说明文字。
# **一个问题一个真相源**：铸造侧直接用消费端（shared/lib/publication_figures）
# 那一份实现 —— 两边各抄一份迟早分叉，而分叉时伪造检测静默失效。
from shared.lib.publication_figures import figure_binding_hash  # noqa: E402


def caller_visual_request(state: State, request_id: str) -> dict[str, Any]:
    """Return one immutable request from the trusted run_node input envelope."""

    node_inputs = state.hook_state.get("node_inputs")
    if not isinstance(node_inputs, dict):
        raise VisualContractError("request_id lookup requires run_node visual_requests input")
    raw = node_inputs.get("visual_requests")
    if raw is None:
        raw = node_inputs.get("figure_requests")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise VisualContractError("run_node input has no visual_requests")
    matches = [
        item
        for item in raw
        if isinstance(item, dict) and normalize_request(item)["request_id"] == request_id
    ]
    if len(matches) != 1:
        # 报错必须**列出合法值**：要求对方引用一个它无从枚举的标识符，本来
        # 就只能靠猜（P5 实测连猜 8 次）。
        available = [
            normalize_request(item)["request_id"]
            for item in raw
            if isinstance(item, dict)
        ]
        raise VisualContractError(
            f"request_id {request_id!r} must identify exactly one caller "
            f"visual_request. Available request_id values: {available!r}"
            + ("" if available else " —— 调用方一个 visual_request 都没传")
        )
    return copy.deepcopy(matches[0])


def _request_findings(state: State, request_id: str | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """request_id 对账：对上了带回 caller 请求；对不上如实入账（不拒绝）。"""

    if not request_id:
        return [], None
    try:
        request = caller_visual_request(state, request_id)
    except VisualContractError as exc:
        return [
            {
                "collector": "OB-DEVIATION",
                "field": "request_id",
                "delivered": request_id,
                "message": str(exc),
            }
        ], None
    findings = []
    for field in sorted(DEPRECATED_REQUEST_FIELDS & set(request)):
        findings.append(
            {
                "collector": "OB-DEPRECATION",
                "field": field,
                "supplied": request.get(field),
                "message": (
                    f"visual_request.{field} 已废除（判决拆除）：字段被忽略。"
                    "机械审计恒跑；视觉审查由平台是否配置 visual_review 角色机械决定。"
                ),
            }
        )
    return findings, request


def _resolve_output_declarations(
    state: State, workspace: Path, output_files: list[str]
) -> list[tuple[str, Path]]:
    # 「至少一个」由 parameters_schema 的 minItems=1 在派发口核；这里只剩类型前提。
    if not isinstance(output_files, list):
        raise VisualContractError("output_files must be an array of workspace-relative paths")
    resolved: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for raw in output_files:
        rel = Path(str(raw))
        if rel.is_absolute() or any(part == ".." for part in rel.parts):
            raise VisualContractError(
                f"output_files entries must be workspace-relative paths without '..': {raw!r}"
            )
        path = (workspace / rel).resolve()
        if path in seen:
            raise VisualContractError(f"duplicate output file: {raw!r}")
        seen.add(path)
        resolved.append((str(rel), path))
    return resolved


def _missing_from_page(expected: list[str], page_text: str) -> list[str]:
    """``expected`` 里哪几段在纸上找不到。**丢字是少了一个字符，不是换了一个码位。**

    PDF 的文字层是从字形反查回码位的（ToUnicode）：一款字体把两个码位画成同一个
    字形时，反查只能挑一个。2026-09-23 Windows + 随包 tectonic + 微软雅黑实测：框架
    自己拼在规格条目之间的「·」（U+00B7）画在了纸上，文字层读回来却是「∙」（U+2219）
    —— 按码位逐字比，每一个多条目的规格块都被报成「没上纸」，而 TeX 日志里一条
    Missing character 都没有。

    XeTeX 缺字形时是把字符**整个丢掉**，所以判据按「删了」算、不按「换了」算：标点
    与符号的位置允许读回另一个非文字字符，但那个位置上必须还有一个字符；字母、数字、
    汉字逐字比。两边先做 NFKC（µ/μ、Ω/Ω 这类兼容重复同理）。整段只有符号时按原样比
    —— 否则一个「→」丢了也会被任何标点冒认。
    """

    import re
    import unicodedata

    def squeeze(text: str) -> str:
        return "".join(unicodedata.normalize("NFKC", text).split())

    def pattern(text: str) -> str:
        if not re.search(r"\w", text):
            return re.escape(text)
        return "".join(
            rf"(?:{re.escape(ch)}|[^\w])" if unicodedata.category(ch)[0] in "PS" else re.escape(ch)
            for ch in text
        )

    haystack = squeeze(page_text)
    return [text for text in expected if not re.search(pattern(squeeze(text)), haystack)]


def _verify_text_reached_the_page(
    geometry: dict[str, Any] | None,
    outputs: list[Path],
    workspace: Path,
) -> list[dict[str, Any]]:
    """合同里写的每一段文字，在**渲染出来的 PDF** 里真的找得到吗。

    几何里有那个字符，不等于纸上有：字体缺字形时整个字符会被静默丢掉。实测两
    次（① 和 ≥）都是肉眼发现的 —— 几何判据在原理上就查不出来，因为它查的是
    几何，不是纸。

    缺席也是可读事实：没有 PDF 可读、或者文字被转成了轮廓（没有文字层），
    都如实记下来，不假装查过。
    """

    if not geometry:
        return []
    from ..diagram_compiler import _visible_strings

    expected = [text for text in _visible_strings(geometry) if text.strip()]
    if not expected:
        return []

    pdfs = [path for path in outputs if path.suffix.lower() == ".pdf"]
    pdfs += sorted(workspace.glob("_contract_tikz/*.pdf"))
    if not pdfs:
        return [
            {
                "collector": "OB-RENDER",
                "message": (
                    "no PDF among the outputs, so the text on the page could not be read "
                    "back; a character the font cannot draw would vanish unnoticed"
                ),
                "text_check": "no_pdf",
            }
        ]
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(str(pdfs[0]))
        page_text = "".join(
            document[index].get_textpage().get_text_range()
            for index in range(len(document))
        )
    except Exception as exc:  # noqa: BLE001
        return [
            {
                "collector": "OB-RENDER",
                "message": f"could not read the text layer back ({type(exc).__name__})",
                "text_check": "unreadable",
            }
        ]

    missing = _missing_from_page(expected, page_text)
    if not missing:
        return []
    # 文字被转成轮廓时**每一条**都找不到 —— 那不是「丢了 37 段字」，那是这道
    # 检查自己失效了。说清楚是哪一种，别把作者引去改文案。
    if len(missing) > len(expected) * 0.6:
        return [
            {
                "collector": "OB-RENDER",
                "message": (
                    f"the PDF has no usable text layer ({len(missing)}/{len(expected)} "
                    "strings not found), so this check cannot tell whether anything was "
                    "dropped — the text is probably drawn as outlines"
                ),
                "text_check": "no_text_layer",
            }
        ]
    return [
        {
            "collector": "OB-RENDER",
            "message": (
                f"{len(missing)} string(s) the contract declares never reached the page: "
                f"{missing[:5]}. The font almost certainly has no glyph for part of it, "
                "and the character is dropped silently — change the wording, or install a "
                "font that covers it."
            ),
            "text_check": "missing",
            "missing_strings": missing[:10],
        }
    ]


async def _witness_review(
    *,
    png_path: Path | None,
    caption: str,
    asset_kind: str,
    publication_grade: bool,
    contract: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """VLM 证人：角色在场跑一次，观察进 findings；缺席返回 ([], 一条说明缺席原因的记录)。

    缺席 = findings 里自然没有该类条目 —— 不是异常，零特例分支（A 刀落点）。
    但**缺席本身要记下来是哪一种缺席**（角色没配 / 没有 PNG 可审 / 跑了没跑完），
    否则只读记录的人重建不出当时的局面。瞬态失败如实分类记录。
    """

    from core import model_roles

    binding = model_roles.resolve("visual_review")
    if binding is None:
        # 缺席也要**说出是哪一种缺席**。回 None 时，只读记录的人分不出
        # 「平台没配 visual_review 角色」和「跑了但没跑完」—— 而紧挨着的兄弟
        # 分支（no_png_output）早就是 {"ran": False, "reason": ...} 这个形状。
        # 两个缺席两种记法，本身就是个分叉。
        #
        # 这不是「替代文案」（设计里禁止的是**假装审过**）：它陈述的是这道检查
        # 自己的出处，让人能从记录重建当时的局面。
        return [], {"ran": False, "reason": "role_not_configured", "role": "visual_review"}
    if png_path is None:
        return [], {
            "ran": False,
            "reason": "no_png_output",
            "provider": binding.provider,
            "model": binding.model,
        }

    from ..vlm_witness import (
        ReviewerConfig,
        map_observations,
        review_checklist,
        review_image,
    )

    config = ReviewerConfig()

    # ── 有合同 → 问封闭问题（抽取），没合同 → 老的开放找茬 ─────────────────
    #
    # 开放找茬实测只会报最显眼的一类（重叠/裁切），而两颗一条线都没有的 CPU
    # 八次无人提。合同在场时，问题由合同机械生成、全是可数的，小模型答得准，
    # 答案能对账，而且每一版问的是同一组问题 —— 回归才看得见。
    if contract:
        from ..vlm_extraction import extract_against_contract

        extraction = await extract_against_contract(
            image_path=png_path, contract=contract, config=config
        )
        completed = extraction.get("status") == "success"
        return list(extraction.get("findings") or []), {
            "ran": True,
            "completed": completed,
            "mode": "contract_extraction",
            "provider": binding.provider,
            "model": binding.model,
            "prompt_version": extraction.get("prompt_version"),
            "calibration_applies": False,
            "questions_asked": extraction.get("questions"),
            # 审图人实际看的是哪张（缩过没有、多大）—— 不写下来的话，一条
            # 「看不出来」没法区分是模型不行还是分辨率不够。
            "reviewed_image": extraction.get("reviewed_image"),
            "extracted": extraction.get("extracted"),
            # 没跑完时 findings 里带的是一条 OB-EXTRACTION-INCOMPLETE，不是空
            # 列表 —— 「没读成」绝不能长得像「读过且干净」。
            "finding_count": len(extraction.get("findings") or []),
        }

    kind = asset_kind if asset_kind not in {"auto", ""} else "quantitative"
    result = await review_image(
        image_path=png_path,
        panel_id="GLOBAL",
        intent=caption or "scientific figure",
        checklist=review_checklist(kind, composition=False, publication=publication_grade),
        composition=False,
        review_context={
            "image_content_hash": hash_file(png_path),
            "review_scope": "whole_figure",
        },
        allowed_panel_ids=["GLOBAL"],
        config=config,
    )
    completed = result.get("status") == "success"
    vlm_review = {
        "ran": True,
        "completed": completed,
        "provider": binding.provider,
        "model": binding.model,
        "prompt_version": config.prompt_version,
        "rubric_version": config.rubric_version,
        # 标定是针对某个具体模型做的：换了模型不等于不能用，但「标定仍然
        # 成立」这句话不再为真 —— 如实带出去。
        "calibration_applies": binding.model == config.calibrated_model,
        # 没跑完时不报 observation_count：0 会被读成「看过、干净」。v1 实测
        # 第 3 次审图 completed=false 记了 observation_count=0，agent 在交接
        # README 里写成「VLM 审图 observation_count=0（干净）」。
        "observation_count": len(result.get("observations") or []) if completed else None,
    }
    findings: list[dict[str, Any]] = []
    if completed:
        for finding in map_observations(list(result.get("observations") or [])):
            findings.append({"collector": "VLM-WITNESS", **finding})
    else:
        findings.append(
            {
                "collector": "OB-REVIEW-TRANSIENT",
                "reason": str(result.get("status")),
                "message": str(
                    result.get("error")
                    or result.get("reason")
                    or "visual review did not complete"
                ),
            }
        )
    return findings, vlm_review


def _contract_store(state: State) -> dict[str, Any]:
    store = state.hook_state.get(_CONTRACT_STORE_KEY)
    if not isinstance(store, dict):
        store = {}
        state.hook_state[_CONTRACT_STORE_KEY] = store
    return store


def _previous_declaration(state: State, request_id: str | None) -> dict[str, Any] | None:
    """同一个 request_id 的**上一次声明**（不是上一版 figure 记录）。

    存储是插入序的 dict，所以倒着找第一个同 request_id 的就是上一次。
    """

    if not request_id:
        return None
    for entry in reversed(list(_contract_store(state).values())):
        if entry.get("request_id") == request_id:
            contract = entry.get("contract")
            return contract if isinstance(contract, dict) else None
    return None


def _previous_figure_record(state: State, request_id: str | None) -> dict[str, Any] | None:
    """同一个 request_id 的上一版 figure 记录（跨版本回归的比较基准）。

    list_artifacts 的返回顺序是契约的一部分（created_at 升序，末位=最新），
    所以倒着找第一个同 request_id 的就是上一版。
    """

    if not request_id:
        return None
    for entry in reversed(state.list_artifacts(artifact_type="figure")):
        record = state.read_artifact(entry["id"])
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        if metadata.get("request_id") == request_id:
            return metadata
    return None


async def _declare_figure_contract(
    state: State,
    request_id: str,
    asset_kind: str,
    contract: dict[str, Any],
    **_: Any,
) -> dict[str, Any]:
    """声明这张图必须成立的结构事实 —— 在写任何渲染代码之前。

    时序是这个工具存在的全部理由：**架构决策前移到出任何像素之前**。v1 的
    八版里，「2×2 四台服务器各画一整套内部结构」这个架构在第 1 版就定了，
    后面七版全在跟它的后果搏斗（中心交换机与机箱抢位、8 条上联必须穿框）——
    而它从来没有被当作一个决策写下来、被看过一眼。

    2026-09-18 iter11 之后这里还绑三件调用方的事（模型改不掉）：家族
    （asset_kind）、版心（constraints.width）、语言与禁用词。以及印刷尺寸：
    schematic 在声明这一刻就算出印出来最小的字还剩几 pt，不够就拒绝并给出
    **算过的**改法 —— 不要渲染完再报（模型会对着事后报告重渲七次）。
    """

    try:
        family = family_for_asset_kind(asset_kind)
        if family is None:
            raise VisualContractError(
                f"asset_kind {asset_kind!r} has no figure contract family. "
                f"Contracts cover: schematic (compiled from the contract), "
                f"quantitative and composite (checked against the rendered object "
                f"model). Other asset kinds render without a contract."
            )
        # ── 调用方绑定：家族归请求，不归模型自填 ─────────────────────────
        # iter7/iter11 实测：schematic 请求被改标 composite 走手写 matplotlib，
        # 「schematic 不许传 code」的闸只看模型自填的 asset_kind → 改标即绕过。
        policy, caller_request = _bound_policy(state, request_id)
        if policy["asset_kind"] and policy["asset_kind"] != asset_kind:
            raise VisualContractError(
                f"request {request_id!r} is a {policy['asset_kind']} request by the "
                f"caller's visual_request; it cannot be declared as {asset_kind!r}. "
                "The family is the caller's decision. If the requested family cannot "
                "express this figure, say so to the caller instead of relabelling it."
            )
        normalized = normalize_contract(contract, family=family)
        # ── 版心：调用方说了算，作者其次，purpose 兜底 —— 写进合同、进 hash ──
        #
        # 为什么写进合同而不是布局里读 request：布局是纯函数，拿不到 request；
        # 而把它一路传下去就有了「合同说的」和「运行时传的」两个真相源。
        # `medium_source` 把三种来源分开记：将来查一张图按哪个版心判的，翻记录
        # 就知道；而 redeclaration_losses 只在作者**自己**写过时才追问它。
        if policy["medium"]:
            declared_medium = normalized.get("medium")
            if declared_medium and declared_medium != policy["medium"]:
                raise VisualContractError(
                    f"the caller pins this figure to medium={policy['medium']!r} "
                    f"(constraints.width) but the contract says {declared_medium!r}: "
                    "drop the contract's medium or match the caller's"
                )
            normalized["medium"] = policy["medium"]
            normalized["medium_source"] = "caller"
        elif normalized.get("medium"):
            normalized["medium_source"] = "declared"
        else:
            normalized["medium"] = medium_of(normalized, policy["purpose"])
            normalized["medium_source"] = "derived"
        if policy["text_language"]:
            normalized["text_language"] = policy["text_language"]
        floor_pt = font_floor_pt(normalized["medium"], policy["min_font_pt"])
        # 印刷下限只在**有人说了这张图印在哪儿**时才拒绝（调用方绑定或作者声明）。
        # 版心只是按 purpose 猜出来的时候，事实照进记录、不罚 —— 拿猜的版心去
        # 罚作者，报出来的是他交不出的东西（iter48 实测三版画布一模一样）。
        enforce_print = normalized["medium_source"] in ("caller", "declared")

        # ── 语言与禁用词：合同里的每一段人读文字，声明这一刻就核 ──────────
        text_failures = text_policy_failures(contract_strings(normalized), policy)
        if text_failures:
            raise VisualContractError("; ".join(text_failures))

        digest = contract_hash(normalized)

        # compiled 家族的断言就是对这张**声明的图**求值 —— 渲染由它编译而来，
        # 所以此刻就能知道成不成立。让 agent 在写第一行渲染代码之前就知道，
        # 而不是渲染完再说。
        results = evaluate_assertions(normalized) if family in COMPILED_FAMILIES else []
        failures = failed_assertions(results)
        if failures:
            raise VisualContractError(
                "the contract contradicts itself: "
                + "; ".join(
                    f"{item['id']} expects {item['comparator']} {item['expected']} "
                    f"but the declared diagram gives {item['actual']} "
                    f"(from the request: {item['derived_from']!r})"
                    # **把选中的是谁一起交回去。** 只说「期望 1 实得 8」，作者
                    # 无从判断是选择器写宽了还是图画错了 —— 2026-09-17 实测同一条
                    # 断言被拒两次、信息一字不差，两次都没改对（16 轮 / 698k）。
                    + (
                        " — selector matched: "
                        + ", ".join((item.get("detail") or {}).get("matched") or [])
                        if (item.get("detail") or {}).get("matched")
                        else ""
                    )
                    for item in failures
                )
                + ". Fix the nodes/edges so the declared structure satisfies the "
                "assertion, or narrow the selector if it caught more than you meant"
                # **没有证据就不许说「上面是匹配到的」。** 这句话过去无条件地
                # 印出来，而 matched 只有 node_count / edge_count 两支记过 ——
                # degree 和 neighbors 那两支上面什么都没有。2026-09-17 实测：
                # 同一条 order-calls 断言被拒四次（5→8→5→8），模型对着一句
                # 「the list above」和一片空白猜了四次。承诺的东西，得给得出。
                + (
                    " (the list above each one is what it actually matched)."
                    if any((item.get("detail") or {}).get("matched") for item in failures)
                    else "."
                )
            )

        request_text = " ".join(
            str(value)
            for key, value in (caller_request or {}).items()
            if key in {"intent", "style_intent", "notes"} and value
        )
        coverage = coverage_findings(normalized, request_text)
        # 词表接不住的键走**原始**合同（归一化正是丢键的那一步）。与
        # coverage 一样不按家族分岔：统计图一样会写出没有词的字段。
        coverage = coverage + unexpressed_findings(contract, family)
        coverage = coverage + dropped_findings(contract, normalized)
        coverage = coverage + empty_row_findings(normalized)
        coverage = coverage + unroled_edge_findings(normalized)
        coverage = coverage + redrawn_component_findings(normalized)
        # 同一个 request_id 的**上一次声明** —— 这一段过去从来没有任何东西比较过。
        # `contract_diff` 只在 render 时跑（拿的是上一版 figure 记录），而丢失
        # 发生在两次 declare 之间：iter50/51 都在第二次声明时丢掉 `medium`，
        # iter49 修断言时丢掉 bands。**重发整份合同时会掉东西，而可选字段
        # 掉了不报错、不拒绝、图照出。**
        _prior = _previous_declaration(state, request_id)
        coverage = coverage + redeclaration_losses(_prior, normalized)

        # 版面判据在**声明这一刻**就交出去。schematic 的合同就是渲染源 —— 这份
        # 合同画出来交叉多少、有没有线叠在一起，此刻已经完全确定，没有任何理由
        # 扣到 render 才说。
        #
        # 2026-09-17：22 节点 / 65 边那张图，agent 连发五次声明，每次都收到
        # "success"，一个字都没提这张图有 268 个交叉、读者根本追不动线；直到
        # 第一次 render 才看见，而那时它已经没有轮次了。**决定结构的那一刻，
        # 判断结构好坏的信息不在场**，五版几何于是一模一样。
        layout: list[dict[str, Any]] = []
        fit: dict[str, Any] | None = None
        guidance: dict[str, Any] | None = None
        if family in COMPILED_FAMILIES:
            layout, geometry = _layout_now(normalized)
            if geometry is not None:
                from ..diagram_compiler import print_fit, print_fit_remedies

                fit = print_fit(geometry, normalized, floor_pt=floor_pt)
                fit["enforced"] = enforce_print
                if enforce_print and not fit["fits"]:
                    # ── 印刷尺寸：不够就在这里拒，并给出算过的改法 ──────────
                    # 2026-09-18 iter11：模型对着事后的 layout.digest 重声明重渲染
                    # 七次；每次都是「7.8pt 但 p10 5.6pt」，它不知道该改哪一步。
                    remedies = print_fit_remedies(normalized, fit)
                    raise VisualContractError(_print_fit_rejection(fit, remedies))
        else:
            guidance = render_guidance(normalized, medium=normalized["medium"], floor_pt=floor_pt)

        contract_id = f"contract__{slug(request_id)}__{digest[7:15]}"
        _contract_store(state)[contract_id] = {
            "contract": normalized,
            "contract_hash": digest,
            "request_id": request_id,
            "asset_kind": asset_kind,
            "assertion_results": results,
            # 覆盖类 findings 过去只活在这个工具的**返回值**里 —— 模型看得见，
            # 而铸出来的记录一个字都没有。于是「记录如实披露图上缺什么」是一句
            # 没兑现的话：下一个读记录的人（referee / 交付）看到的是一张干净的
            # 图（2026-09-17 自查发现；「报告不是事实」同型第 N 次）。
            "coverage_findings": coverage,
            "layout_findings": layout,
            "policy": policy,
            "floor_pt": floor_pt,
            "enforce_print": enforce_print,
            "print_fit": fit,
        }
        used, left = _render_budget(state, request_id)
        return {
            "status": "success",
            "contract_id": contract_id,
            "contract_hash": digest,
            "family": family,
            "bound": {
                "asset_kind": policy["asset_kind"] or asset_kind,
                "medium": normalized["medium"],
                "medium_source": normalized["medium_source"],
                "text_language": policy["text_language"],
                "font_floor_pt": floor_pt,
            },
            "renders_used": used,
            "renders_left": left,
            "summary": contract_summary(normalized),
            "assertion_results": results,
            "coverage_findings": coverage,
            "layout_findings": layout,
            "print_fit": fit,
            "render_guidance": guidance,
            # 家族自查清单**推给**调用方，不指望它自己去 load_skill。v1 实测：
            # scientific-schematic 里恰好有能抓住那次缺陷的那一条，而 agent
            # 从没加载过它。
            "family_checklist": list(FAMILY_CHECKLIST.get(family, ())),
            "next_step": (
                f"render_figure(request_id={request_id!r}, contract_id={contract_id!r}, ...)"
                + (
                    " —— schematic 由框架按本合同编译渲染，不要再传 code/script_path。"
                    + (
                        f" 印刷：{fit['medium']} 下缩放 {fit['scale']}，最小的字"
                        f"（{fit['smallest_tier']}）{fit['final_smallest_pt']}pt"
                        + (
                            f" ≥ 下限 {fit['floor_pt']}pt。"
                            if fit["fits"]
                            else f" < 下限 {fit['floor_pt']}pt（版心是按 purpose 推的，"
                            "没有拒绝；调用方绑定了 width 时会拒）。"
                        )
                        if fit
                        else ""
                    )
                    if family in COMPILED_FAMILIES
                    else " —— 按 render_guidance 写你的绘图代码（figsize 上限 / 字号"
                    "下限 / 调色板 / 图例与轴标签义务）；渲染后框架读 matplotlib "
                    "对象模型逐条核对本合同的断言与这些设计义务。"
                )
                # 版面判据是**改合同**能解决的事，不是渲染完再修图。有缺陷时把
                # 这句摆在 next_step 里，因为 next_step 是模型一定会读的那一行。
                + (
                    f" 先看 layout_findings（{len(layout)} 条）—— 那是这份合同"
                    "画出来的样子，改结构后重新 declare 才会变；render 不会改变"
                    "其中任何一条。"
                    if layout
                    else ""
                )
                + f" 这条请求还剩 {left} 次渲染。"
            ),
        }
    except (VisualContractError, ValueError, TypeError) as exc:
        return {"status": "error", "error": str(exc), "error_code": "rejected"}


def _print_fit_rejection(fit: dict[str, Any], remedies: list[dict[str, Any]]) -> str:
    """印不下的拒绝信：数、原因、算过的改法。"""

    head = (
        f"the figure does not print legibly at {fit['medium']} "
        f"({fit['print_box_mm'][0]:.0f}×{fit['print_box_mm'][1]:.0f} mm): the natural "
        f"canvas is {fit['canvas_mm'][0]:.0f}×{fit['canvas_mm'][1]:.0f} mm, so it is "
        f"scaled to {fit['scale']:.2f} (bound by {fit['bound_by']}) and the smallest "
        f"text tier ({fit['smallest_tier']}) prints at {fit['final_smallest_pt']}pt, "
        f"below the {fit['floor_pt']:.0f}pt floor. Enlarging fonts does not help — "
        "the whole figure scales together; what helps is fewer things side by side."
    )
    if not remedies:
        return head + (
            " No mechanical re-arrangement of this contract reaches the floor: split it "
            "into several figures, collapse structurally identical groups, or tell the "
            "caller the request cannot be met at this width."
        )
    lines = []
    for item in remedies[:4]:
        verdict = "fits" if item.get("fits") else "still short"
        line = f"{item['change']} → {item['final_smallest_pt']}pt ({verdict})"
        if item.get("bands"):
            line += f"; bands={item['bands']}"
        lines.append(line)
    return head + " Measured alternatives, best first: " + " | ".join(lines) + (
        ". Re-declare with one of them (or split into several figures)."
    )


def _layout_now(normalized: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """现在就把这份合同摆一遍：版面判据与几何一起交回。

    摆不出来**不许长得像摆过了**：失败照样出一条 finding，说清楚这一版的版面
    判据缺席，而不是回一个干净的空列表；几何为 None 时印刷判据也不在场。
    """

    import copy

    from ..diagram_compiler import layout_for_backend

    try:
        geometry = layout_for_backend(copy.deepcopy(normalized), "tikz")
    except Exception as exc:  # noqa: BLE001 —— 版面算不出来不该让声明失败
        return [
            {
                "collector": "OB-LAYOUT",
                "message": (
                    "the layout could not be computed for this contract, so none of "
                    f"the layout criteria ran on it: {type(exc).__name__}: {exc}. "
                    "render_figure will surface the real failure."
                ),
            }
        ], None
    return list(geometry.get("layout_findings") or []), geometry


def _layout_findings_now(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    return _layout_now(normalized)[0]


#: 同一个 request_id 最多渲染几次。到上限后返回「这张图我画不出合同要求的样子」
#: 与每次的原因，让调用方决定 —— 而不是无限重渲（iter11 最后一个子 run 同一张
#: 总览渲了 7 次，61 次渲染出 5 张图）。
RENDER_ATTEMPTS_PER_REQUEST = 3
_ATTEMPTS_KEY = "figure_render_attempts"


class RenderBudgetExhausted(VisualContractError):
    error_code = "render_budget_exhausted"


def _attempts(state: State, request_id: str) -> list[dict[str, Any]]:
    store = state.hook_state.get(_ATTEMPTS_KEY)
    if not isinstance(store, dict):
        store = {}
        state.hook_state[_ATTEMPTS_KEY] = store
    return store.setdefault(request_id, [])


def _render_budget(state: State, request_id: str | None) -> tuple[int, int]:
    if not request_id:
        return 0, RENDER_ATTEMPTS_PER_REQUEST
    used = len(_attempts(state, request_id))
    return used, max(0, RENDER_ATTEMPTS_PER_REQUEST - used)


def _begin_attempt(state: State, request_id: str, contract_id: str | None) -> dict[str, Any]:
    """登记一次渲染；到上限就拒绝，并把每次的结果摆出来。"""

    history = _attempts(state, request_id)
    if len(history) >= RENDER_ATTEMPTS_PER_REQUEST:
        delivered = next(
            (item.get("figure_id") for item in reversed(history) if item.get("figure_id")),
            None,
        )
        lines = []
        for index, item in enumerate(history, start=1):
            if item.get("outcome") == "success":
                collectors = ", ".join(item.get("finding_collectors") or []) or "no findings"
                lines.append(f"#{index} {item.get('contract_id')} → record {item.get('figure_id')} ({collectors})")
            else:
                lines.append(f"#{index} {item.get('contract_id')} → {item.get('error') or 'failed'}")
        raise RenderBudgetExhausted(
            f"这张图我画不出合同要求的样子 — request {request_id!r} has used its "
            f"{RENDER_ATTEMPTS_PER_REQUEST} renders: " + " | ".join(lines) + ". "
            + (
                f"The last minted record {delivered} stands as the deliverable, with its "
                "findings disclosed as they are. "
                if delivered
                else "No record was minted for this request. "
            )
            + "Do not declare or render this request again. Move on to the other "
            "requests, and in your final answer tell the caller which requirement "
            "could not be met and why — the caller decides (split the figure, relax the "
            "constraint, or re-request)."
        )
    attempt: dict[str, Any] = {"contract_id": contract_id, "started_at": time.time(), "outcome": None}
    history.append(attempt)
    return attempt


def _has_caller_requests(state: State) -> bool:
    node_inputs = state.hook_state.get("node_inputs")
    if not isinstance(node_inputs, dict):
        return False
    raw = node_inputs.get("visual_requests")
    if raw is None:
        raw = node_inputs.get("figure_requests")
    if isinstance(raw, dict):
        raw = [raw]
    return isinstance(raw, list) and any(isinstance(item, dict) for item in raw)


def _bound_policy(state: State, request_id: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """调用方绑定在这条 request_id 上的政策。

    有请求却对不上 id → 拒绝（绑不上就没有家族、版心、语言可核，而合同的
    每一条判据都靠它们）；run 根本没有请求（ad-hoc 渲染）→ 空政策。
    """

    findings, request = _request_findings(state, request_id)
    if request is None:
        if request_id and _has_caller_requests(state):
            deviation = next((f for f in findings if f.get("field") == "request_id"), None)
            raise VisualContractError(
                (deviation or {}).get("message")
                or f"request_id {request_id!r} does not identify a caller visual_request"
            )
        return caller_policy(None), None
    return caller_policy(request), request

def _resolve_contract(
    state: State, contract_id: str | None, request_id: str | None, asset_kind: str
) -> dict[str, Any] | None:
    """取出已声明的合同；没声明而家族要求时，拒绝并说清怎么补。"""

    store = _contract_store(state)
    if contract_id:
        entry = store.get(contract_id)
        if entry is None:
            raise VisualContractError(
                f"contract_id {contract_id!r} was never declared in this run. "
                f"Declared contracts: {sorted(store) or '(none)'}. "
                "Call declare_figure_contract first — the contract must exist before "
                "the pixels do."
            )
        return entry
    family = family_for_asset_kind(asset_kind)
    if family in COMPILED_FAMILIES:
        # schematic 的全部内容就是结构。没有声明的结构，这张图里没有任何东西
        # 是可核的 —— 所以这是唯一一个「必须有合同」的家族。
        raise VisualContractError(
            "a schematic figure must be rendered from a declared contract: call "
            "declare_figure_contract(request_id=..., asset_kind='schematic', "
            "contract={nodes, groups, bands, edges, assertions}) first, then pass its "
            "contract_id here. The framework renders the diagram from that declaration, "
            "so a line you did not declare cannot be missing and a line you declared "
            "cannot be forgotten."
        )
    return None


def audit_sidecar_path(workspace: Path, request_id: str | None, code_hash: str) -> Path:
    """审计 sidecar 的文件名要**稳定**：同一份代码、同一个请求 → 同一个名字。

    它被前置进送去沙箱的代码文本。高危审批的通行证绑定「逐字相同的参数」，模型
    按合同用完全相同的参数重调 —— 可这里过去每次都掺一个随机 uuid，代码文本
    每次都变，通行证永远对不上。2026-09-18 实测：同一次渲染被拦、批准、重调、
    再被拦，七个来回。
    """

    stem = hash_bytes(f"{request_id or ''}:{code_hash}".encode("utf-8"))[:8]
    return workspace / f".figure_audit_{stem}.json"


async def _finish_tikz_render(state: State, workspace: Path, *, timeout: int) -> dict[str, Any]:
    """沙箱写完 figure.tex 之后，框架把它编成 PDF 并交付声明的输出文件。

    编译走 ``latex.run_tex`` —— 稿件、PDF 实测走的同一条：同一个编译器选择（latexmk，
    缺则随包 tectonic）、同一堵墙（可写的只有 ``_contract_tikz/``）、同一个家（tectonic
    住墙给的家）。这里曾经自己 ``which("xelatex")``、在墙外起它：只有随包 tectonic 的
    Windows 上默认的 tikz 后端一张图都出不来，而 PDF 实测说这台机器「能」。

    进墙不会把 2026-09-18 那道高危审批请回来：那道闸在模型代码的工具层
    （execute_python / run_command），墙里没有它；argv 仍全由框架拼，模型的字只在
    .tex 正文里（经 ``_tex_escape``）。
    """

    from shared.tools.library import latex

    from ..diagram_compiler import (
        TIKZ_WORK_DIR,
        finish_tikz_outputs,
        tikz_log_tail,
    )

    work = workspace / TIKZ_WORK_DIR
    if not (work / "figure.tex").is_file():
        return {"status": "error", "error": "the compiled render script did not write _contract_tikz/figure.tex"}
    run = await latex.run_tex(state, work, "figure.tex", "xelatex", timeout)
    compiled = {"compiler": run.compiler, "command": run.commands[-1] if run.commands else []}
    if run.status == "done" and run.returncode == 0 and (work / "figure.pdf").is_file():
        written, png_reason = finish_tikz_outputs(work, workspace)
        return {"status": "success", "written": written, "png_reason": png_reason, **compiled}
    if run.status == "spawn_failed" and latex.no_tex_engine():
        return {
            "status": "error",
            "error_code": "toolchain_missing",
            "error": (
                "no TeX engine on this machine ("
                + " / ".join(latex.absent_compilers())
                + " not found), so the tikz backend cannot compile; the contract is fine — "
                "render it with backend='matplotlib'"
            ),
        }
    stderr = (run.stderr or b"").decode("utf-8", errors="replace")
    stdout = (run.stdout or b"").decode("utf-8", errors="replace")
    log = tikz_log_tail(work)
    said = latex.tex_errors(log) or [
        line.strip() for line in stderr.splitlines() if line.strip()][-2:]
    return {
        "status": "error",
        "error": (
            f"{run.compiler} {run.status} (rc={run.returncode}) compiling the figure: "
            + ("; ".join(said)[:400] or "no reason in the log")
            + ". The contract was accepted; if this repeats, render it with backend='matplotlib'"
        ),
        "stdout_tail": stdout[-1500:],
        "stderr_tail": stderr[-1500:],
        "log_tail": log,
        **compiled,
    }


async def _render_figure(
    state: State,
    output_name: str,
    caption: str,
    alt_text: str,
    output_files: list[str],
    source_artifact_ids: list[str],
    code: str | None = None,
    script_path: str | None = None,
    request_id: str | None = None,
    purpose: str | None = None,
    asset_kind: str = "auto",
    generative: dict[str, Any] | None = None,
    requirements: list[str] | None = None,
    timeout: int = 300,
    contract_id: str | None = None,
    backend: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    attempt: dict[str, Any] | None = None
    try:
        # ── 调用方绑定：家族归请求；合同归它声明时的那条请求 ─────────────
        policy, _bound_request = _bound_policy(state, request_id)
        if policy["asset_kind"] and asset_kind != policy["asset_kind"]:
            raise VisualContractError(
                f"request {request_id!r} is a {policy['asset_kind']} request by the "
                f"caller's visual_request; render_figure got asset_kind={asset_kind!r}. "
                "The family is the caller's decision — a schematic request is rendered "
                "from its declared contract, never from hand-written plotting code."
            )
        # ── 图合同：先声明、后渲染 ───────────────────────────────────────
        contract_entry = _resolve_contract(state, contract_id, request_id, asset_kind)
        if contract_entry and request_id and contract_entry.get("request_id") != request_id:
            raise VisualContractError(
                f"contract {contract_id!r} was declared for request "
                f"{contract_entry.get('request_id')!r}, not {request_id!r}: one contract "
                "belongs to one caller request"
            )
        contract = (contract_entry or {}).get("contract")
        compiled = bool(contract) and contract["family"] in COMPILED_FAMILIES
        asserted = bool(contract) and contract["family"] in ASSERTED_FAMILIES

        # ── 入参与出处的机械校验 ─────────────────────────────────────────
        if compiled:
            # 合同即渲染源：再收一份手写坐标就等于同时有两个真相源，而它们
            # 迟早分叉（分叉的那一刻正是 v1 的病）。
            if code or script_path:
                raise VisualContractError(
                    "a schematic is rendered from its contract, not from hand-written "
                    "plotting code: drop code/script_path. Change the diagram by "
                    "declaring a new contract."
                )
        elif bool(code) == bool(script_path):
            raise VisualContractError(
                "provide exactly one of code (plotting code string) or script_path "
                "(an existing script in this run)"
            )
        # caption / alt_text 空值照录（判决拆除三波，figure:272 D 降格）：探索路径
        # 对着异常数据画一张看一眼不该先写 alt_text；figure_hash 对空串照样可算，
        # 账不假。缺项记 OB-CAPTION finding，发表路径由消费端义务应答。
        # purpose / asset_kind 的 enum 由 parameters_schema 声明，派发口核取值。
        caption = str(caption or "").strip()
        alt_text = str(alt_text or "").strip()
        empty_text_fields = [
            name for name, value in (("caption", caption), ("alt_text", alt_text)) if not value
        ]

        # 生成式像素不得充当证据图（保留的 B 检查，录入面唯一的拒绝分支之二）。
        generative_content: dict[str, Any] | None = None
        if generative is not None or asset_kind == "generative_illustration":
            if not isinstance(generative, dict) or generative.get("evidence_bearing") is not False:
                raise VisualContractError(
                    "generative pixels may not serve as evidence figures: declare "
                    "generative={'role': ..., 'evidence_bearing': false} explicitly, "
                    "or render the figure deterministically from data"
                )
            generative_content = {
                "role": str(generative.get("role") or "illustration"),
                "evidence_bearing": False,
            }

        # source_artifact_ids 形状（字符串数组、每项非空）由 parameters_schema
        # 声明（items.minLength=1 派发口核）；单个字符串按一项收。
        if isinstance(source_artifact_ids, str):
            source_artifact_ids = [source_artifact_ids]
        source_ids = list(dict.fromkeys(str(item) for item in source_artifact_ids or []))
        source_records = []
        for artifact_id in source_ids:
            record = state.read_artifact(artifact_id)
            if record is None:
                raise VisualContractError(f"source artifact {artifact_id!r} does not exist")
            source_records.append(record)
        source_hashes = [artifact_payload_hash(record) for record in source_records]

        from core.project_workspace import validate_tool_cwd

        workspace = validate_tool_cwd(state, None)
        workspace.mkdir(parents=True, exist_ok=True)
        declared_outputs = _resolve_output_declarations(state, workspace, output_files)

        # ── 渲染代码逐字节冻结（referee 重跑的锚点）────────────────────────
        geometry: dict[str, Any] | None = None
        if compiled:
            from ..diagram_compiler import (
                BACKENDS,
                DEFAULT_SCHEMATIC_BACKEND,
                compile_render_script,
            )

            # 出版级排版是这个节点存在的意义，所以它是**默认**而不是选项。
            backend = backend or DEFAULT_SCHEMATIC_BACKEND
            # tikz 后端的沙箱脚本只写 .tex —— 光栅化在框架进程里做，沙箱不再需要
            # pypdfium2（2026-09-18 之前这里给沙箱塞 TIKZ_RASTER_REQUIREMENTS，
            # 撞上「venv 没有 pip」就整次铸记录被拒）。
            if backend not in BACKENDS:
                raise VisualContractError(
                    f"backend must be one of {list(BACKENDS)}; got {backend!r}"
                )
            writable = {"png", "pdf", "svg"} if backend == "matplotlib" else {"pdf", "png"}
            bad = sorted(
                {
                    rel.rsplit(".", 1)[-1].lower()
                    for rel, _ in declared_outputs
                    if rel.rsplit(".", 1)[-1].lower() not in writable
                }
            )
            if bad:
                raise VisualContractError(
                    f"the {backend} contract backend writes {sorted(writable)}; "
                    f"cannot produce {bad}"
                )
            # 框架生成的渲染代码：几何以字面量内嵌，被冻结、被 hash、可 replay。
            # 出处绑定链一点没动 —— 只是写代码的人从模型换成了编译器。
            code_text, geometry = compile_render_script(
                contract,
                outputs=[rel for rel, _ in declared_outputs],
                backend=backend,
            )
        elif script_path:
            from core.project_workspace import resolve_tool_path

            script_source = resolve_tool_path(state, script_path)
            if not script_source.is_file():
                raise VisualContractError(f"script_path does not exist: {script_path!r}")
            code_text = script_source.read_text(encoding="utf-8")
            # 脚本是当**文件**写的（`Path(__file__).parent / 'parsed_data.json'`），
            # 而沙箱把它当 `-c` 代码跑 —— `__file__` 不在场，NameError。replay 命令
            # 跑的是冻结文件、`__file__` 在场：同一份代码两种结果。2026-09-18 replay11
            # 实测四张图各烧掉一次渲染预算。这里补上它，指向作者写的那个路径。
            code_text = f"__file__ = {str(script_source)!r}\n" + code_text
        else:
            code_text = str(code)
        if not code_text.strip():
            raise VisualContractError("render code is empty")
        if asserted:
            # 数据图的样式默认（字号 / 调色板 / DPI）是**冻结脚本的一部分**：
            # 它改变像素，所以必须进 hash、进 replay —— 只在沙箱里前置而不进
            # 脚本，referee 重跑出来的就不是这张图。
            code_text = (
                style_preamble(
                    medium=contract["medium"],
                    floor_pt=float((contract_entry or {}).get("floor_pt") or 7.0),
                )
                + "\n" + code_text
            )
        script_dir = workspace / "figure_src"
        script_dir.mkdir(parents=True, exist_ok=True)
        frozen_script = script_dir / f"{slug(output_name)}_render.py"
        # hash 的就是落盘的那几个字节：同一份 bytes 既写盘又算 hash。
        # write_text 在 Windows 上会把 \n 写成 \r\n，盘上的冻结脚本就不再是
        # 记录里 content_hash 说的那份，referee 拿文件对 hash 必然对不上。
        code_bytes = code_text.encode("utf-8")
        frozen_script.write_bytes(code_bytes)
        render_code_hash = hash_bytes(code_bytes)

        # ── 沙箱执行（复用 execute_python 的强制沙箱；不自建执行器）─────────
        from shared.tools.library import python_exec

        sidecar = audit_sidecar_path(workspace, request_id, render_code_hash)
        # ── 渲染预算：同一条请求最多 RENDER_ATTEMPTS_PER_REQUEST 次 ─────────
        # 登记在真的要花钱那一刻（沙箱执行之前）；参数写错被拒的不算。
        if request_id:
            attempt = _begin_attempt(state, request_id, contract_id)
        started_at = time.time()
        execution = await python_exec._execute_python(
            state,
            code=audit_preamble(str(sidecar)) + "\n" + code_text,
            timeout=timeout,
            requirements=requirements,
        )
        if execution.get("status") != "success":
            error = execution.get("error") or (
                f"render code did not exit cleanly (status={execution.get('status')})"
            )
            _note_attempt(attempt, "error", error=error)
            return {
                "status": "error",
                "error": error,
                "execution": execution,
                "renders_left": _render_budget(state, request_id)[1],
            }
        if compiled and backend == "tikz":
            # 沙箱里只写了 figure.tex；xelatex 由框架经唯一咽喉起，argv 全由框架拼。
            finish = await _finish_tikz_render(state, workspace, timeout=timeout)
            execution = {**execution, "tikz_finish": finish}
            if finish.get("status") != "success":
                _note_attempt(attempt, "error", error=finish.get("error"))
                return {
                    "status": "error",
                    "error": finish.get("error"),
                    "execution": execution,
                    "renders_left": _render_budget(state, request_id)[1],
                }

        # ── 输出文件必须由**这次执行**产出（像素诚信：不许挂既有文件充数）──
        # 声明了却没产出的文件：零产出无可铸（C）；部分产出时绑定已存在的文件，
        # 缺失项记 OB-DEVIATION（判决拆除三波，figure:351 降格，不再整次作废）。
        missing_outputs = [rel for rel, path in declared_outputs if not path.is_file()]
        declared_outputs = [(rel, path) for rel, path in declared_outputs if path.is_file()]
        if not declared_outputs:
            raise VisualContractError(
                f"none of the declared output files were produced by the render code: "
                f"{missing_outputs}; the code must actually write the files it declares"
            )
        stale = [
            rel
            for rel, path in declared_outputs
            if path.stat().st_mtime < started_at - 2.0
        ]
        if stale:
            raise VisualContractError(
                f"declared output files were not written by this execution: {stale}; "
                "re-render instead of binding pre-existing files"
            )

        # ── 出门检查：合同里写的字，**渲染出来的文件里真的有吗** ─────────────
        # 「合同说了、图上没有、没人报」是这套系统存在的理由，而我们一直只核到
        # 几何为止。2026-09-17 实测两次都是肉眼发现的：TikZ 把 ① 丢了、把 ≥ 丢了。
        # 几何里那两个字符都在，所以任何几何判据都查不出来。
        #
        # PDF 有文字层，抠出来逐条比对即可 —— 这是唯一一次「看见的是最终产物」。
        text_findings = _verify_text_reached_the_page(
            geometry, [path for _rel, path in declared_outputs], workspace
        )

        files: list[dict[str, Any]] = [
            {
                "format": path.suffix.lower().lstrip("."),
                "path": paths.display_relpath(state, path),
                "absolute_path": str(path),
                "content_hash": hash_file(path),
                "size_bytes": path.stat().st_size,
            }
            for _rel, path in declared_outputs
        ]

        # ── 恒跑图像级机械审计（findings，无拒绝分支）───────────────────────
        request_findings, caller_request = _request_findings(state, request_id)
        # brief 只用于导出预设推断（presentation_defaults），不是身份；caption
        # 空时用记录名占位，别让空 caption 在这里撞上「空请求」契约。
        brief = caller_request or {"intent": caption or output_name, "purpose": purpose}
        if purpose:
            brief = {**brief, "purpose": purpose}
        publication_grade, _exploratory = presentation_defaults(normalize_request(brief))
        sidecar_records = read_sidecar(sidecar)
        audit_findings, audit_facts = audit_figure_outputs(
            output_paths={item["format"]: Path(item["absolute_path"]) for item in files},
            sidecar_records=sidecar_records,
            publication_grade=publication_grade,
            glyph_warning=execution.get("figure_glyph_warning"),
        )
        sidecar.unlink(missing_ok=True)

        findings: list[dict[str, Any]] = [*request_findings, *audit_findings]

        # ── 合同核查：你声明的和你做出来的是不是同一件事 ───────────────────
        #
        # 这不是质量判决（「够不够发表」仍然没有任何字段回答，仍归 referee）。
        # 这是自相矛盾检测，和 figure_hash 对不上时拒绝录入同一类 —— 所以它
        # 有拒绝分支，而机械审计/VLM 证人没有。
        observed = observed_object_model(sidecar_records)
        contract_checks: list[dict[str, Any]] = []
        previous = _previous_figure_record(state, request_id)
        if contract:
            contract_checks = evaluate_assertions(
                contract, None if compiled else observed
            )
            failures = failed_assertions(contract_checks)
            design: list[str] = []
            if asserted:
                # ── 数据图的设计合同：图例 / 轴单位 / 印刷字号 / 调色板 / 语言 ──
                # 每一条都是「合同说的和纸上的不是同一件事」，与断言同级：拒绝。
                design = asserted_design_failures(
                    contract,
                    observed,
                    medium=contract["medium"],
                    floor_pt=float((contract_entry or {}).get("floor_pt") or 7.0),
                    policy=(contract_entry or {}).get("policy"),
                    enforce_print=bool((contract_entry or {}).get("enforce_print", True)),
                )
            if failures or design:
                # 断言与设计义务**一次报全**：iter11 的代码没给任何 label，断言先报
                # 「2 条序列实得 192」就停了，而根因（没标签、没图例）在设计那一栏。
                # 一次一个往返报一条，模型就得猜三次。
                parts: list[str] = []
                if failures:
                    parts.append(
                        "assertions: "
                        + "; ".join(
                            f"{item['id']} expects {item['comparator']} {item['expected']} "
                            f"but the rendering gives {item['actual']} "
                            f"(from the request: {item['derived_from']!r})"
                            for item in failures
                        )
                    )
                if design:
                    parts.append("design contract: " + " | ".join(design))
                raise VisualContractError(
                    "the rendered figure contradicts its own contract — "
                    + " || ".join(parts)
                    + (
                        ". The object model was read from the rendered figure, not from "
                        "your description of it. Fix the code and render again — see "
                        "render_guidance from declare_figure_contract."
                        if not compiled
                        else "."
                    )
                )
            if not compiled and not observed.get("attached"):
                findings.append(
                    {
                        "collector": "OB-CONTRACT-UNVERIFIED",
                        "message": (
                            "no matplotlib object model was captured, so the contract's "
                            "assertions were evaluated against nothing; they are neither "
                            "confirmed nor contradicted"
                        ),
                    }
                )
            # 声明期就已经知道的缺口（需求里的数没进合同 / 词表接不住的键 /
            # 归一化丢掉的键）必须跟着记录走到底，否则它只是给模型看了一眼。
            findings.extend(
                {"collector": "OB-CONTRACT-COVERAGE", **item}
                if "collector" not in item else item
                for item in (contract_entry or {}).get("coverage_findings") or []
            )
            if geometry and geometry.get("layout_findings"):
                findings.extend(geometry["layout_findings"])
            # 出门检查：合同里写的字，渲染出来的文件里真的有吗
            findings.extend(text_findings)
            # 跨版本回归：上一版成立、这一版不成立的断言；被删掉/被放松的断言；
            # 变少的节点与边。v1 的八版里没有任何东西比较相邻两版，所以
            # 「4 条 GPU 连线塌成 1 条」完全不可见。
            findings.extend(contract_diff((previous or {}).get("contract"), contract))
            findings.extend(
                check_result_diff((previous or {}).get("contract_checks"), contract_checks)
            )
        else:
            # 合同缺席是**可读事实**，不是沉默。没有它，这张图里没有任何结构
            # 事实被机械核对过 —— 记录必须这么说，而不是长得像通过了。
            findings.append(
                {
                    "collector": "OB-NO-CONTRACT",
                    "message": (
                        "no figure contract: nothing about this figure's structure was "
                        "checked against a declaration; only file-level and visual "
                        "observations apply"
                    ),
                }
            )
            if (
                asset_kind in {"auto", ""}
                and observed.get("attached")
                and observed.get("series_count") == 0
                and observed.get("panel_count")
            ):
                # 零数据序列 = 这是张图解不是统计图。机械可判，所以由框架说，
                # 不靠 agent 自觉声明 asset_kind。
                findings.append(
                    {
                        "collector": "OB-UNDECLARED-SCHEMATIC",
                        "message": (
                            "this figure renders no data series at all, i.e. it is a "
                            "diagram: declare asset_kind='schematic' with a figure "
                            "contract so its structure can be checked mechanically"
                        ),
                    }
                )
        if missing_outputs:
            findings.append(
                {
                    "collector": "OB-DEVIATION",
                    "message": (
                        "declared output files were not produced by the render code; "
                        "the record binds only the files that exist"
                    ),
                    "missing_output_files": missing_outputs,
                }
            )
        if empty_text_fields:
            findings.append(
                {
                    "collector": "OB-CAPTION",
                    "message": (
                        "figure text fields are empty: " + ", ".join(empty_text_fields)
                        + "; recorded as-is (publication paths must supply them)"
                    ),
                    "empty_fields": empty_text_fields,
                }
            )
        if not source_ids:
            findings.append(
                {
                    "collector": "OB-PROVENANCE",
                    "message": (
                        "no source artifacts bound: this figure claims no upstream "
                        "scientific data (legitimate only for schematics/illustrations)"
                    ),
                }
            )
        if generative_content:
            findings.append(
                {
                    "collector": "OB-GENERATIVE",
                    "message": "figure declares generative pixels (evidence_bearing=false)",
                    "generative_content": generative_content,
                }
            )

        # ── VLM 证人（在场即跑；缺席= findings 里自然没有该类条目）──────────
        png_path = next(
            (Path(item["absolute_path"]) for item in files if item["format"] == "png"), None
        )
        witness_findings, vlm_review = await _witness_review(
            png_path=png_path,
            caption=caption,
            asset_kind=asset_kind,
            publication_grade=publication_grade,
            contract=contract,
        )
        findings.extend(witness_findings)
        if compiled and png_path is None:
            findings.append(
                {
                    "collector": "OB-NO-RASTER",
                    "message": (
                        "no PNG was produced, so no visual reviewer looked at this "
                        "figure; the contract check stands on its own"
                    ),
                }
            )

        # ── 铸唯一的 figure 记录（判决词表被机械拒绝）───────────────────────
        script_relpath = paths.display_relpath(state, frozen_script)
        replay: dict[str, Any] = {
            "command": ["python3", script_relpath],
            "cwd": paths.display_relpath(state, workspace),
            "requirements": list(requirements or []),
            "note": (
                "re-running the frozen script from the run workspace reproduces "
                "every declared output file"
            ),
        }
        tex_step = execution.get("tikz_finish")
        if tex_step:
            # tikz 的冻结脚本只写 .tex —— 图是 TeX 编出来的，用的哪个编译器（系统
            # TeX Live 还是随包 tectonic，字体解析不同）也是这张图出处的一部分。
            replay["tex"] = {
                "compiler": tex_step.get("compiler"),
                "source": "_contract_tikz/figure.tex",
            }
            replay["note"] = (
                "re-running the frozen script writes _contract_tikz/figure.tex; the "
                f"framework then compiles it with {tex_step.get('compiler')} "
                "(shared.tools.library.latex.run_tex, the road manuscripts take) and "
                "delivers the declared files"
            )
        metadata: dict[str, Any] = {
            "schema_version": FIGURE_SCHEMA_VERSION,
            "record": RENDERED_FIGURE_RECORD,
            "request_id": request_id,
            "asset_kind": asset_kind,
            "purpose": purpose,
            "source_artifact_ids": source_ids,
            "source_hashes": source_hashes,
            "render_code": {
                "path": script_relpath,
                "content_hash": render_code_hash,
                "interpreter": "python3",
                # 谁写了这段代码。compiled 家族是编译器写的 —— referee 重跑
                # 时该知道改它没意义，要改的是合同。
                "generator": (
                    f"figure-contract-compiler/{backend}" if compiled else "agent"
                ),
            },
            "contract": contract,
            "contract_hash": (contract_entry or {}).get("contract_hash"),
            # 调用方绑定的政策与印刷判据也进记录：将来查「这张图按哪个版心、
            # 哪个字号下限判的」翻记录就知道，不用重演。
            "caller_policy": (contract_entry or {}).get("policy"),
            "font_floor_pt": (contract_entry or {}).get("floor_pt"),
            # 可读性度量逐版进记录：「越改越差」在可读性上也要看得见，
            # 不能只在结构上看得见。
            "layout": (
                {
                    "engine": geometry.get("layout_engine"),
                    "metrics": geometry.get("layout_metrics"),
                    # 度量回答「有没有毛病」，digest 回答「长什么样」。少了后者，
                    # 作者只能去裁 PNG 手算坐标（iter15 实测烧掉大半预算）。
                    "digest": layout_digest(geometry),
                }
                if geometry
                else None
            ),
            "contract_checks": contract_checks,
            "object_model": observed if observed.get("attached") else None,
            "files": files,
            "caption": caption,
            "alt_text": alt_text,
            "findings": findings,
            "audit": audit_facts,
            "vlm_review": vlm_review,
            "generative_content": generative_content,
            "replay": replay,
        }
        metadata["figure_hash"] = figure_binding_hash(metadata)
        reject_verdict_fields(metadata)

        preferred = next((item for item in files if item["format"] == "png"), files[0])
        content = f"![{alt_text}]({preferred['path']})\n\nCaption: {caption}"
        saved = state.save_artifact(
            artifact_type="figure", name=output_name, content=content, metadata=metadata
        )
        _note_attempt(
            attempt,
            "success",
            figure_id=saved["id"],
            finding_collectors=sorted({str(item.get("collector") or item.get("code") or "") for item in findings} - {""}),
        )
        return {
            "status": "success",
            "figure_id": saved["id"],
            "renders_left": _render_budget(state, request_id)[1],
            "figure_hash": metadata["figure_hash"],
            "files": files,
            "findings": findings,
            "audit": audit_facts,
            "vlm_review": vlm_review,
            "contract_checks": contract_checks,
            "layout": metadata.get("layout"),
            "replay": metadata["replay"],
        }
    except (VisualContractError, PermissionError, ValueError, OSError, RuntimeError) as exc:
        _note_attempt(attempt, "error", error=str(exc)[:400])
        out: dict[str, Any] = {"status": "error", "error": str(exc)}
        code_name = getattr(exc, "error_code", None)
        if code_name:
            out["error_code"] = code_name
        if request_id:
            out["renders_left"] = _render_budget(state, request_id)[1]
        return out


def _note_attempt(attempt: dict[str, Any] | None, outcome: str, **facts: Any) -> None:
    """把这次渲染的结果写回登记条目（到上限时它们会被摆给模型看）。"""

    if attempt is None:
        return
    attempt["outcome"] = outcome
    attempt.update({key: value for key, value in facts.items() if value is not None})


def _preferred_png(state: State, figure_record: dict[str, Any]) -> Path:
    metadata = (
        figure_record.get("metadata") if isinstance(figure_record.get("metadata"), dict) else {}
    )
    files = metadata.get("files")
    entries = files if isinstance(files, list) else []
    info = next(
        (item for item in entries if isinstance(item, dict) and item.get("format") == "png"),
        None,
    )
    if not isinstance(info, dict) or not info.get("path"):
        raise VisualContractError("VLM review requires a PNG rendering")
    path = paths.resolve_display_relpath(state, str(info["path"]))
    if not paths.within_run_scope(state, path) or not path.exists():
        candidates = ", ".join(
            str(p) for p in paths.display_relpath_candidates(state, str(info["path"]))
        )
        raise VisualContractError(
            f"figure PNG is missing or outside this run's tree (looked in: {candidates})"
        )
    return path


async def _inspect_figure(state: State, figure_id: str, **_: Any) -> dict[str, Any]:
    """按需 VLM 观察一张已铸的 figure（证人，不铸记录、不给判决）。"""

    try:
        record = state.read_artifact(figure_id)
        if record is None or record.get("type") != "figure":
            raise VisualContractError(f"{figure_id!r} is not a figure artifact")
        from core import model_roles

        # 角色在场由注册表 required_runtime_capability="model_role:visual_review"
        # 在派发口保证（CAPABILITY_DENIED）；这里不再重复一道同条件的门。
        binding = model_roles.resolve("visual_review")
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        png_path = _preferred_png(state, record)

        from ..vlm_witness import (
            ReviewerConfig,
            map_observations,
            review_checklist,
            review_image,
        )

        config = ReviewerConfig()
        kind = str(metadata.get("asset_kind") or "quantitative")
        result = await review_image(
            image_path=png_path,
            panel_id="GLOBAL",
            intent=str(metadata.get("caption") or "scientific figure"),
            checklist=review_checklist(
                kind if kind != "auto" else "quantitative",
                composition=False,
                publication=True,
            ),
            composition=False,
            review_context={
                "image_content_hash": hash_file(png_path),
                "review_scope": "whole_figure",
            },
            allowed_panel_ids=["GLOBAL"],
            config=config,
        )
        observations = list(result.get("observations") or [])
        return {
            "status": "success" if result.get("status") == "success" else "review_incomplete",
            "figure_id": figure_id,
            "completed": result.get("status") == "success",
            "reviewer": {
                "provider": binding.provider,
                "model": binding.model,
                "calibration_applies": binding.model == config.calibrated_model,
            },
            "observations": observations,
            "mapped_findings": map_observations(observations),
            "attempts": result.get("attempts") or [],
            "note": (
                "observations are working notes for you; if you change the figure, "
                "re-render it with render_figure so the new record carries fresh findings"
            ),
        }
    except (VisualContractError, PermissionError, ValueError, OSError) as exc:
        return {"status": "error", "error": str(exc)}


register_tool(
    ToolDefinition(
        name="render_figure",
        description=(
            "Render one scientific figure by executing your plotting code in the mandatory "
            "sandbox and minting the single evidence-carrying figure record. The framework "
            "mechanically binds provenance (source artifact hashes ↔ frozen render-code hash "
            "↔ output file hashes), always runs the image-level mechanical audit, runs one "
            "visual-review witness pass when the platform configures a visual_review role, "
            "and records everything as findings — no verdicts, no status. The frozen script "
            "plus replay command lets a referee re-run the figure byte-for-byte. Pass the "
            "exact request_id from the briefing when fulfilling a caller request. "
            "Forged records are impossible: the record is minted only here, and its "
            "figure_hash is recomputed by every consumer. Each request_id may be rendered "
            "at most 3 times; the 4th call returns error_code=render_budget_exhausted with "
            "every attempt's outcome — stop, move on, and report the unmet requirement to "
            "the caller. For quantitative/composite contracts the rendered object model is "
            "also checked against the design contract (legend, axis units, print font size, "
            "palette) and a contradiction is rejected."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "output_name": {"type": "string", "description": "figure 记录名（也用于冻结脚本名）"},
                "caption": {"type": "string"},
                "alt_text": {"type": "string"},
                "contract_id": {
                    "type": "string",
                    "description": (
                        "declare_figure_contract 返回的 id。schematic 必须给（框架按合同"
                        "编译渲染，不收 code）；quantitative/composite 给了就用对象模型"
                        "逐条核对断言"
                    ),
                },
                "backend": {
                    "type": "string",
                    "enum": ["matplotlib", "tikz"],
                    "description": (
                        "仅 schematic：合同的渲染后端。**默认 tikz**（出版级排版、"
                        "原生矢量 PDF、自动附带光栅化依赖出 png）。只有在需要 svg "
                        "或明确不要 LaTeX 排版时才传 matplotlib。"
                    ),
                },
                "code": {
                    "type": "string",
                    "description": (
                        "matplotlib/绘图 Python 代码；与 script_path 二选一。"
                        "schematic 走合同编译，不要传这个"
                    ),
                },
                "script_path": {
                    "type": "string",
                    "description": "run 内既有绘图脚本路径；与 code 二选一",
                },
                "output_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "代码写出的图文件（工作区相对路径），至少一个；建议含 png",
                },
                "source_artifact_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "这张图读取的上游数据 artifact id；示意图可为空数组"
                        "（记录会如实披露「无上游数据绑定」）"
                    ),
                },
                "request_id": {"type": "string", "description": "开局 briefing 列出的调用方请求 id"},
                "purpose": {"type": "string", "enum": sorted(PURPOSES)},
                "asset_kind": {"type": "string", "enum": sorted(ASSET_KINDS)},
                "generative": {
                    "type": "object",
                    "description": (
                        "仅当图含生成式像素时声明 {role, evidence_bearing:false}；"
                        "evidence_bearing 不为 false 一律拒绝（生成式像素不得充当证据图）"
                    ),
                },
                "requirements": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer", "default": 300, "minimum": 1, "maximum": 7200},
            },
            "required": ["output_name", "caption", "alt_text", "output_files",
                         "source_artifact_ids"],
        },
        allowed_node_types=_ALLOWED_NODES,
        risk_level="high",
        internal_only=False,
    ),
    _render_figure,
)

register_tool(
    ToolDefinition(
        name="inspect_figure",
        description=(
            "Ask the platform-configured visual reviewer (VLM witness) to look at an "
            "existing figure record's PNG and report visible observations. Returns "
            "observations and deterministic rubric mappings as working notes — it mints "
            "nothing and issues no verdict. If you change the figure, re-render it with "
            "render_figure so the new record carries fresh findings."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"figure_id": {"type": "string"}},
            "required": ["figure_id"],
        },
        allowed_node_types=_ALLOWED_NODES,
        risk_level="low",
        internal_only=False,
        required_runtime_capability="model_role:visual_review",
    ),
    _inspect_figure,
)


register_tool(
    ToolDefinition(
        name="declare_figure_contract",
        description=(
            "Declare, BEFORE writing any rendering code, the structural facts this figure "
            "must satisfy — and for schematics, the diagram itself. The contract is the "
            "only bridge between what was asked for and what gets drawn: the framework "
            "renders a schematic FROM this declaration (so a declared edge cannot be "
            "forgotten and an undeclared one cannot appear), and checks a quantitative "
            "figure's rendered matplotlib object model AGAINST it. Assertions are "
            "Three things are bound to the request_id by the caller's visual_request and "
            "cannot be changed here: the family (asset_kind), the print medium "
            "(constraints.width → medium) and the figure-text language/forbidden words. "
            "Schematics are print-fitted at declaration: if the smallest text tier would "
            "print below the floor (7pt in print), the declaration is rejected with "
            "measured alternatives (units per row, split, drop the smallest tier). "
            "Quantitative/composite declarations return render_guidance (figsize ceiling, "
            "font floor, palette, legend and axis-label obligations). "
            "evaluated here, so a contract that contradicts itself is rejected before a "
            "single pixel exists. Returns the contract_id to pass to render_figure, plus "
            "this family's semantic self-check list."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "开局 briefing 列出的调用方请求 id，逐字引用",
                },
                "asset_kind": {
                    "type": "string",
                    "enum": ["schematic", "quantitative", "composite"],
                    "description": (
                        "schematic：合同即渲染源；quantitative/composite：合同即断言，"
                        "渲染后读对象模型核对"
                    ),
                },
                "contract": {
                    "type": "object",
                    "description": (
                        "schematic：{nodes, groups, bands, edges, assertions, title?, notes?}"
                        "；quantitative/composite：{panels, series, assertions, title?}。"
                        "每条 assertion 必须带 derived_from（这条来自需求原文的哪句话）。"
                        "完整 schema 见 declare_figure_contract 的返回值 schema 字段。"
                    ),
                },
            },
            "required": ["request_id", "asset_kind", "contract"],
        },
        allowed_node_types=_ALLOWED_NODES,
        risk_level="low",
        internal_only=False,
    ),
    _declare_figure_contract,
)




async def _figure_contract_schema_tool(state: State, asset_kind: str, **_: Any) -> dict[str, Any]:
    return _figure_contract_schema(asset_kind)


def _figure_contract_schema(asset_kind: str) -> dict[str, Any]:
    family = family_for_asset_kind(asset_kind)
    if family is None:
        return {
            "status": "error",
            "error": f"asset_kind {asset_kind!r} has no contract family",
        }
    example: dict[str, Any] = (
        {
            "title": "两级 PCIe 交换拓扑",
            # 这张图在哪儿被读。画布的 pt 数**就是**物理英寸数 —— 不写就按
            # 调用方的 purpose 推，写了就按这个版心判「复现之后还读不读得了」。
            "medium": "double_column",
            # 版面式：figure 是**带标题的 block 列表**，diagram 只是其中一种。
            # ①②③ 那种分栏叙事、规格摘要、整段注记都落在这里 —— 一张好的
            # 架构图不是一个 graph 的渲染，是一个版面，里面包含一个 graph。
            "blocks": [
                {
                    "kind": "diagram",
                    "title": "① 单台服务器内部拓扑",
                    "nodes": [
                        # **规格写在组件上**：读者看这个盒子时就想知道的事，写 sublabel；
                        # 只是分类信息才交给 role 和图例。用户点名「高级得多」的
                        # 那张参考图 19 个节点里 17 个带副标题 —— 这个 example
                        # 一度只有 1/11，而模型会照抄 example 的密度
                        # （实测真跑 1/19，加了指南才到 3/19；2026-09-17 根因）。
                        {"id": "cpu0", "label": "CPU 0", "role": "CPU",
                         "sublabel": "NUMA 0"},
                        # 图 / 地：这张图讲的是两个 switch 怎么挂 GPU，
                        # 所以 switch 是主角、CPU 是陪衬。多数节点不写 =
                        # 正常档；全体 primary 会被当作自相矛盾拒绝。
                        {"id": "sw0", "label": "PCIe Switch 0", "role": "PCIe Switch",
                         "sublabel": "挂 G0–G3 + NIC 0", "emphasis": "primary"},
                        # chip = 密排小色块：成批的同类元件这样画才排得下，
                        # 而且一眼读成「一批」而不是 N 个独立组件。
                        {"id": "g0", "label": "G0", "role": "GPU", "shape": "chip",
                         "sublabel": "RTX PRO 6000"},
                        {"id": "g1", "label": "G1", "role": "GPU", "shape": "chip",
                         "sublabel": "RTX PRO 6000"},
                        {"id": "g2", "label": "G2", "role": "GPU", "shape": "chip",
                         "sublabel": "RTX PRO 6000"},
                        {"id": "g3", "label": "G3", "role": "GPU", "shape": "chip",
                         "sublabel": "RTX PRO 6000"},
                        {"id": "nic0", "label": "NIC 0", "role": "NIC",
                         "sublabel": "400 Gbps RoCE"},
                        # side="right"：它立在整摞层**旁边**，不属于任何一层。
                        # 放进某一层就得横穿那一层；单独给它一层，别的层之间的
                        # 线又得穿过它。写了 side，框架把它摆到侧边、并让它的
                        # 边走一条公共侧轨。
                        {"id": "bmc", "label": "带外管理 BMC", "role": "Management",
                         "sublabel": "1 GbE", "side": "right"},
                    ],
                    # 出框**叙事引出**：说的是「这条线去哪儿了」，是叙事不是
                    # 拓扑 —— 别硬塞成一条边加一个假节点，那会让机械断言失真。
                    "annotations": [
                        # represents=2：这一句引出代表 2 条链路，不是 1 条。
                        # 跨栏对账数的是链路条数 —— ② 栏那台服务器就得接 2 条
                        # （或者一条 represents=2 的线）。
                        {"anchor": "nic0", "side": "bottom",
                         "text": "2 × 400G 上联 RoCE 交换机", "represents": 2}
                    ],
                    "groups": [
                        # style="dashed" = 机箱/边界（与实体子系统的实线框区分）
                        {"id": "srv", "label": "服务器机箱（单节点）",
                         "label_position": "top", "style": "dashed",
                         "ranks": [["cpu0"]]},
                        # 归属写进结构：switch 和它自己的 GPU、网卡是一个子系统。
                        # 层次形状要让每条边只跨一级：把 switch 的两类下游分居
                        # 它的**上下两侧**（GPU 在上、NIC 在下）。堆在同一侧
                        # 会让 switch→NIC 横穿整条 GPU 级，连线被迫绕大弯。
                        # 画了框就得给名字：读者看见一个灰框，得知道它圈的是
                        # 什么。留空会被判据点名。
                        {"id": "dom0", "parent": "srv", "label": "PCIe 域 0", "ranks":
                         [["g0", "g1", "g2", "g3"], ["sw0"], ["nic0"]]},
                    ],
                    "bands": [["group:srv"]],
                    "edges": [
                        {"from": "cpu0", "to": "sw0", "label": "PCIe", "role": "PCIe 通道"},
                        # **每条边都要有 role。** 没 role 的边不进图例 —— 纸上
                        # 就有一种线没人解释它是什么。这个 example 一度让这四条
                        # 挂着空 role，而模型照抄：真跑连着两轮都是「9 条有 role、
                        # 8 条没有」（2026-09-17）。
                        {"from": "sw0", "to": "g0", "role": "PCIe 挂载"},
                        {"from": "sw0", "to": "g1", "role": "PCIe 挂载"},
                        {"from": "sw0", "to": "g2", "role": "PCIe 挂载"},
                        {"from": "sw0", "to": "g3", "role": "PCIe 挂载"},
                        {"from": "sw0", "to": "nic0", "role": "RoCE 链路"},
                        {"from": "cpu0", "to": "bmc", "role": "带外管理"},
                    ],
                },
                {
                    "kind": "diagram",
                    "title": "② 集群网络拓扑",
                    "nodes": [
                        # detail_of：② 栏这一个盒子，就是 ① 栏画开的那个机箱。
                        # 写上它，图上才会多一道内嵌边框 +「详见 ①」—— 否则
                        # 读者看到的是两栏各画各的，认不出讲的是同一个东西。
                        # 而且两栏会**对账**：① 栏那台机器朝外引了几条
                        # annotation，② 栏这个盒子就得接几条线。
                        {"id": "n1", "label": "服务器 1", "role": "Server",
                         "sublabel": "2×CPU · 4×GPU · 1×NIC", "detail_of": "srv"},
                        {"id": "n2", "label": "服务器 2", "role": "Server",
                         "sublabel": "2×CPU · 4×GPU · 1×NIC", "detail_of": "srv"},
                        # bar = 通栏长条：共享骨干（交换机/总线）用形状说出来。
                        # ports=8：画出 8 个端口小块，「这是一台 8 口交换机」
                        # 一眼可见，不用读者去数连线。
                        {"id": "fabric", "label": "RoCE 交换机",
                         "sublabel": "4 × 400 GbE", "role": "Fabric",
                         "shape": "bar", "ports": 4},
                    ],
                    "groups": [],
                    "bands": [["node:n1", "node:n2"], ["node:fabric"]],
                    # 端口数要对得上线数：2 个口、2 条线。声明 8 个口却只画
                    # 4 条线，读者看到的是 4 个空口、只能自己猜。
                    # represents=2：一笔画两条链路，图上是并行双线。端口数与
                    # 跨栏对账都按 2 算，所以 4 个口对得上 2 笔 × 2。
                    "edges": [{"from": "n1", "to": "fabric", "label": "2 × 400G",
                               "role": "RoCE 链路", "represents": 2},
                              {"from": "n2", "to": "fabric", "label": "2 × 400G",
                               "role": "RoCE 链路", "represents": 2}],
                },
                {
                    "kind": "spec",
                    "title": "③ 规格摘要",
                    "columns": 2,
                    "items": [
                        "每台服务器：2 × CPU，8 × GPU",
                        "每 4 张 GPU 挂 1 个 PCIe Switch",
                        "两个 Switch 互联：400 Gbps / 0.1 µs",
                        "集群合计：32 × GPU、8 个 Switch",
                    ],
                },
                {"kind": "note", "text": "注：4 台服务器硬件配置完全相同。"},
            ],
            "assertions": [
                {
                    "id": "fanout",
                    "kind": "neighbors",
                    "node": "sw0",
                    "within": {"role": "GPU"},
                    "equals": 4,
                    "derived_from": "每 4 张 GPU 连接一个 PCIe switch",
                }
            ],
        }
        if family in COMPILED_FAMILIES
        else {
            "title": "Convergence of two systems",
            # 版心由调用方 constraints.width 绑定；这里写了不同的会被拒。
            "medium": "double_column",
            "panels": [
                {
                    "id": "p1", "label": "a", "message": "energy converges with steps",
                    # 每根轴：量什么、什么单位（分类/无量纲轴明写 "none"）、什么刻度。
                    # 纸上必须出现 `Energy (eV)` 与 `Step` —— 渲染后按对象模型对账。
                    "axes": {
                        "x": {"label": "Step", "unit": "none", "scale": "linear"},
                        "y": {"label": "Energy", "unit": "eV", "scale": "log"},
                    },
                }
            ],
            # ≥2 条序列 ⇒ 图例是义务：条目逐字等于这些 label、不重复、在画布内。
            "series": [
                {"id": "s1", "label": "System A", "panel": "p1", "source_field": "energy_a"},
                {"id": "s2", "label": "System B", "panel": "p1", "source_field": "energy_b"},
            ],
            "assertions": [
                {
                    "id": "n-series",
                    "kind": "series_count",
                    "equals": 2,
                    "derived_from": "对比 A 和 B 两个体系",
                },
                {
                    "id": "log-y",
                    "kind": "axis_scale",
                    "axis": "y",
                    "equals": "log",
                    "derived_from": "纵轴用对数",
                },
            ],
        }
    )
    return {
        "status": "success",
        "family": family,
        "renders_from_contract": family in COMPILED_FAMILIES,
        "schema": contract_schema(family),
        "family_checklist": list(FAMILY_CHECKLIST.get(family, ())),
        "example": example,
        # 密图是第三种局面，也学不会 —— 上面那个例子只有 8 个节点，扇子从来
        # 没出现过。2026-09-17 实测：22 节点 / 65 边那张微服务图，模型第三次
        # 声明写 `to="biz"`（biz 是它自己声明的组），想画「网关调用整个业务层」
        # 一条线而不是十条 —— 被拒后再没找回来，四次声明全被拒、一张图没画出来。
        "dense_example": (
            {
                "title": "微服务全景（收扇子的写法）",
                "medium": "double_column",
                "nodes": [
                    {"id": "gw", "label": "API 网关", "role": "接入层"},
                    {"id": "user", "label": "用户", "role": "业务层"},
                    {"id": "order", "label": "订单", "role": "业务层"},
                    {"id": "pay", "label": "支付", "role": "业务层"},
                    {"id": "mysql", "label": "MySQL", "role": "数据层"},
                    {"id": "redis", "label": "Redis", "role": "数据层"},
                    {"id": "prom", "label": "Prometheus", "role": "可观测"},
                ],
                "groups": [
                    {"id": "access", "label": "接入层", "ranks": [["gw"]]},
                    {"id": "biz", "label": "业务层", "ranks": [["user", "order", "pay"]]},
                    {"id": "data", "label": "数据层", "ranks": [["mysql", "redis"]]},
                ],
                "bands": [["group:access"], ["group:biz"], ["group:data"],
                          ["node:prom"]],
                "edges": [
                    # **边的端点可以写 `group:<id>`** —— 和 bands 同一套词法。
                    # 「网关调用整个业务层」是一条边，不是三条；「每个服务都上报
                    # Prometheus」是一条边，不是十条。这是密图里最省交叉的一个
                    # 动作：22 节点 / 65 边那张图实测 267 → 30，而且 22 个节点
                    # 一个不少、不用折叠、不用分栏。
                    {"from": "gw", "to": "group:biz", "role": "服务调用"},
                    {"from": "group:biz", "to": "group:data", "role": "数据读写"},
                    {"from": "group:biz", "to": "prom", "role": "可观测上报"},
                    # 具体到某个组件的关系照样单独写 —— 收扇子不是把细节抹掉，
                    # 是把**说不出区别**的那一把收起来。
                    {"from": "order", "to": "pay", "role": "服务调用"},
                ],
                "assertions": [
                    {"id": "n-services", "kind": "node_count",
                     "selector": {"role": "业务层"}, "equals": 3,
                     "derived_from": "业务层三个服务"},
                ],
            }
        ),
        # 时序是另一种摆法，光看上面那个例子学不会 —— 词表送到与否，看的是
        # 「他能不能照着写出来」，不是「有没有写在 schema 里」。
        "sequence_example": (
            {
                "title": "课题执行时序",
                # 参与者横排一行；消息用 edges[].step 写次序，框架按步从上往下
                # 各排一行，并画出生命线。**同一对参与者之间来回多条消息时非写
                # 不可** —— 不写，它们全塌成一条线、标签叠成一团。
                "nodes": [
                    {"id": "user", "label": "用户", "role": "参与者"},
                    {"id": "fe", "label": "前端", "role": "参与者"},
                    {"id": "sched", "label": "调度器", "role": "参与者"},
                    {"id": "node", "label": "执行节点", "role": "参与者"},
                ],
                "groups": [],
                "bands": [["node:user", "node:fe", "node:sched", "node:node"]],
                "edges": [
                    {"from": "user", "to": "fe", "label": "① 提交课题",
                     "step": 1, "role": "消息"},
                    {"from": "fe", "to": "sched", "label": "② 创建 run",
                     "step": 2, "role": "消息"},
                    {"from": "sched", "to": "node", "label": "③ 派发任务",
                     "step": 3, "role": "消息"},
                    # 自己发给自己在时序里是正经消息（自查、内部重试、回报进度）
                    {"from": "node", "to": "node", "label": "④ 回报进度（多次）",
                     "step": 4, "role": "消息"},
                    {"from": "node", "to": "sched", "label": "⑤ 提交产物",
                     "step": 5, "role": "消息"},
                    {"from": "sched", "to": "node", "label": "⑤a 失败则重新派发",
                     "step": 6, "role": "重试"},
                    {"from": "sched", "to": "fe", "label": "⑥ 通知刷新",
                     "step": 7, "role": "消息"},
                    {"from": "fe", "to": "user", "label": "⑦ 呈现结果",
                     "step": 8, "role": "消息"},
                ],
                "assertions": [
                    {"id": "msg_count", "kind": "edge_count", "equals": 8,
                     "derived_from": "七步消息 + 一条失败重派"},
                ],
            }
            if family in COMPILED_FAMILIES
            else None
        ),
        "layout_note": (
            "**figure 是一个版面，不是一张图**：blocks 是带标题的分栏，diagram 只是"
            "其中一种 block（还有 spec 规格摘要、note 整段注记）。一栏之内：bands 是"
            "行（自上而下），每行横排若干 group/node；group 内 ranks 是行。几何由框架"
            "按内容算 —— 你不写任何坐标，也不会重叠或出框。\n"
            "形状要表意：成批的同类元件用 shape='chip'（密排小色块），共享骨干用"
            "shape='bar'（通栏长条）；边给 role 就会按角色配色并进图例。\n"
            "顶层直接写 nodes/groups/bands/edges 的平铺旧式仍然合法（= 只有一个"
            "diagram block 的版面），但只有版面式能表达 ①②③ 分栏与规格摘要。"
            if family in COMPILED_FAMILIES
            else "面板与序列的数目、轴刻度会从渲染后的 matplotlib 对象树上读出来核对。"
        ),
    }


register_tool(
    ToolDefinition(
        name="figure_contract_schema",
        description=(
            "Return the exact JSON schema and the semantic self-check list for one figure "
            "contract family, plus a worked example. Read this before declaring your first "
            "contract of a given family."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "asset_kind": {
                    "type": "string",
                    "enum": ["schematic", "quantitative", "composite"],
                }
            },
            "required": ["asset_kind"],
        },
        allowed_node_types=_ALLOWED_NODES,
        risk_level="low",
        internal_only=False,
    ),
    _figure_contract_schema_tool,
)
