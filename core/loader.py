"""YAML → NodeHarness 加载器。

契约：./nodes/{node_type}/harness.yaml 下的每个文件都必须能解析为一个
NodeHarness。加载器刻意做得宽松 —— 缺失字段会回退到 NodeHarness 默认值，
所以即使只有 node_type + version 的 stub yaml 也能正常加载。

文件结构（自 2026-05 重组）：
  nodes/{node_type}/harness.yaml      —— 节点 harness（必填）
  nodes/{node_type}/tools/            —— 节点专属工具（可选）
  nodes/{node_type}/skills/           —— 节点专属 skill（可选）
  nodes/{node_type}/fixtures/         —— 节点 fixture yaml（可选）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .harness import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    HandoffPolicy,
    NodeHarness,
    SummarizerConfig,
)

NODES_DIR = Path(__file__).parent.parent / "nodes"


def _default_context_window() -> int:
    """yaml 缺省时的 max_context_tokens 默认值。

    窗口是**这个模型**的属性，所以跟着 reasoning 角色的绑定走。角色没绑或
    平台没填窗口 → 回退到 harness.py 那个唯一的兜底常量。yaml 显式写
    max_context_tokens 仍然优先。

    `LLM_CONTEXT_WINDOW` 保留为 CLI/开发入口的输入（它会被合成进 reasoning
    绑定），但不再在这里单独读一次 —— 那会让"窗口多大"变成第二个真相源。
    """
    try:
        from . import model_roles

        binding = model_roles.resolve(model_roles.REASONING_ROLE)
    except Exception:  # 目录残缺不该让节点加载崩掉；兜底常量照旧可用
        binding = None
    if binding is not None and binding.context_window_tokens:
        return int(binding.context_window_tokens)
    return DEFAULT_MAX_CONTEXT_TOKENS


def node_dir(node_type: str, nodes_dir: Path | None = None) -> Path:
    """返回 nodes/{node_type}/ 的路径。"""
    return (nodes_dir or NODES_DIR) / node_type


# ── v3.6 框架强制工具（对应既有的 _ALWAYS_ON_HOOKS）──────────────────────────
# "我被上游卡住了"是每个 producing 节点的**基本申诉权**，不该由节点自己选择是否
# 拥有 —— 正如高危命令确认 hook 不允许节点关掉。三轮 E2E 的根因就是卡住的节点
# 无处申诉，只能反复重跑自己（writing 连重 5 次 × 50 轮；curator 单 run 查 5 万次）。
# 系统节点（_orchestrator/_reviewer/_curator）不注入：它们本就有 redirect/决策通道。
_ALWAYS_ON_TOOLS: tuple[str, ...] = (
    "report_blocker",
    "extract_material",
    # 判决拆除批 0：blocking 义务的申报式让步出口。没有它，义务在无人值守下
    # 没有任何合法终点（chat 终态闸驳回 complete、blocked 又非终态）——
    # qinp 事故的批量复制器。让步进永久账本+终审，改不了任何 status。
    "concede_obligation",
    # Compatibility-specific upstream routing remains available while Project
    # v2 agents use the more general blocker channel for non-upstream causes.
    "request_upstream_rework",
    # v3.9（E2E-4 实测）：引用反查工具昨天只接到 _reviewer/_curator（writing 的
    # harness 属 qinp，不越界改），结果 writing 三个 run **一次都没调过它** ——
    # bib 又是清一色 `note = {Preprint}`，无 arXiv ID / DOI / venue，与 E2E-3
    # 同病。KB org 层躺着真 ID，工具在框架里，写引用的那个节点够不着。
    # "机制存在但没接到路径" —— 这条我自己留了一个没接。放 always-on 不动任何
    # 同事的 yaml：只读、低风险，而且"引用要可核对"对所有写引用的节点都成立。
    "resolve_citations",
    # v3.7.2：没有它，被 revise 重新调起的节点**没有能力**读到自己上一版，
    # 只能从零重做（E2E-3：改一个 bib 条目 → 50 轮 4.3M token 的完整重做）。
    "read_own_prior_attempt",
    # 2026-09-24：开局注入的「本组已知」错了，手里有证据的节点要有地方说 —— 和
    # report_blocker 一样是基本申诉权。只提议不落地：组织的管理员裁（core/org_corrections）。
    "propose_org_correction",
)


def _with_always_on_tools(declared: list, node_type: str, skills: list | None = None) -> list[str]:
    tools = [str(t) for t in declared]
    # ── 声明了 skill 就必须够得着 skill 正文（v3.9.1）────────────────────────
    #
    # 两级加载之后 system_prompt 里只剩 L1 索引，正文要另取。取正文的通道是
    # `load_skill`（core/skill_registry.render_index），而它是框架在**索引里
    # 打的广告** —— 广告与默认必须一致：白名单里没有它，模型照着索引调就被
    # "工具不在本节点白名单内" 顶回来，skill 等于只剩标题。
    #
    # 判据取"这个节点看得见 skill 吗"（声明了 skills，或者拿着 list_skills
    # 能列出来），不写节点名单 —— 名单式的护栏对新节点默认漏过。系统节点
    # （`_` 开头）在这里**不跳过**：_curator / _orchestrator 各自声明了 skill，
    # _reviewer 靠 list_skills 查被审节点该守哪份 SOP，跳过它们就是又留一条
    # 没接上的路径。
    if (skills or "list_skills" in tools) and "load_skill" not in tools:
        tools.append("load_skill")
    if node_type.startswith("_"):
        return tools
    for t in _ALWAYS_ON_TOOLS:
        if t not in tools:
            tools.append(t)
    return tools


_POST_RUN_FLOWS = ("full", "review_curate", "review_only", "none")


def _resolve_post_run_flow(raw: dict) -> str:
    """解析 post_run_flow，未声明时由老字段 skip_post_node_review 推导。

    向后兼容是硬要求：既有节点一个字都没改，行为必须一字不变 ——
    `skip_post_node_review: true` 语义上就是 `review_only`（跳 reviewer、
    curator 照跑），所以直接映射过去，不需要 owner 改 yaml。

    非法值 **fail-loud**：静默回落成 "full" 会让一个想当服务的节点默默变回
    producing，然后卡在"欠 flow"门禁上，现场极难查（今天已经在别处栽过
    "配置写了但没生效"）。
    """
    v = raw.get("post_run_flow")
    if v is None:
        return "review_only" if raw.get("skip_post_node_review") else "full"
    v = str(v).strip()
    if v not in _POST_RUN_FLOWS:
        raise ValueError(f"harness.yaml 的 post_run_flow={v!r} 非法，只能是 {_POST_RUN_FLOWS}")
    return v


def _refuse_if_node_code_did_not_load(node_type: str) -> None:
    """节点自己的模块没 import 起来 → 它不是一个能跑的节点，别让它开跑。

    bootstrap 对节点模块的 import 失败一向是 `log.warning` 然后继续。对**框架**
    来说这是对的（一个节点坏了不该拖垮别的节点），对**这个节点**来说是错的：
    工具一件没注册，而 harness.yaml 里照旧点着名，模型只会一个个去找、找不到、
    跳过那一步 —— 现场看到的是"模型不好好干活"，不是"代码没加载"。
    2026-08-21 hypothesis 节点就这么丢了全部 11 个工具，日志里只有两行 WARNING。

    这道闸放在 load_harness：每条启动路径（run_node / chat / executor）都要过它，
    所以不存在"从某个入口进来就绕过了"。判据现算，不缓存 —— 修好重 bootstrap
    之后账本自己就空了。
    """
    from core.bootstrap import node_import_failures

    failures = node_import_failures(node_type)
    if not failures:
        return
    detail = "\n".join(f"  - {mod}: {err}" for mod, err in sorted(failures.items()))
    raise RuntimeError(
        f"节点 {node_type!r} 的代码没有加载成功，拒绝启动：\n{detail}\n"
        f"这些模块没 import 起来，意味着该节点注册的工具/hook 一件都不在 registry 里；"
        f"照常开跑只会让模型对着 harness.yaml 里点名的工具全部落空。先修 import 再跑。"
    )


def _refuse_if_declared_tools_are_not_granted(harness: NodeHarness) -> None:
    """yaml 白名单声明了、但这个节点**永远拿不到**的工具 → 拒绝启动。

    白名单是 owner 的声明；`allowed_node_types` 是框架的安全边界。两者冲突时
    `list_tools_for_node` 静默取交集 —— owner 的声明被无声吞掉，而 harness.yaml
    和 system_prompt 照旧点着这个工具的名。模型看得见文案、找不到工具，只能跳过
    那一步或者一直撞墙。

    2026-08-22 扫盘实测，两处已经这样活着：

      · `safe_execute_python` 是 experiment 自己的私有包装，却因为
        `dataclasses.replace` 从 `execute_python` 继承了
        `allowed_node_types=["postprocess"]`，**把 experiment 自己挡在门外**
        （2026-08-08 P6-b 引入，活了 14 天）。能力上有 `safe_run_bash` 兜底所以
        没瘫痪，代价是那层 execution_params 对账契约从未生效过。
      · `observation` 的白名单和 system_prompt 都写着用 `execute_python` 直接算，
        实际一次也拿不到。

    既有的 `test_prompt_named_tools_exist` 拦不住这一类：它问的是"这个名字在不在
    registry"，不问"这个节点够不够得着"。防线在，边界划错了。

    只查 `allowed_node_types` 这一类**静态**冲突。`required_runtime_capability`
    不查 —— 那是运行时状态（writing 的 fixture 交付工具就该有时有、有时没有），
    按情况不授予是设计本身，不是配置错误。
    """
    from core.tool_registry import get_tool

    blocked: list[str] = []
    for name in harness.tools or []:
        tool = get_tool(name)
        if tool is None or tool.internal_only:
            continue  # 幽灵名/内部工具各有自己的闸，不在本条判据里
        allowed = tool.allowed_node_types
        if allowed is not None and harness.node_type not in allowed:
            blocked.append(f"  - {name}：allowed_node_types={sorted(allowed)}")
    if not blocked:
        return
    raise RuntimeError(
        f"节点 {harness.node_type!r} 的 harness.yaml 白名单里声明了它**拿不到**的工具，"
        f"拒绝启动：\n" + "\n".join(blocked) + "\n"
        f"这些工具的 allowed_node_types 不含 {harness.node_type!r}，"
        f"list_tools_for_node 会把它们静默滤掉 —— 而 prompt 里照旧点着名。\n"
        f"解除路径二选一：① 该节点确实该用它 → 把 {harness.node_type!r} 加进那个工具的 "
        f"allowed_node_types；② 不该用 → 从白名单和 system_prompt 里一并删掉。"
    )


def load_harness(node_type: str, nodes_dir: Path | None = None) -> NodeHarness:
    """按 node type 加载并解析 harness YAML。

    如果文件不存在，会抛出 FileNotFoundError —— 团队 owner 必须先创建它。
    （对全新节点：复制 templates/harness.yaml.template 到 nodes/{your_node}/harness.yaml。）
    """
    base = nodes_dir or NODES_DIR
    path = base / node_type / "harness.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 node_type={node_type!r} 对应的 harness：{path}。"
            f"请复制 templates/harness.yaml.template 到 nodes/{node_type}/harness.yaml。"
        )
    _refuse_if_node_code_did_not_load(node_type)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    handoff_raw = raw.get("handoff_policy") or {}
    handoff = HandoffPolicy(
        strategy=handoff_raw.get("strategy", "summary"),
        max_tokens=handoff_raw.get("max_tokens", 3000),
        required_fields=list(handoff_raw.get("required_fields") or []),
    )

    completion = raw.get("completion_criteria") or {}

    # summarizer 配置（可选，默认从 NodeHarness 默认值走）
    summ_raw = raw.get("summarizer") or {}
    if summ_raw:
        trigger = summ_raw.get("trigger") or {}
        keep = summ_raw.get("keep_verbatim") or {}
        compress = summ_raw.get("compress") or {}
        summarizer = SummarizerConfig(
            enabled=summ_raw.get("enabled", True),
            trigger_type=trigger.get("type", "token_threshold"),
            trigger_threshold=float(trigger.get("threshold", 0.7)),
            keep_system=bool(keep.get("system", True)) if isinstance(keep, dict) else True,
            keep_last_n_turns=int(keep.get("last_n_turns", 3)) if isinstance(keep, dict) else 3,
            keep_tool_calls=list((keep.get("tool_calls") or []) if isinstance(keep, dict) else []),
            strategy=compress.get("strategy", "llm"),
            target_tokens=int(compress.get("target_tokens", 2000)),
            instruction=compress.get("instruction", "") or "",
        )
    else:
        summarizer = SummarizerConfig()

    harness = NodeHarness(
        node_type=raw.get("node_type") or node_type,
        version=str(raw.get("version", "0.1")),
        risk_level=raw.get("risk_level", "low"),
        system_prompt=raw.get("system_prompt", "") or "",
        rules=list(raw.get("rules") or []),
        guidelines=list(raw.get("guidelines") or []),
        skills=list(raw.get("skills") or []),
        tools=_with_always_on_tools(
            raw.get("tools") or [], node_type, raw.get("skills") or []
        ),
        memory_query=(raw.get("context_config") or {}).get("memory_query", "") or "",
        kb_query=(raw.get("context_config") or {}).get("kb_query", "") or "",
        max_context_tokens=(raw.get("context_config") or {}).get(
            "max_context_tokens", _default_context_window()
        ),
        max_output_tokens=(raw.get("context_config") or {}).get("max_output_tokens", 16384),
        temperature=(raw.get("context_config") or {}).get("temperature", 0.7),
        # required_outputs 和 required_output_artifact_types 互为别名。
        # 顶层 required_output_artifact_types 优先；否则取 completion_criteria.required_outputs
        required_outputs=list(
            raw.get("required_output_artifact_types") or completion.get("required_outputs") or []
        ),
        # quality_checks 层已删除（2026-08-22）：yaml 里残留的块静默忽略。
        expected_inputs=dict(raw.get("expected_inputs") or {}),
        expected_outputs=dict(raw.get("expected_outputs") or {}),
        handoff_policy=handoff,
        max_turns=raw.get("max_turns", 12),
        loop_hooks=list(raw.get("loop_hooks") or []),
        hook_config=dict(raw.get("hook_config") or {}),
        summarizer=summarizer,
        required_input_artifact_types=list(raw.get("required_input_artifact_types") or []),
        required_output_artifact_types=list(
            raw.get("required_output_artifact_types") or completion.get("required_outputs") or []
        ),
        max_turns_by_mode={
            str(k): int(v)
            for k, v in (raw.get("max_turns_by_mode") or {}).items()
        },
        required_outputs_by_mode={
            str(k): list(v or [])
            for k, v in (raw.get("required_output_artifact_types_by_mode") or {}).items()
        },
        callable_nodes=list(raw.get("callable_nodes") or []),
        skip_post_node_review=bool(raw.get("skip_post_node_review", False)),
        # 未声明时按老字段推导，保证既有节点行为一字不变
        post_run_flow=_resolve_post_run_flow(raw),
        shell_probe_only=bool(raw.get("shell_probe_only", False)),
        deliverable_writes=bool(raw.get("deliverable_writes", False)),
        # v3.1：per-harness 模型异构（reviewer/judge 与 producer 分模型的开关）
        llm_model=(raw.get("model") or None),
        llm_base_url=(raw.get("model_base_url") or None),
        llm_api_key_env=(raw.get("model_api_key_env") or None),
        # v3.1：框架级默认 hook 的 owner opt-out
        loop_hooks_disable=list(raw.get("loop_hooks_disable") or []),
    )

    # v1.4: 应用 owner_config.yaml override（如果存在）—— 优先级最高
    from .owner_config import load_owner_config, apply_owner_config

    owner_cfg = load_owner_config(node_dir(node_type, nodes_dir))
    apply_owner_config(harness, owner_cfg)

    # ── expected_outputs 的每个 key 都是"要产出的 artifact type" ──────────────
    # context_engine 把这一栏原样渲染成「## 你必须产出这些 artifact」。往里写一个
    # 汇报字段（不是 artifact）＝ 框架当面告诉模型去 freeze 一个不存在的东西。
    # _curator 曾挂了个 kb_writes_summary，三个不同 run 各撞一次「找不到 artifact」。
    # 判据取最机械的那条：一个 artifact 都不产的节点，不可能"必须产出这些 artifact"。
    if harness.expected_outputs and not (
        harness.required_outputs or harness.required_output_artifact_types
    ):
        raise ValueError(
            f"节点 {harness.node_type!r} 没有声明任何 required output artifact type，"
            f"却在 expected_outputs 里列了 {sorted(harness.expected_outputs)}。"
            "expected_outputs 会被渲染成「你必须产出这些 artifact」，"
            "非 artifact 的汇报字段请写进 rules / handoff_policy，不要写这里。"
        )

    _refuse_if_declared_tools_are_not_granted(harness)

    return harness


def list_harnesses(nodes_dir: Path | None = None) -> list[str]:
    """返回 nodes/ 下所有有 harness.yaml 的节点名。"""
    base = nodes_dir or NODES_DIR
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "harness.yaml").exists())


# ── 「这个节点跑完欠不欠 post-node flow」的唯一真相源 ────────────────────────
#
# 这个谓词原先私有在 shared/tools/run_node.py 里（`_owes_post_node_flow`），
# 于是**下决定的那一端问不到它**：present_decision_package 授权一个
# redirect_upstream 目标时，只能校验"目标非空"，校验不了"目标跑起来能不能把这条
# flow 关掉"。2026-09-17 yuankk 那条会话就死在这个缝里 —— reviewer 把根因指向
# postprocess（服务节点），授权通过，而 run_node 里的绑定/计数/闭合三件事全都挂在
# 这个谓词上，于是义务既关不掉、也不被空转熔断看见，空转 40 轮。
#
# 搬到这里：它是**节点契约的属性**，不是调度器的内部细节，两端读同一份实现。


def node_owes_post_node_flow(node_type: str) -> bool:
    """这个节点跑完欠不欠 reviewer/决策包那条链。

    这同时是「一条 post-node flow 能不能被**这个节点**关掉」的判据：run_node
    只在本谓词为真时把 flow entry 绑成 action_in_progress，而闭合与空转计数都
    只认 action_in_progress。谓词为假 ⇒ 起它一万次，flow 一动不动。

    三段判定，顺序不能换：

    1. **系统节点（`_` 前缀）永远 False**。它们不是被审查的对象，恰恰是**执行
       审查的人** —— _reviewer / _curator 正是用来把 pending entry 推进的。
       （系统节点 harness 也默认 post_run_flow=full，少了这条，推进审查链的节点
       会被审查链自己卡住。）
    2. 权威是 harness 自己的 `post_run_flow` 声明。
    3. **harness 读不出来 = 这个节点跑不起来** → False。

    ## 第 3 条原来是一张手写名单，而名单会烂

    原文写的是"读不到 harness（测试假节点 / yaml 损坏）就回退 PRODUCING_NODE_TYPES，
    回退偏保守：宁可多要一次审查"。那张名单里躺着 `analysis`（节点早已删除）、
    `literature`、`data`（两个都已改成服务）—— 于是

        node_owes_post_node_flow("analysis") → True

    也就是说 reviewer 点名一个**不存在的节点**，三道 redirect 闸会一致放行它，
    直到 run_node 起的时候才炸。这正是本轮刚修完的那个病的同一个形状
    （[[feedback_guardrails_must_scan_not_list]]）：护栏内部藏着一张需要有人记得更新的名单。

    ## 为什么正解是 False 而不是"换一张新名单"

    `run_node` 起子节点前有一道 **harness 存在性预检**（`load_harness(node_type)`，
    FileNotFoundError 直接返回错误）。所以 harness 读不出来的节点**根本跑不起来**：

      · 它不会跑完，也就不会产出需要审查的东西 → 没有"欠不欠审查"可言；
      · 它更不可能把任何 flow 关掉 —— 闭合要求它先跑成功。

    两个问题的答案在这一档是同一个 False。"偏保守"当初想防的是"把科学节点误当服务
    放行"，可那要求节点能跑；跑不起来的节点谈不上放行谁。名单因此不只是烂了，
    它从一开始就在回答一个不存在的局面。
    """
    if str(node_type or "").startswith("_"):
        return False
    try:
        return load_harness(node_type).owes_post_node_flow
    except Exception:
        return False
