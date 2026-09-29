# F9 修复规格（A-revised，四路对抗评审 + 合议定稿 2026-09-02）

> 状态：已落地（2026-09-03 随 a65482cb 合并上游后审计定稿）。落点：oc/hooks/
> registry 在 02639547；**§6a/§6b 的 rm 文案在 9b534fe6（walltime commit 误夹带）**。
> 审计后偏离与补修记录见 TEST_RECORD_HPC_AI.md「F9 合并与审计补记」；已知欠账：
> §1b _verify_selected_records 未抽出（未决问题 3 的双源角落，主流不受影响）。
> 原方案 A 的五处证伪与 B/C/D/E 落选理由见 TEST_RECORD_HPC_AI.md F9 节补记。

## 裁决

A 修订版：保留 A①但枚举改为"本 run 账本（信封 run_id 过滤+lifecycle 过滤）→ 空则单例收养 unresolved workflow"两级、闭包侧候选域与归一化对齐 finalize 侧；A② 的恒拒收窄为仅 outcome=success 的两堵 B 墙，fail-closed 判为死路墙改成不吞异常枚举+witness；存量死锁经 continuation-run 收养解决，不加 finalize 回退与 amend 通道；rm/hooks 仅改文案。置信度高——四份裁决的全部关键分歧点均已在 HEAD beace2e6 亲验到行。

## 完整规格

全部改动在 nodes/experiment/ 内。逻辑改动只在 tools/operation_completion.py（含向 resource_manager.py 加一个可选参数）；resource_manager.py 与 hooks.py 另有纯文案改动（理由见 §6）。

§1 新枚举原语（operation_completion.py 内新增两个函数）
1a. `_own_run_managed_submissions(state) -> (records, excluded, errors)`：对 rm._SUBMISSION_RECORD_TYPES（"job_submission" + durable recovery 类型，rm:70）逐类型 `state.list_artifacts(type, own_only=True)`；对每个 id 用 `state.read_artifact` 读完整信封，要求 `record["produced_by_run_id"] == state.run_id`（先例：rm:5461 严格校验同字段；非 v2 run-local 目录下该过滤恒真）；payload 过滤与 rm:593-596 完全一致（status=="success"、非 dry_run、有 scheduler+job_id）；再按 `lifecycle_for_submission(state, payload)` 分流——status 非 None 且 ∉ _ACTIVE_JOB_STATES（rm:124：submitted/running/unknown）的进 excluded（附 lifecycle_status，覆盖 cancelled/superseded/finalized）；读取/解析异常不吞：记入 errors（artifact_id + 异常名），继续枚举。
1b. `_verify_selected_records(state, records)`：把现 `_managed_external_job_verification` 的后半段（oc:≈350-383 的 probe/terminal/success_evidence/refs 铸造循环）抽出为独立函数；`_managed_external_job_verification` 的选择器前半段保留并改两点：候选域从 `_submission_payloads(state)` 扩为 `_submission_payloads(state) + _task_external_jobs(state)`，按 `_job_key_for_record` 去重且同 key 时优先 submission payload（它带 route_attempt_id）——与 finalize 侧 `_external_job_record`（rm:5834-5835）同域；scope 字段匹配（oc:305-318）对 scheduler/launch_host 改用 casefold（镜像 rm:6433-6438 的 `_external_job_evidence_identity_value`），其余字段仍精确。

§2 首次收尾（`if not resumed_input:` 块内，替换 oc:≈1452 起的触发逻辑）
2a. 先算 `own_jobs, excluded, ledger_errors = _own_run_managed_submissions(state)`。
2b. 调用方给了 job_ids/external_job_refs：走现有选择器解析（§1b 扩域版）；解析失败保留现有 error_code（external_job_identity_missing/invalid/ambiguous/mismatch）但升级文案（§6a）。解析成功的记录与 own_jobs 按 _job_key_for_record 取并集——调用方只能增选（跨 run 收养）与交叉确认，不能缩小框架枚举出的本 run 集合。
2c. 调用方没给且 own_jobs 非空：selected = own_jobs，直接进 §1b 校验。
2d. 调用方没给且 own_jobs 为空（continuation run 主场景）：取 `unresolved_external_workflows(state)`（rm:6336）；恰一行→按该行 _job_key_for_record 在 §1b 扩域中解析成权威记录并收养；零行→维持 HEAD 现状不动（kind=external_job 记 managed_job_ids_declared passed=False，oc:1428-1430，success 走机械降 partial——b5d9512a 判决保持）；多行→新增一次性拒绝 `external_job_adoption_ambiguous`，错误体逐行给出每个候选的 `_external_job_reference`（可整项复制为 external_job_refs 元素），文案明确"重调时任选其一传入即可"。
2e. selected 非空时调 `_verify_selected_records`；`resolved_external_job_refs` 恒取其铸造值。三处门按 outcome 分流：`external_jobs_terminal` 不通过时，outcome==success → 保留现有拒绝 external_jobs_not_terminal（oc:≈1480）；outcome∈{failed,blocked} → 只留 passed=False check，不 return（新行为）。`external_jobs_successful` 与 success 路 route projection 维持现状（仅 success 拒）。
2f. 披露：excluded 非空→写进 closure_input 新字段 `external_jobs_excluded`（含各自 lifecycle_status）并 append_transcript，不作 failing check（避免 cancel→重提→success 被误降级）；ledger_errors 非空→append check `external_job_ledger_degraded` passed=False（evidence 含 artifact_id 与异常名）——success 由既有降 partial 机制（oc:≈1560-1571）接管，绝不拒绝冻结（焦点二裁决）。

§3 resumed_input（partial 续传，oc:≈1268-1317）
仅当 `resumed_input.get("external_job_refs")` 为空时启用回填：按 §2a-2d 同样两级枚举+§1b 校验；成功→resolved_external_job_refs 取铸造值、append 校验 checks 与 passed=True witness `resumed_refs_backfilled`；冻结 outcome==success 时施加与 §2e 相同的 terminal/success 硬门（拒绝文案指明"存量 partial-success 无成功证据，走 continuation run"），failed/blocked 只记 witness；续传路径不启用降级（closure_input 复制 resumed_input，避免 outcome/status 分叉）。冻结 refs 非空的续传保持 HEAD 行为一字不动。回填只进本次落盘的 clean/log metadata 与 checks——已冻结的 raw_results 不改（已验证：operation_closure_input 无任何跨件比对消费方；幂等身份只比 task_kind/objective/outcome 三元组）。

§4 幂等 complete 分支（oc:≈1220-1266）
不重写、不重验。仅当冻结 log 的 metadata.external_job_refs 为空且 `unresolved_external_workflows(state)` 非空时，在幂等成功返回里加三个附加字段：`external_job_refs_frozen_empty: true`、`unresolved_external_workflows`（最小行）、`recovery`："该 closure 铸于自动派生 refs 之前，不能作为这些作业的 finalize 证据；用 resume_run 开 continuation run——新收尾会自动收养未决 workflow 并铸出可 finalize 的新 log"。

§5 报错文案要点（oc 侧）
identity_missing/mismatch 类错误：附近似候选诊断（同 job_id 的账本记录之 _external_job_reference、casefold 归一后的 missing/mismatched 字段清单，复用 finalize 诊断的形状），并加一句"external_job_refs 可整体省略——工具会从受管提交账本自动派生完整身份"。全文件禁止出现 `job_submission.required_external_job_ref` 字样。

§6 finalize/hooks 纯文案改动（扩大半径的理由：这是 F10——本缺陷的另一半；两段现文案把 agent 指向被 operation_closure_owner 锁+幂等封死的 "amended experiment_log" 死路，与本节点两次记录的"amendment 出口走不通"教训同型复发；改动零谓词、只动字符串与一处返回附加字段，可独立 revert）
6a. rm:7649-7652（operational 收尾缺 evidence 的错误）：改为"先调用 record_operation_completion 记录 operation 收尾（refs 由工具从受管提交账本自动派生并冻结），再以返回的 experiment_log artifact id 重调 finalize_external_job"。
6b. rm:6494-6500（identity 诊断 recovery）：在 finalize 调用点（rm:≈7799-7812，能读到 operational 布尔）按模式覆写——scientific 保留原文（save gate 豁免 scientific，oc:64-66，另存新版 log 真走得通）；operational 改为"operational closure 的 log 只能由 record_operation_completion 铸造；空 refs 的存量 log 走 continuation run 自动收养后重试"。required_external_job_ref 字段本身保留（test_external_job_handoff.py:1842 断言它）。
6c. hooks:≈3728-3746 closure gate 的 evidence_label：operational 分支的"手抄完整 identity"指引改为指向 record_operation_completion 自动派生；scientific 分支原文保留（手抄绑定是科学侧刻意设计，R1 已证）。

§7 refusal 登记（docs/verdict_demolition/refusal_registry.yaml）
新增/补登（均 class B，记录完整性）：external_jobs_not_terminal（注明已收窄至 success-only）、external_job_success_unverified、external_job_identity_missing/invalid/ambiguous/mismatch、external_job_adoption_ambiguous（新）。同 PR 必须让 tests/test_refusal_sites_are_classified.py 转绿：实测 scan=1090 vs baseline=844、oc.py 29 vs 基线 6、registry 仅 2 条——本分支既有 ~23 处 oc 拒绝点需逐条登记或改写，禁止 --write 静默抬基线。若清欠工作量超出本 PR，拆为同分支前置 chore commit，但合并前测试必须绿。

§8 明确不做
不加 amend/supersede 通道（C）；不给 finalize 加账本回退谓词（B/D——修订 A 落地后新 closure 不再产生空 refs，回退唯一受益者是存量，而存量由 §2d 收养路径+continuation run 覆盖）；不动幂等、owner 锁、exactly-one 审计、E 的数据模型正规化（refs 是冻结时刻的 attestation，账本是活库，且科学侧无 closure_id join key——留作后续提案）。

## 落选理由

- A 原案：枚举原语『本 run 账本』在代码中不存在（v2 节点目录跨 run 持久且信封 run_id 被 _read_json_artifact 丢弃），fail-closed 是不可探测的死路墙，terminal 恒拒取缔合法的运行中 blocked closure 并在含已取消 k8s 作业时锁死全部收尾，报错指向不存在的字段，且对三个存量死锁 run 零疗效。
- B（finalize 账本回退）：把身份墙变 opt-in、给冻结时从未核验过作业的存量 closure 的 success 宣称洗白（错误完成态比死锁更坏），且不治冻结侧陷阱；修订 A 落地后其唯一受益者只剩存量，而存量已有 continuation-run 收养这条零谓词改动的出路。
- C（amend 通道）：开创『修订冻结证据』全新先例类别，全局削弱 frozen 不可变性，需 amend 时重跑全部核验才不致洗白，单独不治新 run 陷阱；收养路径使其连救存量的剩余价值也没有。
- D（A+B 双侧）：为仅存量受益付出对 finalize 墙的永久软化，并继承 B 的集合不一致洞（回退恰恰服务于没做过冻结时核验的旧 closure）；修订 A + 收养以更小半径覆盖其全部收益。
- E（数据模型正规化）：『消灭复制』误判数据性质——log 里的 refs 是冻结时刻的 attestation 而非账本副本，账本是可追加活库，join-at-finalize 使 closure 辖域随时间漂移；科学 analyzed_* 路径无 closure_id 可 join，且会消灭科学侧刻意的手抄绑定证明；爆炸半径最大，留作后续正规化提案而非本缺陷修复。

## 回归测试（14 条）

- ① 主陷阱消灭：operational run 经 submit_job、作业成功终态，record_operation_completion(success, 不传 refs) → closure 冻结 refs 非空且 _job_key_for_record 与 submission 记录一致 → finalize_external_job(operation_completed, evidence=该 log) 走通，workflow 关闭，run 结束无 awaiting_external_job（F9 回归 ①②）。
- ② 教育一次即可执行：传字段不全/大小写漂移的 refs → 拒绝一次，错误体含近似候选的完整 ref 与 missing/mismatched 字段、并声明 refs 可省略、不含 job_submission.required_external_job_ref 字样；随后去掉 refs 重调 → 成功且 refs 完整（F9 样本 2b 前半 + F10）。
- ③ 失败作业诚实收尾：作业 exit≠0，record(failed, 不传 refs) → 冻结成功、refs 自动填、external_jobs_successful passed=False 在 checks；finalize(operation_failed) 关 workflow。
- ④ 运行中 blocked 收据保留：作业仍 running，record(blocked, 不传 refs) → 冻结成功、refs 已填、external_jobs_terminal passed=False witness；下一 session 作业终态后用该 log finalize(operation_blocked) 走通（焦点一）。
- ⑤ success 墙保留：作业仍 running，record(success) → 拒绝 external_jobs_not_terminal（仅 success 触发）。
- ⑥ 取消不毒化：cancel 作业 A（lifecycle cancelled）后重提 B 成功，record(success) → refs 仅含 B，A 进 closure_input.external_jobs_excluded 披露，success 不被降级，finalize(B) 走通（R3 毒化场景）。
- ⑦ 旗舰跨 session 交接：session1 submit+handoff 退出（无 closure）；resume_run 起 continuation run（新 run_id、零本 run 提交），record(success, 不传 refs) → 单例收养未决 workflow、refs 完整，finalize 关闭 session1 的 workflow（R3 作用域死结回归）。
- ⑧ 收养歧义一次性教育：continuation run 面对两个未决 workflow、不传 selector → external_job_adoption_ambiguous 拒绝一次并列出两候选完整 ref；带其一重调成功。
- ⑨ 存量 partial 回填：修复前冻结的空 refs partial closure + 账本有终态作业 → 续传完成时 log metadata 回填 refs + resumed_refs_backfilled witness，finalize 走通；raw_results 冻结件逐字节不变（R2 resume 发现）。
- ⑩ 存量 complete 死锁出路：空 refs 的 complete closure 幂等返回携带 external_job_refs_frozen_empty=true、未决 workflow 清单与 continuation-run 指引；按⑦形状实际关掉该存量 workflow（三个真实死锁 run 的最小复现 1788334189-2b872a / 1788337265-2f223d）。
- ⑪ 账本降级不封死：伪造一条损坏的 job_submission artifact → 收尾照常冻结，external_job_ledger_degraded passed=False witness 含 artifact_id 与异常名；outcome=success 时被机械降 partial 而非拒绝（焦点二）。
- ⑫ kind 无关触发：toolchain_build/build 携带本 run 受管提交 → 与 external_job 同路自动补全与校验（R1 攻击点 5）。
- ⑬ 文案回归：rm 两处与 hooks closure gate 的 operational 指引不再含 'new or amended experiment_log'/手抄 identity 死路；finalize 错误 payload 的 required_external_job_ref 字段保留（test_external_job_handoff.py:1842 兼容）。
- ⑭ 既有套件全绿 + 棘轮闸转绿：test_external_job_handoff.py、test_blocked_operation_closure.py、test_cross_run_route_leftover_finalization.py、test_refusal_sites_are_classified.py（registry 补登后）；跑法带 --with pytest-asyncio，对拢分支 15 条既存失败基线。

## 未决问题（需 owner 拍板）

- unresolved_external_workflows 每行带一次调度器健康探针：收养路径（tier-2）与幂等披露路径的探针成本/可达性是否可接受？规格已把两处收窄为『仅在需要时计算』，但离线/调度器不可达时 tier-2 单例收养会因 health 探针失败而 terminal=False——blocked/failed 收尾不受影响，success 收尾此时被拒是否符合预期，需 owner 确认。
- oc.py 的 ~23 处棘轮欠账（29 vs 基线 6）清偿放本 PR 还是拆前置 chore commit：test_refusal_sites_are_classified.py 当前即应为红，合并策略需 owner 拍板；本环境无 pytest 无法实跑确认红绿。
- 同一作业在扩域候选中同时存在 submission payload 与 task 行且字段完整度不同（如 task 行缺 route_attempt_id）时，规格取『优先 submission payload』——是否存在真实双源不一致案例需要一条专门测试。
- 存量里是否存在『partial 冻结为 success 且作业实际失败』的 run：规格对该角落选择拒绝+continuation-run 指引而非降级（避免 closure_input/status 分叉）；若真实存在此类 run 需确认该出路可走。
- HEAD 已前移到 beace2e6（#726 verdict 义务去 stage）：实现前需 rebase 核对本规格引用的行号与 b5d9512a/341f3af3 判决现场是否被该 commit 触动（本次核查未见 oc/rm 相关函数变化，但未逐行 diff #726）。
