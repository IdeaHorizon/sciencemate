# Agent 科研平台设计白皮书

> Version 0.1 | 2026-04-22
> 本文档是 Agent 科研平台的完整设计原则与架构规范。所有后续实现、论文写作、产品决策均以本文档为基准。

---

## 目录

1. [问题陈述与设计动机](#1-问题陈述与设计动机)
2. [平台定位与设计原则](#2-平台定位与设计原则)
3. [整体架构](#3-整体架构)
4. [Research Graph 形式化定义](#4-research-graph-形式化定义)
5. [最小公约节点与 Harness 设计](#5-最小公约节点与-harness-设计)
6. [知识库与记忆系统](#6-知识库与记忆系统)
7. [证据链与溯源体系](#7-证据链与溯源体系)
8. [Human-in-the-Loop 机制](#8-human-in-the-loop-机制)
9. [工具与权限管理](#9-工具与权限管理)
10. [Artifact 管理](#10-artifact-管理)
11. [Workspace 与交互设计](#11-workspace-与交互设计)
12. [协作模型](#12-协作模型)
13. [技术实现要点](#13-技术实现要点)
14. [MVP 范围与路线图](#14-mvp-范围与路线图)

---

## 1. 问题陈述与设计动机

### 1.1 科研工作流的四大系统性痛点

**痛点一：项目记忆无法沉淀。** 科研项目中大量隐性知识——为什么选了方法 A 而非 B、某次实验失败的真正原因、PI 在某次讨论中的关键判断——随着人员流动和时间推移不断丢失。现有工具（笔记软件、实验记录本、聊天记录）无法结构化保存这些信息，更无法在后续决策中自动调用。

**痛点二：科研流程不清晰。** 科研项目缺乏清晰的流程管理工具。文献调研做到什么程度算充分？实验设计是否遗漏了关键对照？分析结果是否存在统计偏差？这些问题在没有系统支持的情况下，完全依赖研究者个人经验和自律。

**痛点三：复杂项目容易跑偏。** 科研项目往往持续数月甚至数年，涉及多次迭代、多条探索路径。在长期自主推进中，AI agent 或研究者本人都可能逐渐偏离核心目标，在无关方向上投入大量资源。

**痛点四：缺乏组织级工具与知识管理。** 科研团队缺乏统一的工具权限管理、跨项目知识复用机制和组织级最佳实践沉淀。每个新项目几乎从零开始。

### 1.2 为什么现有方案不够

**阶段性工具（覆盖 1-2 个研究阶段，无全流程）：**
- **通用 AI 助手**（ChatGPT、Claude）：单次会话，无项目状态，无记忆沉淀，无证据链
- **AI 文献工具**（Elicit、Semantic Scholar、Consensus、SciSpace）：只覆盖文献检索/综合，无实验和分析能力
- **实验管理平台**（MLflow、W&B）：只覆盖 ML 实验追踪，不覆盖上下游流程，且仅限 ML 领域
- **AI 写作工具**（OpenAI Prism）：只覆盖写作阶段，无文献调研和实验支持
- **知识管理工具**（Zotero、Obsidian、Notion）：管理文献/笔记，但不连接计算和实验

**全生命周期 AI 科研系统（最直接竞品）：**
- **The AI Scientist v1/v2**（Sakana AI, 2024-2026, Nature 2026）：首个全自主 ML 科研 agent，但无持久记忆、无 Research Graph、无溯源体系、纯自主无 HITL
- **FutureHouse/Edison**（$70M 融资）：多专业 AI agent 科研发现平台，但面向生物/制药企业，非通用平台
- **Agent Laboratory**（EMNLP 2025）：HITL 科研助手，84% 成本降低，但无持久记忆、无 Graph 管理、无溯源
- **SciAgents**（Advanced Materials, 2024）：知识图谱驱动假设生成，最接近 Research Graph 概念，但仅限构思阶段

**核心空白：没有任何现有系统同时具备**全生命周期管理 + 分层记忆与知识库分离 + 结构化溯源 + HITL 机制。详见 `related_work.md`。

### 1.3 我们的定位

**不做模型微调和领域训练**——在各领域缺乏数据和算力优势，且当前通用模型加 agent 的范式已经足够强。

**聚焦科研流程工程化**——通过 Harness 设计、Memory 设计、Skills、Tool-call 设计、Context Engine 设计，将优秀的科研逻辑和流程固化为可复用的系统能力，提供给科研用户。

---

## 2. 平台定位与设计原则

### 2.1 双模式运行

平台支持两种运行模式，用户可按项目或按阶段切换：

**辅助模式（Assisted Mode）**
- 人主导，系统辅助执行和整理
- 系统在关键节点汇报进展、暴露问题、请求决策
- 所有 graph 生长和重大操作需用户确认
- 适用场景：高风险研究、新领域探索、PI 需要紧密掌控的项目

**全自主模式（Autonomous Mode）**
- 系统自主推进大部分流程
- 仅在系统判定的高风险关键节点暂停
- 低风险操作自动执行，中风险操作限时等待后按默认方案推进
- 适用场景：成熟流程的批量执行、明确定义的实验矩阵、低风险辅助任务

**不可自主执行操作黑名单**——无论何种模式，以下操作必须人工确认：
- 消耗超过预设阈值的算力
- 删除数据或 artifact
- 提交论文或外部发布
- 修改 Organization Memory 或 Organization KB
- 涉及有副作用的外部 API 调用
- 授予或变更权限

### 2.2 核心设计原则

| 原则 | 含义 |
|------|------|
| **Project 为一等公民** | 所有操作、记忆、权限、工具配置优先挂在 project 上 |
| **知识库与记忆分离** | 外部知识（KB）与内部经验（Memory）是不同系统，更新策略和信任等级不同 |
| **分级溯源** | 事实性陈述必须硬溯源，推理性结论允许软溯源；溯源要求优先于尽力完成 |
| **按阶段调节严谨性** | 探索阶段优先效率，验证阶段优先严谨 |
| **节点即 harness** | 每个最小公约节点有独立的 context engine、skills、tools、review 策略 |
| **Project 级 Agent** | Agent 本质是 project harness + 配置，不是每人一个独立 agent |
| **Workspace 承载状态** | 对话是入口之一，但项目真实状态由 Workspace 承载 |

---

## 3. 整体架构

### 3.1 三层节点体系

```
┌─────────────────────────────────────────────────┐
│  第一层：Organization                            │
│  组织级 Memory / KB / 工具注册 / 权限策略 / 规范   │
└──────────────────────┬──────────────────────────┘
                       │ 继承
┌──────────────────────▼──────────────────────────┐
│  第二层：Project                                  │
│  Research Graph / Project Memory / Project KB     │
│  工具配置 / Artifact / 汇报策略                    │
│                                                   │
│  ┌─────────────────────────────────────────┐     │
│  │  最小公约节点（Research Operations）      │     │
│  │  Survey│Planning│Experiment│Analysis│... │     │
│  │  每个节点 = harness + context + skills   │     │
│  └─────────────────────────────────────────┘     │
└──────────────────────┬──────────────────────────┘
                       │ 执行
┌──────────────────────▼──────────────────────────┐
│  第三层：Execution / Runtime                      │
│  LLM 调用 / Tool 执行 / 日志 / 审计              │
│  Session Memory / 临时状态                        │
└─────────────────────────────────────────────────┘
```

- 科学家主要看第二层和第一层
- 系统内部依赖第三层
- 第一层沉淀长期状态，第二层承载流程与可视化推进，第三层承载审计与调试

### 3.2 数据流架构

```
                    ┌──────────────┐
                    │  Knowledge   │
                    │    Base      │
                    │ (论文/新闻/  │
                    │  领域知识)   │
                    └──────┬───────┘
                           │ RAG 检索
                           ▼
┌──────────┐    ┌──────────────────┐    ┌──────────┐
│  Memory  │───▶│  Context Engine  │◀───│ Handoff  │
│  System  │    │  (按节点类型组装) │    │  摘要    │
└──────────┘    └────────┬─────────┘    └──────────┘
                         │
                         ▼
                ┌────────────────┐
                │  Node Harness  │
                │  (System Prompt│
                │   + Skills     │
                │   + Tools      │
                │   + Rules)     │
                └────────┬───────┘
                         │
                         ▼
                ┌────────────────┐
                │   Execution    │
                │   Runtime      │
                └────────┬───────┘
                         │
                    ┌────┴────┐
                    ▼         ▼
              ┌─────────┐ ┌──────────┐
              │Artifacts│ │  Memory  │
              │ (产物)  │ │  (经验)  │
              └─────────┘ └──────────┘
```

---

## 4. Research Graph 形式化定义

Research Graph 是平台的核心运行时结构，所有科研推进、状态追踪、决策管理都围绕它展开。

### 4.1 基本定义

一个 Research Graph 定义为四元组：

**G = (N, E, B, S)**

- **N** = 节点集合（Research Operation 实例）
- **E** = 有向边集合（节点间关系）
- **B** = 分支集合（并行探索路径）
- **S** = 快照集合（关键决策点的 graph 状态）

### 4.2 节点（Node）

每个节点 n ∈ N 是一个最小公约节点的具体实例，定义为：

```
Node = {
    id:          唯一标识符
    type:        Survey | Planning | Experiment | DataProcess |
                 Analysis | Writing | Review | Custom(string)
    status:      planned | ready | active | paused |
                 completed | failed | archived
    branch:      所属分支标识
    iteration:   该类型在当前分支中的第几次迭代（从 1 开始）
    inputs:      所需输入 artifact / context 的引用列表
    outputs:     产出 artifact 的引用列表
    config:      节点级配置（覆盖默认 harness 的参数）
    metadata: {
        created_at:    创建时间
        started_at:    开始执行时间（null if not started）
        completed_at:  完成时间（null if not completed）
        created_by:    创建者（user | system）
        owner:         负责人
        justification: 创建理由（尤其对于迭代节点，记录"为什么再做一次"）
    }
}
```

### 4.3 节点状态机

```
                 ┌──────────────────────────────┐
                 │                              │
                 ▼                              │
planned ──▶ ready ──▶ active ──▶ completed     │
              │         │                       │
              │         ├──▶ failed ────────────┘
              │         │       │        (用户决定重试 → ready)
              │         ▼       │
              │       paused    │
              │         │       │
              │         ▼       │
              │       active    │
              │                 │
              └────────────┬────┘
                           ▼
                       archived
                    (任何状态均可 → archived，由用户决定)
```

**状态转换规则：**

| 转换 | 触发条件 |
|------|---------|
| planned → ready | 所有 `depends_on` 前驱节点 completed |
| ready → active | 执行开始（自动或用户触发） |
| active → completed | 完成标准满足 |
| active → failed | 执行错误或完成标准不可达 |
| active → paused | 等待人工输入 / 等待外部资源 / 预算耗尽 |
| paused → active | 等待条件满足 |
| failed → ready | 用户决定重试（创建新 iteration 或原节点重试） |
| * → archived | 用户显式归档 / 分支废弃时批量归档 |

### 4.4 边（Edge）

边定义了节点间的关系，每条边 e ∈ E 定义为：

```
Edge = {
    source:  源节点 id
    target:  目标节点 id
    type:    depends_on | informs | triggers | iterates
}
```

**四种边类型：**

| 边类型 | 语义 | 阻塞性 | 示例 |
|--------|------|--------|------|
| `depends_on` | target 必须等 source 完成后才能开始 | **阻塞** | Experiment depends_on Planning |
| `informs` | source 的输出对 target 有用，但不阻塞 | 非阻塞 | Survey informs Analysis（文献对比） |
| `triggers` | source 完成后自动创建 target | 非阻塞 | Experiment triggers Analysis |
| `iterates` | target 是 source 的下一轮迭代 | 非阻塞 | Analysis(1) iterates→Survey(2)（发现新问题） |

**约束规则：**
- `depends_on` 关系在同一分支内不允许形成环（会导致死锁）
- `iterates` 边只能连接同类型或从 Analysis 回到 Survey/Planning（科研循环的自然模式）
- 跨分支的边只能是 `informs` 类型（分支间不应有硬依赖）

### 4.5 分支（Branch）

分支是对并行探索路径的建模：

```
Branch = {
    id:          唯一标识符
    name:        分支名称（描述探索方向）
    status:      active | merged | abandoned
    parent:      父分支 id（从哪个分支分叉出来）
    fork_point:  分叉点的 graph snapshot id
    hypothesis:  该分支探索的核心假设
    created_at:  创建时间
}
```

**分支操作：**

| 操作 | 语义 | 触发条件 |
|------|------|---------|
| **Create（分叉）** | 从当前决策点创建新分支，复制必要 context | 发现多条可行路径需要并行探索 |
| **Merge（合并）** | 将分支的发现整合回主分支或另一分支 | 分支产出有价值的结论，需要回到主线。需要创建 Review 节点评估合并内容 |
| **Abandon（废弃）** | 标记分支为废弃，所有活跃节点归档 | 该方向被证明不可行。失败原因提取到 Memory |

**分支约束：**
- 每个 Project 最多 N 个活跃分支（N 可配置，建议默认 3-5）
- 同类型节点的并发实例数 ≤ 活跃分支数
- 创建新分支需要说明假设/理由（系统生成或用户提供）
- 废弃分支的 artifacts 和 memory 保留，只是节点状态变更

### 4.6 循环管理

科研中循环是核心模式（实验-分析-发现问题-重新调研-重新实验），但需要防止无限循环。

**循环建模方式：**
- 每次循环不是"回到同一个节点重新执行"，而是**创建同类型的新节点实例**
- 新实例的 `iteration` 字段 = 前一实例 + 1
- 新旧实例之间通过 `iterates` 边连接
- 每个新 iteration 必须携带 `justification`（什么变了，为什么需要再来一轮）

**循环控制机制：**

```
循环控制 = {
    soft_limit:   同类型节点在同一分支中的软上限（默认 3）
                  → 达到时系统发出预警，建议评估是否应该改变策略
    hard_limit:   硬上限（默认 5）
                  → 达到时强制插入 Review 节点
    escalation:   超过 hard_limit 必须人工确认才能继续
}
```

**典型循环模式示例：**

```
Survey(1) → Planning(1) → Experiment(1) → Analysis(1)
                                              │
                              (发现结果不支持假设)
                                              │
                                              ▼
                                          Survey(2)  ← iterates ← Analysis(1)
                                              │          justification: "需要调研
                                              │           替代方法 X 的文献"
                                              ▼
                                          Planning(2) → Experiment(2) → Analysis(2)
                                                                            │
                                                              (结果显著，可以进入写作)
                                                                            │
                                                                            ▼
                                                                       Writing(1)
```

### 4.7 Seed Graph 生成

用户可能从任意阶段进入平台，系统需要根据入口构建最小 seed graph。

**入口类型与 seed graph 映射：**

| 入口场景 | 系统行为 | Seed Graph 结构 |
|---------|---------|----------------|
| **模糊 idea** | 引导用户澄清方向，帮助形成初步问题 | `Survey(1)` → `Planning(1)` |
| **已有 proposal** | 解析 proposal 提取目标和计划 | `Planning(1, pre-filled)` → `Experiment(1)` |
| **项目中途接入** | 要求用户提供已有材料，系统重建状态 | 重建已完成节点（completed）+ 识别当前活跃位置 |
| **具体任务** | 创建对应类型的单节点 | 单节点 + 系统建议上下游 |
| **失败恢复** | 识别失败点，创建诊断/恢复路径 | 失败节点(failed) + `Review(1)` + 恢复分支 |

**Seed graph 生成流程：**

```
1. 用户输入（文字描述 / 上传文档 / 选择模板）
     │
2. 系统解析意图，识别入口类型
     │
3. 生成最小 seed graph（通常 2-3 个节点）
     │
4. 展示给用户确认/修改
     │
5. 用户确认后，第一个 ready 节点开始执行
```

### 4.8 Graph 生长规则

Graph 不是一次性生成的，而是随着科研推进动态生长。

**五种生长触发机制：**

| 触发类型 | 描述 | 示例 |
|---------|------|------|
| **完成触发（Completion Trigger）** | 节点完成时，系统评估输出并建议下一步 | Experiment 完成 → 建议 Analysis |
| **发现触发（Discovery Trigger）** | 分析过程中发现新问题，建议分支 | Analysis 中发现异常数据 → 建议创建新 Survey 分支 |
| **失败触发（Failure Trigger）** | 节点失败，建议诊断或替代方案 | Experiment 失败 → 建议 Review 节点 + 替代 Planning |
| **用户触发（User Trigger）** | 用户手动添加节点 | 用户认为需要额外的 Data Process 步骤 |
| **时间触发（Time Trigger）** | 长时间运行后自动插入检查点 | 每运行 N 小时（可配置）自动插入 Review 节点 |

**生长的模式控制：**
- **辅助模式**：所有系统建议的生长必须用户确认
- **全自主模式**：低风险生长自动执行（如 Experiment→Analysis），高风险生长暂停等待确认（如创建新分支）

### 4.9 决策点（Decision Point）

决策点是 graph 中需要人类判断的关键时刻。

**触发决策点的条件：**

```
决策点触发条件 = {
    分叉:         存在多条可行路径，需要选择或并行
    合并:         分支需要合并或废弃
    回退:         当前方向证据不足，可能需要回到更早阶段
    资源冲突:     可用资源不足以支撑所有活跃节点
    外部变化:     新论文/新数据改变了问题前提
    里程碑:       达到预设的阶段性检查点
    循环预警:     同类操作迭代次数达到 soft_limit
    异常:         结果与预期严重不符
}
```

**决策点处理流程：**

```
1. 系统检测到决策点条件
     │
2. 保存 graph snapshot（加入 S 集合）
     │
3. 生成决策包（Decision Package）：
   - 当前状态摘要
   - 触发原因
   - 可选方案列表
   - 每个方案的利弊、风险、资源消耗
   - 系统推荐方案及理由
   - 不响应时的默认处理
     │
4. 根据运行模式处理：
   ├── 辅助模式：暂停，等待用户决策
   └── 全自主模式：
       ├── 高风险：暂停等待
       ├── 中风险：限时等待，超时按默认方案
       └── 低风险：按推荐方案自动执行
     │
5. 决策结果记录到 Project Memory
```

### 4.10 Graph 可视化映射

Graph 的数据结构需要映射到前端可视化：

| 数据结构 | 可视化元素 |
|---------|-----------|
| Node | 带状态颜色的卡片（绿=completed，蓝=active，灰=planned，红=failed） |
| Node.type | 卡片图标（不同节点类型不同图标） |
| Node.iteration | 卡片上的迭代标记（如 "Survey #2"） |
| Edge.depends_on | 实线箭头 |
| Edge.informs | 虚线箭头 |
| Edge.triggers | 点线箭头 |
| Edge.iterates | 弧线箭头（表示循环回路） |
| Branch | 水平泳道或颜色分组 |
| Decision Point | 菱形标记 + snapshot 入口 |

---

## 5. 最小公约节点与 Harness 设计

最小公约节点是科研流程中最常见、最不可避免、最容易被科学家感知的工作单元。系统不直接暴露底层 tool call，而是将其包装成科研操作节点，每个节点背后预设完整的 harness。

### 5.1 节点通用结构

每个最小公约节点共享以下通用结构：

```
NodeHarness = {
    // 身份
    type:               节点类型标识
    description:        节点用途的自然语言描述

    // Harness 核心
    system_prompt:      该节点的 agent 角色定义与行为约束
    rules:              硬性规则（违反则阻断输出）
    guidelines:         软性指南（建议遵循）

    // Context Engine
    context_assembly: {
        memory_query:   从 Memory 系统检索哪些类型的记忆
        kb_query:       从 Knowledge Base 检索什么内容
        handoff_policy: 如何处理前驱节点的输出（全量/摘要/筛选）
        max_context:    context 总量上限（token 数）
        priority:       当 context 超限时的裁剪优先级
    }

    // 能力
    skills:             该节点可用的技能列表
    tools:              该节点可调用的工具列表
    sub_agents:         是否拆分子任务给子 agent（及分工策略）

    // 质量控制
    completion_criteria: 完成标准（系统建议，用户可覆盖）
    review_triggers:     触发 Review/人工介入的条件
    risk_level:          该节点操作的默认风险等级

    // 输入输出
    expected_inputs:     预期输入 artifact 类型
    expected_outputs:    预期输出 artifact 类型
}
```

### 5.2 Survey 节点

**目的**：系统性文献调研与证据收集。在给定研究问题下，全面检索、筛选、提取和综合已有工作。

#### System Prompt 核心

```
你是一个系统性文献调研助手。你的任务是针对给定的研究问题进行全面、
严谨的文献调研。

核心原则：
- 每一个事实性陈述都必须标注来源论文及具体段落
- 不得编造、猜测或混淆论文内容
- 如果检索结果不足以回答问题，明确报告"证据不足"而非强行得出结论
- 主动识别文献中的矛盾和争议，不掩盖不一致性
- 区分高质量证据（顶会/顶刊、大规模实验验证）和初步证据（预印本、小规模实验）
```

#### Context Engine

```yaml
context_assembly:
  memory_query:
    - project.goals              # 项目目标
    - project.hypotheses         # 当前假设
    - project.prior_surveys      # 之前的调研结果（避免重复工作）
    - project.known_gaps         # 已识别的知识缺口
    - org.research_directions    # 组织研究方向
  kb_query:
    - 按研究问题关键词检索相关论文
    - 按引用链扩展（被引/引用）
    - 按时间排序获取最新成果
  handoff_policy:
    from_planning: 提取研究问题和调研范围定义
    from_analysis: 提取需要进一步调研的具体问题（迭代场景）
  priority: [memory.goals, kb.recent_papers, kb.high_cited, memory.prior_surveys]
```

#### Skills

| 技能 | 描述 | 实现方式 |
|------|------|---------|
| **论文检索** | 多数据库检索（Semantic Scholar, arXiv, PubMed, Google Scholar） | API 调用 |
| **论文精读** | 解析 PDF，提取关键信息（方法、结果、结论） | PDF 解析 + LLM |
| **引用图分析** | 追踪引用链，识别关键论文和研究脉络 | 引用 API + 图分析 |
| **综合对比** | 生成方法对比矩阵、结果对比表 | LLM 结构化输出 |
| **缺口识别** | 识别已有工作中的空白和未解决问题 | LLM 推理 |
| **证据质量评估** | 评估每条证据的可靠性等级 | 规则 + LLM |

#### Tools

```yaml
tools:
  - semantic_scholar_api      # 学术搜索
  - arxiv_api                 # 预印本搜索
  - pubmed_api                # 生物医学搜索
  - google_scholar_scraper    # 通用学术搜索
  - pdf_parser                # PDF 解析和段落提取
  - citation_graph_tool       # 引用关系分析
  - kb_ingester               # 将新发现的论文存入 KB
  - comparison_table_builder  # 生成结构化对比表
  - note_taker                # 结构化笔记
```

#### Completion Criteria

```yaml
completion_criteria:
  required:               # 硬性要求
    - 至少检索 3 个数据库
    - 所有事实性陈述都有段落级引用
    - 产出结构化的调研报告
  recommended:            # 建议达到
    - 饱和度检测：最后 K 篇论文未发现新主题（K 可配置，默认 5）
    - 覆盖最近 N 年的文献（N 按领域配置）
    - 包含至少一个对比矩阵
    - 明确列出知识缺口
  user_overridable: true  # 用户可判断提前完成或要求更深入
```

#### Review Triggers

```yaml
review_triggers:
  - 发现文献中存在重大矛盾（不同论文对同一问题的结论相反）
  - 检索范围可能不足（某类关键词返回结果过少）
  - 文献建议研究问题本身可能有问题（已被解决/不可行/定义不清）
  - 发现高度相关的已有工作，可能影响项目 novelty
  - 关键论文无法获取（付费墙/不可用）
```

#### Output Artifacts

```yaml
outputs:
  - type: literature_review
    format: structured_markdown
    description: 完整的文献调研报告，含引用、对比、缺口分析
  - type: paper_collection
    format: annotated_bibliography
    description: 收集的论文列表及关键信息提取
  - type: comparison_matrix
    format: table
    description: 方法/结果对比矩阵
  - type: gap_analysis
    format: structured_list
    description: 已识别的知识缺口和未解决问题
  - type: question_refinement
    format: text
    description: 基于调研结果对原始研究问题的修正建议
```

---

### 5.3 Planning 节点

**目的**：将调研发现转化为可执行的研究计划。包括假设制定、实验设计、资源估算和风险评估。

#### System Prompt 核心

```
你是一个科研策略助手。你的任务是基于已有的文献调研和项目目标，
制定严谨、可执行的研究计划。

核心原则：
- 假设必须是可证伪的（falsifiable），每个假设都要有明确的验证方式
- 实验设计必须包含对照组和基线
- 计划必须考虑失败路径——如果假设不成立，plan B 是什么
- 资源估算要现实，不能低估时间和计算成本
- 方案选择要给出论据，说明为什么选 A 而不选 B
```

#### Context Engine

```yaml
context_assembly:
  memory_query:
    - project.goals
    - project.constraints          # 资源约束、时间约束
    - project.prior_plans          # 之前的计划（迭代场景）
    - project.failed_experiments   # 已失败的实验及原因
    - org.methodology_standards    # 组织的方法论规范
    - org.available_resources      # 可用算力、数据
  kb_query:
    - 与目标方法相关的 methodology papers
    - 类似研究的实验设计参考
    - benchmark 数据集和评估指标
  handoff_policy:
    from_survey: 注入文献调研的缺口分析、方法对比矩阵、关键发现
    from_analysis: 注入上一轮分析的结论和"下一步建议"（迭代场景）
  priority: [memory.goals, handoff.survey_gaps, memory.failed_experiments, kb.methodology]
```

#### Skills

| 技能 | 描述 |
|------|------|
| **假设生成** | 基于证据提出可证伪的科学假设 |
| **实验设计** | 设计实验方案，包括变量、对照、基线 |
| **方案对比** | 对多个可行方案进行利弊分析 |
| **资源估算** | 估算计算资源、数据需求、时间成本 |
| **风险分析** | 识别技术风险、资源风险和依赖风险 |
| **任务分解** | 将计划拆解为可执行的具体步骤 |
| **时间线规划** | 制定里程碑和时间估算 |

#### Tools

```yaml
tools:
  - resource_estimator       # 资源估算（GPU 时间、数据量）
  - benchmark_lookup         # 查询标准 benchmark 和 baseline
  - template_library         # 实验设计模板库
  - risk_matrix_builder      # 风险矩阵生成
  - dependency_analyzer      # 任务依赖分析
```

#### Completion Criteria

```yaml
completion_criteria:
  required:
    - 至少一个明确的、可证伪的假设
    - 实验设计包含变量定义、对照方案、评估指标
    - 成功标准有量化定义
    - 资源估算覆盖算力、数据、时间
  recommended:
    - 包含失败路径和 plan B
    - 风险评估覆盖至少 3 类风险
    - 任务拆解到可执行粒度
```

#### Review Triggers

```yaml
review_triggers:
  - 资源需求超出项目预算
  - 多个方案势均力敌，无法自动决策
  - 假设难以设计验证实验（可能不可证伪）
  - 计划依赖不可用的工具或数据
  - 与组织规范存在冲突
  - 估算的实验规模非常大（高代价执行前必须确认）
```

#### Output Artifacts

```yaml
outputs:
  - type: research_plan
    format: structured_markdown
    description: 完整研究计划，含假设、方法、步骤、时间线
  - type: hypothesis_statement
    format: structured_text
    description: 假设陈述 + 验证方式 + 成功标准
  - type: experiment_design
    format: structured_spec
    description: 实验设计规格（变量、对照、基线、指标）
  - type: resource_estimate
    format: table
    description: 资源和时间估算
  - type: risk_register
    format: structured_list
    description: 风险清单 + 缓解策略
```

---

### 5.4 Experiment 节点

**目的**：执行计算实验，严格记录过程和结果，确保可复现性。

#### System Prompt 核心

```
你是一个精确的计算实验执行者。你的任务是按照实验设计方案执行实验，
严格记录所有参数和结果。

核心原则：
- 完全复现性：记录所有参数、随机种子、软件版本、环境配置
- 零静默失败：所有错误都必须被捕获和记录，不得隐藏
- 预算意识：持续监控计算资源消耗，接近预算时主动汇报
- 忠实于设计：严格按照实验设计执行，如需偏离必须记录原因
- 数据完整性：实验数据不得被篡改、选择性删除或事后修改
```

#### Context Engine

```yaml
context_assembly:
  memory_query:
    - project.experiment_history   # 之前的实验及结果
    - project.known_pitfalls       # 已知的坑（如特定数据集的陷阱）
    - project.parameter_tuning     # 参数调优经验
    - org.compute_policies         # 组织的算力使用策略
  kb_query:
    - 实验方法论的最佳实践
    - 相关工具的使用文档
    - benchmark 数据集信息
  handoff_policy:
    from_planning: 注入完整的实验设计规格（变量、参数、基线、指标、成功标准）
  priority: [handoff.experiment_design, memory.known_pitfalls, memory.parameter_tuning]
```

#### Skills

| 技能 | 描述 |
|------|------|
| **代码生成** | 根据实验设计生成实验代码 |
| **环境搭建** | 配置实验环境、安装依赖 |
| **参数扫描** | 管理多组参数的系统性实验 |
| **进度监控** | 实时监控实验进度和资源消耗 |
| **结果验证** | 对实验结果做 sanity check |
| **复现打包** | 将实验环境和参数打包为可复现单元 |
| **异常检测** | 识别实验过程中的异常情况 |

#### Tools

```yaml
tools:
  - code_executor            # 代码执行环境（支持 Python/R/Julia）
  - gpu_scheduler            # GPU 资源调度
  - experiment_tracker        # 实验追踪（参数、指标、日志）
  - version_control          # Git 操作
  - environment_manager      # 虚拟环境 / 容器管理
  - data_loader              # 数据集加载和预处理
  - checkpoint_manager       # 模型/实验检查点管理
  - resource_monitor         # 资源消耗监控
  - sanity_checker           # 结果合理性检查
```

#### Completion Criteria

```yaml
completion_criteria:
  required:
    - 所有计划中的实验已执行（或显式跳过并记录原因）
    - 所有实验参数已记录（含随机种子、版本号）
    - 实验结果数据完整保存
    - sanity check 通过（无 NaN、无异常值、数据量符合预期）
  recommended:
    - 复现性包生成（Dockerfile/requirements.txt + 脚本 + 配置）
    - 中间结果有检查点
    - 资源消耗在预算范围内
```

#### Review Triggers

```yaml
review_triggers:
  - 实验结果与预期严重不符（如指标异常好或异常差）
  - 计算预算消耗超过预设百分比（如 80%）
  - 实验时间大幅超出预估
  - 依赖的外部服务/数据不可用
  - 发现实验设计可能有缺陷（如缺少关键对照）
  - 多组实验结果出现矛盾
```

#### Output Artifacts

```yaml
outputs:
  - type: experiment_results
    format: structured_data
    description: 原始实验结果数据
  - type: experiment_logs
    format: log_files
    description: 完整的执行日志
  - type: parameter_configs
    format: yaml/json
    description: 所有实验的参数配置
  - type: environment_snapshot
    format: dockerfile/requirements
    description: 可复现的环境描述
  - type: preliminary_summary
    format: structured_markdown
    description: 初步结果摘要（供 Analysis 节点使用）
```

---

### 5.5 Analysis 节点

**目的**：对实验结果进行严谨的统计分析、可视化和科学解读。

#### System Prompt 核心

```
你是一个严谨的数据分析与科学解读助手。你的任务是对实验结果进行
统计分析、可视化和科学解释。

核心原则：
- 统计主张必须有方法论支撑（明确统计检验方法、置信水平、效应量）
- 相关性不等于因果性——严格区分
- 负面结果必须如实报告，不得选择性呈现
- 异常值需要诊断而非简单丢弃
- 与文献的对比必须公平（相同条件、相同指标）
- 所有结论都必须可追溯到具体的数据点和分析过程
```

#### Context Engine

```yaml
context_assembly:
  memory_query:
    - project.hypotheses           # 当前要验证的假设
    - project.prior_analyses       # 之前的分析结论
    - project.interpretation_rules # 项目特有的解读规范
    - org.statistics_standards     # 组织的统计规范
  kb_query:
    - 统计方法论文献
    - 同领域的 benchmark 结果（用于对比）
    - 相关工作的实验结果
  handoff_policy:
    from_experiment: 注入全部实验结果数据 + 参数配置 + 初步摘要
    from_planning: 注入假设定义和成功标准（用于验证判断）
  priority: [handoff.experiment_results, memory.hypotheses, kb.benchmarks, memory.prior_analyses]
```

#### Skills

| 技能 | 描述 |
|------|------|
| **统计分析** | 假设检验、置信区间、效应量计算 |
| **数据可视化** | 生成图表（折线图、箱线图、热力图等） |
| **异常诊断** | 识别和诊断数据中的异常 |
| **假设验证** | 基于数据判断假设是否成立 |
| **对比分析** | 与 baseline 和文献结果对比 |
| **趋势识别** | 识别数据中的模式和趋势 |
| **结论综合** | 从多组实验结果中得出综合结论 |
| **下一步建议** | 基于分析结果建议后续方向 |

#### Tools

```yaml
tools:
  - statistical_computing    # Python (scipy/statsmodels) / R
  - visualization_engine     # matplotlib/plotly/seaborn
  - hypothesis_tester        # 统计假设检验工具
  - benchmark_comparator     # 与已知 benchmark 对比
  - data_profiler            # 数据质量和分布分析
  - effect_size_calculator   # 效应量计算
  - report_generator         # 分析报告生成
```

#### Completion Criteria

```yaml
completion_criteria:
  required:
    - 所有实验数据已被分析
    - 统计检验使用了正确的方法（并说明选择原因）
    - 假设验证有明确结论（支持/不支持/证据不足）
    - 关键结果有可视化图表
    - 结论有证据链（结论 → 数据 → 实验参数）
  recommended:
    - 与文献 baseline 有定量对比
    - 效应量和置信区间已报告
    - 负面结果已记录和分析
    - 包含"下一步建议"
```

#### Review Triggers

```yaml
review_triggers:
  - 结果不支持核心假设
  - 统计显著性处于临界值（如 p ≈ 0.05）
  - 数据中发现无法解释的异常
  - 结果与已有文献严重矛盾
  - 存在多种同样合理的解释
  - 分析建议需要全新的实验方向（将导致新的循环）
```

#### Output Artifacts

```yaml
outputs:
  - type: analysis_report
    format: structured_markdown
    description: 完整分析报告，含方法、结果、解释
  - type: statistical_results
    format: structured_data
    description: 统计检验结果（p 值、置信区间、效应量）
  - type: visualizations
    format: image_files
    description: 图表集合
  - type: hypothesis_verdict
    format: structured_text
    description: 假设验证结论 + 证据链
  - type: next_steps
    format: structured_list
    description: 基于分析结论的后续建议
```

---

### 5.6 Phase 2 节点（轻量设计）

#### 5.6.1 Data Process 节点

**目的**：数据清洗、转换、预处理。

```yaml
core_focus:
  - 数据质量保障（缺失值、异常值、格式不一致）
  - 处理过程完全可追溯（每一步变换都有记录）
  - 不改变数据的科学含义（只做格式/质量处理，不做选择性过滤）
key_skills: [数据清洗, 格式转换, 质量检查, 数据合并, 特征工程]
key_tools: [pandas/polars, data_validator, schema_checker, profiler]
unique_rule: 原始数据永不覆盖，所有处理产生新的派生数据集
```

#### 5.6.2 Writing 节点

**目的**：科研写作（论文、报告、文档）。

```yaml
core_focus:
  - 每个事实性陈述都必须从项目 artifact 和 KB 中溯源
  - 不编造数据、不夸大结论、不掩盖局限性
  - 遵循目标期刊/会议的格式要求
key_skills: [学术写作, 图表生成, 引用管理, 格式适配, 摘要生成]
key_tools: [latex_compiler, citation_manager, figure_generator, grammar_checker]
unique_rule: 溯源要求最严格——所有结论都必须链接到具体的 Analysis artifact
```

#### 5.6.3 Review/Decision 节点

**目的**：关键检查点，暴露问题，请求人类决策。

```yaml
core_focus:
  - 不是 agent 自主决策，而是组织信息帮助人类决策
  - 输出结构化的决策包（Decision Package）
  - 记录决策结果和理由到 Memory
key_skills: [状态汇总, 风险评估, 方案对比, 决策记录]
key_tools: [graph_snapshot_tool, summary_generator, decision_recorder]
unique_rule: 此节点的 output 永远是给人看的，不是给下游节点消费的
special: 这是唯一一个可以被系统自动插入 graph 的节点类型
```

### 5.7 节点间 Handoff 协议

节点之间的信息传递通过 Context Slicing 机制实现：

```
Handoff = {
    source_node:     源节点
    target_node:     目标节点
    strategy:        full | summary | selective | none
    content: {
        artifacts:   传递哪些 artifact（引用，非复制）
        summary:     源节点输出的结构化摘要
        decisions:   在源节点中做出的决策及理由
        open_issues: 源节点遗留的未解决问题
    }
    max_tokens:      摘要的 token 上限
}
```

**默认 Handoff 策略矩阵：**

| 源 → 目标 | 策略 | 传递内容 |
|-----------|------|---------|
| Survey → Planning | summary | 缺口分析 + 方法对比矩阵 + 关键发现 |
| Planning → Experiment | full | 完整实验设计规格 |
| Experiment → Analysis | full | 全部结果数据 + 参数配置 + 初步摘要 |
| Analysis → Survey(迭代) | selective | "需要调研的具体问题" + 推翻的假设 |
| Analysis → Planning(迭代) | summary | 结论 + "下一步建议" + 失败原因分析 |
| Analysis → Writing | full | 全部分析结果 + 图表 + 结论 |
| 任意 → Review | summary | 当前状态摘要 + 需要决策的问题 |

---

## 6. 知识库与记忆系统

### 6.1 架构分离原则

平台将外部知识和内部经验严格分离为两个独立系统：

**知识库（Knowledge Base, KB）**——"世界知道什么"
- 内容：论文、新闻、领域公共知识、参考资料、方法论文档
- 更新策略：自由更新，系统可在空闲时自主检索和入库
- 溯源：保留原始来源（DOI、URL、获取时间）
- 用途：Context Engine 的检索原料
- 分层：Organization KB（跨项目共享）/ Project KB（项目特有参考资料）
- 索引粒度：段落级（支持 Q24 的段落级溯源）

**记忆系统（Memory）**——"我们知道/决定/经历了什么"
- 内容：决策、结论、经验、规则、偏好、失败记录
- 更新策略：受控更新（见 Q17 分类策略）
- 溯源：每条记忆标注完整来源链
- 用途：指导 Agent 行为，影响决策和规划
- 分层：Organization / Project / User / Session

### 6.2 知识库设计

```
KnowledgeBase = {
    // 存储
    entries: [
        {
            id:           唯一标识
            source_type:  paper | news | documentation | dataset_info | benchmark
            source_ref:   DOI / URL / 内部引用
            content:      原始内容
            chunks: [     段落级切片
                {
                    chunk_id:    段落标识
                    text:        段落文本
                    embedding:   向量表示（用于检索）
                    metadata:    页码/章节/图表编号
                }
            ]
            added_at:     入库时间
            added_by:     system | user
            scope:        organization | project(project_id)
            tags:         标签（领域、主题、方法类型）
            quality_tier: tier1(顶刊顶会) | tier2(一般期刊) | tier3(预印本) | tier4(新闻/博客)
        }
    ]

    // 更新策略
    auto_update: {
        enabled:      true
        schedule:     空闲时自动 + 每日定时（可配置）
        scope:        用户定义关键词 ∪ 系统根据 project context 推断的领域
        ingestion:    检索 → 去重 → 解析 → 切片 → embedding → 入库
        notification: 重大发现通知用户（如高度相关的新论文）
    }
}
```

### 6.3 记忆系统设计

```
MemorySystem = {
    layers: {
        organization: {
            write_policy:  自动提取 → 人工审批 → PI 最终确认
            decay:         不衰减，只能管理员废弃/归档
            conflict:      保留冲突，提交 PI 裁决
            content_types: [研究方向, 组织规范, 工具链, benchmark, 失败经验,
                           审稿原则, 写作原则, PI 长期要求, 最高级指导文件]
        },
        project: {
            write_policy:  混合（见 Q17 分类表）
            decay: {
                factual:   不衰减，标注时间
                judgmental: 6 个月未引用/验证 → 标记 stale → 系统提示
            }
            conflict:      保留双版本 + 标记冲突 + 提示用户
            content_types: [项目目标, 关键假设, 历史决策及理由, 项目规则,
                           项目结论, 已确认的 workflow, 失败原因, 参数经验]
        },
        user: {
            write_policy:  用户自主 + 系统建议
            decay:         不衰减，用户自管理
            conflict:      新覆盖旧，保留变更日志
            content_types: [工具偏好, 汇报偏好, 写作风格, 个人探索内容]
        },
        session: {
            write_policy:  自动
            decay:         会话结束后丢弃
            conflict:      直接覆盖
            promotion:     会话结束前提示用户是否将关键内容提升到 Project Memory
            content_types: [当前操作上下文, 临时状态, 中间推理过程]
        }
    }

    // 记忆流动规则
    flow_rules: {
        org_to_project:       可继承（默认注入）
        project_to_session:   按需注入（Context Engine 控制）
        user_to_project:      用户可选择带入
        project_to_org:       不能自动上升，必须显式提升 + PI 确认
        project_to_project:   默认不共享，只能通过 org 层或显式引用
    }

    // 记忆元数据（每条记忆必须包含）
    entry_metadata: {
        id:              唯一标识
        content:         记忆内容
        type:            factual | judgmental | preference | rule
        source: {
            operation:   产生该记忆的节点操作
            artifact:    关联的 artifact
            paper_ref:   关联的论文（如适用）
            reasoning:   产生该记忆的推理过程
        }
        created_at:      创建时间
        last_accessed:   最后被引用时间
        last_verified:   最后被验证时间
        confidence:      置信度（high | medium | low）
        status:          active | stale | archived | conflicted
    }

    // 空闲时记忆优化（参考 Hermes Agent 容量感知合并 + OpenClaw 技能提取）
    memory_refinement: {
        enabled:      true
        schedule:     空闲时执行 + 容量触发（80%阈值）+ 新KB入库触发
        operations: [
            // 核心操作（每次 refinement 必执行）
            去重和合并相似记忆,                                   // Hermes: substring match + duplicate rejection
            检查记忆间的一致性,                                   // 检测矛盾的结论/假设
            标记长期未引用的判断类记忆为 stale,                    // 6个月阈值

            // 容量驱动操作（当某层记忆超过 soft_limit 时触发）
            合并同主题记忆为摘要条目,                              // Hermes: 80%容量时合并相关条目
            按置信度排序淘汰低价值记忆,                            // 低置信度 + 长期未引用 → 归档候选

            // KB联动操作（新论文/数据入库时触发）
            根据新的 KB 内容评估已有结论是否仍然成立,              // 交叉验证
            为被新证据否定的记忆添加 superseded_by 标记,           // 保留历史但标记过时
            
            // 研究技能提取（参考 Hermes procedural memory）
            从成功的复杂操作序列中提取可复用研究技能,              // 5+步骤的成功 workflow → Research Skill
            更新已有研究技能（当发现更优流程时）                    // 增量 patch 而非全量重写
        ]

        // 容量管理（参考 Hermes bounded memory 设计）
        capacity: {
            soft_limits: {                                        // 超过时触发合并
                project_memory:  500 条（约 200K tokens）
                user_memory:     100 条
                session_memory:  不限（会话结束后丢弃）
            }
            consolidation_strategy: merge_by_topic                // 同主题多条 → 一条综合
            archive_policy:        low_confidence_unused_6m       // 低置信+6月未用→归档
        }

        // 完整性检查（参考 Hermes security scanning）
        integrity: {
            pre_persist_validation:   true                        // 入库前校验
            contradiction_detection:  true                        // 与已有记忆矛盾检测
            source_verification:      true                        // 来源链完整性检查
        }
    }

    // 研究技能系统（Procedural Memory，参考 Hermes Skills）
    // 区别于 KB（外部知识）和 Memory（内部决策/事实），这是操作性知识
    research_skills: {
        description:  "从成功的研究操作中提取的可复用方法论知识"
        storage:      project_level + organization_level（可提升）
        examples: [
            "蛋白质对接分析的数据预处理流程",
            "大规模文献综述的三阶段筛选策略",
            "实验参数空间的高效搜索方法",
            "论文写作中引用密度的自动检查流程"
        ]
        lifecycle: {
            extraction:    成功完成复杂节点操作（5+工具调用）后系统自动提议提取
            validation:    用户确认（assisted 模式）或自动（autonomous 模式 + 高置信度）
            refinement:    后续使用中发现更优路径时增量更新（patch）
            promotion:     project → organization 需 PI 确认
            deprecation:   6个月未使用 + 有更优替代 → 标记 deprecated
        }
        context_loading: {                                        // 参考 Hermes progressive disclosure
            level_0:  技能列表元数据（名称+描述，~少量 tokens）
            level_1:  完整技能内容（步骤+工具+参数）
            level_2:  关联 artifact 和历史执行记录
            strategy: 按节点类型和当前任务匹配，仅加载相关技能到 level_1
        }
    }
}
```

### 6.4 参考系统分析与设计启示

本平台记忆系统的设计参考了以下两个开源 Agent 系统，但所有机制的取舍标准是**是否让科研做得更好**。

#### 6.4.1 OpenClaw（开源个人 AI 助手）

[GitHub: openclaw/openclaw](https://github.com/openclaw/openclaw) | MIT License

**系统概述**：本地运行的个人 AI 助手平台，通过 Gateway 统一控制面连接 25+ 消息渠道（WhatsApp/Telegram/Slack/Discord 等）。核心是 workspace 概念。

**记忆相关机制**：
- **Workspace 文件系统**：通过 `SOUL.md`（人格定义）、`AGENTS.md`（能力定义）、`TOOLS.md`（工具文档）三个 Markdown 文件注入 Agent 行为——简洁但有效的 prompt 工程方法
- **Skills 系统**：模块化技能存储于 `~/.openclaw/workspace/skills/`，通过 ClawHub 注册表发现和管理
- **Multi-agent routing**：不同消息渠道可路由到隔离的 agent（独立 workspace + 独立 session），实现关注点分离
- **Cron 自动化**：定时任务支持后台处理

**对本平台的启示**：
| OpenClaw 机制 | 本平台对应 | 科研价值 |
|---|---|---|
| SOUL.md / AGENTS.md | Node Harness 的 system_prompt + rules | 用 Markdown 定义 Agent 行为是经过验证的有效模式 |
| Skills 注册表 | Research Skills（6.3 新增） | 研究方法论的复用和共享 |
| Multi-agent workspace 隔离 | Project-level Agent + User .md 注入 | 项目间记忆隔离 + 个人偏好注入 |
| Cron 后台任务 | 空闲时 KB 更新 + 记忆 refinement | 自动化知识维护 |

#### 6.4.2 Hermes Agent（Nous Research 自改进 Agent）

[GitHub: nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent) | MIT License

**系统概述**：Nous Research 出品的自改进 AI Agent，核心理念是"closed learning loop"——Agent 跨会话积累知识、自主创建和改进技能、渐进式理解用户偏好。从 OpenClaw 演化而来。

**记忆架构（三层）**：

1. **Episodic Memory**：FTS5 全文搜索数据库存储会话历史 + LLM 摘要实现跨会话语义召回。选择 FTS5 而非向量数据库——减少 embedding 依赖，词法搜索 + LLM 摘要已足够。
2. **Semantic Memory**：`MEMORY.md`（~800 tokens, 2200 字符上限）+ `USER.md`（~500 tokens, 1375 字符上限）。有界设计迫使 Agent 做记忆优先级判断——容量达 80% 时自动合并相关条目。
3. **Procedural Memory（Skills）**：Agent 完成复杂任务（5+ 工具调用）后自主提取可复用技能。技能在后续使用中通过 `patch` 操作增量改进。支持 agentskills.io 开放标准。

**关键设计决策**：
- **Frozen snapshot 注入**：记忆在会话启动时作为冻结快照注入 system prompt，会话中不动态更新——为 LLM prefix cache 性能优化
- **Progressive disclosure**：三级加载——Level 0 元数据（~3K tokens）→ Level 1 完整内容 → Level 2 关联文件。按需加载，控制 context window
- **Integrity scanning**：记忆入库前扫描注入/篡改模式和不可见 Unicode，防止 prompt injection
- **External memory providers**：8 个可选插件（Honcho 用户建模、Mem0 图记忆、OpenViking 等）与核心记忆并行运行
- **容量驱动合并**：不是被动等待过期，而是在容量接近上限时主动合并——"三条独立的项目使用记录合并为一条综合描述"
- **Trajectory compression**：压缩操作轨迹而非原始存储——为训练下一代 tool-calling 模型提供数据

**对本平台的核心启示**：

| Hermes 机制 | 本平台采纳方式 | 科研价值 |
|---|---|---|
| 有界记忆 + 容量合并 | Memory 每层设 soft_limit，超限触发合并 | 长期项目中防止记忆噪声积累，保持 Agent 决策聚焦 |
| Procedural Memory / Skills | Research Skills 系统（新增） | 研究方法论从隐性变为显性可复用知识 |
| Progressive disclosure | Context Engine 三级加载 | 控制 context window，让 Agent 聚焦当前研究步骤 |
| FTS5 + LLM 摘要 | Session 历史搜索（"上周讨论的 X 是什么？"） | 跨会话研究连续性 |
| 入库前完整性检查 | Pre-persist validation + 矛盾检测 | 确保研究记忆的科学严谨性 |
| KB 联动评估 | 新论文入库时自动评估已有结论 | 防止基于过时知识做研究决策 |
| Frozen snapshot | **不完全采纳**——科研场景需动态重注入 | 研究者切换子任务时需要不同 context 组合 |

#### 6.4.3 与已有参考系统（MemGPT / Mem0）的关系

| 系统 | 核心贡献 | 本平台借鉴 |
|---|---|---|
| **MemGPT** | 分页式虚拟 context 管理 | Context Engine 的 token 预算管理思想 |
| **Mem0** | 图结构记忆 + 自动提取 | 记忆间关系建模（memory graph） |
| **OpenClaw** | Workspace 文件系统 + Skills 注册表 | Markdown 驱动 Agent 行为 + 技能复用模式 |
| **Hermes** | 有界记忆 + 容量合并 + Procedural Memory | 容量管理 + 研究技能提取 + 渐进加载 |

**本平台的差异化**：以上系统均面向通用个人助手场景。本平台在此基础上增加：
- **KB/Memory 严格分离**——通用系统不区分外部知识和内部经验
- **四层记忆层级**（Org/Project/User/Session）——通用系统最多两层
- **科研溯源绑定**——每条记忆可追溯到具体研究操作和 artifact
- **研究技能 ≠ 通用技能**——限定在科研方法论领域，与 Research Graph 节点类型绑定
```

---

## 7. 证据链与溯源体系

### 7.1 分级溯源模型

```
溯源等级 = {
    hard_provenance: {
        适用于:    事实性陈述（"方法 A 在数据集 X 上达到 90% 准确率"）
        要求:      精确关联到具体数据源
                   - 如果来自论文：论文 + 段落/图表编号
                   - 如果来自实验：实验 run id + 具体数据点
                   - 如果来自数据：数据集 + 行/列引用
        失败处理:  无法提供硬溯源 → 降级为 soft_provenance 或标记"推测/未验证"
    },
    soft_provenance: {
        适用于:    推理性结论（"基于以上结果，我们认为方法 A 更适合场景 Y"）
        要求:      标注推理依据列表 + 置信度
                   - 列出所有支撑该推理的硬溯源事实
                   - 标注推理的置信度（high / medium / low）
                   - 如有反面证据，也必须列出
        失败处理:  可以输出，但必须有置信度标注
    },
    no_provenance: {
        适用于:    一般性建议、工作流建议、格式建议
        要求:      无溯源要求
        标注:      明确标注为"系统建议"而非"科研结论"
    }
}
```

### 7.2 证据链结构

```
EvidenceChain = {
    claim:          最终陈述
    provenance_level: hard | soft | none
    confidence:     high | medium | low
    supports: [     支撑证据
        {
            evidence:    具体证据内容
            source_type: paper | experiment | data | reasoning
            source_ref:  KB chunk_id | artifact_id | memory_id
            strength:    direct | indirect | analogical
        }
    ]
    contradictions: [  反面证据（如有）
        {
            evidence:    反面证据内容
            source_ref:  来源引用
            resolution:  如何解释这个矛盾
        }
    ]
}
```

---

## 8. Human-in-the-Loop 机制

### 8.1 汇报等级

用户设定"汇报等级"控制系统的主动沟通频率：

| 等级 | 触发条件 | 适用场景 |
|------|---------|---------|
| **低** | 仅高风险决策点 + 黑名单操作 | 信任度高的成熟流程，用户希望减少打扰 |
| **中**（默认） | 阶段里程碑 + 高风险决策点 + 重大发现 | 常规科研项目 |
| **高** | 每个显著进展都汇报 | 新项目、高风险研究、PI 需要紧密掌控 |

### 8.2 汇报内容结构

系统的每次汇报必须包含：

```
Report = {
    trigger:          为什么现在汇报（里程碑/决策点/异常/定时）
    current_state:    当前发生了什么（简洁）
    why_it_matters:   为什么值得你关注
    key_question:     需要你决定的核心问题（如果有）
    options: [        可选方案
        {
            description:  方案描述
            pros:         优势
            cons:         风险/代价
            resource_cost: 资源消耗估算
        }
    ]
    recommendation:   系统推荐方案及理由
    default_action:   如果不回复，系统将如何处理
    deadline:         决策窗口（如适用）
}
```

### 8.3 不响应时的行为

| 风险等级 | 行为 |
|---------|------|
| 高 | 暂停等待，持续通知直到用户响应 |
| 中 | 等待设定时间（默认 24h，可配置），超时后按默认方案执行并记录日志 |
| 低 | 直接按推荐方案执行 |

---

## 9. 工具与权限管理

### 9.1 三层工具架构

```
组织级工具层
├── 可用工具注册表（白名单）
├── 需要审批的工具
├── 安全策略（联网/本地、数据访问范围）
├── 可用模型列表
│
项目级工具层
├── 继承组织级白名单
├── 项目特有工具配置
├── 默认启用/需要 review 的工具
├── 可访问的目录、数据集、算力
│
操作级（节点级）
├── 每个节点类型有默认工具集
├── 执行策略（超时、重试、替代）
└── 预算约束
```

### 9.2 工具生命周期

```
发现/需求 → 沙箱试用 → 用户确认 → 正式注册 → 使用 → 监控/审计
                                                         │
                                           失败时：重试 → 替代工具 → 汇报用户
```

### 9.3 预算管理

```
Budget = {
    organization_total:    组织级总预算
    project_budgets: {
        [project_id]: {
            total:         项目总预算
            by_type: {
                compute:   GPU/CPU 计算预算
                api_calls: 外部 API 调用预算
                storage:   存储预算
                llm:       LLM token 消耗预算
            }
            alerts: [
                { threshold: 0.8, action: "notify" },
                { threshold: 0.95, action: "pause_and_notify" },
                { threshold: 1.0, action: "hard_stop" }
            ]
        }
    }
}
```

---

## 10. Artifact 管理

### 10.1 Artifact 定义

Artifact 是科研过程中产生的所有有价值的产物，包括中间产物。

```
Artifact = {
    id:             唯一标识
    type:           paper | dataset | code | figure | log | report |
                    model | config | review_package | custom
    name:           名称
    description:    描述
    source_node:    产出该 artifact 的节点 id
    version:        版本号（最新版 + 里程碑版本）
    scope:          project | organization（是否提升到组织层共享）
    references: [   引用关系
        { artifact_id, relationship: "derived_from" | "references" | "supersedes" }
    ]
    metadata: {
        created_at:    创建时间
        created_by:    创建者
        file_path:     存储路径
        file_size:     文件大小
        format:        文件格式
        tags:          标签
    }
}
```

### 10.2 版本策略

- 保留**最新版本**和**里程碑版本**
- 里程碑版本由 Review/Decision 节点或用户手动标记
- 普通更新只保留最新版本，覆盖旧版本
- 里程碑版本永不自动删除

### 10.3 跨项目复用

- Artifact 默认属于 Project 级别
- 通过显式操作提升到 Organization 层后，可被其他项目引用
- 引用关系被跟踪（A 项目的 artifact 被 B 项目引用了）
- 提升到 Organization 层需要审批

---

## 11. Workspace 与交互设计

### 11.1 混合交互模型

```
┌─────────────────────────────────────────────────────────┐
│                    Web Workspace                         │
│  ┌────────────────────────┬────────────────────────┐    │
│  │   结构化工作区          │    对话面板             │    │
│  │                        │                        │    │
│  │  ┌──────────────────┐  │  ┌──────────────────┐  │    │
│  │  │  Graph 视图      │  │  │  对话式交互       │  │    │
│  │  │  (DAG 可视化)    │  │  │  (探索、提问、    │  │    │
│  │  └──────────────────┘  │  │   快速操作)       │  │    │
│  │  ┌──────────────────┐  │  │                   │  │    │
│  │  │  Artifact 面板    │  │  │  汇报接收 &      │  │    │
│  │  │  (文件管理)       │  │  │  决策回复         │  │    │
│  │  └──────────────────┘  │  │                   │  │    │
│  │  ┌──────────────────┐  │  └──────────────────┘  │    │
│  │  │  Memory 面板      │  │                        │    │
│  │  │  (记忆浏览/编辑)  │  │                        │    │
│  │  └──────────────────┘  │                        │    │
│  │  ┌──────────────────┐  │                        │    │
│  │  │  Timeline 视图    │  │                        │    │
│  │  │  (项目时间线)     │  │                        │    │
│  │  └──────────────────┘  │                        │    │
│  └────────────────────────┴────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

**核心原则：**
- Workspace 承载项目真实状态（graph、artifact、memory）
- 对话是交互入口之一，但不承载状态
- 用户可以纯用对话驱动操作，也可以直接在 workspace 中操作
- 移动端只提供只读查看 + 汇报响应 + 关键决策回复

### 11.2 多视图

| 视图 | 用途 | 核心元素 |
|------|------|---------|
| **Graph 视图** | 查看科研流程全貌 | DAG 可视化、节点状态、分支、决策点 |
| **Timeline 视图** | 按时间查看项目进展 | 时间线、里程碑、活动日志 |
| **Artifact 视图** | 管理科研产物 | 文件列表、版本、引用关系 |
| **Memory 视图** | 浏览和编辑记忆 | 分层记忆列表、来源链接、冲突标记 |
| **Dashboard 视图** | 项目概览 | 状态摘要、预算消耗、待决策项 |

---

## 12. 协作模型

### 12.1 角色权限

| 角色 | 权限 |
|------|------|
| **PI / 管理员** | 全部权限 + Organization Memory/KB 管理 + 预算设定 |
| **研究员** | 项目级全部操作 + 提交 Memory 提升请求 |
| **学生** | 项目级操作（部分需要 review） + 不可直接修改 Organization 层 |
| **只读协作者** | 查看项目状态和 artifact，不可操作 |

### 12.2 Agent 归属

- **Project 级 Agent**：每个 Project 有一个统一的 Agent（= Project Harness + 配置）
- 个人差异通过 User Memory 和个人 .md 注入 Agent context
- 避免多 Agent 在同一 Project 中操作导致的冲突
- 不同用户对同一 Project Agent 发出的指令，按权限控制执行

---

## 13. 技术实现要点

### 13.1 技术栈方向（基于 Related Work 调研 R-00）

**三层技术架构：**

| 层 | 建议方向 | 理由 |
|----|---------|------|
| **Graph 运行时层** | 自研薄 agent loop（`app/core/agent_loop.py`） | R-00 曾建议基于 LangGraph 构建，P0-01 原型验证后否决（决策 D1）：其 StateGraph 与我们的 Research Graph 构成双层图概念冲突；且 mid-loop 预算检查、证据链追踪、completion criteria 这类科研定制钩子在自研 while 循环中更自然。状态机、持久化、错误恢复均自研（checkpoint / transcript / 恢复路径）|
| **模型层** | Claude Agent SDK + MCP | 生产级 agent 循环 + MCP 原生工具集成 + 多模型支持 |
| **记忆层** | 参考 Mem0 图结构，自研科研分层 | Mem0 的图增强记忆提供了性能基准（91% 延迟降低），但缺乏科研语义分层，需要在其架构思路上扩展 |

> 注：本表已由 P0-01 原型验证收口（见 `tech_stack.md` 与 `design_decisions.md` 的 D1–D4 附表）。R-00 的 Graph 运行时层建议（LangGraph）在 D1 中被否决，改为自研；模型层与记忆层方向维持不变。

### 13.2 LLM 策略

- 支持多模型（Claude、GPT、开源模型等）
- 不同节点可使用不同模型（如 Survey 用长上下文模型，Experiment 用代码能力强的模型）
- 用户/项目可配置模型偏好
- 系统保持模型无关的 harness 设计

### 13.3 Context Engine

每个节点的 context 按以下优先级组装：

```
1. System Prompt（节点 harness 定义）
2. Project Memory（当前项目的相关记忆）
3. Handoff Content（前驱节点的输出摘要）
4. KB Retrieval（从知识库检索的相关内容）
5. Organization Memory（组织级规范和规则）
6. User Memory（用户个人偏好）
7. Session Context（当前会话状态）

裁剪策略：当总 token 超限时，按优先级从低到高裁剪
```

### 13.4 部署

- Phase 1：纯云 SaaS
- Phase 2+：支持私有化部署（面向数据敏感的企业/机构）

---

## 14. MVP 范围与路线图

### Phase 1：MVP（目标：验证核心价值）

**包含的节点：** Survey、Planning、Experiment、Analysis（4 个核心节点）

**包含的能力：**
- Research Graph 基础版（seed graph 生成、单分支、循环支持）
- 知识库基础版（论文入库、检索、段落级索引）
- 记忆系统基础版（Project Memory + Session Memory）
- 单人使用模式
- Web Workspace 基础版（Graph 视图 + 对话面板）
- 辅助模式为主

**不包含：**
- 多分支并行
- Organization 层
- 多人协作
- Writing / Review 节点
- 全自主模式
- 移动端
- 私有化部署

### Phase 2：完善（目标：完整科研循环）

- 增加 Writing、Review/Decision、Data Process 节点
- 多分支并行
- 全自主模式
- Organization Memory + KB
- 汇报等级和通知系统
- Artifact 版本管理

### Phase 3：协作与规模化

- 多人协作 + 角色权限
- Organization 层完整实现
- 移动端（只读 + 通知）
- 私有化部署选项
- 领域扩展（模板库）
