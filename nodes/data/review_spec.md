# Data 节点内部交付审核规范（`dataset`）

> Data 服务在发布前执行本地 package review；不依赖外部 reviewer/curator 流程。
> Gate Registry v3 只汇总需求兑现、质量证据和交付完整性，不维护按学科或 authority 模式复制的 rubric。

## 节点内统一前处理审核

所有前处理交付采用同一条审核链，不再按学科维护完整 reviewer：

1. `package_reviewer.py` 只执行可复用的确定性检查，例如文件存在、非空、相对路径、
   placeholder、结构化格式和已声明资产/阶段是否存在。
2. `authority_reviewer.py` 主要判断实际产物是否完整兑现锁定需求；质量判断复用生成工具的原生
   验证收据，并在已有公开参考证据时用于对照。它不重新解析底层格式，也不增加未声明标准。
3. Gate Registry 仅汇总 `requirement_alignment`、`quality_evidence` 和 `delivery_integrity`。
   `plan_bound` 与 `request_bound` 使用同一条审核链，权威内容不同但不复制门禁。

网格拓扑、解析器和外部工具可直接证明的低层质量仍由生成器执行，例如 Gmsh 单元质量、
OpenFOAM `checkMesh` 和格式 parser。这类检查按产物能力复用，不扩展成学科百科。
涉及物理意图、参数是否符合原始需求、是否擅自扩大范围的判断统一交给 authority reviewer。
审核结论通过 `condition_checks` 将每个独立需求关联到实物证据；不能仅引用材料或格式字段，
就声称区域选择、空间分布等其他条件也已满足。内部源文件片段可作为辅助证据，不是新增交付物。
区域统计只提供实际坐标范围、实体数量和长度等观测，不按学科内置合格阈值。
发现上游定义错误时，审核意见用 `source_file` 引用已提供的来源；executor 修复该来源并重建下游。

### Caller Stage Contract

调用请求及其可选上游契约是每个 stage 的参数和数据文件契约，而不是仅供说明的文本：

- 每个 `ready` 或 `candidate_generated` stage 必须写入 `parameter_contract.json`，逐项记录计划中
  显式参数、规范化后的有效参数、学科适配器实际读取的参数键、直接产物证据和传播/消费检查结果。
- 调用方明确写出的参数值高于分析模型补充值；模型只能补缺失参数，不能覆盖显式值。
  除已注册的规范化别名外，任何 `key=value` 参数都必须保留，使未预先适配的新学科也不会在规划阶段丢参。
- 参数存在于 `effective_parameters` 不代表已落实。`solver_input` stage 的每个显式参数必须被适配器实际
  读取，或能在用户提供的 JSON/YAML 等结构化输入中找到等值证据；否则 stage 必须隔离为不可交付，
  不能仅刷新契约后标记 `ready`。authority reviewer 必须复核实际产物是否兑现对应值。
- 调用方显式声明的 stage 必须全部进入工作流索引。分析模型可以补充语义，但不能
  静默删除 stage；当前缺少上游结果或适配器的 stage 应生成可恢复的 `input.pending.json`。
- stage 的目录和候选输入文件已生成，不代表其依赖已经满足。任何依赖前序求解、数据派生或实验
  结果的 stage，只有在对应结果 artifact 存在且通过最低可读性检查后才能标记 `ready`；否则必须
  保留已生成输入、标记 `deferred_dependency`，并在 `input.pending.json` 中写明准确依赖与恢复方式。
- 网格 stage 的单元数、域尺度、壁面/离散无量纲要求必须在生成前转成 mesher 控制，并以实际网格
  数量、域参数和已声明的预运行参考模型复核；通用质量下限通过不能替代计划要求通过。
- 求解器 stage 必须由对应学科适配器验证输入语法和可验证的数值控制，例如 CFD 的求解器、时间推进、
  CFL、黏度/雷诺数和边界速度，材料计算的 INCAR/KPOINTS/POSCAR/POTCAR 一致性。
- 审核失败优先修复负责该缺陷的生成步骤及其受影响下游，并保留无关成功步骤；仅当前方法无法
  修复或依赖图需要变更时，才进入 Designer/Critic
  规划循环。几何、单位、边界语义或用户专有参数无法安全修复时，再走 human-in-the-loop。

### 通用交付目录约定

所有 data 前处理包按资产角色组织，不能以某个学科、求解器或网格格式命名公共目录：

- `stages/<stage>/`：阶段工作目录。`solver_input` stage 必须在本目录内保存
  可运行 case 及其运行时资产；`asset_generation` stage 只保存真实生成资产和质量记录，不能伪造
  求解器输入。网格只是 `runtime_assets.json` 中 `asset_role=mesh` 的一种资产；没有网格的研究任务
  不得生成空网格目录。
- 网格收敛 stage 必须使用 `mesh_convergence.json` 列出每个 level 的目标单元数、实际单元数、
  网格路径和独立质量复核结果。只有所有声明 level 都通过复核时，收敛研究根目录才可标记 `ready`；
  已通过的 level 可作为独立求解案例交付。不得用同一份网格复制到多个目录后声称完成收敛研究。
- 网格收敛只允许改变离散密度。几何来源、计算域、边界命名、近壁拓扑和显式第一层高度属于
  mesh-family 不变量，必须从基准网格继承并逐 level 比对。若研究计划声明壁面无量纲首层要求，
  仅有距离场局部尺寸而没有可报告第一层的网格不得标记为该要求已验证。
- 若 mesher 声明了表面/周向离散和边界层径向离散控制，审核必须分别检查它们随
  `coarse -> medium -> fine` 单调细化；只增加外部非结构区单元数而保持近壁周向节点和径向层数
  不变，不得作为合格的收敛网格族交付。
- 仅当调用请求明确声明 `mesh_convergence` stage 时，才启用三套网格流程；普通单网格任务不得
  自动创建 convergence level 或附加 `medium` 语义。三套网格收敛研究默认以 `medium` 为
  reference/downstream-default：先按正式仿真（包括 DNS）
  的计划约束生成并审核 `medium`，再从同一 mesh-family 派生更稀疏的 `coarse` 和更密的 `fine`。
  实际单元数必须随目标密度严格递增。公共可视化和后续正式求解 case 使用 `medium`，不能使用另行
  生成的公共网格替代。data 节点只交付可比较的输入；只有下游在相同物理与数值设置下比较
  `medium/fine` 的计划观测量并满足计划容差后，才能宣称网格收敛。
- 计划明确要求的收敛 stage 是当前前处理交付物，不是下游结果依赖。任一 level 生成或审核失败时，
  不得把该 stage 标成普通 `deferred_dependency` 后保存成功 dataset；应执行局部网格重试，仍失败则
  使 package review 失败，并把诊断集中到包级 `audit/mesh_convergence/`。
- `visualization/`：仅保存明确请求的可视化导出，例如 Tecplot、VTK 或预览图，不作为求解器
  运行输入。收敛网格使用 `visualization/mesh_convergence/<stage>/<level>/`，各 level 的 case
  目录不得重复保存这些文件。
- `audit/`：保存质量报告、审核结果、交付索引和自动迭代诊断，不作为运行输入。
  收敛网格的逐 level 记录使用 `audit/mesh_convergence/<stage>/<level>/`。
- `reproducibility/`：保存可复现所需的源几何、网格脚本、原始网格格式、转换参数及其哈希。
  收敛网格使用 `reproducibility/mesh_convergence/<stage>/<level>/`。
- 成功任务默认保存结构化验证结果；完整原始日志仅在调用契约明确要求或验证失败需诊断时保存。
  `.geo`、mesher journal、solver input 等原生工具输入已属于生成脚本/复现源，只有契约明确要求
  一键或可执行包装脚本时才额外生成 Shell/Python 脚本。
- `mesh_variants/<level>/` 只保留下游运行所需的输入、运行时网格、case marker 和参数契约。
  若求解器直接消费原生网格格式，该文件保留在 case 中并在 `reproducibility/` 保存副本；若已有
  独立运行时网格（例如 OpenFOAM `constant/polyMesh`），`.geo/.msh` 等只进入复现目录。
- 包根目录只保留跨 case 的 `manifest.json`、工作流索引和简短入口说明；不得同时放置公共运行
  网格、可视化副本、审核副本或临时说明文件。

`twoInternalFacesCells`、Finder `.DS_Store`、工具自动生成但未被下游契约引用的使用说明等
诊断/元数据文件不得作为最终交付物。系统可在生成期间使用它们，但打包完成前必须清理或转入
`audit/`。

## Gmsh 网格产物的节点内强制复核

当 data 节点通过 Gmsh 生成任何学科/算例网格时，data 节点必须在保存成功 dataset 前
自己完成机械复核与必要的自动迭代。该复核不等待 `_reviewer`，由网格工具直接执行并
落盘 `mesh_review.json`。标准参数化 CFD 算例可以通过 profile/role 叠加专项检查，
但所有后续新增 Gmsh 算例都必须继承通用 Gmsh 复核。

复核实现应分为两层：

- 机械质量层：所有 Gmsh 网格共用，包括文件存在性、OpenFOAM/Tecplot 导出、单元数量、
  非正面积、长宽比和拓扑完整性等确定性检查。二维域检查边界环、开口/非流形节点和连通
  区域；三维域检查边界壳闭合及非流形边。
- 语义意图层：先从用户需求、profile、生成结果中推断 `geometry_role`，再使用
  `nodes/data/review/mesh_reviewer.py` 中集中维护的 role 规则检查计算域、边界语义和
  加密区域是否符合物理意图。后续新增算例应优先映射到已有 role 或新增少量抽象 role，
  不要为每个 `mesh_type` 或每个公开几何在 `harness.yaml` 中增加专用分支。

`mesh_review.json` 必须记录 `mesh_intent`、`mesh_features` 和 `semantic_review`。
当语义规则发现网格域与用户任务不一致时，例如内部通道/周期流动被生成为外流远场域，
即使机械网格质量达标也必须判为 fail。

### 复核范围

- 通用文件完整性：`.msh`、`mesh_quality.json` 必须存在；如果生成器输出 `.geo`，
  `.geo` 也应列入 `written_files`；请求 OpenFOAM 时
  `constant/polyMesh/boundary` 必须存在；请求 Tecplot 时 `*_tecplot_surface.dat`
  必须存在。
- 通用网格质量：必须有有效单元；3D 挤出时必须有 3D 单元；不能有非正面积单元；
  `max_aspect_ratio < 500`。OpenFOAM 网格必须执行 `checkMesh -allTopology -allGeometry`；
  即使命令返回码为 0，只要输出包含 `Failed N mesh checks` 也必须判为失败。
- 通用导出一致性：`mesh_review.json` 必须记录 OpenFOAM/Tecplot 是否按请求成功导出，
  并记录失败原因。
- 几何形状：用户指定的几何必须来自显式参数、用户文件或可追溯公开来源；禁止回退成
  默认几何。外流绕流类任务必须保留物体孔洞/壁面和合理尾流域。CFD 导入 CAD 时必须先
  区分实体/轮廓与完整流体计算域；不得把物体表面直接挤出成流体网格，必须构造闭合计算域、
  从流体区域扣除实体并定义物理边界。
- 计算区域：外流、内流、周期通道、进口/出口通道等域类型必须与 `mesh_intent.geometry_role`
  和 `flow_topology` 一致；外流任务应保留足够远场和下游区域，内部/周期流动应保留入口、
  出口、周期或壁面边界。周期/弯折通道还必须检查参考边界形状、周期平移向量、实际节距与
  声明节距的一致性以及轴向域长度比例；只有矩形包围盒和 patch 名称不能证明计算域正确。
- 加密区域：近壁、尾流、边界层、局部曲率或用户指定区域的加密参数必须记录具体数值，
  并由 role-based review 检查是否满足质量门。
- 边界命名：OpenFOAM patch 名称应包含物理语义（如 `airfoil`/`cylinder`、`inlet`、
  `outlet`、`farfield`、`frontAndBack`）。

### 自动迭代要求

若复核失败，data 节点应自动调整网格参数并重试，默认最多 3 次。通用调整包括降低
特征网格尺寸、平滑增长率、修复导出流程；标准算例可叠加专项调整：

默认只保留最终可交付网格、`mesh_review.json` 和紧凑的审核结论；不得保留失败的 `.msh`、
`polyMesh`、Tecplot 文件或 `rejected_mesh/` 目录。需要逐轮复现实验时，才可显式设置
`retain_mesh_attempt_diagnostics=true`，并在 `audit/mesh_attempts/attempt_NN/diagnostics.json` 中记录
本轮 `.geo` 或生成脚本、质量报告、Gmsh/OpenFOAM 日志尾部、审核结果、结构化修复计划及其
SHA-256。对未被后续迭代覆盖的源文件可记录绝对/相对路径、大小和 SHA-256 引用，不重复复制同一份源文件；
最终可复现的 `.geo`、`.msh` 或生成脚本统一存放在 `reproducibility/mesh/`。
修复计划只能修改离散参数、网格算法配置、导出流程和可确定的 physical-group 映射，必须
声明 `geometry_or_boundary_intent_changed=false`；不得自动替换几何、计算域角色或用户边界
语义。OpenFOAM 转换后还必须记录显式期望 patch 与实际 patch 的差集。

对闭合轮廓或其他近壁边界，若边界层条带导致负体积、错误面朝向或不可接受的非正交，
允许在不改变近壁目标尺寸的前提下切换为距离场局部细化，并重新执行完整质量复核；
该回退必须记录在网格参数与 lineage 中，不能把未通过的条带网格作为最终交付。

- 远场/尾流过小：增大 `far_field`、`wake_length`。
- 表面/边界分辨率不足：增加边界离散点数或降低近壁/局部特征尺寸。
- 长宽比过大：适度增大第一层网格距离、降低边界层增长率、平滑近壁尺寸。
- OpenFOAM/Tecplot 文件缺失：重新导出或转换。

自动迭代中如果缺少新的几何/边界/质量门信息，必须先做信息分流：

- 可由本地 profile、KB、公开 benchmark 或论文检索解决的问题，应自动检索并记录
  `source_trace`，例如标准公开算例的常见域尺寸比例、边界条件描述、网格质量建议。
- 无法通过公开资料可靠确定的问题才进入 human-in-the-loop。私有/复杂几何文件、
  非 NACA 翼型坐标等几何本身通常需要用户提供；但如果用户已经提供几何文件、截面或坐标，
  只是缺少 CAD 单位/缩放、patch 物理语义、外围计算域、用户目标 y+、第一层网格距离或
  网格量上限等配套参数，必须先检索公开 benchmark、论文、
  算例说明或几何文件附带文档。检索失败、来源冲突或需要改变用户物理意图时，才 HITL。
- Re/Ma/入口速度/流体属性缺失时，节点应从任务描述或公开来源提取；仍缺失则采用可追溯
  预设并记录 assumption/source_trace。除非用户显式要求确认或工况参数相互冲突，不得仅因
  缺少流动工况启动 human-in-the-loop。
- human-in-the-loop 提示必须明确说明需要用户提供或确认的字段、不能自动猜测的原因、
  以及用户回复后节点将如何继续网格生成和复核。
- 非翼型任务不得复用 NACA/非 NACA 翼型缺参提示；新增或未知任务必须使用对应抽象
  profile、通用几何提示或公开检索提示。用户明确要求从网络/公开资料寻找
  几何参数时，应先走公开参考检索并记录来源，不能重复询问同一个本地几何文件。
- 内部通道、周期通道或级联流动不得默认套用孤立外流远场域。对应 role 应检查其必要的
  几何尺度、入口/出口范围、周期边界配对、壁面和通道区域。若使用物理上不匹配的近似域，
  除非 manifest 明确标记为已确认的非物理近似可视化，否则 reviewer 应判为不合格。
- 运行时不得维护特定学科的 case/profile 注册目录。请求或规划模型应选择已注册的通用
  生成能力，并把具体几何、参数和公开来源写入本次 `source_trace`。
- 几何来源优先级必须可审计：用户提供几何文件时必须优先使用该文件生成网格；未提供时先
  判断是否属于节点内已有参数化生成器的简单标准流动；这些任务应先本地生成。
  其他任务再检索公开几何；公开检索失败后才
  human-in-the-loop 询问用户是否能提供几何文件或确认候选来源。违反该顺序应视为流程问题。
- 该检索优先规则适用于所有网格类型。任何几何文件/截面/坐标已收到但
  外围计算域、边界语义、单位/尺度或网格目标不全的任务，都应先返回 `needs_reference_search`
  并记录检索查询；直接反复要求用户补同一类参数应视为流程问题。

只有 `mesh_review.status == "pass"` 时，网格工具才能返回成功并允许保存 dataset。
多次迭代后仍失败时，必须返回 error/needs_review，并在结果中包含
`review_iterations` 和最后一次 `mesh_review`，设置 `deliverable_valid=false`，并把失败资产
移入诊断/拒绝区。不得复制、打包或把失败网格总结为成功。
