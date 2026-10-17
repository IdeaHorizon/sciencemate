# 受管作业通信能力交接（2026-09-28）

@wangd 用户已授权核查后直接提报。此项需要 Core/平台提供能力，Experiment 负责消费接线；本次没有修改 Core，也没有创建 PR。

## 问题与影响

当前原生执行入口缺少“允许同一受管作业内通信、仍隔离外网和无关宿主服务”的能力。已安装 MPI、成功链接 MPI 程序，不等于当前受管环境可运行 MPI。

2026-09-28 node20 的真实 FVCOM 任务中，MPI 版已成功编译；执行时 `MPI_Init` 出现 `bind(): Permission denied`，独立 socket 探测也失败，普通沙箱和本地受管作业使用同一网络隔离能力。不能将该现象归因于编译失败或 lujy 没有远端 Slurm 权限。

用户原请求是“下载编译 FVCOM”，未要求运行 MPI；本缺口不否定已有编译成果。这里提出的是后续真正要求 MPI 运行时的能力缺口，不把新增验收强加给原任务。

## 固定基线与证据

- 产品基线：`574081d77d6caae98fa3c37a9bf79e310eb89c52`。
- `nodes/experiment/tools/resource_manager.py:3973` 的 `_NativeLocalJobLaunchAdapter.prepare` 调用 `sandbox.prepare_launch`，未传网络能力。
- `core/sandbox.py:975`：`prepare_launch(..., network_access=False)`。
- `core/isolation/__init__.py:164`：`CommandSpec.network_access` 是布尔值，现有契约明确只有框架拼装的取物命令可以开启，普通模型命令不能开启。
- `core/isolation/_landlock_exec.py:110`：网络隔离时声明 TCP bind/connect 受控但不授予网络规则，包括本机回环 TCP 在内均被拒绝。
- `core/isolation/linux.py` 已区分 `net_scope=all/tcp/none`，尚无“同一作业内通信”的策略。
- 本次只读核对 node20 的 `/proc/sys/kernel/apparmor_restrict_unprivileged_userns` 值为 `1`；既有现场选择了 Landlock TCP 隔离。仅有 sysctl 值不代替后端能力探针，也不能假设普通用户可创建需要的网络命名空间。
- 现场原始证据保留在 node20 产品检出的 `nodes/experiment/.tmp/preview-node-slices-0928/data/project-worktrees/`；作业隔离报告为 `net_scope=tcp`，不声称覆盖 UDP/ICMP。

查重检索了 MPI、通信、bind、loopback。#699 是取物通路问题，#893 是受管 GPU 能力问题，#797 是部署准入问题，均不是本项“作业内通信”的同一缺口。

## 建议的最小目标

先支持：**经过授权的本机双进程 MPI，在同一受管作业内完成通信计算；外网和无关宿主服务仍不可访问。**

复用既有 `CommandSpec → backend.prepare → NativeLaunch/受管生命周期`，扩展明确的作业通信策略、授权消费及实际能力报告。策略绑定具体作业和授权，不由模型自行把普通命令的 `network_access` 改成 true，也不按 `mpirun` 等命令名特例放行。

可考虑独立作业网络命名空间等能兑现边界的机制，具体由 Core 与部署条件决定。**Landlock 端口许可本身不能证明 peer/IP 限定或作业级隔离。** 后端无法兑现时，提交前准确报告能力缺口和部署前提，不能静默退化为全网开放。

本项首批不要求实现多节点 Slurm/PBS adapter；当前 Experiment 真提交 factory 只有 local，dry-run 脚本不算远端受管能力。

## Owner 与并行方式

- **Core/平台**：通信策略与授权消费、实际隔离、能力记账，继续承担物理作业生命周期。默认断网不是错误；直接给普通 MPI 打开取物专用全网开关才是错误修法。
- **部署管理员**：必要时提供受支持的 namespace 或受信启动能力；不默认关闭主机安全策略。
- **Experiment**：从 Core 读取同一能力事实，区分“已安装”与“受管可执行”，选择合法路径；缺失时提前说明，不反复换命令撞相同限制。不另造网络执行器。

双方可并行：Core 定义并实现供给接口，节点按稳定接口准备消费与预检测试；Core 尚未具备能力时不声称节点已支持受管 MPI。

## 验收（相称的真实进程测试）

1. 授权后，一个真实受管作业内两进程 MPI 完成最小通信计算，留下实际执行与结果。
2. 同一作业访问外网、无关宿主监听端口被实际拒绝；其他作业不能继承该授权。
3. 未授权或宿主能力不足时，在载荷执行前准确说明缺口，不静默扩大网络范围。
4. 原有文件只读/可写边界保持有效；取消和超时确实结束启动的 MPI 进程，恢复/重复调用不重复提交。不能只拿进程组信号或请求参数充当完成证明。
5. 收据区分请求策略、实际生效边界及未兑现能力；能力宣称须有配置与行为证据，不能由传参推定隔离成功。

## 提报结果

- 已创建 [#1166](https://desktop-9el2944.taile9f15e.ts.net/wangd/harness-framework/issues/1166)，Forgejo API 返回 HTTP 201。
- 正文已 `@wangd`，返回 assignees 为 `wangd`，指派成功。
- 本地仅交接文档，无产品实现提交；没有 PR、merge 或产品分支 push。
