"""极简 MCP（Model Context Protocol）stdio 客户端。

MCP 是 Anthropic 提出的标准协议：LLM agent 通过运行一个本地 server（子进程，
通过 stdin/stdout 收发 JSON-RPC 2.0 消息）来获取工具。社区已有大量现成的
server（filesystem / fetch / github / postgres / ...），直接拿来用即可。

为什么自己写而不是用 `mcp` 官方 SDK：
  - 这是教学脚手架，希望读者能读完代码理解 MCP 实际怎么跑
  - 不引入额外依赖
  - 协议本身很小：握手 + tools/list + tools/call，三个方法

参考：https://spec.modelcontextprotocol.io
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("mcp_client")


@dataclass
class MCPTool:
    """从 MCP server 拿到的一个工具定义。"""
    name: str
    description: str
    input_schema: dict     # OpenAI / JSON Schema 风格


@dataclass
class MCPClient:
    """对接一个 stdio MCP server 的最小客户端。

    生命周期：
        client = MCPClient(command=["npx", "-y", "@modelcontextprotocol/server-fetch"])
        await client.start()
        tools = await client.list_tools()
        result = await client.call_tool("fetch", {"url": "..."})
        await client.stop()
    """
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    name: str = "mcp-server"
    _proc: asyncio.subprocess.Process | None = None
    _next_id: int = 1
    _pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _reader_task: asyncio.Task | None = None

    async def start(self, timeout: float = 15.0) -> None:
        """启动子进程并完成 initialize 握手。"""
        merged_env = {**os.environ, **self.env}
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

        # 握手：客户端发送 initialize，server 回应 capabilities。
        init = await asyncio.wait_for(
            self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "harness-framework", "version": "0.1"},
            }),
            timeout=timeout,
        )
        log.info("MCP server %r initialized: %s", self.name, init.get("serverInfo", {}))

        # 通知服务端我们已准备好（按协议要求发送 notification，没有响应）。
        await self._notify("notifications/initialized", {})

    async def stop(self) -> None:
        """关闭子进程。"""
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        if self._reader_task:
            self._reader_task.cancel()

    async def list_tools(self) -> list[MCPTool]:
        """列出 server 暴露的工具。"""
        resp = await self._request("tools/list", {})
        return [
            MCPTool(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
            )
            for t in resp.get("tools", [])
        ]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """调用一个工具，返回结果。"""
        resp = await self._request("tools/call", {"name": name, "arguments": arguments})
        # MCP 返回 {"content": [{"type":"text","text":"..."}], "isError": false}。
        if resp.get("isError"):
            text = _flatten_content(resp.get("content"))
            return {"status": "error", "error": text or "MCP tool returned isError=true"}
        text = _flatten_content(resp.get("content"))
        return {"status": "success", "data": text}

    # ── 内部 ────────────────────────────────────────────────────────────────

    async def _request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("MCP client not started")
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        message = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _notify(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """持续读取 stdout，把 response 路由给等待的 future。"""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return                                  # server 关闭了
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                log.warning("MCP %r: 收到非 JSON 行：%r", self.name, line[:200])
                continue
            req_id = msg.get("id")
            if req_id is None:
                # 通知（无响应）—— 忽略，或在此扩展
                continue
            fut = self._pending.get(req_id)
            if fut is None or fut.done():
                continue
            if "error" in msg:
                fut.set_exception(RuntimeError(f"MCP error: {msg['error']}"))
            else:
                fut.set_result(msg.get("result") or {})


def _flatten_content(content: Any) -> str:
    """MCP 工具返回的 content 是一个 list of {type, text/...}。
    取所有 text 类型拼起来。"""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content if isinstance(content, list) else [content]:
        if isinstance(item, dict):
            t = item.get("type")
            if t == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        else:
            parts.append(str(item))
    return "\n".join(parts)
