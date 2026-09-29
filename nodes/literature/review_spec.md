# Review spec for literature 节点产物（`survey_report`）

> 当 _reviewer 节点审 literature 产的 survey_report 时，按本 spec 走 5 维度审稿。
> 这套 rubric 是 v0.1 起点设计（架构团队提供，literature 节点 owner 接手迭代）。
> owner 可：保留照用、改维度权重 / 红线、删不相关维度、加你领域特定维度。

## 审稿前必做

reviewer 拿到 `artifact_id + source_node_type='literature' + producer_run_id` 后：

1. `read_artifact(artifact_id)` 拿 survey_report 全文
2. `list_artifacts(producer_run_id=...)` 看本 run 副产物（chunk 登记没 / open_question memory 有几条）
3. `search_kb()`（不传 entity_type）—— 看 KB 现 concept / claim / chunk 状态，判断 survey 是否漏知识
4. （可选）`memory_recall(query='ingestion_backlog')` —— literature 该把"我们 KB 缺 X paper"写进手册而不是 survey_report；看是否走对路了

## 审稿维度（每项 1-5 整数；**不允许** 2.5 / 3.5）

### 1. 覆盖完整性 (coverage)

survey 是否覆盖该研究方向的关键工作？还是只挑容易找的几篇？

- 看 survey_report 的"主题概览"+"关键论文（时间线）"是否包含
  **领域综述论文**和**近期前沿工作**
- 抽查 1-2 个**显然该有但 survey 没提的工作**（reviewer 用
  `search_papers` 跑一遍 user 给的 research_question）
- 5 分：覆盖明显主线 + 1-2 条 dissenting 派系
- 1 分：只有零散几篇，遗漏经典或大综述
- **核心警惕**：cited papers < 5 算 minor；< 3 算 major（除非 research_question 本就是冷门 niche，需 survey 自己说明）

### 2. 作者 wiring (author_wiring)

每篇登记的 chunk 是否传了 `author_concept_ids`？这是 KB v3 设计的关键 ——
未来 curator 跑作者轨迹分析 / 找 author co-citation pattern 全靠这层 wiring。

- `list_artifacts` 看 chunk 是否登记过（literature 应当用 `kb_ingest`
  登记 paper chunk；本 run 内的 artifact 走 `freeze_artifact`，冻结返回里带 chunk_id）
- 抽查 3 个 chunk，`get_kb_record('chunks', chunk_id)` 看
  `author_concept_ids` 字段非空
- 5 分：100% chunk 都有 author wiring；1 分：0 个 chunk wiring

### 3. Gap 识别正确性 (gap_identification)

survey_report 列的"空白点"是 **research gap**（外部文献缺）还是 **KB ingestion backlog**（我们 KB 缺某 paper）？

- 扫 survey_report "空白点列表" 段
- 红线措辞（critical 红线）：含
  - "is not represented in the KB" / "absent from the KB"
  - "gap in KB coverage" / "(not) currently in KB"
  → 这些是**自指 KB**，写错地方了（该走 memory_note tags=['ingestion_backlog']）
- 5 分：所有空白点都是"published 文献世界本身缺 X"
- 1 分：超过半数空白点是 KB 自指措辞

### 4. 诚实 (honesty)

literature 是否实事求是？

- 看是否承认 "相关论文少于 5 篇" / "该 niche 文献稀疏"（而不是凑数）
- 看是否标注**分歧 / 公认观点 vs 异见**（标了 = 高分）
- 看 "推荐下一步方向" 是否基于实际发现而非套话
- **绝不编造**：每篇 paper 必须能从 `kb_ingest` / `search_papers`
  / `arxiv_search` tool result 追溯（reviewer 抽查 2-3 篇引用，
  `search_kb` 或 `search_papers` 验真存在）

#### 元数据缺失的判定边界

检索源客观上可能不返回摘要、作者、DOI、CAS 分区或全文。缺失字段本身不应
直接判失败。Reviewer 应检查：

- 缺失是否明确标记为空值、`unavailable` 或 `fetch_failed`；
- 是否记录来源和尝试过的补齐策略；
- 是否没有用模型自行编写的内容冒充原始摘要或作者信息；
- 缺失字段是否影响筛选，并在报告中说明影响。

字段不全但如实记录是可接受结果；缺失却伪装成完整，才属于 honesty 或
data_provenance 问题。

### 5. 数据完整性 (data_provenance)

survey_report 引用的 paper / claim / chunk 是否都能追溯？

- 抽查 3-5 个 `chunk_<id>` 引用 → `get_kb_record('chunks', chunk_id)` 验内容
- 抽查 3-5 个 paper 描述 → search/scholar 查标题验存在
- phantom chunk_id（KB 不存在的）= critical 红线
- open_question tag 的 candidate / task 是否真有？`memory_recall(query='open_question')` 或 `task(action='list')` 至少返 1 条（literature yaml qc 也有这条）

## 评分规则

- 每维度 1-5 整数
- `overall_score` = 5 维平均，**整数取整**
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
    "coverage": <int>,
    "author_wiring": <int>,
    "gap_identification": <int>,
    "honesty": <int>,
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
| approve_with_revisions | `proceed` 或 `revise` | minor concern → proceed；coverage 不全影响 hypothesis 设计 → revise |
| major_concerns | `revise` | target_node='literature'，feedback 给具体应补的子领域 / 漏的论文 |
| block | `escalate_to_human` | 通常 literature 不会被 block —— 真不行 escalate 让 user 决定要不要换方向 |

`redirect_upstream` 几乎不会用（literature 是上游头）。

## 跟邻居的边界

- _reviewer **不重做调研**（不调 semantic_scholar / arxiv 拉新 paper） —— 那是 literature 的活
- _reviewer **不直接改 survey_report** —— literature 根据 critique 改
- _reviewer **可写 review_critique + memory（observation）+ propose_to_curator**

## 跟 quality_check 的区别

literature 的 `completion_criteria.quality_checks` 已含：
- `at_least_one_open_question_memory` (≥ 1 条 open_question tag memory)
- `papers_have_author_wiring` (≥ 1 chunk 带 author_concept_ids)
- `survey_report_no_kb_self_reference` (survey 不含自指 KB 措辞)

这些是 **mechanical / formal** 检查（看 keys 不看 values）。本 review_spec 是
**deep / content** 检查（看 coverage 真不真、gap 列的是不是 research gap）。
两层都过 = 文献调研可信。
