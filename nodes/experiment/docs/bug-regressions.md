# Experiment 历史缺陷回归目录

> 证据盘点日期：2026-08-27
> 范围：`nodes/experiment/**`、直接相关测试、Core/shared 接线，以及这些路径的 Git 历史、逐行归属和修复补丁。
> 证据规则：至少有修复提交及其 patch、测试中的明确回归说明、代码中的历史注释，或当前工作树补丁之一。没有证据的推测不列入。`WORKTREE` 表示当前未提交修改；它不是提交 SHA。
> 本文记录历史故障机制，不声称逐项重新复现。缺陷家族定义见 [bug-families.md](bug-families.md)。

## 产物绑定与上游契约

### EXP-REG-001 — Experiment 在错误节点目录查 prereg

- **症状**：已有冻结 prereg 的 primary run 被解析为 `secondary / analysis_eligible=false`，只能走 inconclusive。
- **已知根因**：Workspace-First 后 prereg 位于生产者节点目录；旧 `load_run_contract` 只 glob Experiment 自己的 `state.artifacts_dir`。
- **代码/提交证据**：`7a8b1fefb2ee1eee6600f6417689724a5a746b21`（提交说明记录 E2E v14/v15/v16）；[run_contract.py](../tools/run_contract.py)；[跨节点读取测试](../../../tests/test_run_contract_cross_node_read.py)。
- **缺陷家族**：`BF-01 产物绑定与上游契约`。
- **被破坏的不变量**：跨节点消费者必须经 canonical artifact API 读取生产者冻结的契约，不能假设产物位于本节点目录。
- **当前是否有回归测试**：有（已提交）：`test_contract_is_read_from_the_analysis_node_directory`、`test_experiments_own_directory_never_holds_the_prereg`、`test_unfrozen_prereg_does_not_grant_primary`，另有 prereg→Experiment contract-chain 测试。
- **当前是否已有结构性保护**：有；使用 `State.list_artifacts/read_artifact` 的跨节点读取路径，并只接受 frozen prereg。
- **仍未覆盖的路径**：已检查测试未见真实平台 dispatcher→Experiment 的完整 E2E；现有证据主要覆盖真实 worktree 存储链与 contract gate。

### EXP-REG-002 — 多份 prereg 时消费方反复猜错当前契约

- **症状**：新 run 被上一轮旧参数阻断；先前“取最新”修复在同 session 多子问题和 artifact 排序变化后仍会选错。
- **已知根因**：最初用 `setdefault` 让枚举中的先见者胜出；`5608b482` 改按 `frozen_at` 选最新仍把 identity 决策留给消费方；真正权威的 `node_inputs` 绑定此前未接通。
- **代码/提交证据**：修复链 `5608b482407ecf6ee0e88f5f1665ba14c97e868e` → `7255ce7b9e6fe798013cf9b1e9f5932a2de4632e` → `2e3fd408ebb11538e3b845844c8a084aacacf491`；随后 `1cfc4be7` 结构化回传歧义，`4e264230` 绑定版本/hash；[测试](../../../tests/test_run_contract_cross_node_read.py)。
- **缺陷家族**：`BF-01 产物绑定与上游契约`。
- **被破坏的不变量**：多候选时，当前 run 的 prereg identity 必须由调度方显式声明；消费方不得按名字、时间或遍历顺序猜。
- **当前是否有回归测试**：有（已提交）：单份免声明、多份歧义、显式声明胜出、声明缺失 fail-loud、submit gate 接线与版本绑定均有测试。
- **当前是否已有结构性保护**：有；`node_inputs.prereg_artifact_id/prereg_version` 是权威输入，多份或无效声明注册持久 blocker，manifest 保存 identity/version/hash。
- **仍未覆盖的路径**：未见同 project 并发 amendment 与 dispatch 的真实竞态 E2E。

### EXP-REG-003 — Sediment 读取 prereg head 而非 run-bound version

- **症状**：run 明确绑定 frozen v1，但 amendment 产生 unfrozen v2 head 后，sediment 读错或拒绝；refreeze 后也可能选择错误 chunk。
- **已知根因**：旧 `_latest_prereg` 调用 `read_artifact(id)` 读取 identity head，没有按 `prereg_version/content_hash` 查 append-only snapshots；KB chunk 只按 origin ID 选择。
- **代码/提交证据**：`73be74b485ec597413cacf73ef3eeefeab2840b5`；[sediment.py](../tools/sediment.py)；[test_sediment_closure.py](../tests/test_sediment_closure.py) 的 `test_sediment_reads_the_contract_bound_snapshot_across_amendment_and_refreeze`。
- **缺陷家族**：`BF-01 产物绑定与上游契约`。
- **被破坏的不变量**：manifest、execution audit 与 sediment 必须消费同一 prereg identity+version+hash snapshot。
- **当前是否有回归测试**：有（已提交），覆盖 amendment、unfrozen head 与 refreeze。
- **当前是否已有结构性保护**：有；使用 artifact version history，并按 version/hash 同时匹配 prereg 与 origin chunk。
- **仍未覆盖的路径**：未见 artifact history 读取异常的专门诊断测试，也未见重复版本或坏 hash chunk 冲突的直接测试。

### EXP-REG-004 — Experiment 漏接 Data 的 canonical `package_dir`

- **症状**：Data 的合法交付被 Experiment 拒绝；浅层校验还可能在 manifest 或请求文件不存在时误认已交付。
- **已知根因**：消费者支持自创的 `package_path/data_path/path`，却漏掉 Data 标准字段 `package_dir`；旧验收偏向字段/文本存在性。
- **代码/提交证据**：`2c038c1ffac3cc21ea3dbbea6c88f77faa64b058`；[contract_audit.py](../tools/contract_audit.py) 的 dataset consumption 校验；[测试](../tests/test_contract_audit.py)。
- **缺陷家族**：`BF-01 产物绑定与上游契约`。
- **被破坏的不变量**：只有识别生产者 canonical schema，并验证 package 目录、manifest 和 required assets 后，输入才能标为 verified。
- **当前是否有回归测试**：有（已提交）：`test_data_service_delivery_contract_accepts_data_package_dir`、`test_data_service_delivery_contract_rejects_missing_package_asset`。
- **当前是否已有结构性保护**：有；支持 `package_dir`，检查目录、manifest JSON、包内相对路径及真实资产后才更新 delivery state。
- **仍未覆盖的路径**：当前 basename `rglob` 路径未覆盖“请求 `a/input.dat`、只有 `b/input.dat`”同名碰撞；`downstream_contract` 的文本 substring 判断也缺结构化碰撞测试。

### EXP-REG-005 — Data 已运行但欠交付时仍可静默派发 Experiment

- **症状**：上游 Data 已失败或没有 dataset，Experiment 仍启动并自行生成输入；历史现场中冻结参数由 `0.5` 漂成 `0.7` 后仍产生看似正常结果。
- **已知根因**：机械输入契约只表达“现有什么 prereg/dataset”，没有表达“Data 阶段已经发生但尚未交付”的债务。
- **代码/提交证据**：`7d295a15`；[run_node.py](../../../shared/tools/run_node.py) 的 `_data_stage_bypassed`；[test_data_stage_debt_gate.py](../../../tests/test_data_stage_debt_gate.py)。
- **缺陷家族**：`BF-01 产物绑定与上游契约`。
- **被破坏的不变量**：Data 已运行但未交 dataset 时，Experiment 派发必须阻断，除非调用方留下显式 waiver reason。
- **当前是否有回归测试**：有（已提交），覆盖 Data ran/no dataset、交付清债、waiver、跨项目隔离和真实 dispatch 接线。
- **当前是否已有结构性保护**：有；从磁盘 run/artifact 事实计算 `data_stage_debt`，要求 `dataset_waiver_reason` 才可继续。
- **仍未覆盖的路径**：提交明确保留“Data 从未运行时不负责阶段规划”的边界；用户是否要求先走 Data 仍依赖 orchestrator 语义判断。

### EXP-REG-006 — Data child 的 blocked report 跨 run 不可读

- **症状**：Data child 已留下 blocker/report，父 Experiment 却读不到，无法诚实关闭 operation 或给出可执行交接。
- **已知根因**：旧路径在当前 Experiment run/目录中找 child 产物，没有以 `data_run_id` 读取 child run 的 artifact。
- **代码/提交证据**：`423253820f9259510faf308cf8c0642a91a0bb9f`（提交标题明确列出 “blocked report 跨 run 读不到”）；[test_blocked_operation_closure.py](../tests/test_blocked_operation_closure.py)。
- **缺陷家族**：`BF-01 产物绑定与上游契约`。
- **被破坏的不变量**：父节点对 child blocker 的判断必须绑定 child run identity，而非父节点当前目录。
- **当前是否有回归测试**：有（已提交）：`test_blocked_report_is_read_from_the_data_child_run`、`test_missing_data_run_id_gets_an_actionable_error`。
- **当前是否已有结构性保护**：有；读取入口要求 child run identity，并在缺失时返回可行动错误。
- **仍未覆盖的路径**：已检查提交和测试未确认额外缺口。

## 证据身份与审计通道

### EXP-REG-007 — 历史 run 与第二份 log 可污染当前 closure

- **症状**：prior-run log 阻止当前 log freeze，或历史 result 被当作当前证据；另可新建第二份 `experiment_log` 替换原 canonical log。
- **已知根因**：project-wide `list_artifacts` 未按 immutable `produced_by_run_id` 过滤；早期也没有“当前 run 恰一份 canonical log”的完整性门。
- **代码/提交证据**：`6bb601027b437171724ea268d4d7269c91570e42`、`2471c9fe8641db7b5b10a87ef813c3c78b1a77a3`、`6237bb550c5fdd90f8f2f954fe02d44441ce9658`；[contract_audit tests](../tests/test_contract_audit.py)。
- **缺陷家族**：`BF-02 证据身份与审计通道`。
- **被破坏的不变量**：本 run closure 只能由本 run 唯一 canonical artifact 满足；项目历史不可顶替当前证据。
- **当前是否有回归测试**：有（已提交）：prior-run log 不阻断、other-run execution record 拒绝、duplicate log 不可 freeze/绑定、operation audit 忽略 prior run。
- **当前是否已有结构性保护**：有；`current_run_artifacts()` 统一过滤，并在 freeze/audit gate 校验 canonical ID 与数量。
- **仍未覆盖的路径**：缺 `produced_by_run_id` 的 legacy artifact 只有 fail-closed、无专门迁移诊断；并发两次 save/freeze 的唯一性无并发测试。

### EXP-REG-008 — `_brief()` 截断让已成功声明在审计中消失

- **症状**：声明工具成功，但较长 reason 被压缩成不可解析 `result_preview`；audit 误判“从未声明”，导致 log 永远不能 freeze。
- **已知根因**：机械审计把有损 UI/tool preview 当成权威成功回执。
- **代码/提交证据**：`6ad514fbfbd8c09abd3b3986b82ef456d1e5d480`；[test_declaration_receipt_audit.py](../tests/test_declaration_receipt_audit.py)，测试注释记录真实 run `1787052091-0f9f9f`。
- **缺陷家族**：`BF-02 证据身份与审计通道`。
- **被破坏的不变量**：审计必须读取完整 durable receipt；显示摘要不能成为事实源。
- **当前是否有回归测试**：有（已提交），覆盖 verdict/sediment 截断、other-log receipt、短 reason 拒绝、不可读工具名诊断与 legacy fallback。
- **当前是否已有结构性保护**：有；声明先写独立 receipt，audit 先读 receipt，仅对 legacy 完整 result 做 fallback；不可读时 fail-closed 并点名。
- **仍未覆盖的路径**：其他仍依赖 preview 的非声明工具需要逐项确认；unreadable note 只提供诊断，不能恢复已丢失事实。

### EXP-REG-009 — Framework auto-recovery log 可自证科学门通过

- **症状**：零工具调用的 failed run，由 recovery 写入 `verdict: inconclusive`/“无 sediment”后，同一 audit 又扫描该文本并判门已满足。
- **已知根因**：producer 与 auditor 共用自由文本，未区分 agent evidence 和 framework recovery pointer。
- **代码/提交证据**：`068ee0719b100061a91bd3a8a0b4c750f76c5bc9`；[test_run_manifest.py](../tests/test_run_manifest.py) 的 `test_auto_recovery_record_does_not_self_certify_quality_gates`；另有 auto-generated log 的 sediment/receipt 负例。
- **缺陷家族**：`BF-02 证据身份与审计通道`。
- **被破坏的不变量**：framework-generated recovery 只能用于恢复、路由和诊断，不可成为 verdict/sediment 科学证据。
- **当前是否有回归测试**：有（已提交），覆盖 recovery self-certification、auto-generated log 不能声明 sediment 或被 receipt 关闭。
- **当前是否已有结构性保护**：有；`metadata.auto_generated` 与 `_is_auto_generated` 在 verdict/sediment/receipt audit 中 fail-closed。
- **仍未覆盖的路径**：未见全 artifact-type 静态检查，证明其他 framework-generated artifact 不会被间接引用满足 result/citation gate。

### EXP-REG-010 — Recovery consumer 猜 `run_manifest` 文件名

- **症状**：recovery 记录中的 return code/logs 恒为 unknown，尽管 producer 已成功创建 manifest。
- **已知根因**：写方名字派生为 `run_manifest__run_manifest.json`，读方却按 run id 拼 `run_manifest__{run_id}.json`；两个模块各维护路径约定。
- **代码/提交证据**：`068ee0719b100061a91bd3a8a0b4c750f76c5bc9`；[run_contract.py](../tools/run_contract.py) 的历史说明；[test_run_manifest.py](../tests/test_run_manifest.py)。
- **缺陷家族**：`BF-02 证据身份与审计通道`。
- **被破坏的不变量**：producer 的结构化返回值是跨模块契约；consumer 不得猜落盘名。
- **当前是否有回归测试**：有（已提交）：`test_recovery_record_captures_real_returncode_and_logs`、`test_recovery_record_marks_manifest_unavailable_on_failure`。
- **当前是否已有结构性保护**：有；consumer 直接使用 `create_run_manifest()` 返回 dict，失败显式标 `manifest_unavailable`。
- **仍未覆盖的路径**：没有静态检查禁止未来 consumer 再次手拼 artifact 文件名。

### EXP-REG-011 — Execution closure 硬依赖脆弱 `chunk_id`

- **症状**：本 run 已有合法 frozen log，但缺 UI/KB chunk id 时 execution record 不能完成；伪造 chunk 又可能绕过预期绑定。
- **已知根因**：closure 用展示/索引层 chunk identity 代替 immutable current-run log binding。
- **代码/提交证据**：`6237bb550c5fdd90f8f2f954fe02d44441ce9658`；[test_contract_audit.py](../tests/test_contract_audit.py) 的 execution-record binding 用例。
- **缺陷家族**：`BF-02 证据身份与审计通道`。
- **被破坏的不变量**：执行记录必须绑定当前 run 的冻结 canonical log；可选索引 chunk 不是完成前置或事实权威。
- **当前是否有回归测试**：有（已提交）：无 chunk 可绑定、fake chunk 拒绝、other-run 拒绝、freeze-before-registration、duplicate current log 拒绝。
- **当前是否已有结构性保护**：有；以 current-run artifact identity/version/hash 对账，chunk 只作辅助索引。
- **仍未覆盖的路径**：已检查提交和测试未确认额外缺口。

## 收尾状态机与终结权威

### EXP-REG-012 — 合理 blocked operation 被强制按 success 闭环

- **症状**：Data/环境确实受阻且已有 blocker，旧 contract 仍要求 success-style verification；任何诚实 blocked closure 都失败。早期还从 Markdown `status:` 猜生命周期。
- **已知根因**：终态模型只描述“做成了”，没有把 registered blocker 作为合法 receipt；输入边界曾静默丢弃 malformed checks。
- **代码/提交证据**：修复链 `423253820f9259510faf308cf8c0642a91a0bb9f`、`0dfde9bcaa218d8726799b869b3774ecf7d46477`、`7a602f7fa85a80ea94a4998e1c068273dfded0aa`、`3b4622fd3509e5a623371752819aa699b936c6c7`；[测试](../tests/test_blocked_operation_closure.py)。
- **缺陷家族**：`BF-03 收尾状态机与终结权威`。
- **被破坏的不变量**：blocked 是合法终态，但必须绑定当前 run 的 registered blocker、失败证据和 next step；不能靠正文措辞自称 blocked。
- **当前是否有回归测试**：有（已提交），覆盖 registered blocker 成功闭环、无 blocker 拒绝、status-style checks 输入拒绝、blocker 作为 receipt。
- **当前是否已有结构性保护**：有；`record_operation_completion` 是唯一 writer，`blocker_id` 必须解析当前 run blocker，check schema 严格校验。
- **仍未覆盖的路径**：未见多个 blocker 的选择/冲突，以及 blocker 被解决后重新执行的状态迁移测试。

### EXP-REG-013 — Operation 的证据链可缺文件或与 receipt 分叉

- **症状**：早期 operation 只有轻量私有 receipt/单 log；后续三件套初版仍可能不收录 executable/json evidence，或出现 `outcome=success` 但 check failed、clean summary 与 receipt 不一致。
- **已知根因**：operation 使用平行 artifact 机制；audit 只看存在/hash，未解析 receipt 并逐字段与 clean 对账；writer 只收部分路径字段。
- **代码/提交证据**：`635963b7f37dc9c501146ef8f625f886896a1f3a`（统一 raw→clean→log）及 `7f8bb95e03a5ded431e97304a6f420afcaf2e694`（证据保全与诚实审计）；[contract audit tests](../tests/test_contract_audit.py)。
- **缺陷家族**：`BF-03 收尾状态机与终结权威`。
- **被破坏的不变量**：所有用于 operation 结论的文件必须进入 immutable raw manifest；clean 必须由同一 receipt 派生并逐字段一致，且 `analysis_eligible=false`。
- **当前是否有回归测试**：有（已提交），覆盖 install operation triplet/tamper、raw binding、analysis eligibility、project promotion、clean/receipt 对账及手写 success+failed check 拒绝。
- **当前是否已有结构性保护**：有；single writer 生成冻结三件套，receipt JSON schema、manifest hash、binding 和 cross-check 都由 audit 强制。
- **仍未覆盖的路径**：未见 reviewer operation lane 的真实端到端消费；symlink target 或路径复用在 downstream 每次消费时是否重验没有直接 E2E 证据。

### EXP-REG-014 — Operation 三件套部分写入后重试会重复或混绑

- **症状**：raw/clean 已写或冻结、log 尚未完成时重调 writer，可能产生第二套 artifact，或用新参数绑定旧 raw。
- **已知根因**：顺序写入没有 closure identity、owner、持久输入 snapshot、幂等和 resume state。
- **代码/提交证据**：`3b4622fd3509e5a623371752819aa699b936c6c7`；[test_blocked_operation_closure.py](../tests/test_blocked_operation_closure.py)。
- **缺陷家族**：`BF-03 收尾状态机与终结权威`。
- **被破坏的不变量**：一个 run 的 operation closure 是单一、幂等、可续写事务；重试参数不得漂移。
- **当前是否有回归测试**：有（已提交）：foreign/manual result 冲突、完成后幂等、partial closure 恢复且不重复。
- **当前是否已有结构性保护**：有；closure id/owner/schema/input metadata，complete 直接返回，partial 从持久状态恢复，foreign artifact fail-loud。
- **仍未覆盖的路径**：当前 fault injection 只覆盖一个中间失败点；raw save/freeze、clean save、log save/freeze、transcript failure 未逐断点参数化。

### EXP-REG-015 — Sediment/closure 前置在 freeze 后才暴露

- **症状**：methodological claim 在 log freeze 后才因 schema/独立来源门被拒；合法“无 finding”路径无法写回，真实 run 白烧一轮甚至续跑主要补冻结仪式。
- **已知根因**：影响 immutable closure 的校验晚于 freeze；工具说明没有在调用前送达 log-first 前置，description 与 validator 又曾各维护一份事实。
- **代码/提交证据**：`5fee6dc70825009405fdc7bc242f6df29ce8dd18`、`b2cee52026fa0649bdf9028400ba488ef3d035cb`、`295c46d488a545330591ba846daa26e8bb047598`；[test_sediment_closure.py](../tests/test_sediment_closure.py) 与 [通用 caller-contract tests](../../../tests/test_tool_contracts_reach_the_caller.py)。
- **缺陷家族**：`BF-03 收尾状态机与终结权威`。
- **被破坏的不变量**：所有可在 freeze 前判定的 closure 前置必须可预检；freeze 后只能追加绑定该 log 的 immutable receipt/addendum；调用前可见契约与 validator 同源。
- **当前是否有回归测试**：有（已提交），覆盖 shared schema、freeze 前/后声明、rejected claim recovery、final preview 与完整 footer；caller-contract 由全仓扫描保护。
- **当前是否已有结构性保护**：有；assess/preview 复用 shared validator/audit，`declare_no_sediment` 有 pre/post-freeze 路径，`ToolDefinition.content_contract` 同源渲染提示和错误。
- **仍未覆盖的路径**：preview 与 freeze 间的状态变化无并发测试；非结构字段的跨工具调用顺序仍主要依赖说明文字。

### EXP-REG-016 — Closure audit 异常可 fail-open 到 Core `completed`

- **症状**：operation audit failed 只改临时 status；scientific audit 抛异常时只写 transcript，且 transcript 自己失败会再次抛。Core 最终状态可能仍是 `completed`。
- **已知根因**：Experiment hook 的“审计结果/异常”与 Core `finalize_run` 所读取的持久 `hook_state.blockers` 没有统一连接；observability 写入先于 completion authority。
- **代码/提交证据**：`WORKTREE-2026-08-26`：[hooks.py](../hooks.py) 新增统一 completion blocker，并在 best-effort transcript 前写入；[test_sediment_closure.py](../tests/test_sediment_closure.py) 新增异常路径测试。历史链中 `7a63bf623b0908133daad63c6c8cdc06a733ea4b` 只覆盖正常 scientific failed checks。
- **缺陷家族**：`BF-03 收尾状态机与终结权威`。
- **被破坏的不变量**：任何 closure audit 未通过或无法执行，都必须留下 Core 可读持久 blocker；日志/事件写入失败不得重新打开完成路径。
- **当前是否有回归测试**：有，但仅在当前 `WORKTREE`：`test_audit_exception_blocks_even_when_transcript_is_unavailable`、`test_audit_exception_blocker_forces_core_final_status`。
- **当前是否已有结构性保护**：当前工作树有；统一 helper 先写 blocker，后 best-effort 记录 transcript，并用真实 Core final-status 测试 scientific exception。
- **仍未覆盖的路径**：operation audit 与 transcript 同时异常到真实 `finalize_run` 没有对应集成测试；在该补丁提交前不能把保护视为历史基线。

## 外部作业生命周期

### EXP-REG-017 — 外部作业缺持久 handoff，submit/terminal 又曾被当完成

- **症状**：节点结束后 scheduler 作业仍在，但没有可恢复 task/identity；模型 submit 后立即 stop 可被写 `completed`；scheduler terminal 时输出尚未分析、log 未冻结却可能关闭 workflow。
- **已知根因**：最初生命周期只在本次调用或 `on_end` 局部状态中；scheduler 状态和 Experiment 分析/finalize 状态混为一层；submit 成功与 workflow persistence 时序分离。
- **代码/提交证据**：修复链 `4d6c63d1b9042ee61efc15bf410de72582936fba`、`5a9438ce`、`d9607c8f38004b349f1d385e9a6c54347c2a6be9`、`d01012048c7dc1edf32d7da0fa618ca27d1f270d`；[test_external_job_handoff.py](../tests/test_external_job_handoff.py)。
- **缺陷家族**：`BF-04 外部作业生命周期`。
- **被破坏的不变量**：real submit 立即留下幂等 workflow identity；scheduler terminal 不等于 Experiment finalized，必须 inspect output、freeze log、显式 finalize。
- **当前是否有回归测试**：有（已提交），覆盖 running/unknown handoff、task 去重、submit 时立即持久、stop→managed wait、terminal→analysis/finalize、finalize 要求 frozen log。
- **当前是否已有结构性保护**：有；`job_submission`/workflow artifact、持久 Task、handoff key、closure/finalize gate 和 output overlap reservation。
- **仍未覆盖的路径**：真实 scheduler crash-after-submit-before-artifact-write 的原子窗口仍存在；Slurm/PBS/Kubernetes 主要是 mock/script 测试，真实生命周期 E2E 只有 local 证据。

### EXP-REG-018 — 健康等待无交接终点，scheduler probe 又不可取消

- **症状**：bounded healthy wait 后模型 stop 会被 gate 再次插入同一 wait；开放 workflow 只有 footer/task、Core 仍可能 completed；阻塞 scheduler query 期间取消不响应。
- **已知根因**：没有“本 run 已完成一次健康受管等待”的 permit；handoff 没有 Core blocker；同步 probe 位于异步取消链之外。
- **代码/提交证据**：`WORKTREE-2026-08-26`：[hooks.py](../hooks.py) 的 healthy-handoff permit/open-job blocker，[resource_manager.py](../tools/resource_manager.py) 的 cancellable probe；[测试](../tests/test_external_job_handoff.py)。
- **缺陷家族**：`BF-04 外部作业生命周期`。
- **被破坏的不变量**：开放 external workflow 必须让 Core 非 completed；完成一次健康受管等待后应允许持久 handoff，不能无限 wait；长 probe 应响应取消。
- **当前是否有回归测试**：有，但仅在当前 `WORKTREE`：`test_healthy_wait_permit_allows_durable_handoff`、`test_external_wait_cancels_while_scheduler_probe_is_still_running`。
- **当前是否已有结构性保护**：当前工作树有；job-bound permit、on-end 持久 blocker、`asyncio.to_thread` probe 与 kill-event polling。
- **仍未覆盖的路径**：healthy-handoff 测试尚未直接调用真实 Core finalizer；on-end/closure 的同步 scheduler 查询存在已确认且已接受的约 10–12 秒取消延迟窗口；多个并发 running jobs 的 permit 策略无专测。

### EXP-REG-019 — 裸后台/调度器命令和 bypass 可绕过受管作业链

- **症状**：`nohup`、`setsid`、`&`、裸 `sbatch/qsub` 或藏在 `submit_job command` 内的后台化可丢失作业；bypass 还可借 `cd`、build 命令或 submit route 偷渡未声明写入。
- **已知根因**：旧 guard 在 bypass 或路由判定前后次序不一致；后台/路径边界不是不可绕过的入口不变量；节点还暴露了平行 `declare_job/job_progress` 状态面。
- **代码/提交证据**：`9b6360e157572529fcba474099bc2929bbc03c0f`、`9ee1be0bd8734acee1c63797739131fcfa0e864f`；`WORKTREE-2026-08-27` 在 `safe_bash.py` 与 `resource_manager.py` 补上实际 payload 路径投影和 hard-scope bypass 约束；[boundary tests](../tests/test_boundary_guard.py)、[timeout tests](../tests/test_timeout_escalation.py)、[resource manager tests](../tests/test_resource_manager.py)、[high-risk tests](../tests/test_unified_highrisk_gate.py)。
- **缺陷家族**：`BF-04 外部作业生命周期`。
- **被破坏的不变量**：长任务只能走 `submit_job → job_status → handoff`；bypass 不得跳过边界、路径角色或受管生命周期。
- **当前是否有回归测试**：有；已提交基线覆盖 unmanaged background、long route、scheduler command heads/wrappers、bare srun 和未声明 absolute write；当前 `WORKTREE` 另覆盖 bypass 对 framework/source hard scope 零执行，以及 local/Slurm/PBS 的 `cd baseline`、`make -C baseline`、框架状态重定向在零 script/零 intent/零 submit 时拒绝。
- **当前是否已有结构性保护**：有；删除平行 job bookkeeping 工具，统一 managed submission；boundary、route validity、hard scope 与 shell 可见目标在 bypass/spawn/script/intent/submit 前判定，外部提交不消费一次性路径授权。
- **仍未覆盖的路径**：复杂 shell 语义仍受 `BF-06` corpus 限制；节点静态门不能证明任意二进制内部写路径，也不能替代真实 Slurm/PBS/Kubernetes 的远端 mount namespace、逐作业 PID 与磁盘配额。

### EXP-REG-020 — 高危批准只告知人，没有把精确重试契约送回模型

- **症状**：模型把“已批准”误解为“作业已执行”，把 `job_name` 当 `job_id`，改变参数后重试并再次 pause，形成恢复循环。
- **已知根因**：pause payload 的人类 UI 和模型恢复上下文不是同一完整契约；没有明确“必须原工具、原参数重试一次，任何变化使批准失效”。
- **代码/提交证据**：`8df666b26d69fea7ba33059d020c087ed7b47ff1`；[test_highrisk_approval_retry_contract.py](../../../tests/test_highrisk_approval_retry_contract.py)。
- **缺陷家族**：`BF-04 外部作业生命周期`。
- **被破坏的不变量**：批准只授权被暂停的完全相同调用一次；批准本身不代表副作用已执行，拒绝时不得重试。
- **当前是否有回归测试**：有（已提交），覆盖 identical retry、参数变化失效、拒绝不重试、模型可见 contract、local job id 形状及 UI option identity。
- **当前是否已有结构性保护**：有；pause payload 携带 model-facing contract，恢复消息显式要求 same tool/same args，one-shot approval 绑定调用签名。
- **仍未覆盖的路径**：已检查提交和测试未确认 Experiment 特有的额外缺口。

## 进程与超时生命周期

### EXP-REG-021 — `cancel_node` 无法抢占阻塞中的 bash/Python 工具

- **症状**：orchestrator 连续取消活跃 Experiment child，kill signal 已写入，但 child 卡在长命令里数分钟，必须等内层 timeout 返回后才退出。
- **已知根因**：agent loop 只在 turn start 检查 `hook_state.kill_signal`；工具正在 `wait_for(proc.communicate(), timeout=...)` 时没有任何代码读取该信号。
- **代码/提交证据**：`1ba50cc27b49de1acc49e20348a8b735c30ca551`，提交说明记录 H3_synth run `1784025561-3eb19f` 两次取消、9 分钟不生效；[safe_bash.py](../tools/safe_bash.py)、[cancellable_subprocess.py](../../../shared/lib/cancellable_subprocess.py)。
- **缺陷家族**：`BF-05 进程与超时生命周期`。
- **被破坏的不变量**：sticky cancellation 必须抢占当前受管子进程等待，而不是只在下一 turn boundary 生效。
- **当前是否有回归测试**：无直接完整回归；现有 subprocess tests 覆盖 timeout/group kill，runtime-control tests 覆盖控制面，但未在活的 `safe_run_bash/safe_execute_python` 内 set `kill_event`。
- **当前是否已有结构性保护**：有；`State.kill_event` 与 `race_communicate()` 竞速，cancel 同时 set event，工具收到后终止并回收进程。
- **仍未覆盖的路径**：真实 bash 与 Python child 的 cancel→kill tree→tool result→node final status 全链路没有直接测试。

### EXP-REG-022 — Timeout 后漏杀子孙或永久挂在 drain

- **症状**：direct shell 已退出但后台 child 仍存活；timeout 时晚取 PGID 得到 `ProcessLookupError` 后不再杀；即使发 kill，后代持有 pipe 仍可让 `communicate()` 长时间不返回。历史 E2E 出现 15.5 小时/5 小时挂起。
- **已知根因**：PGID 到 timeout 时才查询；只杀 direct process；杀后无界等待 pipe EOF；多个层次的异常被静默吞掉。
- **代码/提交证据**：`9a101d8f0652adbb6cfa0021224c49332ce743e2`、Experiment 跟进 `0d49582dbe5ddab1e0245c1810daeb3439766f67`、竞态再修 `583171e55de6f5e8de197664ad23892498ae503d`；[timeout tests](../tests/test_timeout_escalation.py)、[shared subprocess tests](../../../tests/test_subprocess_kill.py)。
- **缺陷家族**：`BF-05 进程与超时生命周期`。
- **被破坏的不变量**：timeout/cancel 收尾必须有界，并终止受管进程组而非只杀 shell。
- **当前是否有回归测试**：有（已提交）：整树 kill、shell 先退出后后台 child、spawn 时缓存 PGID、禁止晚取 PGID、kill 失败仍 bounded drain。
- **当前是否已有结构性保护**：有；`start_new_session`、spawn 时构造 `group_killer`，PGID=PID 的稳定绑定，以及 bounded drain。
- **仍未覆盖的路径**：`safe_run_bash` 正常成功路径仍以 pipe EOF 作为完成信号；未见“命令成功但后代继承 pipe”的直接节点测试。

### EXP-REG-023 — Regression oracle 把 zombie 当活进程

- **症状**：生产 kill 已成功，但容器 PID 1 不 reap，测试用 `os.kill(pid, 0)` 仍返回成功，CI 错报进程树未终止。
- **已知根因**：测试把 OS 中 PID 仍存在等同于业务进程仍 live，没有识别 `/proc` zombie 状态。
- **代码/提交证据**：`eeabbae161e18898559b5ca7dac8a255e5d32f49`，提交明确“只动判据，不动节点逻辑”；[test_timeout_escalation.py](../tests/test_timeout_escalation.py)。
- **缺陷家族**：`BF-05 进程与超时生命周期`（test-oracle 子类，非生产缺陷）。
- **被破坏的不变量**：测试的 liveness 判据必须把 zombie 视为 terminated。
- **当前是否有回归测试**：原 process-tree tests 已改用 `_process_is_live`；这是修正后的 oracle 本身。
- **当前是否已有结构性保护**：有；统一读取进程状态而非裸 `kill(pid, 0)`。
- **仍未覆盖的路径**：没有已知生产路径风险；静态上仍可在其他测试中重新引入裸 `kill(0)` 作为 liveness。

### EXP-REG-024 — 同一慢目标换命令反复超时且丢失进度

- **症状**：同一下载目标用 git/curl/wget 等不同拼写反复同步 timeout，partial output 被丢弃，累计下载大量数据后仍得到 0 字节并被误判 infeasible。
- **已知根因**：timeout 只返回裸 status/cmd；没有跨调用 ledger，或以完整命令而非目标 identity 归并；没有 managed submission/human 出口。
- **代码/提交证据**：`068ee0719b100061a91bd3a8a0b4c750f76c5bc9`；[timeout_escalation.py](../tools/timeout_escalation.py) 的 E2E 历史说明；[测试](../tests/test_timeout_escalation.py)。
- **缺陷家族**：`BF-05 进程与超时生命周期`。
- **被破坏的不变量**：timeout 必须保留 partial progress；同一目标跨命令拼写共享历史；首次失败后不能无限加长同步重试，必须给出受管出口。
- **当前是否有回归测试**：有（已提交），覆盖 host/target signature、partial output、跨拼写熔断、禁止跳到 infeasible、managed route 指引。
- **当前是否已有结构性保护**：有；hook-state ledger、target signature、统一 timeout payload 和首次 timeout 后的同步 hard stop。
- **仍未覆盖的路径**：模块与测试明确聚焦 `safe_run_bash`；`safe_execute_python` timeout 没有同类 ledger。

## 命令语义

### EXP-REG-025 — 文本关键字被误当成 shell 执行语义，且复合命令可漏检

- **症状**：`command -v`/grep/help/version 中出现 scheduler/MPI 名称会被误判提交或执行；`&&` 被末尾 `&` 正则误判后台；相反，包装器/嵌套执行可能绕过；preprocessing 中一个 probe segment 可给另一个 generator segment 整条白名单。
- **已知根因**：多处使用 substring/regex 或整条 command 级布尔值，没有 AST command-position 与 segment 语义。
- **代码/提交证据**：修复链 `9b6360e157572529fcba474099bc2929bbc03c0f`、`c2d280a73e40d4ead4f0ef09e895d2958fc46f76`、`5a2f81c2759b785bb62626349857043840199215`、`52a1430994cc3b450dee1cd480e32c5a67ad5c73`、`6f412a0df91a44194b62825e4593a5453d7df15d`、`423253820f9259510faf308cf8c0642a91a0bb9f`；[bash_semantics.py](../tools/bash_semantics.py)。
- **缺陷家族**：`BF-06 命令语义`。
- **被破坏的不变量**：只有 executable command position 才算执行；复合命令每个 segment 独立判定；实际后台/调度器启动必须被识别。
- **当前是否有回归测试**：有；既有 scheduler probe、命令位置、包装器、裸 `srun`、`&&`、loop/nested/dynamic、probe+generator segment 和 unmanaged launch 用例继续保留；当前工作树的 [test_bash_path_events.py](../tests/test_bash_path_events.py) 进一步覆盖 pipeline、子 shell、函数、字面 `shell -c`/`eval`、重定向入口 cwd、分支 cwd、已知与未知变量混合、透明/委托调度角色及 `lastpipe` 保守失败。
- **当前是否已有结构性保护**：有；timeout/resource/boundary/preprocessing 共用 tree-sitter Bash analyzer。分析器同时输出结构化 cwd/路径事件；路径门与 `_project_bash_route` 消费同一结构化事件模型，不再分别通过文本切分猜测实际入口。dynamic、不可验证路径、analyzer unavailable 均 fail-closed。
- **仍未覆盖的路径**：静态分析不能完备证明运行期展开、trap payload、复杂 shell option、远端解释器语义及任意二进制内部写入；这些分支必须保持未解析/强守卫，并由 OS sandbox、cgroup、timeout 和平台隔离兜底，不能继续堆正则白名单。

### EXP-REG-026 — Bash analyzer 缺失或导入失败时曾 fail-open

- **症状**：运行时没有 parser 依赖时，submission/background 分类返回“未检测到”，允许危险路径继续；错误又可能引导节点自行修改全局环境。
- **已知根因**：parser 被引入但未完整进入运行时依赖；异常 fallback 返回 safe/false，而不是 framework-owned blocker。
- **代码/提交证据**：`3b4622fd3509e5a623371752819aa699b936c6c7` 引入 analyzer 状态；`4a9754fbb0d57ee200195d7f99b7588dba5c6e94`、`3aa1cefa0393e30b3f6fa0d7538c54b76624dc21` 补依赖；`62e7b9858856cb7c6f7ca0f3a4999072ad29d9eb` 统一 fail-loud blocker；[boundary/resource/timeout tests](../tests)。
- **缺陷家族**：`BF-06 命令语义`。
- **被破坏的不变量**：安全 analyzer unavailable/parse failure 必须 fail-closed，并归因 framework；不能把“没分析成”当“安全”。
- **当前是否有回归测试**：有（已提交）：`test_missing_bash_analyzer_is_an_honest_framework_blocker`、`test_submit_job_reports_missing_bash_analyzer_to_framework_owner`、`test_missing_tree_sitter_is_reported_as_analyzer_unavailable`，以及依赖声明测试。
- **当前是否已有结构性保护**：有；执行前 availability check 和统一 machine-readable `bash_semantic_analyzer_unavailable` blocker。
- **仍未覆盖的路径**：resource manager 在 availability precheck 后若 analyzer 发生其他意外异常，历史上存在 broad `except ... false` 模式；未见针对该“已可用后抛异常”路径的 regression。

### EXP-REG-027 — Python scope guard 把 argv/read 当写入

- **症状**：`subprocess` argv 中的 `/usr/bin/python --version` 或 `open('/etc/hosts', 'r')` 被当作系统写入；只读诊断被高危门误拦。
- **已知根因**：旧 `_PY_WRITE_API_RE` 只看源码中 API 名和路径文本，不理解调用、参数位置或 file mode。
- **代码/提交证据**：`62e7b9858856cb7c6f7ca0f3a4999072ad29d9eb`，以及漏导 `ast` 的跟进 `b6679fdba4497c447c0deee73118ddb3ba8467ab`；[scope guard tests](../tests/test_scope_guard_bash.py)、[high-risk summary tests](../tests/test_unified_highrisk_gate.py)。
- **缺陷家族**：`BF-06 命令语义`。
- **被破坏的不变量**：read-only open/argv 不是写；只有 AST 可证明的直接 write API/mode/target 才交给路径门。
- **当前是否有回归测试**：有（已提交），覆盖 subprocess argv、open read mode、literal global write、`Path.open` write mode；高危摘要测试间接执行 AST 路径。
- **当前是否已有结构性保护**：有；Python AST 提取直接写操作和常见进程入口，只用于精确路径门与早期友好报错；实际 Python 统一经 `_exec_and_log` 进入 sandbox、PID/内存/时间/日志 supervisor，资源终态映射为 Python blocker。
- **仍未覆盖的路径**：AST 不会也不应声称完整理解运行时拼接、C 扩展或库内部行为；动态文件目标依赖 sandbox，动态派生进程依赖 cgroup/整树取消。两者都不是由 AST classifier 证明“安全”。

## 路径与工作目录权威

### EXP-REG-028 — Agent artifact 可自授/自锁 path role

- **症状**：agent 写一个 `declared_route` artifact 可把任意目录变 writable；镜像情况下，错误重叠声明又会把自己永久锁进 invalid contract。list/relative 声明还曾静默消失。
- **已知根因**：把 evidence metadata 当 authority；role collector 对输入形态用字符串 normalize，未逐项解析并报告错误。
- **代码/提交证据**：`52a2f56363278fb41ff87191685a0d0b2b94c9e1`；[path_roles.py](../tools/path_roles.py)；[test_path_role_authority.py](../tests/test_path_role_authority.py)、[test_path_roles.py](../tests/test_path_roles.py)。
- **缺陷家族**：`BF-07 路径与工作目录权威`。
- **被破坏的不变量**：evidence 不等于 authority；角色只能由 trusted node inputs、hook/human 或 framework defaults 授予，且无效声明不得静默丢弃。
- **当前是否有回归测试**：有（已提交），覆盖 artifact 自授、forwarded metadata、list form、relative declaration 报告与默认 build/run role 保留。
- **当前是否已有结构性保护**：有；collector 不从 artifact 授权，逐项解析并累积 issues/warnings，默认角色独立保留。
- **仍未覆盖的路径**：list-form 主要是 collector-level 测试，未见每一种 tool 入口的端到端覆盖。

### EXP-REG-029 — 无法解析的破坏性目标被当成“没有写操作”

- **症状**：`rm -rf *`、`rm -rf build`、`find . -delete`、`rsync --delete` 在 baseline cwd 提取不到目标后可静默放行。
- **已知根因**：旧 guard 把“目标未知”归约成空 target list/无风险，而不是 unresolved。
- **代码/提交证据**：`52a2f56363278fb41ff87191685a0d0b2b94c9e1`；[test_path_role_authority.py](../tests/test_path_role_authority.py) 中 regression docstrings 与 destructive-target cases。
- **缺陷家族**：`BF-07 路径与工作目录权威`。
- **被破坏的不变量**：只有能证明目标在已授权 containment 内的 destructive operation 才能执行；未知目标必须 pause/fail-closed。
- **当前是否有回归测试**：有（已提交），覆盖 baseline 内 destructive commands、unresolved target、glob containment、cleanup capability 与非 deletion 风险。
- **当前是否已有结构性保护**：有；无法证明的目标变 `UNRESOLVED`，cleanup 能力按 path role/containment 而非命令 hash 授予。
- **仍未覆盖的路径**：动态 Bash target 的完备性仍取决于 `BF-06` analyzer corpus，不能由路径层单独证明。

### EXP-REG-030 — 输出目录、写边界和 workspace 派生出两棵树

- **症状**：LAMMPS 日志/大轨迹落在 worktree 外，postprocess、checkpoint、publish 和下一 session 不可见；正确 worktree 路径反被 submit 拒绝。无绑定 worktree 时还曾虚构 `<project_root>/workspace`，与 run-local roles 冲突。
- **已知根因**：输出由 `state.project_root`/gitignored cache 派生，边界由 v2.1 worktree 派生；多个模块各拼路径；sandbox fallback 把 project root 错当可推导 workspace。
- **代码/提交证据**：`82b19607e4cbd02ade5723f8841b56d03bd9dba0`、`e7077e1b98c60ea684107d9ffb00f26134f591de`、sandbox 修复 `52a1430994cc3b450dee1cd480e32c5a67ad5c73`；[compute output tests](../../../tests/test_compute_outputs_live_in_node_dir.py)、[node output tests](../../../tests/test_node_output_paths.py)、[path role tests](../tests/test_path_roles.py)。
- **缺陷家族**：`BF-07 路径与工作目录权威`。
- **被破坏的不变量**：允许写的锚点、实际输出锚点和节点 worktree 必须一致；无绑定 worktree 时不得发明冲突目录，非 project run 才使用明确 legacy/run-local fallback。
- **当前是否有回归测试**：有（已提交），覆盖输出落本节点 Git 目录、下游可读、write/output anchor 一致、unbound fallback、禁止 hardcoded run subdir 和 fake workspace。
- **当前是否已有结构性保护**：有；Experiment 输出 helper 转发统一 `core.paths.node_output_dir`，workspace resolver 只接受真实绑定。
- **仍未覆盖的路径**：fake-workspace 回归主要在 helper 层，没有直接 `safe_write_file` sandbox E2E；有意保留的 unbound legacy fallback 已有测试，不算缺陷。

### EXP-REG-031 — Inline `cd` 失败被尾部成功掩盖，cwd typo 仍可产生副作用

- **症状**：`cd missing; build; ls` 在继承 cwd 继续执行，尾部 `ls/true` 返回 0，整条命令误报 success；Python executor 还会静默创建拼错的 cwd。
- **已知根因**：命令文本中的 `cd` 和 executor 的真实 cwd 是两套 authority；shell list 只用最后退出码；入口没有事前校验且会自动 mkdir。
- **代码/提交证据**：`cfade4fea6713059eeb38eb78f3f54f5b07f849f`；[test_required_workdir.py](../tests/test_required_workdir.py) 的 CUDA probe 事故说明和 regression cases。
- **缺陷家族**：`BF-07 路径与工作目录权威`。
- **被破坏的不变量**：required cwd 无效时必须零 spawn、零副作用且非 success；实际 workdir 由 `cwd` 参数单一声明并进入日志。
- **当前是否有回归测试**：有（已提交），覆盖 failed cd 不执行后续、尾部 success 不掩盖、missing cwd 零 spawn、显式 cwd 权威、bash/Python 一致和日志 header。
- **当前是否已有结构性保护**：有；统一 `resolve_required_workdir` 事前校验，cwd 参数权威，leading `cd` hardening 以 exit 73 fail-fast。
- **仍未覆盖的路径**：结构化 Bash 事件已直接覆盖函数体、子 shell 和字面 `shell -c` 的 cwd 传播；leading-cd fail-fast 仍是独立的受限兼容加固。运行期生成脚本、复杂 shell option 和任意二进制内部 cwd 变化仍无法由静态层完备证明，显式 fallback/heredoc 继续是有测试的受控例外。

## 调度器拓扑与可移植性

### EXP-REG-032 — Scheduler 日志、local/Kubernetes 路径和 stage-in 契约分叉

- **症状**：宣告 `<job>-<id>.out` 而脚本写 `<id>.out`，健康检查永远读不到；bootstrap 在 redirect 前失败无日志；local 可 mkdir/运行越界 workdir；Kubernetes 在 Pod 内创建同名 host path 却没有输入；stage-in 批准后同路径文件可替换且 executable bit 丢失。
- **已知根因**：脚本/result 各维护日志模板；submit host、compute node、Pod 共用一套路径假设；approval 只绑定 source path，不绑定内容和 mode。
- **代码/提交证据**：`1954c41c82ca1b8543390867c860a0f6956f20ed`；[resource_manager.py](../tools/resource_manager.py) 的历史注释；[test_path_boundary_regressions.py](../tests/test_path_boundary_regressions.py)。
- **缺陷家族**：`BF-08 调度器拓扑与可移植性`。
- **被破坏的不变量**：宣告日志=实际写入；bootstrap failure 可见但不算 progress；每种 scheduler 使用自己的路径 topology；stage-in approval 绑定 digest+mode 且 destination 不逃逸。
- **当前是否有回归测试**：有；既有 Slurm/PBS 名称 helper、Slurm 脚本重定向、bootstrap、local 边界、Kubernetes host/volume、stage-in digest/mode/approval invalidation/escape 用例继续保留；当前工作树另覆盖无 volume contract 时在 route resolver、目录、脚本、intent 和 submit 前早停，以及自动推荐绝不选择 Kubernetes。
- **当前是否已有结构性保护**：有；共享日志 basename renderer、预建 bootstrap 目录、按调度器划分的路径边界和已校验的 stage-in payload。Kubernetes 当前明确报告“可探测但提交契约不可用”：通过通用静态有效性检查的显式请求（含 dry-run）返回 `kubernetes_volume_contract_required`，`recommended_default` 只按 SLURM → PBS → local 选择。
- **仍未覆盖的路径**：无真实 Slurm/PBS/Kubernetes 执行 E2E；当前 Kubernetes 保护是安全停用而不是 volume 支持。恢复 Kubernetes 必须先由框架/平台提供 PVC/volume 身份、容器挂载点、读写方向与输入输出 lineage 契约，再验证真实 Pod；PBS redirect 仍只有 helper-level，stage-in 批准到真实 copy/submit 间的 TOCTOU mutation 仍未模拟。

### EXP-REG-033 — 无 `getent` 被判身份坏

- **症状**：macOS/精简容器缺 `getent` 时，scheduler payload 一次未执行便 exit 86；“探测不了”被报告成“用户不存在”。
- **已知根因**：identity preflight 没有区分 unavailable/unknown 与 authoritative invalid，把 Linux/NSS 工具缺失映射为否定结果。
- **代码/提交证据**：`a6d251366e17f8a3b506d4c65c6af0f601df218a`、兼容收束 `3dda9d5705322f98cce179f796134e0bc97ace39`；[test_identity_preflight_portability.py](../tests/test_identity_preflight_portability.py)。
- **缺陷家族**：`BF-08 调度器拓扑与可移植性`。
- **被破坏的不变量**：unknown 与 invalid 必须分离；只有权威探测明确否定才因 identity 阻断，workdir 安全仍独立 fail-closed。
- **当前是否有回归测试**：有（已提交），覆盖无 getent、目录服务明确拒绝、UID mismatch、无 getent 但 workdir 不可写。
- **当前是否已有结构性保护**：有；getent/dscl/none 三来源状态，identity unknown 不误杀，明确否定与 workdir 失败仍阻断。
- **仍未覆盖的路径**：没有真实远端目录服务或 scheduler 环境集成测试。

### EXP-REG-034 — Script preview 截掉真正 payload

- **症状**：identity/bootstrap preamble 增长后，固定展示脚本前 2000 字符，真正 command 位于尾部且完全不可见，审批者无法核对执行内容。
- **已知根因**：使用 `script[:2000]` 的固定前缀截断，把“开头”错误当成 preview 的全部重要内容。
- **代码/提交证据**：`3dda9d5705322f98cce179f796134e0bc97ace39`；[test_identity_preflight_portability.py](../tests/test_identity_preflight_portability.py)。
- **缺陷家族**：`BF-08 调度器拓扑与可移植性`。
- **被破坏的不变量**：任何审批/dry-run preview 必须保留真实 payload，并明确标记被省略部分。
- **当前是否有回归测试**：有（已提交），覆盖 preamble 超预算、payload 本身超预算、短脚本完整返回。
- **当前是否已有结构性保护**：有；中间截断，保留首尾并显示 elided marker。
- **仍未覆盖的路径**：现有测试验证 renderer，不是实际 UI 在所有客户端上的视觉展示。

## 调用方契约漂移

### EXP-REG-035 — `safe_execute_python` 声明了但 Experiment 实际拿不到 14 天

- **症状**：Harness/prompt 列出工具，实际 tool list 没有；因 Bash fallback 可用，现场表现为静默降级，Python 专属 execution contract 长期未生效。
- **已知根因**：`dataclasses.replace` 从原 `execute_python` 继承 `allowed_node_types=["postprocess"]`；tool registry 对 Harness whitelist 与 allowed types 静默取交集。
- **代码/提交证据**：`7366e67a52abd3175d5cbeb724e809b955a8cbd7`；[safe_bash.py](../tools/safe_bash.py) 的显式 node grant；[test_declared_tools_are_granted.py](../../../tests/test_declared_tools_are_granted.py)。
- **缺陷家族**：`BF-09 调用方契约漂移`。
- **被破坏的不变量**：Harness 声明的每个工具必须真实授予该节点，否则加载时 fail-loud。
- **当前是否有回归测试**：有（已提交）：全仓 `test_every_declared_tool_is_reachable_by_its_node`，并有变异证明 gate 会触发。
- **当前是否已有结构性保护**：有；loader 启动时执行 declared-vs-granted 对账并拒绝加载。
- **仍未覆盖的路径**：已检查提交和测试未确认额外缺口。

### EXP-REG-036 — 工具说明、validator、prompt/skill 各维护一份接口

- **症状**：模型按 description 调用仍被 validator 拒绝；节点/skill 还曾点名已移除工具或错误参数，尤其在恢复路径反复失败。
- **已知根因**：手写 description、error 文案、prompt/skill 示例和 live schema 彼此独立；早期测试只维护工具名名单，不能覆盖新增接口或参数。
- **代码/提交证据**：`b1b12c0e35eefc2510567e0393993f0bc0d355b7`（清理幽灵工具名）、`295c46d488a545330591ba846daa26e8bb047598`（`content_contract` 单源）；[test_tool_contracts_reach_the_caller.py](../../../tests/test_tool_contracts_reach_the_caller.py)。
- **缺陷家族**：`BF-09 调用方契约漂移`。
- **被破坏的不变量**：调用方在调用前必须看到实现会拒绝的字段/前置；description 与 validator 必须从同一声明派生，文档不得点名不存在的接口。
- **当前是否有回归测试**：有（已提交），全仓扫描 contract 渲染、validator wording、被拒字段可见性与迁移调用字段；工具名可达性另有 loader test。
- **当前是否已有结构性保护**：有；`ToolDefinition.content_contract` 同供 model-facing description 与 `contract_requirement()`，并由通用扫描覆盖。
- **仍未覆盖的路径**：扫描依赖可识别源码/文案形态；动态 validator、非 dict 字段及跨工具调用顺序可能逃逸；通用测试尚未系统对账 executor 内所有 `kw.get`。

### EXP-REG-037 — `safe_execute_python` 实现读取参数但 schema 不暴露

- **症状**：Python simulation 会消费 `input_package_artifact_id`，但模型可见 schema 没有该字段；调用方无法合法传入，输入交付 preflight 收到 missing。
- **已知根因**：`2fbd88fd` 接线时只修改 `safe_run_bash` schema；`safe_execute_python` 的正常派生 schema和 fallback 注册 schema 都漏改。
- **代码/提交证据**：已提交代码中 executor 的 `kw.get("input_package_artifact_id")` 与 schema 缺口；`WORKTREE-2026-08-26` 在两个注册分支补字段；[test_harness_contract.py](../tests/test_harness_contract.py)。
- **缺陷家族**：`BF-09 调用方契约漂移`。
- **被破坏的不变量**：实现读取的 public 参数必须在所有注册分支 schema 中可见。
- **当前是否有回归测试**：有，但仅在当前 `WORKTREE`：`test_safe_execute_python_exposes_input_package_artifact_id`。
- **当前是否已有结构性保护**：当前工作树只有 live registry 的定点断言；尚无通用 `kw.get/signature ↔ schema` 静态对账。
- **仍未覆盖的路径**：fallback 注册分支虽已修改但未被直接执行测试；补丁提交前不能视为历史基线保护。

### EXP-REG-038 — 节点文档仍描述已删除层和旧裁决权

- **症状**：Experiment 文档仍要求旧 `quality_checks`、默认 curator 链和 per-experiment 最终 verdict，与当前“raw evidence 交给 reviewer/Analysis 最终裁决”的架构冲突。
- **已知根因**：架构删除/owner 迁移时，节点 README、review spec、Harness 输出声明各自维护，缺少语义级同步检查。
- **代码/提交证据**：`WORKTREE-2026-08-26`：[README.md](../README.md)、[review_spec.md](../review_spec.md)、[harness.yaml](../harness.yaml) 的同步修改；[test_harness_contract.py](../tests/test_harness_contract.py) 新增 legacy-QC 与 adjudication ownership 断言；当前 [ROADMAP.md](../ROADMAP.md) 的 N-004 记录漂移。
- **缺陷家族**：`BF-09 调用方契约漂移`。
- **被破坏的不变量**：运行契约、输出 owner、review lane 和节点文档必须描述同一现行架构。
- **当前是否有回归测试**：有，但仅在当前 `WORKTREE`：`test_experiment_declares_main_closure_outputs_without_legacy_qc`、`test_reviewer_spec_keeps_final_scientific_adjudication_in_analysis` 等。
- **当前是否已有结构性保护**：部分；关键 Harness/reviewer 文案有定点断言，但没有全节点语义 schema。
- **仍未覆盖的路径**：测试只锁关键短语/声明，不能证明 README/review spec 全文没有其他旧层、旧 owner 或旧流程残留。

## 进程全局状态与测试隔离

### EXP-REG-039 — 并行 `safe_execute_python` 串用彼此环境

- **症状**：并行 Experiment run 的 Python child 可能继承另一个 run 的 `EXPERIMENT_RUN_ROOT/PYTHONPATH`，从错误 checkout 或输出根执行。
- **已知根因**：wrapper 在 process-global `os.environ` 上 snapshot/set，随后跨 `await` 启动/等待 child；并发调用可在 restore 前交错。
- **代码/提交证据**：`4b48d4b14269e60ca6c57b72b02bb02611b8b323`；[safe_bash.py](../tools/safe_bash.py)；[test_safe_execute_python_repo_path.py](../tests/test_safe_execute_python_repo_path.py)。
- **缺陷家族**：`BF-10 进程全局状态与测试隔离`。
- **被破坏的不变量**：每个 Python child 只继承本 run 环境；snapshot/set/spawn/restore 必须原子且 finally 恢复。
- **当前是否有回归测试**：有（已提交）：`test_parallel_safe_python_calls_keep_run_local_environment`，另覆盖 repo root/PYTHONPATH checkout 解析。
- **当前是否已有结构性保护**：有；module-level async lock 包住完整临时环境生命周期，finally 恢复，repo root 优先进入 PYTHONPATH。
- **仍未覆盖的路径**：并发测试 monkeypatch executor 并观察 `os.environ`，没有同时启动两个真实 Python subprocess 的 integration。

### EXP-REG-040 — Experiment `conftest` 永久 monkeypatch 全局 tool registry

- **症状**：测试结果依赖 collection/import 顺序；Experiment 测试导入后可改变其他测试对 duplicate registration/allowed tools 的行为。
- **已知根因**：`conftest.py` 在模块 import 时替换 `core.tool_registry.register_tool`，且从不恢复。
- **代码/提交证据**：`2ec5333c289b35d966fdeede4e05f8d843891ab6`；当前 [conftest.py](../tests/conftest.py) 留有历史注释。
- **缺陷家族**：`BF-10 进程全局状态与测试隔离`。
- **被破坏的不变量**：测试 fixture 不得在 import 时永久修改全局 registry；测试顺序不能改变被测系统契约。
- **当前是否有回归测试**：无专门的 Experiment 节点级“禁止 module-level registry patch”测试；通用 registry tests 只侧面覆盖真实行为。
- **当前是否已有结构性保护**：生产代码未依赖该 patch，历史 conftest 全局替换已删除；当前只保留局部、可恢复 fixture 模式。
- **仍未覆盖的路径**：未来 conftest 再引入 import-time registry/global mutation 时，没有定点静态 guard。

## 资源可行性顺序

### EXP-REG-041 — 编译后才发现 MPI/GPU/资源计划不可行

- **症状**：configure/compile 已锁定 MPI/GPU/ABI/backend，之后才发现 Core 能力或实时资源不支持，造成昂贵构建作废或错误运行模式。
- **已知根因**：resource discovery/preflight 是可选工具调用或 prompt 约定，没有证明所有 build 入口在副作用前机械经过它。
- **代码/提交证据**：`2bbe21be3c59e9c452845c8a7ed357487bdcf730`；[resource_manager.py](../tools/resource_manager.py)；[test_resource_manager.py](../tests/test_resource_manager.py)。当前工作树另有未提交 `build_resource_guard.py` 接线，不能算已提交历史保护。
- **缺陷家族**：`BF-11 资源可行性顺序`。
- **被破坏的不变量**：会锁定资源/ABI 的构建前必须形成 Core declaration + live snapshot 的可行计划；fixed 不满足时 build 前阻断，策略变化交还上游/用户。
- **当前是否有回归测试**：部分（已提交）：`test_build_resource_preflight_blocks_before_compile_or_pauses_for_user_choice`、`test_build_resource_preflight_rejects_mpi_mode_without_capability` 直接测 preflight；当前工作树有额外 guard tests，但未提交。
- **当前是否已有结构性保护**：已提交版本有 preflight 与持久 plan/blocker/pause；“每次 configure/compile 强制先过 guard”的结构接线仅见当前未提交工作树增强。
- **仍未覆盖的路径**：已提交历史没有证明所有 build 命令入口都强制经过 preflight；真实资源变化、queue 状态和 remote scheduler 能力没有集成测试。


## 当前工作树执行边界

### EXP-REG-042 — bypass 把路径有效性事实误当成人工授权

- **症状**：`--bypass-permissions` 可让 `safe_write_file`、Bash 或 Python 越过源码基线、框架状态或未解析目标；审计事件存在，但副作用已经发生。
- **已知根因**：旧分支以“是否 bypass”整体跳过 scope 结果，没有按 scope 区分可人工确认的环境边界与不可改写的路线/路径有效性事实。
- **代码/提交证据**：`WORKTREE-2026-08-27`：[safe_bash.py](../tools/safe_bash.py) 的 `_scope_bypass_allowed` 及三个入口接线；[test_unified_highrisk_gate.py](../tests/test_unified_highrisk_gate.py)。
- **缺陷家族**：`BF-07 路径与工作目录权威`。
- **被破坏的不变量**：bypass 只能跳过一次精确环境授权；源码基线、框架状态、角色冲突、container-only 和未解析路径始终硬拒。
- **当前是否有回归测试**：有，但仅在当前 `WORKTREE`：`test_bypass_only_overrides_soft_environment_scope`、`test_bypass_cannot_write_framework_state_or_source_baseline`、`test_bypass_cannot_cross_baseline_via_bash_or_python`；断言 writer/executor 零调用且目标不存在。
- **当前是否已有结构性保护**：有；所有 bypass 分支消费同一精确 scope，只有 `_SCOPE_APPROVAL_SCOPES` 且不在 hard-scope 集合中的项可跳过。
- **仍未覆盖的路径**：未来新增 scope 若未进入分类测试，仍可能被错误归类；默认行为是非白名单 scope 不可 bypass。

### EXP-REG-043 — shell payload 路径与路线入口由分散解析器推导

- **症状**：`safe_run_bash` 或 `submit_job` 的 `workdir`/`output_dir` 合法，payload 却可通过 `cd baseline`、`make -C baseline`、重定向或裸相对文件命令写入源码/框架路径；反向场景中，静态 `pipeline`、子 shell、函数或字面 `shell -c` 的真实入口已可证明，却被另一套字符串解析误判成 unknown/compound，造成错误路线阻断。
- **已知根因**：调用参数曾替代实际 payload 路径事实；路径写目标、只读分类和路线入口又各自由局部正则、`shlex` 或字符串递归推导，同一 shell 程序存在多个互相漂移的语义真相源。
- **代码/提交证据**：`WORKTREE-2026-08-27`：[bash_semantics.py](../tools/bash_semantics.py) 的 `CwdDomain`/`StaticPathEvent` 与 `dispatch_role`；[safe_bash.py](../tools/safe_bash.py) 的 `_analyze_shell_path_effects`、`_bash_path_effects_guard` 和 `_project_bash_route`；[resource_manager.py](../tools/resource_manager.py) 的提交前接线。
- **缺陷家族**：`BF-06 命令语义`、`BF-07 路径与工作目录权威`、`BF-08 调度器拓扑`。
- **被破坏的不变量**：调用参数不能替代真实 payload 路径；路径与路线入口必须基于同一结构化 AST 事件模型，且路线入口只能由一个投影函数产生。静态 `pipeline`、子 shell、函数、字面 `shell -c`/`eval` 可展开但必须保留各自 cwd；透明包装、委托命令、多主 payload、动态参数或未知 cwd 不得被错误折叠成可信单入口。
- **当前是否有回归测试**：有，但仅在当前 `WORKTREE`：[test_bash_path_events.py](../tests/test_bash_path_events.py) 覆盖 cwd/重定向/调度角色；[test_route_shadow_wiring.py](../tests/test_route_shadow_wiring.py) 覆盖静态包装展开、可信 `tee` sidecar、多主 payload 和不可信透明 shell；[test_path_boundary_regressions.py](../tests/test_path_boundary_regressions.py)、[test_path_role_authority.py](../tests/test_path_role_authority.py) 与 [test_resource_manager.py](../tests/test_resource_manager.py) 覆盖 safe-run/submission 的零副作用路径阻断。
- **当前是否已有结构性保护**：有；tree-sitter 分析器输出带 cwd、上下文和 `direct/transparent/delegated` 角色的事件。路径投影与路线投影共同消费它；safe-run 在 spawn 前、submission 在 script/intent/submit 前调用同一路径分类器；不确定性 fail-closed，可信 pipeline sidecar 只在解析到受信系统程序时才能折叠。
- **仍未覆盖的路径**：运行期变量展开、trap、复杂 shell option、远端解释器语义和任意二进制内部写路径无法由静态分析完备证明；远端 mount namespace、逐作业 PID 与磁盘配额仍由平台负责。`_is_major_build` 继续作为强制兜底，不能在软件矩阵证明替代机制前降级。

### EXP-REG-044 — Python AST 被误当进程与文件系统完整性边界

- **症状**：直接 `subprocess.Popen` 可早期拒绝，但别名、动态 `getattr`、运行时拼接、C 扩展或库内部仍能派生进程；动态路径写入也可能避开字面 AST 目标。旧 `safe_execute_python` 既没有统一任务级 PID/内存上限，又继承公共 `/tmp`、用户缓存或框架状态的宽写面；数值库隐式线程池还可能在 64 PID 预算内耗尽任务自身。
- **已知根因**：把有限 AST classifier 同时当 UX 检查和安全证明；Python 没有复用 Experiment 已有 cgroup supervisor，也没有逐调用的最小文件系统能力与线程预算。
- **代码/提交证据**：`WORKTREE-2026-08-27`：[safe_bash.py](../tools/safe_bash.py) 的 `_python_process_launches`、`_exec_and_log` 接线、`_python_resource_envelope`、run-local scratch 与 `_SAFE_PYTHON_THREAD_ENV`；[subprocess_policy.py](../tools/subprocess_policy.py) 的 `python_sandbox_roots`/`PythonSandboxContractError`；[test_build_resource_guard.py](../tests/test_build_resource_guard.py) 与 [test_path_boundary_regressions.py](../tests/test_path_boundary_regressions.py)。
- **缺陷家族**：`BF-05 进程与超时生命周期`、`BF-06 命令语义`、`BF-07 路径与工作目录权威`、`BF-11 资源可行性顺序`。
- **被破坏的不变量**：静态识别只能提供友好诊断；任何实际 Python 都必须受 PID、内存、时间、日志和整树取消边界。根文件系统和框架/项目/源码/依赖默认只读，只能按本次路径角色恢复精确写面；临时文件和缓存属于当前 run；全局 bypass 不得关闭完整性沙箱。
- **当前是否有回归测试**：有，但仅在当前 `WORKTREE`：别名/getattr/exec/pty corpus、所有 safe Python 必经 supervisor、Python 终态标签映射、路径角色重叠零启动、缺失 protected role 的最近祖先只读覆盖、框架状态/源码 worktree 的精确覆盖、公共 `/tmp` 与用户 cache 不进入 writable roots、run-local scratch 及全部线程变量为 4。另有真实动态拼接 Popen 微测命中 PID cgroup；这不能替代 NumPy/Matplotlib 与动态文件写的最终生产微测。
- **当前是否已有结构性保护**：有；轻量 Python 默认申请 4 GiB，经弹性余量约 5 GiB、64 PID，数值库线程预算为 4，并复用现有 supervisor。沙箱从根文件系统只读开始，仅恢复 `run_root`、当前执行角色或已通过路径门的目标；`TMPDIR/TMP/TEMP`、`XDG_CACHE_HOME`、`MPLCONFIGDIR` 和 `PYTHONPYCACHEPREFIX` 全部落到 run-local `.python-scratch`。正式科学、显式进程树和重依赖负载仍转交 `safe_run_bash` 或 `submit_job`。
- **仍未覆盖的路径**：AST 不完备是设计边界而非待补黑名单；真实 GPU/MPI/大内存 Python、cgroup OOM/PID 硬事件、Swap 压力事件与 kill-event 竞态仍需专项 E2E。OS 沙箱只能约束文件系统视图，不能证明任意 native extension 的网络、设备或内核行为；这些能力仍需独立平台契约。

### EXP-REG-045 — envelope 的 `storage_binding.roles` 只报"不支持"，不报合法键

- **症状**：`declare_execution_envelope` 拒绝 `roles` 时只说"含不支持的语义角色：['run_root']"。键是语义角色（`input/build/run/output/logs`）、值才是 canonical path role，两套命名都叫 "role"，而 `parameters_schema` 里 `roles` 是不透明的 `{"type": "object"}`，skills / rules / harness.yaml 全文不含 `storage_binding`——拒绝消息是调用方唯一的学习通道。
- **已知根因**：`_normalise_storage_binding` 报出违规项却不报 `_ROLE_NAMES` / `CANONICAL_ROLES`；schema 未把封闭词表暴露为 `enum`。
- **代码/提交证据**：`e35841e1`：[execution_envelope.py](../tools/execution_envelope.py) 的 `_ROLE_NAMES_HINT` / `_CANONICAL_ROLES_HINT`、键值写反诊断与 `propertyNames`/`additionalProperties` enum；[test_execution_envelope.py](../tests/test_execution_envelope.py) 四条断言。
- **缺陷家族**：`BF-12 拒绝不可执行`。
- **被破坏的不变量**：面向模型的封闭词表必须经 schema `enum` 或拒绝消息把合法取值送到调用方。
- **当前是否有回归测试**：有。已反向验证：还原旧消息即两条转红。
- **当前是否已有结构性保护**：有；`test_harness_contract.py::test_model_facing_vocabularies_tell_the_caller_the_legal_values` 机械核对送达通道。
- **仍未覆盖的路径**：登记表人工维护，新增词表漏登记则不受保护。
- **真实代价**：2026-09-02 `e2e_realistic_scientific` 实跑 22 轮（turn 24→46），最终靠模型自行猜中；同一次调用里 `content_digest` 因报错写明 `sha256:<64hex>` 而 1 轮改对。

### EXP-REG-046 — `raw_results` 冻结算出实际 sha256 却只报 mismatch

- **症状**：`_validate_raw_results_manifest` 当场 `digest.hexdigest()`，比完即弃，只报 `sha256 mismatch for <path>`；同一循环里 `bytes` 检查一直是 `declared X, actual Y`。调用方对自己 run_root 内产物本就有读权限，隐瞒实际摘要不构成防线。
- **已知根因**：把"不告诉正确值"误当防作弊边界；实际上文件必须真实存在且可读才走到该分支，报出实际值不会凭空造出证据。
- **代码/提交证据**：`b8813ace`：[contract_audit.py](../tools/contract_audit.py) 的 `actual_sha256` 与 `declared X, actual Y` 措辞；[test_contract_audit.py](../tests/test_contract_audit.py) 中实际摘要与声明摘要双断言。
- **缺陷家族**：`BF-12 拒绝不可执行`。
- **被破坏的不变量**：框架在判定中已经算出的正确值必须随拒绝一起报出。
- **当前是否有回归测试**：有。已反向验证：还原旧消息即红。
- **当前是否已有结构性保护**：部分；"已算出的值是否报出"无法静态判定，只能逐门行为测试。
- **仍未覆盖的路径**：其它同类门（manifest lineage、路线摘要）未逐一排查。
- **真实代价**：2026-09-02 实跑 20+ 轮；节点为取这一个值往科学路线 DAG 里插了 `hash_outputs` 步骤，收尾工序污染了科学语义并留痕在 `declared_route` 里。该污染在 artifact 中不可逆。

### EXP-REG-047 — `program_sequence` 失配不分成因，对非复合命令给出误导提示

- **症状**：节点把 `program_sequence` 当成单个程序的 argv 填写，声明期全部合法（多项、无空白、`program` 与首项一致），路线正常冻结——路线里没有命令文本，声明期确实查不出。提交时失配，而提示说"不要增删、重排入口，也不要改用脚本、管道或条件分支"；调用方并未重排任何东西，它的命令只有一个入口。
- **已知根因**：`_binding_mismatch_detail` 对 `compound_sequence_mismatch` 只有一句提示，未区分"observed 有 sequence 但对不上"与"observed 压根没有 sequence（命令不复合）"两种成因。
- **代码/提交证据**：`46d17731`：[execution_route.py](../tools/execution_route.py) 的双支 hint 与 schema 描述反例；[test_execution_route.py](../tests/test_execution_route.py) 中两支提示必须不同、"重排"不得出现在非复合支的断言。
- **缺陷家族**：`BF-12 拒绝不可执行`、`BF-09 调用方契约漂移`。
- **被破坏的不变量**：同一 kind 的失配若有多种成因，提示必须按成因分流；宁可不给提示也不能给错误成因的提示。
- **当前是否有回归测试**：有。已反向验证：两支发同一句话即红。
- **当前是否已有结构性保护**：无静态保护；成因正确性只能靠行为测试。
- **仍未覆盖的路径**：其它 `mismatch_kind`（`tool_mismatch`、`compound_action` 等）是否也存在多成因合用一句提示，尚未逐一排查。
- **真实代价**：2026-09-02 实跑 2 轮（侥幸——节点得出"submit_job 不接受 program_sequence 参数"这个**错误**结论，却误打误撞改对了路线）。成因判断错误没有留下痕迹，下次未必同样走运。
