"""NodeHarness —— 每个 harness YAML 解析的目标契约。

这是平台的硬契约：任何节点 YAML 都必须能解析为一个 NodeHarness。YAML 中
出现的字段会覆盖默认值；缺失的字段则回退到下面定义的默认值。

如果要改这个 schema，等于改动了每个团队 owner 的契约 —— 请先和架构师讨论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_MAX_CONTEXT_TOKENS = 256000
"""没有任何人说话时的上下文窗口 —— summarizer 在它的 70% 处开始压缩。

**它只是兜底**：真实窗口是模型的属性，平台在建 worker 环境时按所选连接的
`context_window_tokens` 注入 `LLM_CONTEXT_WINDOW`，节点 yaml 显式写了更优先。
兜底值只在"谁都没填"的时候生效。

2026-08-20 从 120000 提到 256000（wangd）。原值是照 128K 一代模型定的，而在跑
的连接（DeepSeek V4 系列）是 1M 窗口 —— 84k 处就开始反复压缩，等于把大半个
可用上下文扔了。⚠️ 反过来的代价：真是 128K 窗口的模型如果**也没填**
context_window_tokens，压缩会晚到 179k 才触发，那时早已撞穿 provider 硬上限。
所以每条连接都该把真实窗口填上，别靠这个数。

改这个值时记得：`core/loader.py`、`chat.py` 从这里导入，前端占位文案由
`platform/frontend/src/features/settings/lib/model-defaults.test.ts` 钉住。
"""

@dataclass
class HandoffPolicy:
    """本节点的输出如何传递给下游节点。"""
    strategy: str = "summary"          # "full" | "summary" | "selective" | "none"
    max_tokens: int = 3000
    required_fields: list[str] = field(default_factory=list)


@dataclass
class SummarizerConfig:
    """节点 context 压缩配置。

    触发：当 messages 估算 token 数 > trigger_threshold * max_context_tokens。
    保留：system message 永远不动；最近 N 轮原样保留；某些工具调用永远不压缩。
    压缩：把中间老消息用 strategy 压成一段简短叙述，替换为单条 system 消息。
    """
    enabled: bool = True
    trigger_type: str = "token_threshold"   # "token_threshold" | "turn_count" | "never"
    trigger_threshold: float = 0.7          # 占 max_context_tokens 的比例
    keep_system: bool = True                # 永远保留所有 system 消息（含 initial 和 hook-injected）
    keep_last_n_turns: int = 3              # 最近 N 轮原样保留
    keep_tool_calls: list[str] = field(default_factory=list)   # 这些工具的 call+result 永远不压缩
    #: **有损层**用哪个。无损清除层由框架在 run_summarizer 里无条件先跑，
    #: 不受本字段影响（做成选项的话，不写的 harness 就默认漏过 —— v22 实测
    #: `_orchestrator` 正是这样错过了它最需要的那一层）。
    strategy: str = "llm"                   # "llm" | "truncate" | "drop_tool_results" | "custom"
    target_tokens: int = 2000               # 压缩后中间叙述目标 token 数
    instruction: str = ""                   # 给 LLM 的额外压缩指引（节点专属）


@dataclass
class NodeHarness:
    """节点在运行时需要的一切。

    owner 自己写的字段：
      system_prompt、rules、guidelines、skills、tools、
      required_outputs、expected_inputs、expected_outputs、
      handoff_policy。

    平台提供默认值的字段（可以覆盖，但一般不需要）：
      context_config（token 预算、检索查询）。
    """
    node_type: str                                   # 例如 "literature"
    version: str = "0.1"
    risk_level: str = "low"                          # "low" | "medium" | "high"

    system_prompt: str = ""                          # LLM 的 system message
    rules: list[str] = field(default_factory=list)   # 硬约束
    guidelines: list[str] = field(default_factory=list)  # 软建议

    skills: list[str] = field(default_factory=list)  # 已注册的 skill 名
    tools: list[str] = field(default_factory=list)   # 已注册的工具名

    # 检索 / 上下文预算
    memory_query: str = ""
    kb_query: str = ""
    # 注意：max_context_tokens 是 summarizer 触发阈值（compress at threshold×0.7），
    # 不是 API hard limit。设满了 = summarizer 永远不触发 = 长 session 必撞 API。
    # 优先级：yaml 显式值 > LLM_CONTEXT_WINDOW env（后端按所选连接的
    # context_window_tokens 注入）> 这个默认值。
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    max_output_tokens: int = 16384
    temperature: float = 0.7

    # 完成判定
    required_outputs: list[str] = field(default_factory=list)   # artifact 类型

    # I/O 契约
    expected_inputs: dict[str, str] = field(default_factory=dict)
    expected_outputs: dict[str, str] = field(default_factory=dict)

    # 下游 handoff
    handoff_policy: HandoffPolicy = field(default_factory=HandoffPolicy)

    # 循环控制：0 = 无限制（开发模式默认）；>0 = 软上限
    max_turns: int = 0

    # Hook 系统：owner 在 yaml 里启用的 hook 名字列表
    # （memory_delta hook 永远启用，不需要在这里写）
    loop_hooks: list[str] = field(default_factory=list)
    # 每个 hook 的配置 dict（hook 自己解读）；yaml 中通过顶层 `hook_config:` 指定
    hook_config: dict[str, dict] = field(default_factory=dict)

    # 节点 context 压缩配置（per-node summarizer）
    summarizer: SummarizerConfig = field(default_factory=SummarizerConfig)

    # subagent / 多节点编排契约（默认空 → 单节点跑法不变）
    # 本节点必须看到的上游 artifact type（缺则 executor 报 missing_input_artifacts）
    required_input_artifact_types: list[str] = field(default_factory=list)
    # 本节点必须产出的 artifact type（同 required_outputs，但语义更清晰）
    # 若 yaml 同时写了 completion_criteria.required_outputs，loader 会合并
    required_output_artifact_types: list[str] = field(default_factory=list)
    # v2.1：服务化节点按请求模式分流交付契约。调用方的诉求差异很大（"查一个
    # 参数" vs "铺一个领域"），只有一档契约就会逼着服务对所有请求跑最重的流程
    # （实测：literature 为一句"给我 5 篇论文"跑完整综述，烧 1.05M tokens）。
    required_outputs_by_mode: dict[str, list[str]] = field(default_factory=dict)
    # 轮次预算同样属于请求契约：定向查证和铺一个领域不该给同一个预算。
    max_turns_by_mode: dict[str, int] = field(default_factory=dict)
    # 本节点允许通过 run_node 工具调用的子节点白名单
    # 空列表 = 默认禁止 run_node；["*"] = 任意节点
    callable_nodes: list[str] = field(default_factory=list)

    # v0.4: owner opt-out post-producing reviewer
    # producing 节点 owner 觉得不值得每次都 review（如 data 节点产 dataset），可以在
    # 自己 harness.yaml 设 skip_post_node_review: true → 框架自动跳过 reviewer，
    # curator 仍跑（KB 整合不可跳）。decision package 仍会出，但 review 段标
    # "skipped (owner opt-out)"。默认 false（多数节点都该被 review）。
    skip_post_node_review: bool = False

    # ── 跑完之后欠什么账（2026-08-04：服务型节点）────────────────────────────
    #   "full"        默认 —— reviewer → curator → decision package 全走
    #   "review_only" 跳 reviewer，curator + 决策包照走（== 老的
    #                 skip_post_node_review: true，保留兼容）
    #   "none"        三样全不欠 —— **服务型节点**
    #
    # 为什么"服务不配独立 reviewer"是对的而不是偷懒：审查存在的理由是
    # **科学主张的作者有动机自欺**。服务的产出是**资产**（图、数据集），
    # 对错分两层：①数据对不对得上 —— 机械可查（figure 必须绑得上取数来源）；
    # ②合不合用 —— 只有**需求方**判断得了，独立审查者在这层没有优势。
    # 所以把关下沉到消费方（writing 收图进稿时自己审），而不是再挂一轮 reviewer。
    #
    # 代价与边界（不许含糊）：
    #   - 服务产出**只能**经两条路进入最终交付：(a) 被一个受审查的消费方采用
    #     （图进手稿 → 手稿被审 → 图间接被审）；(b) 用户直接点的一次性任务
    #     （"画个图就完"）—— 那时用户本人就是审查方。
    #   - 没有 curator 就没有沉淀把关 → 服务节点**不给 KB 写入工具**。运维经验
    #     走 memory_note 排队，由日常 dreaming 统一加工。
    post_run_flow: str = "full"

    def max_turns_for(self, mode: str | None) -> int:
        """本次调用的轮次预算 —— 按请求模式解析，未声明回落到 max_turns。"""
        if isinstance(mode, str) and mode in (self.max_turns_by_mode or {}):
            return int(self.max_turns_by_mode[mode])
        return self.max_turns

    def required_outputs_for(self, node_inputs: dict | None) -> list[str]:
        """本次调用真正该交付什么 —— 按 node_inputs['mode'] 解析。

        没声明 by_mode、或传的 mode 不在表里 → 回落到默认 required_outputs，
        既有节点行为一字不变。
        """
        # required_output_artifact_types 与 required_outputs 互为别名（loader 两个
        # 都填，但手工构造的 NodeHarness 可能只设其一）—— 回落顺序与
        # shared/tools/run_node.py 的解析保持一致，否则同一个 harness 在不同
        # 代码路径上会算出不同的必交产物。
        default = list(self.required_output_artifact_types or self.required_outputs or [])
        if not self.required_outputs_by_mode:
            return default
        mode = (node_inputs or {}).get("mode")
        if isinstance(mode, str) and mode in self.required_outputs_by_mode:
            return list(self.required_outputs_by_mode[mode])
        return default

    @property
    def owes_post_node_review(self) -> bool:
        """跑完是否要挂 reviewer（老字段与新字段的唯一合流点）。"""
        if self.post_run_flow in ("none", "review_only"):
            return False
        return not self.skip_post_node_review

    @property
    def owes_post_node_flow(self) -> bool:
        """跑完是否要登记 post-node flow（reviewer/curator/决策包整条链）。

        False = 服务型：不进 pending_post_node_flow，因此也不会被
        "上一个 flow 没走完就不许起新 producing 节点"那道门禁挡住 —— 那道门是
        为科学产出的审查链设的，服务被消费方随时调用，卡在那里就没法用了。
        """
        return self.post_run_flow != "none"

    @property
    def is_service(self) -> bool:
        return self.post_run_flow == "none"

    @property
    def defers_decision_to_analysis(self) -> bool:
        """跑完走 reviewer + curator，但**不**停下来问人 —— 顺延给下一个 Analysis。

        动机：Analysis ↔ Experiment 每转一圈就要人工答两次 decision，是纯粹的
        决策疲劳；而 Analysis 本来就是"下一步做什么"的裁决者，让它连着裁决
        实验结果，比把同一个判断切成"先问人、再问 Analysis"更顺。

        红线：**只在 review 成功时顺延**。review 挂了（failed_awaiting_human）
        照旧立刻出 REVIEW-FAILED decision package 并继续拦下游 —— 顺延的前提
        是"有一份可信的独立审查"，没有审查就没有可顺延的东西。
        """
        return self.post_run_flow == "review_curate"

    # v3.3（2026-07-09）：shell 探查专用模式。true → 本节点的框架版 run_bash
    # **机械拒绝**一切写操作（重定向/tee/cp/mv/rm/mkdir/sed -i/pip install…）和
    # 内联解释器（heredoc / python -c / bash -c）——只留真·只读探查
    # （ls/grep/which/nproc/nvidia-smi/git status）。给"协调不干活"类节点
    # （_orchestrator）用：此前"run_bash 只用于只读探查"是纯 prompt 文字，实测
    # orchestrator 被 write_file 白名单弹回后直接绕道 run_bash heredoc 替
    # producing 节点干活，零机械后果。
    shell_probe_only: bool = False

    # 2026-08-31：协调者的交付写权。true → 本节点的 write_file/edit_file 除了
    # 自己的作用域，还可写 Project 里的**无主之地**（不属于任何节点作用域、
    # 也不命中框架状态签名 _PROTECTED_SIG 的路径）——典型就是用户点名的
    # 交付文件（LITERATURE_REVIEW.md / 项目根的说明文档）。
    #
    # 为什么要有它（wangd 2026-08-31 拍板）：此前 orchestrator 没有任何写文件
    # 工具，"用户要一份 md"这种轻量请求也只能起完整 producing 节点 ——
    # 实测一次 9 文件的文献梳理走了 26 分钟 / 82 次工具调用，产物还落在
    # literature/artifacts/*.json 里而不是用户点名的文件。
    #
    # 边界（三闸并立，这个 flag 只开第三块地）：
    #   · 六个 producing 节点的目录、MEMORY.md —— 照旧只归属主（写边界拦）
    #   · artifacts/ 等框架状态 —— 照旧硬拒（_PROTECTED_SIG，不受 /bypass 影响）
    #   · 其余（项目根的普通文件）—— 本 flag 放行
    # 正式研究产物仍走 run_node：这里放行的是"交付/搬运"，不是"替节点做研究"
    # 的豁免——QC 管的是 artifact 命名空间，那块一寸没让。
    deliverable_writes: bool = False

    # v3.1（审计）：per-harness 模型异构开关。None → 全局 LLM_MODEL/LLM_BASE_URL。
    # 首要用例：_reviewer / judge 配与 producer 不同的模型（同模型自审偏好）；
    # 也是"同 harness × 2 模型"对照实验（'harness 是差异化' 论题）的实验开关。
    # api key 走 env 间接（yaml 里绝不放 key）：llm_api_key_env 指向环境变量名。
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key_env: str | None = None

    # v3.1：框架级默认 hook 的 owner opt-out（producing 默认列表，目前为空）
    loop_hooks_disable: list[str] = field(default_factory=list)

    # v1.4: owner_config.yaml 可覆盖以下字段（None = 走 framework 默认 / env）
    # 详见 core/owner_config.py 的 SUPPORTED_FIELDS。
    llm_timeout_s: float | None = None              # None → 走 LLM_TIMEOUT env (默认 300s)
    llm_max_retries: int | None = None              # None → 走 LLM_MAX_RETRIES env (默认 3)
    subagent_max_depth: int | None = None           # None → HARNESS_FRAMEWORK_MAX_SUBAGENT_DEPTH (默认 4)
    subagent_max_parallel: int | None = None        # None → HARNESS_FRAMEWORK_MAX_PARALLEL (默认 5)
    subagent_child_timeout_s: float | None = None   # None → 跟随 llm_timeout_s

    def describe(self) -> str:
        """供日志用的简短可读摘要。"""
        return (
            f"NodeHarness(node_type={self.node_type!r} version={self.version!r} "
            f"rules={len(self.rules)} tools={len(self.tools)} "
            f"required_outputs={self.required_outputs})"
        )
