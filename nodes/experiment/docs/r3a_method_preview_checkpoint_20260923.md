# 060 本地方法链验证（2026-09-23）

## 已实际发生的结果

主会话在 `codex/060-r3a-c @ 2bdec9a8` 上运行了一次真实、本地、无网络的
CMake 探针。该提交相对 `e0c78221` 只增加授权恢复测试与文档，运行时代码未变。

执行链为真实的：

`State → bootstrap → safe_run_bash → CMake 配置/编译/运行 → finish → delivery audit`

- 复用只读源码 `nodes/experiment/fixtures/toolchain_smoke_src`；没有下载或改写源码。
- 任务开始即要求交付 `outputs/experiment/build/hf_toolchain_smoke`，不在 finish 时降标。
- 一次有界执行调用内部完成 `cmake -S … -B … -DCMAKE_BUILD_TYPE=Release`、
  `cmake --build … --parallel 1` 和实际运行程序；没有声明/修订路线或手工 verify/finalize。
- 真编译器为 GNU 12.4.0；执行退出 0，stdout 包含 `HF_TOOLCHAIN_SMOKE_OK`。
- 实际命令耗时 0.664 秒；这是本机本次观测，不是性能承诺或新门槛。
- 前后源码 SHA-256 相同；`finish(success)` 返回 `required_satisfied=1/1`，无 gap/contradiction。
- raw/clean/log 三件冻结；`audit_experiment_delivery` 返回 `passed=true, status=sealed`。

探针通过节点真实执行器，不直接在宿主 shell 代做构建；没有 mock 编译、退出码、
产物、finish 或 audit。测试驱动器负责组织调用和独立断言，不声称模型自主选择了这些动作。

## 证据边界

这一结果证明当前内核可承载一条真实的短构建闭环，不证明所有 HPC 任务已变流畅。
探针标准输出保留在本会话工具结果中；一次性临时 run/build 根已自动清理，不能将它
称为持久原始证据包。可重跑回归已固化在
`nodes/experiment/tests/test_060_real_cmake_delivery.py`：

- 正例真实构建、运行和保存 stdout 文件，执行任务开始就冻结的三项要求，再完成交付及审计；
  重复 finish 不重新编译、不增发三件套或 finish 记录。
- 反例通过节点写文件工具在临时工作根准备坏 C 源码，真实编译退出非零；虚假 success
  被拒，但随后 honest failed 可冻结三件套并通过同一个 audit。

新回归不 mock 执行器或账本。缺编译工具时明确 skip，当前宿主实际运行两格均通过、没有
skip。它仍不是模型自主规划测试，也不是实际 WRF 构建。

本轮既有回归另已复跑：正式任务/交付/动作输入四文件 41 passed；
`test_060_s1d_natural_flows.py` 36 passed。后者的 Slurm 场景使用替身，不是真 Slurm。

尚未验证：真实 LLM 自主执行、UI/父编排实际创建 Experiment child、Core 所有终止路径、
受管作业的完整方法预览、最终树真实 Slurm、scientific 最小闭环。Core completion 缺口仍
由 #1141 跟踪；这个探针不能替它宣称端到端完成，也不能作为发布/合并批准。

未启动服务、LLM、下载、远端作业或持续 worker。编译进程已退出；清理只涉及本次生成的
临时 run/build 根，没有删除源码、用户数据或历史 benchmark 证据。
