---
name: hypothesis_novelty_assessment
description: |
  科学假设创新性（HIF）量化评估框架。当 hypothesis 节点在 finalize 候选假设、
  写 pre_registration 之前，或 user 要求比较多个 hypothesis 的创新程度时按本指引做：
  对每条假设在 KB/文献背景下打 5 维分，用 HIF 公式算出 0–100 创新指数并分级。
  不要用在：prereg 已 freeze 之后（假设已锁定）；也不要替代 falsifiability_pretest
  —— 创新性高但不可证伪的 conjecture 仍须重写。
applies_when:
  - 候选 hypothesis 已起草，准备写入 pre_registration 之前
  - 有 2+ 个候选 hypothesis 需要比较创新程度、决定保留哪些
  - user 明确要求评估假设创新性 / novelty / 是否 incremental
  - 读完上游 survey_report 的 open_questions 后，要验证假设是否真针对 gap
tools_used:
  - search_kb
  - get_kb_record
  - memory_recall
  - write_scratchpad
  - score_hypothesis_innovation
expected_outcome: hypothesis_innovation_report artifact + 每条 hypothesis 的 HIF 评分卡
status: validated
relevant_concepts: []
---

# 科学假设创新性评估（HIF）

## 用 / 不用 本 skill

**用 when**：
- 已起草 1–3 条候选 hypothesis，freeze 前想量化比较创新程度
- survey_report 有 open_questions，需确认假设是否真填补 gap 而非复述已知
- user 问"这个假设够不够新 / 是不是 incremental"

**不用 when**：
- prereg 已 freeze → 不能回头改假设，只能开新版 hypothesis
- 假设还缺 falsification_criteria → 先走 `falsifiability_pretest`
- 纯实验设计 / baseline 选择问题 → 那是 research_plan 范畴

## HIF 公式（Hypothesis Innovation Formula）

对**每条** hypothesis 打 **5–7 个**维度分，每个 **0–5 整数**（不允许 2.5）：

| 维度 | 代号 | 权重 | 问什么 |
|------|------|------|--------|
| Gap 覆盖度 | **G** | 0.25 / 0.22* | 是否直接回应 survey/KB 中记录的 open question 或 literature gap？ |
| 概念偏离度 | **D** | 0.20 / 0.18* | 与 KB 已有 validated claim 的概念距离有多远？ |
| 机制新颖性 | **M** | 0.25 / 0.22* | 因果/解释结构是否提出新机制或新组合？ |
| 预测非显然性 | **P** | 0.20 / 0.18* | 若成立，领域专家会意外吗？ |
| 非冗余度 | **N** | 0.10 | N = 5 − R，R 是与 KB 重复程度（0=无重复，5=几乎复述 validated claim） |
| Plausibility | **Q** | gate | 机制/物理是否合理？**Q≤1 → plausibility_reject，不可 prereg** |
| Impact | **I** | 0.10* | 若成立，对 open question 推进多大？（Co-Scientist expert rubric）|

\* 含 I 时用 extended 权重；不含 I 时用 legacy 权重。

**合成公式**：

```
legacy:  HIF = round( 100 × (0.25·G + 0.20·D + 0.25·M + 0.20·P + 0.10·N) / 5 )
extended: HIF = round( 100 × (0.22·G + 0.18·D + 0.22·M + 0.18·P + 0.10·N + 0.10·I) / 5 )
```

**约束规则**（机械执行）：
- 若 **Q ≤ 1** → `plausibility_reject=true`，HIF 上限 **24**，tier=minimal，**不可 prereg**
- 若 **N ≤ 1**（高冗余）→ HIF 上限 **40**（incremental 封顶）
- 若 **G ≤ 1** 且 **D ≤ 2** → HIF 上限 **35**（无 gap + 低偏离 = 复述）
- 若 **M ≥ 4** 且 **P ≥ 4** 且 **N ≥ 3** → 标记 `paradigm_flag=true`（潜在颠覆性，需 extra falsifiability 审查）

**分级（tier）**：

| HIF 范围 | tier | 含义 |
|----------|------|------|
| 0–24 | `minimal` | 极低：已知结论的直接复述或 trivial extension |
| 25–44 | `low` | 低：minor extension，有测的价值但创新有限 |
| 45–64 | `moderate` | 中：合理新颖，填补局部 gap |
| 65–79 | `high` | 高： substantial innovation，值得 prereg |
| 80–100 | `transformative` | 颠覆性：挑战常规或跨域合成，须 extra 证伪审查 |

## 各维度打分 rubric

### G — Gap 覆盖度
- **5**：直接回答 survey open_question 或 KB `status=open` 的 claim
- **4**：针对 survey 识别的 gap，但非最核心 open question
- **3**：间接相关 gap，需额外论证关联
- **2**：gap 模糊，主要靠"没人做过"而非"为什么重要"
- **1**：无明确 gap，假设自说自话
- **0**：假设与已知 validated 结论同方向，无新 gap

### D — 概念偏离度
- **5**：引入 KB 中不存在的新概念/关系/跨域桥接
- **4**：已知元素的新组合，KB 无先例
- **3**：已知方法的非显然新应用
- **2**：标准应用的参数/数据集变化
- **1**：与现有 claim 几乎同义改写
- **0**：字面重复 validated claim

### M — 机制新颖性
- **5**：新因果机制或反直觉解释路径
- **4**：已知机制在新 regime/dataset 的非显然延伸
- **3**：消融/对照揭示的新中间机制
- **2**：标准 pipeline 的小改动
- **1**：纯 phenomenological，无机制主张
- **0**：机制与已有 claim 完全相同

### P — 预测非显然性
- **5**：与领域共识相反或给出 sharp 定量预测
- **4**： plausible 但文献未系统检验
- **3**：方向合理但幅度/条件非显然
- **2**：多数专家会猜到的方向
- **1**："X 比 Y 好"类无 surprise 比较
- **0**：预测是已知事实的 corollary

### R / N — 冗余度（R 打分，N = 5 − R）
- **R=0 → N=5**：KB 检索无相关 validated claim
- **R=2 → N=3**：部分 overlap，但 scope 或 prediction 不同
- **R=4 → N=1**：核心主张与 validated claim 高度重叠
- **R=5 → N=0**：几乎复述已有 validated 结论

### Q — Plausibility（Co-Scientist Reflection gate）
- **5**：机制与已知物理/化学/领域共识完全一致，假设合理
- **4**： plausible，有小不确定性但可实验检验
- **3**：边界 plausible，需额外 justification
- **2**：机制薄弱或 evidence 不足
- **1**：明显 implausible 或与 validated claim 冲突且无 scope 差异
- **0**：物理/逻辑上不可能
- **Q≤1 → plausibility_reject，必须 evolve 或丢弃**

### I — Impact（Co-Scientist expert rubric）
- **5**：若成立，显著推进核心 open question 或开创新方向
- **4**： substantial 推进，影响多个 downstream 实验
- **3**：填补局部 gap，有明确增量价值
- **2**： incremental，测了有用但影响有限
- **1**： trivial extension
- **0**： 无实质影响

## 结论 vs 假设（R 维度必读）

打 R 分前，**必须**区分「论文已报告的 finding」和「待检验的 hypothesis」：

| 信号 | R 建议 | 处理 |
|------|--------|------|
| claim_text 与 survey「关键 finding」词汇重叠 >55% | R≥4 | 回到 `divergent_hypothesis_generation` 重写 |
| 只是把 paper 结论改成「X 比 Y 好」无新条件 | R=5, P≤1 | 丢弃，不是 hypothesis |
| 加了新 regime / metric / mechanism 使预测非显然 | R≤2 | 可保留 |
| 与 validated claim 同方向同 scope | R≥3 | 考虑改 scope 或换 gap |

**推荐**：HIF 打分前先调 `audit_hypothesis_vs_conclusions`；其 flagged 结果应写入
`kb_overlap` 字段。

## 工作流

1. **读上游 gap**：`read_artifact` 读 survey_report 的 open_questions + 关键 finding；`search_kb()`（不传 entity_type）看 KB 大势。
2. **结论审核**：`audit_hypothesis_vs_conclusions(hypotheses=[...])` —— flagged 的先剔除再打分。
3. **逐条检索冗余**：对每条 hypothesis 的 `claim_text`，调 `search_kb(entity_type='claims', query=<claim_text>)` + `get_kb_record` 查 validated/open claim。
4. **打 5–7 维分**：按上表 rubric 给 G/D/M/P/R/Q/I（整数 0–5），算 N = 5 − R。**Q/I 推荐必填**。
5. **算 HIF（调工具）**：`score_hypothesis_innovation(assessments=[...])` —— 自动算分并写 `hypothesis_innovation_report` artifact。
6. **决策**（可选写 scratchpad 摘要）：
   - Q ≤ 1 或 plausibility_reject → **evolve 或丢弃**
   - HIF < 25 → **重写或丢弃**（除非 user 明确要求测 trivial baseline）
   - 2+ 假设 HIF 差距 > 30 → 优先保留高 HIF 的（≤3 条总数）
   - tier=transformative → 额外走 `falsifiability_pretest` 确保可证伪

## 快速验算（CLI，可选）

```bash
python nodes/hypothesis/skills/hypothesis_novelty_assessment/scripts/hif_score.py \
  --g 4 --d 3 --m 4 --p 3 --r 1
# → HIF=72 tier=high
```

JSON 批量：
```bash
python nodes/hypothesis/skills/hypothesis_novelty_assessment/scripts/hif_score.py \
  --json examples/batch_cases.json
```

## Pitfalls

- ❌ 把"没人做过"当创新性 —— 没 gap 的首次尝试 G 仍低
- ❌ 创新性高就跳过 falsifiability —— transformative 更需要 sharp falsifier
- ❌ 只凭 LLM 直觉打分不查 KB —— 必须 `search_kb` 验冗余
- ❌ 把 paper finding 当 hypothesis 还打低 R —— 应直接丢弃而非勉强 prereg
- ❌ 用 HIF 替 user 做方向决策 —— HIF 是辅助，方向由 research question 定
- ✅ KB 无条目时 R=0/N=5 合理，但 G 仍须靠 survey open_questions 支撑
- ✅ 两条假设 HIF 接近时，优先选 falsifiability 更机械可判的

## 完整例子

**场景**：LJ NVT thermostat 比较，survey 发现 open question "NHC vs NH 收敛速率在 dense regime 无系统比较"。

```
# Step 1: 查 KB
search_kb(entity_type='claims', query='NHC NH convergence LJ NVT')
# → 找到 claim_xyz validated "NHC energy drift < NH on ρ=0.8" — partial overlap

# Step 2: 评估 H1
# "NHC 在 ρ=0.85 LJ NVT 上 wall-time-to-equilibrium 比 NH 快 30%"
# G=5 (direct open question), D=2 (standard comparison), M=2 (no new mechanism)
# P=3 (magnitude non-obvious), R=2 (partial overlap) → N=3
# HIF = round(100*(0.25*5+0.20*2+0.25*2+0.20*3+0.10*3)/5) = round(100*3.05/5) = 61
# tier=moderate

# Step 3: 评估 H2 (更高创新)
# "NH 链长 N 与 equilibration time 呈 power-law τ∝N^α，α 在 NHC/NH 间不同"
# G=4, D=4, M=4, P=4, R=0 → N=5
# HIF = round(100*(0.25*4+0.20*4+0.25*4+0.20*4+0.10*5)/5) = round(100*4.1/5) = 82
# tier=transformative, paradigm_flag=true → extra falsifiability check

# Step 3: 调工具（自动写 hypothesis_innovation_report）
score_hypothesis_innovation(assessments=[
  {"label": "H1", "claim_text": "NHC ... 快 30%", "G": 5, "D": 2, "M": 2, "P": 3, "R": 2,
   "kb_overlap": "claim_xyz partial overlap"},
  {"label": "H2", "claim_text": "τ∝N^α ...", "G": 4, "D": 4, "M": 4, "P": 4, "R": 0},
])
# → artifact hypothesis_innovation_report: H1 HIF=61 moderate, H2 HIF=82 transformative
```
