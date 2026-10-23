# Experiment 节点 v3 governing document：执行内核与专业能力层

**地位**：本文件是 Experiment 节点 v3 协议的正式架构规范，优先级按 `nodes/experiment/AGENTS.md` 的冲突顺序排在"当前正式架构、API、数据和生命周期契约"一档。它取代协作分支 060 任务的 01–15 号信箱材料（那些只作历史审查记录）。**060 v3 系列仍是实验集成分支：不可合并、不可发布**。本地 scientific 已接入统一 finish；完整 S2-min、最终树真实 Slurm 与 Core 完成接线仍需分别验收。
**一句话**：框架维护用户目标与物理事实，提供边界、账本与恢复；模型在边界内自由做工程与科研判断；判定由框架自动执行，不由模型办手续。删除的是模型需要办理的 route / receipt / verify / finalize 手续，不删除框架自动维护的物理 action/job 事实、幂等协议、生命周期与专业 provenance。只为真实性、安全、幂等、资源与不可变证据设约束，不为框架仪式设门。

## 1. 模型假设与模型义务

运行时模型具有工具使用、规划、日志理解和方法调整能力，但能力、上下文稳定性与协议遵循程度不固定。框架不依赖模型记住隐藏状态，不把模型当安全或物理事实的权威，也不把所有模型限制成逐步脚本执行器。引导强度由任务状态、风险、事实缺口和模型的主动请求决定，不按模型品牌分支；通用内核不按软件名分支。

模型的义务只有三条：看清单（一页纸）、干活（跑 / 写 / 提 / 等 / 读 / 装 skill / 问人 / 报阻塞）、结束时如实 `finish(outcome, 摘要, 实际交付, 验收方法, 限制)`。自然任务的验收方法是专业解释，不是前置声明手续。

## 2. 三类权威事实

| 事实 | 来源 | owner | 不可变性 | 派生什么 |
|---|---|---|---|---|
| **A 用户目标与约束**：任务原文、绑定的 prereg、上游 artifact contract、路径角色、用户的选择与授权包络（CPU / GPU / 节点 / 时长 / 费用） | 平台与用户；框架在 run 开始时读出并物化可写根 | 平台 | run 内冻结的是完整目标、约束、选择与授权包络及已有正式条件；不得在 finish 事后改标准，用户追加目标要版本化。模型后来选择的核验方法不是 A。**动态能力不冻结**：queue / partition / GPU / quota / adapter capability 在 action 前刷新并版本化，历史观测进 B | 原文要求与已有结构化 required（文件、glob、检查命令及期望），以及三件套接口；一页纸；缺内部数组不等于没有要求 |
| **B 真实行动与外部副作用**：每次 action / job 的执行身份、外部句柄、终态、因果边、output observation；调度器原文与派生判定（unknown 合法）；提交意图与 nonce；能力观测历史 | OS / scheduler / executor / artifact store 是物理来源；框架采集并标准化 | 各物理 owner 拥有其事实；Experiment 维护一份规范化 provenance / event index（下称账本），索引而不取代 | append-only；终态首次冻结后单调 | 投影：作业进度、"到哪了"、分组 |
| **C 可追溯证据与版本化交付**：交付物及其归属标记、raw / clean / log 三件、sealed outcome / closure、任务约定的验证结果、独立核验报告、账本指针 | 模型形成主张，框架按 A 与 B 核对 | Experiment | 版本化；可被后续版本取代，不可改写 B；模型可追加可选发现与实际交付，不能改写原成功标准 | 下游（_reviewer、writing、KB）判断科学意义 |

**authority 原则**：同一事实没有两套可变权威；没有第二套 Experiment 控制状态机；A → B → C 可机械追溯。array 元素状态、checkpoint lineage、artifact version 是合法领域事实，各有其 owner。

**正式输入读取（R3-A）**：State 已有 Core task identity 时，只用 `assignment_for_state(state)` 读取该 uuid/revision/digest 指定的合同；`node_inputs` 的 prereg 提示不再覆盖它。bootstrap、旧接口只读适配、输入见证与 intent 哈希共用同一纯读取视图。合同缺失或精确绑定不完整须如实披露，不能取 latest 或补猜 hash。Core 合同缺少 assignment 只是尚未安排，不能仅因此推导 scientific；这与调用方显式声明 prereg pending 不同。未绑定 Core identity 的节点调用暂沿用显式 node-input 协议，不能在同一 run 中悄悄升级为另一合同。

Core 字段缺席在 A 投影中记录 `prereg_status=omitted`，并保留 `task_assignment_kind=pending_assignment`；旧 contract/admission 视图仍为 pending。C 的 `formal_input.status=none` 只表示本交付没有正式输入绑定，不声称 Core 已作 explicit-none 决定。未使用的正式输入不能阻止 operation 成功或诚实终态；显式消费的动作仍须核验实际使用的输入。

动作的输入核验只选择该次调用的 `input_package_artifact_id` 或 `input_package_bindings`；多个独立请求不因此成为全局前置条件，同一个请求内部的资产完整性仍须满足。bash、Python 和真实作业提交共用核验入口，未通过时不启动载荷、不提交作业；`submit_job(dry_run=True)` 的脚本渲染不产生真实提交，不触发消费核验。不能凭命令名或 `make -n` 一类参数推翻显式输入绑定。没有消费声明的动作不读取无关请求的验收状态。

没有 active 请求且无强制 fallback 政策时，普通已有 dataset 在同一次执行调用中自动做结构检查：读取显式 ID 的当前 record、记录版本/内容身份与 producer，检查包目录、manifest JSON 对象及 lineage/downstream contract 字段。它不是 Core 的派发精确绑定，也不证明 manifest 每项、payload 全字节或科学适用性；记录 `structure_checked`、`formal_verification=not_performed`、`task_fit=not_evaluated`，限制随 B 与 C 保留。不存在或结构损坏的显式包只阻止该次消费，修复后可重试；不要求补登记请求或重跑 Data。已有 active/formal/fallback 请求仍按原来源和验收规则处理，不回落普通包路径。包能被宿主检查不等于执行器具备读取权限，Core 的真实路径边界仍适用。

已登记 Data 请求的历史 `verified` 也不是永久有效的文件快照。消费前复用首次验收的
同一只读检查：沿原来源规则选择 record；有 managed receipt 时仍核对原 child/producer
与请求/派发 payload 身份，再检查当前结构与所请求资产。
历史验收不被清账；当前检查随该 action/intent 保存，损坏只阻止本次消费，修复后直接重试。
不新增模型验收手续，不重跑 Data，不替 Data 重建 manifest 成员文件 hash 或领域 schema 协议；
当前 artifact record 身份不等于 payload 字节不变，包修复后也不因此声明科学适用性已重验。

**纠正与换任务分开**：同一合同绑定的同一 artifact@version@hash 从不可用变可用，框架自动重评并追加观察，不要求模型办修复手续、不重跑动作。新合同 digest 则必须由 Core 正式选择；当前 Core resume 只接受原身份，因此现有合法出口是父编排器按新版合同 fresh 派发。节点不得扫描后继合同代替授权，不把 mutable scope/cache 当任务 owner。节点账本中的 A 是对已选任务的持久索引，不是替代 Core 合同的第二本授权账。旧 acceptance receipt 在迁移期只作下层接口适配，必须与同一精确合同一致；S1d 撤除旧消费者后同时删除适配，不能长期双权威。

未绑定 Core 合同不等于没有任务身份约束。此时在既有首条 A 记录中保存调用方
任务输入与选定上游输入的规范指纹，恢复时由共同只读核验比较；不另建 receipt。
scope、operation_category 这类分类描述不参与该指纹；prereg 使用所声明的精确
绑定，不把其当前可用性混入任务身份。旧 A 没有足够材料时只能报告无法核验，
不能从恢复时传入的新请求补造原任务，不能把两边 Core 身份为空判成任务未变。

**投影与 C 的区分**：作业进度是 B 的投影；交付就绪度由 A + B 推导；sealed outcome / closure 是 C 的版本化权威交付。route、step 分组、active view、next action、UI 状态只能是 A / B / C 的纯函数投影：不持久化、不成为并列状态机、不被任何约束读取。

## 3. 自然行动循环与三层约束

```
A 建立（框架） → admission（按 action 适用） → 行动（模型自由） → 自动记账（B）
  → 观察 / 判断 / 调整（模型；视图只给事实） → finish → 一致性核验 + 独立核验（按风险） → C 交付
```

三层约束，**前两层完全由框架执行，不要求模型填表**：

1. **action 前 admission**（按 action 适用）：权限与路径边界；实际平台能力（discovery 报告的 enforced / missing）；资源与风险（授权包络）；正式输入——只阻止会消费该输入的 action，不阻止能力探测、获取输入、读日志、诊断、取消或诚实 finish；prereg 硬约束（参数偏离问一次）；幂等身份（先持久化提交意图与 correlation key，再提交）。
2. **action / job 生命周期不变量**：先持久化意图与 nonce → 提交 → 绑定外部 ID → 收响应；丢 ACK 后由 adapter 声明的能力决定——能按 correlation key 查询的 adapter 做 reconcile，不能可靠查询的平台上状态保持 unknown / 待接管，绝不盲目重提；终态首次冻结后单调；取消只对本 run 归属的作业；恢复时补记空窗。不假设所有调度器都能按 nonce 查询。
3. **finish 一次交付一致性核验**（§6）。

**scope**：prereg 是 scientific 的充分非必要条件。绑定 prereg 一定 scientific；无 prereg 但任务要用本次结果支持新的科学结论仍是 scientific。scope 是描述与方法选择，不是执行或收尾的独立准入条件；没有确定分类就保留 unknown，不默认 operation，不要求父编排器或模型补分类手续。只有具体方法、必要输入或验收要求确实不清楚时才澄清该问题，而不是为了填 scope 问一次。任务成功仍由 required 的正面证据、实际执行与无确定性矛盾支持；unknown 本身不否决 success。

删除的是流程仪式门：需要路线才能执行、修订前先逐个收尾、finalize 前先声明产物、recovery_basis、classify 必须先调。

## 4. 边界不变量

| 不变量 | 内容 | 删掉会放过 |
|---|---|---|
| I1 边界按真实能力陈述 | 本机命令在 Core 沙箱原生后端里跑（Linux Landlock / bwrap、macOS seatbelt），可写根 rw、其余 ro，资源限制是起进程时的边界；静态解析命令预测写哪儿不再是门。集群作业侧没有沙箱：脚本固定 cwd 与输出根挡不住绝对路径写 baseline；capability discovery 必须报告每条边界是 enforced 还是 missing；缺远端隔离时只能 admission（脚本与参数中的路径、cwd 检查）+ 观测 + 披露，必要时 blocked，不得声称有物理只读墙 | 写坏 baseline；把 missing 写成 enforced |
| I2 三层约束 | §3 | ACK 丢失后重复提交 |
| I3 一事一 authority | §2 | 双轨可变权威 |
| I4 调度器事实由框架读 | 原文入账本，派生判定附出处；unknown 合法；running / unknown 存在时 success 不可达 | TIMEOUT 记成 success |
| I5 投影无状态 | §2 | 路线机器换名回来 |
| I6 finish 谓词 | §6 | 交付物不在却 success |
| I7 outcome 并列语义 | success / partial / failed / blocked / cancelled 按事实选，不是降级阶梯；三件套是所有 outcome 的稳定封套，允许 typed absence（not_run / unavailable），不得伪造测量 | 假三件套 |
| I8 授权 authority | §8 | 越出包络与不可逆损失 |
| I9 引导由状态决定 | §7 | （成本项）每请求 token 的大头 |
| I10 工作根、写 lease 与 capture 命名空间 | 区分三样：输入 / 工作根；串行独占写 lease；action-owned capture / output 命名空间。并发 producer 与独立重试默认隔离；configure → build → install、checkpoint 续写可在**显式 predecessor lineage** 下串行独占同一工作根；作业输出根按 intent nonce 在提交前分配，再绑定 job ID；共享并发 producer 才记 multi-producer / ambiguous，不能以猜测的命令级 lineage 支撑 success | 并发产物归属被猜错 |

## 5. B 的最小不可变结构（框架自动形成，模型面零 ID）

**所有 effectful writer 经过共同记账边界**：safe_run_bash、safe_execute_python、safe_write_file、submit_job、fetch_resource、artifact writer 及其他合法写入动作都登记真实 effect，按 support / experiment 分类但不漏；只读诊断不进 B。

每次物理执行自动形成一条 action / job evidence：执行身份、外部 job / array element 句柄、终态、retry / checkpoint 的因果边（模型显式重试或框架确定性判定时建立）、output observation（该 action 的 capture 命名空间内候选路径的身份变化）。模型不看内部 ID，不铸收据；视图用作业号与相对序号等可见句柄。run 起止快照只作补充。

内容证据按规模分层：小文件 hash；大文件与目录用有界 manifest（路径、大小、身份、修改标记、任务约定的检查命令）。删掉 action 级 observation 会放过：失败运行写出的产物让未修的重跑放行；同 inode 同大小的原地改写被 run 级快照判为"未变"。

**intent 的 crash-durable 语义**：意图记录带 protocol epoch 与 schema marker；先持久化再提交；损坏的尾记录可识别并恢复；旧协议 run 确定性进入只读 / drain / blocked，不能因"没有新账本"被误识别成新 run。

每次物理执行独立不可变；自动归组只用于视图与摘要。

## 6. finish：一致性核验与交付

**success 谓词**：每个冻结的 required obligation 都有正面满足证据，且不存在与当前交付 lineage 相关的确定性矛盾。

**自然要求与核验方法分离**：上句的机械证明只适用于已明确结构化的要求，不能把
“不存在内部 required_outputs 数组”解释成用户没有要求。启动时在同一个 obligations
记录中保存完整任务原文及来源；没有结构化条件时保存原文义务，不猜测文件名、软件名
或 scope。原文与正式条件属于 A，不可由模型替换；把要求落实成什么检查，是模型的
专业解释。方法可在读源码、诊断或执行后形成、调整，不等于修改成功标准。

自然任务在现有 finish 中提供 verification：忠实解释要求、file/glob/check 方法及
可选解释。框架实际运行只读检查、核对输出身份与来源；所有已明确的正式 required
仍独立求值，新增方法不能删改或覆盖它们。检查方法与结果属于 B/C，不写回 A；不
新增声明工具、父节点填表或 action 前手续。无核验时提示缺少哪种实际证据，不要求
补造 artifact 或重做载荷；partial/failed/blocked/cancelled 不强制提供方法。

收到的 `experiment_spec` 是父节点传来的正文，不冒称一定是用户原话；关联由运行时
提供，不用逐字包含或相似度门认证解释。工具路径复用 Core 坐标，shell 内路径按实际
cwd 解释。执行与观察共享本次批准的边界，但观察不授予写权，临时缓存和框架状态不
自动算业务产物；扫描预算、异常和已知并发归属缺口必须披露。

交付对象的身份/来源与检查方法分别求值：换 file/check 不得消除对象来源与失败事实。
正式 required 独立生效；既有输入审计核验输入身份，不强制重写，无文件自检不造文件。
任意 shell check 不提供完整文件读取依赖证明，不能宣称它机械证明了所指对象确被充分
检查。三件套只封存这同一份评价，不另建来源判定器；共享保存/冻结工具通过已有
content_contract 提前说明 Experiment v3 应调用 finish，不能等模型撞门才教入口。

对象类型不能偷偷增加任务条件：自然 file 方法默认不附加非空要求，显式非空与已有
正式 required 仍各自生效。glob 的 min_count 是合格候选的最少数量，不把未交付的
所有匹配项升级为 required；显式交付的对象不因候选筛选消失。未选候选的真实失败
仍保留在 B，不冒称已经恢复。目录是有界成员集合：普通文件与空叶目录核验身份和
来源，中间容器不额外要求本次新建；已有只读输入保留输入来源。目录存在或顶层
mtime 不证明整树正确，超预算、排除项、特殊对象与未观察来源如实披露。同 inode
目录的 metadata 调整不代替原创建者，不能靠 touch 改写创建动作的原退出码。空目录是否满足任务另按实际条件核验。

**语义覆盖边界**：自然语言的 success 是模型基于固定原任务和实际核验提出的交付
主张。引用原文、检查真的通过、文件身份正确，都不能机械证明任意自然语言被完整
理解或检查足够；三件套必须标注 `semantic_coverage=model_interpretation` 并保留完整
来源、方法、实际结果和限制。不得将这一机制宣传为自动防住所有漏项或降标。例如
“运行三次”只检查文件存在，仍是可能的专业解释错误；这要在真实模型验收中评价，
不靠伪语义正则、额外分类或逐句打卡门禁假装解决。正式结构化条件仍有原机械保证。

分类未定或纯分类解释冲突只在交付限制中披露，不进入 success 的缺口或矛盾列表，写入与回读三件套遵循同一规则。真实任务合同、正式输入绑定和产物身份的冲突仍按其事实核验；已绑定 prereg 的参数见证核验由该绑定驱动，不由 scope 标签启停。撤掉分类门不等于撤掉验收，也不授权把未约定的成功标准在收尾时补造出来。

正面证据：文件在其 action 的 capture 命名空间内、由本 run 的 action 写出（observation）、按 spec 非空 / 是文件 / 是目录；"检查命令"类由框架自己跑一遍——**检查命令同样受 admission、只读 / 沙箱与记账约束**，不是未审计的副作用入口；集群侧检查改用文件或日志类交付物；契约明确要求的无文件 operation 或 self-test，exit 0 加无矛盾诊断即为合法证据。"没有报错"不等于完成。
确定性矛盾（任一为真则 success 不可达）：未被合法重试解决的调度器失败、unknown / running；
required 检查命令失败；适用的任务约定 fatal 信号；产物身份与 observation 不符。
任务的正式计算条件、prereg 参数冲突与开放作业仍独立约束交付，不能被报告格式或无关检查替代。

**来源、命令状态与任务完成分别判断**：本地复合命令非零退出，不自动证明它写出的
所有产物无效，也不新增“全部 producer 必须退出零”的任务义务。交付对象在本次真实产生、
当前身份与记录一致且任务适用检查通过时，可以保留失败来源而交付；不得为过门禁要求
模型改写正确报告、添加时间戳或重跑已完成计算。deliverables 也可包括失败日志与诊断材料。
这不声称失败已经无影响：计算是否实际完成、结果是否有效须以相应任务条件及实证核验，
不能用 JSON 格式正确、模型自称辅助错误或无关 `true` 代替。

本地 action 的先后顺序、相同 cwd、串行 predecessor 或同字节重写都不证明恢复，
不替换原内容 producer、不抹掉原退出码。失败事实与当前对象的来源继续在 B/C 保留，
不能因 C 接受任务结果就把失败命令改记成功。未知影响在既有 limitations 披露；
自然语言条件的检查充分性仍遵守上述 model_interpretation 边界，不伪造语义认证。
作业重试仍按调度器事实及既有精确关系核验。

拒绝时在既有 finish 返回具体缺口、检查命令与输出、相关失败命令/退出码和对象路径，
以及局部纠正或诚实终态的下一动作；不要求模型读内部账本、再填声明或无变化重复 finish。

当前交付的正式文件要求、显式对象、file 方法和必要 glob 见证在任何检查执行前
进入同一观察窗口；方法顺序不得把检查后的身份冒充执行前身份。输入/输出的来源要求
不同，但检查后都核对成员集合、类型及叶对象身份。非空中间目录单纯 mtime 变化不等于
成员内容改变；覆盖/hash 不足标为不完整，不冒称已观察到删除或内容变化。窗口失效可在
A 不变时重新观察并实际检查；不永久冻结旧 C，也不承诺有界两端观察等于原子快照。

**诚实收尾永远可达**：success 不受支持时 finish 一次返回缺口清单与可达的诚实 outcome；partial / failed / blocked / cancelled 都不能被卡住。

finish 的 deliverables 是 C 中"实际交付了什么"的主张，verification 是“怎样核验”的专业解释：两者都不能事后修改成功标准；模型提供的 output hint 可以帮助观察，但不改变 A。没有必经的"声明交付物"工具。

**独立核验**（另一个不共享状态的进程；输入任务原文 + 清单 + 账本 + 摘要；输出"摘要里有没有被账本反驳的话"）只披露语义疑点，不替代机械谓词；按风险触发，小 operation 默认不跑。科学意义由下游判断，_reviewer 不是节点诚实交付的前置门。

**统一交付**：operation、scientific、unknown 共用 `ExperimentDeliveryV1`，记录 scope、outcome、执行与结果状态、正式输入绑定或错误、证据缺口、矛盾、限制和下一动作。分类不改变产物类型或为诚实收尾另建流程；scientific success 与 operation 共用 required 正面证据及无确定性矛盾的谓词，failed / blocked / partial / cancelled 保留诚实交付。

**S2 执行证据**：执行工具在既有 `action_started` / 提交 `intent` 内保存当次任务、prereg 精确绑定、输入准入结果与参数见证；不另建科学账本。`execution_params` 是工具入参见证，不等于进程实际使用参数或偏离授权；无参数声明记未观察，无适用的输入验收记未核验。finish 只对当前交付产物的 producer 读取这些见证：与同一精确 prereg 的显式参数冲突反驳 success，无关准备动作不污染交付，无关成功动作也不能清除冲突。缺少见证只披露，不再要求模型填一套参数手续。对照必须明确它比较的是声明而非实际使用；任务要求的物理或领域验证仍由真实检查支撑。

参数比较失败不等于确定性矛盾：同名字段的明确不同值仍为冲突；见证缺字段只能证明
没有记录该字段，额外字段只能证明未列入该比较对象。后两者保留原比较失败和字段明细，
在交付限制中披露，不能推断程序实际缺参或已经违规，也不声称参数已完整匹配。
这个区分同样适用于没有结果 producer 的 check-only 任务；混合出现明确值冲突时仍保留
原冲突或相关性未决缺口。任务明确要求的字段检查、科学验证和其他 required 不因此豁免。

执行前后观察与 finish 复核使用同一个文件身份比较：小文件同时看已有的 size、inode、mtime 与 hash，而不是只凭字节相等把历史 producer 当成当前 producer。同 inode 同内容的时间戳变化只保留为 identity modifier，不替换原内容来源；两者的明确冲突都不能被另一个无关匹配清除。相同字节的独立产物身份可由新动作归属；原地同内容重算与 touch 无法靠快照区分，因此不声称能据此证明生产动作已经恢复；任务完成仍由上述实际验收判断。身份字段完全相同的重写也无法由此证明，不按事件顺序猜测。

无结果 producer 的 check-only 任务，如 B 中存在与当前 A 的明确参数冲突，记录 `parameter_lineage_unresolved` 缺口，不谎称已证明相关或无关；没有这类冲突时不新增整体拒绝。该类任务的精确相关性关系、真实 Slurm 终态输出接线及实际参数使用验证仍未完整实现；不能从本地 file/glob 试验推定为已解决。

三件套的中立名称统一为 `experiment_*`。元数据只承载 `record_kind`、`delivery_id`、`delivery_owner`、`schema_version`、`envelope_sha256`、`artifact_role`，Core 冻结时附加实际冻结标记与时间。交付内容、账本指针与核验结果放在 payload，不再双写 `operation_closure_*` 或 scientific 特有的控制字段。旧协议 run 不作隐式转换。

**可恢复事务与交付纠正**：finish 在写三件套前持久化交付 manifest，逐一准备三件的精确身份，按 raw → clean → log 冻结，最后幂等写入该 closure 的一条 finish 记录。未提交事务的重入只补齐原交付，不混入新参数；已封存后，同请求且相关事实未变时原样重放，新交付主张或相关对象纠正则重新核验当前 A/B，形成后继 C，不重跑实验载荷。后继保持三件套 artifact ID，通过 Core amendment 增版本；完整前版 manifest 归档，前驱身份、目标版本与新 pending 计划在任何 head 修订前持久化。恢复复用精确目标版本，不因中断多造版本；审计与 finish 共用当前 manifest，不另猜“最新”。无关观察本身不触发新版本，历史失败不删除，C 纠正不改 A。

JSON 身份比较保留类型；可见账本行仍须确认持久化才能作为已提交交付。该保证覆盖节点支持的写入口和进程中断恢复；Core 尚无跨三件套的原子冻结或冻结 CAS，普通 Core reader 也不受节点事务锁约束，修订途中可能看到混合 head。节点当前事务未提交时审计不得称 sealed；全框架原子发布及中断完成接线仍属 Core，不能声称已解决。新事务 schema 不隐式迁移旧 run；被测旧实例继续使用其固定代码与历史记录。

## 7. 视图与引导

每轮注入有上限的 delta view，只有事实：开放作业及状态、清单每项当前在不在、最近一次失败信号、当前 blocker 与权限、至多 1–3 条有依据的软建议。完整 dashboard 按需。视图替换零散 hook 注入。方法在 skill。

执行方式建议是软的：综合持续时间、资源、是否需跨调用、平台能力给建议并说明理由；模型可覆盖软建议，不能绕过 hard admission。未识别命令在有界本地跑：超时且进程树确认终止 → 该 action 记 timed_out，目标默认未满足；终止不明 → unknown，禁止自动重提；是否改走作业由模型决定。

**工作计划（模型拥有，非权威；Codex 44 / 用户 09-23 质疑）**：删掉的是模型必须先办理、冻结并维护的 route 状态机，不是模型的专业规划能力。大型 WRF / MOM6 / 多阶段训练 / 参数矩阵 / checkpoint-restart 需要一份简洁、可调整、跨会话可恢复的工作计划：阶段与依赖、正式输入与环境前提、本地动作与受管作业的分界、各阶段成功检查、失败分支、最终交付。它是模型的可变工作假设，写在白板（`write_scratchpad`，每轮回注）；**不是 A/B/C 之外的第四套权威**：不决定权限、作业真相、产物归属或能否诚实 finish；B 的事实永远优先于计划文字。小型有界任务不强制先写计划。新证据改变判断时直接改计划并简述原因，没有 amend / revise 手续。"没有计划"或"计划过时"本身不得拒绝 action 或诚实 finish；只有具体 action 的权限、风险、资源与幂等条件参与 admission。同一 run 被平台恢复（checkpoint-resume，同一 State）时一页纸同时给出目标、最近工作计划（醒目标注为模型自己写的、非权威）、已发生事实、开放作业与未满足交付；跨 run 只在正式 handoff 携带了工作计划时才成立（Codex 46 口径）。专业 skill 提供领域计划模板与检查点（hpc-build：官方入口/依赖 → 工具链与环境 → 配置 → 构建 → 测试/运行 → 产物核验；longrun：staging → 资源与提交 → 观察 → 终态 → 输出核验），只帮助思考，不要求填内部身份，跳过不适用阶段不算违规。

## 8. 授权 authority

用户 / 平台的风险策略是授权 authority。首次真实外部提交、或越出预授权包络时问一次；已预授权包络内不重复询问。高危问人清单短且固定：删数据根、越出网名单、参数偏离 prereg、越出包络。

**取消**：普通 Experiment 只能取消本 run 拥有的作业。取消别人的作业不能靠高危确认升权；必须有任务明确目标与平台独立的 ownership / admin authority。

skill、模型、命令头都不能提升权限，也不能把 missing capability 写成 enforced；skill / KB / profile 只提供专业方法，不授予权限、不改变物理能力声明、不成为任务 authority。

## 9. 跨 session、ownership 与 protocol epoch

- 跨 session 的唯一原因是物理的：作业活得比会话长。机制是账本 + 同 run 互斥锁：任何新会话可只读账本；只有权威 owner 可写入、reconcile 或 cancel。重开时 owner 先补记空窗期的调度器事实。
- 谁在什么时候重开归编排层。Core 没有自动唤醒 / lease / reaper / ownership transfer 之前，不声明无人值守跨 session 闭环或彻底无孤儿；只声明账本可重开读取、开放作业可见且可由 owner 取消。
- **protocol epoch cutover**（用户已确认不兼容旧 run）：新协议不读写旧控制状态；旧终态只读；旧 artifact 只作带 provenance 的显式上游输入；旧开放作业由旧 owner drain，否则 blocked；不双读。
- 范围外机制的映射：data request → A（正式输入）与 B（受管 child 回执）；verdict / sediment / supersede → C 的版本化交付与下游判断；三者不持有 Experiment 控制状态。

**终止边界（Codex 36 P0-E / 38，S1 依赖边界）**：Experiment 只能在模型调用 `finish` 时挡住"有活作业的终态"。Core 的 `agent_loop` 只在「无 tool calls、正文非空、`_finish_gate_used` 未置位」时调 `run_on_before_finish`，任一 hook 返回消息后全局布尔置 True、同一 State 的下一次最终文本永久跳过；hook 异常 fail-open；turn cap、protocol/progress breaker 等终止路径不经过它。所以模型 submit 后直接输出文本可以绕过 `finish` 的拒绝；恢复旧 `external_job_handoff` 既只拦一次又指向退役工具，不是解法；节点也不复制第二个终止状态机。

**S1 声明**：外部作业的无绕过终止保证依赖下述 Core 接口；落地前只能做无外部作业的方法预览，真实 Slurm 不能据此宣称安全闭环。节点侧现有事实：`experiment_live_effect_unsettled`（on_end）在 run 结束仍有开放 / unknown 作业时向账本记 `live_effect_unsettled`（只记录）。

**Core handoff（最小接口，owner = Core）**：所有候选终止共用一个咽喉 `on_completion_admission(ctx, proposed_result) -> CompletionDecision{disposition: allow|reenter|suspend, code, fact_revision, facts, notice?}`。`model_finished`、turn cap、protocol/progress breaker 写 `LoopResult/run_end` 前都经过它（显式 user cancel 与 HITL pause 保持原语义）；`reenter` 每次重新求值、不设 used 布尔，Core 只按 `(code, fact_revision)` 做有界无进展计数，同一事实重复时转 `suspend`；`suspend` 泛化现有 executor 暂停骨架（先 checkpoint、不写 final summary/run_end、不注销 active run），需要非 tool-call 的 `resume_suspended(run_id)`（`State.reopen + resume_run_id`）；admission 回调抛错 fail-closed 为 suspended。Experiment 只提供只读事实契约（不新增状态机、不写 ledger）：A 是否 ready；B 账本 job 投影里是否仍有 intent/submitted/unknown；C 同 closure ID 的三件是否 frozen 且有 finish 记录。只有 A ready、C sealed、B 无开放 effect 才 `allow`。Core 最小回归五条见信箱 38 号。

**S1d-final（Codex 48）三条事务口径**：
- **A / scope**：用户与平台拥有 A，节点账本保存当前 run 对所选任务的持久索引；Core 合同存在时遵循 §2 的精确读取与重评规则，不能让节点收据成为第二授权源。首条 `run_bootstrap` 记录 typed prereg 状态、scope、basis 与 startup blocker，`project_run_authority` 统一投影。
  未绑定 Core 合同的节点输入仍是 exact union：`none{reason}` / `pending` / `bound{artifact_id, version, content_hash}`。bound 必须核对精确冻结身份；invalid、显式 pending、legacy、mixed 是 scientific signal，但仅令相关正式输入不可消费，不全局阻止探测、获取输入、诊断、取消或诚实报告。legacy 两键不作身份解析；Core 路径中的旧提示只留 witness、不覆盖合同。
  省略 prereg 不等于显式 pending，更不是 operation 证据；exact none 也不替调用方选 scope。普通 retry 不修改既有 scope 或合同；同合同同绑定可用性恢复不换任务、不重跑动作。`execution_params` 只是参数 witness；action-specific admission 只能依据显式 artifact/package binding，不能解析命令、代码或自然语言来猜消费。
- **bootstrap**：幂等运行时事务；durable phase=initializing 时，即使 `State.reopen` 丢失内存 flag、即使不是 turn 1，每次 turn 开始仍重放缺失写入直到 ready。任务原文/已有正式要求、授权包络和全部 writable/non-container roots 成功冻结/物化；真正没有任务输入时记具体缺口，不补猜要求。输入 startup blocker 本身不阻止 ready；没有模型可调的修复动作。冻结原文不要求已确定全部具体检查方法。
- **closure**：每笔交付先持久化 manifest，再准备全部三件草稿与精确身份，按 raw → clean → log 冻结；未提交重入只补缺的、只冻没冻的、不重铸；三件全冻后该 closure 恰好一条 `finish`。已封存纠正遵循 §6 后继事务，不把旧回执冒充新主张已被接纳；通用 writer 对保留三件套一律拒（不经旧 mode view）。Core 侧完成判定应要求当前同一 closure id + 精确冻结三件 + 持久 manifest + 规范 finish 记录（handoff，见 §9）。

## 10. 现有概念处置表

| 概念 | 处置 |
|---|---|
| 沙箱、路径角色、资源限制、出网名单；高危暂停（清单收短到 §8） | 保留为边界 |
| 作业提交（框架生成脚本）、调度器事实、取消、nonce | 保留为 B 事实与生命周期不变量；reconcile 能力由 adapter 声明 |
| 诊断规则跟着结果走、调度器 kill 行、终态 references | 保留为证据与方法 |
| skills、能力发现、数据请求契约、verdict、sediment、supersede | 保留（后四者按 §9 映射） |
| raw / clean / log 三件与冻结顺序 | 保留为交付封套，加 typed absence |
| census 的行动记录、作业回执 | 升为 B 的 action / job evidence |
| required obligations 与交付物清单 | 新的 A 派生物；取代路线 expected_outputs、ROC refs |
| action 级 output observation + capture 命名空间 | 新的 B 事实；取代 attempt 级 sha256 归属与见证 |
| 独立核验报告 | 新的 C 证据，按风险触发 |
| execution route / step / route_step_id / effects / 修订 / recovery_basis / envelope | 删除 |
| classify_experiment_scope、preflight、verify_external_job_execution、finalize_external_job、reconcile_external_submission、orphan 登记、handoff task、record_operation_completion、set_deliverables 一类前置表单 | 从模型面删除或不建 |
| 零散 loop hook 注入、拒绝点出口文案 | 删除大部分；拒绝点只剩边界类 |
| 谁触发重开、Core authority pause、UI / CLI 派发一致、lease / reaper / ownership transfer、根 tests 引用 | 跨 owner |

## 11. 专业能力层

分层规则：共同能力进内核；领域方法进 skills（先作候选，经审计、合并、按需加载后再定；允许隔离的应用 profile / parser / validator skill）；平台差异进 capability discovery 与 adapter。能力层只新增事实来源与方法，不新增控制状态。

**验证是 Experiment 的职责**：不裁决最终科学意义，但必须执行并报告任务约定的验证——schema、单位、范围、缺失值、参考解、守恒量；结果进 clean_results，任务约定为 fatal 的进 §6 谓词。

能力域（模型判断 / 共同不变量 / 方法与平台接口 / 交付证据）：科研协议执行；数据与 IO；源码构建与环境；调度器与长作业（adapter 四件契约 + 声明的 reconcile 能力）；并行与加速（rank 级事实按平台可提供的最细粒度并披露缺失）；checkpoint / restart；性能测量（步骤行、日志、profiler、应用计时，标注不确定度）；故障诊断（通用层只送达、判重、指向文档；应用修法进 KB）；可复现性与 provenance；专业工作流交接；AI 训练与推理（模型 / 数据 / 代码身份，model / optimizer / scheduler / scaler / RNG / sampler 的 checkpoint 集合，mixed precision、NaN / Inf / OOM，分布式指标聚合与确定性，推理 warmup 与延迟分布）。

承载力检查（八类：HPC 构建、真实 Slurm 单作业、参数矩阵 / array、checkpoint、MPI 部分 rank 失败、长作业大输出、scientific 三件套、AI 分布式训练与推理）的结论：都不需要第二个状态机；缺的是候选 skill / references / adapter 与两条清单语法（矩阵展开、分段产物）。模拟器只作前测，对应能力只在真实 Slurm / MPI / GPU 上验过后才声明。

专业度验收看六件事：框架是否强迫模型复述已知事实或办理 bookkeeping；已知事实能否一次取回并允许健康刷新；是否发生重复物理副作用；中断、失败、纠正后能否收敛；任务是否完成、不能完成时是否诚实交付；不同正常能力的模型是否都不依赖内部 ID 与生命周期术语。轮数与 token 只作趋势诊断。

## 12. 首个垂直切片与复杂度预算

首切片只证明五件：operation 事实链；一个真实 Slurm adapter；capture 命名空间与 action 级 observation；同 owner 人工 reattach；hpc-build 入口。不证明：无人值守 reaper、跨 owner 接管、array、checkpoint、MPI / GPU、scientific、远端物理隔离。顺序：S0 规范落地 → S1a 内核与共同记账 → S1b adapter、远端 admission、durable intent → S1d 原子切换与撤旧 → S1c-final 在最终树上真 Slurm 验收（成功；cancel 或 nonzero / TIMEOUT 之一；ACK-loss / reconcile 后唯一作业；同 owner runtime reattach；hpc-build 的 source / version / toolchain / command / log / output / smoke provenance）→ S2-min scientific 最小闭环后才评估 merge / release。

复杂度预算：新增账本 → 撤 census / 路线 / finalize / recovery 事件的权威地位；新增 required obligations 与清单 → 撤路线 expected_outputs 与 ROC refs；新增 action 级 observation → 撤 attempt 级 sha256 归属与见证；新增一个 finish → 撤 classify / declare / preflight / verify / finalize / reconcile / ROC 的模型面。每个切片新增一个权威就撤一个旧权威。

## 附：S1d 原子切换——被移出 v3 harness 的 hook 与它们的承接者

每个被移除的 hook 原来保护的物理不变量，及其在 v3 里由谁承接。没有承接者的不删。

| 移除的 hook | 原来保护什么 | v3 承接者 |
|---|---|---|
| `experiment_contract_audit`（on_end） | 结束时机械审计 verdict / sediment / 执行证据闭环，失败则把 run 判 blocked | 闭包一致性由统一 `finish` 与只读交付审计承担；科学意义交下游。不能再按旧 route / 旧 closure 草稿否决已交付的 run |
| `external_job_handoff`（on_end / on_before_finish） | 否决未收尾外部作业的结束；为每个未 finalize 的作业建持久 workflow task | `finish` 对 live / unknown 作业返回 `live_external_effect`，不形成 terminal closure；账本 + 同 run 锁是跨 session 的全部机制（无人值守闭环是 Core 契约请求） |
| `external_job_reconciliation`（on_turn_start） | 启动时对账 dangling 提交，防续跑重复提交 | 提交前持久化 intent + nonce；`scheduler_adapters.resolve_lost_ack` 按 adapter 声明的能力 reconcile，查不了保持 unknown 绝不重提 |
| `route_block_resolution`（on_end） | 从 transcript 推导已被后续成功消解的路线阻塞 | 没有路线就没有路线阻塞 |
| `secondary_experiment_log_recovery`（on_end） | 缺 experiment_log 时生成最小不可分析记录 | `finish` 对所有 outcome 冻结三件套（typed absence） |
| `sediment_closure_advisor`（on_turn_end） | sediment claim 被拒 / 冻结日志未闭环时注入恢复指引 | 不恢复旧 sediment 收尾手续；执行记录交付与下游意义分开 |
| `experiment_skill_router`（on_turn_end） | scope 确认后为 scientific run 路由 SOP | scientific-results 按需加载；不恢复旧 router |
| `execution_supervisor`、`execution_control`、`strategic_review_injector`、`plan_before_compile`、`provisioned_build_tracker`、`critical_rules_reminder`、`workflow_frame` | 中途提示、DAG 证据推进、战略复盘、规划脚手架 | 都是"引导"类，不保护物理不变量；v3 的引导只有事实型 delta view + skills（governing §7）。失败信号由 `generic_failure_detector` / `repeated_error_detector` 继续给 |
| `turn_one_briefing`（含 `path_role_convention`、`prereg_binding_briefing` 两部件） | turn-1 路径角色契约与 prereg 绑定简报 | `experiment_v3_bootstrap` 的一页纸；`task_briefing` 与 `data_source_reachability_preflight` 两个事实型部件保留为独立 hook |

保留的安全与运行观测：`high_risk_command_audit`（高危命令对账）、`preprocessing_boundary_audit`（仅本次运行的文件名/命令启发式线索，不判违规、不写长期 pitfall，不替代权限与正式输入核验）、`repro_snapshot`、`output_corruption_sentinel`、`generic_failure_detector`、`repeated_error_detector`、`experiment_scheduler_facts_router`、`gpu_skill_injector`、两个 ask nudge。

**工具面复核（S1d 全量测试后）**：按"是否属于自然工作循环"重判，五个曾被机械移出的工具回到模型面——`web_fetch` / `web_search`（查官方文档是诊断的一部分）、`write_scratchpad`（白板）、`create_claim`（KB 死路沉淀，skill `hpc-build` 依赖）、`preflight_build_resources`（纯建议、从不阻塞、不问）。仍退出的是有仪式或裁决性质的工具。

**状态视图注入策略**：`experiment_delta_view` 只在 v3 就绪的 run 上、且视图内容较上轮有变化时注入一条 system 消息；旧协议 run 与无变化轮次零字。每轮无条件追加会把工具结果从消息尾部挤开，也等于强迫模型复述已知事实。

**共享工具描述里的旧词**（`save_artifact` / `freeze_artifact` 的 amend、`dispatch_data_request` / `reconcile_data_dispatch` 的 receipt）归 framework owner，节点不越界改；已作为交接项登记。
