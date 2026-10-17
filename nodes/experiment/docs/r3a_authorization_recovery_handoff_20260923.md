# 同一 run reopen 后出网重授权指引缺口交接（2026-09-23）

## 结论

基线为 `codex/060-r3a-c` 的
`e0c78221179ff7899c86cd6b770e2c2322061843`。当前的正常授权链在同一个
`State` 对象上成立；但以同一 `run_id` 和 `session_id` 调用
`State.reopen(...)` 后，已批准的 exact-host grant 不再可见。与此同时，Experiment
仍会从持久 transcript 读到该 host 曾调用过 `request_network_access`，因此既不放行，
也不在该 preflight 的结构化结果中给出新的授权请求建议，而是返回
`network_access_request_unresolved`/`report_blocker`。

这不是“Core 必须持久化 grant”的结论。既有安全决议明确授权按 run 计、不落盘、不写入
环境，避免 agent 能修改自己的权限墙：#1068 已关闭，wangd 在评论 5409 重申该边界；
#770 评论 4661 也明确反对持久化。真正缺口是两个机制组合后的错误恢复指引：Core 有意
的易失 grant 在 reopen 后不再生效，而 Experiment 又因“历史上请求过”从结构化结果中
移除再次请求建议，只给出 `report_blocker`。授权工具本身仍在 harness 中可调用，因此
这不是框架硬拒绝重新审批，也不妨碍诚实报告 blocked。Experiment 不应自行补建持久
白名单，但应把仍然合法的现有 HITL 路径呈现为恢复动作。
本证据包没有访问网络、没有连接 LLM、没有修改 Core，也没有创建 issue、commit 或 push。

## 当前调用链与首个错误状态

1. `shared/tools/builtin.py::_request_network_access` 调用
   `core/capability_grants.request_network_grant`，产生 capability-grant pause。
2. `core/pause_driver.py::drive_pause_chain` 在用户回答后调用
   `core.capability_grants.grant_from_answer`，并追加
   `capability_grant_answered` 审计事件。
3. `core/capability_grants.py` 把批准写入 live `State` 的动态
   `_capability_grants` 属性；该模块明确把它定义为进程内、非持久状态。
4. `nodes/experiment/tools/resource_fetch.py::_require_egress_access` 先通过
   `core.capability_grants.is_granted` 查询 Core 授权；若没有授权，再通过
   `_network_access_was_requested` 扫描持久 transcript 中的
   `request_network_access` tool call。
5. `core/state.py::State.reopen` 重建同一 run 的 `State`，但不恢复动态
   `_capability_grants`。于是第 4 步看到“grant 不存在、请求历史存在”，返回 unresolved，
   从结果中省略 suggested request action，并只建议 `report_blocker`。这不会注销
   `request_network_access` 工具；显式再次调用仍能产生新的 pause。

Core reopen 后没有恢复 grant，符合当前“不落盘”的既定边界，本身不能据此判为缺陷。
首个可执行的组合缺口在 Experiment：`_network_access_was_requested` 只问“是否曾请求”，
没有区分仍待回答、已拒绝和已批准但现已失效，因而给出“只应 report blocker”的错误
恢复建议。它是可达性提示/协议缺口，而非框架级权限工具不可调用。

持久 transcript 只能用于审计，不能直接成为恢复授权的权威来源：节点代码能够追加
transcript，因此仅回放 `capability_grant_answered` 会把节点可写数据错误提升为权限授予。

## 可执行反例

测试文件：
`nodes/experiment/tests/test_060_authorization_recovery_handoff.py`

运行命令：

```bash
uv run pytest -q nodes/experiment/tests/test_060_authorization_recovery_handoff.py -rxX
```

当前结果：

```text
.x...                                                                    [100%]
4 passed, 1 xfailed in 0.36s
```

主会话独立复跑为 `4 passed / 1 xfailed`（0.33 秒）；再以 `--runxfail`
运行，恰好得到 `1 failed / 4 passed`（0.35 秒）。唯一失败是专用的
`SameRunAuthorizationRecoveryGuidanceGap`，不是导入、环境或其它断言失败。

严格 xfail 用真实 `State`、真实 request/grant API 及真实 Experiment preflight 判定构造：

- live `State`：请求 `gitlab.com`，回答允许，exact-host preflight 成功；
- 同一 run reopen：Core grant 消失；
- 持久请求历史仍在；
- preflight 返回 `network_access_request_unresolved`，不提供重新请求参数，只建议
  `report_blocker`。

xfail 的断言没有把某一种修法写死：修复后可以恢复该 run 的权威 exact-host grant；也
可以继续 fail closed，但应在结构化结果中建议重新走 HITL。当前“未授权且只建议
report blocker”才是被锁定的缺陷；它不声称 request 工具不可调用。修复后应让该测试
XPASS，并移除或重新分类 strict xfail。

四个通过的正对照确认：

- live `State` 上批准只放行 exact host；
- reopen 后显式再次调用真实 `request_network_access` 仍会 pause，再次批准后可恢复放行；
- 明确拒绝在 reopen 后不会变成授权；
- host A 的请求历史不会阻止 host B 发起自己的授权请求。

整个测试只走授权和 preflight，不调用 fetch、subprocess 或任何网络客户端。

## 期望契约矩阵

| 决定与恢复场景 | 最小安全结果 |
| --- | --- |
| 已批准，仍使用 live `State` | exact host 放行 |
| 已批准，同一 run reopen | 恢复权威 grant，或 fail closed 后允许重新请求 |
| 已拒绝，同一 run reopen | 不得自动授权；拒绝决定应可区分于丢失的批准 |
| host A 有请求/决定，访问 host B | 独立判定；不得继承 A，B 可按策略请求 |
| 新 run | 不得继承旧 run grant |
| 子域、重定向目标或其他 host | 不得由父域/exact-host grant 自动覆盖 |

“同一 run 的恢复”不等于把批准升级为部署级或跨 run 白名单。恢复的数据仍须绑定 run
identity、规范化 exact host 和用户决定，并保持现有 fail-closed 边界。

## Owner 与最小接口建议

### Core 边界：只有 Core 可以定义可信恢复

Core 现行契约允许 grant 随 live state 消失；若未来选择支持同一 run 恢复，其 owner
覆盖 capability grant、pause settlement 与 `State` recovery 协议，涉及：

- `core/capability_grants.py`
- `core/pause_driver.py`
- `core/state.py`

以下只是可选 Core 扩展的两个最小示例，并非穷尽方案或强制二选一；具体接口由 Core
owner 决定，但必须由 Core 控制，不能由 Experiment 代做：

1. **可信恢复**：在 Core-owned、节点不可伪造的 run checkpoint 中保存
   `{run_identity, normalized_exact_host, decision}`，并由 Core 在 reopen 后恢复或查询。
   不从普通 transcript 推导授权，不写环境变量，不扩为 wildcard、父域或跨 run grant。
2. **可信决定状态**：如果 Core 有意不恢复批准，可提供权威 decision-state API，至少
   区分 `denied`、`unanswered` 与 `approved_but_not_restorable`；最后一种保持 fail closed，
   但允许产生新的 capability-grant pause。

### Experiment owner：不得因历史请求误导为只剩 blocker

Experiment owner 是
`nodes/experiment/tools/resource_fetch.py::_network_access_was_requested` 与
`_require_egress_access`。最小可行修复不需要也不得自行授予网络权限：当 Core 当前明确
返回“未授权”时，Experiment 仍 fail closed；但不能仅因旧 tool-call 存在就在结构化结果
中移除 `request_network_access` 建议并断言下一步只能 `report_blocker`。它至少应区分
当前尚未 settlement 的请求与已经 settlement 后丢失的 live grant。

在 Core 尚无权威 decision-state API 时，一个有界的节点侧恢复办法是：仅把历史
`capability_grant_answered(granted=True)` 当作“允许重新弹出 HITL”的线索，绝不把它
当作 grant；新 pause 的回答仍须通过 Core `grant_from_answer` 才能放行。历史
`granted=False` 继续 fail closed，无回答的 pending 请求继续避免重复弹卡。即使节点伪造
该审计线索，也只能请求用户再次决定，不能产生授权。这个兼容逻辑应在 Core 提供可信
decision state 后移除，届时 Experiment 只消费 Core 契约。

这只是待验证的设计备选，不是本包已实现或批准的修法；还必须覆盖“批准回写未生效”
不能每轮重复弹卡，以及明确拒绝不得被劝说式循环覆盖。不能为了修恢复指引又引入新的
审批空转，亦不能把未受信审计记录当成人的新答复。

当前最小修复 owner 是 Experiment：停止以粗粒度“请求过”生成 blocker-only 恢复建议，
在 grant 失效后引导重新走现有 HITL。Core 恢复或决定状态接口只是可选增强，且必须保持
Core 控制。两层都不应把普通 transcript 升级为授权 authority。

## Issue 交接口径

**标题**：After same-run `State.reopen`, Experiment preflight recommends blocking
instead of the still-available HITL reauthorization

**问题摘要**：按既定安全契约，exact-host 出网批准只存在于 live `State`，不落盘也不
进入环境。`State.reopen` 后批准失效；随后 Experiment 仅凭持久请求历史从 preflight
结果中省略现有 HITL 建议，并只建议 `report_blocker`。显式调用授权工具仍能恢复，因此
问题是默认恢复指引错误，不是系统没有合法出口。修复不得从节点可写 transcript 恢复许可。

**建议验收**：

- 上表六种场景都有 Core/consumer 集成覆盖；
- 不信任节点可写 transcript 作为授权 authority；
- reopen 后批准可安全恢复，或用户可再次授权；
- 明确拒绝、其他 host、其他 run 仍 fail closed；
- 不扩大 exact-host 范围，不声明实际网络连通性。
- 不重开已关闭的 #1068；在 #770 或单独的 recovery issue 中按上述边界跟踪。

主会话已核对历史讨论并按用户授权补充到既有问题，没有另建重复 issue，也没有改变状态：

- [Experiment #1069，恢复引导缺陷](https://desktop-9el2944.taile9f15e.ts.net/wangd/harness-framework/issues/1069#issuecomment-5660)。
- [Core #770，可选可信恢复接口边界](https://desktop-9el2944.taile9f15e.ts.net/wangd/harness-framework/issues/770#issuecomment-5661)，通知 wangd；没有要求从节点日志恢复许可。

证据已本地提交为 `2bdec9a8ebc93b7a3b8bcb6df8112ebb3b1d655f`；该提交不修改运行时，
不代表恢复指引已修复。产品分支未推送。

## 未验证范围

本包没有覆盖完整 agent loop/UI 的进程重启交互，也没有验证部署网络连通性、代理、DNS、
重定向或远端服务。它只复现 `State.reopen` 前后的授权状态与 Experiment preflight 组合，
没有实际出网。Core owner 的最终接口选择尚未确定；节点运行时修复尚未实现。
