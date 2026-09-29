# Review spec for experiment 节点产物（`experiment_log` / `raw_results` / `clean_results`）

> 先读取冻结的 current-run `clean_results.record_kind`，再选择审查轨道：
>
> - `record_kind: operation`：按下方 **operation 四项核验**；它审的是“本节点是否如实完成操作”，不是科学实验。
> - 其他 scientific record：按后文 **8 维度科学审稿**。
>
> 不得因为 operation 缺少 verdict、citation 或 sediment 而否定它；也不得把安装、构建或文件交付成功包装成科学结论。

## Operation 四项核验（仅 `record_kind: operation`）

先读冻结的 `raw_results` manifest，定位唯一 `operation_verification_receipt`，再逐项与
`clean_results`、`experiment_log` 对账。三者冲突、证据文件 hash 不匹配或不属于当前 run，均为 critical。

1. **证据完整性 (`evidence_integrity`)**：receipt、原始 stdout/stderr、构建日志或交付物是否已登记并可读；路径、hash、字节数是否一致？
2. **任务核验 (`task_verification`)**：`task_kind` 与 `objective` 是否明确；完成声明是否有真实 returncode、import/version、可执行文件、JSON 解析、外部 job 终态或交付物证据支撑？
3. **范围与诚实 (`scope_and_honesty`)**：是否只报告实际完成的操作；是否把失败/部分完成写成成功，或把工程结果夸大为科学结论？
4. **失败或交接 (`failure_or_handoff`)**：`blocked` / `failed` 是否保留失败检查、具体 `next_step` 和 blocker（blocked 时）；`completed` 是否没有隐瞒失败检查？

每项 1–5 整数。operation 的 `approve` 必须四项均有证据、无 critical 且没有未解释的失败检查；
否则应 `revise` 或 `block`。不要要求科学八维、最终假说裁决或 methodological sediment。

operation 建议提交字段：

```json
{
  "verdict": "approve | approve_with_revisions | major_concerns | block",
  "per_dimension_scores": {
    "evidence_integrity": 1,
    "task_verification": 1,
    "scope_and_honesty": 1,
    "failure_or_handoff": 1
  },
  "recommended_action": "proceed | revise | abort | escalate_to_human"
}
```

## Scientific 审稿轨（其他 record_kind）
>
> **v2.1 职责边界**：experiment 负责执行、测量、与冻结 prereg 阈值的比较，以及
> `provisional` / `inconclusive` 的**执行层评估**；Analysis（物理 node_type 仍为
> hypothesis）才负责最终的 per-hypothesis 科学裁决和 `research_state` 中的
> `validated` / `refuted` 状态更新。故本 spec 在原 6 维（执行 / 披露 / 复现 /
> failure / 数据 / 诚实）基础上增加 2 维：execution_assessment_quality 和
> methodological_sediment_substance。共 8 维。

## 开工时你已经有什么（v2.1 起框架组装，不用自己找）

- 本 spec 全文（框架直送 —— 不要再去 read_file 找它）
- 项目现状定向（各节点交付面 / research_state / MEMORY 头）
- 被审 artifact id 在 node_inputs 里；`read_artifact` 拿全文，长文分段读
- 需要深查生产过程时用 `read_producer_transcript`

**你的 token 应该花在证据工作上**：读产物全文、核对声明与证据、查 KB 引用
是否真实存在。形式性检查（字段在不在、frozen 没 frozen）机械层已经把过关 ——
你审的是机器审不了的那半句：**内容在科学上站不站得住**。

## 审稿维度（每项 1-5 整数；**不允许** 2.5 / 3.5）

### 1. 执行完整性 (execution_completeness)

实验**真跑了**还是只产了 markdown 报告？

- 看 transcript 是否含 `safe_execute_python` / `safe_run_bash` / `compile_latex` 等真执行类工具调用（v0.11 前的旧 run 记录为不带 safe_ 前缀的 `execute_python` / `run_bash`，同等有效）
- 看 raw_results artifact 是否落盘（不是只有 LLM 编的 markdown 表格）
- 看 raw_results 跟 experiment_log 报告的数字是否**一致**（抽 3-5 个数对账）
- 5 分：raw_results 完整落盘 + experiment_log 数字 100% 来自 raw_results
- 1 分：没有 execute_* 调用 / raw_results 缺 / 数字编造（**critical 红线**）

### 2. Deviation 披露 (deviation_disclosure)

实际执行**偏离 pre_registration / research_plan** 的地方是否**显式标注**？

- 抽查 prereg 的关键参数（sample_size、metric、condition list）vs experiment_log 实际值
- 看 experiment_log 是否有 "Deviation from Pre-Registration" 段（或同等）
- deviation 必须含**原因**（compute / time / dataset issue），不是只列"实际跑了 X"
- 5 分：所有偏离都明确披露 + 原因 + 影响估计
- 1 分：实际跟 prereg 不一致但不提（**critical 红线**：科研诚信底线）

### 3. 复现性 (reproducibility)

别人按 experiment_log 能跑出**同一结果**吗？

- 看 seed 是否记录（RNG / numpy / framework / data shuffle）
- 看 config 是否完整（hyperparameters / hardware spec / 软件版本 / 时间戳）
- 看代码是否落盘（`safe_write_file` 或 `save_artifact` 存 script，不是只在 transcript）
- 看 raw_results 文件路径是否 deterministic + traceable
- 看 `run_manifest` 是否存在，并核对 run_role、requires_hypothesis_verdict、protocol/dataset 引用以及 transcript/log 的 SHA256；
  有 `execution_precondition_witnesses` 时逐条读它（执行前提未满足的如实见证，不改变裁决义务，但影响结论分量）
- secondary run 不得仅因有输出就进入正式统计；若被纳入，必须有明确的晋升记录和可追溯原始文件
- 5 分：seed 全记 + config 全披露 + code 落盘
- 1 分：no seed / config 部分缺失 → 永远复现不出

### 4. Failure handling (failure_handling)

跑失败的 run / 中间 error 是否**报告**？

- 看 transcript 是否有 `safe_execute_python` status=error 但 experiment_log 没提
- 看 experiment_log 是否说明哪些 run 失败 / 部分完成 / 被 abort 重跑
- 看 replicate 数是否真达到 plan（实际跑了 5 个 vs plan 20 个就要披露原因）
- 5 分：所有失败 / 部分跑 / 重试都报告 + 处理理由
- 1 分：跑失败时直接砍数据，experiment_log 只报成功的（**critical 红线**：cherry-picking）

### 5. 数据完整性 (data_integrity)

raw_results 跟 experiment_log 报告的统计**一致**吗？

- 抽 5 个 experiment_log 引用的数字 → read raw_results 算出原值对账
- 看 aggregation（mean / std / median）是否在 experiment_log 算对——reviewer **没有**执行类工具（`safe_execute_python` 是 experiment 私有授权），简单聚合用推导核验工具（`check_step` 数值代入 / `dimensional_check`）或人工抽算做 sanity check；对不上且无法就地核实时 `recommended_action=revise` 回派 experiment 在受管环境重算并补证据
- 看 outlier handling 是否标注（删 outlier 必须显式 + 数量 + 阈值）
- 看 metadata.pdf_path / 引用的 `chunk_<8hex>` / `claim_<8hex>` 都能追溯

### 6. 诚实 (honesty)

experiment_log 是否**直面 negative / null result**？

- 实际数据如果跟 hypothesis 预期相反，experiment_log 是否如实报告（不是粉饰 / "数据需进一步分析"）
- 是否承认 noise / variance 大 / 趋势不显著
- 是否标注 "我们也不确定为何 X" 的段（vs "X is clearly because of Y"）
- 5 分：直面 null/negative + 标 confidence + 不 overclaim
- 1 分：null result 被 spin 成 "promising direction"（红线）

### 7. 执行层评估与 Analysis 交接质量 (execution_assessment_quality) —— v2.1

本维度不审 Experiment 的最终假说裁决；它审查 Experiment 是否把可审计的执行
证据和边界清楚地交给 Analysis：

- primary simulation 是否逐项记录 measured metrics、冻结 prereg 阈值比较、统计/
  稳健性检查与 replay evidence，而不是笼统声称“基本符合”？
- 是否只使用 `verdict: provisional` 或 `verdict: inconclusive`；是否**没有**调用
  `update_claim_status`、也没有宣称 `validated` / `refuted`？
- inconclusive 是否明确说明无法判定的原因和需要补充的证据？
- 相关 hypothesis 是否由冻结 prereg provenance 定位，且明确把下一步交给 Analysis？
- 5 分：执行证据、阈值比较和交接完整，结论边界诚实；
- 1 分：证据/阈值比较脱节、越权作最终裁决，或把工程成功包装为科学结论
  （**critical 红线**）。

### 8. Methodological sediment 真值 (methodological_sediment_substance) —— v2.0 新增

experiment 节点 v2.0 起接管 methodological / dead_end claim 沉淀。本维度评估：

- 是否真有 `create_claim(claim_type='methodological')` 或 `create_claim(claim_type='dead_end')` 调用？
- 或者 experiment_log 中**明确**写"本次未发现，因为 X"（合法 fallback）？
- methodological claim 是否真**跨项目可复用**（不是 project-bound 的 empirical）？
- dead_end claim 是否含 `dont_repeat_reason` 写明"以后哪种情况避免"？
- 5 分：≥ 1 条 well-formed methodological / dead_end claim 真跨项目可复用；或 explicit 解释为何没有
- 1 分：什么 sediment 都没产又不解释为何 → **critical 红线**：每次实验都该有一次 KB 复利尝试

## 评分规则

- 每维度 1-5 整数
- `overall_score` = 8 维平均，**整数取整**
- `verdict` 由 overall_score 推：
  - ≥ 4: `approve`
  - = 3: `approve_with_revisions`
  - = 2: `major_concerns`
  - ≤ 1: `block`
- **单维一票封顶**：`honesty` ≤ 2 或 `data_integrity` ≤ 2 时，verdict 最高
  `major_concerns`（平均分不得抬升）——诚实性或数据完整性崩塌不允许被其余
  维度的高分平均稀释。红线级情形（编造数据等）仍按红线走 `abort`/`escalate`。

## 门禁可执行性检查（BF-12，不计分）

审 transcript 时顺带核查：若 agent 在**同一道门**上连续 ≥3 轮重试，且该门的
报错只说"违规/不支持"而没把合法取值或格式送达（未经 schema enum、消息插值、
或消息逐字列全三条通道之一），记一条 concern 并标 `BF-12`，点名该门与其报错
文案。这**不扣被审 run 的任何维度分**——这是节点门禁的债，不是 agent 的错——
`recommended_action` 也不因此升级，但必须入账让节点 owner 看到。
依据（同一次真实运行内的对照）：说清合法形式的门代价 1 轮；只说违规的门代价
20+ 轮，且会诱发未经审计的绕路。

## 强制 metadata 字段（reviewer 存 review_critique 时必填）

```json
{
  "verdict": "approve | approve_with_revisions | major_concerns | block",
  "overall_score": <1-5 int>,
  "per_dimension_scores": {
    "execution_completeness": <int>,
    "deviation_disclosure": <int>,
    "reproducibility": <int>,
    "failure_handling": <int>,
    "data_integrity": <int>,
    "honesty": <int>,
    "execution_assessment_quality": <int>,
    "methodological_sediment_substance": <int>
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
| approve_with_revisions | `proceed` 或 `revise` | minor → proceed；deviation 未披露 / failure handling 缺 → revise |
| major_concerns | `revise` | target_node='experiment'，feedback 给具体补 seed / 跑漏的 replicate / 补 deviation 段 |
| block | `redirect_upstream` 或 `escalate_to_human` | 实验本身不可救（编造数据 / hypothesis 错误前提）→ redirect_upstream='hypothesis'；模糊 → escalate |

## 跟邻居的边界

- _reviewer **不重跑实验** —— 那是 experiment 节点的活
- _reviewer **不直接改 raw_results 数字** —— 任何修改回 experiment 重跑
- _reviewer **可写 review_critique + memory（observation）+ propose_to_curator**
- _reviewer **没有执行类工具**（`safe_execute_python` 属 experiment 私有授权，白名单里没有）：数值 sanity check 用其推导核验工具（`check_step` / `dimensional_check` / `limit_check` / `find_counterexample`）；需要真正重算对账时走 `recommended_action=revise` 回派 experiment，由产出方在受管环境重算
- _reviewer **不作最终假说裁决，也不调用 `update_claim_status`** —— 审核通过的
  执行证据应交给 Analysis，由 Analysis 更新 `research_state` 和
  `validated` / `refuted`。

## 与节点机械收尾门的区别

Experiment 已不再声明 `completion_criteria.quality_checks`。可机械判定的
要求由冻结门和 `experiment_contract_audit` 执行：

- 当前运行的 raw_results、clean_results 和 experiment_log 证据三件套必须冻结；
- scientific primary simulation 必须如实记录 `provisional` 或 `inconclusive`
  执行层评估；其他运行只记录真实 status 与 Credibility，不得伪造科学 verdict；
- verdict、sediment、结果证据与引用绑定的缺口会产生持久 blocker，不能只靠模型叙述放行；
- operation 只接受其非科学验证回执（`record_kind=operation`）。

本 review_spec 是独立的深度审查：

- 数据是否真与 raw_results 对得上；
- deviation 是否真实披露、是否 cherry-picking；
- 执行层评估、证据与向 Analysis 的交接是否站得住（维度 7）；
- methodological sediment 是否真能跨项目复用（维度 8）。

机械收尾门证明“证据闭环没有缺件”；review 评估“证据是否可信、解释是否站得住”；
最终科学裁决仍由 Analysis 单独完成。
