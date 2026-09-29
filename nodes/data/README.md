# data 节点

**Owner**: TBD

按调用方明确范围，为下游 experiment/simulation 准备**直接可用**的前处理资产。

## 按需入口

调用方可以附带 `pre_registration` / `research_plan` / stage contract，也可以直接要求生成、
获取、检查或转换一个或多个明确资产。research plan 不是接单前提；直接请求只建立完成目标
所需的最小工作单元，不伪造 research stage。调用方已经确定科学范围，data 节点不选择、排序或延期假设。

入口统一规范化为版本化 `PreprocessingRequest`：research plan / preregistration 使用
`plan_bound`，用户或 experiment 的明确资产请求使用 `request_bound`。两者再编译为
`PreprocessingWorkOrder`；缓存、执行、审核和交付均绑定 `request_id + request_spec_hash`。

所有入口先编译同一种工作订单：结构完整的请求或计划直接通过本地 schema/safety gate；只有
含糊文本、未提取的计划阶段和审核回修才按需调用 Requirement、Designer/Critic。之后统一进入
Execute → Review → dataset。Review 由通用机械门禁和模型 authority review 组成；后者把产物
整体对照锁定的最初请求或 research plan，不得自行引入领域偏好或额外交付要求。

## I/O 契约

| 方向 | artifact_type | 备注 |
|---|---|---|
| Input | preprocessing request | 明确资产请求；可选附带上游契约或本地路径 |
| Output | `dataset` | 含真实 package path、manifest、质量门、lineage 和下游契约 |

## 验证边界

自动化回归测试验证的是框架合同，而不是某个具体学科任务已经成功交付。当前测试覆盖
artifact writer 的最终内容写入、工作目录和格式校验、reference evidence 持久化、规划
loop 的工具链以及直接服务驱动；这些通过只说明接口和状态转换满足约定。

真实任务仍须以端到端日志和最终 artifact 为准，至少应看到：

- 每个调用方要求的生成 step 都有对应的 `artifact_id` 和落盘文件；
- 执行器返回成功状态，并记录文件路径、哈希和 provenance，最终 manifest/package 已发布；
- 没有 `blocked`/未解决的必需 gap，且下游要求的参数文件确实位于 package 内；
- 若脚本需要 `xarray` 等依赖，只记录为 `runtime_dependencies`；由 experiment 准备执行环境，
  data 不安装或运行环境管理命令。

Designer/Critic 的高分或 planning loop 的完成本身不等于交付成功；缺少上述落盘和发布证据时，
应判定为“规划完成、交付未完成”，继续检查执行器状态，而不是重新搜索或重复制定计划。

执行、审核和规划统一返回七类 pipeline outcome：`completed`、`retry_step`、`revise_asset`、
`revise_plan`、`needs_input`、`externally_blocked`、`fatal`。审核失败只形成
`RevisionContract`；executor 优先修复产物及其负责步骤，并按 DAG 顺序重建受影响的下游，
保留无关成功步骤。生效参数和修复记录进入执行 checkpoint，后续修复不退回旧参数。
参数修复读取工具自身的 `content_contract`，可补齐原调用遗漏但工具支持的控制项；
不能只按原参数键集合限制修复。修复历史保留实际参数差量和文件哈希，不递归嵌套旧反馈。
实物片段只供审核和产物修复读取；规划恢复传递问题、负责步骤及修复摘要，不反复复制文件内容。
注册器的超长结果摘要仅用于模型上下文；executor 按受校验的本次运行文件指针恢复完整结果，
包括续跑 checkpoint。交付 manifest 不嵌套执行参数历史或重复的下一次调用载荷；
只有审核发布成功且取得 artifact ID 后，生成计划才能报告完成。
只有现有生成方法无法解决问题或 DAG 不完整时，才进入 Designer 调整局部路线，仍锁定原始需求。
一次修复无变化不直接判 fatal；重复无效修订由现有修订状态机停止。
修订计划未获批时，Data 在同一次调用内保留失败草稿、实物证据和原始审核意见继续修订。
局部修订持续无效时，复用 Requirement Analyst 重新核对内部需求解释；锁定的是调用方要求，
不是模型此前推断的文件名或生成方法。相同问题经局部修订及需求复核仍无进展才终止；
审核通信故障从成功步骤的 checkpoint 重试，不重生成已完成资产。
修复解决旧问题后暴露新问题也算进展，但文件变化不等于验收通过，交付仍必须重新审核。
建议文件名尚未匹配时，先由同一审核器根据真实内容完成资产绑定，不要求生产器重生成等价文件。
定向计划修订保留完整依赖接口；同一工具的局部修改合并参数，省略字段不删除已有输入引用或生效控制。
参数合并由 executor/planner 共用，不分别维护两套逻辑；更换工具时仍可重新设计局部路线。
OpenFOAM polyMesh 等多文件求解器网格默认附带 Tecplot 可视化导出；独立网格/输入文件
（如 `.inp`）不额外重复导出。调用方明确要求或关闭可视化时，保留 `write_tecplot` 选择。
可视化按实际单元维度导出：纯二维网格不强求体网格文件。OpenFOAM 的二维计算仍使用
单层体单元和 empty 端面；选择 OpenFOAM 转换时自动完成该存储转换，不改变物理维度。
生成或导出失败时保留 staged 文件和原始错误供修复，不把缺失质量数据解释成零单元或低质量。
审核数值约束时保留单位、参考系及“至少/至多/所有”的含义；实测值与约束的比较不成立时，
形成可修复的 RevisionContract，而不是用整体 pass 或原生工具检查通过掩盖不匹配。
模型审核用 `condition_checks` 将原始需求中的独立条件关联到可核查证据，不能只用少数参数证明整单通过。
每项条件至少保留一条核实过的引用；摘录可按原顺序省略中间行，虚构或改写的数据仍无效。
协议修复复用同一 schema，反馈具体失败字段及上一份结果，不重新生成资产。
普通请求只锁定调用方实际指定的文件名，Analyst 建议名称允许绑定到等价产物。
无进展判断复用稳定的问题身份和审核进展，不因模型改写意见、增加修复历史或更换计划 ID 而重置。
审核还可读取去重、限量的内部生成源文件片段；源文件不是额外交付要求。若缺陷源于上游，
用 `source_file` 指向已知来源，executor 回到原生成步骤修复，再按 DAG 重建下游。
Gmsh 质量统计提供各物理区域的实际坐标范围和线单元长度，不内置某种几何的合格阈值；
区域命名、范围和加密分布是否兑现请求仍由模型依据实物和配置联合判断。

## 关键工具

- `run_preprocessing_planning_loop` —— 从调用请求及可选上游契约推导前处理资产 DAG 并审批
- `execute_preprocessing_python` —— 计划授权后生成、检查或转换数据
- `prepare_scientific_mesh` —— 跨学科网格/粒子构型生成、已有 Gmsh 资产转换、缺参分流和质量审核的唯一入口
- `build_scientific_preprocessing_package` —— 只生成 staged 输入资产与发布候选元数据
- `execute_preprocessing_plan` —— 执行已批准 DAG，统一审核、修复、生成最终 manifest 并发布；不运行正式仿真
- `data_web_search` —— 在批准的发现目标内检索并检查公共线索，不直接把远程结果采纳为科学输入
- `data_web_download` —— 对选定 URL 执行安全下载、类型检查、SHA-256 和 lineage，生成可被后续步骤消费的本地资产

## 职责边界

- 从调用请求提取资产、必要参数、格式和验收要求；仅 plan_bound 提取既有阶段。
- 生成或转换请求要求的数据、几何/结构、网格和求解器输入文件。
- 对每项交付做机械审核，并由模型复核其是否兑现最初的 plan/request；不满足时只修复失败项。
- 缺少公开资产时定向检索；必须由用户确认的信息才进入 HITL。
- 最终保存可复现 `dataset` artifact。

## 跑通示范

```bash
python run_node.py --harness data --sandbox \
  --fixture nodes/data/fixtures/minimal.yaml
```

## 不该做

- 自行提出研究课题、研究问题或科学假设
- 改写调用方给定的目标、阶段和科学判据
- 跑实验本身（experiment 节点的活）
- 解析实验/仿真结果或下科学结论
- 执行正式模型训练或长任务
