---
name: computational_workflow_design
description: |
  计算/仿真工作流设计通用框架。写 research_plan 的 computational_workflow 时，
  确保每步的**建模尺度、方法选择、跨步变更**对读者可追溯——不预设某一学科的具体参数模板。
applies_when:
  - 起草或修订 research_plan 的 computational_workflow / 任务表 / mermaid 流程图
  - audit_computational_workflow 报一致性/可追溯性错误
  - 需要把 experimental_design 的逻辑落到可执行步骤表
tools_used:
  - audit_computational_workflow
  - save_artifact
  - write_scratchpad
  - read_reference_paper
  - search_kb
expected_outcome: 任务表每步可追溯；跨步模型变更显式；validate 的 computational_workflow_coherence 通过
status: validated
relevant_concepts: []
---

# 计算工作流设计（通用）

## 核心原则（学科无关）

1. **一步一问题**：每行 task 对应一个可独立提交的 job，且能回答「本步要得到什么 observable / artifact」。
2. **建模选择可追溯**：凡偏离「默认/最小模型」的选择（更大超胞、更粗网格、不同边界条件等），必须在**关键参数**或**模型尺度列**写依据——依据来自文献、上游 survey 或假设本身，不是套模板。
3. **跨步变更要显式**：若 Step B 相对前置 Step A 改变了模型尺度或离散化，必须二选一：
   - 插入 **bridge 步骤**（postprocess / transform / remap / merge …），或
   - 在 B 的**关键参数**中文字说明变更原因与操作。
4. **图文一致**：mermaid 节点标签与任务表同一 Step ID 的表述应一致（尤其模型尺度、方法名）。
5. **跨任务可比**：若 workflow 服务跨任务/跨场景对比，research_plan 须另有 `## comparison_protocol`
6. **资源可执行**：若 workflow 依赖多模型/多人标注/多完整 benchmark，须另有 `## resource_feasibility`
7. **成本采集**：若假说要分析步骤级计算成本，须另有 `## cost_instrumentation`（挂 Step ID + 可执行采集方法）
   （共同样本 / 共同分母 / 可比较操作 / 统一故障处理）。异构 benchmark 或复杂度·操作比例对比时，
   须有 `unified_definitions`（指标/定义是否统一）；异构另须 `behavior_alignment`；禁止未对齐硬比。

> **不要**从示例或别的项目复制超胞/网格数字；**要**从本研究的 falsifier 反推需要何种分辨率和模型尺度。

## 决策流程（写 task 表前）

```
1. 从 pre_registration / experimental_design 列出每个 hypothesis 需要的 observables
2. 对每个 observable 问：
   - 最小足够模型是什么？（默认选项）
   - 何种误差来源会 invalidate 结论？→ 决定是否放大模型/改边界条件
3. 画 DAG：结构/模型构建 → 核心计算 → 后处理 → 与 falsifier 对齐的汇总
4. 填表：Step ID | 任务类型 | 前置 | 模型尺度 | 关键参数 | 产出 | 对应 falsifier
5. audit_computational_workflow → 按 issues 修订 → save
```

## 任务表列（推荐）

| 列 | 作用 |
|----|------|
| Step ID | 与 mermaid 节点一致 |
| 任务类型 | 动词+对象（geometry_relax / sampling / simulation / postprocess …） |
| 前置步骤 | DAG 依赖 |
| 模型尺度 | 超胞/网格/ensemble 大小等；默认可写「原胞」「unit cell」「baseline mesh」 |
| 关键参数 | **本步依据 + 主要设置**（这是专业性的主要载体） |
| 产出 | 下游可消费的 artifact |
| 对应 falsifier | 链到 pre_registration 的可检验结论 |

## 专业性从哪里来？

| 层级 | 机制 | 谁负责 |
|------|------|--------|
| 知识 | survey、reference paper、KB claims | LLM 阅读后写入关键参数 |
| 结构 | audit / validate 检查可追溯性与跨步一致性 | 代码规则（不含学科硬编码） |
| 收敛 | validate_hypothesis_outputs 不过则不结束 | 门禁 |

代码**不会**判断「phonon 必须 2×2×2」这类学科断言；它会判断「你选了 2×2×2，有没有说为什么」。

## 工作流（与 harness 衔接）

1. scratchpad：observable → 最小模型 → 潜在误差来源。
2. 写 mermaid + 任务表。
3. **`audit_computational_workflow(content=...)`** —— 修 issues。
4. `save_artifact(artifact_type='research_plan', name=..., content_from_file=...)`。
5. `validate_hypothesis_outputs()`（含 `computational_workflow_coherence`）。

## 示例（仅说明格式，数值不可照搬）

材料 DFT 只是**一种**可能形态；分子模拟、FDM、ML 训练同理——关键是列齐「模型尺度 + 依据 + falsifier 链接」。

| Step ID | 任务类型 | 前置 | 模型尺度 | 关键参数 |
|---------|---------|------|---------|---------|
| S1 | geometry_relax | - | unit cell | 依据：bulk 平衡结构；ISIF=3，力收敛<0.01 eV/Å |
| S2 | phonon | S1 | 2×2×2 | 依据：消除有限尺寸虚频；DFPT q-mesh … |
| S9 | postprocess | S8 | - | 依据：AIMD 末段平均结构供 S4 静态计算 |

## Pitfalls

- 从 harness 示例复制 `1×1×3` 而不写依据 → audit 会拦。
- 关键参数只写软件名/泛函名、不写**为什么这一步需要这些设置** → 专业性不足。
- mermaid 与任务表模型尺度不一致 → warning。
- 大段正文用 `content_b64` 保存，避免 JSON 转义问题。
