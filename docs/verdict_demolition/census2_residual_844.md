# 二审普查：残余 844 拒绝点逐条回读（2026-09-02）

- 对象：`refusal_baseline.json` 当前基线 844 点（93 文件），五路并行逐条打开源文件读所在函数，**只读不改**。
- 问题：wangd 三问——剩下这些全都符合科学第一性原理吗？是最优设计吗？符合如无必要勿增实体吗？
- 结论：**三问答案都是「不」**，数据如下。原始逐条记录在 `census2/*.jsonl`（会话 scratchpad），本文收录 D/X 全量与结构性发现。

## 0. 计数

| 类 | 点数 | 占比 | 含义 |
|---|---|---|---|
| A 安全/资源 | 91 | 10% | 合法墙（S6） |
| B 记录完整性 | 36 | 4% | 合法墙（放行则账假） |
| C 物理/协议 | 443 | 52% | 合法（契约事实）；其中 **176 处可由注册表 schema 校验整批替代** |
| E 事实报错 | 151 | 17% | 不是墙，是转述 |
| **D 充分性判决** | **59** | **6%** | **非法类，仍站着** |
| **X 无必要实体** | **64** | **7%** | 死码 / vendored / 重复抄件 / 误计 |

合法+事实 = 721（85%）。**不该在的 = 123（15%）**。

## 1. 三问的诚实回答

### 1.1 全都符合第一性原理吗？不。
- **D 类 59 处**仍是「不够好 / 先做 X / 缺 Y 所以拒 / 顺序不对」。
- 更糟的是其中 **约 20 处是一审（PR#719 判决书）已判降格、执行批次报告「已落地」但代码原样未动**：
  `run_node:1690/1725`（writing-gate，fire_data 28 次开火的冠军墙）、`run_node:1217/1312/2005`（O4）、
  `kb:191/285`（O1）、`kb:686`（O3）、`kb:2059/2074/2084`（O8）、`skill_tools:670/688/698`（O9）、
  `proposals:244`（O3）、`artifacts_extra:729`（O7）、`kb_schema:465/474`（O2）、`builtin:312`（O1）、`builtin:1000`（O10）。
  这是执行侧的报告≠事实（我在收官报告里把它们算作已完成，是我的错）。
- **昨天重建的 figure.py 又长出 1 处新 D**（`figure.py:272` caption/alt_text 非空闸）；vlm_witness 3 处「一坏整份作废」形态也应改剥离+记账。

### 1.2 是最优设计吗？不。四个结构性问题
1. **工具注册表没有 schema 校验器**（`core/tool_registry._execute_dispatch` 不校 required/enum/min/max）。
   结果是 30 个文件里 **176 处手写参数校验**，每处一个实体、一份报错文案。一个派发口的 jsonschema 校验替代全部，
   并顺带兑现「契约必须送到调用方」（合法值在 schema 里，模型第 0 轮就看见）。
2. **`shared/lib/kb_schema.py` 是手写的 schema 解释器**：28 处 raise 是同一件事写 28 遍，应收成每 entity 一份声明 + 一次校验。
3. **同一问题两答案**（分叉不报错）：
   - run_node v2 分支「必需输入零个→不拦」 vs legacy 分支 `2005` 拒；
   - `kb.py update_claim_status` 里 authority/prereg 两门已降落 provisional，同函数 `validate_status_flip` 仍硬拒；
   - kb_schema 把 refuted/superseded 定为终态，`curator_scan(stale_dead_end)` 却提议重审（通过后无路可翻）；
   - scheduler 枚举 `{local,slurm,pbs,kubernetes}` 五份、stage 词表三份。
4. **基线 844 本身只是棘轮读数，不是普查**。扫描器只认两种句法；仓库里另有 **≈450 处其它形态的拒绝**
   （`raise ValueError` 214、`RuntimeError` 45、`_error()` 助手 ≈64、`_err()` 12、`ok:False` 13、专用异常
   ProjectWorkspace/Offer/ModelRole/TaskList ≈34、FileNotFoundError 46 …）、builtin 5 处 tuple 形态返回、
   2 处 docstring 误计。按形状看多数是内部契约/事实（C/E），本轮未逐条分类；诚实分母约 1,300。

### 1.3 符合如无必要勿增实体吗？不。
- **X 类 64 处**：vendored 第三方 `bensz-nsfc/scripts/install.py` 22 处（上游自标 deprecated，仓库零调用方）；
  `forward_artifact` 工具整体死码（登记表零生产读者）；`core/memory_migrate.py` 整模块、`State.update_memory_lifecycle`
  零生产调用方；5 处枚举校验后不可达的 `internal: unhandled` 尾巴；`tasks.py:78` 工具层把一审在 core 删掉的
  空 title 墙**重新立起来**；runtime_control 4 处、tasks 5 处对同一条件重复手写；resource_manager 3 处死分支；
  `publication_figures:100` 运行时防词表复活闸放错层（应是测试/AST 锚点，不该因一个键名拒整张图）。
- **清单外**：`nodes/writing/tools/podsys_safe_source.py`——一次性、写死某次外场研究（164 节点、具体 SHA256、
  记录日期）的证据包构建器，51 处 ValueError，只有一个脚本和它自己的测试调用。领域一次性代码住在框架里。
- **一审删字数闸留下的假文案 ≈15 处**：代码只查非空，报错与工具 description 仍写「≥N 字符」
  （audit:60/92、run_node:3696/3702/3754/3758、tasks:195/219、kb_schema:452/744、cross_model:112、profile_tools:116、
  runtime_control:431、kb:2172 …）。模型会照文案凑字数——框架在制造症状。

## 2. D 类全量（59）

| 位置 | 动作 | 理由 |
|---|---|---|
| `core/domain_registry.py:573` | keep | 『挂叶必须人批背书』是治理仪式而非账真假（记录只会如实写 approved_by 为空）；唯一生产调用方 promote() 恒传 approved_by，从不开火；README 档三点名『domain_registry 人批背书』→呈裁 wangd |
| `core/kb_promotion.py:487` | downgrade | 三查合一拒：evidence_frozen 是 B（未冻证据晋升=org 断链）该留，terminal_batch/deprojectified 是充分性→义务；且 promote() 零生产调用方（只有 tests + scripts/replay_ising_closure.py）；README 档三『KB org 晋升三查』→呈裁 |
| `core/state.py:1584` | downgrade | claim 状态机禁 refuted/superseded 回头、禁 validated 直回 open：转换连同 reasoning/evidence/by_user 都进 review_history，账不会假；『哪些转换合法』是科学判断（新证据可翻案）归 S3；降格=放行并在 review_history 标 unusual_transition，referee 终审 |
| `core/tool_registry.py:634` | downgrade | 事后见证的『调用前快照』(git rev-parse/status) 崩了就拒绝派发任何工具=整条 run 因见证器故障而死；写边界的墙在 spawn（沙箱），此处只是见证；降格=照跑工具，结果附 workspace_witness_failed + transcript 事件。反方论点见 summary 呈裁 |
| `core/whiteboard.py:97` | delete | content='' 就是『擦板』，物理可行、账不假、无资源风险；『别交白卷』是偏好；schema 已 required content，框架合成调用（content=None）另有分支不受影响；放行=写空板（render 返 None） |
| `nodes/data/tools/mesh_generator.py:6057` | downgrade | 「原始 CAD 是物体不是流体域，直接剖会出错网格」是预测失败/质量判决；gmsh 物理上能剖；降格=照剖并挂 geometry_representation_unknown 义务，显式声明可消除 |
| `nodes/data/tools/mesh_iteration_advisor.py:1514` | downgrade | 「像公开 benchmark 且没写 reference_notes 就必须先去检索」是顺序仪式（参数已 resolved 也拦）；降格=用 resolved/默认参数继续并挂 public_benchmark_params_unverified 义务 |
| `nodes/data/tools/preprocessing_planner.py:7100` | downgrade | 「能力超出 task_scope 声明」是预注册范围判决；S4 偏离须申报而非不可能：记 scope_deviation 后照走 |
| `nodes/data/tools/preprocessing_planner.py:7134` | downgrade | 账本里一次 plan_contract_error/execution_error 就永久停管线且无解除路径（gap_id 稳定）；降格=允许重试并计次，超次再走 A 类熔断 |
| `nodes/data/tools/preprocessing_planner.py:7153` | downgrade | 「证据未验证但 plan 没有检索步」拒绝执行；S2 照跑并挂 reference_evidence_unverified 义务 |
| `nodes/data/tools/preprocessing_planner.py:7268` | downgrade | plan 声明本地生成几何、步骤想改外部获取被拒（不许改道）；S4 申报偏离后由 agent 选路 |
| `nodes/data/tools/preprocessing_planner.py:7290` | downgrade | 同 7100 的范围判决第二份抄件；合并为一处并降格 |
| `nodes/experiment/tools/operation_completion.py:156` | downgrade | python_install 缺 package_name/import_name 就拒记收据；同函数 O6 先例已把缺证据降为 passed=False check，此处应一致 |
| `nodes/experiment/tools/resource_manager.py:1908` | downgrade | job 命令含 nohup/&/disown/裸 sbatch 被拒：预测「会孤儿化」；作业脚本返回时 cgroup 会收掉子进程，不可逆损害不成立；降格=照提交并在 submission 记录挂 unmanaged_background_launch 见证 |
| `nodes/experiment/tools/safe_bash.py:1855` | downgrade | 可执行目标不存在就整条命令不跑：宪法档一「预测失败」；让 bash 用 rc=127 拒绝，降格=rc 126/127 时把同一诊断附在结果上 |
| `nodes/experiment/tools/safe_bash.py:1868` | downgrade | 目标无执行位事前拦截：同上，rc=126 现实拒绝后附诊断 |
| `nodes/experiment/tools/safe_bash.py:3631` | downgrade | nohup/&/disown 拒绝：强制容器里 shell 退出即整树被 PID1 收掉，放行无不可逆损害；降格=照跑+见证 background_launch_dies_with_shell 提示走 submit_job |
| `nodes/experiment/tools/safe_bash.py:3650` | delete | 裸 srun 拒绝：Attempt 沙盒 network=none 根本连不上 slurmctld，现实自己拒绝；纯预测墙 |
| `nodes/experiment/tools/sediment.py:161` | downgrade | experiment_log_id 必须等于「当前最新」：给了真实但非最新的 id 就拒；降格=挂到点名的 log 上并见证 not_latest |
| `nodes/experiment/tools/sediment.py:648` | downgrade | 同 161 |
| `nodes/experiment/tools/timeout_escalation.py:247` | downgrade | 声明 expected_duration_s≥600 就必须走 submit_job 是路线仪式；check_after 分支已死（registry 先 pop 保留参数）；降格=在 timeout/沙盒 walltime 下照跑并见证 managed_submission_recommended |
| `nodes/literature/tools/archive_papers.py:382` | delete | 非空闸：零篇论文的 literature_index 是如实的空结果（S2），放行后账不假；与已删的 research_state:176 同型 |
| `nodes/literature/tools/classify_papers.py:87` | delete | 「至少 2 篇」数量下限：n=1 聚类可计算（单主题），阈值任意；上一轮判 C，二审改 D（呈裁） |
| `nodes/literature/tools/finalize_evidence_package.py:38` | downgrade | evidence_summary 非空闸：同函数 included_papers 空已降为 advisories（L-OB1），summary 空同样如实记 advisory 随包走，不拒收尾 |
| `nodes/postprocess/tools/figure.py:272` | downgrade | caption 与 alt_text 非空闸：探索路径（S5）要求每张图先写 alt_text 是仪式；空值照录、findings 记 OB-CAPTION，发表路径由消费端义务应答——重建后新长出的 D |
| `nodes/writing/hooks.py:418` | downgrade | input_audit_gate 已 enforced 就拒绝自动收尾整个工程：函数下游本已支持 status_is_blocked 分支（引用清零+preflight_status 如实写 blocked_*+blocking_items 进 metadata），放行后账不假；拒绝的后果是模型提前停时工程永远不成 manuscript，run 零交付物 |
| `nodes/writing/tools/compact_delivery.py:617` | delete | 「至少五个 claim id」数量下限：_section_sources 自动补齐到 6 个占位，任何数量都能生成；fixture-only |
| `nodes/writing/tools/material_gap_delivery.py:458` | delete | 「至少五个 claim id」：_claim_trace_lines 任意数量可用；fixture-only |
| `nodes/writing/tools/material_gap_delivery.py:486` | downgrade | 「无上游产物」非空闸：材料缺口报告对零输入同样如实可写（把「无上游产物」本身记进 material_gaps）；fixture-only |
| `shared/lib/kb_schema.py:465` | downgrade | 「翻 validated/refuted 必须 ≥1 证据」是充分性判决：evidence_ids=[] 如实入 review_history 账不假；一审已判降格→O2 未执行；同一函数 update_claim_status 里 authority/prereg 两道已改「降落 provisional」，唯此仍硬拒——同函数两种形态。 |
| `shared/lib/kb_schema.py:474` | downgrade | 「hypothesis 证据必须含 chunk/experiment 不能只引 claim」=证据强度判决；如实标 verdict_evidence:claims_only 进 KB 与局限节（O2），referee 终审；一审已判未执行。 |
| `shared/tools/builtin.py:1000` | downgrade | _looks_stalling 关键词表（等待/wait/hold）替模型判「推荐项算不算推进」=硬编码审美阈值，「wait for job then analyze」会误伤；一审已判降格→O10 stalling 信号未执行。 |
| `shared/tools/builtin.py:1637` | delete | edit_file「先 read 再 edit」是顺序仪式：old_string 精确唯一匹配（1657/1662）已机械证明模型知道文件内容，盲改不可能发生；与 write_file 1576 同规则第二抄件，但 write 是整体覆盖 edit 是有界替换，理由不同，此处删。 |
| `shared/tools/builtin.py:237` | downgrade | 「正文引用了 KB 里不存在的 claim id 即拒写」是充分性判决：放行后 KB 一字不变、产物如实带 phantom_claim_ids 进 metadata+引用完整性义务（宪法 S2 例句「引用清单不一致记录即可」）；呈裁：B 论点=幽灵引用近似伪造出处，但检测结果本身即可持久化，判决不必。 |
| `shared/tools/builtin.py:312` | downgrade | 「架构节点不得新建 producing 节点专属类型」是资格/流程判决（S3）：放行后 produced_by_node_type 由框架如实盖章账不假，义务「产物由非属主产出」呈 referee（O1，一审已判未执行）；唯一 B 子项 review_critique 已由 artifact_capabilities.TYPED_ONLY_ARTIFACT_OWNERS 单独守住，此处对它是重复抄件。 |
| `shared/tools/builtin.py:645` | downgrade | shell_probe_only「协调者只许只读探查」是角色/资格判决（S3）：写边界已由 core/sandbox 在 spawn 层守住（只可写本节点目录），剩下的是「不许替 producing 节点干活」的流程规则；放行+probe_only_shell_violation 见证+义务呈 referee；呈裁：pip install 改共享环境有弱 A 味。 |
| `shared/tools/builtin.py:957` | downgrade | 「有正式 decision package 等人时不许 request_human_input」是顺序闸：提问本身不改 package 状态账不假；放行并把 pending_decisions 附进 pause_event/见证，bypass 下 REVISE→PROCEED 的风险由 package 自身 awaiting_human 状态守住而非拒绝提问。 |
| `shared/tools/library/artifacts_extra.py:579` | downgrade | 顺序闸：review→curator→decision 未闭合不许 freeze；冻结件 metadata 机械写入 review_state=open 即账真，amendment 链仍在；round one 呈裁未落（前置：先把未闭合 review_state 写进冻结件） |
| `shared/tools/library/artifacts_extra.py:729` | downgrade | 预注册可行性（预测未来偏离，档一）+闭合条件/capital_basis/数值自洽（真探测）+run_role 声明合集；S4 申报式偏离已是合法路径，违规应写 metadata.freeze_warnings+义务；仅 run_role/expected_params 子句是契约形（呈裁） |
| `shared/tools/library/kb.py:191` | downgrade | hypothesis claim 只许 _curator 写=角色事前审批（S3）；created_by_node_type 已如实入账；原动机（provisional 假说无修订语义）已被 hypothesis_id 原地更新解决；round one O1 未落 |
| `shared/tools/library/kb.py:2059` | downgrade | 「不够格起草知识卡」（文献转述/自产 empirical 无实验关联）是充分性判决；round one O8：文献转述子句删、实验关联降格为卡上 source_warnings 供晋升人批 |
| `shared/tools/library/kb.py:2074` | downgrade | dead_end 卡缺 trigger：无效不是造假；标 dormant/missing_trigger 进人批清单更有信息量（round one O8） |
| `shared/tools/library/kb.py:2084` | downgrade | check_deprojectified 失败（缺字段/域不在注册表/项目指代/泄漏维度）：真探测但拦错位置——org 污染关口在晋升人批，promotion_scan 已把不过的列进 blocked；草稿照落+deprojectified:false+reasons（round one O8） |
| `shared/tools/library/kb.py:2100` | downgrade | 草稿位配额 12 是任意阈值；真实约束在 briefing 注入窗口，应在注入侧按预算截断，起草侧只记 over_budget；round one 判保留（注意力预算 A），本轮改判，见呈裁 |
| `shared/tools/library/kb.py:285` | downgrade | 来源 artifact 未 freeze 不许立 hypothesis=顺序闸；chunk 已带 origin_artifact_frozen/version/content_hash 如实钉死（同 round one 286）；降格为 claim 标 prereg_frozen:false + 冻结义务 |
| `shared/tools/library/kb.py:591` | merge | 本 run 声明 infeasible 不许翻 validated/refuted：与紧随其后两道已降格闸同族（裁决资格），应并入同一「降落 provisional + authority_note」路径而非拒绝 |
| `shared/tools/library/kb.py:686` | downgrade | 同 session 翻 status ≥3 次拒绝=裸计数器（round one O3：thrash_signal 只记不拦，未落地）；第 4 次翻转可能正是新证据，review_history append-only 已留痕 |
| `shared/tools/library/proposals.py:244` | downgrade | 被拒提议不许原样重提除非 new_evidence 非空=仪式闸（任意非空即过）；round one O3 thrash_signal 只记不拦；prior_rejected_count 挂上让人裁（A 熔断论点见呈裁） |
| `shared/tools/library/skill_tools.py:670` | downgrade | 必须给 source_entries 才能提 skill=提案前置证据审批（S3，下一步就是人审 inbox）；round one O9：evidence_strength:none 钉在提案上 |
| `shared/tools/library/skill_tools.py:675` | downgrade | 无 worktree 就拒（事实源不在场当判决）；S2：记 evidence_verified:false 进提案 |
| `shared/tools/library/skill_tools.py:688` | downgrade | 复发证据不足（≥2 条或计数≥2）是充分性判决；round one O9 evidence_strength 钉在提案上给人审 |
| `shared/tools/library/skill_tools.py:698` | downgrade | body 引用不存在的工具名：真探测；unknown_tools 钉在提案上人审一眼可见，比拒绝更强（round one O9） |
| `shared/tools/library/tasks.py:113` | downgrade | 人工决定 revise/retry/redirect 未闭合不许标 task complete：真正的执行闸在 run_node 派发前查 pending_post_node_flow；task 清单是私账，标 complete 时记 completed_against_open_decision 让矛盾可见即可（B 论点见呈裁） |
| `shared/tools/run_node.py:1217` | downgrade | 范畴对不上须先带 modality_rationale=充分性/申报闸；docstring 自认「判断归模型」；一审判收编→O4 modality_deviation 未执行：改派发照跑+理由（或缺理由）如实入永久账本，免一次往返。 |
| `shared/tools/run_node.py:1312` | downgrade | data 阶段欠货须先带 dataset_waiver_reason=同形申报闸；一审判收编→O4 stage_debt 未执行。 |
| `shared/tools/run_node.py:1690` | downgrade | writing-gate「还没有 project_synthesis 不能起 writing」=顺序闸，fire_data 28 次命中/单 run 连撞 24 次=骚扰墙；一审判降格→O5 未执行；「从未做过综合评估」写进稿件局限节即可。 |
| `shared/tools/run_node.py:1725` | downgrade | writing-gate「verdict≠ready_to_write 不能起 writing」=事前审批（S3）；30 行外已有降格形态（conservative_with_limitations 注入）→ override 分支变默认分支，整个 writing_gate 模块与 present_writing_gate_override 随之退场。 |
| `shared/tools/run_node.py:1839` | downgrade | 「服务节点不许 background」：一审改判 C（无交货口）但 _report_background_done→_inject_to_parent 确有送达口，故非物理不可；实为「你会拿不到货」预测判决；放行+blocking 义务「服务结果未到不得定稿」进 obligations，结果到达即消；呈裁。 |
| `shared/tools/run_node.py:2005` | downgrade | legacy 分支「缺必需 artifact 类型不能起子节点」=预测失败+缺 Y 所以拒；v2 分支对同一问题明写「零个→不拦，节点如实产降级产物」——同一问题两答案；一审判降格→O4 missing_inputs 未执行。 |

## 3. X 类全量（64）

| 位置 | 动作 | 理由 |
|---|---|---|
| `core/memory_migrate.py:82` | delete | migrate() 仅 scripts/replay_memory_rebuild.py 调用，core/shared/platform 零调用方；检查本身是 C（没绑 worktree 就没有落点），但整个模块在生产不在场 |
| `core/sandbox.py:1377` | merge | parse_manifest 先查 mount 存在，紧接着 manifest.validate() 在 304 对同一 mount 再查一遍；同一对象同一条件两份，留 304 删此处 |
| `core/sandbox.py:1952` | merge | 与 1865 同一条件：prepare_attempt_command 在此判 ceiling.admits 后，2006 行 _ensure_allocation 对同一 applied_limits 再判一次；留 1865 删此处 |
| `core/state.py:849` | delete | update_memory_lifecycle 除 tests 外零调用方（记忆系统四层重建后 memory.jsonl 生命周期已退场）；整函数可删 |
| `nodes/data/tools/web_search.py:346` | merge | 绝对路径/.. 成员检查被 352 的 resolve 包含性检查完全覆盖（都在写盘前），重复 |
| `nodes/experiment/tools/operation_completion.py:132` | delete | _request_mode 路由不变量与注册表/分诊重复（呈裁②曾留作防御断言）；见 summary 呈裁 |
| `nodes/experiment/tools/resource_fetch.py:286` | delete | node_type 断言与注册表 allowed_node_types 重复（呈裁②曾留作防御断言）；见 summary 呈裁 |
| `nodes/experiment/tools/resource_fetch.py:325` | delete | destination 已存在的检查与 237/248 重复（237/248 才是原子导入处）；此处只是省一次下载的预检 |
| `nodes/experiment/tools/resource_fetch.py:76` | delete | fragment 本就不发给服务器，拒绝不保护任何东西；框架机械剥掉即可 |
| `nodes/experiment/tools/resource_manager.py:1446` | delete | 死分支：唯一调用方 _submit_job 传 highrisk_authorized=not dry_run，条件永假；连带 _reject_high_risk 可删 |
| `nodes/experiment/tools/resource_manager.py:1536` | delete | unsupported scheduler 在唯一调用路径上已由 1825 拒过，同条件二次拒绝 |
| `nodes/experiment/tools/resource_manager.py:1975` | delete | 死分支：stage 已在 1820 限定为 diagnostic/toolchain_build/simulation，三者都在 preflight._STAGE_ALIASES 里，stage_invalid 不可达 |
| `nodes/literature/tools/classify_papers.py:228` | delete | 不可达：assignments 由 for i in range(n) 构造且每项 `ids or [1]`，len==n 且非空恒成立 |
| `nodes/postprocess/contracts.py:118` | delete | require_list 全仓零调用方（含 tests） |
| `nodes/postprocess/contracts.py:124` | delete | require_nonempty_text 全仓零调用方（含 tests） |
| `nodes/postprocess/tools/figure.py:511` | delete | 与注册表 required_runtime_capability='model_role:visual_review'（has_runtime_capability→model_roles.available）同条件重复；经 execute() 到不了这里 |
| `nodes/postprocess/vlm_witness.py:121` | delete | domain_rubrics 参数全仓无调用方传入（figure.py 两处均不传）；死分支连同参数一起删 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:1108` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——argparse 已限定的子命令再判一次（不可达）；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:160` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——mirror_archive_url 镜像名 enum；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:170` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——mirror_raw_url 镜像名 enum；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:178` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——iter_mirrors 镜像名 enum；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:247` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——GitHub API HTTPError 转述；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:249` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——GitHub API URLError 转述；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:258` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——下载 HTTPError 转述；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:260` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——下载 URLError 转述；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:275` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——package.json 缺失；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:345` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——本地路径定位不到包目录；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:358` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——local- 缓存版本不存在；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:381` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——缓存 metadata.json 不存在；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:401` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——zip 快照无仓库根目录；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:404` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——zip 快照缺包目录；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:430` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——镜像快照缺包目录；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:435` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——所有镜像下载失败；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:674` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——source=local 未给 --path；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:828` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——pin 无激活版本且无 --ref；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:848` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——锁文件不存在；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:856` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——锁文件缺 commit；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:900` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——rollback 无上一版本；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/project_templates/chineseresearchlatex/ChineseResearchLaTeX-main/packages/bensz-nsfc/scripts/install.py:902` | delete | vendored 第三方（huangwb8/ChineseResearchLaTeX，MIT；license.txt+THIRD_PARTY_NOTICES）且上游自标 deprecated；仓库零调用方（sci_project 只拷 .sty/profiles/license.txt，不执行 scripts/）——上一版本缓存不存在；整段 scripts/ 应从 vendored 拷贝移除或把 project_templates/** 排除出扫盘基线 |
| `nodes/writing/tools/material_gap_delivery.py:466` | delete | 不可达：claim_ids 取自 state.list_kb('claims')，claim_records 亦取自同一 list_kb，差集恒空 |
| `shared/lib/kb_schema.py:448` | delete | 不是拒绝点：validate_status_flip 的 docstring 里一句「校验失败 raise SchemaValidationError」被扫描器正则误计；修 scan_refusal_sites 跳过注释/docstring。 |
| `shared/lib/kb_schema.py:855` | delete | 不是拒绝点：validate_record 的 docstring「校验失败 raise SchemaValidationError」被扫描器误计（同 448）。 |
| `shared/lib/publication_figures.py:100` | delete | 「metadata 出现 status/quality_mode/verdict 键即拒」是防复发闸放错层：应在测试/AST 扫盘钉住铸造侧，不该在运行时因一个键名拒掉整张图（metadata 恰有 status 键的合法记录会被误伤）；无机器消费者读这些键，放行账不假。 |
| `shared/tools/library/artifact_intake.py:102` | delete | 「只给 orchestrator」已由 harness 白名单承担（import_artifact 只出现在 _orchestrator/harness.yaml），且可写成 ToolDefinition.allowed_node_types；出处真伪由 121/134 位置即来源守住，本处是角色审批抄件 |
| `shared/tools/library/audit.py:64` | delete | enum 校验之后不可达的 internal unhandled 尾巴 |
| `shared/tools/library/kb.py:1279` | delete | _propose_org_promotion 工具面已撤（2026-08-21），唯一调用方 _find_org_promotion_candidates 恒传非空 reasoning，检查不可达 |
| `shared/tools/library/kb.py:1286` | delete | 晋升扫盘只产 project claim 候选，「已是 org」分支不可达，且工具面已撤 |
| `shared/tools/library/kb.py:1931` | delete | enum 校验之后不可达的 unhandled 尾巴 |
| `shared/tools/library/runtime_control.py:116` | merge | reason 非空已在 dispatcher 431 查过 |
| `shared/tools/library/runtime_control.py:248` | merge | child_run_id 非空已在 dispatcher 413 查过 |
| `shared/tools/library/runtime_control.py:428` | merge | 与 413 同一条件（child_run_id 缺）第三次手写；合成一处「action∉{list_active,jobs} → child_run_id 必填」 |
| `shared/tools/library/runtime_control.py:437` | delete | enum 校验之后不可达的 unhandled 尾巴 |
| `shared/tools/library/runtime_control.py:80` | merge | content 非空已在 dispatcher 419 查过（inject 只经 runtime_control 到达） |
| `shared/tools/library/skill_tools.py:392` | merge | core.skill_usage.record_usage 只对 outcome 再抛同一枚举错，381 已查；留一处 |
| `shared/tools/library/skill_tools.py:556` | delete | enum 校验之后不可达的 unhandled 尾巴 |
| `shared/tools/library/tasks.py:140` | merge | task_id 缺，同 89 |
| `shared/tools/library/tasks.py:142` | merge | blocked_reason 非空 core/tasks.block 已查（TaskListError 经 178 转述）；工具层重复 |
| `shared/tools/library/tasks.py:149` | merge | task_id 缺，同 89 |
| `shared/tools/library/tasks.py:171` | merge | task_id 缺，同 89 |
| `shared/tools/library/tasks.py:183` | delete | enum 校验之后不可达的 unhandled 尾巴 |
| `shared/tools/library/tasks.py:78` | delete | 空 title 拒绝：core/tasks.create 已按 round one（tasks:154）改为如实记「(未命名)」，工具层这道抄件把删掉的墙重新立起来 |
| `shared/tools/library/tasks.py:95` | merge | 与 89 同一条件（task_id 缺）重复手写 |
| `shared/tools/run_node.py:1470` | merge | node_type='review' 别名提示：load_harness('review') 在 1780 本就 FileNotFoundError，同一事实两处各答；把别名指路并进 1780 的报错。 |
| `shared/tools/run_node.py:3593` | delete | forward_artifact 工具整体是死码：写进 hook_state.forwarded_artifacts 的登记表全仓零生产读者（只有 tests/test_run_node.py:230 断言它存在），run_node(forward_artifact_ids) 才是真机制；删工具+_orchestrator 白名单行+测试。 |

## 4. C 类可由注册表 schema 校验整批替代（176，按文件）

前置：在 `_execute_dispatch` 加一次 jsonschema 校验（required/enum/min/max/pattern/oneOf），报错自动列合法值。
没有这个前置就删手写检查 = 静默放行畸形参数。

| 文件 | 点数 |
|---|---|
| `shared/lib/kb_schema.py` | 25 |
| `nodes/experiment/tools/resource_manager.py` | 16 |
| `shared/tools/library/kb.py` | 11 |
| `shared/tools/run_node.py` | 11 |
| `nodes/_reviewer/tools/critique_builder.py` | 11 |
| `shared/tools/builtin.py` | 9 |
| `shared/lib/publication_figures.py` | 7 |
| `nodes/experiment/tools/resource_fetch.py` | 7 |
| `shared/tools/library/skill_tools.py` | 5 |
| `nodes/postprocess/tools/figure.py` | 5 |
| `shared/tools/library/profile_tools.py` | 4 |
| `shared/tools/library/proposals.py` | 4 |
| `nodes/data/tools/preprocessing_capabilities.py` | 4 |
| `shared/tools/library/cross_model.py` | 3 |
| `shared/tools/library/job_registry.py` | 3 |
| `shared/tools/web.py` | 3 |
| `nodes/experiment/tools/safe_bash.py` | 3 |
| `nodes/writing/tools/submission.py` | 3 |
| `shared/tools/library/audit.py` | 2 |
| `shared/tools/library/blockers.py` | 2 |
| `shared/tools/library/derivation_check.py` | 2 |
| `shared/tools/library/runtime_control.py` | 2 |
| `shared/tools/library/tasks.py` | 2 |
| `nodes/data/tools/preprocessing_planner.py` | 2 |
| `nodes/experiment/tools/run_contract.py` | 2 |
| `nodes/hypothesis/tools/hif_scorer.py` | 2 |
| `nodes/hypothesis/tools/hypothesis_evolve.py` | 2 |
| `nodes/postprocess/contracts.py` | 2 |
| `nodes/writing/tools/sci_project.py` | 2 |
| `shared/tools/library/concede.py` | 1 |
| `shared/tools/library/producer_transcript.py` | 1 |
| `shared/tools/library/python_exec.py` | 1 |
| `shared/tools/papers.py` | 1 |
| `nodes/data/tools/atomic_structure_recovery.py` | 1 |
| `nodes/data/tools/coordinate_profile.py` | 1 |
| `nodes/data/tools/execute_preprocessing_plan.py` | 1 |
| `nodes/data/tools/mesh_generator.py` | 1 |
| `nodes/data/tools/scientific_mesh.py` | 1 |
| `nodes/data/tools/scientific_preprocessor.py` | 1 |
| `nodes/data/tools/web_search.py` | 1 |
| `nodes/experiment/tools/operation_completion.py` | 1 |
| `nodes/experiment/tools/sediment.py` | 1 |
| `nodes/hypothesis/tools/artifact_save.py` | 1 |
| `nodes/hypothesis/tools/conclusion_audit.py` | 1 |
| `nodes/hypothesis/tools/hypothesis_cluster.py` | 1 |
| `nodes/hypothesis/tools/output_validator.py` | 1 |
| `nodes/hypothesis/tools/paper_reader.py` | 1 |
| `nodes/literature/tools/search_papers.py` | 1 |
| `nodes/writing/tools/venue.py` | 1 |

## 5. 整块可删的实体（跨五路汇总）

| 实体 | 调用方 | 删了谁受影响 |
|---|---|---|
| **writing-gate 整套**：run_node 1590-1760 块 + `writing_gate.py` + `present_writing_gate_override` + pause_driver 分支 + harness.yaml 两处 + writing 两个框架注入输入 | run_node 派发口、chat.py 两处、orchestrator/writing harness.yaml | orchestrator 少一堵 28 次命中的墙；「从未做综合评估」进稿件局限节（O5）；6 个测试改写 |
| `forward_artifact` 工具 | orchestrator harness.yaml 白名单 | 无（run_node(forward_artifact_ids) 才是真机制） |
| run_node legacy 转发分支 1984-2030（含 2005 墙） | 仅 CLI 裸跑/fixture | tests 里走 legacy 的用例改绑 worktree（呈裁） |
| `safe_bash._exec_preflight_bash` + `timeout_escalation.managed_submission_requirement/uses_srun` + 裸 srun 块 | 仅 `_safe_run_bash` | rc 126/127 时附同一诊断 |
| `resource_manager._submit_sync` 高危死分支 + `_reject_high_risk` | 条件永假 | 无 |
| `resource_manager.preflight_build_resources` 缩成纯咨询 | harness.yaml:313/468、hpc-build skill | 三个测试改断言 |
| tasks.py 96-134（#183 执行一致性 gate）+ `blocking_decision_for_task` | 仅 task 工具 | 无（run_node pending_post_node_flow 才是执行闸） |
| `bensz-nsfc/scripts/install.py` + 同目录 `package/` | 无 | 无 |
| `contracts.require_list/require_nonempty_text`、`vlm_witness.domain_rubrics` 参数、classify_papers 226-228、material_gap 463-470 | 无/不可达 | 无 |
| `core/memory_migrate.py`、`State.update_memory_lifecycle` | replay 脚本 / tests | owner 确认旧布局不再迁移 |
| kb_schema 四个 validate_* + `_validate_common` → 一份声明 | state.py:1103、sediment.py:110 | 无（同函数不分叉） |

## 6. 呈裁（两边论点都写在五份 summary 里，此处只列题）

wangd/框架：`tool_registry:634`（见证器崩了拒派发所有工具，须配对称改动 after 端报 witness_unavailable）；
`builtin:1576` write_file「先读再覆盖」（A 保留但真 A 机制是覆盖前快照，落快照后拆墙）；`builtin:645` shell_probe_only；
`builtin:957` decision package 等人时禁提问；`state:1584` claim 状态机禁回头；`kb:2100` 草稿配额搬到注入侧；
`figure:282` 生成式像素「拒绝录入」措辞 vs 覆写+记 finding（动不可压缩核措辞）；扫描器扩形态。
owner：`kb_promotion:487`/`domain_registry:573`（org 晋升三查与人批，README 档三治理项）；`kb:173/263`；`artifacts_extra:579/729`；
`run_node:1516/1839`；`mesh_generator:6057`；`sediment:656`；`rm:1839/1995`；`te:406` 熔断阈值 1；`pp:9659` 关键词判据；
`hooks.py:418`；`classify_papers:87`；`podsys_safe_source.py` 归属。

## 7. 第三波（若拍板）

1. **一个校验器替 176 个实体**：注册表派发口 jsonschema 校验 → 删 176 处手写检查 + kb_schema 28→1 + 五份枚举→1 常量。
2. **一审判了没落的 ≈20 处 D 全部落地**（writing-gate 整套先删）+ figure:272 + vlm 三处。
3. **X 64 清扫** + 假文案 15 处 + podsys 呈 qinp。
4. **扫描器补形态**（docstring 跳过、`_error(`/`_err(`/tuple 返回、专用异常类），让棘轮看见真分母，基线随之重生。
5. 呈裁 ≈25 条逐条定案后再动。
