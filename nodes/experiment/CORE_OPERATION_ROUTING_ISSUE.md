> ⚠️ 状态（2026-08-30）：部分过时——`post_run_flow` 已权威化改造（`shared/tools/run_node.py:62-79`，服务节点声明 `none`），4 个 operation 会话实测 0 次起 reviewer；但「**完成态** operation 不进科学审查链」缺完成态实证，留待 Core 确认后关闭。

# Core issue: Experiment operation 的动态完成契约与后处理路由未接通

## 摘要

Experiment 已将请求区分为 `operation` 与 `scientific`：两者都交付经验证、冻结的 `raw_results`、`clean_results` 与 `experiment_log` 三件套；前者是非科学验证证据，后者才产生可进入科学审查链的证据。但 Core 仍根据 Experiment
Harness 的静态 `post_run_flow: review_curate` 登记后处理，未读取本次运行的 scope 或
record kind。因此，一个完成的 `pip install xlrd` 也会进入 reviewer -> decision package ->
Analysis/hypothesis。

这与“operation 应同步交付给直接调用者”的服务语义冲突，并产生不必要的 token 消耗与
延迟。

## 已有 Experiment 保障

1. `harness.yaml` 要求 `classify_experiment_scope`。operation 的闭环为：执行/安装 -> 最小验证 -> `record_operation_completion` 生成并冻结 `raw_results` -> `clean_results` -> `experiment_log` 三件套；禁止 scientific verdict/sediment 和自行启动审查节点。
2. `run_contract.py::_classify_experiment_scope` 将 scope 写入
   `state.hook_state["experiment_execution_scope"]`，同一 run 不得改道；调用方显式
   绑定 `prereg_artifact_id` 时拒绝 operation。
3. `hooks.py::experiment_contract_audit_on_end` 对 operation 只审计唯一、非自动生成、
   已冻结且 metadata 含 `record_kind: operation`、`execution_scope: operation` 的日志；
   不走 scientific closure audit。
4. 定向验证：`uv run pytest -q nodes/experiment/tests`，446 passed（2026-08-17）。

## Core/shared 证据

1. `core/loader.py` 只支持单一静态 `post_run_flow`（`full`、`review_curate`、
   `review_only`、`none`），没有按运行 profile/scope 解析的接口。
2. `core/loader.node_owes_post_node_flow` 仅按 node type 加载
   Harness；没有 state、summary、scope、record kind 或 caller 参数。
3. child 完成且交付后，`_finish_child` 据此静态判定登记
   `pending_post_node_flow`。Experiment 的 `review_curate` 因而对 operation 同样生效。
4. `required_outputs_by_mode` 不是完整解法：它可以按 `_request_mode` 改输出、预算和
   QC，但不改变 post-run flow。Experiment 现已在 scope 分类时同步写入该 mode，
   以便 mode 化完成契约在需要时读取；当前两种 mode 都要求三件套。
5. Workspace-First 下 Core 对 typed required-output 的 `missing` 固定为空；因此当前
   主路径中稳定存在的缺口是静态**后处理路由**，不是 operation 必然因缺
   `raw_results`/`clean_results` 被输出门阻断。

## 另一个必须同时修复的信任边界

`execute_node` 在 child state 上真实记录 `parent_run_id`，但 Experiment 当前的
`_invocation_context()` 只从 `node_inputs`/`hook_state` 读取 `caller_node_type`、
`caller_run_id` 等字段。也就是说，现有 `invocation.return_target_*` 是调用方提供的
**审计线索**，不是 Core 强绑定的、可用于路由授权的来源凭据。

因此不能直接采用“读取 artifact metadata 后回传给 metadata 里写的节点”的设计；这会
允许伪造或错误的 return target。Core 必须从实际 parent state/call stack 绑定 caller
identity，并只将该受控 identity 暴露给 Experiment 和完成路由。

## 最小复现

1. `_orchestrator` 通过 `run_node(node_type="experiment", ...)` 请求安装 xlrd 并验证
   import/version。
2. Experiment 分类为 `operation`，生成并冻结唯一、相互绑定的 operation 证据三件套，end audit 通过。
3. child summary 为 `completed`；`_finish_child` 仍以 node type `experiment` 调
   `core/loader.node_owes_post_node_flow`。
4. 静态 `review_curate` 为真，故创建 `pending_post_node_flow`；运行时继续 reviewer 与 decision package；仅在 review 有效时才顺延到 Analysis/hypothesis，
   而非把 receipt 作为同步结果交回直接调用者。

## 建议设计

Core 在运行结束时依据**受控 state**和 operation audit 派生 completion profile，并把它
写入 child summary：

```json
{
  "completion_profile": {
    "kind": "operation",
    "required_outputs": ["experiment_log", "clean_results", "raw_results"],
    "post_run_flow": "none",
    "return_to_bound_parent": true
  }
}
```

| profile | 通过条件 | Core 行为 |
|---|---|---|
| `operation` | scope=operation + operation 三件套审计通过 + 受控 parent identity | 不登记 post-node flow；同步回传 receipt 给直接父调用 |
| `scientific` | 现有 evidence/closure 审计通过 | 保持 `review_curate` |

安全要求：未分类、scope/audit 失败、无受控 parent identity、或直接顶层运行，必须
fail closed 为 `incomplete` 或默认 scientific flow；绝不能让模型自述或 artifact metadata
单独取消审查。

## 改动与验收

- `core/harness.py` / `core/loader.py`：增加运行级 flow/profile resolver，静态 YAML 保持
  默认 scientific 行为。
- `core/executor.py`：创建 child 时绑定真实 parent identity；final summary 导出受控
  completion profile。
- `shared/tools/run_node.py`：以 summary profile 决定 post-flow 登记、标准 reviewer
  可用性和同步回传，不能再只按 node type。

验收覆盖：operation package install、operation external job finalize、scientific primary
simulation、伪造 caller/scope、绑定 frozen prereg 后尝试 operation、legacy 与
Workspace-First 两种底座。
