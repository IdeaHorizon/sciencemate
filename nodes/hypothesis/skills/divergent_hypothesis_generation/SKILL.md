---
name: divergent_hypothesis_generation
description: |
  多论文 / 多视角发散思维框架。当 hypothesis 节点读完 survey_report 后，需要避免
  「只围绕单篇论文结论打转」、专业度不足、或假设方向与领域共识相反时，按本 skill
  做发散→对比→收敛。不要用在 prereg 已 freeze 之后。
applies_when:
  - survey_report 已读，准备起草候选 hypothesis 之前
  - 上游 literature 只 ingest 了 1–2 篇论文，担心思维不够发散
  - user 反馈「假设太 incremental / 像论文结论复述 / 专业方向可能反了」
tools_used:
  - read_artifact
  - read_reference_paper
  - search_kb
  - get_kb_record
  - get_research_goal
  - cluster_hypothesis_candidates
  - evolve_hypothesis
  - write_scratchpad
  - audit_hypothesis_vs_conclusions
  - score_hypothesis_innovation
expected_outcome: 3–6 条发散候选 → 收敛为 1–3 条可证伪 hypothesis
status: validated
relevant_concepts: []
---

# 发散式假设生成（Divergent → Convergent）

## 核心问题

单篇论文 survey 后常见三类失败模式：

| 模式 | 症状 | 对策 |
|------|------|------|
| **结论复述** | 把 paper finding 改写成 hypothesis；HIF 的 R 高、P 低 | 走 `audit_hypothesis_vs_conclusions`；强制「反事实 / 跨论文」路径 |
| **单源依赖** | 假设只回应 1 篇论文的 1 个 gap | 按 paper/chunk 分组，每源至少 1 条不同机制路径 |
| **专业方向偏差** | 假设与领域 validated claim 或共识相反，但未显式论证 | `search_kb` + 写 disagreement；必要时 `consult_other_model` |

## 工作流（必做顺序）

### Phase 0 — 多源拆解（≥2 篇论文或 ≥2 个 chunk）

0. `get_research_goal()` —— 确认 goals / constraints / **`must_cover_themes`** / **`locked_definitions`**，**同时读 `user_prompt` + `dialogue_context`**。
1. 在 scratchpad 写清：
   - `user_prompt` 一句话复述
   - **`must_cover_themes` 完整列表**（防退化锚点）
   - **`locked_definitions` 类别表**（防改写锚点：label + 定义原文）
   - 对话里的禁做/只用约束（`inferred_constraints`）
2. `read_artifact` 读 `survey_report` 全文（若有）。
3. 从 survey 提取 **per-paper 结构**（写在 scratchpad）：
   - `paper_A`: core_method, main_finding, limitation, open_question
   - `paper_B`: ...
4. 若 survey 只覆盖 1 篇论文：
   - **`read_reference_paper(doi=...)`** 读原文 abstract/conclusions（必读 primary paper）
   - `search_kb(entity_type='claims', query=<topic>)` 找 validated/open claim
   - `get_kb_record` 读相关 concept / claim
   - **不要** `search_kb(entity_type='chunks')` —— 向量索引不支持
   - 把 survey 内 **不同 finding** 当作多来源，至少 2 条路径来自不同 gap/finding
5. **防退化 + 防改写对齐检查**：
   - 后续每条候选必须标注 `serves_pillars: [与 must_cover 的交集]`
   - **禁止**只跟 survey 最锋利的单一 open_question 走，而丢掉其他 must_cover
   - **禁止**用文献 taxonomy 替换 `locked_labels`；claim 中的操作类别只能用用户原文名
   - 与对话约束冲突的路径直接丢掉

### Phase 1 — 发散（目标 3–6 条候选，允许暂时不可证伪）

对**每个来源**各走 3 条思维路径（写在 scratchpad，不必立刻写 KB）：

| 路径 | 问什么 | 示例 |
|------|--------|------|
| **A. Gap 延伸** | survey open_question 的 direct test | 「NHC 比 NH 收敛快 25%」 |
| **B. 机制迁移** | 把 paper A 的机制用到 paper B 的场景 | 「paper A 的 XX 机制在 paper B 的 regime 是否成立」 |
| **C. 反直觉 / 对立** | 若论文说 X>Y，能否构造 plausible 的 Y>X 条件？ | 「在 sparse regime，single NH 反而更快」 |
| **D. 跨域类比** | 其它领域类似问题怎么解？ | 「借鉴 RL exploration bonus 改 GA fitness」 |
| **E. 边界条件** | 论文结论在什么 scope 外失效？ | 「ρ<0.5 时 NHC 优势消失」 |
| **F. 支柱补全** | must_cover 中尚未被候选服务的支柱 | 「成本分析支柱尚无候选 → 补一条 cost-latency tradeoff」 |

**发散阶段规则**：
- 至少 **2 个不同来源** 各贡献 ≥1 条候选（若无 survey，则「用户 prompt 的子目标」+「对话约束下的可检验预测」可作两源）
- 至少 **1 条** 来自路径 C 或 D（强迫跳出论文结论）——但仍须落在 user_prompt 范围内
- **每个 must_cover 支柱**至少被 1 条候选的 `serves_pillars` 覆盖（路径 F）；否则不得进入收敛
- 每条候选写 **assumption_tree**（2–4 条，标注 fundamental）
- 在 scratchpad 维护 **pillar×candidate 覆盖表**，收敛前后都要全绿
- `consult_other_model` **不要用**（本环境单 provider）；用 `memory_recall` 补专业度
### Phase 1.5 — 去重聚类（Co-Scientist Proximity）

≥3 条候选时：

```
cluster_hypothesis_candidates(hypotheses=[{label, claim_text}, ...])
```

- `duplicates_to_merge` 非空 → 合并或只保留 `representatives`
- 高 overlap 但未达阈值 → 考虑 `evolve_hypothesis(mode=synthesize)` 合成而非保留两条

### Phase 2 — 专业度校正（KB + 纠错）

对 Phase 1 每条候选：

1. `search_kb(entity_type='claims', query=<claim_text>)` —— 找 validated / open claim
2. 若与 validated claim **方向相反**：
   - 不要静默丢弃；在 scratchpad 写：
     `I disagree with claim_<id> because <scope + mechanism reasoning>`
   - 只有能给出 **scope 差异** 或 **新机制** 时才保留对立假设
3. 若 KB 为空（新项目）：`memory_recall` 查跨项目类似 task 的 falsification / baseline 经验
4. 若仍不确定专业方向：`request_human_input` 问 user「该领域共识是否支持此方向？」

### Phase 3 — 结论审核（硬门）

在收敛到 1–3 条之前：

```
audit_hypothesis_vs_conclusions(
  hypotheses=[{label, claim_text}, ...],   # Phase 1 全部候选
  overlap_threshold=0.55,
)
```

- `flagged` 非空 → **必须** `evolve_hypothesis` 重写或丢弃，不可进入 HIF / prereg
- 重写策略：加 **非显然条件**（regime / metric / mechanism）提高 P 分，而非改几个词

### Phase 3.5 — Deep Verification（assumption 分解）

对通过 audit 的候选，在 scratchpad 或 pre_registration 写：

```
assumption_tree:
  - text: "..."
    fundamental: true   # 若错则整个 hypothesis 失效
  - text: "..."
    fundamental: false    # 错则可 assumption_repair evolve
```

non-fundamental 错误 → `evolve_hypothesis(mode=assumption_repair)`，不必丢弃。

### Phase 4 — 收敛 + HIF

1. 从通过审核的候选中选 1–3 条（优先：可证伪性高 + HIF 高 + 来源多样 + **must_cover 全覆盖**）
2. 若收敛后覆盖表有红格 → **先补候选 / synthesize**，禁止为冲 HIF 删支柱
3. 对每条补全 `falsification_criteria_structured`：
   - 数值比较：含 **threshold_rationale**（先依据后数字；禁止拍脑袋比例）
   - 定性/存在性：`comparison=qualitative|exists|not_exists` + 清晰可观察判据（勿硬编伪精确数字）
4. `score_hypothesis_innovation(assessments=[...])` —— R 必须基于真实 `search_kb` 结果；**必须填 Q/I**
5. HIF < 25 或 R ≥ 4 或 Q ≤ 1 → 回到 Phase 1 或 `evolve_hypothesis`，不要硬 prereg
6. `save_artifact`(pre_registration) —— **仅草稿，禁止此时 freeze**
   - `metadata.capital_basis` 必须是 claim id **数组**或 `"none_found"`；❌ 禁止 `{"item":[…]}`
7. 完成 research_plan（含 `audit_computational_workflow`）后，**freeze 前**依次：
   - `audit_user_goal_alignment()` —— passed=false 改未冻结 prereg/plan 后重跑，不得 freeze
   - `audit_definition_fidelity()` —— passed=false 恢复用户原文后重 save，不得 freeze
   - `audit_threshold_grounding()` —— passed=false 补依据/改 comparison 后重 save，不得 freeze
   - `audit_comparison_protocol()` —— 跨任务必过基础字段；须含 unified_definitions；异构另须 behavior_alignment
   - `audit_resource_feasibility()` —— 高成本承诺须 confirmed/assumed+降级/deferred 非核心
   - `audit_cost_instrumentation()` —— 步骤级成本须有采集方案（非仅粗估）
8. **9b–9g 适用审计全部 passed 后**才 `freeze_artifact`（返回里带 chunk_id）→ `create_claim`
   - freeze 若反复报 capital_basis 未声明：立刻改正确形状，或先写 overview，禁止空转多轮
9. 写 overview → `validate_hypothesis_outputs()`

### Phase 5 — Meta-review overview

收敛后写 `hypothesis_research_overview`：
- Top 1–3 假设一句话摘要 + 为何选它们
- **must_cover 覆盖说明**（每支柱 → 哪条 H / 哪段 plan）
- **definition_lock 确认**：locked_labels 均未改名；文献 taxonomy 仅作对照
- 被淘汰候选及原因（audit/HIF/cluster）——注明「未因冲 HIF 丢掉支柱 / 未改用户分类」
- 未探索方向（下一轮建议）
- freeze 卡住时仍须优先交 overview（required_output），勿把轮次耗尽在 freeze 上

## 快速检查清单

- [ ] **完成闸优先**：required（research_state / prereg / research_plan / HIF / overview）齐 + `validate_hypothesis_outputs` passed=true；额外 audit 不得挤占轮次
- [ ] `get_research_goal` 已调，`must_cover_themes` + `locked_definitions` 已写入 scratchpad
- [ ] pillar×candidate 覆盖表在发散与收敛后均全绿
- [ ] prereg 含 `## definition_lock`，类别名与定义与用户原文一致
- [ ] ≥2 个独立来源（paper/claim/KB/用户子目标）被显式引用
- [ ] ≥1 条候选来自反直觉/跨域路径（C 或 D），且仍服务 user_prompt
- [ ] ≥3 候选时 `cluster_hypothesis_candidates` 已调
- [ ] 每条候选有 assumption_tree（≥1 fundamental）+ `serves_pillars`
- [ ] `audit_hypothesis_vs_conclusions` 已调且 passed
- [ ] `audit_user_goal_alignment` 已调且 passed（**freeze 前**）
- [ ] `audit_definition_fidelity` 已调且 passed（有锁定分类时；**freeze 前**）
- [ ] `audit_threshold_grounding` 已调且 passed（数值 threshold 有依据；或 qualitative/exists 判据清晰；**freeze 前**）
- [ ] 跨任务时 `comparison_protocol` 含 unified_definitions；异构时另有 behavior_alignment
- [ ] `audit_comparison_protocol` passed（**freeze 前**；禁止 API/reasoning 与点击/输入未对齐硬比）
- [ ] 高成本资源时 `resource_feasibility` 齐全；`audit_resource_feasibility` passed（**freeze 前**）
- [ ] 步骤级成本时 `cost_instrumentation` 齐全；`audit_cost_instrumentation` passed（**freeze 前**）
- [ ] 适用审计全部通过后才 `freeze_artifact` + `create_claim`
- [ ] 与 validated claim 对立时有显式 disagreement 理由
- [ ] 最终 ≤3 条，每条有机械 falsifier + scope_dimensions

## Pitfalls

- ❌ 审计（对齐/定义保真/阈值依据等）未通过就 `freeze_artifact` —— **硬禁止**（冻结后不可改）
- ❌ 把复杂多支柱诉求退化成 survey 里最好写 falsifier 的单一小点 —— **硬禁止**
- ❌ 把用户操作类别重命名/合并，或换成论文里的 taxonomy —— **硬禁止**
- ❌ 无文献/理论/先导依据就填比例阈值（随意 +20%/2×）—— **硬禁止**
- ❌ 跨任务比较未规定共同样本/分母/可比较操作/统一故障处理就横比 —— **硬禁止**
- ❌ 跨侧对比前未审查指标/定义是否统一，或异构未做行为映射就硬比 —— **硬禁止**
- ❌ 把发散候选直接当最终假设 —— 必须先审核再收敛
- ❌ 单篇论文 survey 不做 read_reference_paper —— 思维必然窄且易复述结论
- ❌ 专业方向错了也不写 disagreement —— curator 无法纠错
- ✅ KB 小 → 更依赖 read_reference_paper + memory_recall 补专业度
- ✅ 结论复述的假设 HIF 通常 R≥4、P≤2 —— 用 HIF 作二次验证
- ✅ HIF 高但缺支柱或改了分类 → 仍须补覆盖/恢复定义；保真优先于冲分
