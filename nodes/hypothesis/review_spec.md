# Review spec for hypothesis 节点产物（`pre_registration` + `research_plan`）

> 当 _reviewer 节点审 hypothesis 产的 pre_registration（frozen）+ research_plan
> 时，按本 spec 走 7 维度审稿。
> 这套 rubric 是 v0.1 起点设计（架构团队提供，hypothesis 节点 owner 接手迭代）。

## 开工时你已经有什么（v2.1 起框架组装，不用自己找）

- 本 spec 全文（框架直送 —— 不要再去 read_file 找它）
- 项目现状定向（各节点交付面 / research_state / MEMORY 头）
- 被审 artifact id 在 node_inputs 里；`read_artifact` 拿全文，长文分段读
- 需要深查生产过程时用 `read_producer_transcript`

**你的 token 应该花在证据工作上**：读产物全文、核对声明与证据、查 KB 引用
是否真实存在。形式性检查（字段在不在、frozen 没 frozen）机械层已经把过关 ——
你审的是机器审不了的那半句：**内容在科学上站不站得住**。

## 审稿维度（每项 1-5 整数；**不允许** 2.5 / 3.5）

### 0. 声明诚实性 (declaration_honesty) 🆕 v0.5

> **这一项只有你能审。** 机械层只查**形式**：每个研究问题有没有 output_kind、
> 有没有闭合条件、有没有写在段外被丢掉、计划里有没有步骤认领它。
> **声明得诚不诚实是语义判断，机械层查不了**，硬编码"哪些措辞算命题"只会得到
> 一张会漏又会误伤的名单。

v0.5 起一等公民是**研究问题**；**写了 `proposition` 的问题就是假设，不写就不是**。
放宽之后唯一还挡着滥用的，就是你这一关。三个方向都要看：

- **漏写命题（主要风险）**：一个问题明摆着在裁决命题（「A 是不是比 B 好」
  「X 导致 Y 吗」「理论 T 成不成立」），却不写 `proposition`，从而绕开预注册判据。
  这等于给 HARKing 开正门 —— 直接 critical/revise。
  判据：**读完 research_plan 后问自己，这项研究做完会不会在论文里写下一句
  「所以 A 确实比 B 好」。会 → 它就是命题，必须写进 proposition 并预注册判据。**
- **表演型数值条（#417 的病灶）**：产出明明是一个解释/一张图，却配上一串凑出来的
  数值闭合条件（"差 30 年算削弱、差 20 年算证伪"）。看 `threshold_rationale` 是不是
  硬凑的、这个数在该学科里有没有人真的会预先承诺。→ 低分/revise，建议改成陈述条。
- **探索型的失败条件**：⛔ **禁止把「没发现新东西」写成闭合条件**。那会制造编造
  发现的压力，跟编阈值是同一个病。探索的闭合条件只能落在覆盖范围、数据质量、
  系统性记录上；"这个区域没有新相"本身就是合法答案。

**预设写得诚不诚实**也归你：机械层只查 `- assumption:` 在不在，查不了它是不是
套话（"假设数据可得"）。重点看**提问方式里藏着的预设有没有被摊开** —— 比如
「负面评价在什么时期形成并固化？」预设了它确实固化过，而这可能正是待检验的。

另外看陈述条**是不是真的可观察**：能想象出一条把它判否的现实路径吗？
"若数据完全不可得则失败"这种同义反复不算。

- 5 分：每个问题写没写命题都与它真实的产出形态一致；闭合条件具体、可能兑现不了
- 1 分：把命题裁决藏起来绕开预注册，或为非命题问题硬造数值阈值

### 1. 可证伪性 (falsifiability)

> **只对写了 `proposition` 的问题打分。** 全都没有命题的研究（解释型、
> 表征型、方法型、复现型、推导型）**没有假设可审，本项填 N/A，不要因此扣分** ——
> 扣分就是把"必须有假设"从机制里赶出去、又从评分里放回来。它们的"可失败性"
> 在第 0 项的闭合条件里审。
>
> **v2.1 分工**：`threshold_rationale` 字段齐不齐、source_type 合不合法、
> 公式在不在 —— 机械审计（audit_threshold_grounding + QC）已经把过关，你
> **不用重查形式**。你审语义：这个依据在科学上站得住吗？引的文献/推导真的
> 支持这个阈值吗？定性判据真的可观察吗？机械层看不出"引用存在但牛头不对马嘴"。

每个 hypothesis claim 是否真**机械可判**？还是模糊到 reviewer / 后续 experiment 节点 verdict
无法明确说"refuted"？

- 看 `falsification_criteria_structured` 是否填了：`{metric, comparison, dataset, ...}` 等机械可判字段
- 看 `comparison`：数值比较须为 `>` / `<` / `>=` / `<=` / `==` / `!=`；**定性/存在性**可为 `qualitative` / `exists` / `not_exists`（自然语言"显著优于"= fail）
- **数值比较**：`threshold` 须为数字；**看 `threshold_rationale`**：是否有 `source_type`（literature/theory/pilot/domain_convention/user_specified）+ 可追溯 `citation_or_derivation` + `scientific_meaning`？无依据的比例阈值（随意 +20%/2×）→ 低分/revise
- **定性/存在性**：不要求编数字；须有清晰可观察判据（`criterion` 或文字 `threshold`）。硬编伪精确数字 → 低分
- 5 分：所有 hypothesis 都机械可判；数值阈值有文献/理论/先导依据与科学含义，或定性/存在性判据清晰，未来 curator 能 AUTO 翻 verdict
- 1 分：falsification_criteria_text 是纯散文 / structured 字段空 / threshold 模糊 / **有数字但无依据** / 该定性却硬编无依据数字

### 2. Scope 明确性 (scope_specificity)

`scope_dimensions` 字段是否填了 dataset / regime / task / split 至少一项？

- 跨 hypothesis 时是否 scope 互斥（避免 H1 跟 H2 在同 scope 下做矛盾预测）
- `claim_text` 是否**没有**重复 scope 信息（scope 只该放结构化字段，避免同假设字面不同被当不同 claim）
- **是否对齐用户 prompt / 对话约束**（读 producer 的 `get_research_goal` 产物或 pre_registration 前言）：假设方向是否回答了用户最新诉求，而不是只复述 survey？
- **是否发生任务退化**：用户 `must_cover_themes`（或多支柱诉求）是否被收窄成单一小点？哪怕该小点可证伪性很好，若丢掉其它支柱 → 低分
- **是否改写用户分类定义**：`locked_definitions` 的类别名/定义是否被重命名、合并或换成文献 taxonomy？若实验对象已变 → 低分/critical
- 5 分：每个 hypothesis scope 明确且**正交**，合集覆盖全部 must_cover，**且**分类定义与用户 proposal 一致，并显式服务 user_prompt
- 1 分：scope 全空、scope 信息硬塞 claim_text，或把复杂诉求退化成 survey 窄 gap / 单一小点，或改写了用户操作类别
### 3. Baseline 选择合理性 (baseline_reasonableness)

`research_plan.baselines` 段：选这个 baseline 的理由站得住吗？

- 看 baseline 是否是该领域**公认的 fair comparison**（而不是随手选个弱的衬托 effect 大）
- 看 baseline 是否**复现可能**（开源 / 文档齐全 vs 内部黑盒）
- 看 baseline 是否覆盖**单一变量隔离**（消融某个机制时只换那一个）
- **跨任务/跨场景时**：是否有 `comparison_protocol`（共同样本、共同分母、可比较操作、统一故障处理、**unified_definitions**）？凡指标/阈值/成功标准/复杂度/类别等定义类，是否逐项审查「要不要统一」？缺一 → 低分/revise
- **异构 benchmark / 操作比例时**：是否另有 `behavior_alignment`？若直接把 API/reasoning 与点击/输入比占比，或未统一定义就比任何指标 → critical/revise
- **高成本资源时**：是否有 `resource_feasibility`？多模型/多人标注/大规模人工审计/多完整 benchmark 是否 confirmed（有来源）或 assumed（有降级）或 deferred（不进核心）？无确认且无降级就写进主证伪 → 低分/revise
- **步骤级成本分析时**：是否有 `cost_instrumentation`（度量定义、挂 Step ID、可执行采集方法、聚合、缺失策略）？只有粗估表 → 低分/revise
- 5 分：baseline 合理且跨任务/异构可比协议齐全（含定义统一审查）；1 分：straw man 或无协议/无定义统一就硬比

### 4. Research plan 完整性 (plan_completeness)

research_plan 5 件套齐全吗？**本维以启发性判断为主**，不要用学科关键词白名单硬卡。

- `experimental_design`（hypothesis ↔ experiment 一对一映射 + controls/treatments/sample_size/metrics）
- `computational_workflow`（**mermaid 流程图** + 逐步任务表：Step ID 与 resource_estimates 可对应；**多前置汇合**写 `S4, S5`，**一对多分叉**在 mermaid 与任务表双向一致）
- `baselines`（baseline 列表 + reason）
- **`comparison_protocol`（跨任务/跨场景时必填）**：common_sample / common_denominator / comparable_ops / env_failure_policy / **unified_definitions**；异构另加 behavior_alignment
- **`resource_feasibility`（高成本资源承诺时必填）**：commitments 清单；confirmed/assumed+降级/deferred 非核心
- **`cost_instrumentation`（步骤级/计算成本分析时必填）**：metric_definition / collection_points（Step ID）/ collection_method / aggregation / missing_policy；仅有 resource_estimates 不够
- `resource_estimates`（compute / 时间 / 数据估算 —— 粗也行；行与 computational_workflow Step ID 对应）
- `risk_analysis`（预想 dead_end + mitigation）
- 缺一项算 minor，缺 2+ 算 major
- 5 分：5 件套都齐且每项有实质内容（不是 "TBD" 占位）；computational_workflow 缺 mermaid 或任务表 → ≤3 分

**computational_workflow 启发性审稿清单**（Agent 判断，非代码硬规则）：
- 任务类型是否**匹配研究领域与用户约束**？（材料 DFT、统计 bootstrap、ML 训练等均可；勿要求出现 geometry_relax/phonon/AIMD/掺杂 等特定词）
- 步骤是否足以覆盖各 hypothesis 的 observables / falsifier？多样性与深度是否合理？
- 是否存在明显空转步骤、与约束冲突的禁做任务、或把资源估算表冒充 workflow？
- 模型尺度 / 方法选择是否写了依据（可追溯），跨步变更是否显式？
- 机械结构（mermaid + 可审计任务行 + DAG）以 `hypothesis_output_validation` / `audit_computational_workflow` 为准；本维重点看**领域适配与科学合理性**

### 5. 诚实 (honesty)

hypothesis 是否实事求是地标 confidence + 预想失败方式？

- 看每个 hypothesis 是否标 `predicted_outcome`（如果是"明显支持"这种自信预测，risk_analysis 必须对应有"如果反向证伪我们怎么解读"）
- 看 `risk_analysis` 是否真预想 dead_end（而不是套话"实验可能失败"）
- 看是否有"我们也可能错"段（partial null / dual prediction）
- **绝不**预设结论（"我们将证明 X"= overclaim 红线）

### 6. 非结论复述 (not_conclusion_restatement)

hypothesis 是真**可证伪预测**，还是把 survey finding / paper 结论改了个说法？

- 读 `hypothesis_conclusion_audit`：`flagged` 非空 → 1 分（critical）
- 读 `hypothesis_innovation_report`：最终 hypothesis 的 R≥4 或 tier=minimal → ≤2 分
- 比对 survey_report 的「关键 finding」段：hypothesis claim_text 是否只是 finding 的
  paraphrase（无新条件 / 新机制 / 新预测）？
- 5 分：每条假设都有**非显然预测**（P≥3），且与 paper finding 有清晰 scope 或机制区分
- 1 分：假设实质 = 论文已报告结论（如「方法 A 比 B 好」而文献早已证明）

**纠错信号**：若假设与 KB validated claim 方向相反，producer 是否写了
`I disagree with claim_<id> because ...`？没写但方向相反 → major concern。

### 7. 数据完整性 (data_provenance)

pre_registration 跟 research_plan 引用的 KB 内容是否都能追溯？

- 抽查 3-5 个 `claim_<8hex>` 引用 → `get_kb_record` 验
- 抽查 3-5 个 `concept_<8hex>` 引用 → `get_kb_record` 验
- phantom 引用 = critical 红线
- pre_registration 必须 frozen（`metadata.frozen=true`）—— 这是科研诚信底线
- pre_registration 必须走过 `freeze_artifact` 拿到它返回的 chunk_id，且至少 1 个 hypothesis claim 引用其 `prereg_chunk_id`

## 评分规则

- 每维度 1-5 整数
- **N/A 的维度不计入平均**（v0.5：没有命题的研究其 `falsifiability` 天然 N/A；
  把它当 0 分或当 5 分都是错的 —— 前者惩罚合法的研究形态，后者白送分）
- `overall_score` = **实际打了分的维度**平均，**整数取整**
- `verdict` 由 overall_score 推：
  - ≥ 4: `approve`
  - = 3: `approve_with_revisions`
  - = 2: `major_concerns`
  - ≤ 1: `block`

## 强制 metadata 字段（reviewer 存 review_critique 时必填）

```json
{
  "verdict": "approve | approve_with_revisions | major_concerns | block",
  "overall_score": <1-5 int>,
  "per_dimension_scores": {
    "declaration_honesty": <int>,
    "falsifiability": <int | "N/A">,
    "scope_specificity": <int>,
    "baseline_reasonableness": <int>,
    "plan_completeness": <int>,
    "honesty": <int>,
    "not_conclusion_restatement": <int>,
    "data_provenance": <int>
  },
  "n_concerns": <int>,
  "n_critical_concerns": <int>,
  "recommended_action": "proceed | revise | redirect_upstream | abort | escalate_to_human"
}
```

## Recommended action 怎么填

| verdict | 常见 recommended_action | 何时不一样 |
|---|---|---|
| approve | `proceed` | 几乎总是 |
| approve_with_revisions | `proceed` 或 `revise` | minor → proceed；falsifiability/scope 影响 experiment 设计 → revise |
| major_concerns | `revise` | target_node='hypothesis'，feedback 给具体哪条 hypothesis 不可证伪 / scope 模糊 |
| block | `revise` 或 `escalate_to_human` | hypothesis 没有可退回的上游（文献检索是**服务**，不是欠审查的节点）。基于错的 survey → `revise`，target_node='hypothesis'，feedback 写清要重新检索什么再重提；方向本身 user 该重定 → escalate |

## 跟邻居的边界

- _reviewer **不重写 hypothesis**（不调 create_claim） —— 那是 hypothesis 节点的活
- _reviewer **不直接改 pre_registration**（frozen，谁都不能改；要新版要改写新 hypothesis）
- _reviewer **可写 review_critique + memory（observation）+ propose_to_curator**

## 跟 quality_check 的区别

hypothesis 节点把 `validate_hypothesis_outputs` 接到框架硬门：
`completion_criteria.quality_checks` 含机械项
`hypothesis_output_validation_passed`（读 `hypothesis_output_validation.metadata.passed`）
与 `prereg_frozen`。节点内自检失败或未跑 → **status=incomplete**，不会假完成。

producer 结束前仍应显式调 `validate_hypothesis_outputs`；loop 结束时
`hypothesis_auto_validation` hook 会在缺失/失败时补跑一次，供 QC 读取最新报告。

本 review_spec 是 **deep** 检查（看 falsification 真不真机械可判、baseline 选得合不合理、
是否结论复述、research_plan 内容是否实质、workflow 是否适配领域——**启发性判断，不用学科关键词白名单**）。
节点内自检只做结构门禁（五件套 / mermaid / 可审计任务行 / DAG）；领域适配交给本 review。
节点内自检 + 本 review 都过 = 假设设计可信。
