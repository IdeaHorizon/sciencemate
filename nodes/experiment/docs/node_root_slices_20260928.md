# 第二版节点小片：路径、审批接线与资源提示（2026-09-28）

## 状态与范围

起点 `a2cf3d162ff2ca8db883303945a6b3163c7d02f6`，本机分支
`lujy/impl-experiment-v3-export`，上游 `origin/lujy/impl-experiment-v3`。
本记录随实现一起提交；验收树以该提交和后续实测记录为准，不用起点冒充修复版本。
三个运行时小片固定在`1f28ab28cf71242551e6991681bca612e7a1a70d`。
方案经信箱159、160、161收敛。未修改 Core、共享登记、根测试、旧实例或历史证据。
这是可测实验版，不是可发布声明；Core #1164 活跃父子接续/#1165 坏JSON派发丢输入、
远端实提和完整科学验收仍是独立缺口。本批不重开这些实现线，不向main提PR。

## 共同修复

| 原因 | 修改 | 不能外推的保证 |
|---|---|---|
| 文件工具检查与Core writer使用不同坐标 | Core resolve_tool_path解析一次，同一目标给guard/capture/writer；字面路径不按shell展开 | 不扩大owner、只读、先读后覆盖权限；子进程grant不扩大Core文件工具权限；不声称解决Core writer竞态 |
| scope暂停type/选项与共享恢复协议不一致 | highrisk_confirm与HIGHRISK_OPTIONS同源；未知目标如实说明静态无法确定 | 不等于共享key/TTL/原子消费或父子UI投影已修；不移除全部静态门 |
| 安装客户端、推荐、公开提交能力各自判断 | discovery/recommend/auto/bootstrap共用实际launch factory候选；无候选报缺口，不降GPU或假local fallback | 候选不等于授权/限额生效/启动成功；远端仍显式dry-run，未证明Slurm/MPI/GPU |

未新增模型手续、平行状态表或授权机制。进程组退出不证明所有后代退出，申请资源也不证明限额生效。

会话2的163号单文件hpc-build补丁（原提交626b3a3e）已核对哈希并集成：
构建/运行验收按任务适用，MPI按真实兼容关系而非目录判断，失败利用新证据纠正，
稳定知识遵循现有项目级create_claim，不教shell绕过不可用受管入口。
集成后的skill-tool一致性、L1按需加载和常驻上下文预算三项均通过；未据此声称HPC效果已实测。

## 回归证据

主解释器为工作区`.venv311-ci/bin/python`，Python3.11.16，sqlite3/pytest可导入。
资源子任务用`/home/lujy/hf-ui-22ae44d2/.venv/bin/python`；主全量另作统一复验。

| 检查 | 结果 |
|---|---|
| 文件路径坏基线 | 修正测试自身字段读取后：11 failed / 4 passed / 1 skipped；修复后15 passed / 1 skipped。含Project/direct、字面路径、拒写、先读后覆盖、baseline symlink |
| 审批桥接坏基线 | 原8项全部红；现10项通过。真实pause_driver/resume_loop/共享hook/节点重试，只有末端模型应答为替身，没有执行载荷 |
| 资源候选坏基线 | 首14项13红1绿；现19项通过。显式偏好/GPU/auto/远端分别覆盖 |
| bootstrap提示 | 3项基线红、修复绿；客户端存在、入口缺席、本地可用分开 |
| 路径/审批/自然交付等定向组合 | 176 passed / 1 skipped，4.71秒 |
| 资源定向组合 | 64 passed / 104 deselected；auto/recommend/preflight/GPU/remote/Slurm/memory_contract相关 |
| 第一轮节点全量 | 4149 passed / 5 failed / 22 skipped / 6 xfailed，199.72秒；不称全绿 |
| 后续两处接线复核 | 旧legacy fixture改用自身owned路径、恢复auto不选Kubernetes的准确文案；相关7项通过 |

第一轮余下三红：

- `test_failed_output_is_not_rehabilitated_by_method_correction[object-omitted]`：既有产品缺口，保留反例，不改判据。任意shell check无完整读取依赖，也不机械证明自然语言验收充分。
- `test_agent_cannot_self_grant_write_access`、`test_approval_registers_directory_and_stops_asking`：指定的非`/tmp`夹具在Git树内，实际protected_source_tree不等于期待unknown_absolute。仅把基线classifier装入测试进程，同环境两例仍红；是局部基线对照，不是干净基线全量。未获授权另建仓库外目录，未绕过限制或降低保护换绿。

最终全量：**4151 passed / 3 failed / 22 skipped / 6 xfailed，190.91秒**；恰为上述三个剩余失败，未改断言换绿。忽略目录`.tmp/root-slices-0928/node-tests-final.xml`保存JUnit；缓存/证据不进产品提交。

## 根检查与owner交接

三个根文件refusal-sites、instructed-tools-whitelist、one-throat：3 failed / 16 passed；不是根全量。
登记旧欠账未清；旧退役工具名仍被文案扫描；one-throat还扫到本工作区ignored `.tmp`下既有UI启动器和活体验证脚本，不是新增生产subprocess入口。不删现场换绿，干净检出本轮未验。

共享表只读，本批新增两个AST返回点交framework owner审核，不抬baseline：

| 定位与计数 | 建议分类/不变量 | 出口 |
|---|---|---|
| safe_bash文件写入实现，ProjectWorkspaceError透传；32→33 | A：Core路径/owner准入同源，不新增权限规则 | 尚未建目录/写入；纠正为合法owned路径重试；subprocess grant不能越过Core文件owner |
| resource_manager._submit_job的auto无候选；107→108 | C：没有公开可用提交契约不能补造候选 | side_effects=none；修能力后重试或诚实blocked；显式远端dry-run仍可用 |

原22条记录见`refusal_registry_handoff_20260924.md`；本件只交增量，不回写历史基线。
内部只读diff复核未发现新增可达越权/误拒，但未独立执行测试，不能代替会话2固定补丁复核。

## 真实UI验证结论

用户授权改完原样复测导热、三组SGD、UI补件、用户FVCOM，再测同根因相邻任务。
固定新提交、新实例和新项目，原题/材料/模型deepseek-v4-pro保持，不加分类、内部调用或恢复答案。
分别核查业务、交付、专业方法、误拦及人工介入；旧证据不修改，一次随机模型通过不等于根因已全消除。
FVCOM原题仅“下载编译FVCOM”，无附件；旧项目通配风险授权不自动沿用，不能为重现而扩大权限。

固定`574081d77d6caae98fa3c37a9bf79e310eb89c52`，模型deepseek-v4-pro，独立UI实际投题。
166号独立复核接受三个接线小片：47 passed / 1 skipped，局部坏基线5红2绿；不是整体验收。

| 任务 | 本轮主要结论 |
|---|---|
| 导热输入生成、三组SGD | 业务和三件套独立核对通过；SGD纠正检查后未重跑九阶段 |
| UI补数据 | 未通过：autonomous代答改变任务，正常补件未恢复原计算；stop后curl需人工精确TERM，Core问题 |
| FVCOM下载编译 | MPI/串行二进制真实生成；领域partial交付。父spec额外要求MPI运行，不据此否定原编译目标；源码修改语义仍有专业风险 |
| 相邻导热、Slurm只探测、两批输入负例 | 有正面证据；只探测不代表真实Slurm，外层exit0未使模型把坏批次判为通过，不外推任意shell保证 |

本轮最重要新增缺陷：FVCOM首次partial封存后，模型真实修正脚本、bash检查通过；
第二次finish仍回旧manifest。旧三件套自身完整，却没有纳入修正，不能声称交付已更新。
另有共同构建路径误拦：合法owned目录被旧活动角色门排除，相邻source-worktree出口依赖退役route。
两项进入下一实现片；取消/授权来源/受控MPI通信交Core，不在节点复制底层机制。

干预保留：FVCOM三次精确UI批准、Slurm探测一次；补件原题按原材料追加，时机与上轮不同。
FVCOM三个local作业均终态，原PID及进程组已无成员；记录目录保留不算孤儿。
首轮Experiment结束后已向两个残留平台续跑发出停止请求；停止送达不冒称物理清理完成，
不计入原题业务成功，也不审下游科学结论。
本机原始证据保留于被测检出的`.tmp/preview-node-slices-0928/`；不复制进产品，
分享前须脱敏Slurm探测的短时令牌。看板只保留主要问题和owner，不逐工具记账。

## 必要后续片（独立开发树，未部署）

从固定574081d7建立`lujy/impl-experiment-v3-hpc-followup-0928`，不热改上述UI。
仅修两类可达共同缺陷：构建入口按实际批准写边界判断，不按目录名字/退役route拒绝；
finish把未完成事务恢复和已封存后的真实C修订分开。后者复用Core同ID amendment，先保存
前版完整manifest与新pending，再更新三个head；中断恢复不增造版本，节点审计同源。
不增加模型登记步骤，不改A，不重跑已完成实验。Core普通reader的混代可见性与完成接线仍未修。

当前定向：构建13通过，真实make/受管提交/只读负探针；局部基线7红6绿。
交付定向106通过；独立修订8通过，含保存/冻结中断、陈旧前版、v2→v3、归档损坏、
glob及目录成员删除。真实脚本“partial→修复→检查→finish”在旧入口会留旧身份，现形成v2。
旧CMake/scientific重放测试改为真正相同请求；旧“任意新参数一律忽略”的断言已退役，
新请求修订和未提交事务忽略新参数分别有测试，不能把两种语义重新混同。

受管通信缺口已按用户授权提报并指派wangd：#1166。不声称MPI运行能力已修复。

本片节点全量为**4179 passed / 1 failed / 22 skipped / 6 xfailed，184.68秒**；
唯一失败是既有object-omitted，不修改判据。此前13格失败是已封存后改summary/outcome或省略
verification/deliverables却期待重放；迁移为完整原请求，四文件73通过，另补不同主张形成v2
的正向对照。未提交事务忽略新参数的恢复覆盖保持不变。两个旧路径fixture本次未失败，
不据此声称修复了它们的宿主位置问题。完整JUnit在`.tmp/hpc-followup-0928/node-tests-final.xml`。
全量后只为观察失败响应补owner/closure/无副作用/恢复说明，并增加对应可恢复反例，未改其拒绝条件。

167与181的四个技能文件已集成，采用精简、按需加载方式，不替换Harness元数据；
四项既有检查与真实Project-bound加载通过，新reference全文可读取且不进入常驻L1。
没有自动安装外部库，没有执行远端代码，加载不证明专业效果；会话2另做有界自然题对照。

### 本片根检查与登记增量

根refusal/旧工具whitelist/one-throat合计**2 failed / 17 passed**；one-throat通过。
只读对比574：safe_bash 33→33、resource_manager 108→108、delivery_transaction 1→1、finish 11→12。
新增一处机械登记项为`finish._finish / closure_observation_unavailable`，建议B类：
无法读取当前对象时不冒称新交付已经核验；保留旧C，无本次副作用，修复读取条件后重试finish、
不重跑载荷；持久交付损坏交runtime owner接管。登记属于framework owner，不改共享表。
事务中另新增11个自定义完整性异常，扫描器不计：版本/前驱身份、历史manifest、并发替换等。
这些保护同一C的版本事务，不能由模型填revision绕过；不能用AST净0冒称没有新增完整性条件。

### 固定检查点与完整门检

代码固定为`dbad7f66b60e8b1cc6f78721bd6d3efea3670777`，相对574共21文件，全部节点范围；
工作树干净，无upstream、没有产品push。补丁通过186号发送，SHA-256为
`4e9149f292a97228c559a02bc3e47ddbfa2a6c4c349602036069cc63a361f642`。
183接受方案不是代码复核结论，固定补丁尚待会话2独立复核。

固定HEAD运行协作gate，`--base 574081d7 --no-baseline`；离线复用已有Python环境，
显式跳过权限外docs重建。全仓3.11编译、改动范围、跑前后干净通过。
根tests为4123通过/6失败/30跳过；全nodes为4837通过/6失败/34跳过/6预期失败，
其中Experiment只有已知object-omitted失败，另5项属于postprocess。
**整体gate未通过**：CI依赖离线补齐失败，不能声称环境与CI等价；C5未跑，C8跳过。
这轮包含最后补充的观察失败响应及回归，不拿先前WIP代替固定提交验证。

根六红涉及旧工具whitelist、refusal登记、内部事务字段被当成模型参数、返回字段被当成工具名、
payload的python3解析与当前venv不一致、platform导入缺pydantic。未做完整基线对照，
不把六项都直接归为既有；也不为静态扫描让模型填写内部revision或修改只读根测试。
日志保留于`.tmp/hpc-followup-0928/gate/`，187号同步独立复核方。

固定补丁独立复核与新版本UI效果尚未完成，跨owner/环境缺口明确保留，不称可发布。
