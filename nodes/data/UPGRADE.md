# data 节点职责与架构说明

## 节点职责

- 输入来自调用方给出的明确前处理请求，可选附带上游阶段契约；research plan 不是必需输入。
- 节点按调用范围生成下游实验/模拟所需数据、几何/结构、离散资产和求解器输入文件。
- 节点不创建研究问题或假设，不运行正式模拟/训练，不解释结果。
- 调用方已确定科学范围；节点不选择、排序、比较或延期假设。

## 通用实现

- `PreprocessingRequest`：锁定 `plan_bound` 或 `request_bound` 权威来源及请求哈希。
- `PreprocessingWorkOrder`：把计划 stage 或明确资产编译为统一 work unit DAG。
- `analyze_preprocessing_requirements`：完整结构化契约本地编译，含糊文本或未提取计划再调用模型推导输入资产。
- `run_preprocessing_planning_loop`：统一审批可执行 DAG，并按结构化执行/审核意见做有界定向回修。
- `prepare_scientific_mesh`：计划要求网格或粒子结构时使用；已有 Gmsh 资产转换为 OpenFOAM 时使用 `operation=convert`，若上游已完成转换则直接复用审核结果。
- `execute_preprocessing_python`：计划授权后的通用解析、转换和文件生成。
- `build_scientific_preprocessing_package`：只生成 executor-owned staging 中的候选资产。
- `execute_preprocessing_plan`：统一执行审核、定向修复、manifest/lineage 收口和 dataset 发布。
- 审核不按学科堆叠完整 reviewer：机械检查按格式/产物能力复用，模型 authority reviewer
  统一判断产物是否符合原始 research plan 或明确请求。
- 专业生成适配器按需惰性加载；新学科保留计划声明的学科名称，内部可走通用数据能力。
- `pipeline_contract.py` 是唯一跨阶段结果协议；旧工具状态仅允许停留在工具内部，并在执行边界
  映射成七类 outcome。Reviewer 只给 `RevisionContract`，Planner 不发布终态，Executor 不写
  blocked report，`data_agent_loop.py` 的 pipeline controller 是唯一终态决策者。
- 回修顺序固定为 asset → step → plan；是否继续由实际进展决定，而不是按学科或固定次数决定。

## 维护与修改准则

data 节点后续修改必须遵守“先复用、再合并、后删除，最后才新增”的顺序：

1. 修改前先用 `git log`、`git blame`、`git diff` 和代码搜索检查同类问题是否已有修复、
   现有权威抽象位于哪一层，以及失败实际发生在请求、规划、执行、审核还是交付阶段。
2. 已有同类实现时，必须修正或泛化原实现；不得在其他模块再写一套相似关键词判断、
   门禁、fallback、状态转换或审核逻辑。
3. 同一事实只能有一个权威解释位置：自由文本在请求规范化阶段解释一次；规划层只消费
   结构化资产和工作订单；执行层只执行批准的 DAG；审核层只核对实际产物和锁定需求。
4. 新增条件前必须证明现有协议、schema、registry 或公共工具无法表达该条件。能够通过
   合并、删除分支或调整既有边界解决时，不得新增 helper、兼容垫片或领域专用门禁。
5. 修复不得只针对单次 transcript、某个文件名或某个学科关键词。真实失败运行可以作为
   回归 fixture，但生产规则必须基于 `plan_kind`、`review_profile`、AssetContract、
   WorkOrder 或工具能力等稳定契约。
6. 完成修改后必须检查净代码量和重复搜索结果。若代码增长，需要说明不可复用的原因；
   若新增实现替代旧实现，应在同一修改中删除旧路径，不保留双轨逻辑。
7. 验收至少包括相关单元测试、真实失败路径的阶段验证、`compileall` 和 `git diff --check`；
   不得通过放宽最终审核来掩盖规划或执行层错误。

以上准则只允许修改 `nodes/data` 范围；其他节点的协议问题应单独提出，不得顺手改动。

## 交付物

前处理包按请求实际需要组织，不强制创建某个学科的目录。plan_bound 通用骨架为：

```text
data_preprocessing/<case>/
├── manifest.json
├── README.md
├── stages/<stage>/
├── data/                  # 仅当计划需要输入数据
├── visualization/         # 仅当计划要求可视化导出
├── audit/
└── reproducibility/
```

request_bound 单文件交付只包含目标文件、`manifest.json` 和
`audit/preprocessing_review.json`，不创建 `stages/` 或额外实验输入。

数据驱动模型脚本、外部数据下载、网格和材料结构都不是默认步骤，只在调用请求明确要求
相应前处理资产时启用。
