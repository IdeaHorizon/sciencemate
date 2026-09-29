---
name: systematic_literature_search
description: |
  对一个研究问题做系统化文献检索的 SOP（从宽到窄、识别核心子领域、记录关键论文、
  写 open_question memory、产 survey_report）。当 literature 节点刚启动、上游让你
  "做 X 调研" 或项目要进入新方向时按本指引做。
  不要用在：已经清楚要测什么 hypothesis 只想 1-2 篇 confirm —— 那种针对性查
  直接 search_papers / arxiv_search 就够，不用系统化框架。
applies_when:
  - 项目刚开始进入一个新研究方向
  - 需要绘制某领域的知识现状 + 识别空白点
  - 上游 user prompt 要求 "做 X 的文献调研"
tools_used:
  - search_papers
  - arxiv_search
  - memory_note
  - save_artifact
expected_outcome: 一个 survey_report artifact + 若干 category=observation/tags=paper:* / category=observation/tags=open_question 的 memory candidates
status: validated
relevant_concepts: []
---

## 工作流

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
7. **保存**：调 `save_artifact(artifact_type='survey_report', ...)`
8. **空白点 task**：对每个识别出的空白点，额外用 `task(action='create', title='调研 <空白点>', owner_node='literature')` 入 first-class task list；如果只是观察性记录（不是 actionable）也可以 `memory_note(category='observation', tags=['open_question'])`

## Pitfalls

- **不要编造引用**。每篇论文必须来自工具返回
- **少于 5 篇就明说**，不要硬凑
- **不要直接调 KB 写入工具**（create_claim / create_concept / 等）—— 那是 _curator 节点结束后整合的活
- 优先看较新的 综述论文，能省大量时间
- 论文之间的分歧不是噪音 —— 那就是空白点
