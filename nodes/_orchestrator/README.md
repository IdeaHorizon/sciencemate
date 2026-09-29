# _orchestrator 节点 ★ Framework 基础设施

**Owner**: wangd

跟 user 长期对话的主 agent；判断 user 意图 + 用 `run_node` 调起合适子节点 + 协调 pipeline。**同事不该改这个节点**。

## 性质

下划线 `_` 前缀 = framework 基础设施。跟 `_curator` 一样不开放给同事 owner。

## 跟 chat.py 关系

- `chat.py` REPL 是 user 入口，每个用户消息会过 orchestrator
- orchestrator 长期对话状态在 `~/.harness-framework/runs/orchestrator__<project_id>/conversation.json`
- 跑得动 `run_node` / `query_project_status` / propose 处理 / memory wet-ledger 维护

## 不开放给同事的原因

- pipeline 路由策略改 = 全 framework 行为改
- callable_nodes 白名单是 orchestrator 的特权（可调任意节点）
- profile_update / project_update 走 orchestrator —— 涉及 user 长期偏好

## 同事如何"曲线影响" orchestrator

- 同事节点 / orchestrator 写 `memory_write(section='law', text)` → 即时进 `memory/directives.md`，下轮全节点 system_prompt 注入
- 同事节点 PR 上线后 orchestrator 自动可调（已注册到 callable_nodes）
- 同事可写 `propose(...)` 进 inbox → orchestrator 触发审批流

## 给 wangd（自己）的备忘

orchestrator 维护点：
- chat.py 整合点（conversation persistence / pause-resume / `/status` 等命令）
- harness.yaml 工具白名单含 run_node / propose / profile_update / memory ops
- pause/resume cascade（多级 sub-run 暂停传播）
- 跟 _curator 的协调（producing 节点跑完 → 调 curator 整合）
