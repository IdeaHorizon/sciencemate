"""跨 backend 的工具调用 / 内容修复层（backend 无关、可扩展）。

## 背景

不同 OpenAI-compatible 后端（官方 API / 自建 GPUStack / vLLM / Ollama /
llama.cpp / SGLang …）对 reasoning-model 的**工具调用 markup** 解析口径不一致。
理想情况下后端把模型输出的 tool-call markup 解析成结构化的 `message.tool_calls`
字段；但很多自建部署的解析器不稳，会把 markup 碎片**漏进 `message.content`**：

  - 高频（实测 GPUStack deepseek-v4-pro：61% 的 orchestrator turn）：content 前缀
    挂一个 dangling 闭合标签，如 `</scratchpad>正文…` 或 `</｜DSML｜tool_calls>`，
    多数时候 tool_calls 仍正确解析出来了，纯属**观感污染**。
  - 低频但更糟：整条 tool-call markup 没被结构化，`tool_calls` 为空、markup 全
    留在 content 里。上层（orchestrator / 节点 loop）一看"没有 tool_calls"就把
    这堆 markup 当**最终回答**显示给用户 —— 用户看到 `</write_scratchpad>` 这种
    残渣，以为 agent 卡了/坏了。

## 本模块做三件事

1. **恢复**：从漏进 content 的**完整** tool-call markup 里解析出结构化 tool_calls，
   让本该执行的工具真的执行，而不是当文本吐出来。
2. **清洗**：把 content 里残留的 tool-call / reasoning markup 碎片剥掉，
   用户只看到干净正文。
3. **判定协议失败**：content 只剩 tool-call 碎片、无正文、又没恢复出任何调用
   → `protocol_leak=True`，供上层重试 / 兜底，绝不把残渣当答案。

## 扩展方式（支持各种 backend）

每种后端的 markup"方言"是一个 `ToolCallDialect`。要支持一个新后端，往
`DEFAULT_DIALECTS` 加一个 dialect 即可，`recover_tool_calls()` 会依次尝试。
内置方言：

  - `XmlInvokeDialect`   —— Anthropic 风格 `<invoke><parameter>`，兼容
                            GPUStack 的 `<｜DSML｜invoke>` 变体（｜=U+FF5C）
  - `HermesDialect`      —— `<tool_call>{json}</tool_call>`（vLLM/Qwen/NousHermes）
  - `DeepSeekNativeDialect` —— `<｜tool▁calls▁begin｜>…<｜tool▁sep｜>NAME\n```json…`

纯函数、无 I/O、无网络 —— 完全可单测。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# ── 结果类型 ────────────────────────────────────────────────────────────────

@dataclass
class RecoveryResult:
    """recover_tool_calls 的返回。"""
    tool_calls: list[dict]                       # 最终 tool_calls（含恢复出来的）
    content: str | None                          # 清洗后的 content
    recovered: bool = False                      # 是否从 content 恢复出了调用
    protocol_leak: bool = False                  # 纯碎片、无正文、无调用 = 协议失败
    stripped: list[str] = field(default_factory=list)  # 剥掉的碎片（审计用）
    dialect: str | None = None                   # 命中的方言名（恢复时）
    # 认出工具名但参数一个没解析出来时的诊断信息 —— 排查后端方言用。markup 会被
    # 清洗掉，不在这里留一份原文就再也查不到（E2E-4 排查时的实际困境）。
    unparsed_tool: str | None = None
    unparsed_body_preview: str | None = None


# ── 方言抽象 ────────────────────────────────────────────────────────────────

class ToolCallDialect(Protocol):
    """一种后端的 tool-call markup 方言。"""
    name: str

    def extract(self, content: str) -> tuple[list[dict], str]:
        """从 content 里抽出完整 tool-call markup → (tool_calls, 去掉 markup 后的 content)。
        抽不到返回 ([], 原 content)。tool_calls 是 OpenAI 风格 dict。"""
        ...


def _mk_tool_call(index: int, name: str, arguments: dict | str) -> dict:
    """构造 OpenAI 风格 tool_call dict。id 用确定性值（不引随机，便于测试 + cache）。"""
    if isinstance(arguments, dict):
        args_str = json.dumps(arguments, ensure_ascii=False)
    else:
        args_str = arguments or "{}"
    return {
        "id": f"recovered_{index}",
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


class UnparsedToolMarkup(Exception):
    """markup 里认出了工具名、却一个参数都解析不出来。

    这是**解析失败**，不是"这个工具不需要参数"。恢复层遇到它必须交回未识别，
    让上层判 protocol_leak 重试 —— 派发一个空参数调用会让模型以为框架坏了。
    """

    def __init__(self, fn_name: str, body: str) -> None:
        super().__init__(f"tool={fn_name!r} 的 markup body 未能解析出任何参数")
        self.fn_name = fn_name
        self.body_preview = (body or "")[:300]


# ── XML-invoke / DSML 方言 ──────────────────────────────────────────────────
# Anthropic 风格：<invoke name="X"><parameter name="p">v</parameter>...</invoke>
# GPUStack 变体：标签名前缀 ｜DSML｜（｜=U+FF5C），如 <｜DSML｜invoke name="X">。
# 两者用同一套 regex（前缀可选）覆盖。

# 可选的标签前缀：GPUStack 的 ｜DSML｜（｜=U+FF5C），以及 XML 命名空间前缀
# （`antml:invoke` / `tool:parameter` 之类）。E2E-4 实测：后端吐了带命名空间的
# invoke/parameter，外层匹配上拿到了工具名、内层一个参数都没匹配上 —— 结果派发
# 了一串空参数调用，模型收到 "missing required positional argument"，合理地判定
# "框架的 XML 解析坏了" 然后放弃整个 run。
_DSML = r"(?:｜DSML｜|[A-Za-z_][\w.-]*:)?"
# E2E-4 二次实测：GPUStack/deepseek-v4-pro 真实吐出的 DSML 变体**不闭合任何标签**
# 且 parameter 带类型属性：
#     <｜DSML｜invoke name="run_node">
#     <｜DSML｜parameter name="node_type" string="true">data
#     <｜DSML｜parameter name="node_inputs" string="false">{JSON...}
# 旧 regex 要求 </invoke> / </parameter> 闭合 → 一个都匹不上 → 内容当叙述文本
# 放行 → 模型看到自己的调用没执行，自增计数重试了 23 轮（"连续十四轮…十五轮…"）。
# 而它当时想发的 node_inputs 是**完全正确**的（含 reused_inputs 声明指引）——
# 生产路线的决策对了，死在传输层。
# 改为开标签扫描：invoke 的 body 到闭合标签 / 下一个 invoke / 文本末尾；
# parameter 的值到闭合标签 / 下一个 parameter / body 末尾。两种形态（规范闭合 /
# 全不闭合）同一套逻辑。属性 `string="false"` 是方言自述的类型：值按 JSON 解析。
_INVOKE_OPEN_RE = re.compile(
    rf"<\s*{_DSML}invoke\s+name\s*=\s*[\"']([^\"']+)[\"']([^>]*)>")
_INVOKE_CLOSE_RE = re.compile(rf"</\s*{_DSML}invoke\s*>")
_PARAM_OPEN_RE = re.compile(
    rf"<\s*{_DSML}parameter\s+name\s*=\s*[\"']([^\"']+)[\"']([^>]*)>")
_PARAM_CLOSE_RE = re.compile(rf"</\s*{_DSML}parameter\s*>")


def _strip_toolcall_markup(text: str) -> str:
    """把 invoke / parameter 开闭标签整段抹掉（issue #267）。

    只在**已判定为未解析工具 markup** 的路径上调用 —— 那时这些标签确定不是
    正文，留着就是满屏残渣。标签之间夹的值（`>hypothesis`、`>{JSON}`）一并
    去掉：它们是参数值，不是给人看的话。
    """
    out = _PARAM_OPEN_RE.sub("\n", text)
    out = _PARAM_CLOSE_RE.sub("", out)
    out = _INVOKE_OPEN_RE.sub("\n", out)
    out = _INVOKE_CLOSE_RE.sub("", out)
    # 标签之间残留的裸值行（参数值）：只在整行看起来像 JSON / 单个 token 时删。
    kept = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("{", "[")) or (len(s.split()) == 1 and len(s) < 60):
            continue          # 参数值残留，不是叙述
        kept.append(ln)
    return "\n".join(kept)


def _extract_params(body: str) -> dict:
    """invoke body → 参数 dict。开标签扫描，不要求闭合。

    值的边界：闭合标签 / 下一个 parameter 开标签 / body 末尾，取最早者。
    `string="false"` 属性 = 方言自述"这是 JSON 不是字符串"：用 raw_decode 解析
    （吃掉前缀合法 JSON，容忍尾随杂文 —— 不闭合形态下最后一个参数的值一路延伸
    到文本末尾，后面可能跟着叙述）。解析失败保持字符串原样，让工具层报真实错误。
    """
    params: dict = {}
    popens = list(_PARAM_OPEN_RE.finditer(body))
    for j, pm in enumerate(popens):
        vstart = pm.end()
        vlimit = popens[j + 1].start() if j + 1 < len(popens) else len(body)
        seg = body[vstart:vlimit]
        pclose = _PARAM_CLOSE_RE.search(seg)
        value: object = (seg[:pclose.start()] if pclose else seg).strip()
        attrs = pm.group(2) or ""
        if 'string="false"' in attrs or "string='false'" in attrs:
            try:
                value, _ = json.JSONDecoder().raw_decode(str(value))
            except (ValueError, TypeError):
                pass
        params[pm.group(1).strip()] = value
    return params


class XmlInvokeDialect:
    name = "xml_invoke"

    def extract(self, content: str) -> tuple[list[dict], str]:
        calls: list[dict] = []
        spans: list[tuple[int, int]] = []
        opens = list(_INVOKE_OPEN_RE.finditer(content))
        for i, m in enumerate(opens):
            fn_name = m.group(1).strip()
            body_start = m.end()
            region_end = opens[i + 1].start() if i + 1 < len(opens) else len(content)
            region = content[body_start:region_end]
            close = _INVOKE_CLOSE_RE.search(region)
            body = region[:close.start()] if close else region
            span_end = body_start + (close.end() if close else len(region))
            params = _extract_params(body)
            if not params and body.strip():
                # **有 body 却一个参数都没解析出来 = 解析失败，不是"无参调用"。**
                # 这里以前照样 append 一个空参数调用 —— 比不恢复更糟：下游报
                # "missing required positional argument"，模型据此判定框架坏了
                # 然后放弃整个 run（E2E-4 实测，literature 都没跑起来就 blocked）。
                # 交回未识别 → 走 protocol_leak → 重试，让后端换个说法。
                # body 为空才是真的无参调用（kb_overview 这类），放行。
                raise UnparsedToolMarkup(fn_name, body)
            calls.append(_mk_tool_call(len(calls), fn_name, params))
            spans.append((m.start(), span_end))

        # ── issue #267（lujy 实测）：第一个 invoke **之前**的孤儿 parameter ──
        # 现场原文：
        #     <｜DSML｜parameter name="node_type" string="true">hypothesis
        #     <｜DSML｜parameter name="node_inputs" string="false">{...}
        #                                    ← 这里本该有个 invoke，丢了
        #     <｜DSML｜invoke name="run_node">
        #     <｜DSML｜parameter name="node_type" string="true">_curator
        # 前两个 parameter 没有前置 invoke（后端把 invoke 开标签吞了/截断了）。
        # 旧实现只沿 invoke 开标签迭代，这段整个被无视：既没恢复成调用，也没被
        # 剥掉 —— 直接当叙述文本流到用户屏幕上（lujy 看到的 markup 残渣），而且
        # `protocol_leak=False`，框架当正常回复**不重试**，模型以为自己发了调用、
        # 实际只跑了后半个。有的 project 会有的不会 = 取决于开标签有没有被吞。
        #
        # 这种残段**不能**猜工具名硬恢复（猜错就是拿错参数跑错节点）。正确处理是
        # 剥掉它并暴露给协议层：交回 UnparsedToolMarkup → protocol_leak → 重试，
        # 让后端重发一份完整的。
        _first = opens[0].start() if opens else len(content)
        _head = content[:_first]
        if _PARAM_OPEN_RE.search(_head):
            raise UnparsedToolMarkup("<orphan-parameters>", _head)

        if not calls:
            return [], content
        cleaned = _cut_spans(content, spans)
        return calls, cleaned


# ── Hermes 方言：<tool_call>{json}</tool_call> ──────────────────────────────

_HERMES_RE = re.compile(r"<\s*tool_call\s*>(.*?)</\s*tool_call\s*>", re.DOTALL)


class HermesDialect:
    name = "hermes"

    def extract(self, content: str) -> tuple[list[dict], str]:
        calls: list[dict] = []
        spans: list[tuple[int, int]] = []
        for m in _HERMES_RE.finditer(content):
            raw = m.group(1).strip()
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                continue
            fn_name = obj.get("name")
            if not fn_name:
                continue
            args = obj.get("arguments", obj.get("parameters", {}))
            calls.append(_mk_tool_call(len(calls), fn_name, args))
            spans.append((m.start(), m.end()))
        if not calls:
            return [], content
        return calls, _cut_spans(content, spans)


# ── DeepSeek 原生方言 ───────────────────────────────────────────────────────
# <｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>NAME\n```json\n{..}\n```<｜tool▁call▁end｜>...
# ｜=U+FF5C，▁=U+2581。

_DS_CALL_RE = re.compile(
    r"<｜tool▁call▁begin｜>\s*function\s*<｜tool▁sep｜>\s*"
    r"([A-Za-z0-9_.\-]+)\s*"
    r"```(?:json)?\s*(.*?)\s*```",
    re.DOTALL,
)
_DS_OUTER_RE = re.compile(
    r"<｜tool▁calls▁begin｜>.*?(?:<｜tool▁calls▁end｜>|$)",
    re.DOTALL,
)


class DeepSeekNativeDialect:
    name = "deepseek_native"

    def extract(self, content: str) -> tuple[list[dict], str]:
        calls: list[dict] = []
        for m in _DS_CALL_RE.finditer(content):
            fn_name = m.group(1).strip()
            raw = m.group(2).strip()
            try:
                args = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                args = raw          # 塞不进 json 就原样带上，至少工具能看到
            calls.append(_mk_tool_call(len(calls), fn_name, args))
        if not calls:
            return [], content
        # 移除整个 <｜tool▁calls▁begin｜>…end｜> 外框（含没闭合的残尾）
        spans = [(m.start(), m.end()) for m in _DS_OUTER_RE.finditer(content)]
        return calls, _cut_spans(content, spans)


DEFAULT_DIALECTS: list[ToolCallDialect] = [
    XmlInvokeDialect(),
    HermesDialect(),
    DeepSeekNativeDialect(),
]


# ── 碎片清洗 ────────────────────────────────────────────────────────────────
# 只清"明显是 markup 的 dangling 标签"：标签名在已知集合里，或含 ｜DSML｜ / ▁。
# 保守：只剥 content 首尾连续的这类标签，不动正文中间（避免误伤讨论标签本身的文本）。

# reasoning 包裹标签：漏一个闭合 = 观感问题，不算协议失败
_REASONING_TAGS = {"think", "thinking", "scratchpad", "reason", "reasoning"}
# tool-call markup 标签：漏这些且无正文无调用 = 协议失败
_TOOLCALL_TAGS = {
    "invoke", "parameter", "tool_call", "tool_calls",
    "write_scratchpad", "function_calls", "antml:invoke", "antml:parameter",
}

# 一个 dangling markup 标签：</name> 或 <name ...>，name 允许 ｜DSML｜ 前缀 / ▁
_TAG_RE = re.compile(
    r"</?\s*(?:｜DSML｜)?([\w.▁:｜|-]+)(?:\s+[^>]*)?/?\s*>",
)


def _classify_tag(tag_text: str) -> str | None:
    """返回 'reasoning' / 'toolcall' / None（不是已知 markup 标签，不动它）。"""
    stripped = tag_text.strip()
    # 含 DSML / ▁ / 全角竖线特殊分隔符的一律当 tool-call markup（provider 专属 token，
    # 正常正文不会出现）—— 直接看整段标签文本，不依赖 name 解析。
    if "▁" in stripped or "｜" in stripped or "DSML" in stripped:
        return "toolcall"
    m = _TAG_RE.fullmatch(stripped)
    if not m:
        return None
    name = m.group(1).lower().lstrip("/")
    if name in _REASONING_TAGS:
        return "reasoning"
    if name in _TOOLCALL_TAGS:
        return "toolcall"
    return None


_LEADING_TAG_RE = re.compile(r"^\s*(</?[^<>]{1,80}>)\s*")
_TRAILING_TAG_RE = re.compile(r"\s*(</?[^<>]{1,80}>)\s*$")


def _strip_dangling(content: str) -> tuple[str, list[str], bool]:
    """剥 content 首尾连续的 markup 碎片。
    返回 (清洗后 content, 剥掉的碎片列表, 是否剥掉过 tool-call 类碎片)。"""
    stripped: list[str] = []
    saw_toolcall = False

    def _peel(pattern: re.Pattern, text: str) -> str:
        nonlocal saw_toolcall
        while True:
            m = pattern.search(text)
            if not m:
                return text
            tag = m.group(1)
            kind = _classify_tag(tag)
            if kind is None:
                return text            # 不是已知 markup → 停，别动正文
            stripped.append(tag)
            if kind == "toolcall":
                saw_toolcall = True
            text = (text[: m.start()] + text[m.end():]) if pattern is _TRAILING_TAG_RE \
                else text[m.end():]
            if not text:
                return text

    content = _peel(_LEADING_TAG_RE, content)
    content = _peel(_TRAILING_TAG_RE, content)
    return content, stripped, saw_toolcall


def _cut_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """按 [start,end) 区间从 text 里剜掉多段，保留其余。"""
    if not spans:
        return text
    spans = sorted(spans)
    out = []
    prev = 0
    for s, e in spans:
        if s < prev:      # 重叠保护
            continue
        out.append(text[prev:s])
        prev = e
    out.append(text[prev:])
    return "".join(out)


# ── 顶层入口 ────────────────────────────────────────────────────────────────

def recover_tool_calls(
    content: str | None,
    tool_calls: list[dict] | None,
    *,
    dialects: list[ToolCallDialect] | None = None,
) -> RecoveryResult:
    """修复一次 LLM 响应的 content + tool_calls。

    - tool_calls 已有：不覆盖，只清洗 content 里漏的碎片。
    - tool_calls 为空但 content 里有完整 markup：恢复成结构化 tool_calls。
    - 清洗后 content 只剩 tool-call 碎片、无正文、又没恢复出调用 → protocol_leak。

    永不抛异常：任何解析失败都退化成"原样返回 + 尽量清洗"。
    """
    existing = list(tool_calls or [])
    if content is None:
        return RecoveryResult(tool_calls=existing, content=None)

    dialects = dialects if dialects is not None else DEFAULT_DIALECTS
    recovered_calls: list[dict] = []
    dialect_hit: str | None = None
    work = content

    # 只有当后端没给结构化 tool_calls 时才尝试恢复（避免重复执行）
    unparsed: UnparsedToolMarkup | None = None
    if not existing:
        for d in dialects:
            try:
                calls, cleaned = d.extract(work)
            except UnparsedToolMarkup as e:
                # 认出了工具名、参数一个没解析出来 —— 交回未识别，但**留痕**。
                # 静默 continue 的话，下次还是查不到后端到底吐的什么方言
                # （E2E-4 排查时 markup 已被清洗，原始文本无处可寻）。
                unparsed = e
                continue
            except Exception:
                continue
            if calls:
                recovered_calls.extend(calls)
                work = cleaned
                dialect_hit = d.name
                break

    # issue #267：markup 解析失败时，**残段绝不能流到用户屏幕上**。
    # lujy 现场看到的就是这个：孤儿 parameter 段没被任何方言吃掉，原样当叙述
    # 文本显示（满屏 `<｜DSML｜parameter name=…>`）。既然已经判定它是未解析的
    # 工具 markup（unparsed），就整段剥掉——内容留在 transcript 里可查，
    # 但不当回复展示。
    if unparsed is not None:
        work = _strip_toolcall_markup(work)

    cleaned_content, stripped, saw_toolcall_frag = _strip_dangling(work)
    final_content = cleaned_content if cleaned_content.strip() else ""

    final_calls = existing + recovered_calls
    protocol_leak = (
        not final_calls
        and (
            not final_content.strip() and saw_toolcall_frag
            # 参数解析失败也是协议失败：正文可能还有一段像样的叙述，但它想调的
            # 工具没调成。不判 leak 的话这一轮就白跑，模型也不知道自己没调成。
            or unparsed is not None
        )
    )

    return RecoveryResult(
        tool_calls=final_calls,
        content=(final_content or None),
        recovered=bool(recovered_calls),
        protocol_leak=protocol_leak,
        stripped=stripped,
        dialect=dialect_hit,
        unparsed_tool=(unparsed.fn_name if unparsed else None),
        unparsed_body_preview=(unparsed.body_preview if unparsed else None),
    )


# ══════════════════════════════════════════════════════════════════════════
# issue #184：args JSON 解析失败的有界重发 + 协议失败熔断
# ══════════════════════════════════════════════════════════════════════════
#
# 上面那套（方言恢复 / 碎片清洗）解决的是"markup 漏进 content"。qinp 2026-07 带
# 证据实测暴露了另外两个洞，都在**结构化 tool_call 已经拿到之后**：
#
#   1. args JSON 解析失败：tool_call 结构上存在，但 `function.arguments` 反序列化
#      挂了（实测 `Expecting ',' delimiter: line 1 column 7711 (char 7710)`，一份
#      7.7KB 的 review_critique 正文）。当前 agent_loop 只回一句干巴巴的
#      "参数 JSON 解析失败：<原始报错>"，模型拿到后**不会自我纠正**，下一轮直接空
#      stop。缺的是：把出错**位置附近的原文**回喂 + 明确的重发指令 + 一个界。
#   2. 完全没有熔断：`_PROTOCOL_LEAK_RETRIES=2` 只在**单次 call 内**重试，跨 turn
#      一次都不数。`HARNESS_DEFAULT_MAX_TURNS=0`（无限）下实测空转 ~40 分钟、
#      ~5300 次失败调用，把 conversation.json 灌到 1101 条消息 / 138K token，
#      项目**无法 resume**。
#
# 这两块都做成**纯判定 / 纯构造**的可测试单元，真正的重发循环与停机动作在
# run_loop（agent_loop.py）里，由它调用本模块。设计成"外部循环喂状态、这里给
# 判定"，与仓库既有的 `_repeated_producing_failure`（chat.py，producing 节点重复
# 失败熔断，commit 2cbd502）同一风格：连续同类失败计数 → warn 档 → abort 档，
# 中间出现一次健康信号即断链。

# agent_loop 在 args 解析失败时写进 result["error"] 的前缀。**必须**与
# agent_loop 里的字面量一致 —— executor 的失败分类靠它区分"调用成功执行、
# 工具自己返回 error"和"调用根本没执行、args 解析就挂了"。
MALFORMED_ARGS_ERROR_PREFIX = "参数 JSON 解析失败："

# 熔断停机时写进 final_text 的标记。executor 认这个标记把 run 归到 provider
# 协议故障，而不是让它伪装成一次普通的"没产出" incomplete。
PROTOCOL_BREAKER_MARKER = "[provider tool-call 协议熔断]"

# 同一个工具的 args 连续解析失败允许重发几次（不含首次）。超界 = 不再让模型
# 原样重试，改成要求它换策略（拆小 / 先落盘再引用）。
_MALFORMED_ARGS_MAX_RETRIES = int(
    os.getenv("HARNESS_MALFORMED_ARGS_RETRIES", "2") or 2)
# 连续 N 次**同类**不可恢复协议失败：warn 档（注入硬指令）/ abort 档（停机）。
_PROTOCOL_FAIL_WARN = int(os.getenv("HARNESS_PROTOCOL_FAIL_WARN", "3") or 3)
_PROTOCOL_FAIL_ABORT = int(os.getenv("HARNESS_PROTOCOL_FAIL_ABORT", "5") or 5)
# 连续 N 次协议失败但**类型在换**（一会儿 leak 一会儿空 stop）——同样是 provider
# 坏了，只是签名不稳，用一个更宽的阈值兜住，避免靠交替签名绕过熔断。
_PROTOCOL_FAIL_ABORT_ANY = int(
    os.getenv("HARNESS_PROTOCOL_FAIL_ABORT_ANY", "8") or 8)


def is_malformed_args_error(result: Any) -> bool:
    """判断一个 tool result 是不是"args JSON 解析失败"（而非工具业务错误）。

    两条都认：新路径打的结构化标记 `malformed_args=True`，以及老/裸路径的
    error 文案前缀（agent_loop 未接线时也能被 executor 正确分类）。
    """
    if not isinstance(result, dict):
        return False
    if result.get("malformed_args") is True:
        return True
    if result.get("status") != "error":
        return False
    return str(result.get("error") or "").startswith(MALFORMED_ARGS_ERROR_PREFIX)


def _error_offset(error: Any) -> int | None:
    """从 json.JSONDecodeError（或它的字符串形式）里取出出错的字符偏移。"""
    pos = getattr(error, "pos", None)
    if isinstance(pos, int):
        return pos
    m = re.search(r"char (\d+)", str(error))
    return int(m.group(1)) if m else None


def malformed_args_excerpt(raw_args: str, error: Any, *, window: int = 120) -> str:
    """截取出错位置前后的原文，并用 ⟪HERE⟫ 标出断点。

    为什么必须有：实测出错的是 7.7KB 单行 JSON 的第 7710 个字符。只回一句
    "column 7711" 等于没说 —— 模型没法定位自己写坏了哪儿，于是不改、直接放弃。
    """
    raw = raw_args if isinstance(raw_args, str) else str(raw_args)
    pos = _error_offset(error)
    if pos is None or not raw:
        return raw[:2 * window]
    lo = max(0, pos - window)
    hi = min(len(raw), pos + window)
    return (("…" if lo > 0 else "") + raw[lo:pos] + "⟪HERE⟫" + raw[pos:hi]
            + ("…" if hi < len(raw) else ""))


@dataclass
class MalformedArgsRepair:
    """args 解析失败的**有界**重发簿记（per-run，按工具名分别计数）。

    纯状态机 + 消息构造，不含循环、不做 I/O。外部（run_loop）的用法：

        repair = MalformedArgsRepair()          # run 开始时建一个
        ...
        except json.JSONDecodeError as e:
            args = {}
            result = repair.feedback(tool_name=tool_name, raw_args=raw_args, error=e)
        else:
            repair.note_success(tool_name)      # 一次成功即清零（不是卡死）

    `feedback()` 返回的 dict 直接当 tool result 用：它会被 agent_loop 原样写成
    tool 消息回喂给模型 —— 这就是"把解析错误回喂 + 让模型重发"的机制本身，无需
    额外的消息通道。`retryable` 字段告诉外部循环还在不在界内。
    """
    max_retries: int = _MALFORMED_ARGS_MAX_RETRIES
    attempts: dict[str, int] = field(default_factory=dict)

    def note_success(self, tool_name: str) -> None:
        """该工具本轮 args 解析成功 → 计数清零。一次成功即完成，不累计历史。"""
        self.attempts.pop(tool_name, None)

    def exhausted(self, tool_name: str) -> bool:
        return self.attempts.get(tool_name, 0) > self.max_retries

    def feedback(self, *, tool_name: str, raw_args: Any, error: Any) -> dict:
        """记一次失败，返回回喂给模型的 tool result。"""
        n = self.attempts.get(tool_name, 0) + 1
        self.attempts[tool_name] = n
        over = n > self.max_retries
        excerpt = malformed_args_excerpt(raw_args or "", error)
        raw_len = len(raw_args) if isinstance(raw_args, str) else 0
        if over:
            hint = (
                f"这是第 {n} 次 {tool_name} 的参数解析失败，已超出重发上限"
                f"（{self.max_retries} 次）。**不要再原样重发同一份参数** —— 同样的写法"
                "只会同样地坏。改用下面任一策略：(a) 把大字段拆成多次小调用；"
                "(b) 先用最小合法参数建立产物，再分次追加内容；"
                "(c) 若确属内容过长导致的截断，缩短本次要写入的正文。"
            )
        else:
            hint = (
                f"这是第 {n}/{self.max_retries} 次重发机会。请**重新发起同一个工具调用**，"
                "把参数写成合法 JSON。注意断点附近：字符串内部的引号 / 换行 / 反斜杠必须转义"
                "（\\\" \\n \\\\），对象与数组元素之间不能漏逗号，末尾不能多逗号。"
                "长正文尤其容易在中途断掉 —— 若不确定能写完整，先缩短本次正文。"
            )
        return {
            "status": "error",
            "error": f"{MALFORMED_ARGS_ERROR_PREFIX}{error}",
            "malformed_args": True,          # 结构化标记（executor 分类靠它 / 前缀）
            "retryable": not over,
            "attempt": n,
            "max_retries": self.max_retries,
            "args_length": raw_len,
            "excerpt": excerpt,
            "hint": hint,
        }


# ── 协议失败签名 + 熔断器 ───────────────────────────────────────────────────

def protocol_failure_signature(
    *,
    protocol_leak: bool = False,
    leak_kind: str | None = None,
    has_tool_calls: bool = False,
    content: str | None = None,
    malformed_args_tools: list[str] | tuple[str, ...] = (),
) -> str | None:
    """把一个 turn 归成一个**协议失败签名**；这一轮健康则返回 None。

    签名而非布尔：熔断只对**同类**连续失败开火（"一直挂在同一处"才说明 provider
    坏了；偶发不同错混着出现是另一回事，由更宽的 any 阈值兜）。

    ⚠️ 接线注意：在**工具调度之后**的调用点必须显式传 `has_tool_calls=True`
    —— 否则"这一轮正常调了工具、没有任何 args 解析失败"会被误判成 blank_stop。
    """
    tools = sorted({t for t in malformed_args_tools if t})
    if tools:
        return "malformed_args:" + ",".join(tools)
    body = (content or "").strip()
    if has_tool_calls or body:
        return None                      # 有调用或有正文 = 这一轮 provider 是活的
    if protocol_leak:
        return f"provider_leak:{leak_kind or 'markup'}"
    return "blank_stop"                  # 无调用、无正文 = 纯空转


@dataclass
class ProtocolBreakerDecision:
    """一次 record() 的判定结果。"""
    signature: str | None
    streak: int = 0          # 当前签名连续出现次数
    any_streak: int = 0      # 连续协议失败次数（不分签名）
    should_warn: bool = False
    should_abort: bool = False
    diagnosis: str = ""


class ProtocolFailureBreaker:
    """连续同类协议失败 → 停 run。

    **与 max_turns 无关**：这是本改动的核心要求。`HARNESS_DEFAULT_MAX_TURNS=0`
    （无限轮）下没有任何东西拦得住空转，实测烧了 ~5300 次失败调用、把
    conversation.json 灌到无法 resume。熔断按"连续失败次数"计，不看轮数上限。

    用法（外部循环，每 turn 恰好调一次 record；健康轮传 None 断链）：

        breaker = ProtocolFailureBreaker()
        ...
        d = breaker.record(protocol_failure_signature(...))
        if d.should_abort:
            return LoopResult(final_text=d.diagnosis, status="failed", ...)
        if d.should_warn:
            messages.append(framework_notice(d.diagnosis))   # ← 不是 role="system"
    """

    def __init__(
        self,
        *,
        warn_at: int | None = None,
        abort_at: int | None = None,
        abort_any_at: int | None = None,
    ) -> None:
        self.warn_at = _PROTOCOL_FAIL_WARN if warn_at is None else warn_at
        self.abort_at = _PROTOCOL_FAIL_ABORT if abort_at is None else abort_at
        self.abort_any_at = (_PROTOCOL_FAIL_ABORT_ANY if abort_any_at is None
                             else abort_any_at)
        self.signature: str | None = None
        self.streak = 0
        self.any_streak = 0

    def record(self, signature: str | None) -> ProtocolBreakerDecision:
        if signature is None:
            # 健康的一轮 = 断链。provider 还能正常干活，之前的失败属偶发。
            self.signature = None
            self.streak = 0
            self.any_streak = 0
            return ProtocolBreakerDecision(signature=None)

        self.any_streak += 1
        if signature == self.signature:
            self.streak += 1
        else:
            self.signature = signature
            self.streak = 1

        should_abort = (
            (self.abort_at > 0 and self.streak >= self.abort_at)
            or (self.abort_any_at > 0 and self.any_streak >= self.abort_any_at)
        )
        should_warn = (not should_abort
                       and self.warn_at > 0 and self.streak >= self.warn_at)
        return ProtocolBreakerDecision(
            signature=signature,
            streak=self.streak,
            any_streak=self.any_streak,
            should_warn=should_warn,
            should_abort=should_abort,
            diagnosis=self._diagnosis(signature, should_abort, should_warn),
        )

    # ── 诊断文案：说清"是 provider 坏了、不是节点写得差" ──────────────────
    def _diagnosis(self, signature: str, abort: bool, warn: bool) -> str:
        kind = signature.split(":", 1)[0]
        what = {
            "malformed_args": "工具参数 JSON 反复解析失败（模型/后端把 arguments 写坏或截断）",
            "provider_leak": "后端把 tool-call markup 当普通文本返回、未结构化",
            "blank_stop": "模型连续返回空响应且不调工具（空转）",
        }.get(kind, "tool-call 协议失败")
        if abort:
            return (
                f"{PROTOCOL_BREAKER_MARKER} 已连续 {self.streak} 次同类协议失败"
                f"（签名 {signature}；不分类型连续 {self.any_streak} 次）：{what}。"
                "继续跑只会堆积无效调用 —— 实测同类空转烧掉 ~5300 次调用、把 "
                "conversation 灌到无法 resume，所以本 run 在这里主动停机"
                "（与 max_turns 无关）。这是 **provider / 后端兼容性**问题，不是节点 "
                "prompt 或产出质量问题：请检查该 endpoint 的 tool-call 解析配置、"
                "或换一个 backend/模型重跑。阈值可用 HARNESS_PROTOCOL_FAIL_ABORT 调整。"
            )
        if warn:
            return (
                f"⚠️ tool-call 协议失败连续 {self.streak} 次（签名 {signature}）：{what}。"
                "原样重试大概率还是同样结果。本轮请改变做法：把参数拆小、缩短单次写入的"
                "长正文，或先产出最小合法产物再追加。"
                f"连续 {self.abort_at} 次将自动停机。"
            )
        return ""
