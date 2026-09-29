"""极简 OpenAI-compatible LLM 客户端。

`LLMClient` 通过 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 环境变量
配置任意 OpenAI-compatible provider：

  - DeepSeek（默认）
  - OpenAI
  - OpenRouter（一个 key 调 200+ 模型）
  - 本地 ollama / vLLM / LM Studio
  - 自托管 LLM
  - Anthropic（要 OpenAI-compatible proxy）

往 `.env` 写对应 base_url + model 就能切。**协议规范**就是 OpenAI
`POST /v1/chat/completions`；不规范的 provider 要新写 client class，
保持 `chat()` 签名一致。

向后兼容：`DeepSeekClient` 是 `LLMClient` 的别名；`DEEPSEEK_API_KEY`
等老 env var 在 `LLM_*` 缺省时仍生效。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("llm")


# ── retry 策略 ───────────────────────────────────────────────────────────────
#
# v2.x：dogfood 实测 LLM provider（Maas / DeepSeek / OpenAI 都遇过）偶发
# 短时网络抖动（TLS 握手失败 / 5xx / read timeout），节点跑几小时被一次
# blip 干死所有工作。加 retry 兜住 transient 错。
#
# **重试** 的：
#   - httpx.ConnectError / ConnectTimeout / ReadTimeout / ReadError /
#     WriteTimeout / PoolTimeout / RemoteProtocolError —— 网络瞬时
#     （ReadError：2026-07 实测 writing 节点长链路 run 里 provider 读响应
#     阶段断流没有归到任何已有类别，issue #105 → 补上）
#   - HTTP 429（rate limit；遵守 Retry-After header）
#   - HTTP 5xx（server 端瞬时）
# **不重试** 的：
#   - HTTP 4xx 除 429（bad request / auth fail / quota 耗尽 —— 重试白费）
#   - JSON parse error（响应坏，重试也是坏）
#
# 默认 3 次（指数退避 1s/2s/4s + jitter，最长单次 30s）。失败抛原异常给
# caller，让 agent_loop 决定是不是把整个 run cancel 掉。

_RETRYABLE_HTTPX = (
    httpx.ConnectError, httpx.ConnectTimeout,
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
    httpx.RemoteProtocolError, httpx.ReadError,
)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_BACKOFF_SECONDS = 30.0

# 429 单独一套退避参数。5xx / 网络抖动是"服务端打了个嗝"，秒级重试就够；
# 429 不是——node20 实测拿到的是 GPUStack 的
#   {"message":"Concurrency limit exceeded for user, please retry later"}
# 这是**并发位被占满**，占位的是别人一次几十秒到几分钟的生成。用 1s/2s/4s
# 去重试，三次加起来等 7 秒，必然三次全撞墙，然后整个 run 判死。
#
# 退避得按"对方那次生成多久跑完"的量级来：5/10/20/40/80s，封顶 120s。
# provider 给了 Retry-After 就听它的（同样放宽到 120s —— 原来卡在 30s，
# 对方说"等 60 秒"我们 30 秒就冲上去，等于没听）。
_RATE_LIMIT_BACKOFF_BASE = 5.0
_RATE_LIMIT_MAX_BACKOFF_SECONDS = 120.0
_RATE_LIMIT_MIN_RETRIES = 5

# ── 重试预算按"扛多久"算，不按"试几次"算（2026-08-10）────────────────────────
#
# 上面那段 429 的分析对了一半，然后**只修了 429**：5xx 仍是 1s/2s/4s，
# 三次加起来 7 秒。而 5xx 的真实成因是 GPUStack 节点重启 / 模型重载 /
# OOM 后拉起 —— 那是**几十秒到几分钟**的量级，不是"打了个嗝"。
#
# 实测代价（E2E v25）：一轮跑了 3 小时、做完 3 个真实 LAMMPS 生产模拟的实验，
# 死在"后端有 7 秒没响应"上。我事后直接探那个端点：HTTP 200，2 秒，回 pong
# —— 它只是当时短暂不可用。
#
# 更根本的错在**单位**：`max_retries` 是按"一次 API 调用"设的预算，而一次闲聊
# 和一个跑了 3 小时的实验共用同一个 7 秒。真正该问的是"这次失败会损失多少，
# 所以值得扛多久"。所以加一层**时间预算**：在预算内就继续按阶梯退避重试，
# 与试了几次解耦。
#
#   闲聊 / 一次性调用    默认 60s   —— 用户在等，扛太久不如快点报错
#   节点 run（长任务）   默认 600s  —— 已经投入几小时，值得等一次重启
#
# 次数上限仍在（防止对方永久 500 时无限打），时间预算是**并行**的第二个闸：
# 谁先到都停。
_TRANSIENT_BUDGET_ENV = "LLM_RETRY_BUDGET_SECONDS"
_DEFAULT_TRANSIENT_BUDGET_S = 60.0
#: 5xx / 网络抖动的阶梯。原来是 1/2/4（继承自"打个嗝"的假设），改成与 429
#: 同量级 —— 事故证明服务端不可用的时间尺度就是这么长。
_TRANSIENT_BACKOFF_BASE = 5.0
_TRANSIENT_MAX_BACKOFF_SECONDS = 60.0
_TRANSIENT_MIN_RETRIES = 5


def parse_provider_error(status: int, body: str,
                         retry_after: str | None = None) -> dict:
    """把 provider 的错误响应解析成机器可读的几个字段。

    ## 为什么（issue #490）

    429 只记下"429"或那句 `Concurrency limit exceeded` 时，**没人分得清限的是
    TPM、RPM 还是并发位** —— 而这三种的处置完全不同（降并发 / 降请求频率 /
    砍 max_tokens 或分批）。provider 其实在响应体里说了，我们只是没留下来。

    兼容两种常见形状：`{"error": {...}}`（OpenAI 系）与顶层
    `{"message": ..., "code": ...}`（GPUStack 等网关）。解析不了就只留状态码
    和截断原文，绝不猜。

    ⚠️ 只解析 body 与 `Retry-After`，**不碰任何请求头** —— API key 不进日志。
    """
    out: dict = {"status": status}
    if retry_after:
        out["retry_after"] = str(retry_after)
    text = (body or "").strip()
    payload: object = None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        err = err if isinstance(err, dict) else payload
        for key, field in (("type", "error_type"), ("code", "error_code"),
                           ("message", "error_message")):
            value = err.get(key)
            if value not in (None, ""):
                out[field] = str(value)[:300]
    if "error_message" not in out and text:
        out["error_message"] = text[:300]
    return out


class LLMHTTPError(RuntimeError):
    """provider 返回了 HTTP >= 400。

    继承 RuntimeError：既有调用方（含平台侧 `run_failures` 的文案解析）**逐字
    依赖**原来的 `"LLM API HTTP <code>: <body>"` 消息，这里只是在同一条异常上
    多挂机器可读的字段 —— 让"这是不是瞬态故障"（#480）和"限的到底是什么"
    （#490）都不必再去正则解析人话。
    """

    def __init__(self, status: int, message: str, *, body: str = "",
                 retry_after: str | None = None, model: str | None = None,
                 payload: dict | None = None) -> None:
        self.status = status
        self.detail = parse_provider_error(status, body or message, retry_after)
        if model:
            self.detail["model"] = model
        if payload is not None:
            # 限流常常跟"这次请求多大"直接相关（TPM / 上下文长度）。粗估就够用：
            # 序列化长度 / 4。标成 est_ 前缀，别让它被当成精确计费数。
            try:
                self.detail["est_prompt_tokens"] = len(
                    json.dumps(payload, ensure_ascii=False)) // 4
            except (TypeError, ValueError):
                pass
        self.retry_after = retry_after
        super().__init__(message)


#: context 超限 400 的判别与取数（issue #710）。OpenAI 系措辞：
#: "This model's maximum context length is 262144 tokens. However, you requested
#:  16384 output tokens and your prompt contains at least 245761 input tokens…"
_CTX_OVERFLOW_HINTS = ("maximum context length", "context length",
                       "context_length_exceeded", "input tokens")
_CTX_WINDOW_RE = re.compile(r"maximum context length is (\d+)")
_CTX_INPUT_RE = re.compile(r"(?:contains at least|at least) (\d+) input tokens?"
                           r"|(\d+) input tokens")


def context_overflow_numbers(exc: BaseException) -> tuple[int, int] | None:
    """这是不是 context 超限 400；是则返回 (服务端窗口, 服务端实收 input)。

    取不到的数字用 0 占位。返回 None = 不是 context 超限，别按容量问题处理。
    服务端报错正文是**权威测量**（窗口多大、这份 prompt 到底多少 token）——
    浪费它等于框架手握真值却继续用自己的错误估算（[[容量后验]] 同一原则）。
    """
    status = getattr(exc, "status", None)
    if status != 400:
        return None
    detail = getattr(exc, "detail", None) or {}
    text = " ".join(str(detail.get(k) or "") for k in
                    ("error_message", "error_code", "error_type"))
    if not text.strip():
        text = str(exc)
    lowered = text.lower()
    if not any(h in lowered for h in _CTX_OVERFLOW_HINTS):
        return None
    window = 0
    m = _CTX_WINDOW_RE.search(text)
    if m:
        window = int(m.group(1))
    server_input = 0
    m = _CTX_INPUT_RE.search(text)
    if m:
        server_input = int(next(g for g in m.groups() if g))
    return window, server_input


def is_transient_provider_error(exc: BaseException) -> bool:
    """这个异常是不是 **provider 侧的瞬态故障**（而不是节点自己的问题）。

    重试策略本来就有这份名单（`_RETRYABLE_HTTPX` / `_RETRYABLE_STATUS`），
    但它只活在重试循环里：预算耗尽后异常照旧往上抛，而**上面没有任何一层
    认得出它是什么**（issue #480）。于是 provider 断流被记成节点失败，
    orchestrator 据此去退回上游补料 / 判 blocked，全都在解一个不存在的问题。

    唯一真相源就放在这里 —— 判据与重试策略共用同一份名单，不另开抄件。
    """
    if isinstance(exc, _RETRYABLE_HTTPX):
        return True
    status = getattr(exc, "status", None)
    return isinstance(status, int) and status in _RETRYABLE_STATUS


def describe_provider_error(exc: BaseException) -> str:
    """给人看的一行故障说明：类型 + 状态码 + provider 自己说的限流原因。

    限流细节（error.type / error.code / Retry-After）来自 `LLMHTTPError.detail`
    —— 有就摆出来，没有就退回截断原文（issue #490）。
    """
    status = getattr(exc, "status", None)
    head = type(exc).__name__ + (f"(HTTP {status})" if isinstance(status, int) else "")
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        bits = [f"{k}={detail[k]}" for k in ("error_type", "error_code",
                                             "retry_after", "model")
                if detail.get(k)]
        message = str(detail.get("error_message") or "")[:300]
        tail = ("; ".join(bits) + " | " if bits else "") + message
        return f"{head}: {tail}" if tail else head
    text = str(exc).strip()
    return f"{head}: {text[:300]}" if text else head


@dataclass
class LLMMessage:
    """对话中的一条消息。"""
    role: str                       # "system" | "user" | "assistant" | "tool"
    content: str | None = None      # 文本内容
    tool_calls: list[dict] | None = None   # 调用工具的 assistant 轮次会有
    tool_call_id: str | None = None        # 工具结果轮次用
    name: str | None = None                # 工具结果轮次中的工具名
    # Reasoning model 支持（DeepSeek V4-Pro / V4-Flash 等）：每条 assistant
    # 消息可能带 reasoning_content；下一轮请求时必须原样回传给 API，否则 400。
    reasoning_content: str | None = None


# ── 框架中途发声的唯一形态：user 角色 + 闭合信封 ──────────────────────────
#
# 为什么不是 role="system"：会话**中段**的 system 消息在训练分布里几乎不存在，
# 模型无法归属说话人，于是当成"自己没说完的话"接着往下写。
# 2026-08-17 实测（积算 deepseek-v4-pro，真实 hypothesis harness + 4 条真实
# hook 注入，两轮共 14 次）：
#     中段 role=system  → 13/14 的回复以复述注入文本的尾巴开头，平均输出 191 token
#     同内容 role=user  →  0/14 复述，平均输出 61 token
# 屏幕上那面「我来判断」复读墙，第一句正是在接框架注入的话往下说。
# llm.py 里的 scaffold 复读剥离是**事后擦显示**，擦不掉已经花掉的 token，
# 也擦不掉模型被自己的复述带偏 —— 所以在发声侧解决，不在清洗侧。
#
# 为什么要信封（而不是裸 user 消息）：换成 user 角色之后，框架的话和**人类
# 用户的话**混在同一个角色里了 —— 这正是"身份/上下文两件事一条规则"那类
# 事故的温床。信封是机械可辨的归属标记：闭合块 + 明写非用户发言。
FRAMEWORK_NOTICE_OPEN = "<framework-notice>"
FRAMEWORK_NOTICE_CLOSE = "</framework-notice>"
_FRAMEWORK_NOTICE_ATTRIBUTION = "（研究框架自动注入的状态与提示，不是用户发言）"


def opening_system_prompt(content: str) -> LLMMessage:
    """**某次 LLM 调用的开篇 system prompt** —— 唯一合法的 system 角色用途。

    ⚠️ 名字里的 `opening` 不是修辞。第一版叫 `system_prompt`，而
    `consult_other_model` 恰好有个同名参数（`system_prompt: str | None`）——
    函数体内参数遮蔽了导入的函数，`system_prompt(sys_msg)` 变成 `None(...)`。
    静态看不出来，import 也不报错，只有真调到那一行才炸，而错误还被上层
    吞成了一句无关的 "未知 model_name"。公共 helper 的名字要避开常见业务参数名。

    与 `framework_notice` 的区别不是语气，是位置：这条是一次调用的**第一条**
    消息（节点的系统提示、压缩器自己的指令、cross_model 的子调用），模型对
    "开篇 system"有充分的训练分布。而会话**中途**冒出来的 system 消息在训练
    分布里几乎不存在，模型无法归属说话人，会当成"自己没说完的话"接着写。

    存在这个函数是为了让护栏能扫盘：合法用途都走它，于是**裸的
    `LLMMessage(role="system")` 一律判违规**，新写的代码默认被覆盖。
    """
    return LLMMessage(role="system", content=content)


def _only_an_opening_system_message(messages: list[LLMMessage]) -> list[LLMMessage]:
    """会话**中段**的 system 消息不上线 —— 就地包成 framework notice。

    `opening_system_prompt` 的文档说"存在这个函数是为了让护栏能扫盘：合法用途
    都走它，于是裸的 `LLMMessage(role="system")` 一律判违规"。**那道护栏一直没
    写**，于是 chat.py 里四处照旧往消息尾巴上 append system 消息
    （[[feedback_absent_check_looks_like_passed_check]] 的形状：不在场的检查和
    通过的检查长得一样）。

    代价两笔，2026-09-15 同一天各兑现一次：
      - 模型侧：中段 system 在训练分布里几乎不存在 → 13/14 的回复复读注入文本
        （上面那段注释的实测数字）。
      - 协议侧：严格的网关**直接 400**。yuankk 的 `qwen3.8-27b` 网关回
        `{"message":"System message must be at the beginning."}`，一整轮就没了。

    所以判据放在**请求出门那一刻**，不放在源码扫盘上：源码扫盘要对 20+ 个
    hook（它们由 `loop_hooks._as_framework_notices` 统一转成 user）开一串豁免，
    那就又是一张名单；而这里问的是"真正发出去的那一串消息长什么样"，新加的
    调用点自动被覆盖。发声侧照旧该用 `framework_notice`，这一层是网不是拐杖。
    """
    offenders = [i for i, m in enumerate(messages) if m.role == "system" and i > 0]
    if not offenders:
        return messages
    out = list(messages)
    for i in offenders:
        log.warning(
            "会话中段的 system 消息已改写为 framework-notice（发声侧应当直接用 "
            "framework_notice）：位置 %d/%d，开头 %r",
            i, len(messages), (out[i].content or "")[:60],
        )
        out[i] = framework_notice(out[i].content or "")
    return out


def framework_notice(content: str) -> LLMMessage:
    """把框架要在会话中途说的话包成一条带归属信封的 user 消息。"""
    body = (content or "").strip()
    return LLMMessage(
        role="user",
        content=(
            f"{FRAMEWORK_NOTICE_OPEN}\n{_FRAMEWORK_NOTICE_ATTRIBUTION}\n"
            f"{body}\n{FRAMEWORK_NOTICE_CLOSE}"
        ),
    )


def is_framework_notice(msg: LLMMessage) -> bool:
    return bool(msg.content) and msg.content.lstrip().startswith(FRAMEWORK_NOTICE_OPEN)


def framework_notice_body(msg: LLMMessage) -> str:
    """取回信封里的正文。

    取证/去重都必须对**正文**做，不能对整条消息做：模型复述的是正文，
    信封与归属行是框架自己加的壳，混进证据池只会稀释判据。
    """
    if not is_framework_notice(msg):
        return msg.content or ""
    inner = (msg.content or "").strip()
    inner = inner[len(FRAMEWORK_NOTICE_OPEN):]
    if inner.endswith(FRAMEWORK_NOTICE_CLOSE):
        inner = inner[: -len(FRAMEWORK_NOTICE_CLOSE)]
    inner = inner.strip()
    if inner.startswith(_FRAMEWORK_NOTICE_ATTRIBUTION):
        inner = inner[len(_FRAMEWORK_NOTICE_ATTRIBUTION):]
    return inner.strip()


@dataclass
class LLMResponse:
    """一次 assistant 回复。"""
    content: str | None
    tool_calls: list[dict]          # OpenAI 风格：[{id, type:"function", function:{name, arguments}}]
    finish_reason: str
    usage: dict
    # Reasoning model 返回的思考链（DeepSeek V4 family / o1 family）。
    # 非 reasoning 模型为 None。
    reasoning_content: str | None = None
    # provider 把 tool-call markup 漏进 content 且没结构化、也没恢复出调用
    # （content 只剩碎片、无正文、无 tool_calls）—— 见 core.tool_call_recovery。
    # 上层不该把这种响应当"最终回答"显示；LLMClient 会先自动重请求几次。
    protocol_leak: bool = False
    # protocol_leak 的细分（jicq 2026-07-24 实测事故）：leak 判定只看"content
    # 只剩碎片"，但一条 completion_tokens=1 的**近乎空响应**若那 1 个 token 恰好
    # 像碎片开头，也会被判 leak —— 那不是"后端把 markup 当文本返回"，是模型在
    # 大上下文下退化、几乎没生成。两者重试策略相同但**归因和提示不同**：
    #   "markup" —— 真有成段 tool-call markup 没解析成结构化调用（后端解析配置）
    #   "empty"  —— 近乎空响应（completion 极小），多为大上下文退化（该压上下文）
    # 非 leak 时为 None。
    leak_kind: str | None = None
    # provider 侧异常与框架为它做的恢复动作（issue #501）。此前这些只进 log：
    # 一次 E2E 里 orchestrator 出现 92 次空 SSE，而 transcript 上一条记录都没有，
    # 只能靠翻服务器日志数出来。既然它决定了这一轮有没有产出，就必须是结构化
    # 事实而不是一行 warning。形如：
    #   {"reason": "empty_sse", "transport": "nonstream_fallback",
    #    "still_empty": True, "elapsed_s": 41.2}
    # 无异常时为 None。
    provider_recovery: dict | None = None


def _split_inline_think(content: str | None,
                          reasoning: str | None) -> tuple[str | None, str | None]:
    """把泄漏进 content 的思维链拆回 reasoning（自建 vLLM/GPUStack 兼容，2026-07-08 实测）。

    部分 OpenAI-compatible 服务端（GPUStack 部署的 deepseek-v4-pro 实测）不把
    reasoning 拆进独立字段——`reasoning` 字段为 null，思考文本混在 content 里，
    形如 `<think>…</think>回复正文` 或残缺的 `…</think>回复正文`（无开标签）。
    不拆的话思维链会泄漏进用户可见回复和下游 artifact。

    保守条件：只在 `</think>` 之前的部分不含代码围栏（```）时才拆——避免误伤
    "讨论 think 标签本身"的正文内容。返回 (content, reasoning)。
    """
    if not content or "</think>" not in content:
        return content, reasoning
    pre, _, post = content.partition("</think>")
    if "```" in pre:
        return content, reasoning     # pre 里有代码块，多半是正文在引用标签，不动
    pre = pre.lstrip()
    if pre.startswith("<think>"):
        pre = pre[len("<think>"):]
    pre = pre.strip()
    merged = (f"{reasoning}\n{pre}".strip()
              if (reasoning and pre) else (reasoning or pre or None))
    return (post.lstrip() or None), merged


# ── 输出防火墙（2026-07-09，与 tool_call_recovery 分层协作）────────────────────
#
# 分工（merge PR#125 时确立）：
#   - core/tool_call_recovery.py：**结构层** —— 从漏进 content 的 tool-call markup
#     恢复结构化调用（让工具真执行）、剥首尾悬挂碎片、判定 protocol_leak。
#   - 本段防火墙：**显示/历史层** —— recovery 之后的最后一道清洗：
#       1. scaffold **复读**剥离：模型逐字复读上一轮注入的 system 提示（scratchpad
#          引导等 400 字正文），收尾接 `</scratchpad>` 再写正文 —— recovery 只剥
#          首尾悬挂 *标签*，不识别被复读的 *正文*，这里带 injected_texts 佐证砍掉
#       2. 残留控制标记扫尾：正文**中间**夹的 DSML / <|...|> / 孤立 think|scratchpad
#          标签（recovery 的首尾 peel 够不到 mid-content 的）
#
# 原则：进历史、进屏幕之前统一清理；检测"控制标记类"而非某个具体串——
# 硬编码句子哨兵在源头文案一改就静默失效（chat.py 的 _ECHO_SENTINEL_RE 之死）。

_FULLWIDTH_BAR = "｜"   # ｜ FULLWIDTH VERTICAL LINE（DeepSeek chat template 用）
# 任何 <...> 标签里含全角竖线 → DSML/chat-template 残留（</｜DSML｜tool_calls> 等）
_DSML_TAG_RE = re.compile(r"</?[^<>]*" + _FULLWIDTH_BAR + r"[^<>]*>")
# <|...|> 半角特殊 token（<|im_end|> <|endoftext|> <|tool_call|> 等）
_CHATML_TOKEN_RE = re.compile(r"<\|[^<>\n]*?\|>")
# 孤立的 think / scratchpad 标签（开或闭）—— 正文不该出现
_SCAFFOLD_TAG_RE = re.compile(r"</?(?:think|scratchpad)\b[^<>]*>", re.IGNORECASE)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _shares_long_run(hay: str, needle: str, min_len: int = 40) -> bool:
    """needle 的某个 min_len 字规范化窗口是否出现在 hay 中（判 echo/复读）。"""
    h, n = _norm_ws(hay), _norm_ws(needle)
    if not n:
        return False
    if len(n) < min_len:
        return n in h
    step = max(1, min_len // 2)
    for i in range(0, len(n) - min_len + 1, step):
        if n[i:i + min_len] in h:
            return True
    return False


def _cut_scaffold_echo(content: str, injected_texts: list[str]) -> str:
    """模型复读注入的 scaffolding 时，收尾常接 `</scratchpad>` 再写正文。

    若 content 里有 `</scratchpad>` 且其**之前**的内容与本轮注入的某条 system
    消息有长重合（确属复读，不是正文在讨论标签）→ 砍到最后一个 `</scratchpad>`
    之后。有 injected_texts 佐证才砍，避免误伤合法正文。
    """
    tag = "</scratchpad>"
    if tag not in content:
        return content
    idx = content.rfind(tag)
    pre = content[:idx]
    if "```" in pre:
        return content
    if any(_shares_long_run(pre, s) for s in (injected_texts or [])):
        return content[idx + len(tag):].lstrip()
    return content


_LEADING_PAREN_RE = re.compile(r"^\s*[（(]([^）)]{8,400})[）)]\s*")


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _scaffold_containment(candidate: str, injected_texts: list[str]) -> float:
    """candidate 的字符 bigram 有多大比例出现在本轮注入的 scaffolding 里。"""
    cand = _bigrams(candidate)
    if not cand:
        return 0.0
    pool: set[str] = set()
    for text in injected_texts or []:
        pool |= _bigrams(text)
    return len(cand & pool) / len(cand)


# 判据阈值：实测（2026-08-07，deepseek-v4-pro 真机）
#   泄漏样本 containment：0.60 / 0.55 / 0.49（模型改写注入文案当开场白）
#   正常正文样本：      0.18 / 0.08 / 0.06 / 0.05
# 取 0.45 —— 距最高正常样本 2.5×。**不是**短语名单：证据来自本轮真实注入的
# system 文案，源头改文案判据自动跟着变（这正是 chat.py 的 _ECHO_SENTINEL_RE
# 之死要避免的）。
_SCAFFOLD_ECHO_CONTAINMENT = 0.45
# 整块复读是**逐字**抄，containment 接近 1；门槛设高些，避免把"正文恰好讨论了
# 同一批术语"的合法段落误砍。
_SCAFFOLD_ECHO_BLOCK = 0.80
# 已经确认"这一条回复开头就在逐字复读"之后，紧跟的开场括号属于同一段复读
# （实测残留：'（别写"我收到用户消息"…）'，它是模型自己的改写，containment
# 只有 0.23）。位置本身就是证据：上下文已证实处于复读模式，单条判据可以放宽。
# 这个放宽**只在剥掉过整块之后生效**，干净回复走不到这里。
_SCAFFOLD_ECHO_AFTER_BLOCK = 0.20
# 行级判据的最短长度：太短的行 bigram 太少，containment 噪声大，宁可停手。
_SCAFFOLD_MIN_LINE = 12


def _cut_leading_scaffold_echo(content: str, injected_texts: list[str]) -> str:
    """剥掉开头那段"复读注入文案"——括号形态与整块形态都认。

    `_cut_scaffold_echo` 以 `</scratchpad>` **标签**为锚；实测泄漏全都不带标签，
    而且有两种形态（deepseek-v4-pro 真机 2026-08-07）：
      1. 开场括号：模型把注入文案改写成一句 `（hook 已启用：…）` 再回答；
      2. 整块复读：模型把整段 400+ 字的 scratchpad 引导**逐字**抄进 content，
         抄完才写答案（UI 上表现为把框架内部提示当成回复给用户看）。
    机制本来就在（injected_texts 一直有证据），只是没接到这两条路径。

    先按段落剥整块复读，再剥残留的开场括号；两步都只砍开头、只在有注入证据
    时砍、砍完必须还剩正文。
    """
    if not injected_texts:
        return content

    # ── 形态 2：整块复读（逐字，containment 接近 1）────────────────────
    # 按**行**扫，不按空行切块：实测泄漏后面常常只跟一个换行（不是空行），
    # 按段落切会把"复读 + 答案"并成一块、重合度被稀释到阈值以下而漏过。
    lines = content.split("\n")
    last_echo = -1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue                           # 空行：不作证据也不打断
        if len(re.sub(r"\s+", "", stripped)) < _SCAFFOLD_MIN_LINE:
            continue                           # 短行（标题/项目符号）：判据不可靠，
                                               # 只跳过、不打断 —— 打断会把复读段
                                               # 从中间截开（实测：'**写作纪律**：'）
        if _scaffold_containment(stripped, injected_texts) < _SCAFFOLD_ECHO_BLOCK:
            break                              # 第一行真正的正文 → 停手
        last_echo = index                      # 确认是复读行
    dropped_any = last_echo >= 0
    if dropped_any:
        # 只砍到**最后一个确认的复读行**为止；它之后的短行归正文，不误伤
        candidate = "\n".join(lines[last_echo + 1:]).lstrip()
        if candidate:
            content = candidate
        else:
            dropped_any = False

    # ── 形态 1：开场括号（改写，containment 0.45+）──────────────────────
    threshold = _SCAFFOLD_ECHO_AFTER_BLOCK if dropped_any else _SCAFFOLD_ECHO_CONTAINMENT
    text = content
    for _ in range(3):                      # 最多剥三层，防病态输入
        match = _LEADING_PAREN_RE.match(text)
        if not match:
            break
        if _scaffold_containment(match.group(1), injected_texts) < threshold:
            break
        rest = text[match.end():].lstrip()
        if not rest:                        # 整条回复只有这段 → 不砍，宁可留着
            break
        text = rest
    return text


def _strip_control_tokens(text: str) -> str:
    """剥离残留控制标记（DSML / chatml special token / 孤立 think|scratchpad 标签）。"""
    for rx in (_DSML_TAG_RE, _CHATML_TOKEN_RE, _SCAFFOLD_TAG_RE):
        text = rx.sub("", text)
    return text


def sanitize_assistant_content(
    content: str | None,
    reasoning: str | None = None,
    injected_texts: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """显示/历史层防火墙：思维链回收 + scaffold 复读剥离 + 控制标记扫尾。

    幂等；在 recover_tool_calls（结构层）之后、content 进入 message 历史 /
    用户屏幕之前调用。返回 (clean_content, reasoning)。clean_content 为空时返回
    None，让调用方（chat.py 回复契约兜底）能识别"这一轮没产出可见正文"并重问。
    """
    content, reasoning = _split_inline_think(content, reasoning)
    if content is None:
        return None, reasoning
    content = _cut_scaffold_echo(content, injected_texts or [])
    content = _cut_leading_scaffold_echo(content, injected_texts or [])
    content = _strip_control_tokens(content)
    content = content.strip()
    return (content or None), reasoning


class StreamSanitizer:
    """增量清洗流式 content，逐块吐出**可安全显示**的干净文本（R2-b 流式显示）。

    流式显示是尽力而为的即时视图；进历史的**权威**版本仍由批量
    sanitize_assistant_content 决定（chat() 收流后再过一遍）。本类只保证：
      - <think>…</think> 泄漏进 content 时不进正文通道（reasoning 不该混进正文）
      - 控制标记（<|…|> / </｜DSML｜…> / 孤立 think|scratchpad 标签）不显示，
        且跨 chunk 分割的半截标记不会被显示出半个

    机制：holdback —— 末尾留若干字符不 emit，等后续 chunk 到齐再判定，这样
    "<|im_" 这种半截 token 永远在缓冲里，直到补全被剥掉或确认是正文。

    2026-07-20（issue #166）两处改进，都是"不泄漏 markup"与"用户能看到进展"的
    平衡，不是把防护删掉：

    1) **holdback 自适应**。原来恒压 _HOLDBACK=24 字，正文再长也永远慢 24 字。
       实际只有"尾巴可能是半截控制标记"时才需要压——所有控制标记都以 < / ｜ / |
       起头，尾窗里没有这些字符就一个字都不用压。但**流开头 _ORPHAN_WINDOW 字
       内保持满 holdback**：孤立 </think>（模型没开标签直接吐思维链再闭合）只可
       能出现在流的最前面，那个检测窗口必须留住，否则思维链会被当正文打出去。

    2) **reasoning_sink**。原来 think 段内的文本一律返回 ""、直接丢弃 → 推理
       模型思考期间屏幕全黑（issue #166 主诉）。现在 think 段内文本改投
       reasoning_sink（由显示层以暗色/"思考中"指示呈现），正文通道行为不变。
       sink 未提供时行为与原来完全一致（纯丢弃）。
    """

    _HOLDBACK = 24        # ≥ 最长控制标记长度，防半截 token 上屏
    _ORPHAN_WINDOW = 96   # 流开头多少字内保持满 holdback（守孤立 </think> 检测窗口）

    def __init__(self, reasoning_sink: Callable[[str], None] | None = None) -> None:
        self._buf = ""
        self._emitted = False
        self._in_think = False
        self._seen = 0
        self._reasoning_sink = reasoning_sink

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _to_reasoning(self, text: str) -> None:
        """把"确认是思维链"的文本投给 reasoning 通道（显示层决定怎么呈现）。

        没有 sink 时等于丢弃 —— 与改造前的行为一致。
        """
        if not text or self._reasoning_sink is None:
            return
        try:
            self._reasoning_sink(text)
        except Exception:
            pass

    @classmethod
    def _min_holdback(cls, text: str) -> int:
        """尾窗里最后一个"标记起始字符"往后的长度 —— 只压这么多就够防半截标记。

        所有控制标记（<|…|> / </｜DSML｜…> / <think> / </scratchpad>）都以
        < 开头，DSML 还含全角/半角竖线；尾窗内一个都没有 → 返回 0（不用压）。
        """
        window = text[-cls._HOLDBACK:]
        idx = max(window.rfind(c) for c in ("<", "｜", "|"))
        return 0 if idx < 0 else len(window) - idx

    def _drain_content(self, *, boundary: bool = False) -> str:
        """把缓冲里"确认可显示"的正文吐出来，剩下的（可能是半截标记）留着。

        boundary=True：后面紧跟一个已确认的标记边界（如 <think>），缓冲里的
        东西不可能是半截标记 → 全吐，不留 holdback。
        """
        cleaned = _strip_control_tokens(self._buf)
        if boundary:
            self._buf = ""
            if cleaned:
                self._emitted = True
            return cleaned
        # 流开头守孤立 </think> 检测窗口；之后只压"可能是半截标记"的尾巴
        if not self._emitted and self._seen <= self._ORPHAN_WINDOW:
            hold = self._HOLDBACK
        else:
            hold = self._min_holdback(cleaned)
        if hold >= len(cleaned):
            self._buf = cleaned
            return ""
        cut = len(cleaned) - hold
        emit, self._buf = cleaned[:cut], cleaned[cut:]
        if emit:
            self._emitted = True
        return emit

    # ── 对外 ────────────────────────────────────────────────────────────────

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        self._buf += delta
        self._seen += len(delta)
        out: list[str] = []
        while True:
            if self._in_think:
                if "</think>" in self._buf:
                    inner, _, rest = self._buf.partition("</think>")
                    self._to_reasoning(inner)
                    self._buf, self._in_think = rest, False
                    continue
                # 仍在 think 内：尾巴之外的部分确认是思维链 → 投 reasoning 通道
                hold = self._min_holdback(self._buf)
                if hold < len(self._buf):
                    cut = len(self._buf) - hold
                    self._to_reasoning(self._buf[:cut])
                    self._buf = self._buf[cut:]
                break

            open_at = self._buf.find("<think>")
            close_at = self._buf.find("</think>")
            # 孤立 </think>（没见过开标签）：它之前的一切都是泄漏的思维链
            if close_at >= 0 and (open_at < 0 or close_at < open_at):
                head, _, rest = self._buf.partition("</think>")
                self._to_reasoning(head)
                self._buf = rest
                continue
            if open_at >= 0:
                pre, _, rest = self._buf.partition("<think>")
                self._buf = pre
                out.append(self._drain_content(boundary=True))
                self._buf, self._in_think = rest, True
                continue
            out.append(self._drain_content())
            break
        return "".join(out)

    def flush(self) -> str:
        """流结束：吐出缓冲里剩余的干净尾巴（最后清一遍）。"""
        buf = self._buf
        self._buf = ""
        if self._in_think:
            self._in_think = False
            self._to_reasoning(buf)          # 未闭合 think → 整段当 reasoning
            return ""
        if "</think>" in buf:
            head, _, buf = buf.rpartition("</think>")
            self._to_reasoning(head)
        if "<think>" in buf:
            buf, _, inner = buf.partition("<think>")
            self._to_reasoning(inner)
        return _strip_control_tokens(buf)


# ── 流式传输层（2026-07-09，R2-b）───────────────────────────────────────────
# 2026-07-09 对 GPUStack deepseek-v4-pro 真端点实测：SSE 流式完整可用（content
# 增量 / tool_calls 按 index 分块累积 / finish_reason / stream_options.include_usage
# / [DONE]）。流式在这里只是**传输变体**：_consume_stream 把 SSE 增量重组成与
# 非流式一模一样的 response dict，下游 _parse_chat_response / recovery / 防火墙
# 全部复用，不加第二条解析路径。
#
# 流式带来的真收益是**生成中可打断**：set_stream_abort_check 注册的回调每个
# chunk 检查一次，/stop 不用再等一次完整生成（长 reasoning 可能几分钟）跑完。
# 中止时丢弃半截 tool_calls（参数 JSON 不完整，不可执行），保留已生成正文。
#
# LLM_STREAM env 控制，默认开；backend 不支持流式（4xx）自动回退非流式。

_STREAM_ABORT_CHECK = None     # () -> bool；True = 立即中止当前生成


def set_stream_abort_check(fn) -> None:
    """注册生成中止检查（chat.py /stop 用）。传 None 清除。"""
    global _STREAM_ABORT_CHECK
    _STREAM_ABORT_CHECK = fn


def _generation_abort_requested() -> bool:
    """这一次生成还该不该继续 —— 每个 chunk 问一次。

    两个来源，取或：注册的回调（CLI 的 panic event，进程级、最快）；以及
    **当前绑定 run 的取消状态**（kill_signal / 粘性取消）。后者不靠任何前端
    记得注册 —— 2026-08-17 实测：平台停止按钮把 kill_signal 写进了子节点
    state，但没有人给平台进程注册过 abort check，于是一次在途的长生成
    （文献调研首轮 prompt）把"立即停"变成了"等这次生成跑完再停"。取消
    咽喉在 chat() 入口挡的是**下一次**调用；这里挡的是**正在跑的这一次**。
    """
    if _STREAM_ABORT_CHECK is not None and _STREAM_ABORT_CHECK():
        return True
    from core import cancellation as _cancel

    return _cancel.current_signal() is not None


#: 中止检查的兜底节拍：**没有数据到达时**也每这么多秒问一次该不该停。
#: 挂在"收到 chunk 之后"的检查对付不了病态上游 —— 2026-08-20 实测（积算
#: vLLM 网关）：连接挂住 300s 不吐一个字节，用户连按 10 次停止全部送达
#: kill_signal，而生成纹丝不动，最后是 worker 自己死掉才算完。停止是用户
#: 对系统的最高优先级指令，它的时延上界必须由**我们的轮询节拍**决定，
#: 不能由对端的吐字节奏决定。
_GENERATION_ABORT_POLL_S = 0.5


async def _sleep_or_abort(delay: float, where: str) -> None:
    """可中止的退避睡眠：停止不该等一个几十秒的 backoff 睡完才生效。

    实现是"**一次** asyncio.sleep(delay) 与中止信号赛跑"，不是把睡眠切片 ——
    切片会改变可观察契约（重试测试断言"Retry-After 只产生一次 sleep(2.0)"），
    且在 sleep 被测试替身 no-op 掉时按墙钟推进就成了忙转。asyncio.wait 的
    超时窗走事件循环计时器，不经过 asyncio.sleep，两边互不干扰。

    命中中止时：绑定 run 有取消信号 → 抛 RunCancelled（cancellation.check）；
    只有进程级 panic 回调（CLI /stop）→ 抛 CancelledError，让上层取消收口。
    """
    if delay <= 0:
        return
    sleep_task = asyncio.ensure_future(asyncio.sleep(delay))
    while True:
        done_set, _ = await asyncio.wait(
            {sleep_task}, timeout=_GENERATION_ABORT_POLL_S)
        if done_set:
            sleep_task.result()
            return
        if _generation_abort_requested():
            sleep_task.cancel()
            with suppress(BaseException):
                await sleep_task
            from core import cancellation as _cancel

            _cancel.check(where)
            raise asyncio.CancelledError(f"generation aborted during {where}")


def stream_enabled() -> bool:
    return os.getenv("LLM_STREAM", "1").strip().lower() not in (
        "0", "false", "off", "no")


def _stream_completion_needs_nonstream_fallback(data: dict) -> bool:
    """Return True when a nominally successful SSE response produced nothing.

    Some OpenAI-compatible gateways terminate an unhealthy generation with
    HTTP 200 and ``[DONE]`` even though the only decoded token is EOS.  That is
    not a valid assistant turn and must not be treated as a normal ``stop``.
    The decision is deliberately protocol-only: it never inspects the user's
    text or guesses what answer should have been produced.

    A user cancellation, visible content, reasoning, or a structured tool call
    is real output and therefore never takes this recovery path.
    """
    if data.get("_stream_aborted"):
        return False
    choices = data.get("choices") or []
    if not choices:
        return True
    choice = choices[0] or {}
    message = choice.get("message") or {}
    if message.get("content") or message.get("reasoning_content") or message.get("reasoning"):
        return False
    if message.get("tool_calls"):
        return False
    if choice.get("finish_reason", "stop") != "stop":
        return False
    usage = data.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    return completion_tokens is None or (
        isinstance(completion_tokens, int) and completion_tokens <= 1
    )


class _StreamHTTPStatusError(Exception):
    """流式请求拿到 HTTP >= 400（headers 阶段）。"""
    def __init__(self, status: int, body: str, retry_after: str | None = None) -> None:
        self.status = status
        self.body = body
        # 流式路径原先根本没把 Retry-After 带出来（retry_after_header 恒为 None），
        # 于是 provider 说"等 60 秒"我们照样按自己的节奏冲——等于没听。
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {body[:200]}")


# provider tool-call 协议泄漏时最多额外重请求几次（见 _parse_chat_response）。
_PROTOCOL_LEAK_RETRIES = 2


def canonical_tool_call_arguments(tool_calls: list[dict] | None) -> list[dict]:
    """tool_call 的 `arguments` 只许有**一种**"没有参数"的写法：`"{}"`。

    ## 为什么（2026-09-16，yuankk，session 74576823）

    模型对 `read_file` 发了一个 arguments 为 `""` 的调用。这个空串在仓里被
    **四处各自翻译**：`agent_loop` 两处 `or "{}"` 把它当无参调用去执行；
    `sanitize_tool_calls_for_wire` 的判据 `json.loads(raw or "{}")` 把它判成
    合法；而 `_msg_to_dict` **原样把 `""` 发上线**。宽松网关也认，严格网关
    （api.icompify.com）回 `Assistant tool call function.arguments must be
    valid JSON` → 400；那条消息已落 checkpoint，续跑照旧重放 → 每轮 400。
    还有一条测试把这个分叉钉成了"设计"（「空串按 {} 处理」——处理只发生在
    判据里，没发生在发出去的值上）。

    三个 `or "{}"` 重叠的部分永远看不出分叉，差集（空串本身）就是撞上的那
    一点。修法不是再加第五处翻译，是让那个值**不存在**：回复被解析成
    LLMResponse 的这一刻（流式 / 非流式两条路都经 `_parse_chat_response`）
    就写成 `"{}"`。历史、checkpoint、wire 从此只见过一种形状。

    只动"空"：缺字段 / None / 空串 / 纯空白 / provider 直接给 dict。**崩坏的
    字符串不在这里碰** —— 那是 #184 的有界重发（MalformedArgsRepair，带断点
    摘录回喂模型）和 #224 的 wire 占位各自在管的事，入口没有截断上下文，
    替它们做决定只会把响的失败换成不响的。
    """
    if not tool_calls:
        return list(tool_calls or [])
    out: list[dict] = []
    for tc in tool_calls:
        fn = (tc or {}).get("function")
        if not isinstance(fn, dict):
            out.append(tc)
            continue
        raw = fn.get("arguments")
        if isinstance(raw, dict):
            fn = {**fn, "arguments": json.dumps(raw, ensure_ascii=False)}
        elif raw is None or (isinstance(raw, str) and not raw.strip()):
            fn = {**fn, "arguments": "{}"}
        else:
            out.append(tc)
            continue
        out.append({**tc, "function": fn})
    return out


def _parse_chat_response(data: dict) -> LLMResponse:
    """把 provider 的 raw JSON 解析成 LLMResponse，含 reasoning 拆分 + tool-call 修复。

    两层清洗（都 backend 无关）：
      1. _split_inline_think：把泄漏进 content 的 <think>…</think> 思维链拆回 reasoning。
      2. tool_call_recovery.recover_tool_calls：从漏进 content 的 tool-call markup
         恢复结构化 tool_calls（让工具真执行），清掉残留碎片，判定 protocol_leak。
    """
    from core.tool_call_recovery import recover_tool_calls

    recovery = data.get("_provider_recovery")
    # provider 200 但一个 choice 都没有 —— 空 SSE 的最重形态。此前这里直接
    # IndexError/KeyError 抛穿，run 以一个看不出成因的异常收场（issue #501）。
    # 不抛：如实返回一个空回合并把成因带出去，让既有的 blank_stop 分类器照常
    # 把它归到 provider 账上（finish_reason 保持 "stop"，否则那条分类不触发）。
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return LLMResponse(
            content=None, tool_calls=[], finish_reason="stop",
            usage=data.get("usage") or {},
            provider_recovery={**(recovery or {}), "reason": "no_choices"},
        )
    choice = choices[0]
    message = choice.get("message") or {}
    # reasoning 字段名两种都认：官方 DeepSeek 用 reasoning_content，
    # GPUStack/vLLM 自建端点用 reasoning（2026-07-08 实测）。
    _content, _reasoning = _split_inline_think(
        message.get("content"),
        message.get("reasoning_content") or message.get("reasoning"),
    )
    rec = recover_tool_calls(_content, message.get("tool_calls") or [])
    usage = data.get("usage", {}) or {}
    return LLMResponse(
        content=rec.content,
        # 入口即规范化：空参数在这里变成 "{}"，下游（执行 / 历史 / wire）只见一种形状。
        tool_calls=canonical_tool_call_arguments(rec.tool_calls),
        finish_reason=choice.get("finish_reason", "stop"),
        usage=usage,
        reasoning_content=_reasoning,
        protocol_leak=rec.protocol_leak,
        leak_kind=_classify_leak_kind(rec, usage) if rec.protocol_leak else None,
        provider_recovery=recovery if isinstance(recovery, dict) else None,
    )


# completion_tokens ≤ 此值 = 近乎空响应：不可能装下哪怕最短的一段真实 tool-call
# markup（`<tool_call>{"name":"x"}</tool_call>` 本身就 10+ token），所以判 empty。
_DEGENERATE_COMPLETION_MAX = 3


def _classify_leak_kind(rec, usage: dict) -> str:
    """把 protocol_leak 细分为 'empty'（近乎空响应/大上下文退化）或 'markup'
    （真有成段 tool-call markup 没解析出来）。见 LLMResponse.leak_kind 注释。

    主信号 completion_tokens：极小 → empty。usage 缺 completion_tokens 时退到
    次信号：剥掉的碎片总长——真 markup leak 会剥掉成段 markup（长），空响应只
    剥掉一个短碎片开头。两者都取不到 → 保守判 'markup'（沿用旧提示，不误报退化）。
    """
    ct = usage.get("completion_tokens")
    if isinstance(ct, int):
        return "empty" if ct <= _DEGENERATE_COMPLETION_MAX else "markup"
    stripped_len = sum(len(s) for s in (rec.stripped or []))
    if stripped_len and stripped_len < 16:
        return "empty"
    return "markup"


class LLMClient:
    """OpenAI-compatible chat 接口的轻量 async 封装。

    **不带参数构造 = 主推理模型**（`model_roles` 里的 `reasoning` 角色）。
    要用别的角色就点名：`LLMClient(role="visual_review")`。显式传
    api_key/model/base_url 仍然优先，用于测试与一次性直调。

    配置从哪来不再是这个类的事 —— 它问 `core.model_roles`，那里是唯一真相源
    （平台走 HARNESS_MODEL_ROLES 通道，CLI 走 LLM_* 合成）。此前这里直接读
    LLM_* env，于是"平台上有哪些模型"这个问题在代码里有九个各自演化的答案。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_backoff_base: float | None = None,
        retry_budget_s: float | None = None,
        role: str | None = None,
    ) -> None:
        from . import model_roles
        from .runtime_secrets import get as _runtime_secret_get

        self.role = role or model_roles.REASONING_ROLE
        # **逐字段**回落到角色，不是"任一字段显式就整份不查"。
        #
        # 那种全有全无的写法有一个具体的受害者：只想换个模型名、provider 不变的
        # 调用方（executor 的 harness.llm_model 覆盖、`LLMClient(model=...)` 这类）
        # 会连凭据一起被摘掉 —— 拿到 api_key=""，然后在 chat() 里被报成"这个角色
        # 没有可用后端"，而角色明明是好的。凭据从来不是"这次点名要用哪个模型"
        # 的一部分。
        #
        # 凭据活在 runtime_secrets（进程内），不在 os.environ —— 模型可控的
        # execute_python / run_bash 是 subprocess，会继承 env。子进程/子节点里
        # 新建的 client 因此必须够得着 runtime_secrets，这是平台的凭据隔离契约
        # （tests/test_platform_runtime.py 那条钉着它）。
        binding = model_roles.resolve(self.role)

        self.api_key = (
            api_key
            or (binding.api_key if binding else "")
            or _runtime_secret_get("LLM_API_KEY")
        )
        self.model = model or (binding.model if binding else "")
        raw_base = (base_url or (binding.base_url if binding else "")).rstrip("/")
        # 容忍 base_url 含或不含 /v1 后缀；下面统一拼 /v1/chat/completions
        if raw_base.endswith("/v1"):
            raw_base = raw_base[:-3]
        self.base_url = raw_base
        # reasoning model（GLM-5.x, o1, deepseek-r1...）在长 context 下单次 call 常
        # >90s。提供 LLM_TIMEOUT env var 让用户调（默认 300s，覆盖 99% reasoning use case）。
        if timeout is None:
            try:
                timeout = float(os.getenv("LLM_TIMEOUT", "300"))
            except ValueError:
                timeout = 300.0
        self.timeout = timeout
        # v2.x：transient 错重试（网络抖动 / 5xx / 429）。env 默认 3 次，per-call
        # 可 override（NodeHarness.llm_max_retries）。设 0 = 关闭重试（旧行为）。
        if max_retries is None:
            try:
                max_retries = int(
                    os.getenv("LLM_MAX_RETRIES", str(_TRANSIENT_MIN_RETRIES))
                )
            except ValueError:
                max_retries = _TRANSIENT_MIN_RETRIES
        self.max_retries = max(0, max_retries)
        if retry_backoff_base is None:
            try:
                retry_backoff_base = float(os.getenv("LLM_RETRY_BACKOFF_BASE", "1.0"))
            except ValueError:
                retry_backoff_base = 1.0
        self.retry_backoff_base = max(0.1, retry_backoff_base)
        # 这次调用值得为瞬时故障扛多久。None = 取 env 默认（60s）。
        #
        # 长任务应当传一个大得多的值：一个已经跑了 3 小时、做完 3 个真实模拟的
        # run，值得为一次后端重启等十分钟；一次闲聊不值得。原本没有这个维度，
        # 于是两者共用同一个"3 次 / 7 秒"的预算 —— 2026-08-10 的事故形状。
        self.retry_budget_s: float | None = retry_budget_s
        # 流式逐 token 显示回调（实例级：只在 orchestrator 的 llm 上设，子节点
        # 用各自新建的 LLMClient() → 不会把子节点的 token 刷到用户屏幕）。
        # 签名 fn(delta: str | None)：delta=文本增量；delta=None=本次响应流结束。
        # 并发准入优先级（core.llm_admission，#426）："normal" = 前台，可用
        # 全部账户级并发槽位；"low" = 后台代谢（curator dreaming），只能用
        # 部分槽位，永远给前台留余量。executor 按 run 的身份机械设置。
        self.priority = "normal"
        self.stream_display = None
        # reasoning（思维链）增量显示回调，与正文通道分开（issue #166）。推理模型
        # 思考阶段常占一次调用的绝大部分时间，原来这些增量整体丢弃 → 用户屏幕全黑
        # 以为卡死。签名同上：delta=增量；delta=None=推理段结束（正文即将开始或
        # 流已结束）。不设则退化成原行为（reasoning 只进 message，不上屏）。
        self.stream_reasoning_display = None

    def spawn_silent(self) -> LLMClient:
        """复制一个**不挂任何显示回调**的独立客户端（同 provider / 同参数）。

        issue #166：后台 dreaming、summarizer 压缩这类"用户没在等它"的调用，
        以前直接复用 orchestrator 那个已挂 stream_display 的实例 → curator 的
        token、压缩摘要顶着 `🔬 Orchestrator` header 流到用户屏幕上，还把
        header_shown 消耗掉，导致真正的回复反而没 header。凡是不该上屏的调用
        都应该走这里拿一个干净实例。
        """
        twin = LLMClient(
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
            retry_backoff_base=self.retry_backoff_base,
        )
        twin.stream_display = None
        twin.stream_reasoning_display = None
        twin.priority = self.priority
        return twin

    def _request_headers(self) -> dict[str, str]:
        """没有 key 就**不发** Authorization 头，而不是发一个空的。

        `Bearer `（空值）在一部分网关上会被当成"给了一把坏 key"而回 401 ——
        那个 401 指的是假因：端点本来根本不要鉴权。缺席就让它缺席。
        """
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout: float | None = None,        # v1.4: per-call override（NodeHarness.llm_timeout_s）
        max_retries: int | None = None,      # v2.x: per-call override（NodeHarness.llm_max_retries）
        _no_cache: bool = False,
    ) -> LLMResponse:
        # 取消咽喉（#284）：进程里发模型请求只有这一个出口，所以取消检查放这里
        # 就覆盖了全部 9 个直调点和一切自定义 agent loop —— 不指望每个调用点
        # 自己记得查（qinp 实测：/stop 之后 data 的恢复链又跑了两轮）。
        from core import cancellation as _cancel
        _cancel.check("llm.chat")

        # 报错必须指向**这个角色**，不能再说"环境变量未设置"。平台路径上根本
        # 没有 LLM_API_KEY 这个变量可填，照着那句话去 .env 里翻是白费时间。
        #
        # api_key **不在这道闸里**（2026-09-15）：自建端点（vLLM / SGLang /
        # Ollama / llama.cpp）默认不鉴权，"没有 key"是它们的正常形态，不是
        # 没配好。把它判成"这个角色不可用"，等于让一台跑得好好的服务器在平台
        # 上永远建不出连接。端点到底要不要鉴权由端点自己回答 —— 它回 401，
        # 那条路已经有正确的归属（upstream_rejected）。
        if not (self.base_url and self.model):
            from . import model_roles

            raise model_roles.ModelRoleUnavailable(
                self.role,
                reason=model_roles.delivery_error() or "",
                absence_note=(getattr(model_roles.spec(self.role), "absence_note", "") or ""),
            )

        # ── dev LLM cache（仅 temperature==0 + HARNESS_LLM_CACHE=on 时生效）─────
        from core import llm_cache
        use_cache = (not _no_cache) and llm_cache.should_cache(temperature)
        ckey = None
        if use_cache:
            ckey = llm_cache.cache_key(
                model=self.model, temperature=temperature,
                messages=messages, tools=tools,
            )
            cached = llm_cache.get(ckey)
            if cached is not None:
                return LLMResponse(
                    content=cached.get("content"),
                    tool_calls=cached.get("tool_calls") or [],
                    finish_reason=cached.get("finish_reason", "stop"),
                    usage=cached.get("usage", {}),
                    reasoning_content=cached.get("reasoning_content"),
                )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_msg_to_dict(m) for m in _only_an_opening_system_message(messages)],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        effective_timeout = timeout if timeout is not None else self.timeout
        effective_retries = max_retries if max_retries is not None else self.max_retries

        # protocol_leak（provider 把 tool-call markup 漏进 content 没结构化，
        # 又恢复不出调用）时自动重请求几次 —— 免得上层把碎片当最终回答显示。
        use_stream = stream_enabled()
        response = None
        for leak_attempt in range(_PROTOCOL_LEAK_RETRIES + 1):
            if use_stream:
                data = await self._stream_with_retry(
                    payload, timeout=effective_timeout,
                    max_retries=effective_retries,
                )
            else:
                data = await self._post_with_retry(
                    payload, timeout=effective_timeout, max_retries=effective_retries,
                )
            response = _parse_chat_response(data)
            if not response.protocol_leak:
                break
            if leak_attempt < _PROTOCOL_LEAK_RETRIES:
                log.warning(
                    "provider tool-call 协议泄漏（content 只剩 markup 碎片、无正文、"
                    "无结构化 tool_calls）—— 第 %d/%d 次重请求。",
                    leak_attempt + 1, _PROTOCOL_LEAK_RETRIES,
                )

        # 显示/历史层防火墙（结构层 recovery 之后的扫尾）：scaffold 复读剥离
        # （以本轮末尾注入的 system 消息为佐证）+ 正文中间残留控制标记清除。
        if response.content:
            # 证据 = 本轮对话里**所有 hook 注入的 system 消息**。
            #
            # 曾经取 messages[-6:]，那是按**位置**取证：模型只要在回答前做了几轮
            # 工具调用，hook 注入就滑出窗口、证据消失、整套剥离静默失效
            # （2026-08-07 UI 实测：400 字 scratchpad 引导被逐字复读成回复，
            # 而剥离逻辑因为拿不到证据完全没触发）。位置不是判据，来源才是。
            #
            # 排除 messages[0]（harness 自己的 system prompt）：它体量极大，
            # 放进证据池会稀释判据、让正常正文误命中。框架注入是循环中**追加**
            # 的那批消息，正是模型会复读的。
            #
            # 判据是"来源是不是框架"，**不是角色**：hook 注入已改成 user 角色 +
            # framework-notice 信封（见上方 framework_notice）。这里若还只认
            # role=="system"，整套剥离会在改造落地那一刻静默失效 —— 防线还在、
            # 只是再也拿不到证据。两种形态都收：信封（新）+ 中段 system（框架
            # 自己的少数控制消息，如截断恢复提示，仍走这条）。
            # 取的是信封**正文**：模型复述的是正文，壳混进来只会稀释判据。
            injected_texts = [
                framework_notice_body(m) for m in messages[1:]
                if m.content and (is_framework_notice(m) or m.role == "system")
            ]
            _clean, _reason2 = sanitize_assistant_content(
                response.content, response.reasoning_content, injected_texts,
            )
            response.content = _clean
            response.reasoning_content = _reason2

        if use_cache and ckey:
            llm_cache.put(ckey, {
                "content": response.content,
                "tool_calls": response.tool_calls,
                "finish_reason": response.finish_reason,
                "usage": response.usage,
                "reasoning_content": response.reasoning_content,
            })

        return response

    async def _stream_with_retry(
        self, payload: dict, *, timeout: float, max_retries: int,
    ) -> dict:
        """流式传输 + transient 重试。返回与 _post_with_retry 同构的 data dict。

        - 网络瞬时错 / 5xx / 429 → 重试（丢弃半截流从头再来，与非流式语义一致）
        - 其它 4xx（backend 不支持 stream / stream_options）→ 记警告，本次调用
          回退非流式（不放弃整个调用）
        """
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}

        last_exc: Exception | None = None
        rate_limited = False
        attempt = 0
        # 时间预算与次数上限**并行**，谁先到都停。
        # 次数防"对方永久 500 时无限打"；时间防"预算按一次 API 调用设、却要
        # 保护一个已经跑了几小时的 run"。见 transient_retry_budget_s 的说明。
        _deadline = time.monotonic() + transient_retry_budget_s(self.retry_budget_s)
        while True:
            retry_after_header = None
            try:
                # 准入槽只罩住真正发请求 + 消费流的窗口；退避睡眠在槽外。
                from .llm_admission import llm_slot

                async with llm_slot(
                    self.base_url,
                    priority=getattr(self, "priority", "normal"),
                ):
                    data = await self._consume_stream(
                        stream_payload, timeout=timeout)
                if _stream_completion_needs_nonstream_fallback(data):
                    log.warning(
                        "LLM SSE ended with no content, reasoning, or tool call; "
                        "retrying the same payload once via non-stream transport"
                    )
                    # 预算是**这一次逻辑调用**的，不是每种传输各一份（#501）。
                    # 此前回退会新开一份完整的 transient_retry_budget_s，于是
                    # 一次调用最坏要烧两倍时间；实测里 orchestrator 因此长时间
                    # 停在一轮上不动，而上层看不到任何状态变化。
                    _started = time.monotonic()
                    recovered = await self._post_with_retry(
                        payload, timeout=timeout, max_retries=max_retries,
                        deadline=_deadline,
                    )
                    still_empty = _stream_completion_needs_nonstream_fallback(
                        recovered)
                    recovered["_provider_recovery"] = {
                        "reason": "empty_sse",
                        "transport": "nonstream_fallback",
                        "still_empty": still_empty,
                        "elapsed_s": round(time.monotonic() - _started, 2),
                    }
                    if still_empty:
                        log.warning(
                            "非流式回退仍然没有正文/推理/工具调用 —— "
                            "本轮按 provider 侧空响应处理。"
                        )
                    return recovered
                return data
            except _StreamHTTPStatusError as e:
                if e.status not in _RETRYABLE_STATUS:
                    log.warning(
                        "流式请求被端点拒（HTTP %d）—— 本次调用回退非流式。body=%s",
                        e.status, e.body[:200],
                    )
                    # 同上：回退与流式尝试是同一次逻辑调用，共用一份时间预算。
                    return await self._post_with_retry(
                        payload, timeout=timeout, max_retries=max_retries,
                        deadline=_deadline,
                    )
                last_exc = LLMHTTPError(
                    e.status, f"LLM API HTTP {e.status}: {e.body[:500]}",
                    body=e.body, retry_after=e.retry_after, model=self.model,
                    payload=stream_payload)
                retry_after_header = e.retry_after
                rate_limited = rate_limited or e.status == 429
            except _RETRYABLE_HTTPX as e:
                last_exc = e

            budget = _effective_max_retries(max_retries, rate_limited)
            if attempt >= budget or time.monotonic() >= _deadline:
                break
            delay = _compute_backoff(
                attempt, self.retry_backoff_base, retry_after_header,
                rate_limited=rate_limited,
            )
            log.warning(
                "LLM stream failed (attempt %d/%d%s): %s — retry in %.1fs",
                attempt + 1, budget + 1, " rate-limited" if rate_limited else "",
                describe_provider_error(last_exc), delay,
            )
            await _sleep_or_abort(delay, "llm.stream_retry_backoff")
            attempt += 1

        assert last_exc is not None
        raise last_exc

    async def _consume_stream(self, payload: dict, *, timeout: float) -> dict:
        """消费一次 SSE 流，把增量重组成非流式同构的 response dict。

        每个 chunk 检查一次 _STREAM_ABORT_CHECK：命中 → 关流返回已生成部分
        （半截 tool_calls 丢弃 —— 参数 JSON 不完整不可执行）。
        若设了 self.stream_display：content 增量经 StreamSanitizer 清洗后逐块回调
        （本次响应结束/中止时回调 None 收尾）。
        若设了 self.stream_reasoning_display：reasoning 增量（含泄漏进 content 的
        <think> 段）走这条**独立通道**逐块回调，正文首个 token 之前先回调 None
        收束推理段 —— 让推理模型的长思考阶段不再是黑箱（issue #166）。
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict] = {}
        finish_reason = "stop"
        usage: dict = {}
        aborted = False
        display = self.stream_display
        r_display = getattr(self, "stream_reasoning_display", None)
        r_open = {"on": False}

        def _emit_reasoning(delta_text: str) -> None:
            if not (r_display and delta_text):
                return
            try:
                r_display(delta_text)
            except Exception:
                pass
            r_open["on"] = True

        def _end_reasoning() -> None:
            """收束推理段。必须在正文第一个字上屏**之前**调用，否则思考指示器
            那半行会跟正文 header 挤在同一行。"""
            if not (r_display and r_open["on"]):
                return
            r_open["on"] = False
            try:
                r_display(None)
            except Exception:
                pass

        # 只要任一通道要显示就得建 sanitizer：<think> 泄漏进 content 时要靠它把
        # 思维链从正文里摘出来投给 reasoning 通道。
        sanitizer = (StreamSanitizer(reasoning_sink=_emit_reasoning)
                     if (display or r_display) else None)

        def _emit(delta_text: str) -> None:
            if not sanitizer:
                return
            shown = sanitizer.feed(delta_text)
            if shown and display:
                _end_reasoning()
                try:
                    display(shown)
                except Exception:
                    pass

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/v1/chat/completions",
                    headers=self._request_headers(),
                    json=payload,
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        # 读 header 不能把**报错路径本身**弄崩：这里已经在处理
                        # 一个错误了，再抛 AttributeError 只会盖掉真正的病因。
                        _headers = getattr(resp, "headers", None) or {}
                        raise _StreamHTTPStatusError(
                            resp.status_code, body, _headers.get("retry-after"),
                        )
                    # ── 读取与中止信号赛跑 ────────────────────────────────
                    # 旧写法 `async for line in ...` 把中止检查挂在"收到一行
                    # 之后"：上游停止吐数据时（实测积算网关挂 300s 零字节），
                    # 检查永远轮不到，"立刻停"退化成"等这次生成自己死掉"。
                    # 读取任务与 _GENERATION_ABORT_POLL_S 超时窗赛跑：没有
                    # 数据也每窗检查一次，命中即取消读取退出（连接随 stream
                    # context 关闭）。
                    line_iter = resp.aiter_lines().__aiter__()
                    pending_read: asyncio.Task | None = None
                    # 距上次收到**真数据行**的时刻。httpx 的 read timeout 只要有
                    # 任何字节到达就重置——而不少 OpenAI 兼容网关（实测 ZJU
                    # GPUStack v4-pro，E2E v35 reviewer 卡 22min）在生成卡死时仍
                    # 周期性发 SSE keepalive（空行 / `:` 注释）。keepalive 一次次
                    # 重置 httpx 超时，连接活着却永远不吐 data → 读循环空转到天荒
                    # 地老，run 挂"running"不动、token 冻住。按"距上次真数据"超时
                    # 中止，抛可重试 ReadTimeout 落进 _stream_with_retry。
                    last_data_at = time.monotonic()
                    while True:
                        if time.monotonic() - last_data_at > timeout:
                            if pending_read is not None:
                                pending_read.cancel()
                                with suppress(BaseException):
                                    await pending_read
                            raise httpx.ReadTimeout(
                                f"SSE stream idle > {timeout:.0f}s with no data "
                                "chunk (keepalive-only stall)")
                        if pending_read is None:
                            pending_read = asyncio.ensure_future(
                                line_iter.__anext__())
                        done_set, _ = await asyncio.wait(
                            {pending_read}, timeout=_GENERATION_ABORT_POLL_S)
                        if not done_set:
                            if _generation_abort_requested():
                                aborted = True
                                pending_read.cancel()
                                with suppress(BaseException):
                                    await pending_read
                                break
                            continue
                        try:
                            line = pending_read.result()
                        except StopAsyncIteration:
                            break
                        pending_read = None
                        if _generation_abort_requested():
                            aborted = True
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        last_data_at = time.monotonic()
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        ch = choices[0]
                        if ch.get("finish_reason"):
                            finish_reason = ch["finish_reason"]
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                            _emit(delta["content"])
                        _r = delta.get("reasoning_content") or delta.get("reasoning")
                        if _r:
                            reasoning_parts.append(_r)
                            _emit_reasoning(_r)
                        for tc in (delta.get("tool_calls") or []):
                            idx = tc.get("index", 0)
                            slot = tool_slots.setdefault(idx, {
                                "id": None, "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["function"]["arguments"] += fn["arguments"]
        finally:
            # 收尾：吐 sanitizer 缓冲的干净尾巴 + 发 None 让显示层收束这段流。
            # flush() 可能把未闭合 think 的残余投给 reasoning 通道，所以先 flush
            # 再 _end_reasoning，最后才收束正文通道。
            tail = sanitizer.flush() if sanitizer else ""
            _end_reasoning()
            if display:
                if tail:
                    try:
                        display(tail)
                    except Exception:
                        pass
                try:
                    display(None)
                except Exception:
                    pass

        if aborted:
            log.warning("流式生成被中止（/stop）—— 保留已生成正文，丢弃半截 tool_calls")
            tool_slots = {}
            finish_reason = "stop"

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_slots:
            calls = []
            for i in sorted(tool_slots):
                slot = tool_slots[i]
                if not slot["id"]:
                    slot["id"] = f"stream_call_{i}"
                calls.append(slot)
            message["tool_calls"] = calls
        return {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage,
            "_stream_aborted": aborted,
        }

    async def _post_with_retry(
        self, payload: dict, *, timeout: float, max_retries: int,
        deadline: float | None = None,
    ) -> dict:
        """POST + retry transient errors。成功返 parsed JSON dict；失败抛原异常。

        retry 策略：
          - 重试：_RETRYABLE_HTTPX 网络瞬时错 / HTTP 429 / HTTP 5xx
          - 不重试：HTTP 4xx 除 429（永久 client error，重试白费）
          - 退避：base × 2^attempt + jitter，单次最长 _MAX_BACKOFF_SECONDS
          - 429 优先用 Retry-After header（如果有）
        """
        last_exc: Exception | None = None
        rate_limited = False
        attempt = 0
        # 时间预算与次数上限**并行**，谁先到都停。
        # 次数防"对方永久 500 时无限打"；时间防"预算按一次 API 调用设、却要
        # 保护一个已经跑了几小时的 run"。见 transient_retry_budget_s 的说明。
        #
        # `deadline` 由调用方传入时**沿用它**（#501）：空 SSE 的非流式回退是
        # 同一次逻辑调用的后半段，另开一份预算等于这次调用的总时长没有上限。
        _deadline = (deadline if deadline is not None
                     else time.monotonic() + transient_retry_budget_s(self.retry_budget_s))
        while True:
            try:
                # 准入槽只罩住真正发请求的窗口；退避睡眠在槽外（#426）。
                from .llm_admission import llm_slot

                async with llm_slot(
                    self.base_url,
                    priority=getattr(self, "priority", "normal"),
                ):
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        # 非流式没有"逐 chunk"可挂中止检查，同样赛跑：
                        # 中止命中 → 取消在途请求，经 cancellation.check 收口。
                        post_task = asyncio.ensure_future(client.post(
                            f"{self.base_url}/v1/chat/completions",
                            headers=self._request_headers(),
                            json=payload,
                        ))
                        while True:
                            done_set, _ = await asyncio.wait(
                                {post_task},
                                timeout=_GENERATION_ABORT_POLL_S)
                            if done_set:
                                resp = post_task.result()
                                break
                            if _generation_abort_requested():
                                post_task.cancel()
                                with suppress(BaseException):
                                    await post_task
                                from core import cancellation as _cancel

                                _cancel.check("llm.request_aborted")
                                raise asyncio.CancelledError(
                                    "generation aborted (panic stop)")
                if resp.status_code < 400:
                    return resp.json()
                # 4xx 除 429 直接 fail（重试白费）
                if resp.status_code not in _RETRYABLE_STATUS:
                    body_preview = resp.text[:2000]
                    raise LLMHTTPError(
                        resp.status_code,
                        f"LLM API HTTP {resp.status_code}: {body_preview}",
                        body=body_preview,
                        retry_after=(getattr(resp, "headers", None) or {}).get("retry-after"),
                        model=self.model, payload=payload,
                    )
                # 5xx / 429 → 进重试分支
                body_preview = resp.text[:500]
                _headers = getattr(resp, "headers", None) or {}
                retry_after_header = _headers.get("retry-after")
                last_exc = LLMHTTPError(
                    resp.status_code,
                    f"LLM API HTTP {resp.status_code}: {body_preview}",
                    body=body_preview, retry_after=retry_after_header,
                    model=self.model, payload=payload,
                )
                rate_limited = rate_limited or resp.status_code == 429
            except _RETRYABLE_HTTPX as e:
                last_exc = e
                retry_after_header = None

            # 预算按**本次遇到的错误类型**现算：一旦见过 429，允许的轮数和
            # 退避一起放大。写死在循环头的 range(max_retries+1) 做不到这件事。
            budget = _effective_max_retries(max_retries, rate_limited)
            if attempt >= budget or time.monotonic() >= _deadline:
                break

            delay = _compute_backoff(
                attempt, self.retry_backoff_base, retry_after_header,
                rate_limited=rate_limited,
            )
            log.warning(
                "LLM call failed (attempt %d/%d%s): %s — retry in %.1fs",
                attempt + 1, budget + 1, " rate-limited" if rate_limited else "",
                describe_provider_error(last_exc),
                delay,
            )
            await _sleep_or_abort(delay, "llm.request_retry_backoff")
            attempt += 1

        assert last_exc is not None
        raise last_exc


def _compute_backoff(attempt: int, base: float,
                      retry_after_header: str | None,
                      rate_limited: bool = False) -> float:
    """base × 2^attempt + jitter，cap 在上限。429 优先用 Retry-After。

    `rate_limited=True`（HTTP 429）换用一套**数量级更大**的参数：并发位被别人
    的长生成占着，秒级重试是纯浪费（见 _RATE_LIMIT_BACKOFF_BASE 处的说明）。
    """
    # 5xx / 网络抖动的上限从 30s 提到 60s、起步从 1s 提到 5s：那类故障的真实
    # 成因是服务端节点重启 / 模型重载 / OOM 后拉起，量级是几十秒到几分钟。
    # 原来的 1/2/4（总 7 秒）继承自"服务端打了个嗝"的假设 —— 2026-08-10 一轮
    # 跑了 3 小时、做完 3 个真实模拟的实验就死在这 7 秒上。
    ceiling = (
        _RATE_LIMIT_MAX_BACKOFF_SECONDS if rate_limited
        else _TRANSIENT_MAX_BACKOFF_SECONDS
    )
    if retry_after_header:
        try:
            return min(float(retry_after_header), ceiling)
        except ValueError:
            pass
    base = max(base, _RATE_LIMIT_BACKOFF_BASE if rate_limited else _TRANSIENT_BACKOFF_BASE)
    delay = base * (2 ** attempt) + random.random() * (base * 0.5)
    return min(delay, ceiling)


def _effective_max_retries(max_retries: int, rate_limited: bool) -> int:
    """429 允许比默认多试几轮 —— 但 max_retries=0（显式关重试）依旧是 0。

    ⚠️ 非 429 **不加下限**。调用方显式传了 `max_retries=1`，那就是 1 ——
    放宽不能覆盖明确意图。第一版我在这里给 5xx 也加了下限，把 per-call
    override 打没了，`test_per_call_max_retries_overrides_client_default`
    当场证伪（传 1 却打了 6 次）。

    "5xx 默认也要扛得久一点"这件事该由**默认值**表达（`LLM_MAX_RETRIES` 的
    缺省从 3 提到 5），不该由这里偷偷抬高别人给的数 —— 那是把策略藏进一个
    看起来只做归一化的函数里。
    """
    if max_retries <= 0:
        return 0
    if rate_limited:
        return max(max_retries, _RATE_LIMIT_MIN_RETRIES)
    return max_retries


def transient_retry_budget_s(explicit: float | None = None) -> float:
    """这次调用值得为瞬时故障扛多久（秒）。

    时间预算与次数上限**并行**，谁先到都停：次数防"对方永久 500 时无限打"，
    时间防"预算按一次 API 调用设、却要保护一个跑了几小时的 run"。
    """
    if explicit is not None:
        return max(0.0, float(explicit))
    try:
        return max(0.0, float(os.getenv(_TRANSIENT_BUDGET_ENV, _DEFAULT_TRANSIENT_BUDGET_S)))
    except (TypeError, ValueError):
        return _DEFAULT_TRANSIENT_BUDGET_S


# ── tool_call arguments 的合法性收口（issue #224）────────────────────────────
#
# jicq 实测：writing 节点跑到上百次工具调用后，某次请求直接 HTTP 400
#   {"message":"Unterminated string starting at: line 1 column 139 (char 138)"}
# 一开始看着像"我们发了非法 JSON body"，但 httpx 的 `json=payload` 是从 dict
# 序列化的，body 必然合法。真正的病在**一层里面**：
#
# `tool_calls[].function.arguments` 本身就是**一个 JSON 字符串**，服务端会去
# 解析它。provider 把 args 截断时（#184 的 malformed_tool_args 签名，弱端点
# 大参数 ~6KB 起就会截），这段坏 JSON 被原样存进 assistant 消息历史，
# **此后每一次请求都带着它** —— 服务端每次解析都报同一个错，节点无法自愈。
# 复现验证：把 '{"content": "## Verdict\\napprove_with_revi' 放进 arguments，
# body 合法但 arguments 报的正是 `Unterminated string starting at: line 1
# column N` —— 与 jicq 的报错一字不差。
#
# 所以修在序列化收口：坏 args 不许上线。既然模型已经拿到 #184 的重发提示，
# 历史里这条坏调用的价值只剩"我尝试过调这个工具"，用合法占位保住语义即可。
_ARGS_TRUNCATED_MARKER = "_framework_note"


def sanitize_tool_calls_for_wire(tool_calls: list[dict] | None) -> tuple[list[dict], list[str]]:
    """把 tool_calls 里非法 JSON 的 arguments 换成合法占位。

    返回 (清洗后 tool_calls, 被修的工具名列表)。合法的原样返回（零拷贝语义
    上不保证，但内容不变）。
    """
    if not tool_calls:
        return [], []
    out: list[dict] = []
    repaired: list[str] = []
    # 存量历史 / checkpoint 里可能还躺着规范化之前写进去的 `""`（2026-09-16
    # yuankk 那条就在 messages_checkpoint.json 里）—— 出口再过一次同一份规范化，
    # 而不是在这里另写一套对"空"的理解。
    for tc in canonical_tool_call_arguments(tool_calls):
        fn = (tc or {}).get("function") or {}
        raw = fn.get("arguments")
        if isinstance(raw, str):
            try:
                # 验**将要发出去的那个值**。原来是 `json.loads(raw or "{}")`：
                # 空串被替换成 "{}" 去验、验过了、然后把 "" 原样发上线 ——
                # 判据说的是另一个值的事。
                json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                name = fn.get("name") or "(unknown)"
                repaired.append(str(name))
                placeholder = json.dumps({
                    _ARGS_TRUNCATED_MARKER: (
                        "上一轮本工具调用的参数被 provider 截断成非法 JSON，"
                        "框架已替换为占位以免污染后续请求。请重新发起该调用，"
                        "参数写成合法 JSON（必要时拆小或分步落盘）。"
                    ),
                    "original_length": len(raw),
                }, ensure_ascii=False)
                tc = {**tc, "function": {**fn, "arguments": placeholder}}
        out.append(tc)
    return out, repaired


def _msg_to_dict(m: LLMMessage) -> dict:
    """序列化为 OpenAI 风格的 wire 格式。

    Reasoning model（DeepSeek V4 family / o1）：assistant 消息要带 reasoning_content
    回传 API，否则报 400 "The reasoning_content in the thinking mode must be passed
    back to the API."

    issue #224：tool_calls 的 arguments 必须是合法 JSON（服务端会解析它）——
    截断的坏 args 在这里被换成占位，否则它会污染**此后每一次**请求。
    """
    d: dict[str, Any] = {"role": m.role}
    if m.content is not None:
        d["content"] = m.content
    if m.tool_calls:
        _clean, _repaired = sanitize_tool_calls_for_wire(m.tool_calls)
        if _repaired:
            log.warning(
                "issue #224：assistant 历史里 %d 个 tool_call 的 arguments 是非法 "
                "JSON（%s），已替换为占位再发送 —— 否则服务端解析 arguments 会 "
                "HTTP 400 且每轮复发。", len(_repaired), ", ".join(_repaired[:3]),
            )
        d["tool_calls"] = _clean
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    if m.name:
        d["name"] = m.name
    if m.reasoning_content is not None and m.role == "assistant":
        d["reasoning_content"] = m.reasoning_content
    return d


# ── 向后兼容别名 ─────────────────────────────────────────────────────────────
# 老代码 + 老 import 路径仍能用；新代码用 LLMClient
DeepSeekClient = LLMClient
