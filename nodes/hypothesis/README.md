# hypothesis 节点（= Analysis）

**Owner**: TBD

读上游 literature / 用户输入，产 **研究协议 + 完整实验设计**两件套，并在后续轮
**裁决实验证据**。**pre_registration 是科研可信度的 anchor**（frozen），
**research_plan 是实验严格化的 anchor**（experimental design + baselines + risks）。

## v0.5 两处结构性改动（2026-08-16，issue #417/#418）

### ① 两档模式：`plan` / `revise`（机械路由）

| mode | 何时 | 交付 |
|---|---|---|
| `plan` | 盘上没有「冻结 prereg + research_state」 | 五件产物，走完整工作流 |
| `revise` | 已有生效协议 | 一版新的 research_state（改了计划才补 plan / 新 prereg） |

模式由 `tools/analysis_mode.resolve_mode` **按项目状态机械判定**，不靠调用方申报
（判定结果写进 transcript 的 `analysis_mode_resolved` 事件）。调用方传 `mode=plan`
可强制重出计划；`revise` **不能被申报出来** —— 没有生效协议时降级回 `plan`。

**修的是什么**：`validate_hypothesis_outputs` 的三条 blocking 判据从 **per-run**
的 `transcript.jsonl` 取料，而 artifacts 跨 run 持久。第 2 轮开局 transcript 空 →
三条全 fail → 只能重新 freeze 一份 prereg 才能收尾 → 整套 generate/cluster/
evolve/HIF 又演一遍。prompt 写着"后续轮不要从零重做"，机制说"不重做就判死"。
现在判据分两条适用轴（见 `output_validator._applicability`）：
- **hypothesis 轴**：项目里没有带命题的问题 → 假说类判据不适用
- **authored 轴**：本轮没新增科学承诺 → "本轮新写的假说"类判据不适用

放宽的同时补了两道：`experiment_results_accounted`（实验结果必须有说法）、
`adjudication_evidence_resolvable`（裁决证据必须指得到真实产物）。

### ② 一等公民是研究问题，假设降级成它的一种

平台原本有一条 `H1` 编号贯穿七处（框架的协议解析器、冻结闸、欠账机制、每轮
上下文注入、KB 判决翻转闸、experiment 结论绑定、Analysis 账本）。它假定
**每个在册条目都有一串可测的量** —— 于是一条散文形式的假设根本冻不进去，
而没有命题的研究（探索/表征/方法/解释/复现/推导）压根不进承诺账，
平台最硬的那层保护对它完全不生效。

v0.5 泛化的不是字母 H，是**账本的单位**：从"指标"改成"闭合条件项"。

| 概念 | 说明 |
|---|---|
| **研究问题**（一等公民） | `## Research Questions` 段下的 `### Q1:`。至少一个。 |
| **命题**（可选） | 写了 `proposition` 的问题**就是**假设，不写就不是。没有 yes/no 开关字段 —— 结构本身就是声明，也就没有"声明成不是、躲开判据"这条路。 |
| **闭合条件**（冻结） | 数值条 `metric/comparison/threshold`，或陈述条 `statement: <可观察的话>`。**两类地位平等。** |
| **预设**（必填，可多条） | `- assumption:` 这个问题预设了什么。**所有问题都要写**，探索型也要。|
| **怎样算失败** | 不用单独写 —— 就是这些条目里有兑现不了的。 |

"测了某个量"只是闭合条件的一种。覆盖度达标、不确定度压进预算、跟基线对照做完、
竞争解释被证据区分、原文声明逐条对过、极限情形核对过 —— 全是陈述条，
**平台一个新分类都不用加**（这也是为什么没做"科研范式名单 + 路由"）。

防"没做也能说做完"那条红线原封不动，覆盖面反而从"有假设的研究"扩到了全部：
数值条要 `measured_metrics: {status: measured}`，陈述条要
`closure_discharges: {status: discharged, evidence: <产物 id>}` —— 空口勾除
等于没勾，跟 `estimated` 不算测量是同一条。

**兼容**：已冻结的 `## Hypothesis N (Hx)` 协议永远读作"带命题的问题，编号 Hx"，
判据一字不变（`tests/test_prereg_commitments.py` 13 项原样通过）。

**解析权在 `core/prereg_commitments.py` 一处** —— 冻结闸、欠账账本、关闭门禁、
experiment 的结论绑定、本节点的收尾自检全都调它。一个问题只有一个真相源。

### 质量判据分两层楼（v0.5.1）

**第一层 · 所有研究问题都要过**：问题与产出形态说得清 / 闭合条件冻结 /
**写明预设** / **不是已知结论的复述** / **价值与可信性被评过** / 计划里有步骤认领。

**第二层 · 只有写了 `proposition` 的额外加码**：结构化 falsifier /
数值阈值要有出处 / scope_dimensions / 冻结后不可改。**这层一分没减。**

第一版把「非结论复述 / 价值可信性」错放进了第二层，后果是无命题的问题一项
内容质量判据都拿不到（英国饮食 A/B 实测：3 个问题只审了 1 个，而 Q1
「负面评价在什么时期形成并固化？」偷偷把待检验的前提当成了背景，没人管）。

### 问题数由课题决定，框架不报数

`target_prereg` / `min_candidates` **没有默认值**。实测：原本默认 3，且被渲染进
节点第 0 轮 briefing，模型就不多不少交了 3 个 —— 那不是课题需要 3 个，是它被点了 3。
调用方自己指定时原样转达；没指定就明说「由课题决定」。

**探索型红线**：`proposition` 留空，闭合条件承诺的是"怎么找、找到哪算找完"，
**不是"必须找到什么"**。禁止把「没发现新东西」写成失败条件 —— 那会制造编造发现
的压力。声明诚不诚实是语义判断，交给 `_reviewer`（review_spec 第 0 维）。

## I/O 契约

| 方向 | artifact_type | 备注 |
|---|---|---|
| Input | `survey_report`（可选）+ `research_intent`（可选）+ **用户 prompt / 对话**（`get_research_goal` 从 node_inputs 与父 orchestrator `conversation.json` 加载） | 没 artifact 也能跑，但必须有用户诉求 grounding |
| Output | `pre_registration` × N | **必须 freeze**（freeze_artifact 自动 promote 到 deliverables/prereg/ + 写 tamper-evident manifest） |
| Output | `research_plan` × 1 | **不 freeze**：experimental_design + **computational_workflow** + baselines + resource_estimates + risk_analysis 五件套 |
| Output | `hypothesis_innovation_report` × 1 | HIF 评分卡（G/D/M/P/R/Q/I + tier） |
| Output | `hypothesis_research_overview` × 1 | top 假设 + 淘汰原因 + 未探索方向（Meta-review 轻量版） |

## 关键工具

- `get_research_goal` —— 解析结构化 research goal（goals / constraints / **must_cover_themes** / **locked_definitions** / iteration 预算）；防复杂诉求退化与分类改写
- `validate_hypothesis_outputs(checks=[…])` —— 可只跑其中几项；`research_questions_declared` / `research_questions_covered_by_plan` 是所有科研范式的共性底线，冻结闸会在不可逆那一刻再兜一次
- `read_research_state` / `update_research_state` —— 版本化研究状态；后续轮的接续点与裁决账
- `audit_user_goal_alignment` —— 硬门（**freeze 前**）：最终 prereg + research_plan 必须覆盖全部 must_cover 支柱
- `audit_definition_fidelity` —— 硬门（**freeze 前**）：prereg/plan 不得改写用户给定分类名与定义
- `audit_threshold_grounding` —— 硬门（**freeze 前**）：数值 threshold 须有 literature/theory/pilot 等依据 + 科学含义；定性/存在性（qualitative|exists|not_exists）写清判据即可，勿硬编伪精确数字
- `audit_comparison_protocol` —— 硬门（**freeze 前**）：跨任务须含共同样本/分母/可比较操作/故障处理/**指标与定义统一审查**；异构另须行为映射
- `audit_resource_feasibility` —— 硬门（**freeze 前**）：多模型/多人标注/大规模人工审计/多完整 benchmark 等须 confirmed 有来源或 assumed 有降级
- `audit_cost_instrumentation` —— 硬门（**freeze 前**）：步骤级/计算成本分析须有采集方案（度量、Step 挂接、方法、聚合、缺失策略）
- `cluster_hypothesis_candidates` —— 候选假设去重聚类（Co-Scientist Proximity 轻量版）
- `evolve_hypothesis` —— Evolution refine 计划（synthesize / simplify / regime_shift 等）
- `create_concept` —— 注册新方法 / 数据集 / 现象 concept
- `freeze_artifact` —— 冻结 prereg，返回里直接带 chunk_id（没有单独的登记工具）
- `create_claim(claim_type='hypothesis')` —— **三件套必填**：
  - `falsification_criteria_text` 或 `_structured`（数值阈值含 **threshold_rationale**；定性/存在性含清晰判据）
  - `prereg_chunk_id`
  - `predicted_outcome`
- `score_hypothesis_innovation` —— **HIF 创新性评分**（含 Q plausibility / I impact；prereg 前必调）
- `freeze_artifact` —— 锁 prereg

## 关键质量门

- 每个 hypothesis 必须可证伪（schema validate 已挡 missing fields）
- **框架硬门**（`completion_criteria.quality_checks`）：
  - `hypothesis_output_validation_passed` —— 机械读 `hypothesis_output_validation.metadata.passed`；未跑或失败 → incomplete
  - `prereg_frozen` —— `pre_registration.metadata.frozen=true`
- **多支柱覆盖**：`validate_hypothesis_outputs` 的 `user_goal_coverage` 挡住「复杂任务退化成单一小点」
- **分类定义保真**：`definition_fidelity` 挡住「改写用户操作类别/定义导致实验对象漂移」
- **阈值依据**：`threshold_grounding` 挡住「有数字无文献/理论/先导依据」的拍脑袋阈值；允许 `comparison=qualitative|exists|not_exists` 的定性/存在性证伪（写清判据，不要求编数字）
- **跨任务可比协议**：`comparison_protocol_complete` 挡住无协议横比；并要求对成功率/阈值/复杂度/类别等**定义类构造**审查是否统一；异构时还要行为映射
- **资源可执行性**：`resource_feasibility` 挡住「多模型/多人标注/大规模审计/多完整 benchmark 未确认且无降级」
- **成本采集方案**：`cost_instrumentation` 挡住「要步骤级成本证伪却只有 resource_estimates 粗估」

## 常见自定方向

- 加 `loop_hooks: [reflection]` 每 N 轮"我这假设真可证伪吗？" 自检
- 加节点专属 skill `falsifiability_pretest`（已 shared）
- 改 prompt 让 LLM 必引上游 survey 的 open_question

## 跑通示范

```bash
python run_node.py --harness hypothesis --sandbox \
  --fixture nodes/hypothesis/fixtures/minimal.yaml
```

## 不该做

- 不 freeze 就让 prereg 跑出去（科研诚信底线）
- **审计未通过就 freeze**（对齐/定义保真/阈值依据等失败后无法改冻结产物；须先 audit 再 freeze）
- **先堆额外 audit / cluster / evolve，却漏 required 或未跑 validate** —— 完成闸优先；turn 中途 nudge + finish gate 都会催
- 没 falsifier 的 conjecture 也算 hypothesis
- **给没有命题的问题硬造数值闭合条件**（#417 的病灶：历史解释课题为了过门编出
  "差 30 年算削弱、差 20 年算证伪"）—— 写不出 threshold_rationale 就说明这个数是
  凑的，改成陈述条
- **反过来**：一个明摆着在裁决命题的问题却不写 `proposition`，绕开预注册 = HARKing 正门
- **把「没发现新东西」写成探索型问题的失败条件** —— 那会制造编造发现的压力
- **mode=revise 时重跑整套生成流程**，或为了"让门禁看见"再冻一份内容相同的 prereg
- baseline 不清就瞎选 —— 用 `request_human_input` 问 user，宁等不错
- research_plan 给个空架子糊弄过去 —— `validate_hypothesis_outputs` 的 `research_plan_complete` 会挡五件套缺一，且 **computational_workflow 必须含 mermaid 流程图 + 可审计逐步任务表**（任务类型按领域自选，无学科关键词白名单），并校验 **fork/join/gate DAG**；领域适配性由 `_reviewer` 按 `review_spec` 启发性判断。`audit_computational_workflow` 在 **0 行可审计任务**时不得假通过。
