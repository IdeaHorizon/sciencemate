---
name: literature_classify
description: |
  将一批论文按摘要自动聚类为若干研究主题，并生成结构化的 survey_report（含关键发现 + open questions）。
  当 literature 节点搜索完论文后需要整理归纳时按本指引做。
  不要用在：论文 < 5 篇（手动归类更快）；需要深读全文分类（当前基于摘要，适合粗略主题归类）。
applies_when:
  - 搜索完论文后需要按主题归类
  - 需要生成结构化的 survey_report 总结
  - 需要快速了解一批论文覆盖了哪些研究方向
tools_used:
  - classify_papers
  - archive_papers
  - save_artifact
expected_outcome: survey_report 与 literature_index 均保存同一份完整分类结果
status: validated
---
## 用 / 不用 本 skill

**用 when**：
- 搜索完论文后需要按主题归类整理
- 需要快速生成 survey_report

**不用 when**：
- 论文 < 5 篇 → 手动归类
- 需要深读全文精确分类 → 仍基于摘要

## 工作流

1. **收集论文**：从 `search_papers` 的返回中提取 papers 列表
2. **调 classify_papers 工具**：把 papers 列表传入，指定最多 4-6 个聚类
3. **审查结果**：检查 clusters 是否合理（每个主题有至少 2 篇论文）
4. **立即保存索引**：调用 `archive_papers`，将完整分类返回传入
   `classification_json`，生成 `literature_index`
5. **立即保存报告**：用 `save_artifact` 保存完整分类结果 + `survey_report`，且与索引一致
6. **核验核心产物**：调用 `list_artifacts`，确认 `literature_index` 和
   `survey_report` 同时存在；缺哪个只补哪个，不重新搜索或分类
7. **完成 author wiring**：以检索结果顶层 `papers[]` 为准。对每篇有作者信息
   且有外部锚点的论文，创建/复用作者 `person` concept，并用
   `kb_ingest(..., author_concept_ids=[...])` 登记（本 run 内的 artifact 改走
   `freeze_artifact`，冻结返回里带 chunk_id）；
   不要只看 clusters 中只有 title/doi 的简化记录。
8. **终检**：逐篇对照 `papers[]` 的 `authors` 与登记清单。作者信息确实缺失时
   记录 `author_wiring_unavailable`，不得编造；其余论文不得以“尽力”代替 wiring。
   至少用 `list_artifacts` / KB 查询确认存在带 `author_concept_ids` 的 chunk，
   再结束节点

## 输入

- papers 列表（可从 `search_papers` 工具返回获得）

## 输出

- 一个 literature_index artifact，含论文元数据、完整分类与全文获取状态
- 一个 survey_report artifact，含：
  - 聚类结果（每组名称 + 论文列表）
  - 关键发现（2-3 条）
  - Open questions（2-3 条）

## Pitfalls

- ❌ 论文太少就分类 → 少于 5 篇时结果无意义
- ❌ 期待全文级分类 → 当前基于摘要适合粗略分主题
- ❌ 分类后先做大量作者 concept / KB ingest → 必须先保存并核验两个核心 artifact
- ❌ 只在最终回答里声称完成 → 必须实际调用 `archive_papers` 和 `save_artifact`
- ❌ 缺产物时重新搜索 → 只补缺失的 artifact
