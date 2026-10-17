# HPC + AI 专业方法与评测：已知调研及复用候选

调研日期：2026-09-28。维护：Experiment 开发会话；当前整理人 review-a1。

本页是供开发者参考的调研记录，不进入运行时提示，不是新契约、安装清单或第二份开发队列。
当前优先级、分工和实施状态只在 [ROADMAP](../ROADMAP.md) 维护；权限与事实 owner 仍服从
[节点规则](../AGENTS.md) 和 [正式架构](experiment_governing_v3.md)。外部资料不是授权来源。

## 1. 先读结论

- 公开资料值得复用，但“有 skill”“加载过 skill”“方法被正确采用”“真实任务改善”是四种不同事实。
- 最有价值的组合通常是：目标版本官方依据 + 按需领域方法 + 实际可执行检查 + 独立对照；不是批量安装技能包。
- HPC 专业度同时包括环境/并行使用判断、数值与领域验证、资源效率、故障诊断和可复现交付，不等同于算得大或跑得快。
- 先定位真实缺口：缺知识补方法，反复漏检补检查资产，重复构建评估环境复用，缺物理事实/生命周期能力交真正 owner。不存在一种 skill 可以补齐这些全部能力。
- 模拟调度、真实计算、硬件成绩、代码优化和完整 agent 工作流分别报告；不能相互代替，也不能用总分抵消真实性或安全问题。

### 核查程度怎样读

本文“已查入口”指读过作者 README、SKILL.md 或指定官方页面；“部分参考已查”只覆盖明确列出的主题，
不是整包脚本/模板审计。除 §6 的本机帮助/版本探针外，候选均未在本轮安装、执行或接入 Experiment；
没有候选被本页认定为已验证有收益。链接多指 main/latest/stable，内容会变；日期不是代码锁定，采用前需锁
适用版本/commit、内容身份、依赖与许可。官方资料优先用于解释软件语义，第三方技能用于整理方法。

## 2. 先分清五类资料

| 类别 | 主要回答的问题 | 在本节点的合适位置 |
| --- | --- | --- |
| HPC/AI 领域 skills | 该怎样准备、运行、检查与诊断 | 既有按需方法层；不掌管权限、任务合同或作业真相 |
| Agent/skill 行为 benchmark | 给方法前后，模型是否更会完成任务 | 独立评测与原始执行证据；不把判卷答案放进任务 |
| 软件/数值/硬件 benchmark | 算对没有、软件有多快、通信/硬件瓶颈在哪里 | 测量工具和参考工作负载；不直接代表 agent 能力 |
| 环境、回归和平台工具 | 能否稳定复用构建/配置、比较不同环境 | 优先已有站点工具与适配边界；不引入第二个调度 owner |
| 官方验证与复现方法 | 检查适用性、容差、结果解释依据是什么 | 领域检查和证据说明；不自动升级为每个任务必过门禁 |

## 3. 已核查的 skill 候选

本节是有限候选，不是完整生态排名。SciMate 的作者[索引](https://github.com/SciMate-AI/HPC-Skills/blob/main/skills-index.md)
还列有 CFD、FEM、求解器、GPU、可视化等方向；未逐项审查，不因此声称已支持这些软件。

| 候选与来源 | 可复用的专业方法 | 已知边界与适用时机 |
| --- | --- | --- |
| SciMate [hpc-lammps](https://github.com/SciMate-AI/HPC-Skills/blob/main/skills/hpc-lammps/SKILL.md) | 输入顺序、单位、势场/原子类型、系综、邻居设置、输出和失败诊断 | 已查入口及[单位/步长参考](https://github.com/SciMate-AI/HPC-Skills/blob/main/skills/hpc-lammps/references/units-neighbor-and-timestep.md)；全部模板/脚本未审。经验性预处理需看任务，不能每次机械最小化或改原物理模型 |
| SciMate [hpc-mpi](https://github.com/SciMate-AI/HPC-Skills/blob/main/skills/hpc-mpi/SKILL.md) | MPI 家族、编译 wrapper、launcher、rank/thread/绑定及调度器接入判断 | 已查入口；深层参考未全部取得。命令存在不证明 ABI/PMI/PMIx 兼容，多 PID 不证明同一并行计算；站点授权与资源限制仍由框架拥有 |
| SciMate [hpc-toolchains](https://github.com/SciMate-AI/HPC-Skills/blob/main/skills/hpc-toolchains/SKILL.md) | 编译器、CMake、库与运行时的一致性，可复现工具链 | 已查入口，未审全套资产。用于实际构建/链接缺口，不重复本节点已有通用 hpc-build；不能无条件改变站点环境 |
| SciMate [hpc-spack](https://github.com/SciMate-AI/HPC-Skills/blob/main/skills/hpc-spack/SKILL.md) | specs/variants、externals、环境锁定与二进制复用 | 已查入口；只有重复构建或依赖漂移值得解决时才评估。Spack 不是每个任务的新必需依赖，缓存密钥信任不随 skill 自动授权 |
| SciMate [hpc-quantum-espresso](https://github.com/SciMate-AI/HPC-Skills/blob/main/skills/hpc-quantum-espresso/SKILL.md) | 赝势、截断能、k 点、占据及 SCF/relax/bands 工作流 | 已查入口，未做全包审计或本机 QE 验收。语义须对照[目标版本官方指南](https://www.quantum-espresso.org/Doc/pw_user_guide/)；不能从一次 SCF 推定物理结果或收敛研究已经可靠 |
| K-Dense [pytorch-lightning](https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/pytorch-lightning/SKILL.md) | 已用 Lightning 时的训练/验证/测试分离、checkpoint、调试与性能方法 | 已查入口及部分 trainer/callbacks 参考，未审全包。不得为了 skill 迁移用户框架；仅凭参数量选分布式策略不够；写了续训参数不证明完整状态恢复 |

SciMate 根目录标注 [MIT](https://github.com/SciMate-AI/HPC-Skills/blob/main/LICENSE)；复制实质内容须保留许可声明，
仍核对所选文件及依赖。Lightning 候选入口许可与仓库总许可须按实际复制范围核查；本轮未复制其代码或模板。
所有候选里的外部联网、安装、调度、上传、自报完成或作者推广指令都不能自动继承为 Experiment 契约。

## 4. Benchmark、回归和测量候选

| 来源及核查范围 | 值得借鉴的内容 | 不能用来证明什么；本节点使用建议 |
| --- | --- | --- |
| [benchmarking-hpc-skills 的特定 MVP](https://github.com/aboucaud/benchmarking-hpc-skills/blob/main/docs/mvp-misuse-benchmark.md)，已读 | 有/无 skill 与站点说明的对照；已注入误用的修复；好坏样本校准；实际行为与推测资源收益分报 | 该 MVP 的 Slurm echo-stubs 不执行载荷，测修复不等于从零规划或真实资源节省。仓库另有后续路线，不把整个项目概括成永久只有模拟。只借鉴方法，不复制完整 harness |
| [HPCAgent-Bench](https://github.com/spcl/HPCAgent-Bench)，已读 README | 数值代码优化的参考正确性、真实计时、预热/重复测量和成本对照 | 不是完整实验安装、输入科学性、调度恢复与交付评测。优先借测量设计，不为首个小包部署独立评测服务，也不擅自优化/改写用户求解器 |
| [ReFrame 官方文档](https://reframe-hpc.readthedocs.io/en/stable/index.html)，已查入口 | HPC 编译/运行回归、正确性与性能参考；测试逻辑与系统配置分离 | 检查充分性仍需领域定义。已有站点使用时可评估复用；不在节点内另嵌其调度生命周期。未在本机安装或执行 |
| [OSU Micro-Benchmarks](https://mvapich.cse.ohio-state.edu/benchmarks/)，已查官方说明 | MPI 通信延迟、带宽、collective 等定向测量 | 通信微基准不能证明应用科学正确或端到端更快；只在真实通信瓶颈需要区分时使用，不作为所有任务必跑项 |
| [HPCG](https://hpcg-benchmark.org/)，已查官方入口 | 代表性计算/通信模式的系统性能参照 | 硬件/系统成绩不是 Experiment 专业度成绩，也不替代目标应用。当前仅留索引，不纳入两天包 |
| [MLPerf Inference](https://mlcommons.org/benchmarks/inference-datacenter/)，已查官方说明 | 固定模型/数据、质量目标与负载场景下的推理性能 | 不证明训练、恢复或 agent 流畅度。简化任务只能叫借鉴方法，不能冒称官方 MLPerf 成绩；数据、模型许可和真实设备前提另查 |

“benchmark skill”也可能只是教模型怎样跑基准的文档，不必然附带独立判卷器。评估时先确认
它测的对象、真正执行的部分和证据来源；LLM 对报告的评分可作解释补充，不能替代实际运行事实。

## 5. 不应遗漏的其他重要方法

| 方向与来源 | 已知可参考的方法 | 适用边界/本节点建议 |
| --- | --- | --- |
| 数值验证：[NASA 网格收敛教程](https://www.grc.nasa.gov/www/wind/valid/tutorial/spatconv.html) | 网格细化、观察收敛阶、渐近区、Richardson/GCI 的条件与限制 | 数值收敛不等于真实物理模型已验证；不把 CFD 的方法机械套到所有任务，不把残差下降当全部收敛证据。是否需要额外计算服从任务与资源包络 |
| 领域语义：LAMMPS [units](https://docs.lammps.org/units.html)、[NVE](https://docs.lammps.org/fix_nve.html)、[thermo_modify](https://docs.lammps.org/thermo_modify.html) | 单位体系、系综条件、总量/每原子量，以及检查适用性 | 负势能本身不是失败；NVT 不能机械套用体系总能量恒定。本文来源为当前网页，执行前匹配实际软件版本 |
| 性能测量：LAMMPS [Measuring performance](https://docs.lammps.org/Speed_measure.html) 与[运行说明](https://docs.lammps.org/Run_basics.html) | 区分串行速度与并行效率，识别通信/工作量/绑定影响，读取真实运行计时 | 小体系不保证并行加速；跨 rank 的长期轨迹可能因舍入分叉。建议分报启动、计算、IO、排队与网络等待，不用总耗时掩盖具体原因 |
| 环境复用：Spack [environments](https://spack.readthedocs.io/en/latest/environments.html)、[build caches](https://spack.readthedocs.io/en/latest/binary_caches.html) | manifest/lock、依赖与变体配置、既有安装/二进制复用 | 已读说明，未探测本机 Spack。latest 当时为开发版文档，采用需匹配实际版本。先查现有站点安装；内容身份、兼容性与信任分别核对，不能默认授权信任缓存密钥 |
| AI 恢复与复现：PyTorch [checkpoint](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html)、[randomness](https://docs.pytorch.org/docs/2.14/notes/randomness.html) | 区分只存模型与续训所需状态；随机性控制及确定性的代价和限制 | checkpoint 文件存在不证明新进程恢复；固定 seed 不保证跨版本/平台逐位相同。按实际任务核 model/optimizer/scheduler/scaler/RNG/sampler 等适用状态，不强迫非 PyTorch 任务安装 PyTorch |

还应在真实任务中持续记录：输入/输出来源与内容身份、完整原始日志、实际资源使用、IO/磁盘压力、
重复下载/编译/计算、数据划分与泄漏、模型指标与统计不确定性。它们是重要观察方向，不是本轮已经
实现的能力清单，也不据此选择新的持久状态、全局缓存或平台依赖。

## 6. 与当前代码、现场及候选算例的关系

### 已有能力不能重复造

2026-09-28 静态核对产品 `a2cf3d162ff2ca8db883303945a6b3163c7d02f6`；本文交接从同运行代码的独立
`05a295551e72a07bed9cd774a8907cb5204e249c` 文档树制作。以下是已查文件，不是新功能声明：

- [hpc-build](../skills/hpc-build/SKILL.md) 已有官方构建路线、工具链、分层诊断与运行前检查。
- [scientific-results](../skills/scientific-results/SKILL.md) 已要求实际执行适用质量检查，不能只补另一篇相同原则。
- [harness.yaml](../harness.yaml) 已有按需 skill 入口。是否被模型有效使用需真实任务观察，不靠注册成功推定。
- 只读核查 Core `skill_loader` 与共享 `load_skill`：支持列出/读取 references、validation 等资产文本；这不等于脚本已安装或执行器能直接访问框架源码。采用检查资产时须验证完整读取、合法保存、实际执行、结果入既有证据链的可达性；不能新增内部抄表手续。
- [dataset_checks](../tools/dataset_checks.py) 负责输入包结构，不证明科学适用性；专业结果检查不应塞进全局输入准入。

现有 hpc-build 的逐项预检有适用性值得验证：串行/静态程序、稳态任务不能机械套用 MPI/动态链接或某应用时长字段。
这是静态候选反例，不是本页已复现的自然任务 bug，更不是直接删掉安全检查的依据。

### 本机探针与 LAMMPS 候选

仅运行帮助/版本探针，未运行 MD 或 MPI 作业。现有 LAMMPS 报告 `22 Jul 2025 Update 4`、MPICH 4.2.0；
默认 mpirun 报告 Open MPI 4.1.6；MPICH launcher 路径存在。它们提示应核对运行栈，不能从路径存在声称兼容，
也不能未运行就断言必然失败。软件入口 SHA256 为 `9a7eade10a7bb9b39f29cec971d339931aa2766aaffbfae08056e94f9f48bcb8`，
实际二进制 SHA256 为 `e7f9f0675b63d0ec9014ce93f8ae90f3794df40c8c9ffac26bae06601f9e69af`；这是当时观测，不是未来环境保证。

首包建议以该版本[官方 LJ 输入](https://github.com/lammps/lammps/blob/stable_22Jul2025_update4/bench/in.lj)为依据，
选小型 NVE 数值可靠性与 CPU 并行对照。固定初态、物理模型、相同物理时长及资源预算，观察时间步长敏感性、
能量变化、实际 rank 与合理性能解释；不要求混沌轨迹逐字一致，不预设步长减半必然四倍改善，不为过检查改变势函数。
这仍是候选：还没有固定任务包、自然执行基线、专业检查实现或改善结果。框架受管 MPI 不可用时披露依赖，不裸启动绕过。

该候选不证明源码构建、多节点/Slurm、GPU、长期平衡采样、真实材料预测或完整 checkpoint 恢复。
既有固定题库的 Verlet 一维谐振子可用于低成本方法前测，但不能冒充真实多粒子 MD/MPI；题库实例、答案、
driver 和成绩继续留在独立 benchmark 分支，不复制进产品。主会话当前四题测试不由本文接手或重新判卷。

## 7. 从调研到采用：最小验证与删除条件

这些是开发取舍方法，不是运行时新增门禁：

1. 定位具体缺口及 owner；先查现有方法/工具，避免重复建设。原版本已经足够好就不预设需要修改。
2. 固定所需来源版本、许可、依赖与所复制文件；只审和引入必要部分。参考中的联网/安装/提交仍受现有权限限制。
3. 按关键不确定性做相称验证。纯资料/方法纠正先核专业依据、适用性、工具契约与许可，不先跑长编译或全套benchmark；新增可执行检查器才验正常、错误、合法不适用和缺证据。参考与容差不按结果临时调绿。
4. 需要证明运行收益时，再在同一任务/模型/资源条件下做少量代表性对照，保留实际尝试和人工介入；不要求每份skill先完成大型评测。未实测的效果标未验证，不因资料已采纳就声称能力已实证。
5. 自然题面只包含业务目标、必要输入与约束，不教内部避坑；答案、隐藏判卷和预填检查结果不进入执行上下文。
6. 分别比较既有八维：任务达成、专业正确性、证据可信、安全资源、恢复稳健、交互流畅、时间成本、框架协同。真实性/安全不被均分抵消；模型解释和机器检查分开报告。
7. 无实质收益、误拦增多或成本抵消收益时，不集成或撤回本次增量；保留负结果。技能数量、上下文长度、单元测试通过数不是能力成绩。

原则上的接入位置：知识进既有按需方法层；可重复检查进有版本适用范围的资产/工具；执行事实进既有 B/C；
平台差异由真实 adapter/capability owner 处理。不得引入第二套授权、作业状态或 finish 结论。

## 8. 后续维护

新增条目至少记录来源/版本或读取日期、读到哪一层、适用场景、限制、拟复用部分、验证结果和决定。
“发现候选 → 已审适用 → 已实现 → 已运行 → 有改善证据 → 决定采用”不可跨级；实施 owner 与当前阶段只回写 ROADMAP。
本页不宣称全面审计所有 HPC/AI skills。出现不同软件、硬件、任务或站点限制时，先补相关来源和真实证据再扩展。
