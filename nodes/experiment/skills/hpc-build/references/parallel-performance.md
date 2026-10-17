# 并行布局与性能诊断

用于 MPI、MPI+threads 或其他多进程程序的 affinity、伸缩、吞吐、抖动和性能回退。
目标是把“运行慢”变成可证伪假设，而不是先堆启动参数。

## 先用已有证据

- 先读已有命令、调度事实、rank/thread 日志、阶段计时、profiler 与产物检查。用户只要分析时，
  分清“已证明”“证据支持的候选”“仍缺的观测”，不要默认重建或启动新作业。
- 只有缺失信息会改变建议时，才提出最小区分性探测；真实作业仍走框架受管入口。
  launcher 存在、dry-run 或本机启动成功不证明计算节点上的 MPI/通信可用。
- 性能候选必须保持任务工作量、精度、必需输出和正确性标准。未通过同一结果检查时不能报加速。

## 建立可比基线

保留输入与规模、算法/精度、停止条件、结果检查、可执行文件与构建身份、编译器/MPI/关键库
版本、实际分配、完整 launcher 命令、并行环境变量，以及每次原始计时。指标按任务选择
time-to-solution、稳态吞吐或延迟分布；排队时间单列，初始化、计算、通信/同步和 IO 能分则分。

冷启动与 warmup 后稳态分别报告。基线和候选采用相同预热、重复和停止规则，交错或随机化
顺序以降低时段漂移；报告中位数和离散范围，不只留最快值。单次更快只产生候选。强伸缩
固定总问题，弱伸缩按资源扩展问题；加速比或效率必须写明资源基线与等价工作量。

## 资源、映射与绑定

记 `R`=总 ranks、`r`=每节点 ranks、`p`=每 rank 的 processing elements、`t`=每 rank
可能同时运行的 CPU 工作线程、`c`=节点实际分给本作业的 processing elements。先核对
`r * p <= c`，并说明 element 是物理 core 还是 hardware thread；scheduler slot、Open MPI
processing element、Linux CPU ID 与物理 core 不是天然同义词。

- 纯 MPI 可从每 rank 一个 core 的可解释基线开始，但这不是最优定律。
- 混合并行的 `p` 应覆盖同时活跃的 CPU 线程。除 `OMP_NUM_THREADS` 外，盘点嵌套 OpenMP、
  BLAS、数据加载、通信进展等线程池；不用的池显式限制。
- SMT/hardware threads 必须作为独立候选实测，不能当作物理 cores 直接宣称资源翻倍。

按同一次运行核对四层：scheduler 实际分配/cpuset；rank 到节点与拓扑对象的映射；每 rank
最终 CPU 集；进程内线程是否落在该集合。不同节点都出现 `core 0` 不表示冲突，必须联合
hostname、rank 与 CPU 集判断；同节点多个 rank 得到相同单 CPU 集才是争用强证据。Slurm
抽象 CPU 编号可能不同于 Linux CPU ID，不能和 launcher 或 `/proc/self/status` 的
`Cpus_allowed_list` 直接逐号比较而不注明编号来源。交付布局时按 host+rank 记实际 CPU 集，
在同一编号空间核对它是否处于分配内、大小及同 host 交叠是否符合独占/共享意图；常规
布局命令只是候选映射，不能据此推算每个 rank 的连续 CPU 编号或顺序。
作业允许的 cpuset 只是可用范围；精确 rank→CPU 列表须有本次各 rank 的实际 affinity
（如 rank 进程内的 `Cpus_allowed_list`）或等价绑定观测，并核对编号空间与拓扑。无法观测
时不列已生效的精确编号，不能把分配集按 rank 想当然切段；同版本文档支持的显式有序配置
可以给标明待实测的预测，不等于实际绑定。

## Open MPI 的版本化候选

先记录实际 `mpirun --version`、程序链接的 MPI 与本机 `mpirun --help`，再查同一主/次版本
官方文档。分别说明 `--map-by`（进程放置）、`--rank-by`（rank 排序）与 `--bind-to`
（可用 CPU 集），不要依赖随版本、资源管理器和站点配置变化的默认值。

Open MPI 4.1 与 5.0 文档均支持 `--map-by ...:PE=<p>`。以下只是用实际 `R/r/p` 替换、并经
本机帮助确认后的诊断模板，不是通用修复：

```text
mpirun -n <R> --map-by ppr:<r>:node:PE=<p> \
  --bind-to core --report-bindings <application> ...
```

5.0 文档将 `--report-bindings` 标为弃用并推荐 `--display bindings`；4.1 使用前者。Slurm
allocation 的 tasks、CPUs-per-task 应与 `r/p` 一致，并遵循站点支持的一条 launch 路径；
不要无证据地嵌套 `srun` 与 `mpirun` 或用手写 hostfile 覆盖 scheduler 分配。

- `--bind-to none` 只适合作为受控对照，不是线程程序的默认最终修法。
- `--use-hwthread-cpus` 会改变 slot/processing element 含义，只在明确测 SMT 时使用。
- `--oversubscribe` 只允许超额放置，不创造 CPU，也不是一般性能修复。

## 区分瓶颈再行动

| 观测模式 | 下一项区分性证据 | 尚不能断言 |
| --- | --- | --- |
| CPU 集重叠或线程多于允许 CPUs | host+rank+CPU-set 与线程池清单；修正后等价对照 | 重复的跨节点 core 编号就是冲突 |
| cores 忙且计算阶段主导 | 应用计时、profiler 热点、编译/vectorization 与频率 | 总 CPU 利用率高就一定 compute-bound |
| 节点内早平台化且带宽/cache/NUMA 指标饱和 | 硬件计数器、NUMA locality、每 socket/rank 对照 | 无计数器时已证明 memory-bound |
| 跨节点变慢且通信/等待占比上升 | rank 级等待、消息模式；必要时用微基准区分平台与应用 | 微基准快就证明应用通信无问题 |
| 少数 rank 决定最大阶段时长 | rank 最大值、中位数与工作量分布 | rank 平均值足以证明均衡 |
| IO/元数据阶段占比上升 | 字节数、文件数、输出频率、存储位置与 IO 计时 | 关闭必需输出仍是等价加速 |

优先用应用计时和现有日志；仍不能区分时才增加 profiler、计数器或微基准。先改变一个可解释
因素，确有交互再做有界矩阵。交付时分列事实、候选原因和最小下一步；性能结论须有等价工作量、
正确性检查、重复对照、波动及资源基线，否则保持 `proposed`/`unverified`，不承诺加速幅度。
CPU 集或线程落点不符，先定位映射、绑定、编号空间或线程 runtime，再修正并复验；结果
不等价，先查正确性，不报加速。布局与正确性均通过仍未提速时，不能倒推绑定失败；按
阶段计时和区分性证据转查计算、内存、通信/同步、
负载不均或 IO。缺少实际对照运行时只给候选，不称性能已改善。

## 已核对的官方入口

以下入口于 2026-09-28 核对；实际任务仍匹配已安装版本与站点文档：

- [Open MPI 4.1.8 `mpirun`](https://www.open-mpi.org/doc/v4.1/man1/mpirun.1.php)
- [Open MPI 5.0.x `mpirun`](https://docs.open-mpi.org/en/v5.0.x/man-openmpi/man1/mpirun.1.html)
- [Open MPI 5.0.x affinity](https://docs.open-mpi.org/en/v5.0.x/tuning-apps/affinity.html)
- [Open MPI 5.0.x with Slurm](https://docs.open-mpi.org/en/v5.0.x/launching-apps/slurm.html)
- [Slurm CPU Management Guide](https://slurm.schedmd.com/cpu_management.html)
