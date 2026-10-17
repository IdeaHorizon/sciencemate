# 060 S2 本地科研统一交付检查点（2026-09-23）

## 结论与范围

基线 `a7c7eaef68f7b051f159e080495cfa1de5fb7a82`，工作分支
`codex/060-r3a-c`。本片打通本地 scientific 的共享 finish，不建立新的科学状态机、
模型工具或收据手续。不是完整 S2/Slurm/UI 验收，不是可合并或可发布声明。

本轮 Claude 离线；主线程实现和整合，独立子代理复核模型面、产物归属及 check-only。
所有产品改动在 `nodes/experiment/**`，产品分支未 push，未创建 PR。

## 行为与 owner

- A 仍由现有 bootstrap/task contract 精确投影；没有改变或放宽 prereg 绑定。
- B 使用上一检查点已经落盘的 action/intent execution context，记录参数见证与输入核验状态。
- C 仍走一个 `finish → manifest → raw/clean/log → finish commit` 事务。
  移除了 scientific-only 的整体拒绝；成功仍需 required 正面证据和无确定性矛盾。
- file 与 glob 的当前产物来源共用同一身份比较。仅当前交付链上的参数声明与精确绑定
  prereg 比较；无关准备动作不污染结果，无关匹配不能消除生产动作的冲突。
- 参数声明不是程序实际使用的证明。缺少见证如实披露，不要求每个 prepare 都填参数表。
- scientific-results 改为按需专业方法：正式输入、参数、原始证据、质量检查、偏离、统一交付。
  状态是 proposed；删掉过时的 primary-scientific-closure，不恢复旧 router 或手工冻结流程。
- 同步 AGENTS、governing、harness 和实际加载测试；没有修改 Core 或根 tests。

## 独立审查促成的修正

### 相同结果不等于同一执行

旧 capture 与 finish 都在小文件有 hash 时只比较字节，导致真正重跑不能替代历史 producer。
现在前后观察与交付复核共用 kind/size/inode/mtime/hash；hash 缺失不能降级成 stat-only 通过，
hash 探测失败也不能制造“发生了写入”的证据。

独立审查随后给出真实反例：value=-7 计算 square=49 后，只 touch 文件并声明 value=7，
一度可消掉旧冲突。这条反例保持普通失败测试，未用 xfail 隐藏。

最终消费者按原有 before/after 投影内容来源：同 inode 同字节的时间戳变化只算 identity modifier，
原内容来源不变；原来源与修改者的明确冲突均保留。新独立产物身份或真实内容改变才可形成新来源。
这是纯读取关系，不改写历史 B，不猜 latest seq，不新增执行跟踪器或模型表单。

因此：原地同字节重算与 touch 仍不可由快照区分，不声称该场景能自动消除旧参数冲突。
实际独立产物重跑可成功；没有证据的情况可诚实 partial/failed/blocked，而非伪 success。

### check-only 的相关性缺口

仅有检查命令、没有 file/glob producer 时，不能从现有 B 证明某个历史参数冲突是相关还是无关。
若确有同一精确 prereg 的已知冲突，则记录 `parameter_lineage_unresolved` 为 gap，不能默默漏掉。
没有这种冲突的 check-only 不增加整体拒绝，并披露实际参数未核实。

这不是已完成的精确因果绑定：无关 prepare 冲突也无法自动免责。后续应由同一执行事实关系解决，
不是让模型挑收据、扫描命令猜依赖或新建用途账本。

## 验证

- 基线原样恢复待实现的五格验收：2 failed / 3 passed；两条真实计算均在旧 scientific-only gate 被拒。
- 本地真实进程正向：有冻结 prereg、无 prereg 探索；结果、检查、三件套、审计及重复 finish。
- 负向：非零退出、缺输入、伪成功、无关参数匹配、file/glob 同源、hash 缺失、旁路修改、touch 与重叠来源。
- 最终本地科研与身份窄集：16 passed；Python 3.11 共同事务与科研窄集曾通过 77 项。
- 模型面定向：74 passed / 4 skipped；真实 load_skill 可用、旧 skill 不可加载，科学 skill 不再 blanket skip。
- 最后独立补入的 check-only：4 passed；与科研/身份/模型面合跑 Python 3.11：94 passed / 4 skipped。
- 全 Experiment 回归：3780 passed / 19 skipped / 5 既有 xfailed，141.84 秒。
  这是节点测试，不代替最终提交上的跨仓 gate 或真实 UI/Slurm。
- 内存变异 M1：移除交付链消费者，file/glob 冲突格 2/2 转红。
- 内存变异 M2：恢复 hash-only 复核，独立重跑与旁路修改格 2/2 转红。
- 内存变异 M3：忽略 check-only 相关性缺口，两条已知冲突格 2/2 转红。
  变异未修改工作树、未 reset 或覆盖用户文件。

## 仍未完成

1. **S2 边界**：check-only 的精确生产关系、实际参数使用验证。父级普通 dataset 的动作内结构检查
   已由[后续检查点](r3a_parent_dataset_admission_20260923.md)接入；不是 Core 正式派发绑定或科学适用性证明。
   不从 witness、schema 或退出码推定真实科学执行；不声称全任务类型已经完成。
   现有 `_simulation_contract_block` / submit 参数审计仍有仅记 deviation、不构成偏离授权的路径；
   本片的 finish 对照不是执行前授权机制，不能声称已完成 governing 的实质偏离前置处置。
2. **受管输出**：下述跟进删除重复终态 writer；后续
   [受管输出来源检查点](r3a_managed_output_provenance_20260923.md) 又修复 after-only 归属及伪 action，
   并验证终态写成但 ACK 丢失的复入。仍不能证明窗口内排他 writer，或恢复未持久化的首次快照；
   不能把唯一 writer、前后快照等同于完整操作系统产物归属证明。
3. **Core 完成**：#1141 的统一完成接线仍未解决；节点成功不能推出平台所有结束路径都正确。
4. **授权恢复**：#1069 的恢复后出网指引、#770 的平台可信恢复选择仍按既有交接处理。
5. **真实验收**：本片没有启动模型/UI/服务或真实 Slurm 作业；最终树上的 S1c 与科学模型任务仍待测。

下一步继续验证共享事实关系与真实执行链，不恢复路线仪式、不把尚未观察到的能力写成已保证。

## 同日跟进：终态只有一个 writer

进一步核实调用链后，纠正上文初始缺口描述的可达性：新 health/status 本来就先走
`_settle_if_terminal → job_settlement.settle_terminal`。重复 writer 位于仍保留的
`freeze_first_terminal_output_observation`，旧 finalize/ROC 在健康查询之后再调用它。
常规组合是完整 terminal 后又追加缺 derived state / exit code / output observation 的 terminal，
导致 `project_jobs` 读后条而 `existing_terminal` 读首条；直接 helper-first 才是抢写后阻止结算。
不能把这个 helper 反例描述成新版模型面的每次健康查询都会缺输出链接。

最小修复：删除 helper 中的 16 行终态写入，只保留原 transcript completion snapshot；
不改 shared settlement、reducer、旧首次快照的不可变与 unfrozen 语义，不增加模型工具或门。
`job_terminal` 的生产 writer 从两处减为一处，不兼容修复旧 run 已污染的账本。

- 独立回归修前 4 failed / 1 passed：helper-first、health-first、status-first、
  队列消失但 accounting 不可用；PENDING 控制格原本就绿。
- 修后连同 035a 健康冻结、自然流程、本地科学交付：56 passed，7.80 秒。
- 独立修后复跑：5 passed，0.35 秒；节点全量：3789 passed / 19 skipped /
  5 既有 xfailed，145.16 秒。未将上一个提交的跨仓 gate 冒充本次提交的门禁。
- 这些测试使用真实 State/ledger/文件，调度响应由 fixture 提供；不是实际 sbatch 验收。
- 独立方案复核赞成删除重复 writer，并指出上述 after-only 与中途落盘失败边界；未顺手重写作业系统。
