# Experiment 节点路线图

这是 nodes/experiment/ 的当前实现计划。它不是运行时契约，不能覆盖仓库 AGENTS.md、
已执行代码或回归测试。v2.0 路线图仅在 docs/history/ 中保留为历史记录。

## 与当前职责边界的迁移差距

职责边界的权威陈述在本分支 `AGENTS.md`，此处不复制。本节只记录实现尚未追平该边界的地方
（2026-09-09 核对）：

- `analysis_eligible`：**2026-09-11 节点侧已拆**。节点不再解释它——所有门禁改读自己真正的
  判据（`execution_mode` / `run_role` / `requires_hypothesis_verdict`），载荷里的资格位换成
  `not_replayable` 这个诚实申报位，operation 收尾与恢复记录不再产出它。
  **仍保留一处派生只读别名**（`load_run_contract` 与 `create_run_manifest` 各一处）：
  仓库根 `tests/test_run_contract_cross_node_read.py` 有三处直接下标取这个键，根测试不在本
  author 写边界内，单方面删键会把它们变成长红。删除条件写在代码注释里，owner=framework。
  跨 owner 待办另见下方 C-001，以及 `shared/tools/library/artifacts_extra.py` 仍在 freeze
  预注册时硬要求同名的**上游**字段（那是另一个字段，不是本节点这个）。
- 机械降格见证改名为 `execution_precondition_witnesses`，**不再翻任何资格门**
  （2026-09-11 owner 裁决：结论有瑕疵不等于没有结论，被记过见证的运行仍然欠裁决）。
- Experiment 不宣布 hypothesis validated/refuted，意义归下游；节点内仍有 verdict 机制。
  **2026-09-11 owner 裁决**：`requires_hypothesis_verdict` 不属于被禁止的"同类资格门禁"，
  保留——它是对框架声明"这趟欠不欠裁决"（`core/obligations.py` 读它），职责与已删的
  资格位不同。
- 三件套已是所有正式启动 run 的终态交付接口；实现上 scientific 仍走冻结日志加裁决，
  只有 operational 走三件套。
- 仓库内没有 `nodes/analysis`；框架的"欠一笔裁决"账目实际指向 hypothesis。
  本文件与其它节点文档里的"Analysis"应读作下游。

## 实施与验收原则

见本分支 `AGENTS.md` 的《根因、最小结构性修复与验证》一节，含分层验证强度表。
本文件不另立一套原则或验收标准。

## 节点内工作

| 编号 | 优先级 | 状态 | 工作 | 验收证据 |
|---|---|---|---|---|
| N-001 | P0 | 已完成 | 让 operation 与 scientific closure audit 失败（包括审计异常）都产生持久 blocker。 | 失败或异常均写入 `experiment_downstream_blocked` 与节点 blocker，强制非 completed 终态；回归覆盖 transcript 不可写和 Core 最终状态。 |
| N-002 | P1 | 已完成 | 让受限 external-job 等待响应取消，并在健康等待结束后允许持久且安全的交接。 | 等待中的 scheduler probe 可取消；健康等待持久化唯一 handoff；提交、取消、终态未知和恢复均有幂等/对账回归。 |
| N-003 | P1 | 已完成 | 在 Bash/Python 执行入口统一声明并消费 Data 输入包绑定。 | 两个 schema 均暴露 `input_package_artifact_id` 与 `input_package_bindings`；preflight 使用 `spec_id → package_artifact_id` 映射并覆盖多 spec/多包。 |
| N-004 | P1 | 已完成 → **2026-09-09 复核：漂移已重新出现，待重开** | 清除旧 quality_checks、默认 curator flow 和 Experiment verdict 所有权的本地文档漂移。 | 原验收"harness、README、review spec 和节点说明均描述当前强制行为"当时成立。AGENTS.md 换版后重新失真：README 仍按 primary simulation / secondary 分叉，review_spec 的 rubric 仍按旧的 verdict 归属评分，多份文档仍指向不存在的 Analysis 节点。**2026-09-11 已随 analysis_eligible 拆除修掉 README 两处、review_spec 两处与 scientific-results SKILL 的那句"按 run role 机械标注"**，其余仍在。文案同步随对应改动的 PR 进行。 |
| N-005 | P2 | 已完成 → 2026-09-09 复核失效 → **同日修复并经 UI 活体复验** | 对可修复的 closure 缺口，在 on-end 强制阻断前提供一次有界恢复机会。 | 原验收当时成立。此后失效：external closure gate 与 empty-stop guard 靠改写 LLM response 插入工具调用工作，而框架把 `on_llm_response` 改为只读（hook 收到深拷贝），experiment 的迁移豁免已于 2026-08-01 到期，两者自此只写事件、不产生效果。活体证据（2026-09-08 UI 运行）：模型无工具调用停机 → 收尾闸记录 `insert_closure_reminder` → 同一秒 run 结束，被插入的调用从未执行。单元测试直接传可变对象调内部函数，因此恒绿。仍有效的只剩 turn-end closure adviser（走注入，不改 response）与 on_end 持久 blocker（事后）。**2026-09-09 修复**：收尾闸迁到框架 `on_before_finish` 正门——返回消息即否决收尾，不接触 response，因此与豁免无关、不会再次静默失效。随之接受并写明三条限制：框架的闸每 run 只拦一次（这正是本条目原本声明的"一次有界恢复机会"）、模型空回复停机不经过该相位、注入的是消息不是伪造的工具调用。注入文本改为按实际观测状态生成，不再按 run 模式分叉教"先冻日志"。空停机守卫**删除**：框架已用空轮事务回滚加有界重试承接同一场景，比原来硬塞一次白板写入更诚实。测试改为经 `run_on_before_finish` 真实相位派发，并加一条机械断言禁止任何 hook 再改写 response；两处变异（摘掉相位注册、重新引入一处改写）均已验证转红。**2026-09-09 UI 活体复验通过**：新建 session 的 experiment run 1788956485-93c75b 在 turn 5 触发 `external_job_closure_gate action=veto_finish`，框架随即写下 `finish_gate_blocked`（该事件由 core 写，证明否决被真正接受并续了一轮），注入文本按观测状态列出两个未收尾作业各自的下一步，`max_wait_s=150` 取自 expected_duration_s（顺带验证旧实现恒为 300 的取值 bug 已修）；模型随后按指引走 check_external_job_health → wait_for_external_job → finalize_external_job，没有直接结束。 |
| N-006 | P0 | 已完成 | 本地构建与全部 `safe_execute_python` 统一进入既有弹性资源 supervisor，不再以 AST 黑名单充当资源边界。 | `safe_run_bash`、本地受管作业和轻量 Python 都限制 PID、内存、时间并有界流式记录日志；Python 默认申请 4 GiB，经 flexible 余量形成约 5 GiB 上限与 64 PID，并把常见数值库线程预算统一限制为 4。Python 使用逐调用最小写面：公共 `/tmp`、用户缓存、框架状态、项目/workspace、源码和依赖默认只读，临时文件与缓存进入 run-local `.python-scratch`，只对当前执行角色和已确认目标恢复写能力。AST 仅提前拒绝常见进程入口；动态拼接 Popen 的真实微测在 AST 返回空时仍触发 `pids.events`，以 `python_pids_limit_exhausted` 失败并清空 cgroup。PID/内存 80%/95% 分别进入压力/临界态并加密采样，不再仅凭比例抢杀；cgroup OOM、PID 拒绝和宿主/磁盘紧急余量仍整树终止，Swap 拒绝作为可恢复临界压力交给作业健康联合判断。同步与持久本地监督器复用同一资源健康分类器，采集 Swap、PSI、增长率与实时 cgroup events，告警恢复后回到常态；`probe_external_job_health` 只读联合进展与资源证据。准入按实时 `MemAvailable` 只检查请求总可行性和启动余量，顶层进程退出后确认 cgroup 后代静默，未静默则终止整树并改判失败。日志按文件系统余量保护，守卫或完整性沙箱不可用时启动前拒绝。local submit 的 CPU、内存、walltime 精确映射为固定 cgroup 合同，不额外放开 swap。 |
| N-007 | P1 | 已完成（通用机制） → **2026-09-09 复核：恢复出口不可执行，待重开** | 扩展现有 `declared_route` 为 Experiment 自主维护的证据驱动路线、步骤投影与失败诊断闭环。 | v2 canonical DAG、纯 resolver、scope/route 两阶段门、路径角色、资源计划、bound/outcome、证据化修订及 external 对账均已有回归。Bash 的 cwd 域、结构化路径事件和入口基于同一 AST 事件模型投影，路径门与路线绑定不再各自猜命令；动态或不可信包装保持 fail-closed。CMake L1 真实 E2E `1787769425-a9538b` 与最终安全回归 `1787773017-592c53` 均完成。缺解释器用 fake-wrapper 哨兵证明下游未启动；真实 WRF、GPU、MPI 和 SLURM/PBS 软件矩阵仍按专项测试集逐项验证，不能由本状态推定已支持。2026-09-09 审计复核：路线的**恢复出口**不可执行——重臂条件是 step 定义哈希变化，而全部恢复指引只讲证据 artifact；`in_progress` 锁给出的"先对账"出口在受管对账恒返回 `integration_pending` 时无交集；`declare_execution_route` 把不可满足的状态门排在格式门之后，每次只暴露一条。三份活体 transcript 均在此处反复烧轮次。机制存在不等于出口可达（AGENTS.md 硬门禁第 5、6 条与可达组合要求）。 |
| N-008 | P0 | 已完成 | 收紧 payload 完整性和 supervisor 自身生命周期：Bash 不再继承整个 state.root 写权，任何外部取消都先回收 payload、readers、日志、cgroup 与内部探针。 | 真实 bwrap 证明未知 writer 不能改框架 artifacts 而合法 run_root 可写；初始 wait、quiescence、内部 probe timeout/cancel 均用真实 sleep 进程验证无存活 PID、无未关闭 transport；本分支节点全量为 1534 passed、1 deselected。被排除项只检查根级 `pyproject.toml` 的依赖声明，而根级依赖文件按本次提交范围不纳入修改。 |

N-007 的“通用机制完成”不等于所有软件已经认证。后续软件案例继续记录在 `HPC_AI_软件专项测试集.md`；若案例暴露的是通用不变量缺口，再重开节点机制条目，而不是加入软件名称特判。

## 运行时升级项：本节点不得自行修补

| 编号 | 优先级 | 归属 | 根因 | 必需的运行时验收测试 |
|---|---|---|---|---|
| C-001 | P0 | Core/shared | post_run_flow 按 node type 静态决定，不看本次 run 的实际认知目的，因此 operation run 仍可能进入 reviewer 与下游科学裁决流程。**2026-09-09 前提更正**：原条目以 `analysis_eligible=false` 作判据，而该字段已从目标契约删除且不得改名重建，Core/shared 侧需要一个不依赖它的判据（run 的分类事实本身，或下游自行判断）。 | scientific experiment 保持审查/顺延；已完成 operation 只返回其声明的交接，不进入科学审查或裁决。判据不得是 `analysis_eligible`。 |
| C-002 | P1 | Core/shared | Project Workspace 的 child summary 可能把跨节点 artifact 当成 child 自己产物，导致 reviewer 选到上游 artifact。 | 已有上游 artifact 的 workspace 中，reviewer 只审 producing child run 所有的 artifact。 |
| C-003 | P1 | 测试基础设施 | 异步流程测试需要 pytest-asyncio，但当前本地测试依赖未提供它。 | 声明的测试环境能执行异步回归测试，而不是跳过或收集失败。 |
| C-004 | P0 | 集群平台/调度器 | Experiment 能声明外部作业的内存和 walltime，但无法仅从节点代码证明每种 SLURM/PBS 部署都启用了逐作业 PID cgroup、磁盘压力监控和文件系统 quota。 | 平台对真实调度器作业验证 PID、内存、walltime、日志文件系统余量和工作目录配额；任一紧急超限只终止该作业，不影响提交端或其他用户。 |
| C-005 | P1 | 框架/集群平台 | 当前 Kubernetes 提交接口没有权威 PVC/volume、容器挂载点和输入输出映射契约；宿主机路径不能被当成 Pod 内同一文件。节点当前只能早停，不能自行发明平台映射。 | 新契约必须绑定 volume 身份、容器路径、读写方向及输入输出 lineage；在契约落地并有真实 Pod E2E 前，通过通用静态有效性检查的显式 Kubernetes 提交在零物化阶段返回 `kubernetes_volume_contract_required`，自动推荐只在 SLURM、PBS 与 local 中选择。 |
| C-006 | P1 | Framework/shared artifact owner | `shared/tools/library/artifacts_extra.py` 已把缺少 `run_role` 的 pre_registration 记录为 metadata 的 `run_role_undeclared` / `freeze_warnings`，并在冻结返回值与 `artifact_frozen_with_warnings` transcript 事件中回显；当前剩余问题不是“上游静默”，而是该门只 warn 不 block，且 dispatch/consumer 尚无统一策略。Experiment 只能诚实记录 `undeclared_defaulted_secondary`，不能在节点内补写上游声明。 | Framework owner 明确 warn-vs-block 策略，并规定 dispatch/consumer 如何机械消费现有缺失事实；无论策略如何，不得允许 `node_inputs.run_role` 越过 frozen prereg 权威。 |

## 验证矩阵

验证强度按 `AGENTS.md`《根因、最小结构性修复与验证》的分层表触发，本文件不另列一套。
路线图条目只在交接中记录了命令与结果证据、并补齐回归覆盖后才能标记完成；
标记完成后若出现相反的实测证据，改判为"待重开"并写明失效原因，不保留失真的完成态。

Experiment benchmark 不在本分支维护，单独保存在 benchmark 分支 `experiment-benchmark-suite`（见 `AGENTS.md`《兼容、分支、交接与交付》）。benchmark 结果可以作为节点缺陷证据引用，但不作为本文件条目的完成门槛。
