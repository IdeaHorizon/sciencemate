# PBS Professional / OpenPBS 与 Torque 终态事实（scheduler-longrun / references）

<!-- facts-format: v1 -->

**性质**：只有事实与判据，没有操作步骤，不含任何站点路径、队列名或 module 名。
**谁叫模型读它**：节点 hook `experiment_scheduler_facts_router`（on_turn_end）在
`check_external_job_health` / `wait_for_external_job` / `finalize_external_job` 返回
`scheduler_phase = terminal` 且 `scheduler = pbs` 时，对每个作业点名一次
`load_skill(name='scheduler-longrun', asset='references/pbs-torque-terminal-facts.md')`。
**来源口径**：PBS Professional 取 Altair 2024.1 的 Reference Guide（RG）/ User's Guide（UG）/
Administrator's Guide（AG）PDF（文档页脚 "Updated 2/27/24"），页码按 PDF 内页标；OpenPBS 官网没有
独立文档页，OpenPBS 版本未覆盖。Torque 取 Adaptive Computing 在线 Admin Guide，版本只在 URL 路径
里（6-1-2 / 6-1-1 / 6-1-3 / 4-2-0-early / 3-0-5），页面不印版本或日期。均抓取 2026-09-21。
**未在真实集群上验证**：两家都没有本地样本；§4 空着。

## 0. 判据（先看这里）

1. 两家的 `qstat -x` 不是一回事：PBS Pro 是"含已结束作业的历史查询"，Torque 是"XML 格式输出"（P01、T01、T02）。先探测口径，再解释输出。
2. 终态字母：PBS Pro 是 **F**（E 是过渡态），Torque 是 **C**（E 是过渡态）；两家的终态字母都**不含成败信息**，成败只在 `Exit_status` / `exit_status`（P06、P07、P09、T04、T08）。
3. `Exit_status` 128–253 = 顶层进程被信号杀（X mod 128 或 256）；254 = execve 失败；负数 = PBS/MoM 自己的码，其中若干表示"会再跑一次"（P12–P15、T10、T11）。
4. 作业不在 `qstat` 里、或报 Unknown Job Id，都不是"没跑过"的证据：PBS Pro 默认不保留历史，Torque 只保留 keep_completed 秒（P03、P19、T06）。
5. 超时被杀的证据在 MoM 日志 / 记账，不在 exit_status 的具体数值（P17、T13、T14）。

## 1. PBS Professional（Altair 2024.1）

### P01 `qstat -x` 与 `qstat -H` 的含义
- 事实：`qstat -x` 在排队 / 运行作业之外显示已结束与已迁移的作业；`qstat -H` 不带作业号时显示全部已结束或已迁移作业，带作业号时无论状态都显示该作业；不带 `-x`/`-H` 的 `qstat` 只覆盖排队与运行中的作业。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG §2.55.6.2，RG-207/208；Updated 2/27/24）
- 原文："-x Displays status information for finished and moved jobs in addition to queued and running jobs."
- 会说谎：作业不在 `qstat -x` 里不证明它没存在过或没跑过——历史未开启（默认）或 job_history_duration 到期后，已结束作业就是没有了。
- 节点侧：`pbs_scheduler.pbs_qstat_argv(history=True)` 只对探测为 PBS Pro/OpenPBS 的口径加 `-x`。

### P02 已结束 / 已迁移作业只在 `-x` / `-H` 下列出
- 事实：qselect 与 qstat 只有用 `-x` 或 `-H` 才列出已结束或已迁移作业，否则选择范围限于排队与运行中。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG §2.52.2，RG-189；Updated 2/27/24）
- 原文："Jobs that are finished or moved are listed only when the -x or -H options are used. Otherwise, job selection is limited to queued and running jobs."
- 会说谎：作业从普通 `qstat` 消失只说明"不再排队 / 运行"——它可能正常结束、失败、被删除，也可能被迁移到另一台 server。

### P03 服务器属性 job_history_enable 默认关闭
- 事实：job_history_enable 是布尔型服务器属性，默认 False；只有设为 True，PBS 才为 `qstat -x` 保留已结束 / 已删除 / 已迁移作业；total_jobs 也只有在 True 时才把它们计入。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-281，Server Attributes；Updated 2/27/24）
- 原文："job_history_enable Enables job history management. Setting this attribute to True enables job history management. Boolean False"
- 会说谎：默认配置下任何已结束作业 `qstat -x` 都返回空——空结果是配置产物，不是关于作业的证据。

### P04 job_history_duration 默认两周
- 事实：job_history_duration 是每个作业历史的保留时长，默认 336:00:00（两周）；设为 0 则不保留；开了历史但没设时长，按默认两周保留。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSAdminGuide2024.1.pdf（AG §10.15.5，AG-481；Updated 2/27/24）
- 原文："If the job history duration is set to zero, no history is preserved. If job history is enabled and job history duration is unset, job history information is kept for the default 2 weeks."
- 会说谎：站点可以把时长设成几分钟；晚于它去查会得到"未知作业"，尽管历史名义上是开着的。

### P05 历史到期与回溯性修改
- 事实：历史从作业结束或被删除时起保留；到期后 PBS 删除它，不再可查。每个作业的保留时长跟随服务器当前值，所以调低 job_history_duration 会提前清掉已结束作业；取消设置 job_history_enable 会立即删除全部历史。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSAdminGuide2024.1.pdf（AG §10.15.4、§10.15.6，AG-481/482；Updated 2/27/24）
- 原文："If job history is being preserved, and you unset the job_history_enable server attribute, PBS deletes all job history information. This information is no longer available."
- 会说谎：几分钟前还能用 `qstat -x` 看到的作业，可能因为管理员改了历史设置而消失，作业侧没有任何事件。

### P06 历史里的"finished job"定义
- 事实：作业历史中的"已结束作业"指执行因任何原因结束：成功结束并退出、运行中被 PBS 终止、因系统或网络故障失败、启动前就被删除。"已迁移作业"指移到另一台 server 的作业。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSUserGuide2024.1.pdf（UG §9.1.1，UG-167；Updated 2/27/24）
- 原文："Jobs whose execution is done, for any reason: • Jobs which finished execution successfully and exited • Jobs terminated by PBS while running • Jobs whose execution failed because of system or network failure"
- 会说谎：状态 F / "finished" 从不蕴含成功；启动前被删除、被 PBS 杀掉的作业同样是 "finished"。

### P07 job_state 字母表
- 事实：job_state 是单个字符：B（Begun，仅作业数组）、E（Exiting：已结束、有无错误皆可，PBS 在清理）、F（Finished：已完成执行、执行中失败、或被删除）、H（Held）、M（Moved 到另一台 server）、Q（Queued）、R（Running）、S（Suspended）、T（Transiting）、U（用户挂起，cycle-harvesting 工作站）、W（Waiting：Execution_Time 在未来；stage-in 失败也会置为 30 分钟后的 W）、X（Expired；仅子作业）。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-334，Job Attributes: job_state；Updated 2/27/24）
- 原文："F (Finished) Job is finished. Job has completed execution, job failed during execution, or job was deleted."
- 会说谎：F 明确包含"执行中失败"和"被删除"；W 可能是 stage-in 失败而不是用户要求的延迟。
- 节点侧：`resource_manager` 以正则 `job_state\s*=\s*[CEF]\b` 判终态（C 是 Torque 的字母，E 两家都是过渡态，见 P09、T04）。

### P08 状态数值与数组 / 子作业状态
- 事实：状态对应数值 T=0、Q=1、H=2、W=3、R=4、E=5、X=6、B=7、M=8、F=9、S=400、U=410（Table 8-1，RG-357）。作业数组永不处于 R、S、U（B 表示至少一个子作业离开了排队；Table 8-3，RG-359）。子作业状态表（Table 8-4，RG-359）列 E、F、Q、R、S、U；X 只在 Table 8-1 描述为"仅子作业；子作业已结束（expired）"。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-357/359，§8.1.1、§8.2、§8.3；Updated 2/27/24）
- 原文："Job arrays will never be in the 'R', 'S' or 'U' states."
- 会说谎：数组作业显示 B 时部分子作业可能已经失败，数组级状态盖住子作业结果。

### P09 E（Exiting）是过渡态且不带成败
- 事实：E 表示作业已结束（有无错误皆可），PBS 正在做执行后清理（子状态 50–59 覆盖收到 obit、stage-out、删文件、epilogue）。它在 F 之前，不说明成功与否。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-334；RG-358 Table 8-2；Updated 2/27/24）
- 原文："E (Exiting) The job has finished, with or without errors, and PBS is cleaning up post-execution."
- 会说谎：在 E 期间采样可能已经看到 Exit_status，但输出文件还没有 stage 回来（子状态 51 "Staging out stdout/err and other files"）。
- 节点侧：节点把 E 当终态读（P07），此时 Exit_status 可能尚未出现——读不到退出码时不应把它读成成功。

### P10 作业属性 Exit_status
- 事实：Exit_status 是整数作业属性（无默认值），记录作业的退出状态；0 表示执行成功。作业数组中任一子作业退出状态非零，数组作业的退出状态即非零。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-332，Job Attributes: Exit_status；Updated 2/27/24）
- 原文："Exit_status Exit status of job. Set to zero for successful execution. If any subjob of an array job has non-zero exit status, the array job has non-zero exit status."
- 会说谎：Exit_status 只在作业退出后才存在，退出后又只在历史里才读得到；交互式作业永远记 0。
- 节点侧：`resource_manager` 以正则 `^\s*exit_status\s*=\s*(-?\d+)\s*$`（忽略大小写）读它，`succeeded = (exit_code == 0)`。

### P11 退出状态 0–127 = 顶层进程（通常是 shell）的退出值
- 事实：0 ≤ X < 128 是作业顶层进程（通常是 shell）的退出值——可能是 shell 里最后一条命令的退出值，也可能是用户 .logout 脚本（csh）的退出值。交互式作业的退出状态永远记 0。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-373，Table 11-1；Updated 2/27/24）
- 原文："This is the exit value of the top process in the job, typically the shell. This may be the exit value of the last command executed in the shell or the .logout script if the user has such a script (csh)."
- 会说谎：真正的负载失败之后尾部命令（或 .logout）成功，就得到 0；交互式作业的 0 没有意义。

### P12 退出状态 ≥ 128 = 被信号杀
- 事实：128 ≤ X < 254 表示作业顶层进程被信号杀，信号号 = X mod 128（某些系统按 256，见 wait(2)/waitpid(2)）。文档例：137 → 信号 9（SIGKILL），143 → 信号 15（SIGTERM）。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-373，Table 11-1；Updated 2/27/24）
- 原文："For example an exit value of 137 means the job's top process was killed with signal 9 (137 % 128 = 9)."
- 会说谎：基数（128 还是 256）依系统而定；数值只反映顶层进程（shell）——子进程被信号杀而 shell 正常退出时得到的是小退出码，不是 128+n。

### P13 退出状态 254 = execve() 失败
- 事实：X = 254 表示作业在 execve() 阶段失败：文件或脚本解释器不是普通文件、无执行权限、文件系统 noexec、路径 / 脚本 / ELF 解释器不存在。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-373，Table 11-1；Updated 2/27/24）
- 原文："X = 254 Job had an execve() failure This means that the job experienced a failure during execve()"
- 会说谎：254 是启动失败而不是作业脚本的结果——负载根本没开始，尽管作业带着正退出码到达 F。

### P14 负 Exit_status（-1 … -13）
- 事实：负退出状态表示作业无法执行：0 JOB_EXEC_OK；-1 JOB_EXEC_FAIL1 文件前失败、不重试；-2 JOB_EXEC_FAIL2 文件后失败、不重试；-3 JOB_EXEC_RETRY 失败、重试；-4 JOB_EXEC_INITABT MoM 初始化时中止；-5 INITRST（检查点、不迁移）；-6 INITRMG（检查点、可迁移）；-7 JOB_EXEC_BADRESRT 重启失败；-10 JOB_EXEC_FAILUID 无效 UID/GID；-11 JOB_EXEC_RERUN 作业被 rerun；-12 JOB_EXEC_CHKP 检查点后被杀；-13 JOB_EXEC_FAIL_PASSWORD 密码错误。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-374，Table 11-2 Job Exit Codes；Updated 2/27/24）
- 原文："Negative exit status indicates that the job could not be executed. Negative exit values are listed in the table below: Table 11-2: Job Exit Codes"
- 会说谎：-3、-11、-12、-15 一类表示作业还会再跑（requeue / rerun），所以记账 R 记录或中间 obit 里的负 Exit_status 不是作业的最终结果。

### P15 负 Exit_status（-14 … -20：hook、sister / MoM 故障）
- 事实：-14 JOB_EXEC_RERUN_ON_SIS_FAIL 主 MoM 与 sister 通信失败，可重跑则重排否则删除；-15 JOB_EXEC_QUERST 从检查点重启而重排；-16 JOB_EXEC_FAILHOOK_RERUN hook 拒绝、重排；-17 JOB_EXEC_FAILHOOK_DELETE hook 拒绝、结束时删除；-18 JOB_EXEC_HOOK_RERUN hook 要求重排；-19 JOB_EXEC_HOOK_DELETE hook 要求删除；-20 JOB_EXEC_RERUN_MS_FAIL server 联系不上主执行主机的 MoM 而重排。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（RG-374，Table 11-2；Updated 2/27/24）
- 原文："-20 JOB_EXEC_RERUN_MS_FAIL Job requeued because server couldn't contact the primary execution host MoM"
- 会说谎：-14 单看是二义的：不可重跑的作业被删除，可重跑的被重排、之后可能以新的 Exit_status 成功。

### P16 作业数组的退出状态与子作业历史
- 事实：数组作业退出状态：全部子作业返回 0 则为 0（被删除的子作业不计）；至少一个子作业非零则为 1；PBS 错误则为 2；只有全部有效子作业完成后才可得。子作业的退出状态只在子作业处于历史中时可得；否则失败或被终止的子作业只显示为 Finished。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSUserGuide2024.1.pdf（UG §8.4.6，Table 8-3，UG-160/161；Updated 2/27/24）
- 原文："When a subjob is not in job history, a failed or terminated subjob will show an exit status of Finished, instead of failed or terminated."
- 会说谎：没有历史时，被杀或失败的子作业读起来只是 "Finished"；数组退出 0 忽略被删除的子作业。

### P17 walltime 与资源限制的执行方式
- 事实：运行中作业超过 walltime 限制即被终止；任一进程超过 pcput、pmem、pvmem，或主机级 mem、ncpus、cput、vmem 超限，同样终止（UG §4.5.4.1）。超过 soft_walltime 不杀作业：每次超出把 soft_walltime 按原值的 100% 延长，永不超过硬 walltime（AG §4.9.44，AG-218）。2024.1 的 RG/UG/AG 正文里**没有**出现 "job killed: walltime … exceeded limit" 之类的字面提示串。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSUserGuide2024.1.pdf 、 https://help.altair.com/2024.1.0/PBS%20Professional/PBSAdminGuide2024.1.pdf（UG-53；AG-218；Updated 2/27/24）
- 原文："If a running job exceeds its limit for walltime, the job is terminated. If any of the job's processes exceed the limit for pcput, pmem, or pvmem, the job is terminated."
- 会说谎：walltime 被杀表现为信号段的 Exit_status（如 143 / 137）加状态 F——不读 comment / 日志就分不出是超时还是用户侧 kill；resources_used.walltime 超过 soft_walltime 不是被杀。

### P18 作业 comment 属性的含义
- 事实：comment 是 "Comment about job. Informational only."。server 会把它设为 "Job was sent for execution at <time> on <execvnode>"，MoM 确认后改为 "Job run at <time> on <execvnode>"，MoM 拒绝则为 "Not Running: PBS Error: Execution server rejected request"；作业会超过运行上限时设为某条 "Not Running: …"；以 "Can never run" 开头表示请求在当前配置下永远无法满足。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSAdminGuide2024.1.pdf 、 https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf（AG-465 §10.7.3.1、AG-638 §20.3.6；RG-327、RG-381；Updated 2/27/24）
- 原文："If the MoM rejects the job, the server changes the job comment to \"Not Running: PBS Error: Execution server rejected request\"."
- 会说谎：comment 可被 operator / manager 改写且仅供参考；它是最近一次调度 / 运行事件的提示，不是终态或退出状态记录。

### P19 查询已结束作业时的错误码与提示
- 事实：文档化的 server 错误码：PBSE_UNKJOBID 15001 "Unknown Job Identifier"（RG-383）、PBSE_HISTJOBID 15139 "History job ID"、PBSE_JOBHISTNOTSET 15140 "job_history_enable not SET"（RG-387）。qalter、qhold、qmove、qmsg、qorder、qrerun、qrls、qrun、qsig 对已结束作业失败并提示 "<command name>: Job <job ID> has finished"（UG-168）；不带 `-x` 的 qdel 提示 "qdel: Job <job ID> has finished"（UG-171）；tracejob 只有历史开启时才能查已结束作业（RG-180）。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSReferenceGuide2024.1.pdf 、 https://help.altair.com/2024.1.0/PBS%20Professional/PBSUserGuide2024.1.pdf（RG-383/387、RG-180；UG-168、UG-171；Updated 2/27/24）
- 原文："PBSE_HISTJOBID 15139 History job ID PBSE_JOBHISTNOTSET 15140 job_history_enable not SET"
- 会说谎：对一个提交过的作业报 "Unknown Job Identifier"，与 (a) 历史未开、(b) 历史到期、(c) qdel -x、(d) 拼错 / 已迁移都一致——它不是作业没跑过的证据。
- 节点侧：`pbs_scheduler._HISTORY_NOT_CONFIGURED` 的四个短语（"not configured to maintain job history" 等）没有一条与文档给出的 "job_history_enable not SET" 逐字相同；命中不了时代码走 `pbs_history_query_unavailable` 分支，只影响原因标签。

### P20 记账日志 E 记录与 R 记录的 Exit_status
- 事实：记账日志里 E（结束）记录的 Exit_status 是作业或子作业的退出状态；R（rerun）记录的 Exit_status 是"该作业上一次启动的退出状态"。两者都注明交互式作业永远记 0。被迁移的作业记为带目的地的 M 记录。
- 来源：https://help.altair.com/2024.1.0/PBS%20Professional/PBSAdminGuide2024.1.pdf（AG §12.4 记账记录字段，AG-535/540；AG-483 §10.15.8；Updated 2/27/24）
- 原文："Exit_status= <exit status> The exit status of the previous start of the job."
- 会说谎：带负 Exit_status 的 R 记录（如 -12 检查点后被杀、-11 rerun）指的是更早的一次尝试；只有 E 记录才收口作业。

## 2. Torque（Adaptive Computing）

### T01 `qstat --xml` 是显示格式，man page 没有 `-x` 条目
- 事实：Torque 6.1.2 的 qstat man page 把 XML 开关记为 `--xml`，"同 -a 但输出为 XML 样式格式"；它是显示格式，不是"包含已结束作业"的请求。2.5.12、4.2.10、5.1.3、6.0.4、6.1.2、6.1.3 的 HTML qstat 页都没有 `-x` 条目。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/commands/qstat.htm（URL 路径版本 6-1-2；页面不印版本 / 日期；抓取 2026-09-21）
- 原文："--xml Same as -a, but the output has an XML-like format."
- 会说谎：按 PBS Pro 写的解析器会把 `qstat -x` 当"包含已结束作业"；在 Torque 上它只改格式、作业集合不变，所以不在 `qstat -x` 输出里的作业是已被清除，不是"还没结束"。
- 节点侧：探测为 Torque 时 `pbs_qstat_argv` 不加 `-x`；`test_torque_status_and_recovery_never_use_xml_x` 钉此。

### T02 `qstat -x` 的 XML 输出从 1.1.0p2 就有、始终未进 man page
- 事实：Torque 变更日志（3.0.5 页）在 1.1.0p2 标题下记 "added -x (xml output) support for 'qstat -f' and 'pbsnodes -a'"；2.1.2 记修复 qstat / pbsnodes 的 XML 截断；2.4.1 记改善 `qstat -x` 的 XML 输出与文档；2.4.6 记为保持 `qstat -f` 产生的 XML 一致而改动 walltime 剩余时间显示。所以在 Torque 上 `qstat -x` 是 `qstat -f` 全状态的 XML 渲染。
- 来源：https://docs.adaptivecomputing.com/torque/3-0-5/changelog.php（Change Log 页，条目在 1.1.0p2 / 2.1.2 / 2.4.1 / 2.4.6 标题下；抓取 2026-09-21）
- 原文："added -x (xml output) support for 'qstat -f' and 'pbsnodes -a'"
- 会说谎：该开关存在于二进制但 man page 不写；脚本不能因为它存在就推断"历史模式"。

### T03 没有作业时 `qstat -x` 曾输出空串
- 事实：Torque 6.1.0 发布说明的已解决问题：没有排队作业时 `qstat -x` 返回空串而不是空 XML 文档（TRQ-3622）；修正后返回空 XML 文档。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-0/releaseNotes/Content/topics/releaseNotes/resolvedIssues.htm（Release Notes 6.1.0；抓取 2026-09-21）
- 原文："qstat -x returned nothing (instead of an empty XML document) when there are not jobs queued. (TRQ-3622)"
- 会说谎：6.1.0 之前零字节的 `qstat -x` 输出是合法的"没有作业"，不是命令失败；把空输入当错误的 XML 解析器会误判。

### T04 job_state 字母表
- 事实：C = 已运行并完成；E = 已运行、正在退出；H = held；Q = 排队（可运行或被路由）；R = 运行；T = 正被移到新位置；W = 等待执行时间；S = 挂起（文档标注仅 Unicos）。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/commands/qstat.htm（URL 路径版本 6-1-2；抓取 2026-09-21）
- 原文："C Job is completed after having run. E Job is exiting after having run."
- 会说谎：Torque 没有 F 状态：C 是唯一终态字母，成功、失败、超时被杀、qdel 一视同仁；C 本身从不表示成功，必须读 exit_status。E 表示作业还在退出，exit_status 可能未定。

### T05 默认显示已完成作业；`-c` 与 PBS_QSTAT_NO_COMPLETE
- 事实：Torque qstat 默认列出已完成（C）作业；`-c` 选项隐藏它们；环境变量 PBS_QSTAT_NO_COMPLETE 使所有 qstat 请求默认不显示已完成作业。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/commands/qstat.htm（URL 路径版本 6-1-2；抓取 2026-09-21）
- 原文："Completed jobs are not displayed in the output. If desired, you can set the PBS_QSTAT_NO_COMPLETE environment variable to cause all qstat requests to not show completed jobs by default."
- 会说谎：用户环境设了 PBS_QSTAT_NO_COMPLETE 时，正常结束的作业从 qstat 消失得和被清除的一模一样；不清掉这个变量，"不在列表里"没有信息量。

### T06 服务器参数 keep_completed
- 事实：keep_completed（服务器参数，整数秒，6.1.1 表中默认 300）是作业进入 completed 状态后仍留在队列里的时长；文档补充作业依赖要生效必须设置 keep_completed。
- 来源：http://docs.adaptivecomputing.com/torque/6-1-1/adminGuide/Content/topics/torque/13-appendices/serverParameters.htm（URL 路径版本 6-1-1；6-1-2 副本重定向不可达；抓取 2026-09-21）
- 原文："The amount of time (in seconds) a job will be kept in the queue after it has entered the completed state. keep_completed must be set for job dependencies to work."
- 会说谎：keep_completed 秒之后作业记录从 qstat 彻底消失（没有历史模式），晚到的轮询对一个正常结束的作业看到 "unknown job id"；站点可设 0，此时作业可能根本观测不到 C 状态。

### T07 队列属性 keep_completed
- 事实：keep_completed 也是按队列的属性（整数，默认 0），指定作业退出后在 Completed 状态保持的秒数。
- 来源：http://docs.adaptivecomputing.com/torque/6-1-1/adminGuide/Content/topics/torque/13-appendices/queueAttributes.htm（URL 路径版本 6-1-1；抓取 2026-09-21）
- 原文："Specifies the number of seconds jobs should be held in the Completed state after exiting."
- 会说谎：队列默认 0、服务器默认 300，不同队列保留期不同；同一 server 上不能假定各队列的 C 状态窗口一样。

### T08 C 状态与 exit_status 的可见性
- 事实：设置 keep_completed 后，已完成作业以 C 状态报告，退出状态见 exit_status 作业属性；Torque 描述为在可配置时长内报告已完成（或已取消、失败等）的作业。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/2-jobs/keepingCompletedJobs.htm（URL 路径版本 6-1-2，§3.16；抓取 2026-09-21）
- 原文："If you set keep_completed on the job execution queue, completed jobs will be reported in the C state and the exit status is seen in the exit_status job attribute."
- 会说谎：没有 keep_completed 时作业在完成瞬间就没了，exit_status 永远无法通过 qstat 观测；C 状态本身包含被取消和失败的作业，"C" 不是"成功"。

### T09 spool 文件与 keep_completed / `qdel -p`
- 事实：设置了 keep_completed 时，作业 spool 文件在该时刻删除并把作业从内存清除；未设置时 spool 文件在作业完成时删除；在完成前用 `qdel -p` 手工清除的作业，其 spool 文件 Torque 永不删除。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/2-jobs/keepingCompletedJobs.htm（URL 路径版本 6-1-2，§3.16；抓取 2026-09-21）
- 原文："When keep_completed is not set, Torque deletes the job spool files upon job completion."
- 会说谎：残留 spool 文件不是作业仍在运行的证据（可能被 `qdel -p` 清除过）；没有 C 记录且 spool 文件也没了，与 keep_completed 未设时的正常完成一致。

### T10 exit_status 的含义
- 事实：作业完成后 exit_status 保存作业脚本返回的结果码，显示在 `qstat -f` 输出的底部；Torque 无法启动作业时该字段是 pbs_mom 给出的负数，否则是脚本的返回值。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/2-jobs/jobExitStatus.htm（URL 路径版本 6-1-2；抓取 2026-09-21）
- 原文："If Torque was unable to start the job, this field will contain a negative number produced by the pbs_mom. Otherwise, if the job script was successfully started, the value in this field will be the return value of the script."
- 会说谎：exit_status 反映批处理脚本最后一条命令，不是科学负载；0 只说明脚本最后一句返回 0；负数是 MoM / 启动失败，不是用户代码。

### T11 Torque 自身的负退出码
- 事实：0 JOB_EXEC_OK；-1 FAIL1（文件前失败、不重试）；-2 FAIL2（文件后失败、不重试）；-3 RETRY；-4 INITABT；-5 INITRST；-6 INITRMG；-7 BADRESRT；-8 CMDFAIL（exec 用户命令失败）；-9 STDOUTFAIL；-10 OVERLIMIT_MEM；-11 OVERLIMIT_WT（超过 walltime 限制）；-12 OVERLIMIT_CPUT；-13 RETRY_CGROUP；-14 RETRY_PROLOGUE（-13 / -14 在 4.2.10 副本中没有）。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/2-jobs/jobExitStatus.htm（URL 路径版本 6-1-2；抓取 2026-09-21）
- 原文："JOB_EXEC_OVERLIMIT_WT -11 Job exceeded a walltime limit"
- 会说谎：-3 / -13 / -14 表示 server 可能重跑，之后同一作业号再出现 R 或 Q 是重试不是新作业；文档把 walltime 超限记为 -11，但 tracejob 样例里出现的是正数 265（T13），解析器要同时接受两种编码。

### T12 exit_status 只保留低字节
- 事实：文档示例：C 程序 exit(256+11) 得到 exit_status = 11，因为 exit 只传低字节；退出状态页没有给出任何 "256+信号" 或 "128+信号" 公式。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/2-jobs/jobExitStatus.htm（URL 路径版本 6-1-2；抓取 2026-09-21）
- 原文："Notice that the C routine exit passes only the low order byte of its argument. In this case, 256+11 is really 267 but the resulting exit code is only 11 as seen in the output."
- 会说谎：脚本以 256 的倍数退出会报 exit_status 0、看起来成功；反之大于 255 的正 exit_status 不可能来自脚本的 exit()，必须按信号 / 调度器编码理解，尽管页面没有定义它。

### T13 walltime 超限：MoM 日志文本与记账 Exit_status
- 事实：官方 tracejob 样例里 walltime 被杀的 MoM 日志行是 "walltime 210 exceeded limit 100"，随后 kill_job 向任务发信号 15，最终记账记录 Exit_status=265，resources_used.walltime=00:07:46 对 Resource_List.walltime=00:01:40。页面上没有 "PBS: job killed: walltime …" 之类的 stderr 文本。
- 来源：http://docs.adaptivecomputing.com/torque/4-2-0-early/Content/topics/11-troubleshooting/usingTracejobToLocateFailures.htm（URL 路径版本 4-2-0-early；样例日志日期 2005-03-02；§11.4；抓取 2026-09-21）
- 原文："03/02/2005 18:02:11 M walltime 210 exceeded limit 100 ... end=1109811987 Exit_status=265 resources_used.cput=00:00:00"
- 会说谎：被杀原因在 MoM 日志行里，不在 exit_status：记账是 265 而不是文档的 -11，且页面从未说明 265 怎么来的（它不等于 256+15），所以 exit_status 单独分不出 walltime 被杀与其他信号死亡；resources_used.walltime 可能超过限制几分钟，因为 kill 升级需要时间。

### T14 2.5.6 之前超限返回 0
- 事实：变更日志 2.5.6 条目引入 JOB_EXEC_OVERLIMIT，使超过 walltime 等限制的作业以该值失败并触发 abort 邮件；此前超限作业"成功返回 0"，且不发 abort 邮件。
- 来源：https://docs.adaptivecomputing.com/torque/3-0-5/changelog.php（Change Log 页，2.5.6 标题下；抓取 2026-09-21）
- 原文："Previous to this change a job exceeding a limit returned 0 on success and no mail was sent to the user if requested on abort."
- 会说谎：旧于 2.5.6 的 Torque 上 walltime 被杀的作业可能带 exit_status 0、看起来成功；新版本上被杀证据也应来自 MoM 日志 / 记账，而不是"exit_status 非零"。

### T15 `qstat -f` 的 walltime 剩余值
- 事实：`qstat -f` 是全状态显示，其 [time] 值是作业剩余 walltime 秒数，不考虑 walltime 乘数。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/commands/qstat.htm（URL 路径版本 6-1-2；抓取 2026-09-21）
- 原文："Specifies that a full status display be written to standard out. The [time] value is the amount of walltime, in seconds, remaining for the job. [time] does not account for walltime multipliers."
- 会说谎：站点用了 walltime 乘数时剩余值不对；3.0.5 变更日志记挂起 / 停止的作业不倒计 walltime，所以"剩余"不是到被杀为止的墙钟时间。

### T16 作业 comment 的来源（`qstat -s`）
- 事实：`qstat -s` 在基本信息之外显示批处理管理员或调度器提供的 comment；文档没有说 comment 承载 MoM 的 kill 原因。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-2/adminGuide/Content/topics/torque/commands/qstat.htm（URL 路径版本 6-1-2；抓取 2026-09-21）
- 原文："In addition to the basic information, any comment provided by the batch administrator or scheduler is shown."
- 会说谎：comment 是调度器 / 管理员文本（如 Moab），不是 Torque 的终止原因；C 状态作业的 comment 为空或陈旧不说明它为何结束。

### T17 `qdel -p` 的清除语义
- 事实：`qdel -p` 强制从 server 清除作业，仅用于运行中作业因节点不可达而不退出的情形；若 mother superior 之后恢复，epilogue 脚本可能仍会运行。仅限批处理 operator / 管理员。
- 来源：http://docs.adaptivecomputing.com/torque/6-1-1/adminGuide/Content/topics/torque/commands/qdel.htm（URL 路径版本 6-1-1；抓取 2026-09-21）
- 原文："Forcibly purges the job from the server. This should only be used if a running job will not exit because its allocated nodes are unreachable."
- 会说谎：被清除的作业从不经过 C 记录，它的消失与 keep_completed 到期无法区分；server 忘掉作业之后节点上的进程可能还活着。

### T18 作业日志是可选开启的
- 事实：server 侧作业日志只在 record_job_info 为 TRUE（默认 FALSE）时启用；job_log_keep_days（无默认值）删除早于给定天数的作业日志文件。
- 来源：http://docs.adaptivecomputing.com/torque/6-1-1/adminGuide/Content/topics/torque/13-appendices/serverParameters.htm（URL 路径版本 6-1-1；抓取 2026-09-21）
- 原文："This must be set to TRUE in order for job logging to be enabled."
- 会说谎：没有作业日志记录不等于作业没跑过；日志可能没开或已轮转掉，默认只有记账 / tracejob 和 C 状态窗口可查。

### T19 Torque 与 PBS Pro 的口径标记
- 事实：man page 可依赖的 Torque 标记：qstat 用法行列出 `--xml` 与 `-c`（隐藏已完成），环境变量 PBS_QSTAT_NO_COMPLETE 与 PBS_QSTAT_EXECONLY；job_state 以 C 为终态；exit_status 可为负（MoM 码 -1 … -14）；记账记录键为 "Exit_status="。
- 来源：https://docs.adaptivecomputing.com/torque/6-1-3/adminGuide/Content/topics/torque/commands/qstat.htm（URL 路径版本 6-1-3；6-1-2 措辞相同；抓取 2026-09-21）
- 原文："qstat [ -a | -i | -r | -e | --xml ] [ -c ] [ -n [ -1 ]] [ -s ] [ -G | -M ] [ -R ] [ -u user_list]"
- 会说谎：按 PBS Pro 写的解析器期待 F 状态、`qstat -x` 历史、Exit_status 限于 0–255，会误读 Torque；反过来 C 状态和负 exit_status 也不能用 PBS Pro 规则解释。
- 节点侧：`pbs_scheduler._classify_version` 以 `qstat --version` 输出判口径——`pbs_version =` / "openpbs" / "pbs pro" → PBS Pro；"torque" 或行首 `Version:` → Torque；其余 unknown（不缓存、下次再探）。

## 3. 节点代码对照与 042/046 用例

| 节点侧符号 | 依据 |
|---|---|
| `pbs_scheduler._classify_version`（`pbs_version =` / `Version:`） | T19、P01 |
| `pbs_qstat_argv(history=True)` 只对 PBS Pro 加 `-x` | P01、P02、T01、T02 |
| `_HISTORY_NOT_CONFIGURED` 四个短语（启发式，见 P19 节点侧） | P03、P19 |
| `resource_manager` 终态正则 `job_state = [CEF]` | P07、P09、T04 |
| `resource_manager` 退出码正则 `exit_status = (-?\d+)`，`succeeded = (exit_code == 0)` | P10–P15、T10–T12 |

| 用例（`tests/test_cluster_job_state_truth.py`） | 事实 |
|---|---|
| PBS Pro：`qstat -x -f` → `job_state = F`、`Exit_status = 0`；不带 `-x` 报 "Job has finished, use -x or -H" | P01、P07、P10、P19（文档只给 "<command>: Job <id> has finished"，"use -x or -H" 后缀未见于文档） |
| Torque：`Version: 6.1.3`；`-x` 给 XML；状态与恢复都不带 `-x` | T01、T02、T19 |
| 口径 unknown：状态只查活动作业，恢复仍试历史 | T19（unknown 不缓存） |
| PBS Pro 历史被关：退回 unknown 并带原因 | P03、P04、P19 |

## 4. 样本

（无：本地没有 PBS Pro / Torque，未取到真实输出。）
