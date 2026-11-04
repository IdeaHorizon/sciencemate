# LiteratureBench-v1：跨平台学术文献检索评测

> 当前公开主任务集为 `tasks/esi22_topics_v1.yaml`（v0.3）。早期的
> `search_v1.yaml` / `e2e_v1.yaml` 是基于中国学科目录的设计草案，保留供内部讨论，
> 不与 ESI22 主榜混算。

另有用于比较ScienceMate与其他AI平台学术检索效果的六领域30题v3：
`tasks/balanced_natural_language_30_v3.yaml` 与
[`scoring_standard_30_v3.md`](scoring_standard_30_v3.md)。各平台结果与独立种子合并后统一双人盲评，七项分别按百分制报告，不合成为总分。

## 要回答的问题

本轮只考各平台共同能完成的“给定研究课题，查找并列出学术文献”。
主榜比较前排相关性、书目真实性、重复、元数据完整性、耗时和失败率。
E2E 综述、全文、图片、持久缓存及平台私有 artifact 不参加横向评分；
保留对应旧材料供平台内部诊断，但不要求对手提供这些能力。

## 目录

```
tasks/esi22_topics_v1.yaml  ESI 22 个文献计量领域的共同检索主任务集
tasks/esi22_e2e_pilot_v1.yaml 旧 E2E 试点集，本轮不运行/不评分
tasks/claim_validation_v1.yaml 指定引文真实性诊断（不与主榜混分）
protocol.yaml               冻结环境、可比性与运行要求
schemas/*.json              统一结果与 gold 标注形状
score.py                    机械计算检索指标；不判定语义相关性
gold/README.md              标注与双人仲裁流程
results/                    每个系统一次运行一个 JSON（默认不入库）
task_selection.md           学科抽样依据、公开集与内部回归集的边界
hermes_review.md            Hermes 已有 benchmark 产物的审阅与整合结论
```

## 首次运行

1. 冻结日期、账户层级、系统版本、网络条件和每题预算；
2. 先跑 12 个 `pilot` 搜索题，检查每个平台是否能导出足够元数据；
3. 建立隐藏 gold：两位领域标注者独立标 0–3 相关性，再仲裁；
4. 保存每次原始页面/导出、交互录像和规范化 JSON；
5. 用 `score.py` 计算共同检索分数；不计算 E2E 综述分。

Hermes 已有两套候选池可离线合并，但不能跳过人工标注：

```bash
python scripts/benchmark/merge_candidate_pools.py \
  --out /secure/literaturebench-v1-candidates.json
# 双标、仲裁后：
python nodes/literature/benchmarks/literature_v1/build_gold.py \
  --adjudicated-candidates /secure/literaturebench-v1-candidates.json \
  --out /secure/literaturebench-v1-gold.json
```

```bash
python nodes/literature/benchmarks/literature_v1/score.py \
  --gold /secure/literaturebench-v1-gold.json \
  --results nodes/literature/benchmarks/literature_v1/results/harness.json
```

Harness 学术搜索可使用环境变量运行，输出会直接符合结果 schema：

```bash
export BENCHMARK_API_BASE=http://127.0.0.1:28081/api/v1
export BENCHMARK_ACCESS_TOKEN='...'
python nodes/literature/benchmarks/literature_v1/run_harness_search.py --pilot \
  --remote-refresh --out nodes/literature/benchmarks/literature_v1/results/harness_esi22_pilot.json
```

每个平台的结果转换为统一 JSON 后，先做协议检查；这一步只检查格式、排序、
上限、失败状态和尝试记录，不判断论文真伪与相关性：

```bash
python nodes/literature/benchmarks/literature_v1/validate_results.py \
  --results nodes/literature/benchmarks/literature_v1/results/harness.json <other_system.json>
```

若首轮失败，最多只对失败题补测一次；合并时保留两次尝试并累计耗时：

```bash
python nodes/literature/benchmarks/literature_v1/run_harness_search.py --ids ESI06,ESI13 \
  --remote-refresh --out <retry.json>
python nodes/literature/benchmarks/literature_v1/combine_retries.py \
  --first <first.json> --retry <retry.json> --out <combined.json>
```

收齐各平台原始结果后，先把所有返回论文并入待标注池，再生成新的盲评表。
盲评 CSV 隐去系统来源，提供 DOI/URL 直达链接；缺 DOI 时使用规范化标题作为身份键，
不得把不同论文混成一条。

最新 12 题探索性试跑、来源/摘要覆盖和待标注状态见
[`PILOT_REPORT_20260918.md`](PILOT_REPORT_20260918.md)。

```bash
python nodes/literature/benchmarks/literature_v1/merge_result_candidates.py \
  --candidates nodes/literature/benchmarks/literature_v1/results/candidates_20260917.json \
  --results <harness.json> <other_system.json> \
  --out <merged_candidates.json>
python nodes/literature/benchmarks/literature_v1/prepare_annotation.py \
  --candidates <merged_candidates.json> --out <new_review_directory>
```

旧的 318 条 pilot 标注表只是初始候选；有新增返回论文时需重新生成标注表。

当前试跑状态见 `scripts/benchmark/results/harness_baseline.md`。HTTP 成功但论文列表为空时，
runner 记为 `partial` 并保存候选数、过滤数和来源告警；这类运行不进入质量分均值。
真正的 literature 节点 E2E 应在执行 `run_node.py --harness literature` 后审计 run 目录：

```bash
python nodes/literature/benchmarks/literature_v1/audit_harness_e2e.py output/<run_id>
```

旧 `scripts/benchmark/run_harness_e2e.py` 只检查学术搜索接口，不能代替节点 E2E。
正式 gold 必须覆盖所有参评系统返回的前 50 篇；有未标注结果时暂停相关性和召回评分。

## 禁止事项

- 不把宣传的论文库规模当检索质量；
- 不在各系统之间人工补关键词、补 DOI、补摘要；
- 不把平台未提供的功能记为失败；
- 不让 judge 通过文风猜测事实正确性；
- 不把模型生成摘要冒充来源原始摘要；
- 不用某系统第一次结果指导另一个系统的 prompt。
