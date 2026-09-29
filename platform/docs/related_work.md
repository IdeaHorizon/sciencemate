# Related Work 调研报告

> R-00 产出 | 2026-04-22
> 用途：竞品分析 + 差异化定位确认 + 论文 Related Work 初稿 + 对设计决策的反馈

---

## 一、竞品全景图

### 1.1 AI4Science 产品（文献/写作阶段工具）

| 产品 | 核心功能 | 覆盖阶段 | 定价 | 关键局限 |
|------|---------|---------|------|---------|
| **Elicit** | AI 文献搜索/摘要/数据提取，125M+ 论文 | 文献调研、系统综述 | 免费+Plus ~$10-12/月 | 无实验设计、无分析能力 |
| **Semantic Scholar** | AI 学术搜索，200M+ 论文，TLDR 摘要 | 文献发现 | 免费（API 限速） | 无综合分析、无写作 |
| **SciSpace** | 全流程：搜索 280M 论文 + PDF 对话 + 写作 + 期刊匹配 | 文献→写作 | 免费/Premium $12-20/月/Advanced $70/月 | 无实验/分析支持，深度 review 需高价 |
| **Consensus** | AI 搜索引擎，综合同行评审论文中的发现，含"共识度" | 文献综合 | 免费/Premium $8.99/月 | 仅限已发表文献中的 claims |
| **Scite** | 智能引用分析，1.5B 引用声明的支持/反驳分析 | 引用评估 | $12/月 | 仅引用上下文，范围极窄 |
| **Connected Papers** | 论文关系可视化（共引/耦合） | 论文发现 | 免费(5图/月)/学术 ~$5-6/月 | 纯发现，无 AI 综合 |
| **Research Rabbit** | 引用网络推荐引擎 | 论文发现 | 免费/付费 | 已被 Litmaps 收购，无 LLM 综合 |
| **Litmaps** | 动态文献图谱 + 新论文监控 | 文献追踪 | 免费/Premium $12.50/月 | 仅发现/追踪 |
| **Undermind** | 深度 AI 搜索，阅读数百篇论文并追踪引用链 | 深度文献搜索 | 免费增值/Pro 未公开 | 仅搜索/检索 |
| **OpenAI Prism** (2026.1) | 免费 LaTeX 科研写作工作台，集成 GPT-5.2 | 写作、协作 | 免费 | 无文献调研、无实验支持 |
| **FutureHouse/Edison** | 多专业 AI agent（Crow/Falcon/Owl/Phoenix/Finch）生物发现 | 文献→实验规划→发现 | 企业级（融资 $70M） | 生物/制药专用，非通用平台 |

### 1.2 Agent 框架

| 框架 | 架构 | 记忆系统 | 长任务 | HITL | 科研适用性 |
|------|------|---------|--------|------|-----------|
| **LangGraph** | 有向图，节点=函数，状态不可变+检查点 | 原生持久化，checkpoint/resume | 原生支持，96% 恢复率 | 内建断点 | checkpoint/resume、HITL 断点等**概念**值得借鉴；**未采用**（决策 D1，见 5.1）——StateGraph 与我们的 Research Graph 双层图冲突 |
| **CrewAI** | 角色制多 agent，manager-worker 层级 | 短期+长期+实体+上下文记忆 | 支持 | 有限 | 角色模式可参考 |
| **Claude Agent SDK** | Agent 循环+工具执行，MCP 集成 | 上下文窗口管理，无内建持久记忆 | 依赖外部管理 | 可配置 | **最适合做模型层** |
| **AutoGen** (MS) | 异步事件驱动 agent 消息通信 | 可插拔（Mem0/Redis/Neo4j） | 支持 | 有限 | 维护模式，迁移到 Agent Framework |
| **MetaGPT** | 流水线+SOP，角色化 | 共享消息池+订阅 | 支持 | 无 | SOP 模式可参考节点 harness |
| **OpenHands** | 事件流+感知-行动循环 | 事件流历史 | 支持 | 有限 | 软件工程专用 |
| **Manus** | Planner+Executor 多 agent，云 VM | 会话级 | 支持 | 有限 | 有"广泛研究"功能，最接近科研 |

### 1.3 科研工作流与知识管理工具

| 工具 | 类型 | 覆盖阶段 | 关键局限 |
|------|------|---------|---------|
| **Jupyter** | 交互式计算 | 探索、原型、分析 | 无流水线编排，版本控制差，不可复现 |
| **Nextflow/Snakemake** | 计算流水线 | 管线执行 | 生物信息专用，无知识层 |
| **Galaxy** | GUI 工作流平台 | 管线执行 | Web-only，生物信息外适用性弱 |
| **MLflow** | 实验追踪+模型注册 | ML 训练/评估 | ML-only，无文献/假设管理 |
| **W&B** | 实验追踪+可视化 | ML 训练/调参 | ML-only，规模化成本高 |
| **Zotero** | 参考文献管理 | 文献收集/引用 | 仅引用管理，无 AI，无实验连接 |
| **Obsidian** | 本地知识库+图谱 | 笔记/知识综合 | 无协作，无实验/管线集成 |
| **Notion** | 全功能工作区 | 项目管理/笔记 | 通用型，无科研领域功能 |

### 1.4 科研 Agent 学术论文

| 论文 | 年份/会议 | 做了什么 | 与我们的关系 |
|------|----------|---------|-------------|
| **The AI Scientist v1** | 2024, ICML | 全自主 ML 研究：生成想法→编码→实验→写论文→自审 | 全生命周期但无 graph/记忆/溯源 |
| **The AI Scientist v2** | 2025, arXiv | 去除人工模板，agentic 树搜索，首篇 AI 全生成论文被同行评审接收 | 更高自主性，但仍然会话级，无跨项目记忆 |
| **MLAgentBench** | 2024, ICLR | Agent 解 ML 任务的 benchmark | 仅评估，无工作流管理 |
| **ChemCrow** | 2024, Nature MI | LLM + 18 个化学工具，自主合成催化剂和新发色团 | 领域工具集成模式可参考，但单会话 |
| **Coscientist** | 2023, Nature | 多 LLM agent 规划+设计+执行湿实验（机器人） | 首个 LLM→机器人闭环，但无持久记忆/溯源 |
| **SciAgents** | 2024, Advanced Materials | 多 agent + 本体论知识图谱（1000 篇论文）生成研究假设 | **最接近 Research Graph 概念**——用图谱做知识驱动假设生成，但仅限构思阶段 |
| **Agent Laboratory** | 2025, EMNLP | 人在环路科研助手：文献综述→实验→报告，可选人工反馈，成本降低 84% | **展示 HITL 价值**，但无持久记忆/溯源 |
| **AgentRxiv** | 2025, arXiv | Agent 实验室的共享预印本服务器，agent 上传/检索报告协作 | 跨 agent 知识共享（类似 KB 分离），但用扁平文档 |
| **ResearchAgent** | 2024, NAACL 2025 | 从核心论文生成研究问题/方法/实验，审稿 agent 迭代优化 | 实体级知识库+迭代优化，但仅限构思 |
| **Virtual Lab** | 2024 | 多学科 LLM 专家 + 人类 PI 协作设计纳米抗体 | 强 HITL + 学科专业化，湿实验验证 |
| **SciSciGPT** | 2025, Nature Comp. Sci. | 5 个专业 agent 编排科学学工作流，比人快 10x | 最接近工作流管理+评估，但领域专用 |
| **MemGPT** | 2023, ICLR 2024 | OS 启发的两层记忆：核心记忆(上下文=RAM) + 档案/回忆(外部=磁盘) | **KB/Memory 分离的理论基础**——建立分层存储范式 |
| **Mem0** | 2025, arXiv | 生产级记忆层，图增强变体(Mem0g)存储有向标签图，91% 延迟降低 | 图结构记忆与 Research Graph 类似，但无科研溯源 |
| **Generative Agents** | 2023, UIST | 记忆流+反思+规划的模拟小镇 agent | 反思层级(观察→反思→计划)可映射到分层知识表示 |
| **Voyager** | 2023, NeurIPS | Minecraft 终身学习 agent，持久技能库 | 技能库模式类似持久 KB + 可复用研究 artifact |

**关键综述论文：**
- "Agentic AI for Scientific Discovery: A Survey" (Guo et al., 2025) — 按功能分类 agentic AI for science
- "From AI for Science to Agentic Science" (2025) — 统一过程/自主/机制导向视角
- "Memory in the Age of AI Agents: A Survey" (2025) — 记忆系统分类，识别认知校准缺口

---

## 二、空白分析（Gap Analysis）

### 2.1 核心空白：无平台覆盖完整科研生命周期

科研涉及 7 个阶段：(1) 文献调研 (2) 假设构建 (3) 实验设计 (4) 数据收集/计算 (5) 分析/解读 (6) 知识综合 (7) 写作/发表。

**没有任何现有工具覆盖超过 2 个阶段。** 研究者目前需要拼接 3-5 个工具（如 Elicit 做综述 + Connected Papers 做发现 + Jupyter 做实验 + W&B 做追踪 + Overleaf 做写作），造成上下文断裂和知识流失。

### 2.2 五个具体空白

| 空白 | 描述 | 我们如何填补 |
|------|------|-------------|
| **知识与计算断裂** | Zotero/Obsidian 管文献，Jupyter/MLflow 管实验，中间完全断裂。没有工具能从引用发现连接到验证实验，或从异常结果回溯到预测论文。 | Research Graph 将文献节点、假设节点、实验节点、分析节点统一在一张图上，通过 depends_on/informs/iterates 边显式连接 |
| **实验追踪 ML-only** | MLflow/W&B 只服务 ML 超参搜索。物理学家、化学家、社科研究者没有等效工具。 | 最小公约节点抽象适用于所有计算驱动学科，不绑定 ML 范式 |
| **无假设到论文的可追溯性** | 研究者手动从 Jupyter 复制结果到 Notion 再到 LaTeX。论文中的图表与产生它的分析/数据管线没有活链接。 | 分级溯源体系：每个结论 → 证据 → 数据/论文段落，全链路可追溯 |
| **AI 工具是点状方案** | 每个 AI 工具在单一阶段加持，但不编排跨阶段工作流，也不维护关于研究项目的共享上下文。 | 每个节点有独立 harness + 共享的 KB/Memory 系统 + Context Engine 按需组装 |
| **无跨项目知识复用** | 一个课题组做了 10 个项目，但每个新项目从零开始。失败经验、成功 workflow、参数经验无法系统性复用。 | Organization Memory/KB + Artifact 跨项目复用 + 记忆流动规则 |

### 2.3 最接近的竞品分析

| 竞品 | 与我们的重合 | 我们的差异化 |
|------|------------|-------------|
| **The AI Scientist (v1/v2)** | 全生命周期自主科研 | 我们有 Research Graph（它没有）、分层记忆（它是会话级）、HITL 机制（它是纯自主）、溯源体系（它没有） |
| **FutureHouse/Edison** | 多专业 agent + 科研发现 | 我们是通用平台（它是生物/制药专用）、我们面向研究者（它面向企业）、我们有 KB/Memory 分离（它没有公开相关设计） |
| **Agent Laboratory** | HITL 科研工作流 | 我们有持久记忆和 KB（它没有）、我们有 Research Graph 和分支管理（它是线性流程）、我们有溯源体系 |
| **SciAgents** | 知识图谱驱动假设生成 | 我们覆盖全生命周期（它只做构思）、我们有执行层（它没有）、我们的图是运行时状态图而非静态知识图 |
| **SciSciGPT** | 多 agent 工作流编排 | 我们是通用研究平台（它是科学学专用）、我们有跨项目记忆（它没有） |

---

## 三、差异化定位确认

### 3.1 我们的独特价值主张

经过全面调研，**我们的四个核心设计元素确实没有被任何现有系统同时覆盖**：

1. **Research Graph**——基于循环有向图的科研流程管理，含分支/合并/循环控制。最近的是 SciAgents 的知识图谱，但它是静态知识图，不是运行时状态图。

2. **KB/Memory 分离架构**——外部知识（论文/新闻）和内部经验（决策/结论）的严格分离。MemGPT 建立了分层存储范式，Mem0 做了图结构记忆，但都没有科研领域的语义分层。

3. **最小公约节点 + 节点级 Harness**——每个科研操作节点有独立的 prompt/skills/tools/context engine。MetaGPT 的 SOP 模式有相似的理念，但面向软件开发。

4. **分级溯源体系**——事实硬溯源、推理软溯源、段落级引用粒度。没有任何现有系统提供这种结构化的溯源层级。

### 3.2 论文 Contribution 定位

建议论文的核心 contribution 表述为：

> We present Research Graph, the first full-lifecycle AI-assisted scientific research platform that unifies:
> (1) a cyclic directed graph for research workflow management with branching, iteration tracking, and decision point detection;
> (2) a separated Knowledge Base / Memory architecture that distinguishes external knowledge from internal experience with different trust levels and update policies;
> (3) minimum common denominator nodes with independent harnesses that encode domain-agnostic research best practices;
> (4) a tiered provenance system that enforces hard traceability for factual claims and soft traceability for reasoning, at paragraph-level granularity.

### 3.3 潜在风险

| 风险 | 说明 | 应对 |
|------|------|------|
| **AI Scientist 系列快速进化** | Sakana AI 在 Nature 发文，势头强劲。v3 如果加入记忆和溯源，会直接侵入我们的空间 | 我们的差异化在 HITL + 平台化 + 组织级管理，这不是 AI Scientist 的方向 |
| **FutureHouse 拓展通用性** | Edison 融资 $70M，如果从生物拓展到通用科研 | 我们先发在记忆/溯源/Graph 设计上，且面向个人研究者不是企业 |
| **OpenAI Prism 扩展** | 如果从写作扩展到全流程 | OpenAI 的产品模式是通用工具，不太可能做科研专用 harness |
| **LangGraph + 开源社区** | 如果有人用 LangGraph 直接搭建类似平台 | 我们的壁垒在 harness 设计质量和记忆系统，不在基础框架 |

---

## 四、论文 Related Work 初稿

### Related Work

#### AI-Assisted Scientific Research

Recent years have seen growing interest in AI agents for scientific discovery. The AI Scientist (Lu et al., 2024; 2025) demonstrated end-to-end autonomous ML research, from idea generation through experimentation to paper writing. ChemCrow (Bran et al., 2024) and Coscientist (Boiko et al., 2023) extended LLM agents to chemistry with tool integration and robotic execution. SciAgents (Ghafarollahi & Buehler, 2024) introduced ontological knowledge graphs for hypothesis generation. Agent Laboratory (Schmidgall et al., 2025) showed that human-in-the-loop checkpoints significantly improve agent research quality while reducing costs by 84%.

However, these systems share common limitations: they operate within single sessions without persistent cross-project memory, lack structured provenance tracking, and do not provide the organizational-level knowledge management needed for sustained research programs.

#### Research Workflow and Knowledge Management

The scientific workflow landscape is fragmented. Computational pipeline tools (Nextflow, Snakemake, Galaxy) orchestrate data processing but lack knowledge management. Experiment trackers (MLflow, W&B) monitor ML training metrics but do not connect to literature or hypothesis management. Reference managers (Zotero, Mendeley) handle citations but not experimental data. Knowledge tools (Obsidian, Notion) support note-taking but lack computational integration.

AI-powered research tools have emerged to address individual stages: Elicit for literature review, Consensus for evidence synthesis, SciSpace for search-to-writing. Yet none spans the complete research lifecycle or maintains structured project state across stages.

#### Memory Systems for AI Agents

MemGPT (Packer et al., 2023) introduced a two-tier memory architecture inspired by operating systems, establishing the paradigm of in-context core memory and external archival memory. Mem0 (Choudhury et al., 2025) extended this with graph-enhanced memory supporting temporal reasoning. Generative Agents (Park et al., 2023) demonstrated hierarchical memory abstraction through observation-reflection-planning cycles. Voyager (Wang et al., 2023) showed how persistent skill libraries enable lifelong learning.

These systems provide foundational memory patterns but lack the domain-specific structure needed for scientific research — particularly the distinction between external knowledge (literature, reference data) and internal experience (decisions, failed approaches, project-specific conclusions), and the provenance chains required for scientific rigor.

#### Provenance in Scientific Computing

Provenance tracking in scientific workflows has been studied extensively in the context of data lineage and reproducibility. Souza et al. (2025) proposed LLM agents for querying workflow provenance data. However, existing provenance systems focus on data transformations rather than epistemic provenance — tracking why a conclusion was reached, what evidence supports it, and at what confidence level.

#### Our Contribution

Our work addresses the gap at the intersection of these areas. We propose a full-lifecycle research platform that combines: (1) a Research Graph for workflow management with support for the iterative, branching nature of scientific inquiry; (2) a separated Knowledge Base and Memory architecture with different trust levels and update policies; (3) minimum common denominator nodes that encode research best practices through independent harnesses; and (4) a tiered provenance system that distinguishes factual claims requiring hard traceability from reasoning requiring soft traceability. To our knowledge, no existing system unifies these four elements.

---

## 五、对设计决策的反馈

基于调研结果，以下是对现有设计的建议调整：

### 5.1 技术栈建议（影响 P0-01）

调研结果强烈建议：
- **~~基础设施层考虑 LangGraph~~ → 已否决（决策 D1，2026-04-23）**：调研当时建议基于 LangGraph 构建 Research Graph 运行时。P0-01 原型验证后改为自研薄 agent loop，理由：
  - **双层图冲突**：LangGraph 的 StateGraph 是运行时概念，我们的 Research Graph 是产品概念（节点=研究阶段，边=依赖/证据链）。两层图并存会各自演化且分叉时不报错。
  - **循环中段的钩子是我们的产品面**：mid-loop 预算检查、证据链追踪、completion criteria、停止信号到达即生效——这些要在轮内乃至 token 流一级插手，框架的 node/interrupt 粒度够不着。
  - 其 checkpoint/resume、HITL 断点等**概念**仍被借鉴，但不引入依赖。实现见 `app/core/agent_loop.py` 与 `app/core/checkpoint.py`。
- **模型层用 Claude Agent SDK**：MCP 原生支持，生产级验证。
- **记忆系统参考多源架构**：
  - **Mem0**：图增强记忆，91% 延迟降低。记忆间关系建模。
  - **Hermes Agent**（Nous Research）：有界记忆（MEMORY.md ~800 tokens + USER.md ~500 tokens）+ 容量感知合并（80%触发）+ 自主技能提取（Procedural Memory）+ FTS5 跨会话搜索 + 三级渐进加载。特别是"Research Skills"概念可直接借鉴。
  - **OpenClaw**：Workspace 文件驱动 Agent 行为（SOUL.md/AGENTS.md）+ Skills 注册表 + Cron 后台自动化。验证了 Markdown 定义 Agent 行为的有效性。
  - 我们的分层记忆在以上基础上增加：KB/Memory 严格分离 + 四层层级 + 科研溯源绑定 + 研究技能（非通用技能）。

### 5.2 需要重点研究的竞品

以下三个系统需要团队深入试用和分析：
1. **AI Scientist v2** — 阅读论文原文，理解其树搜索和实验管理机制
2. **Agent Laboratory** — 阅读论文原文，理解其 HITL 设计和成本优化
3. **FutureHouse Platform** — 申请 API 试用，理解其 agent 编排机制

### 5.3 对白皮书的小修正建议

1. **白皮书 1.2 节（为什么现有方案不够）**应更新加入 AI Scientist、FutureHouse、Agent Laboratory 的分析
2. **白皮书中应增加对 Sakana AI Scientist 的显式定位**——它是最直接的学术竞品，需要明确说明我们与它的区别（HITL vs 纯自主、平台 vs 工具、组织级 vs 会话级）
3. **论文的 evaluation 设计**（R-02）应考虑与 Agent Laboratory 做对比实验——它有 84% 成本降低的 baseline，我们需要展示在质量/溯源/记忆沉淀方面的优势

### 5.4 设计决策无需调整

调研结果**验证了以下核心设计决策的正确性**：
- **KB/Memory 分离**：MemGPT 验证了分层存储范式的有效性，但没有人做科研级的语义分层。我们的设计是对 MemGPT 范式的科研扩展，定位准确。
- **最小公约节点**：没有竞品有类似抽象。最近的是 MetaGPT 的 SOP 和 SciSciGPT 的专业 agent，但都是特定领域。
- **分级溯源**：没有任何现有系统提供这种层级的溯源。这是最强的差异化点之一。
- **双模式（辅助+自主）**：Agent Laboratory 证明了 HITL 的价值（84% 成本降低），AI Scientist 证明了全自主的可能性。我们同时支持两者是正确的。

---

## 六、引用列表（供论文使用）

### AI Research Agents
- Lu et al. "The AI Scientist: Towards Fully Automated Open-Ended Scientific Discovery." ICML 2024.
- Lu et al. "The AI Scientist-v2: Workshop-Level Automated Scientific Discovery via Agentic Tree Search." arXiv:2504.08066, 2025.
- Huang et al. "MLAgentBench: Evaluating Language Agents on Machine Learning Experimentation." ICLR 2024.
- Bran et al. "ChemCrow: Augmenting large-language models with chemistry tools." Nature Machine Intelligence, 2024.
- Boiko et al. "Autonomous chemical research with large language models." Nature, 2023.
- Ghafarollahi & Buehler. "SciAgents: Automating scientific discovery through multi-agent intelligent graph reasoning." Advanced Materials, 2024.
- Schmidgall et al. "Agent Laboratory: Using LLM Agents as Research Assistants." EMNLP 2025.
- Schmidgall et al. "AgentRxiv: Towards Collaborative Autonomous Research." arXiv:2503.18102, 2025.
- Baek et al. "ResearchAgent: Iterative Research Idea Generation over Scientific Literature with Large Language Models." NAACL 2025.
- Swanson et al. "Virtual Lab: AI Agents Design New SARS-CoV-2 Nanobodies with Experimental Validation." 2024.

### Workflow and Provenance
- Shao, Wang et al. "SciSciGPT: A Multi-Agent System for Automated Science of Science Research." Nature Computational Science, 2025.
- Souza et al. "LLM Agents for Interactive Workflow Provenance." SC'25 Workshops, 2025.

### Memory Systems
- Packer et al. "MemGPT: Towards LLMs as Operating Systems." ICLR 2024.
- Choudhury et al. "Mem0: Building Production-Ready AI Agent Memory." arXiv:2504.19413, 2025.
- Park et al. "Generative Agents: Interactive Simulacra of Human Behavior." UIST 2023.
- Wang et al. "Voyager: An Open-Ended Embodied Agent with Large Language Models." NeurIPS 2023.

### Surveys
- Guo et al. "Agentic AI for Scientific Discovery: A Survey." arXiv:2503.08979, 2025.
- "From AI for Science to Agentic Science." arXiv:2508.14111, 2025.
- "Memory in the Age of AI Agents: A Survey." arXiv:2512.13564, 2025.

### Industry / Products
- Sakana AI. "The AI Scientist." Nature, 2026.
- OpenAI. "Introducing Prism." January 2026.
- FutureHouse. "Launching FutureHouse Platform." 2025.
- Edison Scientific / Kosmos. AIwire, November 2025.
