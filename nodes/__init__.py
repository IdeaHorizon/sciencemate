"""nodes/ —— 每个研究节点一个子目录。

每个节点目录：
  harness.yaml         —— 节点契约（必填）
  tools/__init__.py    —— 节点专属工具（可选）
  skills/__init__.py   —— 节点专属 skill（可选）
  fixtures/*.yaml      —— 节点 fixture（可选）

orchestrator（`_orchestrator/`）是特殊节点：它是用户对话的入口，调度其它节点。
"""
