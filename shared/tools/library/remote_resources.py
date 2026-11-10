"""Agent-facing Project Resources and managed SSH operations."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from core.remote_resources import call
from core.tool_registry import ToolDefinition, register_tool


async def resource(state, action: str, **params: Any):
    if params.get("password"):
        from core.secrets import register_secret

        register_secret(params["password"])
    if action == "probe" and not params.get("resource_id"):
        return dict(status="error", error="probe requires resource_id")
    if action == "connect" and not params.get("host"):
        return dict(status="error", error="connect requires host")
    return await call(state, action, **params)


async def remote_exec(state, resource_id: str, command: str, **params: Any):
    return await call(state, "exec", resource_id=resource_id, command=command, **params)


async def remote_job(state, action: str, resource_id: str, **params: Any):
    if action in ("status", "cancel", "wait") and not params.get("job_id"):
        return dict(status="error", error="status/cancel/wait requires job_id")
    if action == "wait":
        from core.remote_resources import wait

        return await wait(
            state,
            resource_id,
            params["job_id"],
            poll_interval_s=params.get("poll_interval_s"),
            max_wait_s=params.get("max_wait_s"),
        )
    result = await call(
        state, "jobs" if action == "list" else action, resource_id=resource_id, **params
    )
    if action in ("submit", "status") and result.get("state") in ("launching", "running"):
        result["next_action"] = dict(
            tool="remote_job", action="wait", resource_id=resource_id, job_id=result["job_id"]
        )
    return result


async def remote_file(
    state, action: str, resource_id: str, remote_path: str, local_path: str, **params: Any
):
    from core.project_workspace import ProjectWorkspaceError, resolve_tool_path
    from core.sandbox import model_tool_roots
    from core.secrets import is_sensitive_path

    try:
        local = resolve_tool_path(state, local_path, write=action == "download").resolve()
        sensitive = is_sensitive_path(local)
        if sensitive:
            raise ValueError(sensitive)
        if action == "download":
            roots, readonly = model_tool_roots(state)
            from core.isolation._native import write_layers

            layers = write_layers(roots, readonly)

            if not layers.grants_write(local):
                raise ValueError("Download destination is outside this node writable scope")
        request = dict(resource_id=resource_id, path=remote_path, **params)
        if action == "upload":
            if local.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("File exceeds 8 MiB transfer limit")
            request["content_base64"] = base64.b64encode(local.read_bytes()).decode()
        result = await call(state, "put" if action == "upload" else "get", **request)
        if result["status"] != "success":
            return result
        if action == "download":
            content = base64.b64decode(result.pop("content_base64"), validate=True)
            if hashlib.sha256(content).hexdigest() != result["sha256"]:
                raise ValueError("Downloaded content failed SHA-256 verification")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(content)
        return dict(**result, local_path=str(local))
    except (ValueError, OSError, ProjectWorkspaceError) as exc:
        return dict(status="error", error=str(exc))


def register(name, description, properties, required, executor):
    register_tool(
        ToolDefinition(
            name=name,
            description=description,
            risk_level="medium",
            parameters_schema=dict(
                type="object", properties=properties, required=required, additionalProperties=False
            ),
        ),
        executor,
    )


S = {"type": "string"}
COMMAND = dict(
    resource_id=S,
    command=S,
    cwd=S,
    timeout={"type": "number", "exclusiveMinimum": 0, "maximum": 604800},
    environment={"type": "object", "additionalProperties": S},
)
register(
    "resource",
    "登记和使用项目计算资源。用户给 SSH 地址/账号/密码即调用 connect，"
    "成功后自动出现在 Resources；"
    "省略密码使用已有密钥/SSH 配置。list 查已登记资源，probe 刷新硬件现状。"
    "SSH 资源可由 remote_exec、remote_file、remote_job 直接使用；"
    "本机 shell 断网不代表服务器不可达。",
    dict(
        action={"type": "string", "enum": ["connect", "list", "probe"]},
        resource_id=S,
        host=S,
        user_name=S,
        port={"type": "integer", "minimum": 1, "maximum": 65535},
        password=S,
        jump=S,
        name=S,
    ),
    ["action"],
    resource,
)
register(
    "remote_exec",
    "在已登记 SSH 资源上按远程账户权限执行同步命令，支持查看文件、安装依赖、编译和环境操作。"
    "cwd 默认项目专属远程目录。timeout 最多 300 秒；长任务使用 remote_job submit，"
    "以便断线恢复和取消。返回真实退出状态及有界日志；执行回执本身不是科学结论。",
    {**COMMAND, "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 300}},
    ["resource_id", "command"],
    remote_exec,
)
register(
    "remote_job",
    "受管 SSH 长任务：submit 启动后立即返回，可先提交多个独立任务；status 查询（含日志），"
    "wait 将指定 job_id 的等待交给 Harness，终态后自动继续，不耗模型轮次；"
    "等待已有任务用 wait，不要用 shell sleep/进程名轮询。list 列出持久任务，cancel 终止进程组。"
    "submit 必须给稳定 idempotency_key，连接失败后复用同一个键或查询返回的 job_id，避免重复启动。"
    "任务由远端监督器守护，平台重启和 SSH 断线后仍可跟踪。timeout 为强制墙钟上限，默认一天。"
    "运行记录 analysis_eligible=false；正式研究证据仍须走节点的审计与交付流程。",
    {
        **COMMAND,
        "action": {"type": "string", "enum": ["submit", "status", "wait", "cancel", "list"]},
        "poll_interval_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
        "max_wait_s": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": (
                "Optional observation budget. Omit to wait until terminal or diagnostic state."
            ),
        },
        "idempotency_key": S,
        "input_manifest_sha256": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$",
            "description": (
                "Optional on submit. SHA-256 of the immutable input manifest declared for this job."
            ),
        },
        "job_id": {
            "type": "string",
            "pattern": "^[a-f0-9]{32}$",
            "description": "Persistent SSH job ID returned by the remote service.",
        },
    },
    ["action", "resource_id"],
    remote_job,
)
register(
    "remote_file",
    "在项目工作区和 SSH 资源之间传输文件（每文件最多 8 MiB），验证 SHA-256。"
    "upload/download；本机下载仍遵守节点写入边界。"
    "remote_path 相对远程项目目录，或指定远程账户可访问的绝对路径。",
    dict(
        action={"type": "string", "enum": ["upload", "download"]},
        resource_id=S,
        remote_path=S,
        local_path=S,
        cwd=S,
    ),
    ["action", "resource_id", "remote_path", "local_path"],
    remote_file,
)
