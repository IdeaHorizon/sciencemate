---
name: hpc-build
description: |
  通过目标软件的官方构建系统安装、构建并诊断源码型 HPC 应用。首次源码构建前，
  或构建、链接、ABI、头文件、生成配置失败后使用。
applies_when:
  - 涉及源码构建、配置、CMake、Make、MPI、ABI 或依赖图
  - 先前构建或链接尝试产生了需要诊断的证据
  - 启动任何主程序二进制前 —— 包括本 run 没有构建、直接复用或继承的已有二进制
tools_used:
  - preflight_build_resources
  - safe_run_bash
  - safe_write_file
  - write_scratchpad
expected_outcome: 可复现的官方构建路线及经验证产物，或有证据的 blocker
status: validated
---

# HPC 源码构建

1. 读取冻结运行契约、源码侦察、`build_env`、`platform_profile` 与
   `build_graph`。配置锁定 MPI/GPU/ABI 前先调用 `preflight_build_resources`
   取一份资源咨询（capability_issues / capacity_warnings / recommendation）：
   **它不拦编译**，编不编由你定，把它的疑虑与你的决定记进 experiment_log。
   上游明确给出 CPU、内存或 walltime 时逐值提交，`fixed` 计划不能在执行时
   偷偷增加余量。
2. Experiment 必须主动弄清正确路线：先确认目标软件的正式名称、版本、release/分支
   和目标平台，再读取该版本对应的官方安装/构建文档与发布说明；随后读取源码中的
   README/INSTALL/BUILD、构建脚本、CI 与源码侦察候选，核对官方路线在当前树中是否
   存在、参数是否适配、解释器/依赖是否齐全。把选择及其可复核证据写入
   `declared_route`，包括入口、固定参数、解释器/依赖、预期产物与源码版本。
   `declared_route` 是记录选择的承诺，不是 LLM 自我授权；不能仅因看见 Makefile
   就假定裸 `make` 是正确入口。
   框架能提取 `build_graph`（哪怕 partial）时，首次 major build **不要求**先声明
   declared_route。只有提取不可用时才走 fallback：
   `save_artifact(artifact_type='declared_route', name='<route_name>', content=<JSON/YAML>)`，
   必填字段写错会被直接拒 —— `route_type`、`activities`、`env_domains`、
   `expected_artifacts`（逐字要求见 `build_contract.DECLARED_ROUTE_CONTRACT`）。
   不得把 declared_route 存成 `artifact_type='experiment_log'`。
3. 遵循官方层级：官方构建系统；带官方文档变量的官方构建系统；针对已观察失败的
   官方文档；最后才请求方向。不得把依赖图替换为手工逐文件编译，也不得在失败后
   无证据地换入口、改用裸 `make` 或后台任务。
4. 工具链选择只写入本次运行的 `env/build_env.sh`，每个主要命令均加载它；编译器、
   MPI wrapper、库与 launcher 必须来自相互兼容的家族。优先使用 `build_graph`；
   图不可提取时使用已验证的 `declared_route`。
5. 只在声明的 `build_root` 或 `source_worktree_root` 中构建，保持
   `source_baseline_root` 只读。持久保存配置/构建命令、版本、环境变化、产物路径及
   patch/diff 证据。
6. 每个阶段核验预期库或可执行文件是否存在，并更新里程碑/问题账本：组件、最近错误、
   已尝试修复与状态。没有新证据不得重新打开已通过阶段。
7. 每次真实失败先分类为环境、依赖、参数、源码、资源或运行时；一次只诊断一层：
   编译、链接、运行环境、配置、运行。提出可证伪原因，做最便宜的区分性探测，进行
   一项受限修改并验证。反复失败时改变假设，记录 dead end 或请求帮助，不得重复同一
   编辑或无依据切换路线。
   查修复方案按固定顺序，不得跳级：① 对照适用的 SKILL 匹配已知症状 —— **认识**
   （有对应症状且有明确修复步骤，模糊相似不算）才直接修；② 不认识就查目标版本的
   官方构建/故障文档，并对照该软件的官方或社区 Spack recipe 中声明的依赖、变体与
   已知补丁；只采用与当前版本、工具链和已观察错误相符的修法，先做受限预览；
   ③ 官方资料与 recipe 仍不能解释，或同一证据支持的修复失败 2 次，才进入自由诊断，
   并把新证据写进 experiment_log。
   一条路线经 ≥2 次尝试仍失败、决定换方向时，**立刻**调用
   `create_claim(claim_type='dead_end', scope='org', dont_repeat_reason='<失败路线>: <根本原因> (<症状关键句>)')`。
   不得等到实验结束再补。目的：下次 run 的 agent 从开局注入里就能看到这条
   dead_end，直接跳过已知死路 —— 实测省 20-40 轮。

8. **运行前预检（每个主程序二进制启动前逐项过，缺一不启动）**：
   - **P1 链接完整**：`ldd <binary>` 无 `not found`。缺库只把缺的**单个库所在目录**
     加进 `LD_LIBRARY_PATH`；禁止把整个 conda/lib 塞进去 —— 会引入第二套
     MPI/netCDF 运行时。
   - **P2 MPI 一致**：`ldd <binary> | grep -i libmpi` 看二进制链的 MPI 来源，启动用的
     mpirun/srun 必须来自同一套安装；MPI 编译的二进制必须经其官方启动方式运行。
   - **P3 cwd 正确**：`workdir` 必须是声明的 `build_root` 或 `run_root`。很多科学程序
     从 cwd 读输入并写日志；丢失 cwd 会在错误目录启动或静默失败。
   - **P4 时长对账**：namelist/输入卡的模拟时长（`run_days` / `nsteps` 等）必须与
     prereg 或研究计划的 period 一致；禁止沿用模板默认值 —— `run_days=0` 是空转，
     会以 returncode 0 "成功"结束却什么也没算。
   复用或继承来的二进制**同样要过 P1-P4**：它上次能跑不等于这次环境下能跑。

优先使用 case-local 环境变量、构建配置与 overlay，而不是移动或删除系统/conda
文件。生成缓存可以重建，但可复现修复必须落在官方或 case-local 配置接口中。
本 Skill 不授权调度器提交、GPU 假设或科学 verdict；涉及这些问题时加载对应 Skill。
