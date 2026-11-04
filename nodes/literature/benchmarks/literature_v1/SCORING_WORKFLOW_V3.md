# LiteratureBench v0.3 跨平台正式评分操作流程

## 当前已经准备好

- 冻结结果：`results/sciencemate_nl30_final_scoring_20261016.json`
- ScienceMate 单平台预备盲评表：`annotation/final_scoring_20261016/reviewer_A.csv` 与 `reviewer_B.csv`
- 原始名次映射：`annotation/final_scoring_20261016/rank_audit.json`，只交给复算人员
- 固定问题要点：`annotation_facets_30_v3.yaml`
- 七维标准：`scoring_standard_30_v3.md`

当前551条材料只来自ScienceMate，适合检查表格和开展预标注，**不是跨平台最终Gold Set**。取得其他AI平台结果和独立种子论文后必须重新生成统一盲评包。

## 1. 收齐所有平台结果并生成统一候选池

所有平台使用相同题目和运行条件，各自保存原始顺序。独立检索种子也整理成相同的 `task_id + papers` JSON结构，然后一次性生成盲评包：

```bash
python nodes/literature/benchmarks/literature_v1/prepare_annotation_v3.py \
  --results <sciencemate.json> <platform_a.json> <platform_b.json> \
  --seed-results <independent_seed_papers.json> \
  --taskset nodes/literature/benchmarks/literature_v1/tasks/balanced_natural_language_30_v3.yaml \
  --facets nodes/literature/benchmarks/literature_v1/annotation_facets_30_v3.yaml \
  --out <pooled_review_directory>
```

工具按DOI优先去重，纳入各平台前20条、各平台深层固定抽样和全部独立种子。评审CSV不含平台和排名；这些信息只保存在私有 `rank_audit.json`。

## 2. 两位评审独立填写

两人分别填写自己的 CSV，完成前不得互看。字段取值见盲评包 README。每一条都必须提供 `evidence_url` 和一句 `rationale`。资料不足时使用 `unknown` 或 `unresolved`，不能猜；这些状态会暂停正式出分。

## 3. 合并并生成仲裁表

```bash
python nodes/literature/benchmarks/literature_v1/merge_reviews_v3.py \
  --reviewer-a <pooled_review_directory>/reviewer_A.csv \
  --reviewer-b <pooled_review_directory>/reviewer_B.csv \
  --out <pooled_review_directory>/adjudication.csv
```

工具会先验证候选集合、评审身份、标签范围、证据链接和理由。两人一致的标签自动写入 `final_*`；不同的字段留空，并列在 `conflict_fields`。

## 4. 仲裁分歧并冻结qrels

仲裁者只处理分歧、`unknown` 和 `unresolved`，填写全部缺失的 `final_*`、`adjudicator_id` 与 `adjudication_rationale`。不能删除难判断的论文，也不能把空值默认为 0。

仲裁完成后，相关度≥2的是Gold Set正例，相关度0–1的是已判断负例；完整仲裁表就是统一qrels。所有平台使用同一份qrels。

## 5. 分别计算各平台七项独立分数

```bash
python nodes/literature/benchmarks/literature_v1/score_v3.py \
  --results <one_platform_results.json> \
  --taskset nodes/literature/benchmarks/literature_v1/tasks/balanced_natural_language_30_v3.yaml \
  --adjudication <pooled_review_directory>/adjudication.csv \
  --out <one_platform_scores.json>
```

存在缺失标签、未仲裁分歧、`unknown` 或 `unresolved` 时，工具拒绝生成正式分数。输出逐题七项百分制、各领域均值、六领域等权均值和 30 题直接均值；`weighted_total` 固定为 `null`，不合成总分。

对每个平台分别运行一次，随后按同一维度并列比较；不比较回答文风，不合成总分。

## 6. 发布前复核

运行自动测试：

```bash
.venv/bin/pytest -q \
  nodes/literature/benchmarks/literature_v1/test_benchmark_tools.py \
  nodes/literature/benchmarks/literature_v1/test_v3_scoring_tools.py
```

正式报告还需同时附上运行成功率和耗时、深层结果审计、低召回诊断、Crossref 元数据核验及人工评审覆盖率。
