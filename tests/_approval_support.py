"""Exercise a low-level owner's real permission result through Core's dispatcher.

This adapter is only for tests that call an owner function directly. It grants
no command-text pass and does not bypass any gate or execution boundary.
"""

import hashlib

from core import approvals, tool_registry


async def prepare_result(state, result, executor, arguments):
    name = (
        "approval_owner_"
        + hashlib.sha256(f"{executor.__module__}:{executor.__qualname__}".encode()).hexdigest()[:16]
    )

    async def execute(*, state, **values):
        return await executor(state, **values)

    tool_registry.register_tool(
        tool_registry.ToolDefinition(
            name=name,
            description="Approval owner regression adapter",
            parameters_schema={"type": "object", "properties": {key: {} for key in arguments}},
        ),
        execute,
    )
    with approvals.invocation(state, name, arguments):
        normalized = await approvals.capture(state, result)
    return normalized["pause_event"]


async def resume_result(state, result, executor, arguments, *, choice="approve"):
    payload = await prepare_result(state, result, executor, arguments)
    record = approvals.decide(state, payload, choice)
    return await approvals.run_approved(state, record["operation_id"])
