> 📦 已归档（2026-08-30）：本计划已全部落地——`declared_route.v2` + resolver + transcript 事件即其交付物（见 `tools/execution_route.py`）。

# Experiment 路线步骤模型实施计划

> 状态：已实现并完成节点级回归与两个真实受控 E2E（2026-08-27）。本文是
> `ROADMAP.md` N-007 的详细设计、实施记录与验收边界，不是运行时契约。
> 当本文与已执行代码、仓库 `AGENTS.md` 或回归测试冲突时，以后者为准并修订本文。

## 一、目标与边界

Experiment 接到的是“安装、构建、运行并验证某个软件”等目标，不是由上游预先拆好的
固定 `stage`。节点应先调查目标版本对应的官方路线，再以当前源码、平台、输入和资源证据
形成本节点计划，并守护它直到成功、诚实失败或形成 blocker。

本次只修改 `nodes/experiment/`：

- 不修改 `core/`、`shared/` 或其他节点。
- 不建立通用工作流引擎，也不把 `build_state` 泛化成全任务状态机。
- 不在框架里枚举 WRF、GROMACS、LAMMPS 等软件名称或专用构建命令。
- 不让 Agent 编写的路线 artifact 自行授予路径、科学权限或资源豁免。

### 供评审判断的总逻辑图

符号：`[=]` 复用现有权威；`[+]` 新增最小连接件；`[!]` 修复接线或所有权；`[-]` 降权或取消旧权威。

```text
修改前：外部 stage 与命令形状驱动，判断分散

自然语言目标
└─ 上游/调用方猜 stage、requires_build_root
   ├─ stage 同时影响科学门、cwd、资源语义、故障作用域
   ├─ 缺 stage → diagnostic；缺 requires_build_root → 无 build_root
   └─ 每条命令由 AST、正则、最新 artifact、多个 hook 分散判断
      ├─ 路径/科学/资源门的先后和覆盖范围不统一
      ├─ safe_run/submit 未共同验证 shell 可见的实际写目标
      ├─ bypass 可在部分入口先于硬路径事实返回
      ├─ Python AST 被误当进程边界，实际执行未统一进 cgroup
      ├─ spawn 前没有 route + step + attempt 的持久绑定
      └─ 失败后由多个 ledger/hook 各自推进
         ├─ 可能原样重试或换入口
         ├─ 外部提交中断存在重复提交窗口
         └─ Data、operation、external closure 存在组合缺口

修改后：scope、冻结路线与执行事实驱动

自然语言目标
├─ [=] 只读官方资料/源码/环境调查
│  └─ 无 scope、无完整路线快速通过；仍受绝对安全边界约束
├─ [=][+] 执行控制面准备（两者声明先后无关）
│  ├─ classify_experiment_scope：每个 run 一次
│  │  └─ operation / scientific 身份不可由 route、stage 反向改写
│  └─ 高后果动作的 canonical declared_route v2 steps[]
│     └─ 低风险局部写入不强迫声明完整路线，也不能完成路线步骤
└─ 首个会改变目标软件/工作目录或启动/提交 payload 的动作
   ├─ [=][!] 按入口适用的无副作用前置检查
   ├─ [+] 纯 resolver：共同投影 scope、冻结 route triple、依赖、收据和领域状态
   ├─ [+] pre_materialization 门
   │  ├─ resolver 异常、scope 缺失/冲突、高后果动作缺 ready route → 尚未建目录即拒绝
   │  └─ 只读和不提交的 dry-run 保持可用
   ├─ [=][!] path_roles 只派生 source/build/run 语义；本机物化还必须有 Core/平台或人工 path capability
   ├─ [=][!] 完整门禁取并集
   │  ├─ 同一 Bash AST 事件模型定义 cwd 域、结构化路径事件和单一路线入口投影 → 既有 path_roles；bypass 不越过硬边界
   │  ├─ 科学 prereg/input/parameter + Data
   │  └─ route effects + 机械观察 + major-build 兜底 + 全部本地执行 cgroup/timeout
   ├─ [+] 匹配路线/高后果分支
   │  ├─ spawn/submit 前 route_step_bound 或 submission intent
   │  ├─ [=] 受管执行 / scheduler 生命周期
   │  │  └─ 顶层进程退出后确认 cgroup 后代清空；取消时先回收 payload 与内部探针再传播
   │  └─ route_step_outcome + 客观产物/领域收据
   │     ├─ success → 解锁依赖步骤
   │     ├─ failed → 分类；无新证据禁止原样重试
   │     ├─ interrupted/unknown → 先对账，禁止盲目重提
   │     ├─ 仅 expected_outputs 契约写错 → 成功收据只授权修订；修订后重新受管执行
   │     └─ blocked → 明确 owner、证据和解除条件
   │        └─ [=][!] 唯一 operation/scientific closure 与下游交接
   └─ [=] 未匹配 route 的低风险局部写入
      └─ scope + 路径/高危门后直接写入；不产生 route bound/outcome

[-] 不再作为权威：stage、requires_build_root、时间上“最新”的路线、
    可变 current_step 游标、未校验 recovery_basis、route 自授路径/科学权限。
```

## 二、修改前逻辑

```text
上游任务
├─ node_inputs.stage / requires_build_root
│  ├─ requires_build_root=true 才预分配 build_root
│  └─ 工具未传 stage 时默认 diagnostic
│
├─ Agent 调查后保存 declared_route
│  └─ 当前契约以 activities/compiler/build_discovery 为中心
│
└─ 每次执行命令
   ├─ stage → default_stage_workdir
   ├─ boundary / Bash AST / timeout / scientific preflight
   ├─ major-build 正则命中 → _build_gate
   │  ├─ 优先 build_graph
   │  └─ DAG 不可用时才读取时间上“最新”的 declared_route
   ├─ _activity_path_role_guard 已实现但未接入主链
   ├─ stage=toolchain_build 或 _is_major_build → 资源守卫
   └─ 执行 → 通用 tool_call/tool_result
      └─ 没有持久记录“本次调用属于哪版路线、哪一步”

失败后
├─ execution_control、repair_ledger、hook 提示分别记录
├─ 无统一的路线步骤客观派生关系
└─ 没有新证据时仍可能换入口或重复相同失败

旁路缺陷
├─ Data：一个 input_package_artifact_id 与所有 active spec 比较
└─ external job：operation completion 与 finalize 的唯一写者契约不能组合闭合
```

## 三、修改后逻辑

以下顺序是实际执行不变量，不是 Agent 建议流程。符号沿用上图。

```text
A. 调查与身份

上游自然语言目标
├─ [=] 读取官方资料、README/INSTALL、脚本、CI 和平台事实
│  └─ 可证明只读：不要求 scope 或 route，不产生执行成功收据
└─ 首个改变目标软件/工作目录或启动/提交 payload 的动作前 [=][!] classify_experiment_scope
   ├─ operational：非科学安装、构建、smoke、调度器探查
   └─ scientific：继续服从冻结 prereg、输入和参数

B. 本节点计划（与 scope 分类无强制先后；执行前共同投影）

[+] declare_execution_route
└─ 保存唯一 canonical declared_route，schema_version=2
   ├─ goal / evidence_refs
   └─ steps[]
      ├─ id / after
      ├─ action(tool + 规范化入口)
      ├─ effects：只能增加守卫
      ├─ workdir_role：只能选框架已授权角色
      └─ expected_outputs：可选完成证据，不是唯一证据

C. 每次工具调用的确定性顺序

[=][!] 入口适用的零副作用前置检查
  ├─ Bash/Python/submit：后台、动态、语法与绝对执行边界
  └─ safe_write_file：只构造 action，不创建目录
  ↓
[+] resolve_execution_context 纯投影
  ├─ 保留 matched_ready_step 等原始路线结论
  └─ 共同附加 scope_required / scope_status / scope_mode；不依赖声明先后
  ↓
[+] execution_route_block(phase=pre_materialization)
  ├─ resolver_error：所有目标变更或 payload 执行 fail-closed
  ├─ 未分类 scope：同样 fail-closed
  ├─ operational + formal_scientific_execution：顺序无关地拒绝
  ├─ 高后果动作缺 ready route：目录创建前拒绝
  └─ 只读 / 不提交 dry-run：继续
  ↓
[=][!] route 派生 path_role，随后才允许创建 source/build/run 目录
  ↓
[=][!] 实际 payload 路径投影
  ├─ safe_run/submit：同一 Bash AST 事件模型展开静态 pipeline、子 shell、函数、字面 shell-c/eval，并定义带 cwd 与调度角色的结构化路径事件；路径门和唯一路线投影器共同消费该模型 → 同一 path_roles 分类器
  ├─ safe_write/Python：AST 可证明写目标 → 同一分类器；实际 Python 再由逐调用只读沙箱兜住动态目标
  └─ 未解析、源码基线、框架状态和角色冲突均为不可 bypass 的有效性硬拒
  ↓
[=][!] 完整预检
  ├─ immutable baseline / worktree / build_root / run_root 路径门
  ├─ 本机 OS 写能力门：上游 path_roles 不能自行生成 bwrap bind
  ├─ prereg / input / parameter / Data 门
  ├─ route effects ∪ 机械观察 ∪ _is_major_build 兜底
  └─ resource plan → cgroup PID/内存/时间 + 磁盘/日志监督；全部 safe Python 同样受管
  ↓
动作分支
├─ 未匹配 route 的低风险局部写入
│  └─ scope + 路径/高危门 → writer；不写 route bound/outcome
└─ matched route step（高后果动作必须属于此分支）
   ↓
   [+] execution_route_block(phase=pre_spawn)
     ├─ ready、cwd、入口、attempt、歧义、失败恢复检查
     └─ 路线不权威或绑定不精确 → fail-closed
   ↓
   [+] spawn/submit 前写 route_step_bound / submission intent
   ↓
   [=] 受管执行（Bash、全部 safe Python、本地作业或外部 scheduler）
   ↓
   [+] route_step_outcome + 预期产物 + 领域权威状态

D. 失败与终结

失败/超时/取消/缺产物
├─ 归类 observation / environment / dependency / command / resource /
│      timeout / cancelled / external / framework / unknown
├─ 有始无终 → interrupted/unknown，先对账
├─ 一般失败 → 必须有新诊断/修复证据，禁止原样重试或换入口
├─ 唯一窄例外 → 实际执行已成功且整版 route 只改 expected_outputs；
│                既有成功收据只授权这次修订，步骤重新变为 ready，随后必须重新受管执行并产生新 bound/outcome
└─ 外部作业 → submission nonce + scheduler/local 身份对账，禁止盲目重提/重取消

终结
├─ [!][=] Data 使用 spec_id → package_artifact_id 映射
├─ [!][=] operation completion 可携带 scope-exact external refs
└─ [=] 唯一 raw_results → clean_results → experiment_log closure 与下游交接
```

## 四、状态所有权

### 修改前后同屏差异图

```text
修改前（多个 stage 语义耦合）                  修改后（目标驱动、事实投影）

上游目标                                      上游目标
  │                                             │
  ├─ stage / requires_build_root 猜整轮类型      ├─ run_contract 只锁定科学/操作身份 [复用]
  │                                             └─ Experiment 调查后声明 route.steps[] [新增]
  ▼                                             ▼
调用方为每次命令选择 stage                     resolver 从依赖、收据、领域状态纯派生 ready step
  │                                             │
  ├─ stage 决定 cwd                             ├─ workdir_role 只选择框架已授权路径
  ├─ major-build 正则决定部分守卫               ├─ 路线声明 ∪ 机械观察 ∪ 旧正则兜底
  └─ build gate 读取多种“最新”artifact          └─ 冻结 route triple 精确绑定
  ▼                                             ▼
执行                                            不可逆动作前写 bound / intent
  │                                             │
  └─ 通用 tool_result，无法回答“属于哪一步”     ├─ 既有路径、科学、Data、cgroup 门继续生效
                                                └─ 执行后写 outcome + 客观领域收据
                                                      │
失败分散进入多个 ledger/hook                         ▼
  │                                             快照投影步骤状态
  ├─ 无统一 current-step 派生                    ├─ ready / in_progress / interrupted
  ├─ 无新证据仍可能原样重试                      ├─ failed / blocked / verified
  └─ scheduler 身份丢失可能诱发重提              └─ 无新证据禁原样重试；外部作业先对账
                                                      │
  ▼                                                   ▼
completion / Data / external 各自闭合困难          复用唯一领域终态并完成 operation/scientific 交接
```

| 判断点 | 修改前权威来源 | 修改后权威来源 | 明确不做的事 |
|---|---|---|---|
| “当前做哪一步” | 调用方传入的 `stage` 和命令正则 | 冻结路线 + 依赖 + 收据的纯函数投影 | 不保存可漂移的 `current_step` 游标 |
| “去哪执行” | `requires_build_root`、stage 默认目录 | ready step 的 `workdir_role` + 框架路径授权 | route 不自行创建路径权限 |
| “带哪些守卫” | stage/正则各走一部分分支 | 路线 effects、机械事实、旧兜底取并集 | effects 不能解除 cgroup/科学/Data 门 |
| “是否真的开始” | 主要依赖工具返回 | spawn/submit 前的 bound/intent | 不把无 outcome 的 attempt 当普通失败 |
| “是否完成” | Agent 声明或局部产物 | 工具收据、输出指纹、领域权威状态至少一种 | 空 success check 不自动成功 |
| “失败后能否重试” | 各 hook 独立提示 | 失败分类 + 新 remediation artifact + payload digest | 不因换措辞或换入口绕过相同失败 |
| “外部作业是否可重提” | job_id 持久化后才可靠 | attempt nonce 的 intent + scheduler/local 对账 | 结果 unknown 时禁止盲目重提 |


| 信息 | 权威来源 | 不允许成为权威的对象 |
|---|---|---|
| 路线计划 | 冻结的 canonical `declared_route` 版本 | scratchpad、workflow frame |
| 路线版本身份 | Core artifact 的 `id/version/content_hash` | 路线正文自报的 hash/revision |
| 工具调用事实 | Core 通用工具日志 + 节点 route 收据 | 可变 `route_execution` artifact |
| 当前步骤 | 路线、步骤定义 hash、执行收据和领域状态的纯派生结果 | 持久 `current_step/status` 游标 |
| 构建子目标 | 现有 `build_state` | 通用软件生命周期状态机 |
| 输入交付 | Data spec 与经验证的数据包绑定 | route 自报“输入已满足” |
| 外部作业 | submission/lifecycle/workflow 与 scheduler 对账 | route 自建 job 状态 |
| 科学身份 | 冻结 `run_contract` / prereg | route effects 或调用方 `stage` |
| 路径语义 | `path_roles` 只说明 source/build/run 用途 | route、artifact 或上游 `node_inputs` 自授本机 OS 写能力 |
| 本机写能力 | Core 已授予写根，或本 run 消费人工确认后登记的精确 capability | 仅凭目录名、角色名或 `authority` 字符串扩张 bind |

`hook_state` 可以作同进程缓存，但不同恢复入口对它的持久性不同，不能作为唯一执行事实。
transcript 是追加式、正常控制流下及时落盘的主要恢复依据，但当前 Core 写入没有 `fsync`、
记录锁或防篡改保证；读取必须容忍残缺尾行，不能宣称主机崩溃绝不丢。

## 五、路线结构

仍然只有一种 artifact：`artifact_type=declared_route`。`schema_version=2` 是内容结构版本，
不是第二个 artifact，也不是自动回滚机制。

```yaml
schema_version: 2
goal: 安装并验证目标软件
evidence_refs:
  - 官方文档、发布说明或源码内证据引用
steps:
  - id: acquire-source
    goal: 获取固定版本源码
    after: []
    action:
      tool: safe_run_bash
      program: git
      evidence_refs: []
    effects: [network_access, workspace_write]
    workdir_role: managed_source_root

  - id: build-main
    goal: 使用官方入口构建
    after: [acquire-source]
    action:
      tool: safe_run_bash
      program: ./official-wrapper
      evidence_refs: []
    effects: [workspace_write, process_tree]
    workdir_role: build_root
    expected_outputs: [bin/example]
```

初版只实现框架真正能执法的有限效果；未实现的效果必须显式标为 advisory，不能制造
“已经受保护”的假象。`effects` 是意图声明，只能增加守卫。真实动作机械分析发现更高风险
时，以机械结果为准；Agent 漏标不能获得豁免。

旧 `activities` 契约继续只读兼容并归一化为内存视图。格式错误的 v2 不能退回旧格式放行。

## 六、执行收据与恢复

Core 已记录通用 `tool_call/tool_result`，节点不重复复制完整命令日志。只补路线绑定缺口：

```text
route_step_bound       # 所有高后果 spawn/submit 之前
  route_artifact_id / version / content_hash
  route_step_id / step_definition_hash / attempt_id
  tool / resolved_workdir_role / applied_policy

route_step_outcome     # 正常返回、失败、超时或已处理取消之后
  attempt_id / outcome / failure_class / evidence_refs
```

只有 `bound` 没有 `outcome` 时，状态是 `interrupted/unknown`，绝不能自动视为失败后可重试。
外部作业尤其需要先使用既有 submission identity 与 scheduler 对账；写 started 并不能单独
解决“scheduler 已接受，但 job_id 尚未持久化时进程崩溃”的重复提交窗口。

步骤完成必须至少有一种客观依据：

1. 受管工具收据；
2. 预期产物验证；
3. Data、external job、build 或科学契约等现有领域权威状态。

空 `expected_outputs` 不自动失败，也不自动成功。只读 probe 的非零返回可以是有效观察，
但不能被误记为软件执行成功。

## 七、风险分级

| 条件 | 当前确定性处理 | 仍须经过 |
|---|---|---|
| 可证明只读的探查 | 无 scope、无 route 可继续 | boundary、Bash AST、危险命令检查 |
| 不提交作业的 dry-run | 无 scope、无 route 可继续 | 脚本语义、路径和危险命令检查 |
| 任一真实执行缺 scope | 物化前 fail-closed | 先完成一次 run scope 分类 |
| 已分类 scope 的低风险局部可逆写入、但无 route | 复用既有门并记录 advisory；不产生 route 收据 | 写边界、路径角色、高危门 |
| 任一真实执行发生 resolver 异常 | 物化前 fail-closed | framework-owner blocker；只读诊断保持可用 |
| 构建、主要运行、进程树 | 缺权威 ready route 时 fail-closed | 路线、路径、资源计划、cgroup、timeout |
| 外部提交 | 缺权威 ready route 或 shell 可见目标越界时，在 script/intent/submit 前 fail-closed | 路线、path_roles、submission identity、scheduler 生命周期 |
| Kubernetes 且无 volume contract | 在 resolver、目录、脚本和 intent 前返回平台 blocker；`auto` 不选 Kubernetes | 等待框架/平台提供 PVC/volume、容器挂载点与输入输出映射契约 |
| 轻量 Python | 统一 cgroup + 逐调用只读 sandbox；仅恢复本次角色/目标，scratch 与 cache 留在 run_root，线程预算为 4 | 路径角色、PID/内存/时间、动态写 OS 拒绝和资源终态 |
| 正式科学执行 | 缺权威 ready route 时 fail-closed | run_contract、prereg、输入、参数、Data 和路线一致性 |

fail-closed 返回当前动作级结构化阻断，不因一次错误永久封死整个节点。只读诊断、日志查看和
诚实上报保持可用；最终 `blocked` 仍服从节点 blocker 生命周期。外部提交的静态路径门只能证明
shell 可见目标与构建 cwd；任意二进制内部写路径、远端 mount namespace、逐作业 PID/磁盘配额仍由
平台执行，缺平台证据时必须明确标为未验证。

## 八、实施前缺陷及处理结果

这些是实施前已证实的结构缺口；现在均通过既有权威机制的扩展或接线解决，没有再建立平行状态机。

| 实施前缺口 | 处理结果 | 权威仍归属 |
|---|---|---|
| operation completion 不能携带 finalize 所需的 scope-exact external refs | 已修复并覆盖组合闭环、partial 恢复和幂等冲突 | `record_operation_completion` 与既有 external lifecycle |
| 一个 Data 包 id 被错误地比对全部 active spec | 已改为 `spec_id → package_artifact_id` 持久映射，并保留单包兼容入口 | 既有 Data spec/package ledger |
| 路径活动门未完整进入执行主链，baseline 与隔离 worktree 边界不清 | 已接入 Bash/Python/submit；route 只能选择已授权角色，不能授予路径 | `path_roles` 与框架/人工授权 |
| bypass 在部分入口提前返回，硬路径事实可被跳过 | bypass 只对精确环境授权有效；源码基线、框架状态、未解析目标与角色冲突始终硬拒 | 既有 scope 分类器 + `_scope_bypass_allowed` |
| safe_run 与 submit 只检查 workdir/元数据，未共同检查 payload 内实际目标，路径与路线又可能分别猜入口 | 统一 tree-sitter analyzer 定义 cwd 域和 `StaticPathEvent`（含 `direct/transparent/delegated` 调度角色）；`_bash_path_effects_guard` 投影实际写目标，`_project_bash_route` 作为唯一函数从同一事件模型投影入口。静态嵌套可展开，动态/不可信包装保持未解析或强守卫 | `bash_semantics` + `path_roles` + route resolver |
| Python AST 进程识别曾承担过强安全假设，实际执行器没有统一 PID/内存 cgroup，且继承公共临时目录/缓存宽写面 | AST 降为友好诊断；全部 safe Python 进入现有 supervisor，并把资源终态映射为 Python 语义；逐调用 sandbox 默认只读框架/项目/源码/依赖，只精确恢复授权角色，临时目录和缓存进入 run-local `.python-scratch`，常见数值库线程预算统一为 4 | `_exec_and_log` + `build_resource_guard` + `python_sandbox_roots` |
| 按时间取“最新” declared_route，缺少确定绑定 | 已改为唯一 canonical route triple 和 attempt 绑定；歧义 fail-closed | Core artifact identity + route resolver |
| `verify_min_run` 裸 subprocess 绕过受管入口 | 已停止该旁路，最小运行统一通过 declared route 与受管工具 | `safe_run_bash` / `submit_job` |
| Bash payload 继承 Core 对整个 state.root 的通用写能力，未知入口可绕过文本分析改写 transcript、artifact 或路线账本 | Bash OS 沙箱先对框架状态和受保护角色只读覆盖，再仅按 cwd/目标、路径角色与真实本机 capability 精确回绑 run/build/worktree；真实 bwrap 验证未知 writer 失败、合法 run_root 成功 | Core 状态所有权 + bash_sandbox_roots |
| supervisor 只在初始 wait 捕获取消，quiescence 阶段取消仍可留下后台组；内部 systemctl show 超时/取消也会泄漏探针 PID/transport | 所有异步终态阶段共享“先 kill/reap/readers/log/quiescence、后传播取消”的边界；内部探针统一 bounded communicate，异常时 kill、drain、wait 并关闭 transport | 既有进程组/cgroup + _communicate_bounded_probe |
| 远端 AST 把 TMPDIR 父级跳转投影回 run_root，看似已声明但实际可逃出 scheduler scratch | raw token、AST marker、CLI operand 与 redirect 统一走 scratch containment；父级、二次变量、动态或绝对重解释在提交前归为 unresolved | Bash AST 事件 + 既有远端路径门 |
| 轻量 Python 曾在 await 期间修改进程级环境并用全局锁串行化，既可能串 run 又降低并发流畅度 | 每次调用构造独立 child_env 直接传给 spawn；两个并发 run 的 root/scratch/thread 环境互不串线，宿主 os.environ 不变 | 单次工具调用 + 子进程环境 |
| Kubernetes 可探测却没有 volume contract，宿主机路径可能被误当 Pod 内同一文件 | 通过通用静态有效性检查的显式 Kubernetes（含 dry-run）在 route、目录、脚本和 intent 之前返回 `kubernetes_volume_contract_required`；自动推荐不选择 Kubernetes | `resource_manager._discover` 与 `_submit_job`；volume contract 仍归框架/平台 |

每项分别有回归测试；没有通过一次大重命名把行为变化隐藏起来。

## 九、完整实施计划与状态

| 序号 | 工作项 | 状态 | 主要完成证据 |
|---|---|---|---|
| 1 | 读取根/节点规则，保存修改前基线并画出差异图 | 完成 | 修改前后权威来源与调用链已在本文固定 |
| 2 | 建立中文设计、状态所有权、不变量和软件专项测试集 | 完成 | 本文与 `HPC_AI_软件专项测试集.md` |
| 3 | 先修 Data 多 spec 映射和 operation closure 等已证实缺陷 | 完成 | 独立回归覆盖映射、三件套、partial 恢复与幂等冲突 |
| 4 | 实现 `declared_route` v2 canonical DAG 与旧格式只读兼容 | 完成 | schema、无环、唯一 id、冻结 route triple 测试 |
| 5 | 实现快照投影、纯 resolver 与影子判断 | 完成 | ready/blocked/interrupted 和 shadow 差异测试 |
| 6 | 接入路径角色、执行前 bound 和执行后 outcome | 完成 | Bash、Python、submit 三条执行链均有 attempt 收据 |
| 7 | 接入风险分级、失败分类和证据化恢复 | 完成 | 相同 payload、步骤隔离、契约修订和恢复上下文测试 |
| 8 | 闭合外部提交身份、恢复、取消与终态投影 | 完成 | nonce、ambiguous accept、PID 复用和幂等取消测试 |
| 9 | 关闭 bypass、实际 payload 路径、safe Python 资源/文件系统封套与 Kubernetes topology 四项独立复审缺口 | 完成 | Bash 结构化路径事件与单一路线投影、零 spawn/零 submit、Python 动态目标只读沙箱/线程预算、真实动态 Popen cgroup 微测，以及 Kubernetes 零物化 blocker 回归 |
| 10 | 运行受控真实 E2E、聚焦组合回归、节点全测试和跨层契约测试 | 完成 | 见第十一节实测记录 |
| 11 | 同步文档并审计 diff、格式、临时文件和越界修改 | 完成 | `git diff --check` 通过、无临时文件；根级依赖与角色文件作为明确范围项单列交接 |

`stage/requires_build_root` 只保留兼容读取，不再是路线、目录或高后果动作的隐藏授权。
里程碑字段改名与 benchmark 分组修复都不是“零行为变化”，也不是本模型前置条件；没有借本次
改造进行无关的大规模重命名。

## 十、验证矩阵

### 本机可执行

- v2 schema、无环依赖、唯一步骤 id、旧格式兼容、route triple 绑定。
- 纯 resolver 的 ready/blocked/interrupted 派生与步骤定义变更失效。
- route 永不授予路径权限；只读探查不要求路线。
- Bash 的 pipeline、子 shell、函数、字面 `shell -c`/`eval` 由结构化路径事件保留各自 cwd；路径门和路线入口消费同一 AST 结果。动态展开、不可信透明包装器和多主 payload 不得被折叠成已知单入口。
- Make/CMake/Ninja、官方 wrapper、configure-first、隔离 in-tree build。
- 缺解释器在 spawn 前拒绝；使用 fake executable 哨兵证明错误 fallback 没有启动。
- Data 多 spec、多数据包映射。
- operation/external job 可组合闭合、取消/恢复/重复调用幂等。
- scientific prereg/input/parameter 门不被路线绕过；operation smoke 不误入科学门。
- timeout 杀整棵本地进程树，不遗留后台子进程。
- Kubernetes 未提供 volume contract 时，通过通用静态有效性检查的显式提交在零物化阶段拒绝，且 `scheduler=auto` 不选择 Kubernetes；SLURM/PBS/local 的既有选择不退化。

### 服务器真实 cgroup

- 小型 Make/CMake/Ninja 正常构建不误杀。
- 全部 `safe_execute_python` 也进入同一 supervisor；默认申请 4 GiB，经弹性余量形成约 5 GiB 与 64 PID，并把常见数值库线程环境限制为 4。
- Python 文件系统根默认只读，只精确恢复 run/current-role/已确认目标；公共 `/tmp` 与用户缓存不可写，临时目录、缓存、Matplotlib 和字节码缓存均落在 run-local `.python-scratch`。路径角色重叠、沙箱不可用或 bypass 请求均不得降级为宽写执行。
- 动态拼接绕过 Python AST 的进程派生必须仍由 PID cgroup 终止，终态报告 `python_pids_limit_exhausted`。
- 受控递归进程、内存增长、超时和高速日志任务只影响自己的 cgroup。
- 检查 `pids.current/memory.current` 和资源事件；`pids.events` 只能证明 PID 上限拒绝，
  不能证明某个进程名从未启动。
- 验证整个 cgroup 收敛、无孤儿，并且旁路 sentinel 进程不受影响。

### 真实 scheduler

- dry-run、短作业提交、pending/running/completed、取消与 finalize。
- 模拟提交成功后本地持久化中断，恢复先对账且不重复提交。
- 真实 SLURM/PBS cgroup、quota 和磁盘压力属于平台侧验收，节点不能伪造已验证。

### 软件专项

按路线拓扑选代表案例，而不是在框架中堆软件特判：无构建安装、CMake out-of-source、
configure-first、官方 wrapper、隔离 in-tree、缺依赖诚实 blocker、build 后 smoke、Data 交接、
scientific run、local/外部作业和路线修订。输入只给目标、来源、资源预算和成功判据，不提供
`stage`、`requires_build_root`、内部 artifact 名或标准答案。

## 十一、已执行验证记录（2026-08-27）

### 自动化回归

- 路径、作用域、Bash AST、资源、取消、外部提交恢复与路线关键组合回归均已通过；未关闭 subprocess transport 警告按错误处理。
- 当前六提交分支的 Experiment 节点全量结果为 1534 passed、1 deselected、1 warning。测试在 Python 3.12 下显式安装 Bash parser，并使用 `--frozen`；外层 user cgroup 为 MemoryMax=6 GiB、MemorySwapMax=0、TasksMax=384、RuntimeMaxSec=1800。被排除测试只检查根级 `pyproject.toml` 是否声明 Bash parser；按用户指定的六提交范围，`pyproject.toml/uv.lock` 不提交，因此不能把该项写成通过。唯一 warning 是根级 pytest 配置中的未知 `asyncio_default_fixture_loop_scope`，不属于本节点可修改范围。
- 最终 Core/节点跨层组合覆盖 closure、prereg、跨节点读取、artifact capability 与工具契约，共 `83 passed`；使用临时 `uv run --with pytest-asyncio`，未修改仓库依赖。
- 本轮新增边界均已进入全量结果；其中 scheduler scratch、Bash state-root 只读覆盖、初始/quiescence 取消和内部 probe 回收另有真实进程或真实 bwrap 定向测试，独立 Reviewer 最终未发现可复现 P0/P1。

### 真实受控本地 E2E

- 同一自然语言 fixture 独立运行两次：v8 run `1787769425-a9538b`（19 turns、21 次工具调用）与最终安全回归 v9 run `1787773017-592c53`（23 turns、25 次工具调用），均为 `completed`。
- 运行外层约束：`MemoryHigh=3 GiB`、`MemoryMax=4 GiB`、`MemorySwapMax=1 GiB`、`TasksMax=256`、`RuntimeMaxSec=900`。
- Agent 只收到自然语言目标，没有 `stage`、`requires_build_root`、route 字段或标准答案；v9 只分类一次 operational scope，最终 canonical route 为 configure → build → smoke。
- v9 的 scope 事件出现在全部 `route_step_bound` 之前；configure、build 和未知自定义 smoke 入口均走受管执行，资源计划为 2 CPU、2 GiB、5 分钟、local fixed policy，实际投影为 `CPUQuota=200%`、`MemoryMax=2 GiB`、`TasksMax=64`、timeout 300 秒。
- 一次错误的 smoke `expected_outputs` 契约被拒后，既有成功 attempt 收据只授权修改该字段；修订后的 smoke 步骤重新执行并产生新的 bound/outcome，未复用旧收据直接宣告成功，也未允许任意 recovery 文本或入口替换绕过。
- 两次运行的源码 `CMakeLists.txt` 与 `main.c` 前后 SHA-256 均一致；每次最终只有一组冻结的 raw/clean/log closure，无外部提交、无重复 closure。
- v9 暴露一个非阻断流畅度观察：Agent 曾把只读 probe 错列为路线依赖；只读动作不会产生 route receipt，Agent 最终按工具说明移除该步骤后完成。当前不为这类可纠偏规划失误加入按名称/goal 猜测的 validator；若在软件矩阵中重复出现，再基于基准设计显式 observational prerequisite，而不是扩张执行状态机。

### 明确尚未验证

- 尚未在这轮真实下载/构建 WRF，也未在缺 csh/tcsh 的真实 WRF 源码上跑 L2 案例；当前只有通用 official-wrapper、缺解释器和 fake 下游哨兵回归。
- 未验证真实 SLURM/PBS 集群的逐作业 cgroup、quota、磁盘压力和提交成功后主机崩溃恢复；这些仍是平台侧验收。
- 未运行 GPU、MPI、多节点和大型领域软件矩阵；不能从本次 CMake E2E 推断它们已经支持。

## 十二、完成判断

只有同时满足以下条件才能宣布完成：

- 权威事实源没有增加成互相竞争的状态机。
- 高风险动作不能通过漏标 effects、resolver 异常或旧 stage 绕过资源/科学门。
- 只读调查和低风险纠偏没有被繁重流程拖死。
- 无新证据不能重复同一失败；中断的外部作业不会重复提交。
- Experiment 全测试、聚焦组合测试和适用的真实 E2E 有命令与结果证据。
- 无法执行的真实 cgroup/scheduler/主机崩溃测试明确列为未验证。
