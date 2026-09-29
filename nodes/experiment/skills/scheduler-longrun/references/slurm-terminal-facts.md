# Slurm 终态事实（scheduler-longrun / references）

<!-- facts-format: v1 -->

**性质**：只有事实与判据，没有操作步骤，不含任何站点路径、分区名或 module 名。
**谁叫模型读它**：节点 hook `experiment_scheduler_facts_router`（on_turn_end）在
`check_external_job_health` / `wait_for_external_job` / `finalize_external_job` 返回
`scheduler_phase = terminal` 且 `scheduler = slurm` 时，对每个作业点名一次
`load_skill(name='scheduler-longrun', asset='references/slurm-terminal-facts.md')`。
**来源口径**：SchedMD 在线文档（站点导航标 Version 26.05；页面不印修改日期），抓取
2026-09-21，每条事实的「原文」是页面逐字摘录。§4 的样本分两组：真实集群（node20，分区 main，
两个成功作业，2026-09-21）验证了 COMPLETED 的 allocation / step 行形状、MaxRSS 只在 step 行、`-X` 丢 step 行、
数组的两个 allocation 行；被杀终态（TIMEOUT / CANCELLED / NODE_FAIL / OOM）的样本只来自 Docker 单机
Slurm 23.11.4（Ubuntu 24.04 发行包），**未在真实集群上验证**。

## 0. 判据（先看这里）

1. 终态由 **allocation 行的 State** 决定；ExitCode 只在 State 是 COMPLETED / FAILED 时是程序自己的退出码。
2. **ExitCode 低位为 0 不是成功**：TIMEOUT / CANCELLED / NODE_FAIL / PREEMPTED / DEADLINE / BOOT_FAIL 都常见 `0:0`（S05、S11–S14、S20）。
3. `State` 可能带后缀（`CANCELLED by 1000`、`CANCELLED+`），只取第一个词（S09）。
4. 一条 COMPLETED 记录可能盖住同一 JobID 之前失败的运行（S16、S17）；作业不在 sacct 里也不等于没跑过（S18）。
5. step 行（`123.batch`、`123.0`）的 OOM / 信号是诊断证据，不改变 allocation 行的结论（S06、S08、S15）。
6. OOM 不一定写成 OUT_OF_MEMORY：OverMemoryKill 杀掉的作业记 FAILED 0:9，证据只在 stderr（§4 O3）；不带原因的 `*** JOB … CANCELLED AT … ***` 横幅在 OverMemoryKill 下同样出现。

## 1. 事实

### S01 sacct 的作业状态词表（长名 / 短名）
- 事实：sacct 文档 JOB STATE CODES 列出 BOOT_FAIL/BF、CANCELLED/CA、COMPLETED/CD、DEADLINE/DL、FAILED/F、NODE_FAIL/NF、OUT_OF_MEMORY/OOM、PENDING/PD、PREEMPTED/PR、RUNNING/R、REQUEUED/RQ、RESIZING/RS、REVOKED/RV、SUSPENDED/S、TIMEOUT/TO。逐字定义：BOOT_FAIL "Job terminated due to launch failure, typically due to a hardware failure (e.g. unable to boot the node or block and the job can not be requeued)"；CANCELLED "Job was explicitly cancelled by the user or system administrator. The job may or may not have been initiated"；COMPLETED "Job has terminated all processes on all nodes with an exit code of zero"；DEADLINE "Job terminated on deadline"；FAILED "Job terminated with non-zero exit code or other failure condition"；NODE_FAIL "Job terminated due to failure of one or more allocated nodes"；OUT_OF_MEMORY "Job experienced out of memory error"；PREEMPTED "Job terminated due to preemption"；REQUEUED "Job was requeued"；REVOKED "Sibling was removed from cluster due to other cluster starting the job"；TIMEOUT "Job terminated upon reaching its time limit"。`-s/--state` 接受长名或短名，大小写不敏感。
- 来源：https://slurm.schedmd.com/sacct.html（Version 26.05，抓取 2026-09-21）
- 原文："Either the short or long form of the state name may be used (e.g. CA or CANCELLED) and the name is case insensitive (i.e. ca and CA both work)."
- 会说谎：只按这张表做穷举匹配会漏掉只在活控制器里出现的状态（squeue 的 CF、CG、SI、SE、ST、RD、RF、RH、SO，见 S02）以及带 `+` 后缀的截断状态（S09）。
- 节点侧：`resource_manager._SLURM_TERMINAL_STATES` = COMPLETED、FAILED、CANCELLED、TIMEOUT、OUT_OF_MEMORY、NODE_FAIL、BOOT_FAIL、DEADLINE、PREEMPTED、REVOKED、SPECIAL_EXIT；其中只有 COMPLETED、FAILED 算"作业自己跑到头"，其余都算被调度器结束（`_SLURM_SCHEDULER_KILL_STATES` 取补集）。SPECIAL_EXIT 不在 sacct 词表里，是 scontrol / squeue 的渲染（S17）。

### S02 活控制器（squeue）多出的状态
- 事实：squeue 文档在 sacct 词表之外还列出 CONFIGURING/CF、COMPLETING/CG、RESV_DEL_HOLD/RD、REQUEUE_FED/RF、REQUEUE_HOLD/RH、SIGNALING/SI、SPECIAL_EXIT/SE、STAGE_OUT/SO、STOPPED/ST。
- 来源：https://slurm.schedmd.com/squeue.html（Version 26.05，抓取 2026-09-21）
- 原文："CG COMPLETING Job is in the process of completing. Some processes on some nodes may still be active."
- 会说谎：CG 不是终态——输出文件可能还在写，State/ExitCode 尚未定；squeue 里的 RQ 意思是 "Completing job is being requeued"，即它还会再跑一遍。

### S03 COMPLETED 与 FAILED 的含义
- 事实：COMPLETED = 所有节点上的所有进程以退出码 0 结束；FAILED = 非零退出码或其他失败条件（定义在 sacct / squeue 的 JOB STATE CODES）。批处理脚本任何非零退出码都判为作业失败，State 记 FAILED、Reason 记 NonZeroExitCode（job_exit_code 页）。
- 来源：https://slurm.schedmd.com/sacct.html 、 https://slurm.schedmd.com/job_exit_code.html（Version 26.05，抓取 2026-09-21）
- 原文："Any non-zero exit code will be assumed to be a job failure and will result in a Job State of FAILED with a Reason of \"NonZeroExitCode\"."
- 会说谎：COMPLETED 只证明批处理脚本退出码为 0。job_exit_code 页自己就提醒：作业的核心任务失败而脚本返回 0 的情况存在，所以 `COMPLETED / 0:0` 不是科学结果正确的证据。

### S04 ExitCode 记的是什么
- 事实：sbatch 作业记的是批处理脚本的退出状态；salloc 记的是结束会话的 exit；srun 记的是命令的返回值。值是 8 位无符号数 0–255，负数按无符号显示。
- 来源：https://slurm.schedmd.com/job_exit_code.html（Version 26.05，抓取 2026-09-21）
- 原文："For sbatch jobs, the exit code that is captured is the output of the batch script."
- 会说谎：脚本最后一条命令成功（尾部 echo、清理）会盖住前面失败的步骤；超过 255 或为负的退出码会回绕，看到的 `1` 未必是程序原本的码。

### S05 ExitCode 的形状是 `退出码:信号`
- 事实：ExitCode 打印为冒号分隔的两个数：第一个是 exit() 设置的退出码，第二个是导致终止的信号号，只有被信号终止时才非零。`0:0` = 正常退出 0；`1:0` = 退出 1；`0:9` / `0:15` = 被 SIGKILL / SIGTERM 杀。
- 来源：https://slurm.schedmd.com/scontrol.html 、 https://slurm.schedmd.com/job_exit_code.html（Version 26.05，抓取 2026-09-21）
- 原文："The first number is the exit code, typically as set by the exit() function. The second number of the signal that caused the process to terminate if it was terminated by a signal."
- 会说谎：从未跑完脚本的作业也是 `0:0`（启动前被取消、NODE_FAIL、BOOT_FAIL）；信号位只说 Slurm 或内核发了什么信号（SIGTERM / SIGKILL），不说原因——原因（超时、抢占、scancel）只在 State 里。
- 节点侧：`_slurm_exit_status` 按 `-?\d+(:-?\d+)?` 解析；解析不出（空字段）时 returncode 记 None，不落到产物兜底（046）。

### S06 信号按作业与 step 分别记录
- 事实：作业或 step 被信号终止时，信号号记在**该**作业或 step 的记录里；每个 srun step 的退出码单独存在 step 记录中，与作业级 ExitCode 分开。
- 来源：https://slurm.schedmd.com/job_exit_code.html（Version 26.05，抓取 2026-09-21）
- 原文："When a signal was responsible for a job or step's termination, the signal number will be displayed after the exit code, delineated by a colon(:)."
- 会说谎：step 行 `0:9`（被杀）而批处理脚本忽略失败并 exit 0 时，作业级 ExitCode 仍是 `0:0`；只看作业级会漏掉 step 级的被杀。
- 节点侧：`_slurm_accounting_facts` 用 JobIDRaw 不含 `.` 的 allocation 行判终态与成功，step 行只提供 OOM 与 MaxRSS。

### S07 DerivedExitCode 与 ExitCode 不是一个字段
- 事实：DerivedExitCode 是作业各 step（srun 调用）返回的最高退出码，冒号后是信号；它可以在作业结束后用 sacctmgr modify job 或 sjobexitmod 修改。job_exit_code 页的例子：State COMPLETED、ExitCode `0:0`、DerivedExitCode `49:0`（sjobexitmod -e 49 之后）。
- 来源：https://slurm.schedmd.com/sacct.html 、 https://slurm.schedmd.com/job_exit_code.html（Version 26.05，抓取 2026-09-21）
- 原文："The highest exit code returned by the job's job steps (srun invocations). Following the colon is the signal that caused the process to terminate if it was terminated by a signal."
- 会说谎：它事后可改，可能反映的是人工批注而不是运行本身；没有 srun step 的作业它一直是 `0:0`，哪怕脚本自己的命令失败。

### S08 `-X/--allocations` 会丢 step 行
- 事实：`-X` 只显示作业分配本身的统计，不含 step（`.0`、`.batch`、`.extern`）；不含 step 时资源利用统计（如 MaxRSS）报 0。
- 来源：https://slurm.schedmd.com/sacct.html（Version 26.05，抓取 2026-09-21）
- 原文："-X, --allocations Only show statistics relevant to the job allocation itself, not taking steps into consideration. NOTE: Without including steps, utilization statistics for job allocation(s) will be reported as zero."
- 会说谎：加了 `-X`，只出现在 `.0` / `.batch` 行的 step 级 OOM 或信号杀就看不见；资源用量为 0 是选项的副作用，不是作业没干活的证据。
- 节点侧：`_slurm_accounting_argv` 不带 `-X`（`sacct -n -P -j <id> -o JobIDRaw,State,ExitCode,Partition,NodeList,AllocCPUS,ReqMem,ElapsedRaw,MaxRSS`），所以 step 行在，OOM / MaxRSS 从 step 行取。

### S09 State 列的 `+` 截断与 `CANCELLED by <uid>`
- 事实：State 的信息装不下列宽时（例如取消作业的 UID），状态后面跟一个 `+`；用 `%NUMBER` 格式修饰符加宽可显示完整文本。文档没有给出 `CANCELLED by <uid>` 的字面渲染，只说多出来的信息是取消者的 UID；字面形状见 §4 样本。
- 来源：https://slurm.schedmd.com/sacct.html（Version 26.05，抓取 2026-09-21）
- 原文："If more information is available on the job state than will fit into the current field width (for example, the UID that CANCELLED a job) the state will be followed by a \"+\"."
- 会说谎：对 `CANCELLED` 做整串精确匹配会在 `CANCELLED+` 或 `CANCELLED by 0` 上失败；`by <uid>` 只说谁发了 scancel，不说为什么（用户主动、管理员、还是抢占 / 超时相关的系统取消）。
- 节点侧：`_slurm_state_name` 取第一个空白分隔的词并去掉尾部 `+`。

### S10 `--state=RUNNING` 查询会带回已结束的作业
- 事实：选 `-s RUNNING` 也会返回 SUSPENDED 的作业，并且会返回在查询时间窗内**结束**（取消或其他）的作业，因为它们在窗内也曾 RUNNING；PENDING 作业只有 EligibleTime 落在窗内才被选中，`--hold` 提交的作业 EligibleTime 是 Unknown。
- 来源：https://slurm.schedmd.com/sacct.html（Version 26.05，抓取 2026-09-21）
- 原文："NOTE: The RUNNING state will return any jobs completed (cancelled or otherwise) in the time period requested as the job was also RUNNING during that time."
- 会说谎：出现在 `--state=RUNNING` 列表里不等于现在还在跑。

### S11 TIMEOUT 的机制
- 事实：TIMEOUT = 作业到达时间限制被终止（S01）。到点时每个 step 的每个任务先收 SIGTERM，KillWait 秒后收 SIGKILL（默认 30 秒，最大 65533）。配置了 OverTimeLimit 时时间限制变成软限制，作业在软限制 + OverTimeLimit 才被取消。
- 来源：https://slurm.schedmd.com/slurm.conf.html（KillWait、OverTimeLimit；Version 26.05，抓取 2026-09-21）
- 原文："The interval, in seconds, given to a job's processes between the SIGTERM and SIGKILL signals upon reaching its time limit. If the job fails to terminate gracefully in the interval specified, it will be forcibly terminated. The default value is 30 seconds."
- 会说谎：作业捕获 SIGTERM 并在 KillWait 内 exit 0，仍记 TIMEOUT 而 ExitCode 可能是 `0:0`；被 SIGKILL 的是 `0:9`——两种 ExitCode 都分不出"跑完了"和"被掐断"；有 OverTimeLimit 时作业可以超过名义 TimeLimit 而不记 TIMEOUT。
- 节点侧：046 把 TIMEOUT 归入调度器结束，`succeeded=False`、`scheduler_terminated=True`、`termination_matched=False`，不论 ExitCode 是 `0:0` 还是 `0:15`。

### S12 CANCELLED 的含义与 scancel 的信号序列
- 事实：CANCELLED = 用户或管理员显式取消，作业可能启动过也可能没有（S01）。不带 `--signal` 的 scancel 先向所有 step 发 SIGCONT（唤醒），再发 SIGTERM，等 KillWait，未结束再发 SIGKILL；对整个作业发 KILL 信号只取消活动 step，不取消作业本身。
- 来源：https://slurm.schedmd.com/scancel.html 、 https://slurm.schedmd.com/sacct.html（Version 26.05，抓取 2026-09-21）
- 原文："This will send first a SIGCONT to all steps to eventually wake them up followed by a SIGTERM, then wait the KillWait duration defined in the slurm.conf file and finally if they have not terminated send a SIGKILL."
- 会说谎：CANCELLED + `0:0` + 无 Start 时间 = 从未运行；CANCELLED 有 Start 时间 = 可能已部分运行并写了输出；`scancel -s KILL <jobid>` 不产生 CANCELLED 作业。

### S13 NODE_FAIL、BOOT_FAIL 与节点故障后的重排
- 事实：NODE_FAIL = 一个或多个分配节点故障导致作业终止；BOOT_FAIL = 启动失败（典型是硬件，无法重排）。作业的 Requeue={0|1} 标志规定节点故障后是否重排（scontrol 页）；默认任一节点故障就终止整个分配，`--no-kill` 改变这一默认（sbatch 页）。
- 来源：https://slurm.schedmd.com/sacct.html 、 https://slurm.schedmd.com/scontrol.html 、 https://slurm.schedmd.com/sbatch.html（Version 26.05，抓取 2026-09-21）
- 原文："NF NODE_FAIL Job terminated due to failure of one or more allocated nodes."
- 会说谎：NODE_FAIL 通常 ExitCode `0:0`，因为脚本没返回过；Requeue=1 时同一 JobID 会再跑一次，NODE_FAIL 那条记录变成隐藏的重复记录（S16），可见记录可能显示 COMPLETED。

### S14 PREEMPTED 与 PreemptMode
- 事实：PREEMPTED = 因抢占终止。按 PreemptMode，被抢占作业被取消（CANCEL）、能重排则重排否则取消（REQUEUE）、或挂起后恢复（SUSPEND / GANG）。有 GraceTime 时先发 SIGCONT + SIGTERM，到新结束时间再走 SIGCONT / SIGTERM / SIGKILL 序列。
- 来源：https://slurm.schedmd.com/preempt.html 、 https://slurm.schedmd.com/sacct.html（Version 26.05，抓取 2026-09-21）
- 原文："A job selected for preemption that exits before GraceTime expires will be handled according to PreemptMode regardless of why it exited (most visible if PreemptMode=REQUEUE)."
- 会说谎：被选中抢占、又在 GraceTime 内自己结束（真跑完或真失败）的作业，按 PreemptMode 处理——可能被记成 / 重排成"被抢占"；SUSPEND 模式下作业是 S 不是 PR。

### S15 OUT_OF_MEMORY 的判定来源
- 事实：OUT_OF_MEMORY = 作业遇到内存不足错误（S01）。task/cgroup 的 ConstrainRAMSpace 下，内核 OOM 杀掉 step 里一个或多个进程，step 状态标 OOM，但 step 本身继续运行，其余进程可能继续（cgroup.conf 页）；JobAcctGatherParams=OverMemoryKill 则杀掉整个 step，它基于轮询、有延迟（slurm.conf 页）；TaskPluginParam=OOMKillStep 让任一任务的 OOM 事件杀掉整个 step（需 task/cgroup、cgroup v2、内核新于 4.19；不作用于 extern step）。
- 来源：https://slurm.schedmd.com/cgroup.conf.html 、 https://slurm.schedmd.com/slurm.conf.html 、 https://slurm.schedmd.com/sacct.html（Version 26.05，抓取 2026-09-21）
- 原文："The step state will be marked as OOM, but the step itself will keep running and other processes in the step may continue to run as well. This differs from the behavior of OverMemoryKill, where the whole step will be killed/cancelled."
- 会说谎：cgroup 约束下一个 rank 被 OOM 杀、step 照常跑到头，脚本 exit 0，作业 COMPLETED 而 step 行 OUT_OF_MEMORY；OverMemoryKill 按轮询到的 RSS 杀，与内核 OOM 不是同一判据，两者的"OOM"不互相蕴含。
- 节点侧：`_slurm_accounting_facts` 的 `oom_killed` 来自任一行 State=OUT_OF_MEMORY，只作诊断证据；allocation 行 COMPLETED 时不否定成功（046 既有决定）。

### S16 重排 = 同一 JobID 从头再跑；sacct 默认只显示最新一条
- 事实：作业被重排时批处理脚本从头开始执行，JobID 不变（sbatch 页；SLURM_RESTART_COUNT 计数）；REQUEUED/RQ 是过渡状态。sacct 默认对同一 JobID 或 SLUID 只显示最新记录，重复记录来自重排、联邦、改大小或 MaxJobId 回绕；`-D/--duplicates` 显示全部；重排会重置 Submit 时间；Restarts 记重排 / 重启次数；SLUID 每次重排 / 重启 / 改大小都变。
- 来源：https://slurm.schedmd.com/sacct.html 、 https://slurm.schedmd.com/sbatch.html（Version 26.05，抓取 2026-09-21）
- 原文："By default, if multiple job records for the same job ID or SLUID match the request, only the most recent one will be shown. Duplicate records can result from requeues, federation, resizes or from jobs reaching the MaxJobId value and resetting."
- 会说谎：一条 COMPLETED 记录可能盖住同一 JobID 之前一次或多次失败 / 被抢占的运行，而那些运行可能已向同一批文件写了部分输出（`--open-mode` 决定追加还是截断）；Submit 时间被重置后按时间窗查询可能漏掉原始提交。

### S17 终态不是最终：scontrol requeue、RequeueExit、SPECIAL_EXIT
- 事实：scontrol requeue / requeuehold 能把运行中、挂起或**已结束**的批处理作业放回 pending（requeuehold 还置优先级 0 的 held；State=SpecialExit 时作业进入 JOB_SPECIAL_EXIT，scontrol show job 显示 SPECIAL_EXIT，squeue 显示 SE）。RequeueExit=<codes> 让以这些码退出的批处理作业自动重排，RequeueExitHold 则重排后 held 直到人工释放（slurm.conf 页）；Prolog 失败会重排并 held，除非 SchedulerParameters 配了 nohold_on_prolog_fail。
- 来源：https://slurm.schedmd.com/scontrol.html 、 https://slurm.schedmd.com/slurm.conf.html（Version 26.05，抓取 2026-09-21）
- 原文："requeue [<option>] <job_list> Requeue a running, suspended or finished Slurm batch job into pending state."
- 会说谎：T 时刻读到的 FAILED / COMPLETED 记录可能之后被同一 JobID 的新一次运行取代；看到 PENDING + SPECIAL_EXIT / held 的作业已经执行过一次并以匹配的码退出——"pending" 不等于"从未运行"。

### S18 slurmdbd 不可用时的记账延迟与丢失
- 事实：配置了 SlurmDBD 但它不响应时，slurmctld 把作业与 step 记账记录缓存在内部队列，恢复后再传；队列上限由 MaxDBDMsgs 决定（默认 10000，或 MaxJobCount*2 + 节点数*4 取大者）。溢出且 max_dbd_msg_action=discard 时先丢 step 的开始 / 结束消息，再丢作业开始消息，最后不再跟踪新消息，造成数据丢失和"失控作业"（runaway jobs）。
- 来源：https://slurm.schedmd.com/accounting.html 、 https://slurm.schedmd.com/slurm.conf.html（Version 26.05，抓取 2026-09-21）
- 原文："Note that if SlurmDBD is down long enough for the number of queued records to exceed the maximum queue size then messages will begin to be dropped."
- 会说谎：作业（或它的 step 行）不在 sacct 里不等于没跑过或还在跑：dbd 故障期间及之后记录可能晚到、缺 step 行、或永远不到；反过来只有开始记录没有结束记录的作业会一直显示 RUNNING。

### S19 控制器内存里的记录会被清（MinJobAge）
- 事实：MinJobAge（默认 300 秒）是已完成作业的记录从 slurmctld 内存清除前的最短年龄；backfill 周期内不清，所以可能超过 MinJobAge；0 表示不清。sbatch 的 afterok / afternotok 依赖必须在被依赖作业活跃期间或结束后 MinJobAge 秒内提交。
- 来源：https://slurm.schedmd.com/slurm.conf.html 、 https://slurm.schedmd.com/sbatch.html（Version 26.05，抓取 2026-09-21）
- 原文："The minimum age of a completed job before its record is cleared from the list of jobs slurmctld keeps in memory. Combine with MaxJobCount to ensure the slurmctld daemon does not exhaust its memory or other resources. The default value is 300 seconds."
- 会说谎：结束几分钟后 squeue 里没有、scontrol show job 报无效作业号，不是作业不存在或未结束的证据（推论：清除后只剩记账库能查到它）。

### S20 DEADLINE 与"系统取消"的两种 0:0
- 事实：DEADLINE = 在截止期限上终止（S01）；`--deadline` 下若无法在截止前结束（start > deadline - time）作业被移除。依赖永远无法满足的作业默认留在 PENDING、Reason=DependencyNeverSatisfied；`--kill-on-invalid-dep=yes`（或站点 kill_invalid_depend）则终止，状态 JOB_CANCELLED。
- 来源：https://slurm.schedmd.com/sbatch.html 、 https://slurm.schedmd.com/sacct.html（Version 26.05，抓取 2026-09-21）
- 原文："A terminated job state will be JOB_CANCELLED. If this option is not specified the system wide behavior applies. By default the job stays pending with reason DependencyNeverSatisfied"
- 会说谎：DEADLINE 和依赖取消都留下 ExitCode `0:0`、通常无 Start 时间的终态记录；不看 Start / Elapsed 就把 `0:0` 或 CANCELLED 读成"跑过又被停"是错的——它可能根本没执行过。

## 2. 节点代码对照

| 节点侧符号（`nodes/experiment/tools/resource_manager.py`） | 依据 |
|---|---|
| `_SLURM_ACCOUNTING_FIELDS` / `_slurm_accounting_argv`（`sacct -n -P`，不带 `-X`） | S06、S08 |
| `_SLURM_TERMINAL_STATES`（11 个） | S01、S17（SPECIAL_EXIT）、S20（DEADLINE） |
| `_SLURM_SELF_TERMINAL_STATES` = {COMPLETED, FAILED}；补集为被调度器结束 | S03、S05、S11–S14 |
| `_slurm_state_name`（首词、去 `+`） | S09 |
| `_slurm_exit_status`（`code:signal`；空字段 → None） | S04、S05、S18 |
| `oom_killed` 只作诊断，不否定 allocation 成功 | S06、S15 |
| `scheduler_terminated` ⇒ `succeeded=False` 且 `termination_matched=False` | S05、S11–S14、S20 |

## 3. 覆盖的 046 用例（`tests/test_cluster_job_state_truth.py`）

| State | ExitCode | 事实 |
|---|---|---|
| TIMEOUT | 0:15 | S11、S05 |
| TIMEOUT | 0:0 | S11 |
| CANCELLED by 1000 | 0:15 | S09、S12 |
| NODE_FAIL | 0:0 | S13 |
| PREEMPTED | 0:0 | S14 |
| DEADLINE | 0:0 | S20 |
| BOOT_FAIL | 0:0 | S13 |
| REVOKED | 0:0 | S01 |
| SPECIAL_EXIT | 0:0 | S17 |
| OUT_OF_MEMORY | 0:125 | S15 |
| COMPLETED | 0:0 | S03 |
| FAILED | 1:0 | S03、S04 |
| COMPLETED（allocation）+ OUT_OF_MEMORY（`.batch` step） | 0:0 / 0:125 | S06、S08、S15 |
| COMPLETED + TIMEOUT（数组的两个 allocation 行） | 0:0 | S11、S16 |
| TIMEOUT | （空） | S05、S18 |

## 4. 样本

### 4.1 真实集群（node20，分区 main，节点 gnr-cu03；Codex 2026-09-21 提交，042/06 号原文）

两个最小作业（1 CPU、256 MiB、`--time=00:02:00`）：单作业 681，两元素数组 682（allocation 行是 683 与 682）。
节点侧同一条 argv（`sacct -n -P -j <id> -o JobIDRaw,State,ExitCode,Partition,NodeList,AllocCPUS,ReqMem,ElapsedRaw,MaxRSS`）原样输出：

```text
681|COMPLETED|0:0|main|gnr-cu03|1|256M|2|
681.batch|COMPLETED|0:0||gnr-cu03|1||2|5204K
681.0|COMPLETED|0:0||gnr-cu03|1||1|712K
683|COMPLETED|0:0|main|gnr-cu03|1|256M|2|
683.batch|COMPLETED|0:0||gnr-cu03|1||2|5544K
683.0|COMPLETED|0:0||gnr-cu03|1||1|784K
682|COMPLETED|0:0|main|gnr-cu03|1|256M|12|
682.batch|COMPLETED|0:0||gnr-cu03|1||12|4496K
682.0|COMPLETED|0:0||gnr-cu03|1||12|708K
```

同一作业加 `-X`：

```text
681|COMPLETED|0:0|main|gnr-cu03|1|256M|2|
```

真实集群观察：

- R1 COMPLETED 的 allocation 行 ExitCode `0:0`，`.batch` 与 `.0` step 行各自带 MaxRSS，allocation 行 MaxRSS 为空（S03、S06）。
- R2 `-X` 只剩 allocation 行、MaxRSS 全空（S08）。
- R3 数组提交号 682 在记账里是两个 allocation 行 683、682，顺序不按编号；判终态与成功要看全部 allocation 行（S16 的数组语义；046 的 `test_partially_timed_out_array_is_not_a_success` 就是这个形状）。
- 边界：只有成功作业；被杀终态未在这台集群上取样。Slurm 版本未记录。

### 4.2 Docker 单机 Slurm 23.11.4（非真实集群）

单节点、`ProctrackType=proctrack/linuxproc`、`TaskPlugin=task/none`、`JobAcctGatherParams=OverMemoryKill`、
`KillWait=5`、`JobRequeue=0`，作业以 uid 0 提交。节点侧同一条 argv（`sacct -n -P -j <id> -o JobIDRaw,State,ExitCode,Partition,NodeList,AllocCPUS,ReqMem,ElapsedRaw,MaxRSS`）的原样输出：

```text
1|TIMEOUT|0:0|low|lulu|1|100M|86|                  ← --time=1 的 sleep 600
1.batch|CANCELLED|0:15||lulu|1||86|2816K
2|TIMEOUT|0:0|low|lulu|1|100M|86|                  ← 同上，但脚本 trap TERM 后 exit 0
2.batch|COMPLETED|0:0||lulu|1||86|2816K
3|FAILED|3:0|low|lulu|1|100M|1|                    ← exit 3
5|COMPLETED|0:0|low|lulu|1|100M|1|                 ← srun bash -c "exit 7"; exit 0
5.0|FAILED|7:0||lulu|1||0|                            （DerivedExitCode 7:0）
6|CANCELLED by 0|0:0|low|None assigned|0|100M|0|    ← 启动前 scancel（Start=None）
7|CANCELLED by 0|0:0|low|lulu|1|100M|9|            ← 运行中 scancel
7.batch|CANCELLED|0:15||lulu|1||9|3M
8|FAILED|0:9|low|lulu|1|50M|6|                     ← --mem=50 分配 300 MB，被 OverMemoryKill 杀
8.batch|CANCELLED by 0|0:9||lulu|1||6|310M
9|NODE_FAIL|0:0|low|lulu|8|800M|387|               ← 运行中 slurmd 被杀、节点 down
9.batch|CANCELLED|||lulu|8||388|
```

作业 stderr 里调度器写的行（前缀 `slurmstepd-<node>:`，该发行包按 MULTIPLE_SLURMD 构建；别锚前缀）：

```text
slurmstepd-lulu: error: *** JOB 1 ON lulu CANCELLED AT 2026-09-21T08:03:22 DUE TO TIME LIMIT ***
slurmstepd-lulu: error: *** JOB 7 ON lulu CANCELLED AT 2026-09-21T08:02:05 ***
slurmstepd-lulu: error: StepId=8.batch exceeded memory limit (325058560 > 52428800), being killed
slurmstepd-lulu: error: Exceeded job memory limit
slurmstepd-lulu: error: *** JOB 8 ON lulu CANCELLED AT 2026-09-21T08:02:02 ***
slurmstepd-lulu: error: *** JOB 9 ON lulu CANCELLED AT 2026-09-21T08:12:01 DUE TO NODE FAILURE, SEE SLURMCTLD LOG FOR DETAILS ***
srun: error: lulu: task 0: Exited with exit code 7
```

样本观察（只来自这一次 Docker 运行，不是官方文档；真实集群上要重验）：

- O1 `CANCELLED by <uid>` 的字面形状得到确认（S09）；启动前取消的作业 NodeList 是 `None assigned`、ElapsedRaw 0、Start None（S12、S20）。
- O2 捕获 SIGTERM 后 exit 0 的作业仍记 TIMEOUT，且 `.batch` 行是 COMPLETED 0:0（S11 的会说谎成立）。
- O3 **OverMemoryKill 杀掉的作业记 FAILED 0:9，不是 OUT_OF_MEMORY**；step 行是 `CANCELLED by 0` 0:9。OOM 证据只在 stderr 的 "exceeded memory limit (… > …), being killed" / "Exceeded job memory limit" 两行里——节点侧 `oom_killed`（按 State=OUT_OF_MEMORY）在这种配置下是 False，诊断规则 `slurm_step_oom_killed` 才是抓手（S15）。
- O4 NODE_FAIL 的 `.batch` 行 ExitCode 为空；横幅里的时间（08:12:01）是控制器判定节点故障的时刻，比记账 End（08:09:49）晚两分多钟（S13、S05）。
- O5 `-X` 输出只剩 allocation 行、MaxRSS 全空（S08）。
- O6 srun step 失败而脚本 exit 0：allocation COMPLETED 0:0、step FAILED 7:0、DerivedExitCode 7:0（S06、S07）。
- O7 slurmstepd 启动失败（cgroup 内存控制器不可用）的两个作业记成 `CANCELLED by 0` 0:0、`.batch` 行 CANCELLED 且 ExitCode 为空、ElapsedRaw 1——没有人执行过 scancel。`by 0` 在这里是系统取消，不是用户决定（S09 的会说谎成立）。
- 未取到样本：PREEMPTED（两个分区的抢占没有触发，preemptor 直接并行运行了）、BOOT_FAIL、DEADLINE、REVOKED、SPECIAL_EXIT、cgroup 约束下的 OUT_OF_MEMORY（容器里 swap 未约束，超配的进程被换页、作业 COMPLETED 0:0 而 MaxRSS 停在限额上——这本身也说明 ConstrainSwapSpace=no 时超内存不一定产生 OOM）。
