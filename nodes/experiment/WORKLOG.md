# Experiment 工作日志

## 2026-09-15 — benchmark 单独放在 benchmark 分支

- 用户决定：Experiment benchmark 不合入 main，单独保存在 `experiment-benchmark-suite`（案例、driver、判卷、答案夹具、实测记录）。原因是代码量大（1000 多个文件、约 4.5 万行），不改任何产品代码，其他人也不需要看。
- 本分支删除随 `22089b61`、`af0450c5` 带进来的旧 benchmark 材料，共 67 个文件：
  - `EXPERIMENT_BENCHMARK_SET.md`（v0.2 设计稿）：benchmark 分支归档在 `test_runs/benchmark/legacy/benchmark_set_v0.2.md`，只少一句 `a45d5b44` 补的「cpus 是请求值」说明，该句留在 git 历史里；
  - `test_runs/benchmark/` 的 v0.12-native D 系列实测记录（49）、fixtures（5）、materials（10）：benchmark 分支有逐字相同的副本；
  - `test_runs/benchmark/scripts/` 的 `drive_pauses.py`、`run_case.py`：benchmark 分支有同名新版本。
- `AGENTS.md`、`README.md`、`ROADMAP.md`、`TEST_RECORD_HPC_AI.md` 注明 benchmark 分支单独存在。节点代码里的 `bench_enabled` / `EXPERIMENT_BENCH_GROUP` 开关早已在 main，保留不动。

## 2026-08-27 — 路径角色投影与源码构建隔离

- 对应提交：`e51a6063 fix(experiment): 对齐源码构建运行路径沙箱`；后续
  `83273e4f` 仅回退了不在节点授权范围内的 `pyproject.toml` 与 `uv.lock`，不改变
  Experiment 的净功能改动。
- 根因：Core 已提供节点输出根，但 Experiment 未将其完整投影为源码、构建、安装和
  运行角色；`safe_run_bash` 使用 Core 的粗粒度 sandbox 根，导致外部/共享
  `run_root` 可能意外只读，而 workspace 中 immutable baseline 的父目录又可能可写。
  此外，`safe_write_file` 会拒绝冲突路径角色，Bash 入口却仍可能启动子进程。
- 改动：以 Core `node_output_dir` 为唯一目录锚点，默认派生
  `runtime/source`（managed source）、`build`、`build/install` 和 `runtime`；将
  path roles 投影给 `safe_run_bash` 子进程 sandbox；对冲突角色在 spawn 前
  fail-closed；baseline/dependency 保持只读，外部 `run_root` 在已具备平台 capability
  时可写。本地 `submit_job` 继续使用框架拥有的本机写边界并拒绝外部 workdir，未因
  路径角色单方面扩大本地写面。
- 安装原则：不把 CMake 当作唯一情形。凡工具提供原生安装前缀、安装根或输出目录选项，
  都显式定向至 `<build_root>/install`；参数名称由工具决定（如 CMake
  `-DCMAKE_INSTALL_PREFIX`、Autotools/Meson `--prefix`、Cargo `--root`），而不是
  强行使用统一的字面量 `-prefix`。无该能力的工具必须在受管 build/run 路径生成或
  staging 产物，并记录实际位置；禁止系统或账户级默认安装目录。
- 验证：CMake smoke 源码在 run-local `runtime/source` 构建，安装至
  `build/install/bin/hf_toolchain_smoke` 并成功运行；Experiment 测试套件当时通过
  `965 passed, 1 warning`。

## 2026-08-27 — 受管作业时间语义与唯一生命周期

- 正式构建、simulation 与其他 `process_tree` 改由现有 `submit_job` 从启动时统一
  持有 local/SLURM/PBS 的 submission intent、作业身份、路线收据、恢复、取消和
  finalize 生命周期；`safe_run_bash` 保留为有界同步诊断，超时仍终止诊断进程树。
- 时间合同拆为四类：`foreground_wait_s` 只控制同步等待；
  `expected_duration_s` 只触发 overdue；`stall_after_s` 只作健康分类；只有显式
  `hard_deadline_s` 或 SLURM/PBS `walltime_minutes` 才允许墙钟终止。local 省略硬期限
  时仍保留 PID、内存、磁盘和取消监督，但不生成 `RuntimeMaxSec`。
- `foreground_wait_s` 发生在 job_submission artifact、外部工作流和路线 submitted
  收据持久化之后；等待到期返回 `wait_elapsed_running`，不取消作业，也不会把路线
  写成 failed。健康投影区分声明式 progress、普通日志活动和无进展证据，CPU/日志
  活跃不再冒充可信科学进展。
- SLURM/PBS 省略 walltime 时不再写隐含的 60 分钟指令，而是记录站点默认未知；
  Kubernetes 只有显式 hard deadline 才生成 `activeDeadlineSeconds`，且在平台补齐
  PVC/volume 合同前仍按既有规则拒绝提交。


## 2026-08-27 — 乐观准入与作业/资源联合健康

- 修改前的链条把“能否启动”“运行时压力”“资源耗尽”压成同一道算术门：
  MemoryMax 既被当作请求，又被当作启动前必须空出的预留；任务运行后，PID/内存
  采样达到 95% 就先于内核事件抢杀。结果是 4/8 GiB 笔记本可能连轻量任务也无法
  启动，而正常链接或高内存阶段可能被误停；反过来，外部健康探针看不到监督器的
  资源证据。

```text
修改前
请求 → MemAvailable 是否覆盖“宿主保留 + 整个 MemoryMax”
  ├─ 否：拒绝
  └─ 是：启动 → 80% 预警 → 95% 抢杀 → 事后读取 cgroup 事件
                              └─ 作业健康与资源健康彼此不可见

修改后
请求
  ├─ 确定超过宿主安全总容量：拒绝并报告 deficit
  └─ 可行且具备最低启动余量：启动到 cgroup
       ├─ 作业健康：进展 / 无证据 / stalled / failure / terminal
       └─ 资源健康：healthy / pressure / critical / unknown / exhausted
                     │
                     └─ 联合只读决策
                        ├─ 正常进展 + 高利用率：继续并加密采样
                        ├─ 无进展证据 + 资源恶化：诊断，不建议取消
                        ├─ stalled + 资源恶化：建议受管取消，不自动执行
                        ├─ Swap 拒绝：临界压力；继续采样并联合判断
                        └─ OOM/PID 拒绝或宿主/磁盘紧急线：整树终止
```

- 三种内存量已分工：资源请求只表达任务意图，MemoryMax 是 cgroup 硬上限，
  startup headroom 只判断 supervisor/payload 能否安全创建。宿主保留线按容量比例
  推导，小平台不再被固定 4 GiB/2 GiB 保留量直接吃空；明显超过宿主安全总容量的
  fixed 请求仍在确认、intent 和 spawn 前拒绝。
- 同步前台监督器与持久本地作业监督器复用同一个纯资源分类器，统一采集 PID、内存、
  Swap、cgroup/宿主 PSI、内存/PID 增长、磁盘和实时 cgroup events。80%/95% 只改变
  健康等级与采样频率；OOM、`pids.events.max`、宿主/磁盘紧急线仍终止整棵
  cgroup。Swap 拒绝使用相邻采样增量作为可恢复的临界压力证据，不再单独杀掉
  仍有进展的任务。连续遥测失败进入 unknown，单次读失败不再杀掉长任务。
- 告警拆成 current 与 history：压力解除会记录 recovered，当前告警清空并恢复 1 Hz，
  不再让一次预警永久把状态钉在 running_warning。
- 外部作业探针只读既有 resource-guard JSON，不复制阈值，也不直接取消任务。它保留
  原有 `health_state`，新增 `resource_health`、`decision` 和证据。
  GPU 整机利用率仍仅作 advisory；在没有 device UUID/作业归属绑定前，本轮没有把
  别人的 GPU 使用率变成当前作业的终止依据。
- 验证：资源守卫、资源管理器、外部作业健康与 MPI 核心联合测试 `223 passed`，
  其中新增 5 个 Swap 回归，覆盖终态降级、累计事件增量、payload 返回码、同步与
  持久监督器的告警恢复。Experiment 全量测试为
  `1574 passed, 5 failed, 1 warning`；5 项均无本轮生产代码
  调用链：当前 Python 环境分别缺少 `pip`、`_ctypes`、`_sqlite3`（其中 `_ctypes`
  影响两个 bwrap Unix socket 用例），另有一项检查根目录 `pyproject.toml` 缺少
  tree-sitter 依赖。根目录依赖文件按用户要求不在本分支提交范围内，因此这些失败
  诚实保留为环境/基线问题，不能记成本功能通过，也没有证据表明是本轮资源回归。
