"""工具注册表：按名字注册工具 + 调度执行。

## 工具接口规范（v2.1+）

一个工具 = （ToolDefinition + ToolExecutor）。

**ToolExecutor 接口契约**（强制，启动时校验）：

  ```python
  async def my_tool(*, state: State, arg1: T1, arg2: T2 = ..., **_: Any) -> dict:
      ...
      return {"status": "success", ...}  # 或 "error" / "pause"
  ```

  - **必须** `async def`
  - **必须** 第一个 keyword arg 是 `state`
  - **必须** 返回 `dict`
  - **必须** 在返回 dict 里包含 `status` 字段（success / error / pause）
      → 不写也行，框架自动包 `{"status": "success", "data": <原 dict>}` + warning
  - **必须** 接受 `**_: Any` 吸收未知参数（防 LLM 传多余字段炸 TypeError）
  - **不应该** 抛异常（registry 兜底；但工具不主动报 status=error 会丢上下文）

**ToolDefinition 字段**：

  - `name` 必须匹配 `^[a-zA-Z0-9_-]{1,64}$`（OpenAI function-calling 限制）
  - `parameters_schema` 必须是合法 JSON Schema object
  - executor 的 keyword 参数应该都在 parameters_schema.properties 里（warn on mismatch）

## 注册路径

  1. **节点专属 Python 工具**：在 `nodes/<my>/tools/<feature>.py` 里调
     `register_tool(...)`，然后在 `nodes/<my>/tools/__init__.py` import 触发。
     详见 `templates/tool.py.template`。

  2. **共享 Python 工具**：在 `shared/tools/builtin.py` 或 `shared/tools/library/<name>.py`
     里 register。节点 yaml `tools: [<name>]` 启用。

  3. **MCP / 外部工具**：在 `mcp_servers.yaml` 配置外部 server 命令；
     `shared/tools/mcp_loader.py` 启动时自动 `register_tool` 注册（带 prefix）。
     节点 yaml `tools: [<prefix>__<server_tool_name>]` 启用。
     详见 `mcp_servers.yaml.example` + `docs/tool-spec.md`。

三条路径都殊途同归到 `register_tool`，agent loop 不区分。
"""

from __future__ import annotations

import inspect as _inspect

import asyncio
import inspect
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from core import tool_errors as _errs

log = logging.getLogger("tool_registry")


# ── ToolDefinition ──────────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters_schema: dict  # JSON Schema object
    allowed_node_types: list[str] | None = None  # None = 所有节点都可用
    risk_level: str = "low"  # "low" | "medium" | "high"
    # Internal implementation tools may be called by another typed tool but
    # are never sent to an LLM, even if a stale Harness still lists them.
    internal_only: bool = False
    # Test/maintenance tools stay registered for deterministic test runners but
    # are neither exposed nor executable without a process-owner capability.
    required_runtime_capability: str | None = None
    # ── 上下文压缩契约（谁定义工具，谁声明旧结果怎么恢复）─────────────────
    # replayable_read=True：结果可以用同样参数重调取回（纯读工具）。压缩器可以
    # 把旧结果内容清成占位符 —— 参数还在前一条 assistant 消息里，信息零丢失。
    replayable_read: bool = False
    # result_compactor：工具声明的"旧结果 → 摘要"函数（输入原 content 字符串，
    # 返回带恢复指针的短摘要）。用于结果不可重读但落了盘的工具（如 run_node：
    # 子 run 的 summary/产物在磁盘上）。压缩器不写工具名单，只扫这两个声明。
    result_compactor: object | None = None
    # ── 内容契约（`{字段: 要求}`）────────────────────────────────────────────
    #
    # `parameters_schema` 管的是**调用参数**，框架会校验、模型也看得见。但很多
    # 工具真正会拒绝你的地方在**内容里**：`freeze_clean_results` 要 artifact 的
    # content JSON 带 `analysis_eligible`，`validate_contract` 要 `route_type`
    # 合法 —— 这些要求此前只活在校验器的 `errors.append("x must be …")` 里。
    #
    # 于是同一份契约有两份抄件：一份给模型看的 description（手写），一份是校验
    # 器的实现。两份各自演化，**分叉时不报错**。2026-08-18 实测代价：
    # `freeze_raw_results` 的说明写着 "…retention entries"，校验器要的却是
    # `raw_results.files` —— 模型照说明写，被自己的说明坑掉一整个来回。
    #
    # 声明放这里之后只剩一份：`register_tool` 把它渲染进模型看到的 description
    # （见 `_render_content_contract`），校验器用 `contract_requirement()` 从同一
    # 个 dict 取措辞。想让两边分叉，得先把这个 dict 拆成两个 —— 结构上做不到。
    content_contract: dict[str, str] | None = None


# ── ToolExecutor protocol ───────────────────────────────────────────────────


@runtime_checkable
class ToolExecutor(Protocol):
    """所有工具实现必须满足的协议。

    框架调用：`await executor(state=state, **llm_provided_kwargs)`
    工具返回：`dict` —— 应当含 `status` 字段。
    """

    async def __call__(self, *, state: Any, **kwargs: Any) -> dict[str, Any]: ...


# 兼容旧代码：保留 Callable 别名（一些地方旧 import）
ToolExecutorCallable = Callable[..., Awaitable[dict[str, Any]]]


# ── Registry ────────────────────────────────────────────────────────────────


@dataclass
class _Registry:
    tools: dict[str, ToolDefinition] = field(default_factory=dict)
    executors: dict[str, ToolExecutorCallable] = field(default_factory=dict)
    # v3.1 owner 归属审计：tool name → 注册它的 module 路径 / 源文件
    tool_sources: dict[str, str] = field(default_factory=dict)
    tool_source_files: dict[str, str] = field(default_factory=dict)
    # v3.1 豁免期内发生的同名覆盖记录（hf doctor 展示）
    tool_overrides: dict[str, list[dict]] = field(default_factory=dict)
    # 能力闸控的工具目录 —— **本框架有什么** ≠ **这台机器能跑什么**。
    # 见 register_capability_gated_tool。name → {capability, available,
    # definition, executor}。agent 只看 tools/executors（缺能力就没有这个
    # 工具，那条规矩不变）；文档站看这里，于是站的内容不再取决于谁来 build。
    capability_gated: dict[str, dict] = field(default_factory=dict)


_REGISTRY = _Registry()

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


# ── 内容契约：一份声明，两个消费者 ──────────────────────────────────────────


def contract_requirement(contract: dict[str, str], field_name: str) -> str:
    """校验器拒绝时的措辞 —— 从声明里取，不要另写一句。

    另写一句就是又开了一份抄件。校验器和模型看到的说明必须逐字同源，否则
    "说明写 entries、校验器要 files" 那类事会以别的形态再来一次。
    """
    requirement = contract.get(field_name)
    if not requirement:
        # 声明里没有这个字段 = 契约没覆盖到它。fail-loud：宁可报得难看，
        # 也不要静默退化回"只有校验器知道"的老样子。
        return f"{field_name} is rejected but not declared in this tool's content_contract"
    return f"{field_name} must be {requirement}"


def _render_content_contract(contract: dict[str, str] | None) -> str:
    """把声明渲染成模型读得懂的一段话，供 register_tool 拼进 description。"""
    if not contract:
        return ""
    items = "; ".join(f"`{name}` ({requirement})" for name, requirement in contract.items())
    return f" Content contract — the call is rejected unless: {items}."


# ── register_tool（带校验）──────────────────────────────────────────────────


def register_tool(definition: ToolDefinition, executor: ToolExecutorCallable) -> None:
    """注册一个工具。在工具模块 import 时调用。

    启动时强校验：
      - name 匹配 OpenAI function 名字规则
      - executor 是 async
      - executor 接受 `state` 关键字参数
      - parameters_schema 是 dict

    软警告（不阻塞注册，便于 MCP / 动态工具）：
      - parameters_schema.properties 跟 executor 签名 keyword 不一致
      - executor 没接 `**kwargs` 容错（LLM 传多余字段会 TypeError）
    """
    _validate_tool(definition, executor)

    # 契约**自动**拼进模型看到的说明 —— 不靠人记得手写一遍。
    # 这一步是"两份抄件变一份"的落点：声明改了，模型看到的立刻跟着改。
    rendered = _render_content_contract(definition.content_contract)
    if rendered and rendered not in definition.description:
        definition = replace(definition, description=definition.description + rendered)

    # 注册来源（owner 归属审计 —— hf doctor / 冲突报错都用）。
    # 身份判定用**源文件路径**而非 module 字符串：同一文件被两条 import 路径
    # 加载（如 nodes/<x>/hooks.py 里 sys.path hack 导致 `tools.foo` 与
    # `nodes.<x>.tools.foo` 双注册）是幂等 re-register，不是跨 owner 冲突。
    module = getattr(executor, "__module__", None) or "unknown"
    try:
        src_file = inspect.getsourcefile(inspect.unwrap(executor)) or module
    except (TypeError, ValueError):
        src_file = module

    if definition.name in _REGISTRY.tools:
        # v3.1（审计 高危#9）：同名注册不再静默覆盖。全局扁平命名空间 +
        # bootstrap 无条件 import 所有节点 tools/，曾发生 data 的 web_search
        # （allowed=['data']）遮蔽共享版 → 其它节点该工具直接消失。
        # 默认硬拒；迁移期豁免在 framework_exemptions.yaml 登记（降级 WARN）。
        prev_module = _REGISTRY.tool_sources.get(definition.name, "unknown")
        prev_file = _REGISTRY.tool_source_files.get(definition.name, prev_module)
        if prev_file != src_file:
            from shared.lib.exemptions import tool_collision_exemption

            ex_entry = tool_collision_exemption(definition.name, module)
            if ex_entry is None:
                raise ValueError(
                    f"工具 {definition.name!r} 同名冲突：已由 {prev_module} 注册，"
                    f"{module} 又要注册。同名覆盖会改变**所有**节点看到的这个工具"
                    f"（跨 owner 破坏）。解决：改名（如 <node>_{definition.name}）"
                    f"或向 framework owner 申请登记 framework_exemptions.yaml。"
                )
            level = log.error if ex_entry.get("expired") else log.warning
            level(
                "⚠️ 工具 %r 同名覆盖（%s ← %s）在豁免期内%s，deadline=%s。尽快迁移：%s",
                definition.name,
                prev_module,
                module,
                "（已过期！下版本将硬拒）" if ex_entry.get("expired") else "",
                ex_entry.get("deadline"),
                ex_entry.get("migrate_to"),
            )
            _REGISTRY.tool_overrides.setdefault(definition.name, []).append(
                {
                    "overridden": prev_module,
                    "by": module,
                    "deadline": ex_entry.get("deadline"),
                    "expired": bool(ex_entry.get("expired")),
                },
            )
        else:
            log.debug(
                "工具 %r 同源文件重复注册（reload/双 import 路径），幂等覆盖", definition.name
            )

    _REGISTRY.tools[definition.name] = definition
    _REGISTRY.executors[definition.name] = executor
    _REGISTRY.tool_sources[definition.name] = module
    _REGISTRY.tool_source_files[definition.name] = src_file


def register_capability_gated_tool(
    definition: ToolDefinition,
    executor: ToolExecutorCallable,
    *,
    capability: str,
    available: bool,
) -> None:
    """注册一个**依赖本机外部能力**的工具（Lean 工具链、python-flint……）。

    ⛔ 不改运行时那条规矩：`available=False` 时**不进** tools/executors，
    agent 照旧看不到这个工具，模型手写的调用照旧被当场拒掉
    （[[能力缺席就不给工具]]）。

    改的是**另一个问题**：这个框架里有没有这件东西。以前两个问题共用一份
    答案（"注册表里有" = "存在"），于是文档站的内容变成了 build 那台机器的
    函数 —— 实测三台机器三个站：

        写 origin/main 那台（有 lean + flint）→ 331 页
        我的笔记本（有 lean、没 flint）      → 330 页（少 interval_check）
        CI（两个都没有）                      → 329 页（两个都少）

    而每个工具页还列同侪工具，所以少一个工具会改掉上百页。后果不是"文档少
    一页"，是**没有任何一台机器能重建出提交的那份**，于是"重建结果 == 提交
    的产物"这道闸永远红，只能被关掉 —— 那正是文档站上次烂掉三个月的形状。

    所以：定义永远登记（文档站读它），可用性单独记一笔（文档站照实标注
    "需要 X"）。`python-flint` 连声明都没有、Lean 工具链也不适合进 CI ——
    正因为装不齐，才更需要把"装不齐"写成站上看得见的一句话，而不是让那页
    凭空消失。
    """
    # 校验不跟着能力走 —— 不然缺能力的机器上，这个工具的契约就从来没被验过，
    # 等哪天装上了才第一次发现它是坏的。
    _validate_tool(definition, executor)

    _REGISTRY.capability_gated[definition.name] = {
        "capability": capability,
        "available": bool(available),
        "definition": definition,
        "executor": executor,
    }
    if available:
        register_tool(definition, executor)


def _validate_tool(d: ToolDefinition, ex: ToolExecutorCallable) -> None:
    """启动时校验 —— 不通过 raise ValueError，让同事立刻知道哪写错了。"""

    # 1. name 合法
    if not isinstance(d.name, str) or not _NAME_RE.match(d.name):
        raise ValueError(f"工具 name={d.name!r} 不合法（OpenAI 要求 ^[a-zA-Z0-9_-]{{1,64}}$）。")

    # 2. parameters_schema 是 dict
    if not isinstance(d.parameters_schema, dict):
        raise ValueError(
            f"工具 {d.name!r} 的 parameters_schema 必须是 dict（JSON Schema object），"
            f"got {type(d.parameters_schema).__name__}"
        )

    # 3. executor 必须是 async
    if not inspect.iscoroutinefunction(ex):
        # callable wrapper（如 MCP _make_executor 返回的）也可以 —— 检查 __call__
        call = getattr(ex, "__call__", None)
        if call is None or not inspect.iscoroutinefunction(call):
            raise ValueError(
                f"工具 {d.name!r} 的 executor 必须是 async（async def 或返回 coroutine 的 callable）"
            )

    # 4. signature 检查（只对真函数，不对 lambda 或 partial）
    try:
        sig = inspect.signature(ex)
    except (ValueError, TypeError):
        # 内置 / wrapper 拿不到 signature 也行
        return

    params = sig.parameters
    has_state = "state" in params
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    if not has_state and not has_var_kw:
        raise ValueError(
            f"工具 {d.name!r} 的 executor 必须接受 `state` 参数（kwarg-style）。当前签名：{sig}"
        )

    if not has_var_kw:
        log.warning(
            "工具 %r executor 没有 `**kwargs` 吸收兜底 —— LLM 多传一个字段就会 TypeError。"
            "建议加 `**_: Any`。当前签名：%s",
            d.name,
            sig,
        )

    # 5. schema keywords 跟 signature kwargs 对照（warn 不 fail）
    schema_props = d.parameters_schema.get("properties") or {}
    explicit_kwargs = {
        name
        for name, p in params.items()
        if p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and name != "state"
    }
    schema_only = set(schema_props.keys()) - explicit_kwargs
    sig_only = explicit_kwargs - set(schema_props.keys())

    if schema_only and not has_var_kw:
        log.warning(
            "工具 %r：schema 声明了 %s 但 executor 签名没有这些参数（且没 **kwargs）",
            d.name,
            sorted(schema_only),
        )
    if sig_only:
        # signature 有但 schema 没声明 —— LLM 不会知道这些参数存在
        log.warning(
            "工具 %r：executor 接受 %s 但 parameters_schema 没声明 —— LLM 看不到，也不会传",
            d.name,
            sorted(sig_only),
        )


# ── 查询 ────────────────────────────────────────────────────────────────────


def get_tool(name: str) -> ToolDefinition | None:
    return _REGISTRY.tools.get(name)


def list_tools_for_node(
    node_type: str,
    whitelist: list[str],
    *,
    state: Any | None = None,
) -> list[ToolDefinition]:
    """该节点既被允许使用、又在自己 yaml 白名单里的工具。"""
    out: list[ToolDefinition] = []
    for name in whitelist:
        t = _REGISTRY.tools.get(name)
        if t is None:
            continue
        if t.internal_only:
            continue
        if t.allowed_node_types is not None and node_type not in t.allowed_node_types:
            continue
        if t.required_runtime_capability:
            from core.runtime_capabilities import has_runtime_capability

            # `state is None` 不再当成"没能力"：那句话属于测试档位那类能力
            # （它们挂在 state 上），而模型角色那类根本不看 state。
            # 判据交给 has_runtime_capability 一处回答，别在这里再判一次。
            if not has_runtime_capability(state, t.required_runtime_capability):
                continue
        out.append(t)
    return out


# ── 框架保留参数：check_after_seconds ───────────────────────────────────────
#
# "多久算异常"是**领域知识，只有 agent 知道**：`pip install` 半分钟没动静就该
# 看一眼，LAMMPS 弛豫跑六小时才正常，HPC 排队几天也正常。框架去猜一个统一阈值，
# 本来就是在替它做判断（而且必然猜错一半）。
#
# 所以由 agent 在调用时自己声明检查点。到点框架**只把轮次还给它**，命令一点没动
# 继续跑（见 `_hand_back`）。它拿自己的 shell 去看日志/进程/端口，判断合理就接着
# 干别的，结果跑完自动送到。判断不对劲就自己 kill。
#
# 注入在 `to_openai_schema` 这一处 —— 所有工具（含同事的、MCP 的）自动获得，
# 谁都不用改。
RESERVED_CHECK_AFTER = "check_after_seconds"

_CHECK_AFTER_SCHEMA = {
    "type": "number",
    "description": (
        "（框架参数，可选）你估计这条要跑多久才算异常，单位秒。到点框架把轮次"
        "还给你去查看，**命令不会被中断**，它继续在后台跑、结果跑完自动送到你"
        "下一轮。填你认为『超过这个时间就该看一眼』的值：快命令不用填；长作业"
        "（编译 / 下载 / 训练 / HPC 提交）填大些，可以先短后长 —— 第一次早点看"
        "确认起步正常，之后放宽。不填 = 一直等到它自己返回。"
    ),
}

_MAX_HANDBACK_S = float(os.getenv("HARNESS_TOOL_HANDBACK_MAX_S", "21600") or 21600)
"""agent 没声明检查点时的兜底（默认 6 小时）。**不杀任何东西**，只是到点把轮次
还给它 —— 纯粹为了"再离谱的挂死也不会挂穿一整夜"（E2E-5 实测挂了 15.5 小时）。
设 0 关闭。agent 自己声明的值优先。"""


def _pop_check_after(kwargs: dict) -> float:
    """摘出框架保留参数。非法值当没填（不因为参数写错就拒绝执行工具）。"""
    raw = kwargs.pop(RESERVED_CHECK_AFTER, None)
    if raw is None:
        return _MAX_HANDBACK_S
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _MAX_HANDBACK_S
    return v if v > 0 else 0.0


def to_openai_schema(tool: ToolDefinition) -> dict:
    """把 ToolDefinition 转成 OpenAI function-calling 风格的 schema。

    统一注入框架保留参数 `check_after_seconds`（见上）——**唯一注入点**，
    所有工具自动获得，工具定义一行不用改。工具自己已有同名参数就不覆盖。
    """
    params = tool.parameters_schema
    props = params.get("properties") if isinstance(params, dict) else None
    if isinstance(props, dict) and RESERVED_CHECK_AFTER not in props:
        params = {**params, "properties": {**props, RESERVED_CHECK_AFTER: _CHECK_AFTER_SCHEMA}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": params,
        },
    }


def tool_ownership_report() -> dict:
    """v3.1：谁注册了什么、谁在豁免期内覆盖了谁（`hf doctor` 审计视图）。"""
    return {
        "sources": dict(_REGISTRY.tool_sources),
        "overrides": {k: list(v) for k, v in _REGISTRY.tool_overrides.items()},
    }


def all_tool_names() -> list[str]:
    return sorted(_REGISTRY.tools.keys())


# ── 执行 + result envelope ──────────────────────────────────────────────────

# ── 工具调用必须有界（E2E-5 实测：一次 tool_call 挂了 15.5 小时）────────────
#
# `execute()` 的 envelope 契约写着"工具抛异常 → catch 成 error"。但**挂住不是
# 异常** —— 第 345 行原本是一个裸 await，没有任何上界。现场：experiment 节点
# 用 safe_run_bash 起 vLLM 服务，`A && B &` 让 bash fork 出的子 shell 继承了
# stdout 管道并 wait 到服务结束为止，而工具等的是管道 EOF 而非进程退出 →
# 工具永不返回 → 节点永不返回 → 两个 E2E 分别僵死 15.5 小时和 5 小时。
#
# 那个 bug 在节点 owner 的工具里，但**框架允许它挂穿一整夜**是架构缺兜底：
# 工具实现的质量参差不齐是常态，框架不能假设每个工具作者都写对了收尾。
#
# **不设硬性超时。** 合法的长任务是常态 —— 那次模型下载真跑了 60 分钟，HPC
# 上一个作业跑几天很正常。框架无从判断"多久算太久"，硬杀只会把真实验砍掉。
#
# 框架该做的是让**"卡了多久"变成监督方看得见的事实**：工具在飞期间按递增间隔
# 记 `tool_long_running`。orchestrator 的 runtime_control progress 读的就是
# transcript，于是它看到的不再是"子节点安静着"（15 小时里它一直只知道这个，
# 每次都合理地选择继续等），而是"子节点在**同一次 safe_run_bash 调用**里待了
# 3 小时、零输出"。后者是完全不同的信号，够它判断该不该干预了。
#
# 决定权归有上下文的那一方 —— 跟今天早上把"等"从模型手里收回框架是同一条原则
# 的另一面：机械的事（等）框架做，需要判断的事（这么久合不合理）给看得见全局
# 的人/agent 判，但**必须先让它看得见**。
_LONG_RUNNING_MARKS_S = (600, 1800, 3600, 7200, 14400, 28800, 57600, 86400)
"""在这些时刻各记一条 —— 10 分钟起，之后 30m/1h/2h/4h/8h/16h/24h，
之后每 24h 一条。递增是为了长作业不刷屏，又不会彻底失声。"""


async def _long_running_watch(state, tool_name: str, kwargs: dict) -> None:
    """在飞期间按递增间隔留痕。被 cancel（工具正常返回）即安静退出。"""
    started = time.monotonic()
    marks = list(_LONG_RUNNING_MARKS_S)
    while True:
        nxt = marks.pop(0) if marks else None
        if nxt is None:
            nxt = int(time.monotonic() - started) + 86400
        delay = nxt - (time.monotonic() - started)
        if delay > 0:
            await asyncio.sleep(delay)
        elapsed = int(time.monotonic() - started)
        try:
            state.append_transcript(
                "tool_long_running",
                tool_name=tool_name,
                elapsed_seconds=elapsed,
                elapsed_human=f"{elapsed // 3600}h{elapsed % 3600 // 60}m",
                args_preview=str(kwargs)[:300],
                note=(
                    "这次工具调用仍未返回。长任务本身正常（编译/下载/训练/"
                    "HPC 作业跑几小时～几天都合理）——像科学家盯集群作业那样"
                    "判断它：用 runtime_control progress 看 workspace 输出"
                    "文件在不在长，用 tail_file 看日志尾部（时间戳/吞吐/有无 "
                    "error）。输出在长就继续等。常见的**假长任务**：shell 里"
                    "用 `A && B &` 起长驻服务，bash 会 fork 子 shell 跑整个 "
                    "`&&` 串并继承输出管道、等到服务结束才退出，调用方永远等"
                    "不到管道 EOF（`nohup`/`setsid` 不关文件描述符）——症状是"
                    "服务日志显示早已就绪、这里却一直不返回。"
                ),
            )
        except Exception:
            pass


async def _await_bounded(tool_name: str, executor, state, kwargs: dict, check_after: float):
    """await 工具，同时开一个留痕看门狗。配置了硬上界才会超时。

    看门狗或上界机制本身出问题时退化成裸 await —— 兜底层不该比没有兜底更坏。
    """
    try:
        watch = asyncio.ensure_future(_long_running_watch(state, tool_name, kwargs))
    except Exception:
        return await executor(state=state, **kwargs)
    try:
        call = asyncio.ensure_future(executor(state=state, **kwargs))
        if check_after <= 0:
            return await call
        try:
            return await asyncio.wait_for(asyncio.shield(call), timeout=check_after)
        except TimeoutError:
            return _hand_back(state, tool_name, kwargs, call, check_after)
    finally:
        watch.cancel()


# ── 交还控制权（不是中止）──────────────────────────────────────────────────
#
# agent 说的 check_after_seconds 到点了 → 框架把**轮次**还给它，**底下的调用
# 一点没动**，继续在后台跑。它跑完时结果走 `injected_messages` 推回去 ——
# 复用后台子节点那条现成通道（agent_loop.py 每轮 turn_start 消费，所有节点都吃）。
#
# 为什么这里不加任何"查作业"的工具：agent 判断作业健不健康靠的是看外部世界
# （tail 日志 / ps / curl 端口），那些**它自己的 bash 全能干**。框架唯一能提供
# 而 bash 拿不到的，只有"框架自己这个还没返回的调用最终返回了什么"—— 而那个
# 直接推给它就行，不需要它来查。
#   （wangd 连着三次戳这一点：我一遇到问题就伸手加工具。最后净新增 = 一个参数。）
_PENDING: dict[str, dict] = {}


def _job_id(state, tool_name: str) -> str:
    try:
        n = int(state.hook_state.get("_job_seq", 0) or 0) + 1
        state.hook_state["_job_seq"] = n
    except Exception:
        n = len(_PENDING) + 1
    return f"{tool_name}#{n}"


def _hand_back(state, tool_name: str, kwargs: dict, call, waited: float) -> dict[str, Any]:
    """到检查点了：把轮次还给 agent，调用继续跑。"""
    job_id = _job_id(state, tool_name)
    _PENDING[job_id] = {"task": call, "tool": tool_name}

    def _done(task) -> None:
        _PENDING.pop(job_id, None)
        try:
            if task.cancelled():
                body = f"⏹️ 后台调用 `{job_id}` 已取消。"
            elif task.exception() is not None:
                body = f"❌ 后台调用 `{job_id}`（{tool_name}）出错：{task.exception()!r}"
            else:
                body = (
                    f"✅ 后台调用 `{job_id}`（{tool_name}）已返回。结果：\n"
                    f"{str(task.result())[:2000]}"
                )
            state.hook_state.setdefault("injected_messages", []).append(
                {"content": body, "source": "pending_tool_call"}
            )
        except Exception:
            pass

    call.add_done_callback(_done)
    try:
        state.append_transcript(
            "tool_handed_back",
            tool_name=tool_name,
            job_id=job_id,
            waited_seconds=waited,
            args_preview=str(kwargs)[:400],
        )
    except Exception:
        pass
    return {
        "status": "running",
        "job_id": job_id,
        "waited_seconds": round(waited, 1),
        "note": (
            f"到你设的检查点了（{waited:.0f}s）。**命令没有被中断，也没有失败** —— "
            f"它还在后台跑，跑完的结果会自动送到你下一轮。\n"
            f"现在轮到你判断它健不健康：用你自己的 shell 去看 —— 日志尾部"
            f"（时间戳有没有推进 / 吞吐是否合理 / 有没有 error、OOM、nan）、"
            f"输出文件在不在变大、进程还在不在、端口通不通。\n"
            f"判断合理 → 什么都不用做，继续干别的，结果到了会通知你；"
            f"判断不对劲 → 用 shell 自己处理（kill 进程 / 换做法）。\n"
            f"⚠️ **不要因为没拿到返回值就重跑同一条命令** —— 它还在跑，重跑会"
            f"起第二份。"
        ),
    }




def _dumps_for_scan(result: Any) -> str:
    """把工具结果压成一段可扫的文本（只用于识别报错，不改内容）。"""
    import json as _json

    try:
        return _json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


def _record_witness_failure(state, tool_name: str, *, phase: str, error: str) -> None:
    """见证器（调用前快照 / 事后比对）崩了：留证据，不下判决。

    `phase` ∈ {before, after}。这条事件是「这一次调用没被见证」的账本记录 ——
    没有它，transcript 上这次调用看起来像「见证过、干净」。
    """
    try:
        state.append_transcript(
            "workspace_witness_failed",
            tool_name=tool_name,
            phase=phase,
            error=error,
            note="工作区见证器故障；工具照跑，本次调用的越界写**未被见证**"
                 "（不是「没有越界」）。墙在沙箱，这里只是事后拍照。",
        )
    except Exception:
        log.debug("could not record witness failure for %s: %s", tool_name, error)


async def execute(tool_name: str, state, /, **kwargs: Any) -> dict[str, Any]:
    """按名字调度工具。返回工具的 dict 结果（带 envelope 保证）。

    保证返回 dict 一定有 `status` 字段（success / error / pause）：
      - 工具自己写了 status → 透传
      - 工具返非 dict → wrap 成 {"status": "error", "error": "...", "raw": ...}
      - 工具返 dict 但没 status → wrap 成 {"status": "success", "data": <orig>} + warn
      - 工具抛异常 → catch 成 {"status": "error", "error": "<type>: <msg>"}

    `tool_name` 和 `state` 用 positional-only（防 LLM 传 'name' / 'state' kwarg 撞名）。

    这里是**唯一的工具派发口**，两条全局不变量因此收在这一层，不用每个工具各写
    一遍（写一遍就漏一遍，而漏掉的那个没人会发现）：

      - 取消（#284）：已 /stop 的 run 不许再派发新工具 —— 抛 RunCancelled，
        由 run_loop / execute_node 收口成 cancelled 并照常 finalize。
      - 脱敏（#279）：工具结果里凡出现环境里的秘密值，出这道门之前换成
        `«REDACTED:VAR»`。不管秘密是 read_file 捞的、run_bash `cat` 的、还是
        execute_python 打印 os.environ 打出来的，出口只有这一个。
    """
    from core import cancellation as _cancel
    from core import secrets as _secrets

    _cancel.check(f"tool:{tool_name}")
    # 写边界的墙在 spawn 那一刻（core/sandbox.py）；这里只是事后见证的「调用前
    # 拍照」。拍照崩了（gitdir 指针坏、git 超时……）不构成拒绝派发的理由 ——
    # 从前这里 fail-closed，见证器一故障整条 run 的每次工具调用都 GUARD_FAILED。
    # 判决拆除（2026-09-02）：照跑工具，把「这一次没见证」如实记进结果与
    # transcript；对称面在 enforce_after_tool（baseline 缺席报 witness_unavailable
    # 而不是空集）。
    witness_failure: str | None = None
    try:
        from core.project_workspace import capture_before_tool

        capture_before_tool(state)
    except Exception as exc:
        witness_failure = f"{type(exc).__name__}: {exc}"
        _record_witness_failure(state, tool_name, phase="before", error=witness_failure)
    result = await _execute_dispatch(tool_name, state, **kwargs)
    # 写边界的墙在 spawn 那一刻（core/sandbox.py 的进程沙箱）；这里是事后
    # **见证**：读文件系统的真实结果，发现越界写只报告（transcript 事件 +
    # 结果附注），不回退不删除 —— 守卫的销毁能力已整体下线（2026-08-13）。
    try:
        from core.project_workspace import (
            enforce_after_tool,
            explain_permission_denied,
            observe_after_tool,
        )

        # 沙箱给的是 `Permission denied` —— 位置精确，但读起来像环境故障，
        # 模型接下来多半去试 chmod / sudo。翻译成"这是谁的东西、该找谁"，
        # 否则就是又一个指向假原因的报错。
        note = explain_permission_denied(state, _dumps_for_scan(result))
        if note and isinstance(result, dict):
            result = {**result, "boundary_note": note}

        report = enforce_after_tool(state, tool_name)
        if report and isinstance(result, dict):
            if report.get("witness_unavailable"):
                # 快照缺席：账上写「没见证」，不写「没越界」
                result = {
                    **result,
                    "workspace_witness": "unavailable",
                    "workspace_witness_note": report["note"],
                }
            else:
                result = {
                    **result,
                    "workspace_scope_warning": report["note"],
                    "out_of_scope_paths": report["paths"][:50],
                }
        observe_after_tool(state, tool_name)
    except Exception as exc:
        # 工具已经跑完、副作用已经发生；见证器在事后崩了不能把真实结果换成
        # error（模型会重试已成功的副作用）。同一条规则：如实附注。
        witness_failure = f"{type(exc).__name__}: {exc}"
        _record_witness_failure(state, tool_name, phase="after", error=witness_failure)
    if witness_failure and isinstance(result, dict):
        result = {
            **result,
            "workspace_witness_failed": witness_failure,
            "workspace_witness": "unavailable",
        }
    # ── 首用附单：本项目手册里挂在这个工具上的教训 ──────────────────────
    #
    # 为什么附在**首次调用之后**而不是拦在之前：拦在之前需要在派发口做决策
    # （拦不拦、拦了怎么放行），那是一道会误伤的闸。附在结果上零风险，
    # 而真正要防的是**第二次**用错 —— 首错由开工切片预防（turn 1 已送过
    # 适用于本节点的条目），迭代错由这里在下一次调用前拦住。
    #
    # 每工具每 run 只附一次：教训念第二遍不产生价值，只挤占上下文。
    try:
        _seen = state.hook_state.setdefault("_tool_brief_sent", set())
        if tool_name not in _seen and isinstance(result, dict):
            from core.memory_delivery import tool_briefing

            brief = tool_briefing(state, tool_name)
            _seen.add(tool_name)
            if brief:
                result = {**result, "memory_note_on_this_tool": brief}
    except Exception as exc:                     # 送达失败不该拖垮工具调用
        log.debug("tool briefing failed for %s: %s", tool_name, exc)

    leaked = _secrets.contains_secret(result)
    if leaked:
        result = _secrets.redact(result)
        try:
            # 只留变量名，绝不留值 —— 留痕是为了提示轮换凭据，不是再泄一次。
            state.append_transcript(
                "secret_redacted_from_tool_result",
                tool_name=tool_name,
                env_var_names=leaked,
                note="工具结果命中进程环境里的秘密值，已在进入模型上下文前脱敏；"
                "请视这些凭据为可能已暴露并考虑轮换。",
            )
        except Exception:
            pass

    # ── 出口有界（2026-08-22）────────────────────────────────────────────
    #
    # 必须在**脱敏之后**：落盘的是完整原文，秘密得先换掉，否则等于把凭据写进
    # 一个长期留在 run 目录里的文件。
    #
    # 实测缺口：632 个真实 checkpoint 里，8 条 read_artifact / 2 条
    # read_producer_transcript 单条超 20 万字符，最大一条 **1,865,703 字符**
    # （约 58 万 tokens，是默认窗口的 2.3 倍）。撑爆窗口的 checkpoint 全部是被
    # 单条撑爆的 —— 不是"读了太多次"。工作集的「至多一份活副本」对它无效
    # （本来就一份），压缩也救不了（它自己比 context 还大）。唯一的解是不让它进来。
    #
    # 放在这个咽喉而不是各工具里：`read_file` 和 KB 早就各自做了有界读，而
    # read_artifact 没有 —— 逐个实现就是逐个漏，漏掉的那个没人会发现。
    try:
        from core import bounded_output as _bounded

        result = _bounded.bound(state, tool_name, result)
    except Exception as exc:            # 有界化失败不该吃掉工具结果
        log.warning("bounded_output 失败（%s），原样返回：%s", tool_name, exc)
    return result


def _missing_required_params(executor: Any, kwargs: dict[str, Any]) -> list[str]:
    """executor 签名上没有默认值、调用方也没给的参数名。

    只看签名，不看 schema —— schema 可能和实现分叉，而真正会抛 TypeError 的
    是签名。`state` 由框架注入；`**_` 吸收多余参数，不在此列。
    """
    try:
        sig = _inspect.signature(executor)
    except (TypeError, ValueError):
        return []
    missing: list[str] = []
    for name, param in sig.parameters.items():
        if name == "state" or param.kind in (
            param.VAR_KEYWORD, param.VAR_POSITIONAL,
        ):
            continue
        if param.default is param.empty and name not in kwargs:
            missing.append(name)
    return missing


# ── 实参形状：崩了之后回头问一句"是不是形状不对" ────────────────────────────
#
# `parameters_schema` 在**注册时**验过格式、和 executor 签名比对过，但调用时
# 一次都没拿它对过实参。于是"模型给了 list[str]，工具声明 list[dict]"这类
# 调用一路走到工具内部才炸：
#
#     AttributeError: 'str' object has no attribute 'get'
#         （score_hypothesis_innovation / cluster_hypothesis_candidates）
#     AttributeError: 'dict' object has no attribute 'replace'   （run_node）
#
# 对模型三重无用：不知道是哪个参数、不知道该是什么形状、更不知道这是它自己
# 调错了。节点只好各写一遍逐条 try/except —— hif_scorer 就写了，捕了
# KeyError/TypeError/ValueError 三种，漏了 AttributeError，整个工具还是崩。
# 每个节点各补一遍，补漏一种就崩一次（[[护栏要扫盘，不要写名单]]）。
#
# ## 为什么是"崩了之后"才检查，而不是调用前拦
#
# 拦在调用前会**改变现在正常工作的行为**：`save_artifact.metadata` 声明
# `type: object`，工具却故意也接受 JSON 字符串并解析（有专门的测试守着）；
# `content` 声明 string，框架现在会把对象规范化成 JSON 文本。这类"刻意的
# 宽松"在 178 个工具里不知道还有多少，一刀切的类型闸会把它们全拒掉。
#
# 所以这道门只走**异常路径**：调用成功就当无事发生（宽松的工具照常宽松），
# 崩了才回头问一句"是不是实参形状不对"——是就把它记成 `rejected` 并把声明的
# 形状给出去，模型下一轮改对；不是就照旧记 `tool_exception`（我们的 bug）。
# 零风险，且覆盖全部工具，包括 MCP 来的和以后新写的。
#
# 代价说清楚：形状不对 + 恰好又是真 bug 的调用会被记成 rejected，从 bug 面板
# 上消失。所以原始异常正文一并带回，traceback 也照写 transcript —— 判决变了，
# 证据没少。
_JSON_TYPE_CHECKS: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


# ── 值约束：schema 里已经写着的 enum / 区间 / pattern / 非空，派发口核一次 ──────
#
# 与上面「崩了之后才查形状」不同：这里查的不是**类型**（类型有刻意宽松的工具，
# 见上），而是**取值**——schema 明写 `enum: [a, b]`、`minimum: 1`、`minLength: 1`、
# `pattern`，给了别的值没有任何工具会「刻意宽松」地接受。二审普查数了一下：
# 仓库工具 schema 里已声明 enum ×100、minimum ×76、maximum ×52、pattern ×19，
# 派发口一个都不看，于是 30 个文件里散着 176 处手写的同一件事，各配一份报错文案。
# 契约只在 schema 声明一次、这里核一次，报错自动列合法值（[[契约必须送到调用方]]）。
# 工具体内不再手写这些检查；schema 没写的约束，就是没有这条约束。
_VALUE_KEYS = ("enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "minLength", "maxLength", "pattern", "minItems", "maxItems")


def _schema_value_violations(schema: dict | None, kwargs: dict[str, Any]) -> list[str]:
    """按 `parameters_schema` 核实参**取值**；返回人话违规列表（空 = 合规）。

    只看值约束（enum/区间/长度/pattern/条目数），不看 type（见上文）；
    值为 None 视作未给。递归进 object 的 properties 与 array 的 items。
    """
    if not isinstance(schema, dict):
        return []
    out: list[str] = []
    _walk_value_constraints(schema, kwargs, "", out)
    return out


def _walk_value_constraints(schema: dict, value: Any, path: str, out: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    props = schema.get("properties")
    if isinstance(props, dict) and isinstance(value, dict):
        for name, sub in props.items():
            if name in value and value[name] is not None:
                _walk_value_constraints(sub, value[name], f"{path}.{name}" if path else name, out)
        return
    items = schema.get("items")
    if isinstance(items, dict) and isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path} 至少 {schema['minItems']} 项（给了 {len(value)} 项）")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path} 至多 {schema['maxItems']} 项（给了 {len(value)} 项）")
        for i, v in enumerate(value):
            if v is not None:
                _walk_value_constraints(items, v, f"{path}[{i}]", out)
        return
    _check_leaf(schema, value, path, out)


def _check_leaf(schema: dict, value: Any, path: str, out: list[str]) -> None:
    if "enum" in schema and isinstance(schema["enum"], list):
        allowed = schema["enum"]
        if isinstance(value, (str, int, float, bool)) and value not in allowed:
            out.append(f"{path}={value!r} 不在合法值里，合法值：{allowed}")
            return
    if isinstance(value, bool):          # bool 是 int 的子类，别拿区间去卡它
        return
    if isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}={value!r} 小于最小值 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}={value!r} 大于最大值 {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append(f"{path}={value!r} 须大于 {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            out.append(f"{path}={value!r} 须小于 {schema['exclusiveMaximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value.strip() if schema["minLength"] == 1 else value) < schema["minLength"]:
            out.append(f"{path} 不能为空" if schema["minLength"] == 1
                       else f"{path} 至少 {schema['minLength']} 个字符")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path} 超过 {schema['maxLength']} 个字符")
        pat = schema.get("pattern")
        if isinstance(pat, str):
            try:
                if not re.search(pat, value):
                    out.append(f"{path}={value[:80]!r} 不匹配 {pat!r}")
            except re.error:
                pass
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path} 至少 {schema['minItems']} 项（给了 {len(value)} 项）")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path} 至多 {schema['maxItems']} 项（给了 {len(value)} 项）")


def _schema_required_missing(schema: dict | None, kwargs: dict[str, Any]) -> list[str]:
    """schema `required` 里声明了、调用方没给（或给了 None）的顶层参数名。"""
    if not isinstance(schema, dict):
        return []
    req = schema.get("required")
    if not isinstance(req, list):
        return []
    return [str(n) for n in req if kwargs.get(n) is None]


def _is_provider_failure(exc: BaseException) -> str:
    """是模型服务侧的故障/限额就返回给人看的那一行，否则返回 ``""``。"""
    try:
        from core.llm import LLMHTTPError, describe_provider_error, is_transient_provider_error
    except Exception:      # noqa: BLE001 —— llm 模块出问题不该连累工具分类
        return ""
    if isinstance(exc, LLMHTTPError) or is_transient_provider_error(exc):
        return describe_provider_error(exc)
    return ""


def _json_type_name(value: Any) -> str:
    for name, expected in (("boolean", bool), ("object", dict), ("array", list),
                           ("string", str), ("integer", int), ("number", float)):
        if isinstance(value, expected):
            return name
    return type(value).__name__


def _type_matches(value: Any, declared: str) -> bool:
    expected = _JSON_TYPE_CHECKS.get(declared)
    if expected is None:
        return True
    if declared in ("integer", "number") and isinstance(value, bool):
        return False            # bool 是 int 的子类，但没人declare integer想要 True
    return isinstance(value, expected)


def _argument_shape_mismatches(schema: Any, kwargs: dict[str, Any]) -> list[str]:
    """模型给的实参里，与声明形状不符的那些。只看**它真的传了**的参数。"""
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return []
    out: list[str] = []
    for name, value in kwargs.items():
        spec = props.get(name)
        if not isinstance(spec, dict) or value is None:
            continue
        declared = spec.get("type")
        if not isinstance(declared, str):
            continue            # 联合类型 / 没声明：这道门不表态
        if not _type_matches(value, declared):
            out.append(f"{name} 声明是 {declared}，实际收到 {_json_type_name(value)}")
            continue
        items = spec.get("items")
        item_type = items.get("type") if isinstance(items, dict) else None
        if declared == "array" and isinstance(item_type, str):
            for index, item in enumerate(value):
                if not _type_matches(item, item_type):
                    out.append(
                        f"{name} 声明是 {item_type} 的数组，但第 {index} 个元素是 "
                        f"{_json_type_name(item)}"
                    )
                    break
    return out


async def _execute_dispatch(tool_name: str, state, /, **kwargs: Any) -> dict[str, Any]:
    """execute 的原始派发实现（取消/脱敏由 execute 包在外面）。"""
    # 框架保留参数：**在这里摘掉，永远不传给工具** —— 于是任何工具（包括同事
    # 写的、MCP 来的）都不用改一行就获得"到点交还控制权"的能力。
    check_after = _pop_check_after(kwargs)

    executor = _REGISTRY.executors.get(tool_name)
    if executor is None:
        return {"status": "error", "error_code": _errs.NOT_REGISTERED,
                "error": f"工具 {tool_name!r} 未注册。"}
    definition = _REGISTRY.tools.get(tool_name)
    if definition and definition.required_runtime_capability:
        from core.runtime_capabilities import has_runtime_capability

        if not has_runtime_capability(state, definition.required_runtime_capability):
            from core.runtime_capabilities import capability_absence_reason

            try:
                state.append_transcript(
                    "runtime_capability_denied",
                    tool_name=tool_name,
                    required_capability=definition.required_runtime_capability,
                )
            except Exception:
                pass
            return {
                "status": "error",
                "error_code": _errs.CAPABILITY_DENIED,
                # 原因由能力自己说 —— 从前这里写死"当前生产 run 未授权"，
                # 对模型角色那类能力是句错话（它跟"生产/测试"无关）。
                "error": (
                    f"工具 {tool_name!r} 在本次运行里不可用："
                    + capability_absence_reason(definition.required_runtime_capability)
                ),
                "required_runtime_capability": definition.required_runtime_capability,
            }
    # ── 缺必填参数：当场说清楚，别让裸 TypeError 冒到模型面前 ──────────────
    # `_request_human_input() missing 1 required positional argument: 'question'`
    # 对模型是三重无用：它不认识内部函数名、不知道工具叫什么、更不知道这个工具
    # 到底收哪些参数。合法取值只在运行时报错 = 逼模型猜。
    _schema = (definition.parameters_schema or {}) if definition else {}
    _props = (_schema.get("properties") or {}) if isinstance(_schema, dict) else {}
    _missing = _missing_required_params(executor, kwargs)
    for _n in _schema_required_missing(_schema, kwargs):
        if _n not in _missing:
            _missing.append(_n)
    if _missing:
        return {
            "status": "error",
            "error_code": _errs.MISSING_PARAMETERS,
            "error": (
                f"调用 {tool_name!r} 缺必填参数：{_missing}。"
                f"本工具接受的参数：{sorted(_props) or '（见工具定义）'}。"
            ),
            "missing_parameters": _missing,
            "parameters_schema": _schema,
        }
    # ── 取值不合 schema 声明（enum/区间/非空/pattern）：派发口核一次，工具体内不手写 ──
    _violations = _schema_value_violations(_schema, kwargs)
    if _violations:
        return {
            "status": "error",
            "error_code": _errs.REJECTED,
            "error": (
                f"调用 {tool_name!r} 的参数不合契约：" + "；".join(_violations)
                + f"。本工具接受的参数：{sorted(_props) or '（见工具定义）'}。"
            ),
            "parameter_violations": _violations,
            "parameters_schema": _schema,
        }
    try:
        result = await _await_bounded(tool_name, executor, state, kwargs, check_after)
    except _errs.ToolRejection as e:
        # 故意用 raise 实现的驳回 —— 不是 bug，也别带 `TypeError:` 那种前缀。
        return {"status": "error", "error_code": _errs.REJECTED, "error": str(e)}
    except Exception as e:
        import traceback as _tb

        tb_str = _tb.format_exc()
        # provider 侧的故障/限额不是"我们的代码崩了"。判据用 core.llm 那份
        # 唯一真相源（重试策略共用同一份名单），别在这里再写一份长得像的。
        _provider = _is_provider_failure(e)
        if _provider:
            return {"status": "error", "error_code": _errs.PROVIDER_ERROR,
                    "error": _provider, "traceback_tail": tb_str.splitlines()[-3:]}
        # 崩了先回头问一句：是不是实参形状不对？（见上面那段注释）
        _shape = _argument_shape_mismatches(
            definition.parameters_schema if definition else None, kwargs)
        if _shape:
            _props = ((definition.parameters_schema or {}).get("properties")
                      if definition else None) or {}
            try:
                state.append_transcript(
                    "tool_argument_shape_rejected",
                    tool_name=tool_name, mismatches=_shape,
                    exc_type=type(e).__name__, traceback=tb_str[-2000:],
                )
            except Exception:
                pass
            return {
                "status": "error",
                "error_code": _errs.REJECTED,
                "error": (
                    f"调用 {tool_name!r} 的参数形状不对：" + "；".join(_shape)
                    + f"。本工具接受的参数：{sorted(_props) or '（见工具定义）'}。"
                    + f"（工具因此报错：{type(e).__name__}: {str(e)[:200]}）"
                ),
                "argument_shape_mismatches": _shape,
                "parameters_schema": definition.parameters_schema if definition else {},
            }
        # 完整 traceback 写 transcript（不返给 LLM 避免 context 爆炸）
        try:
            state.append_transcript(
                "tool_exception",
                tool_name=tool_name,
                exc_type=type(e).__name__,
                exc_msg=str(e),
                traceback=tb_str[-2000:],
            )
        except Exception:
            pass
        # 返给 LLM 的精简错误（含末 3 行 traceback 帮 LLM 自纠）
        tb_tail = tb_str.splitlines()[-3:] if tb_str else []
        return {"status": "error", "error_code": _errs.TOOL_EXCEPTION,
                "error": f"{type(e).__name__}: {e}", "traceback_tail": tb_tail}
    return _wrap_result(tool_name, result)


def _wrap_result(tool_name: str, result: Any) -> dict[str, Any]:
    """统一执行结果 envelope。所有工具结果出 registry 时都过这层。"""
    if not isinstance(result, dict):
        log.warning(
            "工具 %r 返回了非 dict（%s）—— 自动 wrap 为 error。规范要求返 dict。",
            tool_name,
            type(result).__name__,
        )
        return {
            "status": "error",
            "error_code": _errs.NON_DICT_RESULT,
            "error": f"tool {tool_name!r} returned non-dict ({type(result).__name__})",
            "raw": str(result)[:500],
        }
    if result.get("status") == "error":
        patch: dict[str, Any] = {}
        # envelope 必须带一句**人和模型都读得懂的原因**。子进程类工具历来把
        # 失败写在 returncode / stderr_tail 里，envelope 没有 `error`，下游只
        # 剩一句 "Tool execution failed"（本机库 27 条 + 325 条连正文都没留下）。
        #
        # 补在这里而不是逐个工具里：这是唯一的派发口，MCP 工具、同事节点自己
        # 的工具（如 experiment 的 safe_run_bash）一行都不用改就都覆盖到 ——
        # 写名单的护栏，新工具默认漏过。
        if not result.get("error") and result.get("returncode") not in (None, 0):
            patch["error"] = _errs.command_failure_note(
                result.get("returncode"), result.get("stderr_tail"))
            patch.setdefault("error_code", _errs.COMMAND_FAILED)
        # 工具自己报的 error 默认按"框架说不"记 —— 它是**选择**返回 error 的，
        # 不是崩的。崩的那条路走上面 except，永远带 TOOL_EXCEPTION。
        if not result.get("error_code"):
            patch.setdefault("error_code", _errs.REJECTED)
        if patch:
            return {**result, **patch}
    if "status" not in result:
        log.warning(
            "工具 %r 返回的 dict 缺 `status` 字段 —— 自动包成 success。规范要求显式 status。",
            tool_name,
        )
        return {"status": "success", "data": result}
    return result
