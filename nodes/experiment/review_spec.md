# Experiment v3 交付审查

本规范审查 Experiment 是否按当前任务诚实执行和交付。审查对象是 `finish` 冻结的同一笔 `raw_results → clean_results → experiment_log`，而不是另一次科学裁决。

## 先确定要审什么

1. 读取被审 `experiment_log`，并读取同一 run、同一 delivery 的冻结 `raw_results` 和 `clean_results`。以 `clean_results.delivery` 为交付主张，核对 `raw_results.delivery`、日志、关键原始文件与作业事实；只追查与主张有关的证据。
2. 三件套的 `metadata.record_kind=experiment_delivery` 表示统一交付格式，**不是** operation/scientific 分类。需要描述任务目的时读取 `clean_results.delivery.scope` 和原任务、正式输入；`scope=unknown` 不自动变成 scientific，也不单凭分类不明否定有证据的交付。
3. `outcome` 描述本次交付：`success` 要有任务验收证据；`partial`、`failed`、`blocked`、`cancelled` 则审已发生与未发生的事实、限制和下一步。诚实报告任务未成功也可以得到 `approve`。
4. 只有当前 run 确实绑定了 prereg，才核对冻结版本、实际使用参数、约定检查与偏离；没有 prereg 不要求补造。没有 prereg 的任务仍可能是 scientific，应按其真实目标审执行与证据。Reviewer 不作最终假说裁决；科学意义交给 Analysis 和用户。

若平台 prereg 绑定失效，但用户仍随附研究计划或预注册文件，核对交付是否披露绑定缺口、实际参数与随附文件是否一致。不能把随附文件伪称为已正式绑定的冻结 prereg，也不得仅因绑定失效要求重算已核实的数值。

若唯一缺口是已披露的上游绑定失效，交付产物、数值、实际参数来源与适用验收均有证据，且无失管作业，应对诚实的 `partial` 给出 `approve` 和 `recommended_action=proceed`，把绑定修复交还上游 owner，不回派 Experiment 补内部手续或重算。若绑定缺口使具体任务要求不可核验，应指出该要求及缺失证据。

## 四项共同核验

每项给 1–5 整数分；按任务适用性核验，不制造额外的实验手续。

1. **证据完整性 (`evidence_integrity`)**：关键原始输出、日志、作业终态和交付对象可追到实际 action/attempt；抽查决定性数值、文件身份及三件套是否一致。审查时读不到的证据如实标明，不能假称独立重算过。
2. **任务验收 (`task_verification`)**：对照原目标、正式 required、实际实现与检查，核对重要要求是否被遗漏、替换，或把准备工作当成完成。成功声明必须有对应的运行与结果证据；检查是否能发现足以推翻交付主张的错误，而非只证明文件存在、退出码零或内部一致。验收失败时，检查交付是否准确描述了缺口。方法或参数检查只要求当前任务适用的内容；`execution_params` 是声明，实际使用情况还须由代码、日志或输出支撑。
3. **失败与交接 (`failure_or_handoff`)**：与交付相关的失败、重试、未完成作业和能力缺口是否披露；开放副作用是否仍有负责方，必要的恢复或下一步是否明确。无关的历史诊断错误不自动否定后来证实的结果。
4. **诚实与边界 (`honesty_and_limits`)**：主张不超出证据覆盖和方法适用范围，不把未运行、部分结果或无来源产物写成成功；不隐瞒影响结论的偏离；不把构建、软件核验等操作结果冒充新的科学结论，也不要求节点创造知识 claim。负结果或无显著效应不等于执行失败；经授权的简化模型可以在披露适用范围后有效交付，实现核验不等于现实观测验证。

对新写或实质改动的关键科学实现，或相互矛盾的证据，围绕决定性公式、定义或输入输出做有界独立抽查：先从原任务或可靠依据形成预期，再比对交付方“全部通过”的汇总及原始证据。预期可以是关系、范围或不变量，不必另用模型、软件或重做实验；可信的已有核验可以复用。只在必要时做低成本局部核算，不把抽查推广成所有任务的新门槛，也不把模型补充的检查升级为正式条件。证据不足时限制相应主张，不能仅凭直觉判“科学错误”。

`approve` 取决于主张与证据是否相符，不能靠四项平均分掩盖伪造证据、关键身份冲突或无人负责的开放作业。发现缺口时指出具体主张、证据位置、影响和可执行的修正；能就地解释的轻微问题不要求重跑载荷。Reviewer 只写审查意见，不重跑作业或修改冻结结果。

提交 `compose_review_critique` 时，将四项分数写入 `per_dimension_scores`；`verdict` 使用 `approve | approve_with_revisions | major_concerns | block`，`recommended_action` 按真实问题选择。旧版 `operation_verification_receipt`、八维科学评分、`provisional/inconclusive` 与 methodological sediment 均不是 v3 交付的前提。旧产物中若出现 `analysis_eligible` 或 `run_role`，只视为历史字段；不得据其值给数值证据判资格、决定 reviewer verdict 或阻断下游 Analysis。不得要求 `create_claim` 或“未发现沉淀”的说明，也不得以缺失为 critical 或触发 revise。
