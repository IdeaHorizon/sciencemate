# 灵活执行环境：节点实现与 Core 接线

用户目标：用自然语言指定本机、某个 WSL 发行版或服务器，以及软件、已有环境或安装目录。节点自主完成获授权工作；不要求固定部署方式、环境登记或先填计划。组织服务器是现有可选路线，不是 WSL/SSH 的前置手续。

## 分工

- Experiment：复用已有软件，按目标准备依赖与专业自检，保存真实日志、动作和交付证据；维护自身 Slurm adapter 的提交、监控、取消、恢复和结果回传。项目内独立环境只作默认建议；不能重复否决 Core 已授予的外部写能力。
- Core/平台：通用执行目标与凭据交付、本机实际写边界、进程启动和资源限制、整体恢复。WSL 与通用 SSH 执行后端仍归 Core；Experiment 的 Slurm adapter 不构成通用远端安装或进程执行后端。复用现有机器/项目归属及 run/action/job 身份，不另建授权账本或作业状态机。

## 本次节点改动

运行时提示允许复用获授权的原位置软件、源码和安装目录；不强制先复制源码、创建空环境脚本、登记环境或填写计划。Core 已授予的外部安装写能力沿用到实际子进程，节点声明本身不授予写权，更具体的只读边界保留。

已有构建环境脚本供工具链探测和 Bash 执行共同消费；探测通过 Core 执行边界加载脚本，不临时改宿主全局环境。未选脚本时使用实际当前执行环境；所选环境初始化或边界失败须如实报告，不能悄悄回退到宿主环境。

上述构建环境部分仍运行在 Core 已有执行端上。下述 SSH Slurm 增量只作用于既有 Slurm 工具，不增加软件安装服务或作业状态机。


## 2026-11-04 维护者整合验收

本 PR 的验收范围是已有环境复用、授权路径投影和 SSH Slurm 协议，不代表历史 060 路线中所有科研任务已经验收。Core 通用 SSH 执行后端已由 #1313 合入主干；本适配器继续复用个人 OpenSSH 配置与原作业账本，不替代该后端。

- 真实 Slurm 22.05.8 隔离 CPU 集群，运行 slurmctld、slurmd、slurmdbd 与真实 accounting；完成上传、提交、终态、哈希回传、重复收集及取消。测试集群为 nonce 的终态恢复启用 `AccountingStoreFlags=job_comment`；站点历史不保存 nonce 时保持 unknown，不能自动重投。
- macOS 端在真实 sbatch 完成后丢弃 SSH 响应，恢复找回同一 nonce 的唯一作业，再完成回传；没有第二次 sbatch。
- Windows 端使用个人版随包 Python 3.12 与 Git OpenSSH 访问同一集群。发现并修复 Git OpenSSH 在约 8 KiB 处截断远端命令的问题：启动命令保持很短，带长度的脚本帧和文件字节走 stdin，实际 shell 环境和同连接身份校验继续生效。
- v3 无旧 route 的提交按原 nonce 校验本 run 账本 owner、目标和已有绑定，恢复写回同一账本；目标/owner/已绑定作业冲突均不替换身份。旧参数 `route_attempt_id` 在 v3 恢复调用中填写原 `submission_nonce`。
- 本地只读子目录在 allow-list 后端上复用 Core 的补集规则并冻结缩小后的根；超出预算拒绝启动。受保护目录的父级可能不能新建条目，应使用洞外的已有具体目录，不能重开整个父根。

可复跑的操作协议验收（会创建两个有界 CPU 作业，须在已获授权的测试账号/分区执行）：

```bash
python nodes/experiment/scripts/accept_slurm_ssh.py --host <个人SSH别名> --remote-base <共享工作目录> --output-dir <本机证据目录>
```

证据摘要见同目录 `acceptance-20261104.json`。Windows 随包运行时验收覆盖 worker 使用的原生 Python/SSH 路径；未覆盖安装器、完整 GUI、生产站点、GPU/MPI 或远端逐作业写隔离。此次没有部署或发布 Release。

## SSH Slurm 初始候选（2026-10-22，以下保留当时设计说明）

`submit_job` 与 `discover_resources` 增加可选 `slurm_target`：`host` 可为个人 SSH 配置中的别名，另可传 `user`、`port`、`login_shell`（默认 true）。跳板机使用个人 SSH 配置的 `ProxyJump`；不在工具参数、账本或提示里存密码、私钥或任意 SSH options。运行前需已有可用的 OpenSSH 非交互认证及可信 host key；目标不需要部署 ScienceMate 服务。

远端提交端需有 Bash、`python3`，并在所选登录环境的 PATH 中提供 Slurm CLI；作业脚本的输入准备也使用 `python3`。不传目标时保留原本机入口。传目标时，在 Linux 提交节点执行 Slurm 命令，Windows 只负责调度与文件传输，WSL 不是前提。探测、提交及后续操作使用同一登录环境；软件初始化可放在既有作业 command 中，例如站点的 `module load`，不另填环境登记表。

远端 `workdir`、`output_dir` 与 `output_paths` 使用 Linux 坐标。工作和输出目录必须已存在且由计算节点共享；正常共享盘别名在确认前解析成实际路径。输入仍用 `stage_in` 的本机 src 与远端相对 dst：核对获准内容后传到 nonce 专属启动目录，再由作业脚本复制。不能把 Windows 路径、父目录穿越或符号链接当作远端目标。原提交确认绑定 command、目标、路径及输入哈希；已在预授权范围内不重复增加确认。

目标、实际用户、集群与原 nonce 保存在既有 intent/job 中，状态、取消、终态和 ACK 恢复消费这些事实。同号作业可用可选 `submission_nonce` 区分；查询失败保持 unknown，不自动重提。取消请求已发送不等于已取消，仍需原作业的终态证据。

第一次可信终态把远端输出窗口冻结进原 `job_terminal`。结果回传写入 `experiment_output_dir(state, "runtime")/returned/<nonce>/`，文件名由远端路径摘要与安全 basename 生成，不展开远端归档。`job_output_collected` 只是原 B 账本中的传输收据：本地镜像核对后仍归属原 job producer，下载不能制造计算成功。已有本地修改不被覆盖；断线后可用 `job_status(collect_outputs=true, max_output_bytes=...)` 重传，不重算、不改原终态。默认单次总回传预算为 256 MiB，可按需提高；超额文件保留远端。超过快照哈希阈值的文件明确采用冻结元数据与传输时哈希，不宣称早先已有完整内容哈希。

边界保持明确：当前公开真实 Slurm 请求仍限单节点、单任务、0 或 1 GPU；没有增加 MPI、多节点、array 或自动安装 Slurm。文件快照有数量与耗时上限，重传不能补造遗漏文件在过去时点的生产身份。静态路径保护与账号权限不等于远端逐作业 OS 写隔离。Windows 原生回归、POSIX 文件原语测试不替代 exe 沙箱内访问 SSH、真实集群生命周期与科研结果验收；生产集群尚未部署；当前验收范围见上节。

## 已有入口与当前缺口

1. `core/isolation` 和 `core/sandbox.py` 提供本机执行边界；`core/sandbox.write_roots_for/manifest_for` 是授权依据。仅在节点声明 `writable` 不是权限。
2. `platform/backend/app/pro/relay.py` 和 `projects_across_homes.py` 支持组织项目由 Linux 服务器回答。`server_deploy.py` 部署的是 ScienceMate 服务，不是任意 HPC 软件安装器。
3. 通用执行目标由 Core 的 `core.execution_target` 与隔离后端负责，主干已含 WSL 和 #1313 的 SSH 入口。Slurm adapter 只覆盖既有作业协议；它不决定通用目标、凭据或跨环境安装权限。
4. `core/host_capabilities.py` 已有软件声明。UI worker 的环境白名单未完整交付 `HARNESS_HOST_CAPABILITIES`；需由 Core 提供受信配置交付，在实际执行端核验，不能把宿主登记当作远端安装事实。
5. 无 SSH 目标时 `resource_manager._detect_slurm/_run` 继续使用 worker PATH。SSH Slurm 分支在选定目标的登录环境中发现、提交、查询与取消；这不等于构建环境脚本或所有 Core 工具都已支持跨主机执行。
6. Core 的项目路径解析仍限制工具工作目录和 `safe_write_file` 的写入坐标。外部安装可从项目工作目录向已授权前缀执行；任意目标工作目录、文件工具和跨环境路径映射需由 Core 提供一致接口，节点不得绕过解析器另写外部文件。

## 最小验收

- 用户选已有软件时直接使用；没有环境脚本时不先造空文件或要求登记。
- 本机用户目录、指定 WSL 和指定 SSH 主机上的安装与自检，均在真实目标执行；权限已足够时不重复问，同名软件在不同目标上不串用。
- Core 授权的外部目录可用；节点伪造角色、只读子目录、相邻未授权目录、符号链接逃逸及框架状态仍不可写。
- 环境初始化一致作用于探测与运行；初始化失败不能转成宿主默认环境的假成功。
- 长任务能够取消或恢复；丢失提交响应时核对既有作业，不能自动重复提交。结果回传继续使用现有证据链。

本文件是跨 owner 接线说明，不是运行时新增流程，也不代表尚缺的通用 WSL/SSH 后端已经实现。集群实机验证、部署与安装须按用户实际任务范围分别进行。
