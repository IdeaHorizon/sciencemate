"""Artifact 进阶工具：freeze_artifact。

把现有 artifact 标记为 frozen，之后 save_artifact 试图覆盖同一 type+name 时
会直接被 state.save_artifact 拒掉。

冻结是账本上的一行（`core/ledger`，RFC 2026-09-12 §6）：文件一个字节不动，
账本钉死 `path@sha256`，平台 commit 咽喉据此拒绝后续改动。没有第二份拷贝
（曾经的 deliverables/ 与 org/_archive/ 抄件已删：目录页、收尾清单、发布链全读账本）。
按 `artifact_policy`，chunk_then_drop 类型与 pre_registration 冻结时自动登记成 KB chunk。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool
from shared.lib.artifact_text import artifact_text


# ── v3.4 prereg 可行性门禁 helpers ───────────────────────────────────────────

# 常见外部模型名（小写、长名在前防子串误报顺序问题）。只用于"点名了但平台没有"
# 的检测；平台若真配置了同名 provider，自然会被 available 集覆盖。
#: 外部（别家）模型族。这份**手写名单只负责别人家的模型** —— 平台自己那些族
#: 由 `_registered_family_variants` 从已登记 providers 现推，不用手抄。
_KNOWN_EXTERNAL_MODELS = (
    "gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4", "gpt-5", "o1-mini", "o1", "o3",
    "claude", "gemini", "grok", "llama", "mistral", "mixtral", "qwen",
)

#: 形如 `deepseek-v4-pro` / `kimi-k3` 的型号写法：族名 + 至少一个 `-` 段。
_MODEL_VARIANT_RE_TMPL = r"\b({family}(?:-[a-z0-9.]+)+)\b"


def _registered_family_variants(content: str) -> set[str]:
    """从**已登记 providers 的族名**出发，扫出正文里同族的其它型号。

    为什么不能只靠 `_KNOWN_EXTERNAL_MODELS`：那是手写名单，只抓有人想到要枚举
    的族。2026-08-22 实测——**平台自己的主力族 `deepseek` 压根不在名单里**，
    于是 prereg 写个 `deepseek-v9-ultra`（不存在的型号）能直接冻结通过，而这道
    闸的职责恰恰是"不得承诺平台没有的资源"。硬编码枚举 = 新东西默认漏过。

    改成从 providers 现推族名：
    - 平台配了 deepseek → 自动覆盖所有 `deepseek-*` 变体
    - 换/加 provider → 自动跟着变，不用回来改名单
    - 族名来自真实配置，不会凭空造出误伤（只在正文真提到该族时才比对）

    只扫「族名 + 版本段」的写法（`deepseek-v4-pro`），不扫裸族名。
    ⚠️ 说清楚：**裸族名不开火，实际是后面 `m not in available` 那步挡住的** ——
    派生族名必然是某个已登记型号的前缀，因而必然是 available 的子串。这里的
    `+` 只是让意图显式，不是那道保护本身；别把功劳记到它头上（变异掉 `+`→`*`
    测试不会红，我试过）。

    ## 已知的误报面，没解决，靠出口兜着

    正文里把某个型号当**前人工作**提到（"文献里用 deepseek-v4-pro 做过"），机械
    上和"我要用它"分不开 —— 那是语义判断，不该硬编码。所以这道闸会误报这一类。
    兜底是报错里给了合法出口（改用登记里那个 / 在 execution_commitment 里写
    substitutes 或 deferred），**误报可解除就不会变成死锁**；真正致命的是"拒了
    又不给路"，那才是这个平台反复栽的形。
    """

    try:
        from core.llm_providers import list_providers

        providers = list_providers()
    except Exception:
        return set()

    found: set[str] = set()
    for spec in providers:
        model = (getattr(spec, "model", "") or "").lower()
        family = model.split("-", 1)[0]
        if len(family) < 3:  # 太短的前缀会满篇乱撞
            continue
        found.update(
            re.findall(_MODEL_VARIANT_RE_TMPL.format(family=re.escape(family)),
                       content))
    return found

# 人工标注承诺的信号（保守：明确的"标注员/human annotator"表述才算）
_HUMAN_ANNOTATION_RE = re.compile(
    r"(human annotat|independent annotators|annotators are|名标注员|人工标注员)",
    re.IGNORECASE)


def _prereg_capital_basis_violations(state, record: dict) -> list[str]:
    """v3.5：预注册必须声明它基于哪些承重结论（或明确写"查过、无相关"）。

    两次 E2E 的 KB 都零复用 —— 因为唯一强制消费点是 writing 的引用 QC，于是
    KB 只在"引用是义务"的地方被读，在"知识本该有用"的设计阶段被完全忽略。
    这里把消费提到**设计时**并变成义务：metadata.capital_basis 必须是
      - 已存在的承重 claim id 列表，或
      - 字符串 "none_found"（显式声明检索过但无可用资本）。
    承重层为空时自动豁免（新项目还没有资本，不该被卡住）。
    """
    meta = record.get("metadata") or {}
    basis = meta.get("capital_basis")
    try:
        capital = [c for c in state.list_kb("claims")
                   if isinstance(c.get("card_draft"), dict) and c.get("card_draft")
                   and c.get("status") not in ("superseded", "refuted")]
    except Exception:
        return []
    if not capital:
        return []          # 无资本可用 → 不设门槛
    if basis == "none_found" or basis == ["none_found"]:
        return []
    if isinstance(basis, list) and basis:
        known = {c.get("id") for c in capital}
        bad = [b for b in basis if b not in known]
        if bad:
            return [f"capital_basis 引用了不存在/非承重的 claim：{bad}"]
        return []
    listing = ", ".join(str(c.get("id")) for c in capital[:12])
    return [
        f"metadata.capital_basis 未声明。本项目已有 {len(capital)} 条承重结论，"
        f"预注册必须说明设计依据了其中哪些（列 claim id），或显式写 "
        f"\"none_found\" 表示检索过但无相关可用资本。当前承重层：{listing}"
    ]


def _prereg_feasibility_violations(record: dict) -> list[str]:
    """机械核对 prereg 承诺 vs 平台资源登记。返回违规清单（空=可冻结）。

    覆盖逃逸口：metadata.execution_commitment 里 substitutes（声明替代）或
    deferred（声明推迟）提到该资源名 → 视为已诚实处理，放行。
    """
    content = artifact_text(record).lower()
    meta = record.get("metadata") or {}
    commitment = meta.get("execution_commitment") or {}
    declared = json.dumps(commitment, ensure_ascii=False).lower()

    try:
        from core.llm_providers import list_providers
        available = " ".join(sorted({
            part for p in list_providers() for part in (p.name, p.model) if part
        })).lower()
    except Exception:
        available = ""
    # 算力授权允许本地部署时，开源权重模型族可承诺（钥匙在手，部署是登记内
    # 动作）。闭源 API 模型不受此豁免 —— 有 GPU 也变不出闭源权重。
    # 与 system prompt 注入读**同一份** snapshot（core/capabilities.py）。
    try:
        from core.capabilities import OPEN_WEIGHT_FAMILIES, allows_local_deployment
        _local_deploy_ok = allows_local_deployment()
    except Exception:
        OPEN_WEIGHT_FAMILIES, _local_deploy_ok = (), False

    violations: list[str] = []
    # ⚠️ `_KNOWN_EXTERNAL_MODELS` 是**刻意按长到短**排的（gpt-4o-mini → gpt-4o
    # → gpt-4），这样报错点到的是最具体的那个型号。别拿 sorted() 重排 —— 一排
    # 就变成字典序，`gpt-4o` 会被 `gpt-4` 抢先报出来。派生族名追加在后面。
    _derived = sorted(_registered_family_variants(content)
                      - set(_KNOWN_EXTERNAL_MODELS))
    for m in (*_KNOWN_EXTERNAL_MODELS, *_derived):
        if m in content and m not in available and m not in declared:
            if _local_deploy_ok and any(m.startswith(f) or f in m
                                        for f in OPEN_WEIGHT_FAMILIES):
                continue
            violations.append(
                f"内容点名模型 {m!r}，但平台 providers 未登记该模型，且 "
                f"execution_commitment 未声明 substitutes/deferred 覆盖它。"
                f"平台当前登记的是：{available.strip() or '（未配置）'}。"
                f"改成登记里的那个，或在 metadata.execution_commitment 里写 "
                f"substitutes/deferred 说明怎么替代"
            )
    if (_HUMAN_ANNOTATION_RE.search(content)
            and not os.getenv("HARNESS_HUMAN_ANNOTATION_CHANNEL", "").strip()
            and "annotat" not in declared and "标注" not in declared):
        violations.append(
            "内容承诺人工标注（human annotators），但平台未配置人工标注通道"
            "（HARNESS_HUMAN_ANNOTATION_CHANNEL 为空），且 execution_commitment "
            "未声明替代（如 llm_annotation）或 deferred"
        )
    return violations


_RUN_ROLES = ("primary", "secondary")


def _run_role_declaration_violations(
    record: dict,
    run_role: str | None,
    expected_params: Any = None,
) -> tuple[list[str], dict]:
    """prereg 必须显式声明本课题的实验运行算不算正式证据。

    背景（E2E v14 实测）：Analysis 冻结了 prereg，metadata 里只有 capital_basis /
    frozen / frozen_at / freeze_reason。experiment 那边的规矩是"未声明 → 按
    secondary 记录"（`nodes/experiment/harness.yaml`），于是**这个课题此后每一次
    实验都不是正式证据**，verdict 恒为 inconclusive。等发现时 prereg 已冻结，
    不可逆——7.4M tokens 撞在一扇焊死的门上。

    规则本身是对的（防挑数据：不能"跑出了数字"就算证据，必须事前说死哪次算）。
    坏在没人把这条要求送到唯一能满足它的那一方，而且失败发生在不可逆之后。
    所以门设在冻结这一刻，报错必须给出合法值。

    2026-09-21（#979）：这道门原先还要求同时申报 `analysis_eligible`，理由写的是
    "与消费方 `nodes/experiment/tools/preflight.py:141` 同源"。**那个消费方已经
    不在了**——experiment 2026-09-11 把 `analysis_eligible` 从目标契约整个删除
    （`nodes/experiment/AGENTS.md:143`），所有门禁改读自己真正的判据，正式证据资格
    现在由 `requires_hypothesis_verdict` 从 execution_mode + run_role 现算。一道
    为不存在的下游强制的申报，模型每次冻结都要答一遍，而答案没有任何人读——
    它和"问过了、有人在看"长得一模一样。所以收掉字段，留下这道门：门真正管住的
    是 `run_role` 和 `expected_params`，这两样至今仍被 experiment 读。

    返回 (violations, 要写进 metadata 的声明)。
    """
    existing = dict(record.get("metadata") or {})
    role = run_role if run_role is not None else existing.get("run_role")

    if role is None:
        return ([
            "缺 run_role 声明。冻结不可逆，而 experiment 对未声明的 prereg 一律按 "
            "secondary 记录 —— 冻结后这个课题的**任何实验都不能作为正式证据**，"
            "verdict 恒为 inconclusive，且没有补救路径。\n"
            "  正式实验（绝大多数情况）：run_role=\"primary\"（同时必须给 expected_params）\n"
            "  探索/调试（结果明确不进正式统计）：run_role=\"secondary\""
        ], {})

    normalized = str(role or "").strip().lower()
    if normalized not in _RUN_ROLES:
        return ([
            f"run_role={role!r} 不是合法值。合法值只有：{', '.join(_RUN_ROLES)}。"
        ], {})

    is_primary = normalized == "primary"

    declaration = {"run_role": normalized}

    # 声明"算正式证据"就必须同时交出要比对的参数集。
    #
    # 这是 run_role 那条的**同一个坑再往下一格**：experiment 的
    # scientific_execution_preflight 对 primary run 要求
    # `frozen pre_registration.metadata.expected_params`，缺了直接
    # `safe_bash.py:3108` 硬拒（"禁止执行，需 formal amendment"）。而
    # `nodes/experiment/hooks.py:1989` 的注释白纸黑字写着"目前尚无节点写它"。
    #
    # 于是修好 run_role 之后会出现更糟的局面：以前是"跑得动但不算数"，变成
    # "根本跑不动"。补这一格才算真修完 —— 判据是"修完之后一批待办自己消失"。
    #
    # 只对 primary 要求：探索性 prereg（secondary）那边 preflight 本来就跳过。
    if is_primary:
        params = expected_params
        if params is None:
            params = (record.get("metadata") or {}).get("expected_params")
        if not isinstance(params, dict) or not params:
            return ([
                "声明了 run_role=\"primary\" 却没给 expected_params。"
                "experiment 会拿本次 simulation 的 execution_params **逐字段**比对"
                "冻结 prereg 里的 expected_params，缺了就直接拒绝执行"
                "（不是记一笔，是不让跑）——而冻结不可逆。\n"
                "  expected_params 是你在此刻承诺的结构化参数集，例如：\n"
                "    {\"n_particles\": 4000, \"timestep\": 0.005,\n"
                "     \"cooling_rates\": [5e-5, 5e-4, 1e-3, 5e-3],\n"
                "     \"T_start\": 2.0, \"T_end\": 0.01, \"ensemble\": \"NPT\"}\n"
                "  只写你真要机械比对的量；写进去就是承诺，跑的时候对不上要走"
                "正式 amendment。确实不做正式实验就声明 "
                "run_role=\"secondary\"。"
            ], {})
        declaration["expected_params"] = params

    return ([], declaration)


# ── 冻结门注册表：门禁跟类型走，不跟工具名走 ─────────────────────────────
#
# 此前每个要带门禁的类型都各包一个 freeze_* 工具（experiment 三个、observation
# 一个、外加 freeze_and_register 二合一），同一个动作六个名字。门禁是**类型**
# 的性质：owner 对自己的类型声明冻结门，唯一的 freeze_artifact 冻结前执行它。
# 新类型加门 = 声明一次，不再造工具。
#
# 门函数契约：fn(state, artifact_id, record) -> dict
#   {} / {"failures": {}}                       → 放行
#   {"failures": {名: 原因}, "hint": 出口说明}   → 拒绝，原样返回给模型
FREEZE_GATES: dict[str, Any] = {}

#: 门按字段名拒绝时，那些字段的声明：{artifact_type: {字段: 要求}}。
#:
#: 为什么必须跟着门一起注册（2026-08-21）：门原来长在 `freeze_raw_results` /
#: `freeze_clean_results` 这些工具上，声明就挂在那些工具的 `content_contract`，
#: register_tool 自动渲染进模型看到的 description。门改成按类型注册之后工具被
#: 删了 —— 门照旧按 `replay_manifest` 拒绝，而模型再也**看不到**这个要求。
#: 契约和拒绝是同一件事的两面，谁搬家另一个就得跟着搬。
FREEZE_GATE_CONTRACTS: dict[str, dict[str, str]] = {}


def register_freeze_gate(artifact_type: str, fn, content_contract: dict[str, str] | None = None) -> None:
    """给一个 artifact 类型声明冻结门（可带内容契约）。

    带了 content_contract 就同时渲染进唯一的 `freeze_artifact` 说明里 ——
    模型调的是那个工具，契约就得送到那个工具的门口。
    """
    FREEZE_GATES[artifact_type] = fn
    if content_contract:
        FREEZE_GATE_CONTRACTS[artifact_type] = dict(content_contract)
        _publish_contract("freeze_artifact", artifact_type, content_contract)


# ── 写入面的门：与冻结门对称 ─────────────────────────────────────────────────
#
# 为什么必须有这一面（2026-08-23 实测事故）：derivation_log 的结构纪律
# （验证章只认工具落的、每步要么有章要么显式挂未验）全部注册在冻结门上，而
# **冻结是模型自愿调用的一个动作**。一次真跑里模型调了 9 次 check_step 真验了，
# 却把 step.verification 写成字符串 `"verified"`，然后 save_artifact 两次、
# 从头到尾没调 freeze_artifact —— 于是那道写得很好的门**一次都没跑**，
# 产物带着伪造的验证章交付，run 判 completed。
#
# 这是「机制存在但没接到路径」的纯粹形态：机制不缺，缺的是它站在模型的必经之路上。
# 闸放哪层按**对手是谁**推：对手是"不冻结就交付"，那闸就不能只长在冻结上。
#
# 分工（两道门查的不是同一件事，谁也替不了谁）：
#   · 写入面：**这个类型的产物长什么样** —— 结构纪律，与模式无关，每次写都查
#   · 冻结面：**这份承诺兑现了吗** —— 模式契约（闭合项、审计裁决），冻结时查
#
# 两边共用同一批判据函数（`_audit_steps` 那些），不是各抄一份 ——
# 「一个问题一个真相源」：有几份抄件就有几个会各自演化的答案。
#
# 门函数契约（与冻结门同形，只是产物还没落盘，所以传 draft 不传 record）：
#   fn(state, draft) -> dict，draft = {type, name, metadata, content}
#   {} / {"failures": {}}                       → 放行
#   {"failures": {名: 原因}, "hint": 出口说明}   → 拒绝，原样返回给模型
SAVE_GATES: dict[str, Any] = {}

#: 与 FREEZE_GATE_CONTRACTS 同理：契约和拒绝是同一件事的两面。
SAVE_GATE_CONTRACTS: dict[str, dict[str, str]] = {}


def register_save_gate(artifact_type: str, fn, content_contract: dict[str, str] | None = None) -> None:
    """给一个 artifact 类型声明写入门（可带内容契约）。

    契约渲染进唯一的 `save_artifact` 说明 —— 模型调的是那个工具，
    契约就得送到那个工具的门口，而不是只在被拒时才出现。
    """
    SAVE_GATES[artifact_type] = fn
    if content_contract:
        SAVE_GATE_CONTRACTS[artifact_type] = dict(content_contract)
        _publish_contract("save_artifact", artifact_type, content_contract)


async def run_save_gate(state, draft: dict) -> dict | None:
    """跑该类型的写入门。放行返回 None，拒绝返回给模型的错误结果。

    与 `_run_freeze_gate` 同形：拒绝是**驳回**不是崩溃，理由原样送到模型手上。
    """
    import inspect as _inspect

    gate = SAVE_GATES.get(str((draft or {}).get("type") or ""))
    if gate is None:
        return None
    result = gate(state, draft)
    if _inspect.isawaitable(result):
        result = await result
    failures = (result or {}).get("failures") or {}
    if not failures:
        return None
    out = {k: v for k, v in (result or {}).items() if k != "failures"}
    out.update({
        "status": "error",
        "error": (f"⛔ {draft.get('type')} 没通过写入门："
                  + "；".join(f"{k}: {v}" for k, v in failures.items())),
        "failed_checks": sorted(failures),
        "reasons": failures,
        "hint": (result or {}).get("hint", ""),
    })
    return out


def _publish_contract(tool_name: str, artifact_type: str,
                      contract: dict[str, str]) -> None:
    """把该类型的契约拼进已注册工具的说明（幂等）。

    门是在节点 tools import 时注册的，那时目标工具早就注册好了 ——
    所以这里改的是 registry 里那份活的 definition，而不是 import 期的字面量。
    """
    from dataclasses import replace as _replace

    from core.tool_registry import _REGISTRY, _render_content_contract

    tool = _REGISTRY.tools.get(tool_name)
    if tool is None:
        return
    rendered = f"\n\n**{artifact_type}**:" + _render_content_contract(contract)
    if rendered in (tool.description or ""):
        return
    _REGISTRY.tools[tool_name] = _replace(
        tool, description=(tool.description or "") + rendered
    )


async def _run_freeze_gate(state, artifact_id: str, record: dict) -> dict | None:
    import inspect as _inspect

    gate = FREEZE_GATES.get(str(record.get("type") or ""))
    if gate is None:
        return None
    result = gate(state, artifact_id, record)
    if _inspect.isawaitable(result):
        result = await result
    failures = (result or {}).get("failures") or {}
    if not failures:
        return None
    out = {k: v for k, v in (result or {}).items() if k not in ("failures",)}
    out.update({
        "status": "error",
        "error": f"{record.get('type')} cannot be frozen before its gates pass",
        "failed_checks": sorted(failures),
        "reasons": failures,
        "hint": (result or {}).get("hint", ""),
    })
    return out


#: 冻结后自动登记成 KB chunk 的产物类型。C 类（chunk_then_drop）由策略表现算，
#: 外加 pre_registration —— 它是 permanent/tamper_evident 不归 C 类，但
#: hypothesis 立 claim 时同样按 chunk_id 引用它。
def _should_auto_chunk(artifact_type: str) -> bool:
    if not artifact_type:
        return False
    if artifact_type == "pre_registration":
        return True
    from shared.lib.artifact_policy import is_chunk_then_drop

    return is_chunk_then_drop(artifact_type)


def _closure_debt_note(state: Any, record: dict) -> str | None:
    """冻结一份带兑现账的产物时，把还欠着的闭合条件当场报回去。

    ## 为什么是"冻结那一刻"

    `render_commitment_brief` 已经把逐条 ⬜/✅ 清单摆给模型看了 —— **每个节点每一
    轮**，连字段格式都写了。2026-09-07 真机：experiment 在 160 多轮里看着那 8 个
    ⬜，把 experiment_log 冻上，收工。硬闸在三次派工之后的 writing 输入门上，于是
    writing 白跑一轮（还排版编译出一份「我为什么写不出论文」的 PDF）→ 决策卡
    REDIRECT → experiment 回去补账 → 再派一次 writing。

    所以缺的不是"告诉过没有"，是**告诉的时机**：常驻简报是背景，模型可以一直往后
    放；而冻结这份产物就是在宣布"这就是我这一轮的证据"，欠账在那一刻才真正变成
    "现在就该处理的事"。同一条原理的人那半边是 PR#843（把闭合账亮到决策卡上）。

    **不拒绝。** 冻结照常成功 —— 这一层早就定过调子（顺序闸降格：冻结件机械写入
    未闭合状态即账真，拒绝只制造第二份判决）。这里只把事实送到手边。

    全程吞异常：冻结是"从此改不了"的那一步，绝不能因为一个诊断算不出来而失败。
    """
    try:
        from shared.lib.artifact_policy import carries_discharge_ledger

        if not carries_discharge_ledger(str(record.get("type") or "")):
            return None
        from core.prereg_commitments import closure_tally

        tally = closure_tally(state)
    except Exception:
        return None
    if tally is None or not tally.total or not tally.open_total:
        return None

    lines = [
        f"⚠️ 冻结这份产物 = 宣布「这就是本轮证据」，但预注册闭合账还欠 "
        f"{tally.open_total}/{tally.total} 条："
    ]
    for item in tally.open_items[:8]:
        lines.append(f"  ⬜ {item}")
    if tally.open_total > min(len(tally.open_items), 8):
        lines.append(f"  … 另有 {tally.open_total - min(len(tally.open_items), 8)} 条")
    lines.append(
        "兑现记录写在**本产物的 metadata**：数值条 `measured_metrics`、陈述条 "
        "`closure_discharges`（discharged 必须挂 evidence）。写在正文散文里不算 —— "
        "账本只读结构化字段。"
    )
    lines.append(
        "现在补最省事：`amend_artifact` 补上字段再冻。等到 writing 那步才发现，"
        "要走「writing 被拦 → 决策卡 REDIRECT → 本节点重跑补账 → writing 再跑」"
        "一整圈。真做不到的条目也要显式申报（`not_run` / `not_applicable` / "
        "数值条的定性降级），别让它停在「未申报」。"
    )
    return "\n".join(lines)


async def _freeze_artifact(
    state: State,
    artifact_id: str,
    reason: str = "",
    run_role: str | None = None,
    expected_params: Any = None,
    **_: Any,
) -> dict:
    # Freeze is irreversible and may register a manuscript into the KB.
    # An artifact whose own post-producing review flow still awaits curator /
    # decision **is frozen anyway** — but the frozen record carries the open
    # review state (`metadata.review_state="open"` + the pending flows), so a
    # reviewer REVISE can no longer be laundered into a "clean" frozen
    # manuscript: the account says the review was open when it was signed.
    # （判决拆除·第三波：顺序闸降格——冻结件机械写入未闭合 review_state 即账真，
    # amendment 链仍在；拒绝只制造第二份判决。）
    open_review_flows = [{
        "producing_node": entry.get("producing_node"),
        "producing_run_id": entry.get("producing_run_id"),
        "review_state": entry.get("review_state"),
        "decision_state": entry.get("decision_state"),
        "recommended_action": entry.get("decision_recommended_action"),
    } for entry in (state.hook_state.get("pending_post_node_flow") or [])
        if isinstance(entry, dict)
        and artifact_id in (entry.get("artifact_ids") or [])]

    # 用跨节点查找，不要只看自己目录。
    #
    # 预注册是 **Analysis/hypothesis 节点**的产物（落在 `plan/`），
    # 而调 freeze 的往往是 orchestrator —— 只看本节点目录就永远
    # 找不到它。E2E v24 实测活死锁：experiment 拒绝启动（"pre_registration 未
    # freeze"），orchestrator 去 freeze 却得到"找不到 artifact"，于是反复重派，
    # 两边都在等对方，而报错互相矛盾。
    #
    # 这与 v14/v15/v16 连栽三轮的那个 bug 是**同一个**：当时修的是
    # `load_run_contract`（它也曾只 glob 自己目录），但这一处没跟着改 ——
    # 同一个 bug 的另一份拷贝。`find_artifact_path` 是 v2.1 规定的跨节点读入口，
    # 且与 `read_artifact` 同一套搜索顺序（"读到哪份"和"路径指哪份"必须同解）。
    path = state.find_artifact_path(artifact_id)
    # ── 冻结是**所有者对自己承诺的签字**，不能代签 ─────────────────────────
    #
    # freeze 写的是账本（core/ledger）。v2.1 的规矩是"读跨节点、写不跨节点"，
    # 冻结是对**自己**承诺的签字。
    #
    # 这不只是权限问题，语义上也该如此：预注册是作者在开工前锁定意图，冻结
    # = 他签字承诺。调度器替他签，等于经理替下属签承诺书 —— 之后再问"这条
    # 判据是谁承诺的"，答案就糊了。hypothesis 自己的 harness（第 9h 步）本来
    # 就写着"审计全过之后才 freeze"。
    #
    # 以前这里不拦，让写下去撞边界守卫，被回滚后报"Node '_orchestrator' may
    # only write .research/orchestration" —— 技术上没错，但**没说该找谁**，
    # 于是调用方只能重试（E2E v25 实测撞了 3 次）。提前拦住，并指名下一步。
    if path is not None:
        worktree = getattr(state, "project_worktree", None)
        if worktree is not None:
            from core.project_workspace import owning_node_for_path

            owner = owning_node_for_path(worktree, path)
            here = str(getattr(state, "node_type", "") or "").lstrip("_")
            if owner and owner != here:
                return {
                    "status": "error",
                    "error": (
                        f"{artifact_id!r} 是 {owner} 节点的产物，只有它能冻结自己的"
                        f"承诺（冻结 = 作者签字，不能代签）。\n"
                        f"正确做法：run_node(node_type={owner!r}, node_inputs={{...}})，"
                        f"在 node_inputs 里说明要冻结哪一份、以及 run_role /"
                        f" expected_params 分别取什么。"
                    ),
                    "owner_node": owner,
                }
    if path is None or not path.exists():
        return {
            "status": "error",
            "error": (
                f"找不到 artifact {artifact_id!r}。已在本 Session 的全部节点目录里"
                f"找过；用 list_artifacts 确认 id 拼写，或先 save_artifact 再 freeze。"
            ),
        }

    record = state.read_artifact(artifact_id)
    if not isinstance(record, dict):
        return {"status": "error", "error": f"读取 artifact {artifact_id!r} 失败"}

    # 类型声明的冻结门先跑（experiment/observation 的证据门都在这层执行）。
    gate_block = await _run_freeze_gate(state, artifact_id, record)
    if gate_block:
        return gate_block

    # ── v3.4 prereg 可行性探测（E2E#1/#2 同一剧本连演两次）────────────────────
    # 两次 E2E 的 prereg 都承诺了平台没有的资源（GPT-4o / GPT-4o-mini / 3 名人工
    # 标注员），且全程零承认。探测是真的（资源登记 / 闭合条件 / capital_basis /
    # 数值自洽 / run_role 声明），但它们预测的是**未来的协议偏离**，而申报式
    # 偏离本就是合法路径（execution_commitment / amendment）。所以冻结照做，
    # 探测结果**如实写进冻结件** `metadata.freeze_warnings`（run_role 子句缺席
    # 另标 `run_role_undeclared`：experiment 对未声明的 prereg 按 secondary 记，
    # 这里不替作者编一份声明）。HARNESS_PREREG_GATE=0 跳过探测（应急口）。
    # 非 prereg、或跳过探测时都走这条默认值 —— 否则下面 metadata.update
    # 会 NameError（探测只在 prereg 分支里算这个变量）。
    run_role_declaration: dict = {}
    freeze_warnings: list[str] = []
    if (record.get("type") == "pre_registration"
            and os.getenv("HARNESS_PREREG_GATE", "1") != "0"):
        violations = _prereg_feasibility_violations(record)
        # v3.8 承诺对象化：判据解析不出来 = 这条假设根本没被对象化，后面的判定
        # 门禁对它形同虚设。fail-loud —— 宁可拒绝冻结，也不留看不见的洞。
        # ── v0.5 研究问题闸 ────────────────────────────────────────────
        # 公理二：开工前必须承诺"怎样算答完"，而且这份承诺要冻结。
        # 这里守的就是这一条 —— 但**不再要求那份承诺必须长成数值三元组**。
        #
        # 旧版把"可核对"实现成了"必须有 metric/comparison/threshold"，后果是
        # ① 一条散文形式的假设根本冻不进来；② 没有命题的研究（探索/表征/方法/
        # 解释/复现/推导）压根不进承诺账，整套门禁对它不生效。于是历史解释类
        # 课题为了过门编出"差 30 年算削弱、差 20 年算证伪"。
        # 现在：闭合条件可以是数值条，也可以是陈述条；缺的是**闭合条件本身**才拦。
        try:
            from core.prereg_commitments import (
                closure_shape_hint,
                parse_questions,
                questions_without_closure,
                sections_without_parsable_criteria,
            )
            _content = artifact_text(record)
            _questions = parse_questions(_content)
            if not _questions:
                violations.append(
                    "预注册里没有任何研究问题。协议必须至少声明一个 —— "
                    "`## Research Questions` 段下写 `### Q1: <要回答什么>`，"
                    "每个问题给 `output_kind`、【判断某句话对不对时】`proposition`、"
                    "以及一个 ```yaml``` 闭合条件块（`- metric/comparison/threshold` "
                    "或 `- statement: <可观察的条件>`）。"
                    "没有问题的协议关不掉，也没人能说它被糊弄了。")
            else:
                _no_closure = questions_without_closure(_content)
                if _no_closure:
                    violations.append(
                        f"研究问题 {', '.join(_no_closure)} 没有任何闭合条件。"
                        "每个问题都必须承诺「怎样算答完」，冻结后不可改。\n"
                        + closure_shape_hint())
                _bad = sections_without_parsable_criteria(_content)
                if _bad:
                    violations.append(
                        f"研究问题 {', '.join(_bad)} 声称写了闭合条件/Falsification "
                        "Criteria，但框架一条都解析不出来 —— 那这个问题没被对象化，"
                        "关闭门禁对它形同虚设。\n" + closure_shape_hint())
        except Exception:
            pass
        violations += _prereg_capital_basis_violations(state, record)
        # 数值自洽：协议里声明的量必须跟它自己写的定义公式对得上。
        # E2E v18：表里 γ=10⁻² 配 n_steps=90，而同一张表上方就写着
        # γ = 0.9/(n_steps×0.001) —— 反推应是 90,000，整列差 1000 倍，
        # run_experiments.py 直接用了错的那列。冻结之后不可逆。
        try:
            from core.prereg_numeric_consistency import formula_violations
            for item in formula_violations(artifact_text(record)):
                violations.append(f"数值与正文公式不自洽：{item}")
        except Exception:
            pass
        role_violations, run_role_declaration = _run_role_declaration_violations(
            record, run_role, expected_params)
        freeze_warnings = list(violations) + list(role_violations)
        run_role_undeclared = bool(role_violations)
    else:
        run_role_undeclared = False

    metadata = dict(record.get("metadata") or {})
    if metadata.get("frozen"):
        return {"status": "success",
                "artifact_id": artifact_id,
                "already_frozen": True,
                "frozen_at": metadata.get("frozen_at")}

    # 运行角色声明与 frozen 同一次写入：experiment 侧读的就是**冻结后**的
    # prereg metadata，晚写一步等于没写。冻结是账本上的一行（core/ledger），
    # 文件一个字节不动 —— 文件哈希 = 正文哈希 = 账本钉死的那个哈希。
    patch: dict = dict(run_role_declaration)
    if freeze_warnings:
        patch["freeze_warnings"] = freeze_warnings
    if run_role_undeclared:
        patch["run_role_undeclared"] = True
    if open_review_flows:
        patch["review_state"] = "open"
        patch["pending_review_flows"] = open_review_flows
    if reason:
        patch["freeze_reason"] = reason
    record = state.mark_frozen(artifact_id, patch)
    metadata = dict(record.get("metadata") or {})
    try:
        state.append_transcript("frozen_path_registered", artifact_id=artifact_id,
                                path=str(path), sha256=record.get("content_hash"),
                                version=record.get("version"))
    except OSError:
        pass    # transcript 是观测不是机制；账本行上面已经落盘，才是执法依据
    if freeze_warnings or open_review_flows:
        with contextlib.suppress(Exception):
            state.append_transcript(
                "artifact_frozen_with_warnings", artifact_id=artifact_id,
                freeze_warnings=freeze_warnings,
                run_role_undeclared=run_role_undeclared,
                pending_review_flows=open_review_flows,
            )

    # 冻结即登记：账本那一行就是平台 commit 咽喉（checkpoint_session_workspace）
    # 据以拒绝后续改动的依据。没有第二份拷贝（曾经的 deliverables/ 与
    # org/_archive/ 抄件已删：目录页、收尾清单、发布链全读账本）。
    from core.ledger import record_version as _record_version

    result: dict = {
        "status": "success",
        "artifact_id": artifact_id,
        "frozen_at": metadata["frozen_at"],
        "version": _record_version(record),
    }
    debt = _closure_debt_note(state, record)
    if debt:
        result["closure_debt"] = debt
        result["note"] = (result.get("note", "") + "\n\n" + debt).strip()
    if freeze_warnings:
        result["freeze_warnings"] = freeze_warnings
        result["run_role_undeclared"] = run_role_undeclared
        result["note"] = (
            "已冻结，但以下探测结果**如实写进了冻结件 metadata.freeze_warnings**"
            "（终审与 experiment 都看得见）：\n- " + "\n- ".join(freeze_warnings)
            + "\n\n承诺了平台登记里没有的资源：写进 metadata.execution_commitment"
            "（substitutes: {\"<资源名>\": \"<替代>\"} / deferred: [...]）或走正式 "
            "amendment 改写协议。"
            + ("\n缺 run_role / expected_params 声明：experiment 对未声明的 prereg "
               "一律按 secondary 记录——本课题的实验将不作为正式证据，"
               "除非走 amendment 补上声明。"
               if run_role_undeclared else ""))
    if open_review_flows:
        result["review_state"] = "open"
        result["pending_flows"] = open_review_flows
        result["note"] = (result.get("note", "") + (
            "\n该产物的 reviewer→curator→decision flow 尚未闭合，冻结件已如实记 "
            "metadata.review_state='open'（连同待闭合的 flow）。这不是 reviewer 放行："
            "先恢复/重新呈递正式 decision package，并按 reviewer 决议处理。")).strip()

    # ── 冻结即登记：把 chunk_id 交出去，别让模型去找一个不存在的工具 ────────
    #
    # 下游有两处**必填** chunk_id：`create_experiment(frozen_log_chunk_id=…)`
    # 和 hypothesis 立 claim 时的 prereg 锚。两处的参数描述都写着"先 freeze，
    # 再 kb_register_artifact_as_chunk"——而那个工具在 KB 15→13 精简时已经
    # 下架，只剩内部函数。于是这条链在注册面上**没有任何合法产生路径**：
    # 2026-08-21 实测，experiment 连挂两次，模型在两个 run 里提这个幽灵名
    # 147 次和 48 次，最后只能报 blocker 收尾。
    #
    # 而 `chunk_then_drop` 这个策略名本来就承诺了"已 chunk 进 KB 后原文件可删"
    # —— 承诺一直在，只是没有实现。这里把它补上。
    #
    # 判据取产物策略（扫声明，不写名单），额外加 pre_registration：它是
    # permanent/tamper_evident，不归 C 类，但下游同样按 chunk_id 引用它。
    # 整段都在 try 里：freeze 此刻**已经落盘且不可逆**，登记是附加动作，它没有
    # 任何资格把 freeze 判失败。第一版把 except 只套在登记调用上，结果错误处理
    # 里那句 append_transcript 自己抛了（测试 state 没有 run 目录），异常照样
    # 穿透 —— 错误路径本身也要能失败。
    if _should_auto_chunk(record.get("type", "")):
        try:
            from shared.tools.library.kb import _kb_register_artifact_as_chunk

            # created_by_role 必须是 `CREATED_BY_ROLES` 里的值。第一版我按语义
            # 顺手编了个 "freeze_auto" —— schema 逐字校验，于是自动登记**每次
            # 都失败**，chunk_id 一次都没交出去过。单测只验了类型路由，从没真
            # 调过这个函数；是真 E2E 里那条 chunk_registration_error 抓到的。
            registration = await _kb_register_artifact_as_chunk(
                state, artifact_id=artifact_id, created_by_role="agent_auto",
            )
            if registration.get("status") == "success":
                result["chunk_id"] = registration["chunk_id"]
                result["chunk_scope"] = registration.get("scope")
                if registration.get("superseded_chunk_ids"):
                    result["superseded_chunk_ids"] = registration["superseded_chunk_ids"]
            else:
                # 不吞：模型此刻正等着这个 chunk_id 去 create_experiment。
                result["chunk_registration_error"] = registration.get("error")
        except Exception as e:                          # noqa: BLE001 —— 见上
            result["chunk_registration_error"] = str(e)
        if "chunk_registration_error" in result:
            with contextlib.suppress(Exception):
                state.append_transcript(
                    "freeze_auto_chunk_failed",
                    artifact_id=artifact_id,
                    error=result["chunk_registration_error"])

    await _file_sediment_findings_as_proposals(state, record, result)
    return result


#: 沉淀段里"这是一条发现"的写法。节点模板用 `- **claim**: …`，模型也会写成
#: `- claim: …`。两种都认；认不出的行不算，宁可少报也不误报。
_SEDIMENT_HEADING = re.compile(r"^#{1,6}\s*Sediment\b", re.MULTILINE | re.IGNORECASE)
_SEDIMENT_CLAIM_LINE = re.compile(
    r"^[ \t]*[-*][ \t]*\**[ \t]*claim[ \t]*\**[ \t]*[:：][ \t]*(?P<text>.+)$",
    re.MULTILINE | re.IGNORECASE)


async def _file_sediment_findings_as_proposals(
    state: State, record: dict, result: dict,
) -> None:
    """把沉淀段的发现机械搬运进 proposal 收件箱 —— 立不立 claim 归 curator 判。

    ## 为什么要有这条管线（2026-08-22 验尸 + wangd 拍板）

    一个跑了 6.5 小时、Q1-Q4 全部裁决完成的课题，`kb_claims.jsonl` **文件都不
    存在** —— 全程 `create_claim` 调用次数为 0。科学发现确实产出了，但以散文
    形式躺在 experiment_log 的 `## Sediment（…供 curator）` 段里。curator 三次
    dreaming 读了 112/102/144 次文件、烧掉 48.9M token，一条也没搬 —— 不是它
    失职：它的扫描工具扫的是 KB 里**已有的**实体（过期 claim、晋升候选），
    产物散文根本不在它的视野里；而 proposal 类型表当时也只有"对既有记录的
    动作"，结构上装不下"新发现"。发现就这样停在了没人负责的缝隙里，六小时后
    以「论文引用了不存在的 claim」的形式爆掉。

    ## 分工（wangd 定的，第一性原理）

      产出节点：只把发现写清楚（Sediment 段）。**不要求**它们精通 KB schema —
        claim_text 必填、claim_type 五选一、concept_ids 或 orphan_reason……
        这套契约灌不进生产方的 prompt，四次实测全是这么误伤的。
      框架（这里）：freeze 时把每条发现**机械**搬进 proposal 收件箱。零判断、
        零模型参与；target 锚在刚登记的 frozen chunk 上，证据链天然成立。
      curator：唯一的 KB claim 写入口。审 proposal（去重、定 claim_type、挂
        concept、拒收噪声），加上它自己的扫描 —— 语义判断归它。

    ## 失败语义

    freeze 已落盘且不可逆：搬运失败**不许**影响 freeze（连错误处理路径也要能
    失败 —— 本文件上一段的教训）。搬运不做判决：立不立、怎么立，全归 curator。
    """
    try:
        content = record.get("content")
        if not isinstance(content, str) or not _SEDIMENT_HEADING.search(content):
            return
        findings = [m.group("text").strip()
                    for m in _SEDIMENT_CLAIM_LINE.finditer(content)]
        findings = [f for f in findings if f]
        if not findings:
            return

        chunk_id = result.get("chunk_id")
        filed = 0
        if chunk_id:
            from shared.tools.library.proposals import _propose

            artifact_name = str(record.get("name") or "")
            for i, finding in enumerate(findings, start=1):
                try:
                    filed_result = await _propose(
                        state,
                        proposal_type="kb_claim_candidate",
                        target_entity="chunks",
                        target_id=str(chunk_id),
                        proposed_action=f"create_claim: {finding}",
                        reasoning=(
                            f"freeze 机械搬运：{artifact_name} 的 Sediment 段"
                            f"第 {i}/{len(findings)} 条。证据锚点在 target chunk。"
                        ),
                    )
                    if filed_result.get("status") == "success":
                        filed += 1
                except Exception:                        # noqa: BLE001
                    continue

        result["sediment_findings"] = len(findings)
        result["sediment_proposals_filed"] = filed
        if filed < len(findings):
            # 没搬全就说出来 —— 静默少搬比报错难查得多。常见原因：chunk 没
            # 登记成功（chunk_registration_error 已在返回值里）。
            result["sediment_hint"] = (
                f"Sediment 段有 {len(findings)} 条发现，只搬进收件箱 {filed} 条。"
                f"没搬进去的发现 curator 看不见 —— 请对剩余条目手动 propose("
                f"proposal_type='kb_claim_candidate', ...) 或 create_claim。"
            )
        with contextlib.suppress(Exception):
            state.append_transcript(
                "sediment_findings_filed",
                artifact_id=record.get("name"),
                n_findings=len(findings), n_filed=filed)
    except Exception:                                   # noqa: BLE001
        # 搬运坏了不许影响 freeze —— 同上一段的理由。
        return


register_tool(
    ToolDefinition(
        name="freeze_artifact",
        description=(
            "把指定 artifact 标记为不可变（frozen），并按 `artifact_policy` 自动升级。\n\n"
            "**Use when**：\n"
            "  - pre-registration 写完，要冻结实验意图\n"
            "  - 终版 paper / accepted_paper / 调研报告 / 研究计划 等永久交付物\n"
            "  - 不可篡改的实验记录\n\n"
            "**自动行为**：\n"
            "  - 冻结是账本上的一行：文件一个字节不动，账本钉死这一版的 sha256，"
            "之后对这个文件的改动进不了库（要改走 amendment）\n"
            "  - C 类（chunk_then_drop，experiment_log / observation_log 等）与 pre_registration：\n"
            "    **自动登记成 KB chunk**，`chunk_id` 随返回值给你\n\n"
            "**Do NOT use when**：\n"
            "  - artifact 还可能继续改 → 先别 freeze\n\n"
            "**返回**：`{status, artifact_id, frozen_at, version, chunk_id?}`。\n"
            "  `chunk_id` 就是 `create_experiment(frozen_log_chunk_id=…)` 和 hypothesis "
            "立 claim 时要的那个锚 —— 冻结时一并给你，不需要再调别的工具登记。\n"
            "  已 frozen 调本工具是 no-op。\n\n"
            "**版本语义（RFC 2026-08-18）**：freeze 冻的是当前**版本**。之后要修订：save_artifact "
            "带 amendment_reason 重发（同 type 同 name）→ 产生未冻结新版本；新版本要治理新的"
            "实验必须再次 freeze。冻结版的快照与逐字段差异由框架自动入账本。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "list_artifacts 返回的 id。"},
                "reason": {"type": "string", "description": "可选：冻结理由，记入 metadata。"},
                "run_role": {
                    "type": "string",
                    "enum": list(_RUN_ROLES),
                    "description": (
                        "**冻结 pre_registration 时必填**（其它类型忽略）。"
                        "primary=本课题的正式实验，结果可作为证据；"
                        "secondary=探索/调试，结果不进正式统计。"
                        "冻结不可逆：未声明则 experiment 一律按 secondary 记录，"
                        "此后该课题任何实验都出不了 confirmatory verdict。"
                    ),
                },
                "expected_params": {
                    "type": "object",
                    "description": (
                        "**run_role=primary 时必填**。此刻承诺的结构化科学"
                        "参数集，例如 {\"n_particles\": 4000, \"timestep\": 0.005, "
                        "\"cooling_rates\": [5e-5, 5e-3]}。experiment 会拿实际 "
                        "execution_params 逐字段比对，对不上就拒绝执行。"
                    ),
                },
            },
            "required": ["artifact_id"],
        },
        risk_level="medium",
    ),
    _freeze_artifact,
)


# ─────────────────────────────────────────────────────────────────────────────
