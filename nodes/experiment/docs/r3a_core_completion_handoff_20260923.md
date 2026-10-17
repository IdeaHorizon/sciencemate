# R3-A → Core completion admission 交接（2026-09-23）

## 当前复核注记（2026-09-24，86be1094）

只读复核基线：`lujy/impl-experiment-v3 @ 86be10944f6b92c6c1cbdd881782f0c16101bdd4`。
下文历史实测与基线原样保留，本注记不是新一轮全量或 UI 验收，也未修改 Core。

- 现入口为 `tools/contract_audit.py:4765` 的 `audit_experiment_delivery(state)`；
  它仍只读校验 manifest，并调用 `tools/delivery_transaction.py:716` 的 `_sealed`。
  `validate_manifest` 与 `_head_identity`（559）共同核对精确三件、内容/元数据摘要、
  本 run producer、当前 frozen head；`_sealed` 再核对同 closure 唯一且匹配的 finish commit。
- 当前 `core/executor.py:1392-1452` 仍只做产出/缺项、blocker 和 task-outcome floor；
  没有消费上述 C audit。Project 模式的 type-presence 跳过仍在，不能据接口测试通过声称已接线。
- 消费端必须区分 `applicable=true, status=sealed, passed=true` 与
  `passed=true, applicable=false, status=not_applicable`；后者不是 sealed 证明。
  即使 sealed，`passed` 也不等于业务 success，必须单独传递审计返回的 `outcome`。
- 下文 provider/admission 和 `fact_revision` 仍是待 owner 接线的接口建议，不是现成 API。
  audit 失败结果尚不统一暴露完整 read-set；若 Core 需要无进展判定，Experiment 配合补只读
  revision，不新增账本、不调用 `settle_delivery`，也不恢复旧 closure hook。

该缺口应独立由 Core owner 推进，不阻塞已有授权范围内的节点方法与专业能力开发。

> Issue owner：wangd / Core
> Experiment owner：lujy
> C 事务与 Core 引用代码基线：`codex/060-r3a-c`，HEAD
> `a80703eed53e7f0473f5fb8ef6e4e13a670c47de`。取证时共享 worktree 另有未提交的
> A/task-authority 接线；本稿不声称工作树等于 clean HEAD，也不把那些改动算进 C/Core
> completion 结论。
> 已创建并分配给 wangd：[Core issue #1141](https://desktop-9el2944.taile9f15e.ts.net/wangd/harness-framework/issues/1141)。
> 本交接没有修改 Core/runtime；接口实现与对应 root 回归由 Core owner 落地。

## 结论

当前 Core **可以把未 sealed 的 Experiment v3 交付写成 `status=completed`**。

这不是 `finish` 事务本身缺校验：节点已有只读的
`audit_experiment_delivery(state)`，它会核对持久 manifest、三件精确 frozen head、
以及同一 closure 恰好一条匹配的 `finish` 记录。缺口是这份结果没有接到 Core 的
统一终止/完成判定上。

两个概念必须分开：

1. **交付闭环**：C 是否 sealed；决定本次 run 能否结束。
2. **任务结局**：sealed delivery 的 `outcome` 是
   `success | partial | failed | blocked | cancelled`；诚实的 `failed` / `blocked`
   是“交付已完整形成”，但绝不是“任务成功”。

因此，不能把 `audit.passed=True` 直接翻译成任务 success，也不能把 sealed 的
`failed` / `blocked` 再判为 C 不完整。

## 2026-09-24 增补：零工具调用也会被自动 manifest 判成 completed

在 `831a610c` 的真实父派发接口验证中发现，并由主会话独立读取原始 summary
与父 transcript 确认。路径保留注册的 `task`/`run_node`、executor、agent loop、
Experiment bootstrap/hooks 与 finalize；只把外部 `LLMClient.chat` 换成有界固定响应。
这不是自然模型/UI 实测，但也不是直接调用或 mock `finalize_run` 的合成判定。

子节点模型仅返回结束文本，没有工具调用、实际计算、三件套或 `finish` 提交。
`repro_snapshot` 的 `_repro_snapshot_on_turn_start`/`_on_end` 自动保存 run manifest，
在 Project 工作区形成文件变化。实际 summary 的关键原值为：

```json
{
  "status": "completed",
  "tool_call_count": 0,
  "node_task_outcome": null,
  "produced_artifact_types": ["run_manifest"],
  "missing_required_outputs": [],
  "blockers": [],
  "delivery_empty": false
}
```

父 transcript 随后依次记录：

1. `subagent_call_end(child_status="completed")`；
2. `post_node_review_skipped_no_artifacts`；
3. `decision_deferred_to_analysis(recommended_action="proceed")`。

没有可审产物时实际跳过了 reviewer，不能把这段说成 reviewer 已核验通过，
也不能仅凭 deferred 事件声称下游已完成科学分析。错误已经在 child 完成投影形成：
`core.executor.finalize_run` 的 Project 模式跳过 required-type 存在性检查；
没有 blocker、任务 outcome 降级等条件时，仅以节点作用域对 Git HEAD 有变化
作为交付地板。节点自动写出的诊断/provenance 文件恰好满足这个粗粒度条件。
因此这里不是 finish 事务放过假结果，而是整个 finish/C 审计被绕过。

不应删除有用的 repro 记录来让测试变红，不应按 `run_manifest` 文件名加例外，
也不在节点恢复旧 route/closure 门。沿用 #1141 的共同修复方向：Core 在接受任何
候选结束前消费节点的只读交付判定，把“有文件变化”与“已形成诚实交付”分开。
此前“手写 note 导致假完成”的反例，现在有了无需模型写文件的自然调用链形态。

最初合同派发正例用明确 `report_blocker` 收尾，只验证 v1/v2 精确绑定和不隐式重做。
`077b91be` 已另补同一真实父派发链上的本地执行、统一 finish、三件精确冻结与审计，
两版结果为 49 / 64；父侧 Reviewer 因夹具白名单受阻，不代表 Reviewer / Analysis
已运行。这些正例不消除本反例，不能据此声称 Core 缺口修复或 060 已端到端就绪。
本轮尚未将增补发到远端 issue 评论：当前没有可用的已认证 issue 接口；不重复开 issue。

可重复用例在 `test_060_parent_contract_dispatch.py`。默认运行的负例是
`xfail(strict=True, raises=KnownManifestOnlyCompletionGap)`：只有精确身份、零动作、
无三件套、仅 manifest、假 completed 及后置流程推进均实际出现，才抛该专用异常。
身份、环境或其他失败不能被这层 marker 吞掉；`--runxfail` 可直接展示预期红格。

**验收范围更正（独立复核 93 号）**：这条负例是当前缺陷哨兵，不是所有 Core
修法的完整验收。其固定模型只允许一次响应，并要求零工具、无三件；若 Core 正确
选择 reenter、suspend/resume，或续跑后形成诚实交付，夹具本身也可能先报普通失败。
因此不能说“任何正确修复都会 XPASS”，也不能仅为去掉红灯而放宽这些断言。
Core owner 落地时须另验证各终止入口、合法重入与恢复、诚实非成功交付、无重复
副作用及阻止错误后流程，再按已证明的新契约替换或撤除这个缺陷哨兵。

## 本次定向实测

本次用真实 `State`、v3 bootstrap/ledger、`delivery_transaction`、
`audit_experiment_delivery`、实际 Experiment harness 和 Core `finalize_run` 做了一个
可执行回归；没有 mock `finalize_run` 的判定。结果如下：

| 场景 | delivery audit | Core summary | 结论 |
|---|---|---|---|
| 精确 sealed 的 success delivery | `passed=true`, `status=sealed` | `status=completed` | 正对照 |
| 三件已全部冻结，在写 `finish` 前注入中断 | `passed=false`, `closure_manifest_not_committed` | `status=completed`, `missing_required_outputs=[]` | **假 completed** |
| 正常 seal 后让 `raw_results` 当前 frozen head 漂移 | `passed=false`, `closure_head_mismatch` | `status=completed`, `missing_required_outputs=[]` | **假 completed** |
| project-workspace 模式三件全缺，但 `experiments/` 有一处真实 Git 工作区改动 | `passed=false`, `closure_manifest_missing` | `status=completed`, `missing_required_outputs=[]` | **假 completed**；该模式明确跳过 required outputs |

前两个 transaction 反例使用的 delivery `outcome=failed`，但 summary 中
`node_task_outcome=null`，所以现有 `status_floor` 没有降级依据。

最短可复现命令是：

```bash
uv run pytest -q nodes/experiment/tests/test_060_core_completion_handoff.py
# 1 passed, 3 xfailed in 0.57s
```

其中 sealed success 正对照普通通过；三个反例均为 `pytest.xfail(strict=True)`，并断言
期望结果是“不 completed”。当前缺口表现为 XFAIL；若修复在这些直接 finalize
夹具的既定控制流中满足该断言，会成为 XPASS(strict)，提醒 owner 审核并删除 marker。
这不证明完整的 reenter/suspend/resume 接线；新终止契约须有相应的独立回归。
project-workspace 夹具复用 Core 的 `ensure_project_worktree`，没有新增裸
`subprocess` / Git 调用点，因此不产生新的共享调用点登记债。

此外，节点侧新增的 adversarial readback 回归已定向运行：

```bash
uv run pytest -q nodes/experiment/tests/test_060_delivery_adversarial_readback.py
# 8 passed in 0.61s
```

它证明现有只读 audit 能拒绝：任一三件信封 digest 缺失/类型错误、三件一致伪造
success 但不匹配 manifest，以及 blocked 部分冻结恢复时遗漏 blocker 参数；这些测试
不覆盖 Core completion 接线。

### 没有声称实测的部分

- 没有跑完整测试集或真实 LLM/harness E2E。
- 没有实测未来的 `reenter` / `suspend` / resume 接口；下文是 owner 接口建议。
- 没有实测平台 ingestion/UI 如何展示新 decision；只核实到 Core summary/run_end 边界。

## 当前调用链（源码核实）

### 1. v3 harness 没有启用旧 contract audit

- `nodes/experiment/harness.yaml:120-144`：模型面的唯一收尾工具是 `finish`；v3
  `loop_hooks` 中没有 `experiment_contract_audit`。
- `nodes/experiment/harness.yaml:125-128`：启用的是 bootstrap、delta view、ledger
  recorder 和 `experiment_live_effect_unsettled`。
- `nodes/experiment/tools/ledger_hooks.py:184-213`：
  `experiment_live_effect_unsettled` 只追加事实；docstring 明确“只记录，不是终止门”。
- `nodes/experiment/docs/experiment_governing_v3.md:157-168`：旧
  `experiment_contract_audit`、`external_job_handoff` 等是有意移出 v3 harness 的，
  不能把整套旧 closure 状态机重新挂回去。

### 2. Core 先接受终止，再运行 observe-only on_end

- `core/agent_loop.py:300-324,482-489`：实际 hook 集来自 always-on + harness 名单。
- `core/agent_loop.py:1270-1281`：模型无 tool call 时直接构造
  `LoopResult(status="completed", terminal_cause="model_finished")`。
- `core/agent_loop.py:345-359,1613-1632`：`turn_cap` 在兼容 status 里同样投影成
  `completed`。
- `core/agent_loop.py:1634-1640`：所有这些结果形成后才执行 `run_on_end`。
- `core/loop_hooks.py:182-189`：任一 on_end 异常只 warning 并继续，是 fail-open 的
  观察面，不是完成 admission。

因此，把 C audit 只接成新的 on_end hook 既太晚，也无法覆盖所有终止路径，更无法
安全表达失败时的 reenter/suspend。

### 3. finalize 只看 type presence / blocker / task-outcome floor

- `core/executor.py:1392-1425`：`produced_types` 只来自 artifact type；不读 manifest、
  frozen identity、信封 digest 或 `finish` 记录。
- `core/executor.py:1415-1425`：project-workspace 模式把 `missing` 无条件置空；
  run-local 才按 `required_outputs_for(...)` 做 type presence。
- `core/executor.py:1439-1452`：没有 missing/blocker 就先判 `completed`；只有已经写入
  `node_task_outcome` 时 `status_floor` 才能把非 success 降级。
- `core/executor.py:1463-1475`：project-workspace 的额外地板只问“是否有任何工作区
  改动”，不问 ExperimentDeliveryV1 是否存在或 sealed。
- `core/executor.py:1570-1650`：上述值直接进入 summary 与 `run_end`。

`nodes/experiment/harness.yaml:155-163` 声明 raw/clean/log 三件，只能提供旧的存在性
地板；它不是 C transaction seal，也不能代替 audit。

### 4. 节点已经有足够强、且只读的 C audit

- `nodes/experiment/tools/contract_audit.py:4679-4687`：audit 声明不 repair、不 settle，
  persisted manifest 是唯一 C plan。
- `nodes/experiment/tools/contract_audit.py:4696-4724`：缺 manifest、未 prepare、未全冻、
  缺 commit 均 fail closed。
- `nodes/experiment/tools/contract_audit.py:4726-4738`：ledger 尾损坏拒绝；最终复用
  `_sealed` 做精确只读断言。
- `nodes/experiment/tools/delivery_transaction.py:711-755`：`_sealed` 要求三件精确 frozen
  identity、恰好一条同 closure `finish`、commit 内容与 manifest 完全一致。
- `nodes/experiment/tools/contract_audit.py:4762-4775`：通过时已经返回 `closure_id`、
  `delivery_digest`、`outcome`、三件 identity 和 `finish_seq`，足以作为 completion
  receipt。

### 5. 不能通过“重新启用旧 hook”解决

有两个独立原因：

1. `audit_experiment_contract` 虽在
   `nodes/experiment/tools/contract_audit.py:4779-4807` 返回
   `experiment_delivery`，但 `TERMINAL_CLOSURE_REGISTRY`
   (`contract_audit.py:109-128`) 没登记该 key；旧 hook 在
   `nodes/experiment/hooks.py:3798-3800` 先做 `terminal_closure_projection`，而 projection
   (`contract_audit.py:131-160`) 只遍历 registry。也就是说，旧 scientific projection
   当前会**漏掉 delivery audit**。
2. 旧 operation 分支在 `nodes/experiment/hooks.py:3715-3751` 把任何诚实的非 success
   outcome 写成 closure blocker。R3-A 的语义相反：`failed` / `blocked` 可以是完整、
   sealed 的 C，只是任务结局不是 success。

旧 `_block_experiment_completion` 自己也说明了现状：
`nodes/experiment/hooks.py:174-225` 必须写 `hook_state["blockers"]`，因为
`finalize_run` 不把 on_end 修改的 `loop_result.status` 当 status authority。将它复活会
重新引入旧状态机与错误的 outcome 压平。

## 建议的最小 Core 接口

沿用 governing v3 已写明的 Core choke point，不让 Core import
`nodes.experiment.*`，由 harness 注册 provider：

```python
on_completion_admission(
    ctx,
    proposed_result,
) -> CompletionDecision(
    disposition="allow" | "reenter" | "suspend",
    code=str,
    fact_revision=str,
    facts=dict,
    notice=str | None,
    task_outcome="success" | "partial" | "failed" | "blocked" | "cancelled" | None,
)
```

Core owner 负责：

1. 让 `model_finished`、`turn_cap`、protocol/progress breaker、hook terminal 等所有候选
   终止在写 `loop_finished`、summary、`run_end`、注销 active run **之前**经过同一个
   admission choke point。显式 user cancel 与既有 HITL pause 保持自己的语义。
2. provider 抛错时 fail closed 为 `suspend`，不能 warning 后继续 completed。
3. `reenter` 每次重新求值，不使用 `_finish_gate_used` 这类一次性布尔；只按
   `(code, fact_revision)` 做有界“同一事实无进展”计数，重复不变后转 `suspend`。
4. `suspend` 先 checkpoint，不写 final summary/run_end、不注销 active run；由非
   tool-call 的 resume 入口继续同一个 State。
5. 在 `allow` 时把 typed `task_outcome` 交给现有 `core.task_outcome` / `status_floor`
   这条通用轴。不要从 `audit.passed` 猜 success。

Experiment provider 只做只读投影：

```python
audit = audit_experiment_delivery(state)
if not audit["passed"]:
    # 尚未形成 C：模型仍可恢复时 reenter；不能安全继续时 suspend。
    disposition = "reenter" if ctx.can_reenter else "suspend"
    return CompletionDecision(
        disposition=disposition,
        code=audit.get("error_code", "delivery_audit_failed"),
        fact_revision=...,
        facts=audit,
        task_outcome=None,
    )

# C 已完整形成；outcome 只回答任务结局。
return CompletionDecision(
    disposition="allow",
    code="experiment_delivery_sealed",
    fact_revision=...,
    facts=audit,
    task_outcome=audit["outcome"],
)
```

### outcome 的必须语义

| delivery audit | delivery outcome | admission | closure/run status 投影 | task outcome |
|---|---|---|---|---|
| fail/error | 不可信/不可读 | `reenter` 或 `suspend` | 不得 completed | 不推断 |
| pass | `success` | `allow` | completed（若无其它 Core gate） | success |
| pass | `partial` / `failed` / `cancelled` | `allow` | 由现有 floor 降为 incomplete | 原值 |
| pass | `blocked` | `allow` | 由现有 floor 投影为 blocked | blocked |

特别注意：sealed `blocked` 的 admission 仍是 `allow`；`blocked` 是任务结局，不是
“C 还没写完”。否则模型会被要求无限重做一个已经完整、诚实的 blocked delivery。

### fact_revision

通过时可由现有返回值稳定计算：

```text
sha256(closure_id, delivery_digest, finish_seq, artifact_identities)
```

失败时不能只哈希错误文案；至少要覆盖 manifest 当前摘要/阶段、ledger tail identity、
三件当前 head identity 和 `error_code`。当前 audit 的部分失败结果未暴露完整 read-set；
若需要，由 Experiment owner 给 audit 增加**只读** `fact_revision`，不新增状态、不调用
`settle_delivery`。

## 最短回归与验收条件

节点 audit 的现成定向回归：

```bash
uv run pytest -q \
  nodes/experiment/tests/test_060_core_completion_handoff.py \
  nodes/experiment/tests/test_060_r3a_delivery_audit_gate.py \
  nodes/experiment/tests/test_060_delivery_adversarial_readback.py
```

Core owner 应在 Core-owned `tests/**` 新增 completion-admission 回归，至少覆盖：

1. post-freeze / pre-`finish` 中断：audit 为
   `closure_manifest_not_committed`，不得写 completed/run_end。
2. seal 后任一三件 head、metadata digest 或正文身份漂移：不得 completed。
3. project-workspace 有其它真实改动但 manifest/三件缺失：不得 completed。
4. 完整 sealed success：允许 completed，`node_task_outcome=success`。
5. 完整 sealed failed：允许结束，closure 是 complete；
   `node_task_outcome=failed`，兼容 `status` 不得 completed，且不伪造 closure blocker。
6. 完整 sealed blocked：允许结束，closure 是 complete；
   `node_task_outcome=blocked`，不能被 admission 反复 reenter。
7. provider 抛异常：fail closed 为 suspended，不能生成终态 summary/run_end。
8. `model_finished`、turn cap、protocol/progress breaker 都命中同一 admission；不能只测
   正常 final text。

缺 `finish` 的最短测试可直接复用
`nodes/experiment/tests/test_060_r3a_delivery_audit_gate.py:16-65` 的 `_state/_manifest`
夹具：把 `Ledger.append_once("finish", ...)` 注入为异常，`settle_delivery` 会停在三件
全冻、manifest commit 为空的真实中断点；随后先断言 audit fail，再走 Core 终止入口。

## Owner 边界

- **Core / wangd**：统一 completion admission 生命周期、所有终止路径接线、
  reenter/suspend/no-progress、summary/run_end/active-run 顺序，以及 typed task-outcome
  bridge；回归写在 Core-owned `tests/**`。
- **Experiment / lujy**：维持 `audit_experiment_delivery` 的只读 C 语义；按 Core 接口
  提供 provider 和必要的只读 `fact_revision`；继续维护节点 transaction/audit 回归。
- **禁止的捷径**：Core 直接 import Experiment；Core 调 `settle_delivery` 自动修复；
  重挂旧 `experiment_contract_audit`；用 required-output type presence 代替 seal；把
  `audit.passed` 当任务 success。
