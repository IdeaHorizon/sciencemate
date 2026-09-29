# Experiment 缺陷家族

> 证据盘点日期：2026-08-27
> 范围：`nodes/experiment/**`、直接相关的 Core/shared 代码与测试、Git 历史修复。
> 口径：这里只归纳有提交补丁、测试中的回归说明、代码注释或当前工作树补丁直接支持的缺陷家族；`WORKTREE` 表示尚未提交，不能当作历史提交。

## BF-01 — 产物绑定与上游契约

- **定义**：Experiment 消费跨节点 artifact 或上游交付时，从目录、枚举顺序、时间或自创字段推断“当前输入”，而没有绑定生产者提供的身份、版本和 schema。
- **典型历史缺陷**：`EXP-REG-001`～`EXP-REG-006`：prereg 只在 Experiment 自己目录查找、多 prereg 取“最新”、sediment 读取 identity head、漏接 Data 的 `package_dir`、Data 已运行但未交付仍派发 Experiment、Data child blocker 跨 run 读不到。
- **系统不变量**：跨节点输入必须通过 canonical artifact API 读取；只接受所需冻结状态；多候选必须由调度方显式绑定 `(artifact_id, version, content_hash)`；生产者 canonical schema 必须由消费者验证；已发生但未结清的上游债务必须阻断或显式 waiver。
- **容易出现的代码模式**：直接 glob `state.artifacts_dir`；`setdefault`/文件名/`frozen_at` 选“第一份/最新一份”；`read_artifact(id)` 读取 head 而忽略 run-bound version；消费者自造字段别名；把结构化 contract 转字符串后做 substring；只检查“有字段”而不检查文件、manifest 与 lineage。
- **当前测试覆盖**：`tests/test_run_contract_cross_node_read.py`、`tests/test_prereg_version_binding.py`、`tests/test_data_stage_debt_gate.py`、`nodes/experiment/tests/test_sediment_closure.py`、`nodes/experiment/tests/test_contract_audit.py`、`nodes/experiment/tests/test_blocked_operation_closure.py`。
- **是否适合静态检查**：部分适合。可禁止跨节点消费者直接访问本节点 artifact 目录，并检查共享 schema/typed contract；“哪份输入属于本 run”和真实文件验收仍需行为测试。
- **剩余风险**：真实并发 amendment/dispatch 竞态、Data package 同 basename 不同相对路径、以及完整平台 dispatch→Experiment E2E 未被现有证据完全覆盖。

## BF-02 — 证据身份与审计通道

- **定义**：审计读取了错误 run、错误 canonical identity、可损展示摘要或框架自己生成的恢复文本，导致历史证据顶替当前证据、真实 receipt 丢失，或系统自我证明完成。
- **典型历史缺陷**：`EXP-REG-007`～`EXP-REG-011`：prior-run log/result 被当前 closure 计数、第二份 log 替换 canonical log、`_brief()` 截断声明、auto-recovery 文本自证 verdict/sediment、consumer 猜 manifest 文件名、execution record 依赖脆弱 chunk id。
- **系统不变量**：node-local closure 只能由当前 run 的唯一 canonical artifact 满足；机械审计读取完整 durable receipt；展示摘要不是事实源；framework-generated recovery 只能诊断和路由，不能成为科学证据；producer 的结构化返回值/immutable run binding 优先于文件名或 UI chunk。
- **容易出现的代码模式**：无 `produced_by_run_id` 过滤的 `list_artifacts(type)`；取 latest；`result_preview`/Markdown 正则作为权威；framework 写文本后由同一 audit 扫文本；consumer 手拼 artifact 文件名；拿可缺失 `chunk_id` 当唯一连接键。
- **当前测试覆盖**：`test_contract_audit.py` 的 current-run/duplicate/binding 用例，`test_declaration_receipt_audit.py`，`test_run_manifest.py`，以及 auto-generated log 的 sediment/receipt 负例。
- **是否适合静态检查**：较适合检查 audit 是否调用 canonical current-run helper、是否读取 receipt、是否禁止 consumer 拼 manifest 文件名；唯一性、版本冲突和 auto-generated 间接引用仍需运行测试。
- **剩余风险**：legacy artifact 缺 `produced_by_run_id` 时只有 fail-closed、缺专门迁移诊断；并发创建两份 canonical log 的原子性和 framework-generated artifact 的全类型间接引用尚无完整检查。

## BF-03 — 收尾状态机与终结权威

- **定义**：把 Experiment 收尾当一次性文本写入，而不是可验证、可恢复、幂等且能影响 Core 最终状态的事务。
- **典型历史缺陷**：`EXP-REG-012`～`EXP-REG-016`：合理 blocked operation 无合法终态、operation 私有 receipt 缺标准证据链、clean/receipt 分叉、partial write 重试重复产物、sediment 门在 freeze 后才暴露、audit 异常 fail-open。
- **系统不变量**：`blocked` 是合法但必须可验证的终态；raw→clean→log 的 identity/hash/binding 一致；一次 run 只有一个 closure owner；重复调用幂等、部分写入可续；所有 irreversible freeze 前可判的前置先预检；任一 closure audit 失败或异常必须留下 Core 可读 blocker。
- **容易出现的代码模式**：从 Markdown `status:` 猜生命周期；只描述 success 路径；多 writer；先后 `save/freeze` 无 closure id/owner/input snapshot；audit 只检查 artifact 存在不做 receipt cross-check；先写 transcript 再写 blocker；`except` 只改临时 `loop_result.status`。
- **当前测试覆盖**：`test_blocked_operation_closure.py`、`test_contract_audit.py` 的 operation 三件套与 tamper 用例、`test_sediment_closure.py`。audit-exception 的 Core final-status 保护目前仅在 `WORKTREE` 测试中。
- **是否适合静态检查**：部分适合。可检查 finalization failure path 必须调用统一 blocker helper、closure writer 唯一性和 schema 字段；事务恢复、幂等及 freeze 时序必须靠 fault-injection 测试。
- **剩余风险**：operation partial-write 测试没有参数化覆盖每个 save/freeze/transcript 断点；operation audit 与 transcript 同时异常的真实 `finalize_run` 集成路径仍缺直接测试；preview→freeze 的 TOCTOU 未测。

## BF-04 — 外部作业生命周期

- **定义**：把 scheduler job 生命周期、agent turn 生命周期和 Experiment 分析/冻结生命周期混为一层，导致作业丢失、误报完成、重复提交或永远等待。
- **典型历史缺陷**：`EXP-REG-017`～`EXP-REG-020`：缺通用持久 handoff；submit 后立即 stop/terminal 被当 completed；健康等待无安全交接且取消不响应；裸后台/裸 scheduler/bypass 绕开受管流程；高危批准没有把“原样重试”契约送回模型。
- **系统不变量**：真实 submit 成功即持久化 workflow identity；running/unknown/terminal 都不等于 workflow finalized；必须检查输出、冻结 log 并显式 finalize；开放 workflow 必须阻止 Core `completed`；长任务只能经受管 submit/status/handoff；批准只授权同一次完全相同的调用。
- **容易出现的代码模式**：只在 `on_end` 写 footer；把 scheduler 无记录或 terminal 当成功；模型 stop 后不再插 gate；只有 transcript/task 没有 Core blocker；允许 `nohup`/`setsid`/`&`/裸 `sbatch`；bypass 提前跳过路径和后台 guard；批准后允许参数漂移。
- **当前测试覆盖**：`nodes/experiment/tests/test_external_job_handoff.py`、`test_boundary_guard.py`、`test_timeout_escalation.py`、`tests/test_highrisk_approval_retry_contract.py`。healthy handoff、scheduler probe cancellation 与 Core blocker 目前含 `WORKTREE` 用例。
- **是否适合静态检查**：部分适合。可检查 real-submit 成功分支是否立即持久化，以及所有 shell 入口是否调用统一语义 guard；完整生命周期必须用状态机和集成测试。
- **剩余风险**：无真实 Slurm/PBS/Kubernetes stop→terminal→finalize E2E；submit side effect 与 workflow artifact 持久化不是同一事务；静态 payload 路径门不能证明任意二进制内部写路径；同步 on-end/gate probe 仍有约 10–12 秒取消延迟，多并发 handoff 未专测。

## BF-05 — 进程与超时生命周期

- **定义**：子进程的完成、超时、取消与进程树回收判据不一致，或把一次 timeout 当作无上下文终局。
- **典型历史缺陷**：`EXP-REG-021`～`EXP-REG-024`：cancel 只在 turn boundary 生效；晚取 PGID/只杀 shell/无界 drain；zombie 被测试当 live；同一慢目标换命令反复超时且丢 partial progress。
- **系统不变量**：sticky cancellation 能抢占当前等待；timeout/cancel 后整组终止且有界返回；zombie 是 terminated；partial output 与目标身份必须保留，同目标的重复同步尝试应被熔断并给出受管出口。
- **容易出现的代码模式**：`wait_for(proc.communicate())` 不监听 cancel；kill 时才 `getpgid`；只 `proc.kill()`；kill 后无界 await；`os.kill(pid, 0)` 直接当业务 liveness；timeout 返回裸 `{status}`；ledger key 使用完整命令字符串。
- **当前测试覆盖**：`nodes/experiment/tests/test_timeout_escalation.py`、`test_build_resource_guard.py`、`tests/test_subprocess_kill.py`。当前工作树另覆盖全部 safe Python 必经 cgroup supervisor、动态 Popen 命中 PID 限制、Python 终态标签和常见数值库线程预算为 4。仍没有在活的 `safe_run_bash`/`safe_execute_python` 中设置 `kill_event` 的完整取消链路测试。
- **是否适合静态检查**：可检查 `start_new_session`、group killer 和 bounded wait 是否配对，并禁止测试用裸 `kill(0)` 充当 liveness；真实进程树与竞态仍需运行测试。
- **剩余风险**：live bash/python cancellation 缺直接 regression；`safe_run_bash` 正常成功路径中“后代继承 pipe”没有直接测试；safe Python 虽已复用同一执行/资源 supervisor，尚无真实 cgroup OOM/PID 硬事件、Swap 压力事件与 kill-event 组合竞态测试。

## BF-06 — 命令语义

- **定义**：用 substring/regex 代替 Bash 或 Python 语义分析，混淆“文本提及”和“实际执行/写入”；让路径门、路线入口和资源门分别猜同一命令；或 analyzer 缺失/异常时放行。
- **典型历史缺陷**：`EXP-REG-025`～`EXP-REG-027`、`EXP-REG-043`～`EXP-REG-044`：scheduler/MPI 关键字误报与包装执行漏报、probe segment 掩盖生成、Python read 被当 write、Bash analyzer fail-open、合法静态嵌套被路线解析器误判 unknown，以及把 Python AST 当完整资源边界。
- **系统不变量**：只把 executable command position 当执行；Bash cwd、路径事件与路线入口基于同一 AST 事件模型，路线入口只有一个投影器；读取与写入按 AST/API/mode 区分；dynamic/parse failure/analyzer unavailable 必须 fail-closed；AST 只负责可证明的静态事实，不替代 OS sandbox、cgroup 或平台隔离。
- **容易出现的代码模式**：`re.search("sbatch|srun|mpirun")`；整条复合命令因出现 `--version` 就白名单；多份 `split/shlex/regex` 分别推路线与路径；`if path in code and subprocess/open in code`；`except Exception: return False/safe`。
- **当前测试覆盖**：既有 timeout/preprocessing/boundary/resource/path corpus 继续保留。当前工作树新增 `test_bash_path_events.py`，覆盖 pipeline、子 shell、函数、字面 `shell -c`/`eval`、重定向入口 cwd、分支传播、混合未知变量及透明/委托调度角色；`test_route_shadow_wiring.py` 验证路径与路线共同消费结构化事件、可信 sidecar 折叠和多主 payload 保守分类。Python corpus 覆盖 read/write 区分和常见进程别名，但不把它们当完整安全证明。
- **是否适合静态检查**：适合禁止新增独立 regex/shlex 入口分类器、检查所有 Bash 入口必须走统一 analyzer，并禁止异常返回 safe；不适合用静态规则代替命令语义 corpus。
- **剩余风险**：运行期展开、trap、复杂 shell option、远端解释器语义与任意二进制内部写入无法由静态分析完备证明；submission 分析异常现已 fail-closed。Python 动态文件目标由逐调用 sandbox 约束，动态派生进程由 cgroup/整树取消约束。

## BF-07 — 路径与工作目录权威

- **定义**：把 artifact/evidence、命令文本或目录名称误当路径权限与工作目录事实，或让输出位置、静态路径检查和实际 OS 写能力从不同锚点派生。
- **典型历史缺陷**：`EXP-REG-028`～`EXP-REG-031`、`EXP-REG-042`～`EXP-REG-044`：agent artifact 自授/自锁 path role、无法解析的破坏性目标被当无写入、输出落第二棵树、inline `cd` 失败、bypass 越过硬边界、shell payload 目标漏检及 Python 动态写继承宽目录。
- **系统不变量**：evidence 不授予 authority；path roles 只来自 trusted node inputs/hook/human/default；调用参数不能替代 payload 实际目标；硬路径事实不可 bypass；无法证明 containment 的目标不执行；output anchor=write anchor=节点 worktree。Python 根文件系统默认只读，只按本次角色和已确认目标恢复精确写面。
- **容易出现的代码模式**：从 artifact metadata 反推 role；相对/list declaration 静默丢弃；`/tmp` 或用户 cache blanket allow；重复手拼 outputs；无绑定 worktree 时发明 `<project>/workspace`；依赖 shell 最后一条退出码；executor 自动 mkdir typo cwd；sandbox 先恢复宽根再尝试局部只读。
- **当前测试覆盖**：`test_path_role_authority.py`、`test_path_roles.py`、输出锚点测试、`test_required_workdir.py`、`test_path_boundary_regressions.py`；当前工作树另覆盖 hard-scope bypass、Bash/submit 实际目标的零副作用阻断，以及 Python 路径角色重叠、框架状态/源码/依赖只读覆盖、公共 `/tmp` 与用户 cache 不进入 writable roots、source worktree 仅按本次已确认目标恢复写能力。
- **是否适合静态检查**：较适合检查 artifact→authority、重复 output path 拼装、系统根宽放行，以及执行器是否调用统一 cwd/role resolver；动态 shell/Python target、overlay 顺序和 TOCTOU 仍需行为测试。
- **剩余风险**：fake-workspace 与部分 list-form role 仍偏 helper-level；结构化 AST 已覆盖函数/子 shell cwd，但运行期生成脚本和任意二进制内部路径仍不可静态证明。Python bwrap/mount 隔离只约束文件系统视图；执行后替换 symlink 的 TOCTOU 和 native extension 的设备/网络能力需独立防线。

## BF-08 — 调度器拓扑与可移植性

- **定义**：把 submit host、scheduler、compute node 和 container 的路径、日志、identity 与文件可见性当成同一环境，或把“探测不了/可探测”分别误作“身份无效/提交契约可用”。
- **典型历史缺陷**：`EXP-REG-032`～`EXP-REG-034`、`EXP-REG-043`：日志宣告与实际重定向分叉、bootstrap 错误不可见、local/Kubernetes 路径误用、stage-in 批准不绑定内容、无 `getent` 被判身份无效、preview 截掉 payload，以及 submission 只检查参数不检查 shell 可见目标。
- **系统不变量**：宣告日志和脚本写入来自同一命名源；bootstrap 失败可观察但不算 progress；local 不越 host write boundary；Kubernetes 必须先有权威 PVC/volume、容器挂载点与输入输出映射契约；stage-in approval 绑定 digest+mode；unknown identity 与 invalid identity 分离；preview 保留真实 payload。
- **容易出现的代码模式**：脚本和 result 各拼一套 filename；submit 端 mkdir 远端路径；把 host absolute path 传入 Pod；因 `kubectl` 可用便自动选择 Kubernetes；approval 只含 source path；硬依赖 `getent`；固定 `script[:N]`。
- **当前测试覆盖**：`test_path_boundary_regressions.py` 和 `test_identity_preflight_portability.py` 主要覆盖 dry-run、渲染或 mocked health；当前工作树另验证通过通用静态有效性检查的 Kubernetes 在无 volume contract 时于 route、目录、脚本、intent、submit 前返回 `kubernetes_volume_contract_required`，以及自动推荐只按 SLURM → PBS → local 选择。PBS redirect 仍仅 helper-level 参数化。
- **是否适合静态检查**：部分适合检查共享日志模板、Kubernetes 在契约缺失时零物化、自动推荐不选 K8s、approval payload 含 digest/mode；真实 topology 与目录服务需集成测试。
- **剩余风险**：没有真实 Slurm/PBS/Kubernetes 执行级测试；当前 Kubernetes 是安全停用，不是 volume 支持。恢复它需要框架/平台契约与真实 Pod E2E；stage-in 人工确认到 copy/submit 的内容 TOCTOU、真实远端目录服务、逐作业 PID/磁盘配额仍未集成。

## BF-09 — 调用方契约漂移

- **定义**：实现、工具 schema、节点授权、Harness、prompt/skill 或节点文档分别维护接口事实，最终出现“声明了却拿不到”“实现会读但调用方不能传”“说明要求与 validator 不同”。
- **典型历史缺陷**：`EXP-REG-035`～`EXP-REG-038`：`safe_execute_python` 因继承 allowlist 14 天不可达；description/validator 和幽灵工具名漂移；`input_package_artifact_id` 漏 schema；旧 `quality_checks`、curator 默认链和 Experiment 最终 verdict 文档漂移。
- **系统不变量**：Harness 声明的工具必须真实授予节点；实现读取/拒绝的 public 参数必须在所有注册分支 schema 中可见；调用前说明与 validator 来自同一 source of truth；架构删除或裁决权迁移必须同步所有节点文档。
- **容易出现的代码模式**：`dataclasses.replace` 继承旧 `allowed_node_types`；只改正常或 fallback schema 之一；`kw.get(...)` 没有 schema 字段；手写 description + 手写 error；名单式测试；重构后保留旧工具、层或 owner 名称。
- **当前测试覆盖**：`tests/test_declared_tools_are_granted.py`、`tests/test_tool_contracts_reach_the_caller.py`、`nodes/experiment/tests/test_harness_contract.py`。schema 与节点文档新断言目前部分仅在 `WORKTREE`。
- **是否适合静态检查**：高度适合。应对账 live registry/harness grants、executor signature/`kw.get` 与全部 schema 分支，并扫描文档中的 tool/layer/owner 引用；语义性调用顺序仍需行为测试。
- **剩余风险**：fallback 注册分支没有直接执行测试；当前通用 contract 扫描依赖可识别源码/文案形态；文档测试只钉选定关键句，不证明全文无漂移。

## BF-10 — 进程全局状态与测试隔离

- **定义**：并行 run 或测试模块通过进程全局状态相互污染，使结果取决于调度和 import 顺序。
- **典型历史缺陷**：`EXP-REG-039`、`EXP-REG-040`：并行 `safe_execute_python` 互串 `EXPERIMENT_RUN_ROOT/PYTHONPATH`；Experiment `conftest` 永久 monkeypatch `register_tool`。
- **系统不变量**：每个 child 只继承本 run 的环境；任何临时全局 mutation 必须在 await 全程串行且 finally 恢复；测试 fixture 不得在 import 时永久修改全局 registry。
- **容易出现的代码模式**：await 跨越裸 `os.environ` mutation；module-level monkeypatch；不恢复 registry；测试成功依赖 collection order。
- **当前测试覆盖**：`test_safe_execute_python_repo_path.py::test_parallel_safe_python_calls_keep_run_local_environment`；全局 monkeypatch 修复主要由提交补丁、当前 `conftest.py` 的历史注释和 registry 测试侧面保护。
- **是否适合静态检查**：适合扫描 await 跨越全局 env mutation、测试模块顶层 monkeypatch/registry 写入；真实并发行为仍需测试。
- **剩余风险**：env 并发测试使用 monkeypatched executor，没有启动两个真实 Python child；没有专门防止未来 Experiment conftest 再引入 import-time registry patch 的节点级测试。

## BF-11 — 资源可行性顺序

- **定义**：先 configure/compile 锁定 MPI/GPU/ABI/backend，之后才发现 Core 能力或实时资源不支持；或虽然有限制，却让库的隐式线程/子进程在任务边界内自我耗尽。
- **典型历史缺陷**：`EXP-REG-041`、当前工作树的 `EXP-REG-044`。
- **系统不变量**：任何会锁定资源/ABI 的构建前，必须由 Core 声明与 live resource snapshot 形成可行计划；fixed 约束不满足时 build 前阻断；任何本地实际执行，包括轻量 Python，都必须有任务级 PID/内存/时间边界，轻量 Python 的数值库线程预算必须与 PID 预算相容。
- **容易出现的代码模式**：只在 prompt 建议调用 preflight；compile/configure 入口不机械检查 plan；资源发现和 build routing 各自维护判断；只设 `TasksMax` 却不限制 OpenMP/BLAS 隐式线程池。
- **当前测试覆盖**：既有 preflight 测试覆盖 fixed blocked/pause 与 MPI capability；当前工作树新增 build guard 强制接线、PID/内存/磁盘事件、全部 safe Python 必经 supervisor，以及 `OMP_NUM_THREADS`、OpenBLAS、MKL、NumExpr、Accelerate、BLIS 等线程环境统一为 4 的断言。
- **是否适合静态检查**：有限。可检查 build/Python 入口必须调用 guard、线程预算变量是否成组设置并恢复；容量、队列和资源可用性只能在运行时验证。
- **剩余风险**：当前增强尚未提交，不能当作历史基线；真实资源变化、scheduler queue、远端逐作业 cgroup/quota、大内存 Python 压力和线程库忽略环境变量的情况仍无完整集成覆盖。

## BF-12 — 拒绝不可执行（门只说违规，不说合法形式）

- **定义**：门的判定本身正确、schema 与实现也没有漂移，但拒绝消息只陈述"你错了"，不携带调用方据以改对所需的信息——合法取值集合、字段语义，或框架当场已经算出来的那个正确值。模型于是把试错当成唯一的学习通道。
- **典型历史缺陷**：`EXP-REG-045`～`EXP-REG-047`（2026-09-01/02 `e2e_realistic_scientific` 真实运行取证）：`storage_binding.roles` 只报"含不支持的语义角色"而不列 `input/build/run/output/logs`，节点误把 path_role 当键、耗 22 轮后靠猜通过；`raw_results` 冻结当场算出实际 sha256、比完丢弃、只报 `mismatch`，节点耗 20+ 轮并为取这一个值往科学路线 DAG 里插了 `hash_outputs` 步骤，污染路线语义；`program_sequence` 失配时不分成因，对"命令本就不是复合命令"也报"不要增删、重排入口"，节点据此得出"submit_job 不接受该参数"的错误结论。
- **系统不变量**：面向模型的封闭词表，其合法取值必须在**调用前**经 `parameters_schema` 的 `enum` 送达，或在**拒绝时**由消息携带（插值该常量，或逐字列全成员）；框架在判定过程中已经计算出的正确值（实际摘要、实际字节数、实际派生序列）必须随拒绝一起报出——调用方对自己 run_root 内的产物本就有读权限，隐瞒它不构成防线，只是把成本转嫁成试错；同一个 kind 的失配若有多种成因，提示必须按成因分流，宁可不给提示也不能给错误成因的提示。
- **容易出现的代码模式**：`errors.append(f"...含不支持的 X：{bad}")` 而不带合法集合；`if computed != declared: errors.append("mismatch")` 丢弃 `computed`；`parameters_schema` 里把封闭词表写成裸 `{"type": "object"}` / `{"type": "string"}`；一个 `hint` 覆盖多种成因；只在 docstring 或模块常量里定义词表、消息侧完全不提。
- **当前测试覆盖**：`test_harness_contract.py::test_model_facing_vocabularies_tell_the_caller_the_legal_values` 机械核对已登记词表的送达通道（schema enum / 插值 / 逐字列全），配套 `::test_the_vocabulary_detector_is_not_vacuous` 用两个非模型面词表当阴性对照防止检测器变松；行为侧由 `test_execution_envelope.py` 的键值写反与值域用例、`test_contract_audit.py` 的 sha256 实际摘要断言、`test_execution_route.py` 的 `program_sequence` 双支提示断言分别钉住。
- **是否适合静态检查**：高度适合，且已实现。词表送达通道可由 AST 机械核对；难点只在"哪些词表面向模型"仍需人工登记（新增时进 `_MODEL_FACING_VOCABULARIES`）。"消息成因是否正确"和"已算出的值是否报出"目前只能靠逐门行为测试，不宜静态化。
- **剩余风险**：登记表是人工维护的，新增封闭词表若忘记登记则不受保护；非词表类拒绝（格式、顺序、状态机前置）尚无统一检查；真实代价只有在完整 E2E 里才显形——两轮之间唯一变量就是消息措辞时，代价差是 1 轮 vs 20+ 轮，单元测试无法复现这个量级。
