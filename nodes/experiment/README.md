# experiment 节点

**Owner**: TBD —— 需要 **HPC 实验执行 + 统计推断 + 实验方法学** 背景
（v2.1 起科学裁决归 Analysis/hypothesis，experiment 仅作执行层评估）。

按 run contract 执行计算实验、收集原始 log；primary simulation 严格按 **frozen pre_registration** 执行，产出可回放的
`clean_results`，并负责执行层 provisional/inconclusive 评估、credibility 守门和
methodological / dead_end sediment。**严格按 prereg，不许偏离**。

职责：执行 → 监控 → 诊断 → 解析运行输出 → 结果清洗/聚合/异常标记 → 冻结
`clean_results` 与 `experiment_log` → 执行层评估/credibility → Analysis 交接 → sediment。
不做实验设计、正式输入数据预处理、科学图片或项目级综合结论。

## I/O 契约

| 方向 | artifact_type | 备注 |
|---|---|---|
| Input | `pre_registration`（仅 primary simulation，frozen）| diagnostic、toolchain_build 与 secondary run 无需 prereg |
| Input | `dataset`（可选）| 输入数据文件 |
| Output | `raw_results`（必需，已 freeze）| 真实输出或 operation 验证收据的文件清单；记录 path、SHA256、bytes、role 与 retention |
| Output | `experiment_log`（必需，已 freeze）| 固有输出物；primary 完整记录，secondary/pilot/failed 可短格式，缺失时仅自动补一份明确不可分析的状态记录 |
| Output | `clean_results`（必需，已 freeze）| 真实运行输出经可回放解析、清洗、聚合与异常标记后的结构化结果；无可分析结果时也必须显式记录状态与原因 |
| Output | Analysis handoff + KB claim | frozen experiment artifacts 交给 Analysis 更新 research_state；`create_claim(methodological/dead_end)` 可沉淀方法学或失败教训，或在 log 中明确说明本次没有 |
| Output | `run_manifest`（自动）| 每个已初始化 run 的角色、状态、协议/数据集引用、transcript/log 路径和 SHA256 |
| Output | `environment_snapshot`（自动）| `repro_snapshot` hook 在 run 结束采集工具链、源码版本、环境变量、硬件和结构化 credibility/verdict metadata |
| Output | `repro_bundle`（条件自动）| 仅欠正式裁决（`requires_hypothesis_verdict`）的 run 生成完整复现包 |


所有 run 的**必产出**均为冻结的 `raw_results`、`clean_results` 和 `experiment_log` 三件套；
operation 的 `clean_results` 是 `record_kind: operation` 的验证回执，
scientific 的 `clean_results` 则是可回放的测量结果。

三件套的“必须产出”与“调用方收到哪些摘要或 artifact”是不同概念：Experiment 负责生成、冻结并
记录它们，不在节点内决定调用方回传策略或后处理路由。

## 5 阶段工作流

1. **读取运行契约** — primary simulation 解读 prereg 并提取参数；其他运行确认任务、stage 和工具安装
2. **准备输入** — primary simulation 按 prereg 写输入并确认数据就位；其他运行准备所需工具链或诊断环境
3. **执行与监控** — 正式构建/运行统一由 submit_job(local/SLURM/PBS) 持久化生命周期；safe_run_bash 仅作有界诊断，hooks 检查健康与错误证据
4. **结果整理** — 用可回放脚本解析真实输出；primary simulation 按 prereg 清洗/聚合，所有运行保留 status、anomaly flags 与 exclusions
5. **归档与交接** — 保存并 freeze raw_results、clean_results 与 experiment_log → 执行层评估/credibility → 可选 KB 登记 → Analysis → sediment

## 本地资源决策链

```text
资源请求
  ├─ 请求确定超过宿主安全总容量 → 启动前拒绝，给出结构化缺口
  └─ 请求可行且有最低启动余量 → 进入 cgroup 硬边界
       └─ 运行时统一采集 PID / 内存 / Swap / PSI / 磁盘 / 宿主余量
            ├─ healthy                         → 继续，1 Hz
            ├─ pressure（≥80% 或压力信号）     → 继续，4 Hz
            ├─ critical（≥95%、Swap 拒绝或强压力信号）
            │                                  → 继续，4 Hz
            ├─ unknown（连续遥测缺失）          → 继续并诚实标记，4 Hz
            └─ exhausted（OOM/PID 拒绝，
               或宿主/磁盘紧急余量耗尽）        → 整树终止并分类失败
```

`probe_external_job_health` 不再重复推导资源阈值，而是只读监督器状态，将
`health_state`（作业进展）与 `resource_health`（资源事实）联合解释。
CPU 满载本身不是异常；没有可信进展证据也不等于 stalled。探针只返回继续、
加密采样、诊断或受管取消建议，不直接执行取消。

## 文件结构

```
nodes/experiment/
├── harness.yaml          # 节点配置（system_prompt、rules、tools、hooks 与机械门禁）
├── hooks.py              # hook 注册与运行期编排
├── owner_config.yaml     # 默认模板
├── review_spec.md        # 8 维评审 rubrics
├── README.md             # 本文件
├── tools/                # HPC 诊断、构建图、环境、资源、安全包装和契约审计工具
├── skills/               # GPU/HPC 与证据链 skill
└── fixtures/
    └── minimal.yaml      # WRF 12km 24h CONUS baseline（E2E 验证用）
```

Experiment benchmark（案例、driver、判卷、答案夹具和实测记录）不在本分支和 main 里，单独保存在 benchmark 分支 `experiment-benchmark-suite`；要用最新节点代码跑 benchmark，把节点分支或 main 合进该分支（见 `AGENTS.md`《兼容、分支、交接与交付》）。

## 关键设计原则

- **不在本项目中实现具体科学工具的业务逻辑**。LAMMPS/VASP/GROMACS/WRF 等工具的
  编译、参数配置、运行方式，agent 应查询对应官方文档或按条件加载领域 skill；本目录的
  工具只提供通用 HPC 构建、诊断、资源和审计能力。
- **本项目只定义**：实验执行的约束（rules）、归档与机械审计（freeze gates 与 contract_audit）、
  错误监控机制（hooks）、以及实验与框架的 I/O 契约。
- **工具环境检测**：`which` / `module avail` / `conda list` 等 shell 命令
  即可完成，不需要手写 Python 扫描脚本。
- **运行错误检测**：由 diagnose 工具和 failure hooks 负责；科学结果解析由可回放脚本完成，
  并把 source/hash、参数和异常规则写入 `clean_results` lineage。
- **启动前 gate**：`experiment_preflight` 只校验本次 Experiment 消费的冻结
  `pre_registration`、run contract、path roles 和已有资源计划；失败时禁止真实 HPC 提交，
  不审查调用方或上游前处理方法。
- **终态审计**：contract_audit.py 只核对成功的 tool_result、artifact content
  和 run metadata；verdict 是否真实更新、sediment 是否真实产生，不交给 LLM judge 猜测。

## 路径角色契约

目录名不等于权限。结构化路径角色会投影到 Experiment 子进程 sandbox；
最具体角色优先；workspace_root 可写但不是 job workdir 或清理根，
其下 immutable baseline 保持只读：

| 角色 | 何时需要 | 写权限 |
|---|---|---|
| `source_baseline_root` | 源码实验通常需要；已有 binary 可省略 | 永久只读 |
| `managed_source_root` | 每个 run 的源码下载/解压区 | 可写；默认 `runtime/source`，不得在此编译 |
| `source_worktree_root` | 只有需要修改源码或源码内构建时 | 可写；源码变更必须留 patch/diff |
| `source_patch_root` | 可选的 patch/overlay 独立目录 | 可写；目录可省，但变更记录不可省 |
| `experiment_root` | 可推导的逻辑容器 | 不直接写 |
| `build_root` | 编译缓存、产物和安装前缀 | 可写；默认 `build`，安装在 `build/install` |
| `run_root` | 实际运行时基本需要 | 可写 |
| `dependency_root` | 本 run 管理/staging 依赖时 | 默认只读 |

旧字段 `source_root`、`build_src`、`workdir`、`case_dir`、`deps_root`
只在输入边界兼容；新 artifact、scratchpad 和 `experiment_log` 使用规范角色名。
baseline 不为每个 run 复制；可复用受版本控制的 worktree，并用 baseline revision、
worktree revision 和 diff/patch 保证回退与复现。scope_guard 事前阻断对 baseline 的
显式写入；每次 run 结束还会做 baseline 完整性审计：Git 源码比较 HEAD diff 和未跟踪
文件内容，非 Git 源码计算文件树 SHA256。没有容器或 user namespace 依赖；若审计发现
变化，质量门会失败并在 transcript 留下变化路径。远程任务同样适用，但其源码目录必须对
提交节点可见；否则应由集群存储 ACL 管理只读发布区。

未显式声明路径时，Experiment 从 Core 的节点输出根派生 run-local 的
源码、构建和运行目录；项目绑定时该根由 Core 指向该项目的 Experiment 工作区：

```text
runs/<run_id>/outputs/experiment/
├── runtime/          # 默认 run_root；应用输入、输出、checkpoint 和命令日志
│   └── source/       # 默认 managed_source_root；下载/解压源码，禁止 in-source build
├── build/            # 默认 build_root；编译缓存和产物
│   └── install/      # 约定 install_prefix（由构建工具的原生安装前缀/输出选项指定）
└── repro/            # run_manifest、环境快照、patch 记录和条件生成的 repro bundle
```

默认 `build_root` 已为每个 run 分配；需要跨 run 复用 baseline/worktree/cache 时，
仍必须由编排输入显式声明对应规范角色和版本/锁契约。安装不得使用系统默认前缀。
凡构建工具提供安装前缀、安装根或产物输出目录选项，均必须优先显式指向
`<build_root>/install`（例如 CMake `-DCMAKE_INSTALL_PREFIX=...`、Autotools
`--prefix=...`、Meson `--prefix=...`、Cargo `--root ...`）；`DESTDIR` 仅在其语义
与最终安装根组合后仍落在该目录时使用。工具不提供这类选项时，执行配方必须将其
产物复制或生成在受管的 build/run 路径，并记录实际产物位置。没有跨进程独占锁时，不允许多个
run 并发共享同一可写 worktree。项目级 `deliverables/` 由 Core artifact policy
管理，Experiment 不直接创建其子目录或复制 `experiment_log`。

## 跑通

```bash
python run_node.py --harness experiment --sandbox \
  --fixture nodes/experiment/fixtures/minimal.yaml
```

## 不该做

- **改 prereg**（科研诚信）
- 静默删除异常点、缺失值或失败 run；必须保留 flag、规则和样本/run 身份
- 做**项目级宏观综合**判断（"这堆 verdict 加起来够写论文了吗"是
  `_reviewer(source_node_type='_project')` 的活）
- 制作科学图片、图表、示意图或多面板 composition（Scientific Visualization / postprocess 的活）
- 做项目级结构化结论生成（reviewer/project_synthesis 的活）；本节点只做执行层 provisional/inconclusive 评估、credibility 和 sediment；最终科学 verdict 属于 Analysis/hypothesis
- 实验设计 / 参数规划（hypothesis 节点）
- 在项目中硬编码具体科学计算工具的调用方式
