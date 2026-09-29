"""把 MCP server 暴露的工具注册进 tool_registry。

入口：`load_mcp_servers(config_path)`。读取一份 YAML 配置，启动每个 server，
列出它们的工具，按 `{prefix}__{tool_name}` 注册进 tool_registry —— 之后
agent loop 就能和原生工具一样调用它们。

YAML 配置示例（见 mcp_servers.yaml.example）：

    servers:
      - name: fetch
        prefix: fetch
        command: ["npx", "-y", "@modelcontextprotocol/server-fetch"]
        env:
          # 可选环境变量
      - name: filesystem
        prefix: fs
        command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/path"]

owner 在 harness yaml 的 `tools:` 列表里直接写带 prefix 的工具名即可，
例如 `fetch__fetch` 或 `fs__read_file`。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from core.mcp_client import MCPClient
from core.state import State
from core.tool_registry import ToolDefinition, register_tool

log = logging.getLogger("mcp_loader")


async def load_mcp_servers(config_path: Path) -> list[MCPClient]:
    """读取 YAML 配置，启动所有 MCP server，把它们的工具注册进 registry。

    返回 启动了的 MCPClient 列表 —— 调用方应在程序结束时对每个 .stop()。
    """
    if not config_path.exists():
        log.info("没有 MCP 配置文件（%s），跳过 MCP。", config_path)
        return []

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    server_cfgs = cfg.get("servers") or []
    clients: list[MCPClient] = []

    for sc in server_cfgs:
        name = sc.get("name") or "mcp"
        prefix = sc.get("prefix") or name
        command = sc.get("command") or []
        env = sc.get("env") or {}

        if not command:
            log.warning("MCP server %r 没配置 command，跳过。", name)
            continue

        client = MCPClient(command=command, env=env, name=name)
        try:
            await client.start()
        except Exception as e:
            log.error("启动 MCP server %r 失败：%s", name, e)
            continue

        try:
            tools = await client.list_tools()
        except Exception as e:
            log.error("列出 MCP server %r 工具失败：%s", name, e)
            await client.stop()
            continue

        for t in tools:
            tool_name = f"{prefix}__{t.name}"
            register_tool(
                ToolDefinition(
                    name=tool_name,
                    description=(
                        f"[MCP:{name}] {t.description}"
                        if t.description else f"[MCP:{name}] tool {t.name}"
                    ),
                    parameters_schema=t.input_schema,
                    allowed_node_types=None,    # MCP 工具默认任何节点可用；
                                                # 实际是否调用由 harness yaml 的 tools: 白名单决定
                    risk_level=sc.get("risk_level", "medium"),
                ),
                _make_executor(client, t.name),
            )
        log.info("MCP server %r 注册了 %d 个工具（前缀 %r）。", name, len(tools), prefix)
        clients.append(client)

    return clients


async def stop_mcp_servers(clients: list[MCPClient]) -> None:
    """干净地停掉所有 MCP server 子进程。"""
    for c in clients:
        try:
            await c.stop()
        except Exception as e:
            log.warning("停止 MCP server %r 时出错：%s", c.name, e)


def _make_executor(client: MCPClient, mcp_tool_name: str):
    """闭包：把 framework 调度的 (state, **args) 转成 MCP 的 call_tool。"""
    async def _executor(state: State, **kwargs: Any) -> dict:
        # 框架注入的 _project_id / _node_id 等下划线开头的不传给 MCP server
        clean = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        return await client.call_tool(mcp_tool_name, clean)
    return _executor
