# 普查：nodes/experiment + nodes/data（2026-08-31）

计数：403 拒绝点 → A=68 / B=21 / C=248 / **D=66**；另 `status: blocked/needs_*` 家族
~55 处几乎全 D（plan 审批链），实际裁决对象 ≈121。

## D 类逐条（裁决对象）

### experiment/tools/resource_manager.py（7）
| file:line | 强制的要求 |
|---|---|
| resource_manager.py:1005 | workdir 落在 source_worktree_root 时必须另行显式声明 output_dir |
| resource_manager.py:1931 | primary simulation 求解 job 必须声明 stage=simulation |
| resource_manager.py:1946 | 正式输入包未验收禁止任何真实提交 |
| resource_manager.py:1951 | 执行参数与冻结 prereg 不一致即禁止提交，必须走 formal amendment |
| resource_manager.py:1977 | preflight 未通过禁止真实 HPC 提交 |
| resource_manager.py:2560 | 关闭 workflow 的 evidence 必须是本次分析产生的 frozen 非 auto_generated log |
| resource_manager.py:2563 | 该 frozen log 正文必须出现 job_id 字符串 |

### experiment/tools/safe_bash.py（9）
| file:line | 强制的要求 |
|---|---|
| safe_bash.py:2093 | ldd 显示未解析共享库依赖时拒绝运行（预测失败） |
| safe_bash.py:2109 | MPI launcher 前缀不在编译档案前缀集合即拒 |
| safe_bash.py:2121 | 二进制 libmpi 与 launcher 不同族即拒 |
| safe_bash.py:3112 | declared_route 校验器不可用时拒绝构建 |
| safe_bash.py:3153 | declared_route build contract 校验失败拒绝构建 |
| safe_bash.py:3341 | 前置组件未 verified 即拒 build |
| safe_bash.py:3512 | preflight 未通过禁止 simulation |
| safe_bash.py:3517 | 输入包未验收禁止 simulation |
| safe_bash.py:3521 | 参数与 prereg 不一致禁止执行 |

### experiment 其余（20）
| file:line | 强制的要求 |
|---|---|
| contract_audit.py:1077 | data_request 的 acceptance 四项必须全 true 才登记 |
| contract_audit.py:1244 | Data blocked report 必须自记 terminal/recoverable 状态 |
| contract_audit.py:1269 | Data 未记 terminal blocker 不许授权 fallback |
| contract_audit.py:1272 | run contract 不允许 fallback 时拒绝授权 |
| contract_audit.py:1285 | fallback 未事先授权拒绝校验其输入包 |
| sediment.py:151 | sediment closure reason ≥ _MIN_REASON_CHARS |
| sediment.py:156 | 必须先存 experiment_log 才许声明"无 sediment finding" |
| sediment.py:167 | auto_generated 兜底 log 不得声明 sediment closure |
| sediment.py:636 | inconclusive verdict reason ≥20 字、next_step ≥10 字 |
| sediment.py:639 | 必须先存 experiment_log 才许声明 inconclusive |
| sediment.py:648 | auto_generated log 不得形成 verdict closure |
| operation_completion.py:128 | 只有 operation 模式 run 许记 operation 收据 |
| operation_completion.py:136 | external_job 收据必须给受管 job_ids |
| operation_completion.py:138 | generic/build 收据必须给至少一个证据路径 |
| operation_completion.py:170 | 必须至少一项可验证 check |
| operation_completion.py:176 | failed/blocked 收据必须有失败证据且 next_step ≥8 字 |
| operation_completion.py:178 | outcome=blocked 必须先登记 blocker |
| run_contract.py:646 | scope 声明 reason ≥12 字符 |
| timeout_escalation.py:402 | 同一目标签名同步超时 N 次后拒绝再同步执行 |
| resource_fetch.py:282 | fetch_resource 只开放给 experiment 节点 |

### data（30）
| file:line | 强制的要求 |
|---|---|
| mesh_generator.py:504 | 翼型坐标必须 ≥20 唯一点 |
| mesh_generator.py:1996/2375/4609/5027/6427 | 网格已成功但 Tecplot 导出失败即整体判 error（五处同构） |
| execute_preprocessing_plan.py:223 | 本地复用资产必须带已声明 stage_id |
| execute_preprocessing_plan.py:300 | 每个 generation step 必须显式 stage_id |
| execute_preprocessing_plan.py:355 | required_deliverables 未全产出即拒发布整包 |
| execute_preprocessing_plan.py:1663 | step 参数超 plan scope contract 即拒派发 |
| execute_preprocessing_plan.py:1668 | 工具未被 approved plan 授权即拒派发 |
| execute_preprocessing_plan.py:1674 | 工具 allowed_node_types 不含 data 即拒 |
| install_preprocessing_tool.py:316 | CLI 自动安装必须给 public HTTPS license evidence URL |
| install_preprocessing_tool.py:462 | 包未被 approved plan 声明即拒安装 |
| install_preprocessing_tool.py:467 | package_type 必须与 plan 声明一致 |
| preprocessing_planner.py:286 | asset contract 冲突/超范围能力即整份 payload 作废 |
| preprocessing_planner.py:288 | task_scope.allowed_capabilities 不得为空 |
| preprocessing_planner.py:327 | 每条 required_files 必须齐 6 个 contract 字段 |
| preprocessing_planner.py:331 | workflow_capability=unclassified 视为不可路由拒绝 |
| preprocessing_planner.py:354 | required_files 必须声明 stage_id 或 consumer_stages |
| preprocessing_capabilities.py:342 | approved plan 必须授权该前处理工具 |
| preprocessing_capabilities.py:350 | allowed_node_types 不含 data 即拒网关派发 |
| preprocessing_capabilities.py:396 | plan 必须含 execute_*python 生成步骤 |
| preprocessing_capabilities.py:557 | 多步 plan 禁用直接产物生成 |
| preprocessing_capabilities.py:576 | artifact_spec 必须齐 5 字段（含 evidence/acceptance_criteria） |
| web_search.py:2402 | 下载目标不像该 asset kind 的文件即跳过 |
| web_search.py:2421 | 参数文件"不像"CFD 相关即拒保存 |
| scientific_preprocessor.py:8575 | plan 必须含 build_scientific_preprocessing_package 步骤 |
| coordinate_profile.py:205 | 合并后 profile 必须 ≥20 点 |
| package_publisher.py:112 | staging 缺 manifest.json 即拒晋升 |

### blocked/needs_* 家族（~55，几乎全 D，代表条目）
planning/store.py:468 未过 Designer/Critic review 即 blocked；
preprocessing_capabilities.py:333/410/601/736 动态工具只许经 plan 执行/多步禁直跑/格式校验失败即 blocked；
execute_preprocessing_plan.py:1356/1365/1372/1586/1609 非当前 approved plan/确定性失败步抑制重跑/review 失败须 revise；
preprocessing_planner.py:8267/8499/11557/11849/12219/12315 账本无进展/超 scope/Critic 只审 schema 合法 plan/重复修订判定；
install_preprocessing_tool.py:297 需 SPDX 开源许可证；web_search.py:214/247 未经 approved reference plan 不许搜索。
另 contract_audit/preflight 的 {"passed": False} 判决核心（一 run 一份 canonical log、缺 citation_validation、三档 input delivery 未验收、operation 恰一份 log）被上述 D 消费转成拒绝。

## 骑墙判例
1. safe_bash.py:2093/2109/2121 预测失败型拦截 → D（未执行命令，预测失败替调用方做决定；同族异前缀只 warning 不拦，自证是阈值判决）。
2. mesh_generator.py:1996 五处：成功产物被"交付物不齐"降级 → D。
3. resource_manager.py:2557 job 未终态不许标 finalized → B（账变假）；:1951 prereg 不一致禁提交 → D（如实记录偏离后账仍真）。
4. resource_fetch.py:282 node-type 作用域 → D 但性质特殊（架构分层非质量门槛），建议单列。
5. package_publisher.py:112 无 manifest 拒发布 → D（如实标注后账不假，且有 .previous 可回滚）。
