# R3-A/C 统一交付本地检查点

日期：2026-09-23。状态：节点确定性验证通过，尚非整合或发布验收。

## 基线与协作边界

- worktree：`harness-framework-impl-060-r3a-c`；分支：`codex/060-r3a-c`，无 upstream。
- 本批起点：`ce0f12c6a489a13d2a448318ccc64b1e9497bd03`。
- 本批之前已有四个本地提交，顺序为 `0b11a71a`（账本幂等）、`54772a9c`（bootstrap）、`b0a1b592`（交付事务）、`ce0f12c6`（提交和模型界面）。共同起点是 `fb88f35f`。
- 核对时本地远端引用 `origin/impl/060-slice1` 为 `d0830bac91911faeeef70fcfc5cc00992c765998`；相对本批起点为远端独有 9、本地独有 4。未 merge/rebase/push。
- 用户已安排另一独立会话核验 `d0830bac` 的四项修复、补回归并提供归并清单。两边不得直接覆盖对方 worktree；运行时重叠缺陷先列明，再指定唯一实现方。

## 本批完成了什么

目标是把同一次交付收敛到一个写入/恢复入口，不增加模型要办理的路线、核验或冻结手续。

1. `finish` 以冻结的 A 与最终读取的 B 组装 `ExperimentDeliveryV1`。operation 的五种 outcome 共用一个事务；scientific/unknown 的非成功结果同样能够诚实交付。本切片不支持 scientific success。
2. 三件套统一 `experiment_*` 名称、中立元数据与相同交付信封。不再在新协议交付中双写旧 `operation_closure_*` 控制字段。
3. 先持久化 manifest，再准备全部三件及其精确身份，按 raw → clean → log 冻结，最后幂等提交一条 finish。恢复补齐首份交付，不按第二次参数重新铸造。
4. 恢复时仍检查当前开放/未知副作用，不能因为已有 manifest 而遗忘后来开放的作业。正常 fresh finish 在异步核验后重读 A/B，并重算确定性矛盾，不能用旧快照掩盖新 blocker 或失败。
5. 精确身份比较保留 JSON 类型；`True`、`1`、`1.0` 不混同。manifest、三件 head/冻结身份与账本 commit 必须相互一致；审计复用事务的只读核验，不另建完成事实。
6. `append_once` 对新写及可见的重放行确认文件和父目录持久化。fsync 失败不能被下一次“行已存在”误报成耐久提交，也不追加第二条 finish。
7. v3 旧收尾 writer/通用三件套写入口不能绕过新事务；旧协议状态不隐式迁移。
8. 支持工具的真实执行不再误记为 not-run；远端写边界披露保留在 raw/clean 内容中，不因元数据改名丢失。架构正文与旧测试同步迁移。

## 验证记录

在上述 worktree、本批完整未提交源码上执行：

- `uv run pytest -q nodes/experiment/tests --tb=short`：**3696 passed, 20 skipped, 1 xfailed**，136.06 秒；12 条弃用警告。
- `test_060_r3a_finish_delivery.py`：41 passed；其中恢复开放作业、最终快照新增失败、原始交付重放三格由独立子审查再跑，3 passed，窄范围 verdict 为 accept。
- 两个独立进程内变异：忽略恢复 live-effect / 复用陈旧 contradictions，各有对应测试转红；不回退或修改产品源码。另有精确身份类型坏基线和陈旧 A 投影变异验证记录。
- `/usr/bin/python3.11 -m py_compile` 覆盖全部已跟踪节点 Python 与三个新增测试文件，通过。解释器已验证 SQLite 等必要标准库可导入。
- `git diff --check` 通过。

上述结果不是 Python 3.11 全套 pytest、真实模型/UI 或 Slurm 结果，不能互相替代。

### 根测试与跨 owner 剩余项

只读运行以下四文件：`test_refusal_sites_are_classified.py`、`test_instructed_tools_are_in_whitelist.py`、`test_model_commands_reach_the_os_through_one_throat.py`、`test_no_model_tool_carries_an_exemption.py`。结果 **21 passed / 2 failed**。

1. 拒绝点登记测试失败。当前扫描值/共享基线为 delivery_transaction `1/0`、finish `12/0`、operation_completion `40/26`、run_bootstrap `3/0`。不能把这些都称为本批新增：使用同一 AST 扫描器比较 `ce0f12c6` 与本批，分别为 `1→1`、`9→12`、`38→40`、`3→3`，本批净增 **5**。finish 包含移除旧分支和新增共同错误出口；operation_completion 新增 epoch/旧 writer 边界保护。登记必须解释语义及恢复出口，不能改名躲过扫描。共享登记文件在本会话授权外，尚未修改。
2. 工具白名单测试仍扫描出已退出 harness 白名单的旧提示：declare_execution_envelope、declare_inconclusive_verdict、declare_no_sediment、finalize_external_job、reconcile_external_submission、record_operation_completion、resolve_unknown_orphan、supersede_closure_draft。需结合最终工具/旧代码退役包清理或明确接线，不应为让测试转绿而恢复旧仪式工具。没有对基线执行整套根测试，不声称已证明所有失败均为继承。

协作完整 `gate.sh`（包含基线两侧、根层/其他节点、3.11 分片和文档导出）本检查点尚未执行，不是正式跨机交付验收件。

## 与 d0830bac 的归并清单

| 工作面 | 我方本批 | 归并要求 |
| --- | --- | --- |
| finish.py | 统一信封、读取 A/B、完整恢复路径 | 不整文件覆盖；保留对方四项修复的反例，再验证新事务承接旧三件交叉核验的完整不变量 |
| delivery_transaction.py / ledger.py | 唯一事务及幂等 commit | 不保留两套 closure manifest、锁或完成判定；以共同核验函数承重 |
| run_bootstrap / prereg 校验 | 本批未改，上方本地提交曾改 | 由另一会话核验后归并：非法 typed 输入、遗留键存在性、scope 不可静默改写均不得回退 |
| contract_audit / operation_completion | 中立审计、旧 writer 退役 | 不重新接回旧 scientific 流程门；也不得放开通用 writer 铸造三件套 |
| tests / governing 文档 | 新事务回归与旧测试迁移 | 两边独立反例合并保留；正式规则只保留一份一致定义 |

下一步：收到独立核验和归并清单后，由一个整合 owner 在独立分支归并；重新跑节点/根耦合测试，再安排真实入口验收。此检查点没有改共享登记、部署服务、提交真实作业或向产品远端 push。

## 未实现、未验证及保证边界

- 未实现：scientific success（S2-min）和 Core 完成消费端的完整迁移；不能据本检查点宣称整个 060 完成。
- 未验证：本批真实 UI、LLM、真实 Slurm 及跨 session 无人值守闭环；另一个会话的 `d0830bac` 核验仍独立进行。
- 事务锁与故障注入覆盖受支持的节点写入口和进程中断恢复；没有声称 Core 跨三件原子冻结、冻结 CAS 或整机断电的整组三件原子性。
- Core 正常工具派发下 finish 为非 replayable-read 顺序动作；独立并发直调整个 `_finish` 的 evaluate→snapshot→seal 原子性不在本批保证内。事务内部 settle 的并发保护不等于整个运行生命周期已原子化。
- 只读交付审计核对 C 的 manifest/冻结三件/finish 一致性，不冒充 A 准入或 B 作业生命周期 owner。没有 lease/reaper/handoff 接口时，不宣称节点自行实现了这些 Core 能力。
