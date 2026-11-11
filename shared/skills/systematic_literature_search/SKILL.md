---
name: systematic_literature_search
description: |
  对一个研究问题做系统化文献检索的 SOP（从宽到窄、识别核心子领域、记录关键论文、
  写 open_question memory、产 survey_report）。当 literature 节点刚启动、上游让你
  "做 X 调研" 或项目要进入新方向时按本指引做。
  不要用在：已经清楚要测什么 hypothesis 只想 1-2 篇 confirm —— 那种针对性查
  使用下文的定向查证路径，不用系统化框架。
applies_when:
  - 项目刚开始进入一个新研究方向
  - 需要绘制某领域的知识现状 + 识别空白点
  - 上游 user prompt 要求 "做 X 的文献调研"
tools_used:
  - memory_note
expected_outcome: 按请求模式交付 survey_report/literature_index 或 literature_evidence_package；消费方维护自己的证据记录
status: validated
relevant_concepts: []
---

## 工作流

### 先按实际能力选择入口

- **Literature** 有直接检索工具，执行下列检索和服务交付步骤。
- **Observation 等消费方** 有 `run_node` 且获准调用 Literature 时，把检索交给服务：
  ```python
  run_node(node_type="literature", user_note="正在委派文献检索，随后核对覆盖与筛选。",
           node_inputs={"research_question": "<问题、检索式、来源、时间范围与排除标准>",
                        "mode": "landscape"})
  ```
  消费返回的 `survey_report` / `literature_index`，用 `read_artifact` 读完整产物。
  只需几篇定向查证时改为 `mode="targeted_lookup"`，消费 `literature_evidence_package`。
  Observation 自己维护 search_protocol、覆盖分母和 observation_log，不代写服务的报告。
- 两条路径都不可用时，报告检索能力缺口并交回调用方；不尝试未授予的工具。

### Literature 的直接检索路径

1. **宽 query**：用 `search_papers`（或 `arxiv_search`）跑 2-3 条宽泛查询，覆盖问题的不同表述方式
2. **聚类主题**：扫读结果 title / abstract，按主题聚类，识别 2-3 个核心子领域
3. **聚焦 query**：对每个子领域跑一次更精确的查询（加上方法名、技术词、年份过滤）
4. **记 paper**：对每篇**相关**论文，用 `memory_note` 存一条：
   - category: `observation`
   - text: "1 句话结论 + 关键数据 + 年份"
   - tags: `["paper:<key>", "domain:<子领域>"]`
   候选会在 run 结束 + curator dreaming 后被消化进 known_pitfalls / workflows / 等
   topic 文件（具体进哪类由 curator 判定）。
5. **饱和判断**：当最近 5 条新结果都不再带来新信息 → 停（这是饱和信号，别硬凑）
6. **写报告**：用 markdown 写结构化 `survey_report`：
   - 主题概览
   - 关键论文（按时间线）
   - 公认观点 vs. 分歧（分歧 = 空白点信号）
   - 空白点列表
   - 推荐下一步方向
7. **交付**：仅 Literature 按自己的服务输出契约保存报告与索引；定向模式交付 literature_evidence_package。
8. **空白点 task**：对每个识别出的空白点，额外用 `task(action='create', title='调研 <空白点>', owner_node='literature')` 入 first-class task list；如果只是观察性记录（不是 actionable）也可以 `memory_note(category='observation', tags=['open_question'])`

## Pitfalls

- **不要编造引用**。每篇论文必须来自工具返回
- **少于 5 篇就明说**，不要硬凑
- **不要直接调 KB 写入工具**（create_claim / create_concept / 等）—— 那是 _curator 节点结束后整合的活
- 优先看较新的 综述论文，能省大量时间
- 论文之间的分歧不是噪音 —— 那就是空白点
