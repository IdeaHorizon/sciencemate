# R3-A 正式输入接线检查点（2026-09-23）

## 基线与边界

- 工作区：`harness-framework-impl-060-r3a-c`，分支 `codex/060-r3a-c`，无 upstream。
- 本轮基线：`a80703eed53e7f0473f5fb8ef6e4e13a670c47de`；产品代码未 push，无 PR。
- 只改 Experiment。Core 的合同、状态、完成规则及共享登记均未修改。

## 修的共同问题

Core 已按 task uuid/revision/digest 选定正式合同，但 bootstrap、旧 contract reader、输入见证、intent 哈希原先仍消费可变 node-input 提示。相同任务会被多个入口解释成不同绑定，甚至只纠正一个过时提示就被拒为 intent_changed。

新增的 `task_input_view` 是纯读取适配，不写授权、不扫描 latest、不建立第二本账：

1. Core identity 存在时，只读并核对该精确合同；缺合同或不完整 identity 不回退到 node inputs。
2. 三个输入消费者与 intent 哈希使用同一有效输入；旧验收收据必须与 Core 实际绑定一致，不能自称 task-contract 来源。
3. 同合同、同 artifact@version@hash 的可用性恢复由现有 turn hook 自动重新核验并追加观察；不重跑动作、不增加模型工具。
4. 新 digest 不会从项目后继版本自动采纳；目前须由父编排器 fresh 派发。Core 没有 same-run 合法 adoption 接口，节点不猜授权。
5. Core assignment 字段缺席不等于调用方显式 pending scientific 请求。A 保留 `task_assignment_kind=pending_assignment`，本交付无绑定仍为 absence；未消费的缺失输入不阻止 operation 成功或诚实 blocked。

## 回归证据

- 新正式输入回归首批在修改前 **7 failed**；自动 hook 可达性及 Core 字段缺席不等于 scientific 的反例也分别先证实转红。
- 正式输入与完整性专测：**14 passed**；另覆盖真实产物 action → finish → audit、自动可用性重评、普通 retry、新合同不自动采用。
- 节点全量：`uv run pytest -q nodes/experiment/tests --tb=short` → **3719 passed / 20 skipped / 4 xfailed**，135.10 秒。
- 四条 xfail 中三条是新加入的已知 Core completion 缺口，严格断言不得 completed；另有一条原有 xfail。不能把它们算成修复通过。
- 只读框架相关四文件：**21 passed / 2 failed**。失败与 a80703ee 检查点一致：共享 refusal registry 未同步，以及已退役工具的旧模型文案；无新增 subprocess 调用点登记债。
- 本轮新增/修改 Python 文件实际 Python 3.11 编译、diff whitespace 检查通过。

## 已交接与未完成

- [Core #1141](https://desktop-9el2944.taile9f15e.ts.net/wangd/harness-framework/issues/1141) 已创建并分配 wangd：交付未 commit、精确 head 漂移或 project-workspace 缺交付时，不得凭 artifact type/任意工作区改动判 completed。可执行反例与建议在 `r3a_core_completion_handoff_20260923.md`。
- 后续 action-specific 包基于 `b8d44974`：bash、Python、真实 submit 共用输入消费核验；先按显式 selector 选择本次输入，再核验这些 spec，保留单 spec 内资产完整性。没有 selector 的准备动作和 dry-run 不核验无关请求；审计失败或异常都不启动载荷、不提交作业。v3 同时停用旧的全 active-spec 输入审计，避免重复检查和污染 run 见证。
- 子集回归在修改前 13 failed / 1 passed；修改后 14 passed，连同原有 preflight / input-delivery / contract-audit 测试为 94 passed / 1 skipped。旧 bash 入口另经执行前 tripwire 证实跳过消费核验；三个入口及真实 sandbox 正例 8 passed。当前节点全量 3740 passed / 20 skipped / 4 xfailed（133.74 秒，收集后补入的真实 sandbox 正例另已通过）；上节数字保留为此前正式任务 authority 检查点的历史证据。
- 相关四个根测试仍为 21 passed / 2 failed。失败项名称没有新增，但 refusal registry 差异确实增加了 `input_admission.py:1/0`：这一个共用执行前拒绝保护实际选中但未验证的输入，另含审计不可读取时的同出口；只限制当前消费动作，提供修正或诚实终态出口。根登记文件只读，不能以失败条数相同声称没有新增集成债。旧工具文案的八项失败保持不变。
- 父节点直接提供 dataset 的正式验真路径还未完整接通；不能借本轮修复新加“必须重新登记/重跑 Data”的墙，也不能宣称输入已验真。
- 旧运行不作迁移。现存旧 contract reader/receipt 是暂时的接口适配，随 S1d 旧消费者撤除，不是长期双权威。
- 完整协作 gate 已在干净 `87a359e2`、共同基线 `fb88f35f` 上跑完，**未通过**：Python 3.11 编译及受审树完整性通过；根测试 HEAD 4123 passed / 6 failed，base 4125 passed / 4 failed；全节点 HEAD 4395 passed / 5 failed / 35 skipped / 4 xfailed，base 4235 passed / 5 failed / 32 skipped / 1 xfailed。五项节点失败全部为两边相同的 postprocess 既有失败，Experiment 无新增失败。
- 相对共同基线新增的两项根失败为 refusal registry 和 `test_tool_contracts_reach_the_caller`。后者跨模块收集内部交付封套与 ledger 字段，含 `delivery_owner`、`schema`、`scope_status`、`idempotency_key`；这些由确定性运行时生成，不应为过静态检查强迫模型填写。完整失败字段与共享登记变化须交 Core owner 核查；不能改错误文字躲扫描，也不能自行修改根测试。文档站另有两页新增 tools 漂移，在节点写权限之外。
- 旧工具白名单的八项失败经调用链复核均来自 v3 未启用的 hook 或未暴露的 legacy 工具；暂未发现 v3 模型可达路径。保留失败，不恢复旧工具；S1d 撤旧时一起处理，不靠改测试掩盖旧代码债。
- CI 额外的慢测试闸和外部失败回报步骤本地 gate 未覆盖，未触发外部回报。真实 UI/模型/Slurm 和 S2-min 尚未验收；本检查点不是可发布或端到端完成声明。

## 312d25a5 上的纠正路径贯通复核

以上测试数字与未完成项属于各自历史检查点；普通父包的后续进展以
`r3a_parent_dataset_admission_20260923.md` 为准。本次仅补验证，不修改运行时：

- 同一 Core task digest 和 prereg 三元组由未冻结变为冻结：正常 turn-start
  自动重评，恰好追加一次 authority refresh；真实本地计算的 execution context
  与 finish 交付均绑定同一身份。再次 turn/finish 不重跑动作、不追加账本，复用交付。
- 既有负例保护旧 run 不采纳后继合同；新增正例证明首次 bootstrap 前已经选择
  revision 2 时，节点采用它，不被 stale node-input revision 1 覆盖。
- 测试通过真实 `TaskContractLog` 写入选择，再进入节点；**未走父编排器实际
  派发 API**，不证明 same-run amendment，也不证明 UI/跨进程恢复。
- 自动可用性重评位于正常 turn-start；直接调用 finish 不会额外探测并改写 A。

`test_060_authorized_correction_probe.py` 与 `test_060_formal_task_authority.py`
定向 **13 passed**；主会话连同 scientific producer/check-only lineage 复测
**22 passed**。这是既有恢复实现的贯通回归，不是新修复的 base-red 声明。

仍未解决：`execution_context` 的参数只是工具入参 witness；bash 的
`_simulation_contract_block` 与 submit 的旧 preflight 比较后只记录 deviation，
未接入正式偏离授权。finish 对交付 producer 的冲突核验不能替代执行前授权。
须先核实 Core 已有 amendment/决定接口，再接正确 owner；普通高风险确认、
模型解释、pending amendment 或参数比较通过都不得伪装成科学偏离授权。
不恢复旧路线门，不让无关准备动作一并受阻。该项仍是未实现，而非已通过。

## 831a610c：正式修订与决定接口的事实核验

本节是代码与接口核验，不是已实现偏离授权，也不是 UI/模型运行证据。
核查范围为当前 checkout 的 Core/shared 只读实现；没有修改它们。

| 现有入口 | 能证明什么 | 不能据此声称什么 |
| --- | --- | --- |
| `task(action="contract", parent_revision_digest=...)` | Core 追加合同版本，保留 actor、reason、父 digest 与调用方声明的 prereg assignment | 工具不独立验证真人批准，version/hash 参数可缺省；不能因此声称完整绑定已验证，也不能由 Experiment 擅自改写任务 |
| prereg 作者保存 amendment，再冻结新版本 | 保留原版本和修改来源；新版本有独立冻结身份 | amendment 的存在不自动更新既有 task 或 child 的绑定 |
| 父调用方 `run_node` 指定 task UUID 与新 digest | 派发按该精确合同，旧 digest 的 interrupted run 不作为同任务续跑候选 | 不是把原 child 原地换绑，也不自动接管旧开放作业 |
| `request_human_input` | 一次通用问答；实际路径可能是真人回答或自动答复 | 不是绑定旧 prereg、新参数及决定来源的科学修改许可 |
| 高危命令确认 | 绑定特定命令文本的一次性执行通行证，持久副本有 TTL | 不是 task/prereg 修订或科学参数豁免；风险预授权不等于任务变更授权 |
| post-node `decision_package` | review 后的 proceed/revise/redirect 等流程选择 | 不是任意 scientific action 的参数变更接口 |

可复用的调用链：

1. `shared/tools/library/tasks.py::_task` 调 `core.task_contract.TaskContractLog.append`，
   写 `<project_root>/tasks/task_contract_revisions.jsonl`；有后继版本时需精确指定 digest，
   不取隐含 latest。
2. `shared/tools/run_node.py::_resolve_task_identity` 读取所选合同；
   `_resumable_run_for` 与显式 resume 的身份核对防止新合同混入旧 run。
3. `core/executor.py` 把选择的 task UUID/revision/digest 传给 State；
   `assignment_for_state` 与节点 `task_input_view` 读取同一绑定。
   直接调用 `State.reopen` 不会替调用者找回或选择合同，不能拿它代替整条派发链。
4. 冻结 prereg 的修订经过 `State.save_artifact(amendment_reason=...)` 形成新未冻结版本，
   由所属 owner 冻结，再由父调用方明确选入新合同。Experiment 不自造科学授权收据。

自动答复须分开说明：`request_human_input` 在 bypass/continuous 可直接返回
`bypassed=True`；普通 autonomous 可由 pause driver 按推荐项代答。高危确认则不同：
未预授权的 autonomous 受 `NEVER_AUTO_APPROVE_TYPES` 保护，仍等待人工；显式类别
或 `"*"` 预授权可在 `build_pause_payload` 发一次性票据。continuous 设置 bypass，
现有 bash/submit 调用方可以直接跳过交互确认并留痕，不必经过“代答再发票”路径。
以上都不会自动修改 A。

高危票据按 session 与命令文本摘要绑定，持久票据有 TTL、消费后删除；内存确认
先于 TTL 检查，不能概括为所有确认均限时。没有 project_root 时仅存内存。
回答落到 pending 到 hook 建立票据之间仍有恢复窗口，
不能把“存在落盘路径”宣传成任意崩溃点恢复已验证。该能力也不同于出网 grant 的
进程内语义，后者仍按 #1069/#770 的既有交接处理。

当前安全的纠正流程是：有权的任务/prereg owner 形成新版 → 父调用方精确重新派发 →
新 child 执行；旧 run 保存原身份、已有事实并诚实收尾。若旧 run 仍有开放作业，必须
先按现有生命周期终止或正式交接，不能因为新合同已写入就抹去旧副作用。
参数入参缺失、额外诊断参数或未选中正式输入，不自动等于“实质偏离获证实”；
不能把旧比较器的整个 `passed=False` 直接恢复成全局执行门。

仍缺的不是另一份记录表，而是当前 run 内已知实质偏离的正式决定与消费契约。
当前 checkout 未找到可直接复用、同时绑定任务/prereg/拟变更并明确自动决定语义的接口。
本节不决定该能力必须由真人逐次批准，也不将通用问答或普通风险批准冒充此能力。
后续若需原 run 继续，先由 Core owner 明确 authority 与恢复方式；若采用已有新版合同
重新派发路径，则应验证父 API 到节点消费，而非只用直接写账本的 fixture。

既有根测试 `test_task_authority.py` 与 `test_the_dispatch_carries_the_task.py`
在此 checkout 的 Python 3.11 环境复跑 **23 passed**。它们覆盖合同不可变性、
精确身份和恢复过滤，部分采用静态检查或替换模型循环；尚不等于真实父 API
贯通节点 action/finish，更不是 UI、真人审批或跨进程恢复验收。

随后新增 `test_060_parent_contract_dispatch.py`，保留真实注册工具
`task(create/contract)` → `run_node` → executor → agent loop → Experiment bootstrap，
仅将外部 `LLMClient.chat` 换成有界固定响应。父夹具只允许派 Experiment；
两次调用以 `report_blocker` 诚实结束，分别核对合同 v1/v2 的 UUID/revision/digest
与 prereg 身份、两个不同 child、父子 dispatch ID 相接、旧 ledger 不变，以及两边均
没有 `action_started`。这证明真实父 API 的绑定传递，不声称模型自主恢复、
原 run 内改绑、科学偏离获批或计算任务完成。action/finish 的本地执行贯通仍由
此前的独立纠正用例证明，不能把分开的两段拼成一次 UI E2E。

主会话 Python 3.11 联合复跑新增文件、两个节点纠正文件、上述两个根文件及
命令执行入口登记测试，结果为 **40 passed / 1 xfailed**；新文件 Ruff 与实际
Python 3.11 编译通过。唯一 xfail 是同文件独立的 text-only 假完成反例，
`--runxfail` 实际为 **1 passed / 1 failed**，失败类型精确为
`KnownManifestOnlyCompletionGap`。不是把未完成判据计为通过；原始证据持久保存，
根因与 owner 见 `r3a_core_completion_handoff_20260923.md` 的 2026-09-24 增补。

## 2026-09-24：参数见证不完整不等于已证实科学冲突

基线 `2ea8d800` 的 finish 将 `missing_expected_parameters`、
`unexpected_execution_parameters` 和明确的 `mismatched_parameters` 取并集，
分别在 producer 与 check-only 两处升级为成功拒绝。这样连任务产物已经真实生成、
required 已通过、只是少记一个参数见证的合法路径也被拒绝。

当前修复只在正确 owner（C 的统一收尾）区分这三种比较结果：同一精确绑定的
明确不同值仍反驳成功；缺项/额外项在 limitations 中逐字段披露。原 comparison
的 `passed=False` 和三张明细不变，不伪造匹配、不改写 B、不增加准入门或授权收据。
required 文件、命令验证、开放作业和产物身份等成功条件保持原样。

新增真实本地进程回归：正例实际使用正确标量及三次重复，只省略 `replicates` 的
工具参数见证；另一个正例多报诊断标签；check-only 也覆盖缺项。修复前固定
Python 3.11 环境复跑为 **3 failed / 1 passed**，均是上述误拒；混合缺项、额外项
与明确值冲突的负例已通过，且 partial 可诚实收尾。修复后联同既有参数比较、
producer 与 check-only 反例定向 **23 passed**。本地日志为
`nodes/experiment/.tmp/partial-parameter-witness-base-red.log`。
该四项版本随后跑完整 `test_060*.py` 为 **392 passed / 5 xfailed**；xfail 保持为
未修复问题，不计作成功。独立审查另跑五个定向文件 **29 passed** 并核对七格纯函数
边界；只接受本次收尾语义修复，不表示 060 或 Core 接线完成。
最后把不同绑定、check-only 混合冲突及仅额外字段三格固化为纯 consumer 回归，
新文件共 **7 passed**；主会话连同四个相邻文件复跑 **32 passed**。
Python 3.11 编译、新测试 Ruff 和 diff 检查通过。finish 的既有 Ruff 问题为
基线 41 项、修改后 39 项，不声称该老文件全绿；原 AST 拒绝计数仍为 11/11，
模型字段扫描没有新增或删去的拒绝字段，共享登记债没有因本片增加。

不扩大结论：纯比较仍失败；完整实际参数使用没有因此自动验证。模型文案对一致性
的要求也没有取消，只是不把缺少见证反推为确定性事实。若任务要求特定参数或领域
验证，仍应由该任务的实际检查证明，不能以本条 limitations 替代。

执行前偏离处理仍未闭合。现有 Core `intended_use` 是任务级，Data 的
`request_kind/purpose/scientific_authority` 是包请求及来源级，动作 selector 只表示
读取哪个包；它们不足以判断同包计算是诊断还是正式实验。不把
“formal 包 + 任意比较失败”恢复成执行墙，不从命令/stage/旧 route 猜用途。
需要原 run 内变更时，仍待正确 owner 明确动作约束适用性与正式决定消费接口；
现有新版 prereg/任务合同后 fresh 派发的路径不变。本片不声称已实现该能力。

## 2026-09-24：父合同修订到真实本地交付的贯通证据

在 `ef73a83d` 上扩展 `test_060_parent_contract_dispatch.py`，没有修改产品运行时。
此前只证明了父合同到子 bootstrap 的传递；本次把同一条生产调用链延伸到
`safe_run_bash` 的真实本地进程和 `finish` 的真实冻结事务，不再把两段独立测试
拼称为一次贯通。唯一替换的是外部 `LLMClient.chat`：每个 child 三次固定响应，
依次执行、收尾、退出；后一响应必须先读到上一工具成功，额外响应立即使测试失败。
这是确定性派发集成，不是 UI、真实模型自主恢复或 Slurm 验收。

两轮使用同一项目、同一 task UUID 的两版正式合同和两版冻结 prereg，分别实际计算
7 的平方与 8 的平方，输出 49 和 64。第二轮用独立父 session 选择新 digest 并 fresh
派发；特意附带的旧 prereg 提示不能覆盖该选择。第一父会话的待审查 flow 不清空，
旧 child 不换绑、不重执行；两个输出在执行前就指定为不同的项目工作区路径。

每轮分别核对三层证据：

- A：父 dispatch、子 `run_start`、节点账本、action context 和 raw 中的
  task UUID/revision/digest 与 prereg 精确相接。
- B：唯一真实 action 和同 ID 的 settled/exit 0；实际文件内容；clean 的 required
  验收及 producer 指向该 action，参数比较保留为工具见证而非泛化的实际用参证明。
- C：唯一 finish commit、manifest、三件当前 frozen record 的版本/hash/producer
  一致；只读交付审计为 sealed/success。两组 artifact ID 不重用；新版执行后旧账本、
  manifest、结果文件及第一父会话的待处理 flow 保持不变。

父夹具只允许派 Experiment，所以真实 post-node 流程尝试 Reviewer 时因白名单
受阻，外层返回 pause；测试同时核对真实错误事件和包含本轮三件套的 flow entry。
这不是 Experiment 交付失败，也不是 Reviewer、Analysis 或科学结论已经完成。
不以 summary 的 `completed` 或 `node_task_outcome` 投影代替上述节点证据。

开发中曾把 obligation 写成子状态目录相对路径，而实际默认 shell cwd 在项目
`experiments/runtime`；计算成功但 finish 如实返回 missing。固定脚本若仍直接退出，
Core 会落入既有假完成缺口。本片纠正的是夹具路径和测试推进条件，不改产品路径安排，
不放宽 success。独立 text-only 反例以 `--runxfail` 再跑仍为 **1 failed**，
精确错误 `KnownManifestOnlyCompletionGap`，#1141 没有因此解决。

主线独立联合父派发、根任务身份、纠正、参数见证与命令执行入口登记回归为
**37 passed / 1 xfailed**；独立审查接受本测试片，已读此日志但未另行重跑。
原始正负证据保存在 `nodes/experiment/.tmp/parent-chain-proof-i8UCOM35/`。
这证明现有正式 owner 修订后重新派发的一个本地可行路径，不证明真人批准了偏离、
same-run 改绑、旧开放作业自动接管或全框架完成判定已接通。
