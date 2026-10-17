# 060：普通上游 dataset 的动作内核验（2026-09-23）

基线 `8c52ddff8f72d43a000ad3596c85e68acc5ab06a`，分支 `codex/060-r3a-c`。
只改 Experiment；不涉及 Core 权限、派发、根测试或 benchmark 分支。

## 修复什么

原动作准入在没有 active Data 请求时整体返回不适用，显式选择不存在的 dataset
也会启动执行。另一方面，若为修补这点要求所有父传数据先补 spec、重新运行 Data、
取得 Core exact pin，则会把合法普通输入变成新的流程门；当前契约没有这个要求。

本次复用已存在的 dataset payload 检查，不新建输入账本或验收工具：

- `dataset_checks.inspect_dataset_package` 只检查传入 record 与其声明路径，不拥有授权或来源选择。
- 正式 Data 核验在原来源、receipt、prereg 检查之后调用它；原 input-delivery writer 不变。
- 无 active 请求且无强制 fallback 的动作，对显式选择的普通 dataset 自动调用同一检查器。
- `structure_checked` 与正式 `verified` 分开；当前 lookup ID、record 版本/hash/producer、
  结构结果进入已有 action/intent execution context，结果生产关系带入交付，不另建收据。
- 显式包缺失或损坏在载荷前拒绝；修复同一包后可重试。准备、读日志、取消、诚实 finish
  不因未使用的输入受阻；不要求重新登记 Data 请求。

## 不声称解决什么

- 普通检查不是 Core exact dispatch binding，不能证明“这个包就是用户唯一指定的输入”。
- `content_hash` 是 artifact record 的既有身份，不是此次重新计算 payload 全体文件哈希。
- 无请求资产清单时，只检查目录、manifest JSON 对象、lineage/downstream contract 存在性；
  不推断任意 manifest 的成员格式，不声称每个声明文件已校验或科学 schema/单位已验证。
- 检查器没有扩展执行器读取路径；实际进程仍服从 Core 沙箱。
- 正式请求既有的已验证账本复用语义没有重写；本片不声称其后续可变 payload 获得永久完整性保证。

## 验证与有意调整

第一版 12 条动作级回归在未修复实现上：**10 failed / 2 passed**。两个绿格是已有正式请求
不能被绕过、未选择输入的准备不受影响。没有用这些正向控制伪称所有新测试都应 base-red。

两条旧测试曾要求“未登记且不存在的包可继续执行、标为 not_performed”；这次明确更新为
零执行拒绝，同时新增真实存在普通包的正向、修复重试、多包、辅助包和身份披露测试。
纯检查器有 9 格；动作到真实计算与统一交付的回归另验证限制进入 C。

最终定向 Python 3.11 输入与模型契约回归：175 passed / 3 skipped / 1 既有 xfailed。
独立审查给出本切片 ACCEPT，并独立复跑 55 passed。内存变异将普通检查的结论改成无条件
放行，缺失/错类型/坏 manifest/多包一坏四格全部转红；没有修改工作树来做变异。
节点全量在最后四格模型说明/多输入测试加入前为 3822 passed / 22 skipped / 5 既有 xfailed；
最终提交门禁以交接包中的精确 HEAD 报告为准，不能用定向测试替代真实 UI/Slurm。
本片没有启动模型、UI 服务或外部作业。

## 后续：已登记 Data 输入的当前有效性（基线 40e33294）

上述 312d25a5 切片之后发现同一输入边界的另一处接缝：真实验收通过的 Data 包，
在 manifest 变坏或 required 文件消失后，执行前审计仍直接复用 `verified=true`。
普通请求与 managed formal Data direct-child 两条验收链都已在修改前复现；后者的
Data 调用与 child 返回由受控夹具提供，节点验收函数和持久账本为真实实现，未运行
Data LLM 或完整父子编排。现有结构检查器能正确报出损坏，根因是 action audit
没调用它，而不是缺少新 schema。

修复将首次验收里的 producer/receipt 读取与结构核验抽到同一个只读入口；原验收 writer
保留，动作 audit 对本次选中的 Data 包自动复读。当前 record 身份、结构结果与限制进入
既有 execution context，并由交付引用。原 `verified` 继续表示历史验收，不因当前失败
改写或删除；包修复后原动作可重试，不要求再调验收工具或重新提交 Data。

仍不证明两次观察间的字节不变、并发写隔离、包内全部成员文件哈希或科学单位/schema；
这些不能从结构通过推导。正式 Data 来源仍沿原 receipt/direct-child 规则，没有降为
普通父包；原 receipt 的请求/派发 payload digest 仍核对，与文件字节哈希是两件事。
没有 selector 的准备动作、无关 pending spec、fallback 自有验收不扩大范围。
五条旧正例先前仅有不存在的 dataset ID 加 verified 标记，现补真实包/请求/验收，
保留原有选择和迁移断言；新增缺失 artifact 不能借历史 verified 放行的负例。
四条独立 formal 回归覆盖坏包零执行、修复直接继续且仅一次原始验收、未声明消费的
准备不受阻、多个请求只核当前选择。修复前最小证据明确区分了“当前结构检查失败”
与“动作审计仍通过”；不是把正常控制格也虚称为 base-red。

合并定向 124 passed / 1 skipped；Python 3.11 节点全量 3833 passed / 22 skipped /
5 既有 xfailed。三组根静态测试 20 passed / 3 failed，失败字段和拒绝点计数与
312d25a5 的精确日志一致；共享登记及 Core 接线债仍未解决。完整干净 HEAD gate
与独立审查以随补丁交接的结果为准，不把前一切片的 gate 结果移作本轮证据。

独立审查确认共同入口与恢复路径，35 条定向通过；内存变异绕过当前检查后，关键
负例真实启动载荷并转红。审查要求区分“已核对的派发 payload digest”和“未重算的
成员文件内容 hash”，已修正运行时披露措辞；无新增状态或模型手续。
