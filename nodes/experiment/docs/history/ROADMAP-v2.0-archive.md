# 历史归档：Experiment v2.0 路线图（已废止）

> 当前有效路线图：nodes/experiment/ROADMAP.md。本文仅保留历史记录，不得作为实现依据。

AI4S Experiment Node — 整体方案路线图（ROADMAP）
全节点交互流程图（突出 experiment）
experiment I/O 与证据层

科研工作流节点层

框架级控制 / 治理层

pre_registration artifact

claim_type='hypothesis' in KB

dataset / inputs

调度 workflow 节点

调度 workflow 节点

调度 workflow 节点

调度 workflow 节点

调度 workflow 节点

调度 workflow 节点

判不动 / prereg 需改 / 数据缺失

redirect_upstream

补数据或前处理

经验 / dead_end / best practice

历史失败与方法经验

项目记忆

不做实验设计

不做正式统计/绘图

不做项目级综合/论文判断

_orchestrator
调度 / redirect_upstream

_reviewer
质量评审 / project_synthesis

_curator
KB / memory consolidation

literature
文献与方法依据

hypothesis
假设 / 变量 / prereg

data
数据与前处理

experiment
执行 / HPC build-run / 诊断 / verdict

postprocess
数值提取 / 统计 / 图表

writing
论文写作

输入
frozen prereg / dataset
KB: open hypothesis claim

内部控制证据
build_env / platform_profile / source_recon
build_graph / build_state

主输出与证据
frozen experiment_log / Verdict / Credibility
KB updates / environment_snapshot / repro_bundle





本文件以 stage4:experiment 完整需求 为骨架，按 Phase 组织实现计划。 每个 Phase 完成后更新状态。跟 WORKLOG.md（做了什么）互补。 需求来源：框架文档网站 https://desktop-9el2944.taile9f15e.ts.net:8443/index.html + stage4 需求表

实现方向原则（2026-06-09 更新）：

工具/SKILL 文件数量最少化；优先用 harness.yaml rules + SKILL.md 知识库，而非独立 .py 工具文件
单个 .py 工具只在 SKILL/rules 无法满足时才加
v2.0 框架变更：analysis 节点已取消，per-experiment verdict 职责并入 experiment 节点
需求全景
experiment 节点需覆盖 7 大一级需求，共 36 项二级需求。以下按优先级/依赖关系分配到各 Phase。

一级需求	二级需求数	覆盖 Phase
① 实验设计与方案规划	7	大部分归 hypothesis 节点；experiment 只接收 prereg
② 多学科科研工具支持及使用辅助	4	Phase 1（基础）+ Phase 2（扩展）
③ 计算与实验流程自动化执行	8	Phase 1（基础）+ Phase 5（完整）
④ 算力资源灵活调用	4	Phase 3（资源发现/提交）
⑤ HPC 软硬件适配移植	4	Phase 3（GPU 编译/依赖/调优）
⑥ 异常诊断与失败恢复	4	Phase 4（诊断工具 + KB 沉淀）
⑦ 实验版本管理与可复现性	8	Phase 6 + 框架已有机制
Phase 0：验证节点基础框架 ✅ 已完成
证明 experiment 节点能在框架中正确跑通 E2E 流程。

验证结果
框架 v1.7 starter skeleton 到位
E2E 用 LAMMPS NVT 验证基本工作流
发现并记录 hooks.py 导入 bug（register_loop_hook() + 顶层导出必须同时存在）
Phase 1：通用实验执行节点 ✅ 已完成（2026-05-28）
从 LAMMPS 骨架改为通用实验执行节点，以 WRF 为 minimal verification target。 覆盖需求 ②（基础）+ ③（基础）。

交付物
文件	状态	说明
harness.yaml	✅	通用 4 阶段 system_prompt；rules ≥10 条；tools 白名单
hooks.py	✅	wrf_failure_detector（14 种错误）+ deviation_detector
fixtures/minimal.yaml	✅	WRF 12km 24h CONUS baseline
fixtures/failure.yaml	✅	CFL violation 场景
fixtures/deviation.yaml	✅	执行参数偏离 prereg 场景
review_spec.md	✅	6 维评审 rubrics + WRF 领域红线
README.md	✅	完整 I/O 契约、工作流、文件结构
覆盖的需求
需求	实现方式
②.3 运行过程辅助	hooks.py failure_detector
②.4 结果初检	harness.yaml rules + hooks
③.5 关键决策点确认	harness.yaml rules + request_human_input
③.6 过程监控与主动预警	hooks.py 监控 CFL/rsl.error
③.7 实验结构化归档	框架 artifact/freeze 机制
⑦.1 数据结构化归档	harness.yaml 要求记录代码版本/参数/环境
Phase 2：多工具通用抽象 ✅ 已完成（2026-05-28）
从 WRF 单一工具扩展到支持多学科 HPC 工具的通用抽象层。 覆盖需求 ② 完整实现。

设计原则：5 种抽象计算模式适配任意工具，不为每个工具写独立 skill。

交付物
文件	状态	说明
tools/discover_tools.py	✅	扫描 20+ HPC 工具 + Python 科学栈 + GPU/硬件
tools/suggest_config.py	✅	按工具类别生成输入配置模板（5 种模式/15+ 场景）
skills/compute_tool_protocol/SKILL.md	✅	通用执行 skill：5 种抽象计算模式 + 通用错误诊断
harness.yaml	✅	更新至 Phase 2：通用化 skills/tools/rules
5 种抽象计算模式
模式	适配工具
serial_binary	单进程（Gaussian, GAMESS）
mpi_binary	MPI（LAMMPS, GROMACS, VASP, WRF, OpenFOAM）
mpi_preprocess_binary	多阶段（WRF real→wrf, AMBER）
python_training	AI/ML（PyTorch, TensorFlow, JAX）
custom_script	用户自定义
Phase 3：HPC 软硬件适配 + 算力资源 ⚠️ 部分完成
覆盖需求 ⑤（HPC 软硬件适配）+ ④（算力资源调用）。

⚠️ 2026-06-10 审计结论：本 Phase 与 Phase 4 的 ✅ 标记代表"机制已实现"， 但 E3SM run 1780910300-3282c5 审计（272 turns）证明在高难任务下这些机制未生效： 节点 0 次查 SKILL.md、0 次 grep FermiLink、0 次 web_search、1/360 次 diagnose、 0 次 request_human_input，91% 调用是裸 run_bash。根因是 rule 无强制力 + hooks 当时存在 _prev_tool_call_records bug。已修复方向：enforcement 下沉到 hook 层（分级错误计数、节奏复盘、task briefing），见 WORKLOG 2026-06-10。 各 ✅ 应理解为"已实现，有效性待 E3SM 复跑验证"。

实现策略调整（2026-06-09）：原计划写多个独立 .py 工具文件。 实际采用"SKILL.md + harness.yaml rules"方式，更符合最少文件原则，已证明足够有效。

⑤ HPC 软硬件适配
需求	状态	实现方式
⑤.1 编译环境适配	✅	tools/env_provision.py（agent 可编辑 build_env + framework probe/coherence/fingerprint）+ skills/gpu-hpc-porting/SKILL.md + harness rules（CUDA 环境检测、GPU 架构→版本映射）
⑤.2 依赖库移植	⚠️ 部分完成	tools/build_graph.py（CMake/真实 Make DAG + 编排边 + 失败回填边）+ tools/build_state.py（图状态/blocked_by/stale）+ tools/diagnose.py + tools/build_contract.py fallback + 官方文档 + FermiLink。递归 mkmf/MOM6 这类跨组件 DAG 仍未能可靠抽取
⑤.3 性能调优建议	✅	harness rules（GPU 编译双路径选择、-arch sm_XX 强制、CUDA-GCC 兼容性矩阵）
⑤.4 正确性验证	✅	harness rules（GPU benchmark 三要素：同体系/同参数/只改加速方式）；LAMMPS GPU 16.6× 已验证
关键交付物：

skills/gpu-hpc-porting/SKILL.md：CUDA 环境检测、GPU 架构映射、双路径编译策略、7 条已知 CUDA/GPU 错误+修复
tools/diagnose.py：五层诊断模型（编译→链接→运行环境→配置→运行时）
tools/env_provision.py：provision-first 环境入口；env/build_env.sh 是 agent 可编辑、框架强校验的工具链意图，profile 和 major build/run 均 source 同一文件
tools/build_graph.py：通用 build DAG extraction；优先从官方构建系统导出 target 图，并合并脚本/CI/recipe/docs 的低置信编排边与失败日志 confirmed edges。已防止 phony-only Makefile 被误标为 extracted；递归 mkmf 子 Makefile / 跨组件顺序抽取仍是下一步
tools/build_state.py：图状态与 stale 传播；每个 DAG node 维护 state、deps、blocked_by、outputs、evidence
tools/build_contract.py：通用结构化 build contract validator/template；现定位为 DAG 不可得时的 fallback，不写具体软件专属逻辑
④ 算力资源灵活调用
需求	状态	说明
④.1 资源发现	✅ SLURM 已真实验收	tools/resource_manager.py::discover_resources 自动探测 local CPU/mem/GPU + SLURM/PBS/Kubernetes CLI 可用性，并保存 resource_profile artifact；SLURM debug 分区真实 E2E 已通过
④.2 智能匹配	✅ SLURM 已真实验收	recommend_resources 按 MPI ranks / CPU / GPU / memory / walltime 选择 local/SLURM/PBS/K8s，输出 warnings 和 resource_recommendation artifact；SLURM 2 tasks / 2min 推荐已用于真实提交
④.3 成本预估	⚠️ 首版，待真实集群验收	recommend_resources(hourly_rate_usd=...) 只在有真实费率时估算资源费用；未知价格标 not_estimated_no_rate，不编造
④.4 跨平台提交	✅ SLURM 已真实验收	submit_job 统一生成 local/SLURM/PBS/Kubernetes 提交脚本，默认 dry_run=true；真实提交前必须 request_human_input，真实提交后用 job_status 查询；SLURM job_id=11 完成且 ExitCode=0:0
④ 当前边界：已实现通用接口，并完成一次真实 SLURM debug 分区端到端提交验收（dry_run 脚本生成 → request_human_input 人工确认 → submit_job 真提交 → job_status 轮询 → frozen experiment_log + job_submission/resource_profile/resource_recommendation artifacts）。PBS/Kubernetes 仍未真实验收。云厂商实例发现/自动报价不做猜测，需用户或 profile 提供真实费率。

Phase 4：异常诊断与失败恢复 + v2.0 Verdict ⚠️ 部分完成
覆盖需求 ⑥（异常诊断与失败恢复）+ v2.0 新增 verdict 职责。

⑥ 异常诊断与失败恢复
需求	状态	实现方式
⑥.1 错误分类	✅	tools/diagnose.py 五层诊断 + diagnose_patterns/ 错误模式库
⑥.2 根因定位	✅	diagnose.analyze_log() + SKILL.md 知识库匹配
⑥.3 自动修复与重跑	⚠️ 已实现未生效	harness rules 强制诊断优先级（SKILL→FermiLink→五层诊断）。2026-06-10 审计：E3SM run 中节点完全未走此阶梯，已加 hook 层强制（repeated_error_detector 分级升级）
⑥.4 失败知识积累	⚠️ 已实现未生效	v2.0 verdict dead_end claim 机制。2026-06-10 审计：sediment 依赖 freeze，长 run 被 kill 前死掉 → 0 沉淀（GCC OpenACC 250 轮教训未进 KB）。已修复：rule 199 要求放弃路线当下写 dead_end + 2026-06-10 人工回填 6 条
实现方式：非原计划的 4 个独立 .py 文件，而是 1 个 diagnose.py + SKILL.md 模式库 + harness 规则强制执行。效果等价，文件数更少。

v2.0 Verdict 职责（新增，框架升级带来）
v2.0 起 analysis 节点取消，experiment 节点接管 per-experiment verdict：

功能	状态	实现方式
拉取 in-flight hypothesis	✅	harness Part B 步骤 16
判 validated/refuted/inconclusive	✅	update_claim_status + reasoning ≥10 字符
写 methodological sediment	✅	create_claim(claim_type='methodological')
写 dead_end sediment	✅	create_claim(claim_type='dead_end', scope='org')
Quality checks 自动验证	✅	5 条 quality_checks（experiment_log_frozen / cited_claim_ids / kb_status_judgments / sediment / verdict_reasoning）
关键 fixture 变化：fixture 必须提供 frozen pre_registration；hypothesis claim 以 KB 中 claim_type='hypothesis' 为准。E2E fixture 可以额外给出 claim_id/seed KB 记录作为测试 hint，但 claim 不应成为 experiment 的 required artifact input。

Phase 5：流程自动化完善 ⚠️ 部分完成
覆盖需求 ③（计算与实验流程自动化）剩余部分。

需求	状态	实现方式
③.1 流程编排（DAG）	✅	框架 orchestrator 职责，非 experiment 节点
③.2 数据流管理	✅	框架 artifact 系统
③.3 断点续跑	✅	run_node.py --resume <run_dir>（原生续跑，~140 行实现）
③.4 并行调度	✅	框架 orchestrator + Phase 3 submit_job（待实现）
③.5 关键决策点确认	✅	Phase 1 已有（hooks + request_human_input）
③.6 过程监控与主动预警	✅	Phase 1 已有（hooks.py failure/deviation detector）
③.7 实验结构化归档	✅	框架 artifact/freeze + harness rules
③.8 运行结束通知	❌	未实现（hooks.on_end 通知机制）
额外实现的 run_node.py 功能（超出原 ROADMAP 计划）：

--stream：实时 transcript 解析输出（阶段标签 + LLM 预览 + tool 调用格式化）
全局 stdin 打断：运行中可随时 inject 消息或 cancel
运行前费用预估门控（_prerun_gate）
运行后 token/费用统计（40+ 模型定价表）
Phase 6：版本管理与可复现性完整实现 ✅ 基础闭环完成（⑦.7 不作为独立出图需求）
覆盖需求 ⑦（实验版本管理与可复现性）。

需求	状态	说明
⑦.1 数据结构化归档	✅	框架 artifact + KB 机制（已有）
⑦.2 产物追溯	✅	框架 artifact lineage
⑦.3 环境复现	✅ 已真实 E2E 验收	tools/repro_snapshot.py + repro_snapshot hook：run 结束自动采集工具链/源码 commit+dirty/submodule/外部依赖/环境变量/硬件 → environment_snapshot artifact + runs/<id>/repro/ 可复现包。真实 run_node.py E2E：1782100284-47f6f4 completed
⑦.4 结果分级与留存	✅ 已真实 E2E 验收	credibility=reliable 的结果自动 promote 到 deliverables/results/ + _results_ledger.jsonl 台账；E2E run 1782100284-47f6f4 中 repro_snapshot_saved 记录 promoted_to=.../deliverables/results
⑦.5 实验组管理	✅	query_experiment_groups()：按 hypothesis(claim_id) 聚合跨 run 实验，比较 verdict 一致性（不一致告警）+ 源码 commit 差异
⑦.6 结果可信度评估	✅ 已真实 E2E 验收	on_end 从 frozen experiment_log 提取 ## Credibility(reliable/questionable/invalid) + ## Verdict → 结构化进 snapshot metadata 和 repro manifest。已修复多 experiment_log 撞名选择问题；E2E run 1782100284-47f6f4 得到 credibility=reliable、verdict=inconclusive
⑦.7 初步统计与可视化	✅ 口径调整	不作为“单纯出图”需求；其真实目标是实验 sanity check / 物理合理性 / 崩溃检测 / credibility 守门，已并入 ⑦.6 ## Credibility、experiment_log 关键诊断量和 postprocess 边界。需要正式统计检验或图表时交给 postprocess
⑦.8 结构化结论生成	⚠️	v2.0 起部分归 experiment verdict（per-experiment），项目级综合仍归 _reviewer(project_synthesis)
实现策略：遵循最少文件原则——⑦.3~⑦.6 主要由 1 个 repro_snapshot.py（采集器 + 提取器 + repro_bundle + promote + 聚合查询）+ 1 个 on_end hook 完成，复用框架 artifact / deliverables / experiments entity。2026-06-22 补充通用 deterministic quality-check mode，用于避免可机械判定项被 LLM judge 误判。 on_end 自动采集呼应 2026-06-10 审计核心教训——可复现这种"该做但 agent 容易忘"的事必须 hook 强制，不靠自觉。 repro_bundle 验收：真实 run_node.py E2E run 1782100284-47f6f4 已完成，7/7 quality checks passed；manifest 含 source repo/submodule dirty、external_dependencies、input/output hash、runtime_config、result_info。 ⑦.7 口径调整：Experiment 节点不承担展示型可视化；只承担最低限度可信度守门（是否跑崩、输出是否完整、关键物理/数值量是否异常）。正式统计检验、绘图和显著性标注归 postprocess。

职责边界汇总（v2.0 更新版）
Experiment 节点该做的
工具发现/配置/运行/诊断/修复（需求 ②⑤⑥）
流程执行基础：监控、预警、决策点确认、归档、通知（需求 ③）
per-experiment verdict：判 hypothesis 状态（validated/refuted/inconclusive）+ 写 sediment（v2.0 新增）
HPC 编译/链接/运行环境适配（需求 ⑤）
版本管理增强（需求 ⑦ 基础部分）
Experiment 节点不该做的（属于其他节点/框架）
实验方法推荐、变量规划、基线选择、参数探索、样本量评估 → hypothesis 节点（需求 ①）
方案文档化（prereg 生成） → hypothesis 节点
方案动态调整 → orchestrator 决策，hypothesis 重出 prereg
流程编排/DAG → framework orchestrator
数据流管理/格式转换 → framework artifact 系统
数值分析/统计检验 → postprocess 节点
项目级综合 verdict（"这堆实验加起来能写论文了吗"）→ _reviewer(project_synthesis)
评审/curation → _reviewer / _curator（框架节点）
已取消的节点（v2.0 变更）
analysis 节点：已取消。per-experiment verdict 职责并入 experiment 节点（Phase 4）。 项目级综合分析由 _reviewer(source_node_type='_project') 负责。



## 五、node4:experiment 功能需求

### 5.1 实验设计与方案规划

| 序号 | 二级需求 | 功能描述 |
|:---:|------|------|
| 1 | 实验方法推荐（承接上游） | 承接之前的假设或任务，根据假设类型推荐验证方法（对照实验/模拟/统计分析）及适用理由 |
| 2 | 变量规划 | 具体到物理量，明确自变量、因变量、控制变量，生成变量关系 |
| 3 | 基线选择 | 根据领域知识 + 文献调研积累，推荐领域内公认的基线方法和数据集，解释选择依据 |
| 4 | 参数探索与论证 | 什么样的参数是合适的，方案中必须包含每个参数的取值 + 选择依据 + 参考文献等 |
| 5 | 样本量评估 | 根据变量定义/预期效应大小，估算所需最小样本量/模拟次数 |
| 6 | 方案动态调整 | 支持基于中间结果修改实验方案：追加实验组、废弃无效方案、调整参数范围，并记录每次调整的原因和依据 |
| 7 | 方案文档化 | 以上所有规划结果，生成完整结构化实验方案文档（假设、方法、变量、预期结果） |

### 5.2 多学科科研工具支持及使用辅助

| 序号 | 二级需求 | 功能描述 |
|:---:|------|------|
| 1 | 工具发现 | 根据用户当前任务类型，选择适配的仿真/计算工具（如 WRF / VASP / GROMACS） |
| 2 | 工具配置引导 | 生成工具的输入文件模板，解释每个参数含义及推荐取值 |
| 3 | 运行过程辅助 | 运行中遇到报错，解析错误信息并给出修复建议 |
| 4 | 结果初检 | 工具运行结束后，自动检查输出文件完整性、关键字段是否合理（通过/异常） |

### 5.3 计算与实验流程自动化执行

| 序号 | 二级需求 | 功能描述 |
|:---:|------|------|
| 1 | 流程编排 | 将多步骤实验编排为 DAG 工作流 |
| 2 | 数据流管理 | 步骤间数据依赖自动传递，文件格式不匹配时自动转换 |
| 3 | 断点续跑 | 长流程中断后，从最近成功步骤恢复，不重跑已完成部分 |
| 4 | 并行调度 | 根据工作流，无依赖关系的步骤自动并行，有依赖的按序执行，提高效率 |
| 5 | 关键决策点确认 | 在实验流程的关键节点（结果异常/修复方案选择/方案调整/结论下判断）主动暂停并请求用户决策，而非全自动执行 |
| 6 | 过程监控与主动预警 | 对长时运行实验，在关键节点自动检查中间结果的物理合理性，发现异常主动预警并建议是否终止，避免浪费电力/算力资源 |
| 7 | 实验结构化归档 | 每次实验运行时自动记录代码版本、参数、数据版本、环境信息 |
| 8 | 运行结束通知 | 长任务完成后主动通知用户 |

### 5.4 算力资源灵活调用

| 序号 | 二级需求 | 功能描述 |
|:---:|------|------|
| 1 | 资源发现 | 自动感知可用计算资源，列出规格和空闲情况 |
| 2 | 智能匹配 | 根据任务特征推荐最优资源、理由 |
| 3 | 成本预估 | 如果是云资源，根据实例类型预估时长、费用，并请求用户确认 |
| 4 | 跨平台提交 | 统一接口屏蔽底层差异（SLURM / PBS / K8s / 本地），都能顺利准确提交作业 |

### 5.5 HPC 软硬件适配移植

| 序号 | 二级需求 | 功能描述 |
|:---:|------|------|
| 1 | 编译环境适配 | 根据目标硬件自动选择编译器和编译选项 |
| 2 | 依赖库移植 | 识别软件依赖的库版本，检查已有依赖是否可用，在目标环境编译安装兼容版本 |
| 3 | 性能调优建议 | 针对目标硬件、软件特性，提供编译优化选项（如 `-arch sm_80`、MPI 参数） |
| 4 | 正确性验证 | （可选）自动跑官方的 benchmark 验证正确性、性能 |

### 5.6 异常诊断与失败恢复

| 序号 | 二级需求 | 功能描述 |
|:---:|------|------|
| 1 | 错误分类 | 根据运行日志 + 错误输出 + 退出码，自动将实验失败分为：参数错误/数据错误/环境错误/资源不足/代码 bug 等类别 |
| 2 | 根因定位 | 分析错误日志、上下文、实验记录，访问已保存的实验结构化文档，定位最可能的根因 |
| 3 | 自动修复与重跑 | 针对常见失败模式给出修复方案（改参数/换数据/调环境）；重新运行重试结果（成功/再次失败 + 新诊断） |
| 4 | 失败知识积累 | 每次失败的诊断结果 + 修复方案 + 验证结果写入失败卡片库，减少同类错误不再犯 |

### 5.7 实验版本管理与可复现性

| 序号 | 二级需求 | 功能描述 |
|:---:|------|------|
| 1 | 实验数据结构化归档 | 每次实验运行时自动记录代码版本、参数、数据版本、环境信息 |
| 2 | 产物追溯 | 任何实验结果可一键回溯到完整的产生条件（代码 + 数据 + 参数 + 环境） |
| 3 | 环境复现 | 根据快照信息重建完整运行环境 |
| 4 | 结果分级与留存 | 识别原始结果，进一步识别关键结果并持久保存；非关键结果可清理或压缩 |
| 5 | 实验组管理 | 定义实验组（新方法）和多个对照组（基线方法），关联各自的实验 run |
| 6 | 结果可信度评估 | 对实验结果做多维可信度检查：数值收敛性、物理合理性、与已知结果的一致性，给出可信度等级和存疑项 |
| 7 | 初步统计与可视化 | **职责拆分**：Experiment 节点只做最低限度 sanity check / 物理合理性 / 崩溃检测 / credibility 守门；正式统计检验、效应量、显著性标注和展示型图表归 postprocess / reviewer，不作为 experiment 独立出图需求 |
| 8 | 结构化结论生成 | 综合所有实验结果，生成结构化的假设验证结论：假设是否成立、成立程度（完全确认/部分确认/需进一步验证/否定）、支撑证据链、局限性说明 |

---

## 六、领域覆盖

实验软件至少涵盖 **HPC 和 AI** 领域：流体力学 / 气象海洋地球 / …（持续扩展）