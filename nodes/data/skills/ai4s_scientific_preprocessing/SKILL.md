---
name: ai4s_scientific_preprocessing
description: |
  AI4S 通用科学研究前处理。用于科学数据识别、语义声明、readiness 检查、格式转换、
  仿真输入准备、跨学科网格或结构生成、外部资料补齐、质量自迭代以及模型训练资产准备。
  具体求解器的文件集合由显式工作订单编译器或 Requirement Analyst 动态推导，不在 skill 或 harness 中枚举。
applies_when:
  - data 节点收到科学数据 readiness、格式转换、输入文件、网格/结构或模型训练前处理任务
  - 任务需要单位、坐标、拓扑、measurement context、质量门、lineage 或 downstream contract
  - 专业前处理需要外部资料、自动质量迭代或 human-in-the-loop
tools_used:
  - build_scientific_preprocessing_package
  - prepare_scientific_mesh
  - execute_preprocessing_python
  - search_kb
  - data_web_search
  - data_web_download
  - save_artifact
expected_outcome: 可复现的 dataset artifact，包含真实资产、完整 manifest、质量证据、lineage、assumptions 和 downstream contract
node_type: data
status: validated
relevant_concepts: []
---

# AI4S 通用科学研究前处理

## 职责边界

本 skill 定义跨应用通用的方法、证据标准、质量门和交付契约。不要为每个具体应用分别创建
skill。具体求解器在具体计算阶段需要哪些文件，必须
由 `analyze_preprocessing_requirements` 根据显式契约、任务、上游 artifact 和参考证据动态得到；不要把
非网格任务硬塞进网格流程。

本节点是按需前处理服务：既可消费上游阶段契约，也可直接执行一个或多个明确资产的生成、
获取、检查或转换请求。research plan 不是前提；没有计划时只建立完成该请求所需的最小范围。
调用方已经确定科学范围，本节点不得选择、排序、比较或延期假设。它不提出课题、不生成或
修改假设、不扩写实验方案、不运行正式仿真、不解释结果、不执行长训练、不下科学结论。

## 标准执行流程

1. 将调用输入规范化为版本化 `PreprocessingRequest`。research plan/preregistration 使用
   `plan_bound`，明确用户/experiment 资产请求使用 `request_bound`；后者映射为资产 work unit，
   不创建调用方 stage。缓存和交付绑定 `request_id + request_spec_hash`。
2. 以调用请求及可选上游契约作为学科、阶段、目标产物和验收要求的最高优先级证据；规划服务
   只用内部识别器补充学科和数据形态，不覆盖其中的明确声明，并声明 data model、语义、单位、
   坐标、拓扑、measurement context 与未知项。
   只有调用上下文和其他可靠证据都未声明学科时才保留 `unknown`。尚未内置适配器的新学科
   仍保留其原始学科名称，并按数据形态和 downstream contract 选择通用能力；不得自动改成 CFD、
   多物理场或其他已知领域。
3. 根据数据形态选择专业路径：
   - 普通数据：检查、清洗、转换并生成可消费数据集。
   - 仿真输入：生成参数文件、边界/材料契约和求解器输入包。
   - 网格或粒子结构：统一调用 `prepare_scientific_mesh`。
   - 数据驱动模型：准备训练数据审计、特征/目标语义、脚本与模型计划，不执行长训练。
   外部资料处理分为 discovery 和 acquisition：前者可在锁定目标内检索、调整线索并检查候选页；
   后者才对选定 URL 执行下载、类型验证、hash 和 lineage。搜索摘要不得直接冒充本地科学输入。
4. 对真实资产执行可复用的机械质量检查；再由模型把完整产物对照锁定 authority 审核需求兑现，
   不新增领域偏好或未声明参数。失败时按结构化证据只修复对应资产。
5. 调用 `build_scientific_preprocessing_package` 生成 staged assets，由 executor 统一审核、写 manifest 并发布。
6. Gate Registry 只汇总需求兑现、质量证据和交付完整性；两种 review profile 共用同一审核链，
   差异仅来自锁定 authority 的内容。模型不得重复解析已由原生工具验证的格式。

不得用 Markdown、占位 manifest、参数说明或手写伪文件代替实际工具产出。

## 通用数据与语义契约

- 未知单位、坐标系、边界语义、采样率、校准、reference standard、instrument setting 或
  protocol 必须进入 assumptions；会改变数据解释或下游执行时才请求用户确认。
- readiness 至少检查可读性、shape/size、缺失、重复、NaN/Inf、非法值和单位一致性；
  再按 table、time series、mesh、structure、spectrum、graph 等形态叠加专业检查。
- 内部资产检查器根据内容签名和数据形态选择能力适配器。适配器按格式族
  复用，不按课题命名；同一 NetCDF/HDF5/Parquet/图数据适配器可以服务多个学科。
- 数据计算、统计、hash 和格式转换使用 `execute_preprocessing_python` 或专业工具完成。
  该工具只在批准计划明确选择 `execute_python` 后运行；cwd 使用当前 run 的
  隐藏临时工作区；最终交付按请求身份进入
  `data_preprocessing/<delivery-name>__<run-id>/`；request id、spec hash 和完整参数保存在
  `manifest.json`，不同 run 不得覆盖。
- 每次转换记录 input、op、params、output、timestamp 和 hash/stable id。
- manifest 的专业字段可以扩展，但不能缺少通用契约字段。

## 网格与几何统一流程

任何学科的计算网格、离散结构、粒子构型、几何补齐和质量迭代都调用
`prepare_scientific_mesh`。具体学科只是该工具内部的领域分支。

- 不得为临时网格指定 `data_preprocessing/coarse_mesh`、`fine_mesh`、`mesh_case` 或
  `preprocessing_workspace` 等交付目录。独立网格调用只写 run 内隐藏工作区，成功打包后自动清理。
- 已批准的计划包含 `build_scientific_preprocessing_package` 时，不要在计划执行后再手工重复调用
  `prepare_scientific_mesh` 生成收敛网格或变体；package 步骤必须消费已通过审核的依赖输出，不能
  再次生成同一资产。所有生成与审核发生在隐藏 staging 中，只有 Publisher 能发布最终目录。
- 可运行输入放在交付根目录的 `stages/<step>`；可视化、审核和复现资产
  分别放在 `visualization/`、`audit/`、`reproducibility/`，不得在 package 根目录保留副本。
- 默认交付结构化验证回执和原生工具输入即可。原始完整日志仅在请求明确要求或失败诊断时保留；
  `.geo`、mesher journal、solver input 已满足普通“生成脚本/复现源”要求，只有明确要求一键或
  可执行包装时才额外生成 Shell/Python 脚本。

## 通用错误恢复

不要在后续 turn 原样重复失败的工具调用，也不要重新跑完整 Designer/Critic 来掩盖局部错误。
执行器按错误语义统一处理，学科 skill 只能补充局部修复动作：

1. 网络断连、超时、429/5xx 等瞬态错误：重试同一步；错误签名不变时停止，签名变化时继续。
2. 生成资产未通过质量门：保留已通过资产，只修复审核指出的文件、参数或网格层级。
3. 缺少公开可复用资产：按批准计划定向搜索一次；缺少用户拥有或必须确认的信息才 HITL。
4. 运行环境缺失：返回 `externally_blocked` 环境契约，由 experiment 补齐后续跑；data 不安装依赖。
5. 参数、schema、路径、工具授权或内部调用契约错误：先修对应资产或步骤；只有资产 DAG
   不完整时才交给 Designer。审核只提供 `RevisionContract`，终态只由 pipeline controller 决定。

控制器只接受 `completed / retry_step / revise_asset / revise_plan / needs_input /
externally_blocked / fatal`。通过项增加、失败资产减少或错误签名变化都算进展；只有同一问题
没有进展才熔断。

任何恢复都必须保持调用方参数、几何身份、单位、边界语义和 lineage；不能为了完成状态
把列表偷偷压成首个值、换用替代几何或删除失败的质量门。

### 几何来源优先级

1. 用户已提供几何、坐标、CAD、离散模型或参考网格：直接评估并使用该资产。
2. 节点已有与用户原始几何角色明确匹配的参数化生成器：直接生成。
3. 其余情况先补齐公开、可复现的几何或计算域资料。
4. 仍无法获得安全输入时进入 human-in-the-loop。

不得因为错误、缺参、检索失败或审核失败而回退到另一种标准几何。只有用户明确批准
`surrogate_geometry_approved=true` 且记录 `nonphysical_approximation=true` 时，才允许
非物理替代，并必须声明其适用范围和限制。

### 通用几何判断

- 区分实体/轮廓、完整计算域和已有离散网格。实体表面不等于计算域；需要时必须构造闭合
  外域或内部区域、执行布尔扣除并定义边界角色。
- 按拓扑角色审核，不按算例名称硬编码。至少识别外部域、内部通道、周期/重复域、旋转或
  滑移区域、多区域接口、开放边界和实体孔洞等角色。
- 参考网格默认只作为几何形状、闭合域、边界分区和拓扑语义来源。提取边界后生成可追溯
  的网格器输入并重新划分最终单元；禁止把格式转换后的参考单元直接作为默认交付。
- 多实体、多区域或边界语义不足时不得静默丢弃区域；应进入专业处理或 HITL。

### 质量审核与自迭代

网格审核必须同时覆盖：

- 文件与拓扑完整性：节点、单元、邻接、边界、区域、索引和连通性有效。
- 几何正确性：计算域闭合，实体孔洞未被填充，网格不穿透实体，无意外空洞或重叠。
- 物理区域正确性：边界角色、周期配对、区域接口和加密区域符合原任务拓扑。
- 数值质量：维度、体积/面积、长宽比、非正交、扭曲、Jacobian/determinant 和专业质量门。
- 交付一致性：原生网格、目标格式和可视化导出描述同一批有效单元，不能只导出轮廓。

专业检查器即使命令返回码为 0 或输出包含总体成功信息，只要同时报告异常单元集合、失败
检查或拓扑警告，仍判为失败。失败结果放入 diagnostic/rejected 区域，不能复制到最终 package。
根据 review 建议调整尺寸场、边界层、局部加密、算法或几何修复并重试；达到迭代上限且仍
需要改变用户物理意图时才暂停询问。

只有同时满足 `status=success`、`deliverable_valid=true` 和 `mesh_review.status=pass` 的网格
才能打包。目标求解器或可视化格式的必需文件由对应工具契约决定，harness 不枚举文件名。

## 外部资料获取

外部资料只用于补齐可公开复现且直接影响前处理的几何、边界、单位、材料、协议或质量目标。

1. 优先复用主节点提供的搜索结果、下载路径和 `source_trace`。
2. 主节点结果不可用或不足时，使用 data 节点兜底检索。模型可在已批准的缺口、资产类型和
   能力范围内调整关键词并检查候选页，不因 query 文本变化重新触发契约门禁。
3. 优先获取能直接用于前处理的坐标、CAD、离散模型、参考网格、完整 benchmark/archive
   或权威参数表。普通论文、摘要页和无资产的介绍页只作为 source hint。
4. 找到高相关仓库或下载页后只返回候选链接；由 pipeline 为选中的直接 URL 生成独立下载
   步骤。下载成功并记录 hash/lineage 后才交给专业工具，避免搜索阶段暗中落盘或采纳资产。
5. 小型数据实际下载时记录来源、许可信息（可获得时）、hash、保存路径和适用范围；大型或未确定来源的数据记录逐 step 获取流程、规模估算、请求参数、验证和恢复方式，URL 不是必填项。
6. 学术搜索只在普通检索无法获得必要参数或需要权威方法依据时使用。

检索预算、搜索引擎顺序、下载和归档解析属于工具实现，不写入 harness。检索失败后将失败
摘要和已尝试来源传回专业 resolve 流程，再由工具判断是否需要 HITL。

## Human In The Loop

仅在以下情况暂停：

- 信息属于私有资产或无法公开复现；
- 多个可靠来源冲突且选择会改变用户物理意图；
- 单位、尺度、边界角色、区域语义或目标质量无法安全推断；
- 自动修复需要改变几何或下游契约。

如果专业工具已返回 `pause`、`needs_input`、`needs_reference_search` 或
`needs_geometry_processing`，遵循其 `next_action/resume_instruction`，不要额外询问同一问题。
resume 后把用户回复合并回原参数并重试原工具，不重新启动一条独立流程。

## 分阶段计算与下游回调

research plan 中后续 stage 依赖前置仿真、实验或数据派生结果时，先交付所有当前可运行
case；依赖尚未满足的 stage 必须标记为 `deferred_dependency`，不能假装 ready，也不能
阻塞独立 case。每个 deferred stage 的 `input.pending.json` 必须声明前置 stage、预期结果
角色、文件或目录验收条件，以及结构化 `resume.request`。

package 在 `workflow_manifest.json` 的 `deferred_stage_resume` 字段汇总恢复请求。下游节点完成前置计算并通过
自身质量门后，将结果注册到 `dependency_artifacts.<stage_id>`，然后按该字段中的请求再次
调用 data 节点。请求使用 `target_stage_ids`/`resume_stage_ids` 限定本次只生成新解锁的
stage；已经交付的 ready case 保持有效，不重新设计研究计划或重复生成。依赖 artifact
可以是文件或目录，必须提供完成状态、实际路径、质量状态，并可用 `required_files` 声明
最低内容检查。若研究计划只给连续范围而没有采样点或建模方法，保留参数空间缺口；不得为
凑工况数自行发明组合。

研究计划中的 Gate/decision 表同样属于执行契约，不能因为它不在计算任务表中而跳过。
有独立编号的门控生成 `gate_guidance.json`、`gate_result.schema.json` 和面向下游节点的
`GATE_INSTRUCTIONS.md`，但不伪装成求解器 case；直接进入既有 stage 的门控则生成
`entry_gate_guidance.json`。data 节点只准备判据与分支契约，不读取正式结果或替下游作出
科学判断。下游节点依据真实结果写出 `gate_result.json`，再按选定分支回调 data 节点。

人工问题必须说明：需要提供或确认什么、为什么不能安全自动决定、收到回答后将如何继续。
用户要求“从网络寻找”时视为授权公开检索，不得立即再次要求本地文件。

## 缺失输入的自主补全顺序

发现输入不完整时，不得立即暂停。按以下顺序处理，并把每一步的证据、失败原因和最终假设
写入 plan/manifest：

1. 把原始路径交给 `run_preprocessing_planning_loop`：目录清单、JSON 顶层字段和受限内容
   预览由 planning loop 的输入检查自动读出并进入 task_context。目录枚举不需要 approved
   plan，也不需要（且没有）单独的目录列举工具。
2. 从上游 artifact 中提取 DOI、论文标题、研究对象、几何/结构标识、软件与版本、计算阶段、
   物理模型、单位、边界/初始条件、材料或介质参数、收敛标准和验收指标。
3. 缺少公开可复现的输入资产时，计划阶段先写入有明确缺口、资产类型和能力范围的
   `targeted_search_requests` 并生成 reference-search plan；执行阶段可在该范围内调整
   query 和检查候选页，但不得扩大科学目标或直接采用远程内容。只有该计划批准后，才检索出版社 supporting information、作者仓库、官方
   数据库、标准 benchmark 和软件官方示例。输入资产包括但不限于结构/几何/离散模型、
   计算域、网格、材料或属性模型、参考库、机理/规则、边界条件、载荷、源项、初始场、
   校准系数和数据 schema。下载后按对应数据形态执行格式、单位、拓扑、组成、尺寸、
   版本、适用范围和 hash 校验。各专业资产的有效证据、可接受来源和 simulation-ready
   判据由对应工具定义；通用 skill 不枚举具体格式或学科规则。选中候选后另建精确下载
   步骤，完成内容、hash 与 lineage 校验后才可采纳。
4. 对有公认保守默认值且不会改变研究对象的信息，可生成带来源和 assumption 的候选配置，
   并通过预检查或小规模 convergence plan 验证；不得把默认值描述为用户提供的事实。
5. Python 包、CLI、编译器、环境模块和运行库统一由 experiment 准备。data 只能验证当前调用
   环境能否执行批准步骤，不创建 venv、不调用 pip/conda/brew/apt、不下载源码或构建软件。
   缺失时返回 `externally_blocked`，准确记录缺失能力、受影响步骤和用于恢复的环境要求。
6. 许可证、商业协议、组织权限或受控数据库限制的资产不得伪造、转录或联网转发。应先检查
   用户环境中已授权的本地安装、环境变量、模块系统、数据库路径或凭证可见性，再生成不含
   受限正文的 manifest、定位/组装脚本和其余可交付输入。若资产仍不可用，返回
   `externally_blocked`，由控制器写 blocked report，准确列出缺失角色、校验方式和恢复命令。
7. 只有公开检索、已有 artifact、本地授权资产检查、保守默认方案和替代模型均无法获得可靠
   输入，且缺失信息会改变物理体系或使生成文件不可执行时，才进入 HITL。
   只有用户确实能补充决定性科学参数、本地附件或授权路径时返回结构化 `needs_input`；用户
   无法改变的外部条件返回 `externally_blocked`，不得把两者都压成 failed。

上述顺序适用于所有前处理任务。领域工具可以增加更严格的验证，但不能
跳过只读发现、证据检索和自动补全步骤，也不能仅因应用名称陌生就启动 HITL。

内部服务故障不是科学信息缺失。Designer/Critic 断连、超时或非 JSON 输出应自动重试；Critic
持续不可用时可对 schema-valid、无 blocking unresolved question 的计划使用可审计的确定性
校验。不得把内部规划服务错误包装成“请用户列目录或粘贴文件”。

## Package 与 Artifact

最终 package 必须包含真实资产路径、manifest、quality gates、lineage、assumptions、
reproducibility 和 downstream contract。`build_scientific_preprocessing_package` 已保存结构化
dataset 时，不再用自然语言或手写内容覆盖它。

禁止在专业工具仍处于可恢复状态、等待输入或审核失败时保存成功 dataset。任务不能在没有
dataset 且没有结构化 pause/error 的情况下静默结束。

## 常见错误

- 把实体轮廓当作完整计算域，或把参考网格的格式转换当作重新生成网格。
- 只验证文件存在，不验证单元、区域、边界和导出格式的一致性。
- 为新算例增加专用 skill/profile/mesh_type，而不是复用拓扑角色和通用工具参数。
- 检索到介绍页面后继续大范围搜索，却没有优先发现或下载可用资产。
- 专业工具已经暂停后再次调用通用人工输入，造成重复提问。
- 用另一种标准几何作为复杂任务的隐式兜底。
