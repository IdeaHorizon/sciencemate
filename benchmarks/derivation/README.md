# derivation benchmark：harness 到底值不值那些 token

> 纲领：`docs/DERIVATION_NODE_DESIGN_20260822.md` §Q1.5
> 计划：`docs/DERIVATION_ITERATION_PLAN_20260822.md` Line M

## 这套 benchmark 要回答什么

**「比裸 agent 强」在此之前是一句没有证据的断言。** 这里把它变成数字。

四个指标，各对应 harness 的一条价值主张：

| 指标 | 对应主张 | 怎么算 |
|---|---|---|
| `false_detection` **假命题识别率** | 错误在发生那步被抓住 | 命题本身是错的，它证伪了还是"证明"了 |
| `assumption_recall` **假设完备率** | 假设不会静默丢失 | 该出现的假设，账本里有几条 |
| `disclosure` **未验披露率** | 诚实的残差 | 有没有说清哪些步骤没被机械验证 |
| `correctness` 结果正确率 | （对照项） | 最终表达式与 ground truth 规范化等价 |

**预期：harness 赢在前三项，不在第四项。** 如果四项全面 ≈ 裸模型，
说明 harness 没在付钱 —— 这个结论如实入报告，那正是先测量的意义。

## 公平性的机械保证

两臂产出**同一形状的 submission**，判分器跑**同一套代码**：

```json
{"final_expression": "...", "assumptions": [...],
 "verdict": "derived|refuted|inconclusive", "unverified_steps": [...]}
```

- A1（节点）：从冻结的 `derivation_log.metadata` 机械提取
- A0（裸模型）：prompt 里给同样的任务全文 + 要求输出这个 JSON

⚠️ 裸臂的 prompt 必须给足格式要求 —— 不给会把「机制之差」测成「格式之差」。

`unverified_steps` 两臂来源不同（A1 框架现算 / A0 模型自报），这**正是要测的
差异**：harness 让诚实的残差变成机械产物，裸模型只能靠自觉。报告里如实标注。

## 任务集

16 题，两类：

- **正常推导 12 题**：ground truth 已知，覆盖统计物理 / 力学 / 统计 / 工程
- **假命题 4 题**：预注册的命题**微妙地错**，正确行为是证伪并给反例

一题一个自包含 yaml（`tasks/`）：上半是 fixture（`run_node --fixture` 直接吃），
下半是 `ground_truth`（judge 读，**不给模型看**）。

## 跑

```bash
python benchmarks/derivation/run_bench.py --arm A0 --tasks all
python benchmarks/derivation/run_bench.py --arm A1 --tasks all
python benchmarks/derivation/grade.py --results benchmarks/derivation/results/
```
