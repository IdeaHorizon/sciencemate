"""Per-node summarizer：把过长的 messages 历史压成一段叙述。

设计目标：
  1. **每节点一种压缩策略**。owner 在 harness yaml 写 `summarizer:` 声明配置；
     特殊需求时在 `nodes/{node}/summarizer.py` 里写 Python 函数注册覆盖。
  2. **保留契约**：system message + 最近 N 轮 + 关键工具调用永不动；
     只压缩中间的 LLM 独白 + 大 tool result。
  3. **触发可控**：默认 token_threshold=0.7（占 max_context_tokens 比例）；
     可配置成 turn_count、或 'never' 关闭。

调用流（在 agent_loop 每轮开头执行）：
  1. 估算当前 messages token 数。
  2. 若超阈值且 summarizer.enabled，调用 run_summarizer。
  3. run_summarizer 优先查 _CUSTOM[node_type]（Python 注册的）。
  4. 没注册则用 _DEFAULT_BY_STRATEGY[harness.summarizer.strategy]。
  5. 替换 messages 列表（in-place），写 transcript 事件。

token 估算用 char/4 粗略 —— 足够 trigger 判断；精确 token 用不上。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import tool_call_cache as _tool_cache
from .harness import NodeHarness, SummarizerConfig
from .llm import (LLMClient, LLMMessage, framework_notice, framework_notice_body,
                  is_framework_notice, opening_system_prompt)
from .state import State

log = logging.getLogger("summarizer")


# ── token 估算 ───────────────────────────────────────────────────────────────

# v1.6: 若 tiktoken 可用，用 cl100k_base / o200k_base 精算（GPT-4 系 + 通用 BPE
# 跟 GLM/DeepSeek/Claude 等 OpenAI-compat backend 的实际 tokenization 误差 < 10%）。
# 不可用时 fallback char/4（旧行为，约 50% 偏低中文 / 100% 偏高 JSON）。
_TIKTOKEN_ENC = None


def _get_encoder():
    """lazy 加载 tiktoken encoder。失败返 None。"""
    global _TIKTOKEN_ENC
    if _TIKTOKEN_ENC is False:    # 已尝试失败
        return None
    if _TIKTOKEN_ENC is None:
        try:
            import tiktoken
            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            # warning 不是 debug：估算器的**身份**决定所有 token 判据的刻度
            # （中文差 8 倍）。它静默换人时，压缩触发点、工具预算、慢测阈值
            # 全部跟着变 —— 2026-08 就是这样让 CI 与开发机各测各的世界。
            # get_encoding 首次使用要联网下载 BPE 词表，网络不通就会走到这。
            log.warning("tiktoken 不可用，token 估算回落 char/4（中文低估约 8 倍）：%s", e)
            _TIKTOKEN_ENC = False
            return None
    return _TIKTOKEN_ENC


def estimate_tokens(messages: list[LLMMessage]) -> int:
    """估算 messages 总 token 数。tiktoken 精算（cl100k_base）；不可用回 char/4。"""
    enc = _get_encoder()
    if enc is not None:
        total = 0
        for m in messages:
            if m.content:
                total += len(enc.encode(m.content))
            # v3.1（审计）：reasoning model（DeepSeek V4 / o1 等）的 reasoning_content
            # 会原样回传给 API —— 不计它会系统性低估 context，压缩触发过晚。
            rc = getattr(m, "reasoning_content", None)
            if rc:
                total += len(enc.encode(rc))
            if m.tool_calls:
                for tc in m.tool_calls:
                    # tc 是 dict，serialize 后 encode
                    total += len(enc.encode(str(tc)))
            # 每条消息有 ~4 token role + format overhead（按 OpenAI 公式）
            total += 4
        return total
    # fallback: char/4
    chars = 0
    for m in messages:
        chars += len(m.content or "")
        rc = getattr(m, "reasoning_content", None)
        if rc:
            chars += len(rc)
        if m.tool_calls:
            for tc in m.tool_calls:
                chars += len(str(tc))
    return chars // 4


# ── SummarizerContext ────────────────────────────────────────────────────────

@dataclass
class SummarizerContext:
    """Summarizer 拿到的全部上下文。

    summarizer 函数接收一个 SummarizerContext，返回新的 messages 列表。
    返回的列表会**替换** ctx.messages（in-place 也行）。
    """
    harness: NodeHarness
    state: State
    messages: list[LLMMessage]
    estimated_tokens: int
    llm: LLMClient                 # 用 LLM 做压缩的话调它
    # 触发时该 turn 编号（信息性，summarizer 一般不用）
    turn: int = 0


# Summarizer 签名：(ctx) -> new_messages
Summarizer = Callable[[SummarizerContext], Awaitable[list[LLMMessage]]]


# ── 注册表 ───────────────────────────────────────────────────────────────────

_CUSTOM: dict[str, Summarizer] = {}            # node_type -> 自定义 summarizer
_STRATEGIES: dict[str, Summarizer] = {}        # strategy name -> 内置 summarizer


def register_summarizer(node_type: str, fn: Summarizer) -> None:
    """注册节点专属 summarizer。

    在 `nodes/{node_type}/summarizer.py` 顶层调用。优先级高于 yaml 里的
    strategy 字段。一个 node_type 注册多次后写的会覆盖之前的。
    """
    if node_type in _CUSTOM:
        log.warning("Summarizer for %r 被覆盖。", node_type)
    _CUSTOM[node_type] = fn


def register_strategy(name: str, fn: Summarizer) -> None:
    """注册一个内置策略（llm / truncate / drop_tool_results / 等）。"""
    _STRATEGIES[name] = fn


def get_summarizer_for(harness: NodeHarness) -> Summarizer | None:
    """按优先级解析本节点该用哪个 summarizer：

      1. 节点专属 Python 注册（_CUSTOM）
      2. yaml 声明的 strategy（_STRATEGIES）
      3. None → 不压缩
    """
    fn = _CUSTOM.get(harness.node_type)
    if fn is not None:
        return fn
    return _STRATEGIES.get(harness.summarizer.strategy)


# ── 触发判断 ─────────────────────────────────────────────────────────────────

_THRASH_GUARD_KEY = "_summarizer_thrash_guard_until_turn"
_LAST_COMPRESS_RESULT_KEY = "_summarizer_last_result"

# ── context 溢出安全（v3.3，atomic-agents E2E 事故）──────────────────────────
# 本地 tiktoken(cl100k) 对 DeepSeek V4 等非 OpenAI 后端**系统性低估**：实测服务端
# 真实计数比本地估算高 ~28%（事故现场 192k 本地 vs 245.8k 服务端），造成"本地以为
# 没超、服务端已拒收"的永久 HTTP 400 死锁。两道保护：
#   1. 校准系数：触发判定用 est*CALIBRATION 作有效 token（放大以补低估）；
#      不改 estimate_tokens 原始返回（那仍是给人看的估算）。
#   2. emergency 高水位：eff ≥ window*EMERGENCY_RATIO 时无视 thrash 冷却强制压缩
#      —— 冷却本防"压不动反复重跑"，但逼近上限不压 = 下一轮 100% 撞 400。
# 两者均可 env 覆盖；CALIBRATION≤1 等于回到旧的乐观行为。
_CONTEXT_CALIBRATION = float(os.getenv("HARNESS_CONTEXT_CALIBRATION", "1.3") or 1.3)
_CONTEXT_EMERGENCY_RATIO = float(
    os.getenv("HARNESS_CONTEXT_EMERGENCY_RATIO", "0.9") or 0.9)

# ── v3.4 真校准（E2E#2 实测 1.3× 静态系数不够）────────────────────────────
# 事故：本地估 132k、服务端实收 245.8k（~1.85×）——两大盲区：
#   (1) tool schemas 不在 messages 里，估算完全没数它（orchestrator 工具几十个）；
#   (2) tokenizer 差异是逐 backend 的，拍固定系数必然有的 backend 不够。
# 修法：每轮 LLM 响应带回服务端权威 prompt_tokens，与本地对同一请求的估算求
# 观测比 —— 之后的触发判定用 max(env 静态系数, 观测比)。观测比只升不降
#（context 安全要的是上界），并封顶防止 usage 异常值把压缩逼疯。
_CONTEXT_CALIBRATION_CAP = float(
    os.getenv("HARNESS_CONTEXT_CALIBRATION_CAP", "3.0") or 3.0)
_OBSERVED_RATIO_KEY = "_ctx_observed_prompt_ratio"
_TOOL_SCHEMA_TOKENS_KEY = "_tool_schema_tokens"

# ── 容量后验（E2E-5a 事故：22 轮空响应循环）─────────────────────────────────
# 配置是先验，观测是证据。env 宣称窗口 262144，但该模型在此 endpoint 上实测
# 174k 就开始解码退化（completion_tokens=1 的空响应）。旧逻辑只信配置：
# 0.7×262144=183.5k 的压缩线永远等不到，框架手握 22 次"174k 上发请求返回空"
# 的直接测量，一次也没用来修正自己对容量的信念。
# 修：agent_loop 在空响应时记录 OBSERVED_CEILING_KEY = min(旧值, prompt_tokens)，
# 触发判定用 min(配置窗口, 观测上限) —— 下一轮请求比上一轮小，系统朝解走。
# 对称清除：更大 prompt 的成功响应证明那次空响应是瞬态而非容量 → 清掉，
# 瞬态故障不会永久污染信念。
OBSERVED_CEILING_KEY = "_observed_context_ceiling"

#: provider 的**硬上限**（input+output ≤ 此值，超了 HTTP 400），来自 context
#: 超限 400 正文里的权威数字。与 OBSERVED_CEILING 是**两种语义**，不能混：
#: 退化上限只约束 prompt（"这么大的 prompt 会解码退化"→ 驱动压缩收缩），
#: 硬上限约束 input+output（"这么大直接拒收"→ 驱动发送闸拒发）。混用的代价
#: 实测过一次：把 5000 的退化观测当硬上限，发送闸判什么都装不下，run 白死。
PROVIDER_HARD_CAP_KEY = "_provider_context_hard_cap"


def _state_int(state, key: str) -> int:
    if state is None:
        return 0
    try:
        return int(state.hook_state.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def hard_cap_window(harness: NodeHarness, state) -> int:
    """provider 硬上限语义的窗口 = min(配置窗口, 400 观测硬上限)。

    发送闸（`presend_overflow`）与工具预算（`derived_tool_budget_bytes`）用它：
    这两处回答的都是"provider 收不收"，不掺解码退化观测 —— 退化是靠压缩
    收缩来治的，不是靠拒发。
    """
    window = int(harness.max_context_tokens)
    cap = _state_int(state, PROVIDER_HARD_CAP_KEY)
    return min(window, cap) if cap > 0 else window


def effective_context_window(harness: NodeHarness, state) -> int:
    """有效窗口 = min(配置窗口, 400 观测硬上限, 空响应观测退化上限)。

    压缩触发用它 —— 触发端越保守越好，两种观测都该收紧它。"""
    window = hard_cap_window(harness, state)
    ceiling = _state_int(state, OBSERVED_CEILING_KEY)
    if ceiling > 0:
        window = min(window, ceiling)
    return window


def estimate_text_tokens(text: str) -> int:
    """单段文本的 token 估算（tool schema 用；与 estimate_tokens 同一 encoder）。"""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        return len(enc.encode(text))
    return len(text) // 4


def effective_calibration(state) -> float:
    """env 静态系数与服务端观测比取大者（观测比封顶）。"""
    obs = 0.0
    if state is not None:
        try:
            obs = float(state.hook_state.get(_OBSERVED_RATIO_KEY) or 0.0)
        except (TypeError, ValueError):
            obs = 0.0
    return max(_CONTEXT_CALIBRATION, min(obs, _CONTEXT_CALIBRATION_CAP))


def note_observed_prompt_tokens(state, *, local_estimate: int,
                                 server_prompt_tokens: int) -> None:
    """agent_loop 每轮响应后回报：服务端实收 prompt_tokens vs 本地对该请求的估算。

    观测比只升不降（max），确保校准朝安全方向收敛；local_estimate 已含
    tool schema tokens，故观测比吸收的是纯 tokenizer/协议差异 + 未建模开销。
    """
    if state is None or local_estimate <= 0 or server_prompt_tokens <= 0:
        return
    ratio = server_prompt_tokens / float(local_estimate)
    prev = 0.0
    try:
        prev = float(state.hook_state.get(_OBSERVED_RATIO_KEY) or 0.0)
    except (TypeError, ValueError):
        prev = 0.0
    new = max(prev, min(ratio, _CONTEXT_CALIBRATION_CAP))
    if new > prev:
        state.hook_state[_OBSERVED_RATIO_KEY] = new




def effective_prompt_tokens(messages_est: int, state) -> int:
    """messages 估算 → 提交给 provider 的有效 prompt tokens。唯一算法。

    = (messages est + 工具 schema est) × 校准系数。
    v22 首跑 5 分钟就抓到两把尺子的代价：should_compress 按本式判"该压"，
    escalate 内部复核却用裸 est × 校准（漏了 schema 份额）—— 入口说超了、
    策略进门一量说没超，压缩空转（37,622 → 37,622，零效果）。增长门兜住了
    重触发，但判据必须同源。
    """
    extra = 0
    if state is not None:
        try:
            extra = int(state.hook_state.get(_TOOL_SCHEMA_TOKENS_KEY) or 0)
        except (TypeError, ValueError):
            extra = 0
    return int((messages_est + extra) * effective_calibration(state))


# ── 工具结果的预算：从「窗口减去其余一切」现算 ─────────────────────────────
#
# issue #710 的病根不是哪一层坏了，是**三个各自为政的预算互相不一致**：
# per-turn 驱逐用 0.45×窗、清除层用 0.5×窗、而真实窗口里还坐着非工具消息
# （qinp 会话实测 34 万字符）、几十个工具的 schema、输出预留 —— 每一层都
# 守住了自己的配额，没有任何一处拥有「总量必须装得下」这个不变量，于是
# 三层全绿、网关 400。
#
# 从此预算只有一个 owner：工具结果能占的空间 = 窗口 − 输出预留 − 安全边距
# − 校准后的（非工具消息 + schema + 墓碑残留）。固定份额（0.45×窗）降级为
# 上限，不再是承诺。

#: 工具结果活副本占窗口的上限（旧 agent_loop._WORKING_SET_SHARE 收编至此）。
WORKING_SET_SHARE_CAP = 0.45
#: 发送前安全边距：吸收估算噪声（墓碑近似、role 开销、reasoning 回传）。
_PRESEND_MARGIN_RATIO = float(
    os.getenv("HARNESS_CONTEXT_PRESEND_MARGIN_RATIO", "0.03") or 0.03)
_PRESEND_MARGIN_MIN = 2048
#: 一条墓碑/紧凑化工具消息的估算占位（≈480 字符）。
_TOMBSTONE_EST_TOKENS = 120
#: 预算下限：别把「余量为负」翻译成 enforce_budget 的「配置写错保护」（<=0
#: 不驱逐）。余量为负时恰恰要最大限度驱逐 —— 地板的唯一职责是保持正数，
#: 取值必须小于任何一份像样的工具结果，否则"最大限度"名不副实。
_TOOL_BUDGET_FLOOR_BYTES = 1024


def presend_margin(window: int) -> int:
    return max(_PRESEND_MARGIN_MIN, int(window * _PRESEND_MARGIN_RATIO))


def derived_tool_budget_bytes(harness: NodeHarness, state, messages: list[LLMMessage],
                              *, output_tokens: int | None = None) -> int:
    """工具结果活副本在 context 里的字节预算。**预算的唯一 owner。**

    real 空间恒等式（real = 服务端 token；est = 本地估算；calib = est→real 系数）：

        window ≥ (非工具 est + schema est + 墓碑 est) × calib
                 + 工具活副本 est × calib + 输出预留 + 边距

    解出工具活副本的 est 配额，×4 折回字符（estimate 的字符→token 假设的逆）。
    固定份额 WORKING_SET_SHARE_CAP 仍作**上限**：对话很短时也不许工具结果
    独占窗口 —— 但它不再被当成"这么多一定装得下"的承诺。

    窗口取 `hard_cap_window`（provider 收不收的语义）；解码退化观测走压缩
    触发端，不该把这里的预算压塌。
    """
    window = hard_cap_window(harness, state)
    calib = effective_calibration(state)
    output = int(output_tokens if output_tokens is not None
                 else getattr(harness, "max_output_tokens", 0) or 0)
    schema_est = 0
    if state is not None:
        try:
            schema_est = int(state.hook_state.get(_TOOL_SCHEMA_TOKENS_KEY) or 0)
        except (TypeError, ValueError):
            schema_est = 0
    non_tool = [m for m in messages if getattr(m, "role", None) != "tool"]
    tool_msgs = [m for m in messages if getattr(m, "role", None) == "tool"]
    fixed_est = (estimate_tokens(non_tool) + schema_est
                 + len(tool_msgs) * _TOMBSTONE_EST_TOKENS)
    avail_real = window - output - presend_margin(window) - int(fixed_est * calib)
    # token 预算 → 字符预算（enforce_budget 的 size = len(content)）。
    #
    # 这个换算曾经是硬编码 ×4 —— char/4 fallback 估算器的逆。tiktoken 在场时
    # 那个逆是错的，而且错的方向随内容变：英文 ~4 字符/token 没事，中文
    # ~0.5 字符/token 时预算宽了 **8 倍** —— CI 实测 4 份 25k 字中文 blocked
    # 报告钉死在 context，eff 263k 对着 60k 的窗，正是 qinp 事故在真
    # tokenizer 下复活（同一函数两把尺，见 test_context_fits_by_construction）。
    # 换算比必须由**当班的那个估算器**对**正被预算的这堆内容**现算：
    # fallback 环境下 pile_est == chars//4 → 比值≈4，行为与旧版逐字节一致。
    pile_chars = sum(len(m.content or "") for m in tool_msgs)
    pile_est = estimate_tokens(tool_msgs) if tool_msgs else 0
    if pile_chars > 0 and pile_est > 0:
        # 上限 4.0：换算比只许比旧假设更紧，不许更松（老行为是上界）。
        chars_per_token = min(4.0, pile_chars / pile_est)
    else:
        # 还没有工具结果可测（首轮）：预算此时没有对象，取旧假设即可。
        chars_per_token = 4.0
    budget_bytes = int(avail_real / calib * chars_per_token)
    cap_bytes = int(window * WORKING_SET_SHARE_CAP * chars_per_token)
    return max(_TOOL_BUDGET_FLOOR_BYTES, min(budget_bytes, cap_bytes))


def presend_overflow(harness: NodeHarness, state, messages: list[LLMMessage],
                     *, output_tokens: int) -> tuple[int, int, int, bool]:
    """发送前不变量的量尺：返回 (有效 prompt real, 窗口, 超出量 real, 权威否)。

    超出量 ≤ 0 表示装得下。与 should_compress / escalate 同一把尺
    （effective_prompt_tokens），不另立算法。

    第四个返回值是**拒发权**：配置窗口只是 summarizer 触发参考（harness.py
    明写"不是 API hard limit"），拿它拒发会把"小窗 + 大输出"的合法配置全判死。
    只有 provider 亲口说过的硬上限（PROVIDER_HARD_CAP_KEY，来自 context-400
    正文）才有资格让框架拒绝发送 —— 配置先验驱动尽力而为的驱逐与收缩，
    权威观测才驱动拒发。没有权威时放行，让 400 处理器去学真值（有界、可恢复）。
    """
    eff = effective_prompt_tokens(estimate_tokens(messages), state)
    cap = _state_int(state, PROVIDER_HARD_CAP_KEY)
    window = cap if cap > 0 else hard_cap_window(harness, state)
    over = eff + int(output_tokens) + presend_margin(window) - window
    return eff, window, over, cap > 0


def should_compress(harness: NodeHarness, messages: list[LLMMessage],
                    turn: int, state=None) -> tuple[bool, int]:
    """返回 (是否要压, 估算 token 数)。

    v3.1 thrash guard（审计：实测单 run 13 次近乎无效压缩，61,402→61,317）：
    上次压缩节省 < 5% 时，接下来 5 个 turn 不再触发 —— 大 tool result 无法
    再压时反复重跑 LLM 压缩是纯浪费。state 传 None（老调用方）时守卫不生效。
    """
    cfg = harness.summarizer
    if not cfg.enabled or cfg.trigger_type == "never":
        return False, 0
    est = estimate_tokens(messages)
    # 有效 token = (messages 估算 + tool schema 估算) × 校准系数。
    # tool schemas 每轮全量随请求发送但不在 messages 里 —— v3.3 之前完全没数它。
    # 校准系数 = max(env 静态, 服务端观测比)，见 effective_calibration。
    eff = effective_prompt_tokens(est, state)

    # 有效窗口：配置是先验，空响应观测是证据（见 OBSERVED_CEILING_KEY 注释）。
    window = effective_context_window(harness, state)

    # ── 增长门（v3.4 / P2 水位滞回）────────────────────────────────────────
    # 上次压缩后 est 没实质增长，就不再压：同样的输入再压一遍必然是同样的
    # 结果。E2E v20 实录就是这个环：压缩压不下去（notice 累积 bug）→ est 不降
    # → emergency 每轮强制再压 → 每轮重写中段 → 前缀缓存全失效、烧 LLM 调用，
    # 直到模型返回空响应。thrash guard（按轮冷却）挡不住它，因为 emergency
    # 无视冷却；增长门是证据判据（"压缩已证明无能为力"），对 emergency 一样
    # 生效 —— 真撞 400 由空轮回滚路径处理，反复重压不是出路。
    if state is not None:
        _last_after = state.hook_state.get(_LAST_COMPRESS_EST_KEY)
        if isinstance(_last_after, int) and est <= _last_after + _MIN_GROWTH_TO_RECOMPRESS:
            return False, est

    # emergency：逼近真实窗口时无视 thrash 冷却强制压缩，避免下一轮撞 API 400。
    # 仅 token_threshold 模式有窗口概念；turn_count 模式无。
    if cfg.trigger_type == "token_threshold":
        emergency = int(window * _CONTEXT_EMERGENCY_RATIO)
        if eff >= emergency:
            return True, est

    if state is not None:
        guard_until = state.hook_state.get(_THRASH_GUARD_KEY)
        if isinstance(guard_until, int) and turn <= guard_until:
            return False, est

    if cfg.trigger_type == "token_threshold":
        threshold = int(window * cfg.trigger_threshold)
        return eff > threshold, est
    if cfg.trigger_type == "turn_count":
        # threshold 在 turn_count 模式下解读为整数轮次（也可写 float 当倍数）
        n = int(cfg.trigger_threshold)
        return turn > 0 and (turn % max(1, n) == 0), est
    return False, est


#: P2 水位滞回：压缩后 est 至少要比上次结果多这么多，才允许再压。
_MIN_GROWTH_TO_RECOMPRESS = 2_000
_LAST_COMPRESS_EST_KEY = "_summarizer_last_est_after"


def note_compress_result(state, *, turn: int,
                          tokens_before: int, tokens_after: int) -> None:
    """v3.1：agent_loop 压缩后回报效果；节省 < 5% → 启动 5-turn 冷却。"""
    if state is None or tokens_before <= 0:
        return
    saved_ratio = (tokens_before - tokens_after) / float(tokens_before)
    state.hook_state[_LAST_COMPRESS_RESULT_KEY] = {
        "turn": turn, "before": tokens_before, "after": tokens_after,
        "saved_ratio": round(saved_ratio, 4),
    }
    if saved_ratio < 0.05:
        state.hook_state[_THRASH_GUARD_KEY] = turn + 5


# ── 给人看的那份占用报告 ─────────────────────────────────────────────────────

def context_window_report(harness: NodeHarness, state,
                          messages: list[LLMMessage], *,
                          server_prompt_tokens: int | None = None) -> dict:
    """这一次请求把窗口占到了哪里 —— **与触发判据同一把尺**。

    界面上那条"当前上下文 xx / 窗口（xx%）· 到 70% 自动压缩"要回答的是
    "离压缩还有多远"。压缩看的是 `effective_prompt_tokens`（本地估算 ×
    校准系数，含工具 schema），不是服务端的 prompt_tokens：静态校准下限 1.3
    意味着服务端计数 54% 时框架已经在 70% 线上了。所以百分比、分段条、
    压缩线三样全按 effective 报；服务端的实收数字另给一列（`prompt_tokens`），
    是事实，不是量尺。

    分段（全部换算到 effective 单位，加起来 ≈ effective_tokens）：
      system        开篇 system prompt
      tools         工具 schema（随请求全量发送，不在 messages 里）
      toolResults   工具结果（可驱逐的那部分）
      summary       压缩 notice（被压掉的历史现在占多大）
      framework     其余框架中途发声（hook 注入等）
      conversation  用户与模型的对话正文

    这里逐条再量一遍 messages（一整个 context 再过一次 encoder）。每轮已经
    有三四次全量估算，多这一次换来的是分段；嫌贵就先把这条事件关掉，
    别改分类去省。
    """
    est = estimate_tokens(messages)
    calib = effective_calibration(state)
    eff = effective_prompt_tokens(est, state)
    window = effective_context_window(harness, state)
    cfg = harness.summarizer
    thresholded = bool(cfg.enabled and cfg.trigger_type == "token_threshold")
    compress_at = float(cfg.trigger_threshold) if thresholded else None
    emergency_at = _CONTEXT_EMERGENCY_RATIO if thresholded else None

    buckets = {"system": 0, "summary": 0, "framework": 0,
               "toolResults": 0, "conversation": 0}
    for m in messages:
        n = estimate_tokens([m])
        if m.role == "system":
            buckets["system"] += n
        elif _is_compression_notice(m):
            buckets["summary"] += n
        elif is_framework_notice(m):
            buckets["framework"] += n
        elif m.role == "tool":
            buckets["toolResults"] += n
        else:
            buckets["conversation"] += n
    breakdown = {k: int(v * calib) for k, v in buckets.items()}
    breakdown["tools"] = int(_state_int(state, _TOOL_SCHEMA_TOKENS_KEY) * calib)

    last = state.hook_state.get(_LAST_COMPRESS_RESULT_KEY) if state is not None else None
    last_compaction = None
    if isinstance(last, dict):
        try:
            last_compaction = {
                "turn": int(last.get("turn") or 0),
                "tokens_before": int(last.get("before") or 0),
                "tokens_after": int(last.get("after") or 0),
                "saved_ratio": float(last.get("saved_ratio") or 0.0),
            }
        except (TypeError, ValueError):
            last_compaction = None

    try:
        prompt_tokens = int(server_prompt_tokens or 0) or None
    except (TypeError, ValueError):
        prompt_tokens = None
    return {
        "prompt_tokens": prompt_tokens,
        "est_tokens": est,
        "effective_tokens": eff,
        "calibration": round(calib, 3),
        "window": window,
        "configured_window": int(harness.max_context_tokens),
        "compress_at": compress_at,
        "emergency_at": emergency_at,
        "breakdown": breakdown,
        "n_messages": len(messages),
        "last_compaction": last_compaction,
    }


# ── 顶层入口 ─────────────────────────────────────────────────────────────────

async def run_summarizer(harness: NodeHarness, state: State,
                          messages: list[LLMMessage], llm: LLMClient,
                          turn: int, estimated_tokens: int) -> list[LLMMessage]:
    """跑 summarizer，返回新的 messages 列表。

    若没有任何匹配的 summarizer，返回原 messages 不动（log 一次 warning）。
    """
    fn = get_summarizer_for(harness)
    if fn is None:
        log.warning(
            "节点 %r 触发了压缩，但没有匹配的 summarizer（strategy=%r 未注册）。跳过。",
            harness.node_type, harness.summarizer.strategy,
        )
        return messages

    # ── 无损清除先行（无条件，不看 harness 配置）──────────────────────────
    # 清除层是**无损**的（内容凭指针可恢复），没有任何节点有理由跳过它。
    # 做成 strategy 选项的代价 v22 实测到了：`_orchestrator` 硬编码
    # `strategy: llm`，于是最需要它的节点（中段 93% 是 run_node 结果、
    # 单条 ~7k）完全用不上，直接进有损层。让每个 harness 作者"记得选"就是
    # 名单式护栏 —— 不写就默认漏过。
    #
    # 分工因此变清晰：**清除是框架无条件做的；`strategy` 只决定清完还不够时
    # 用哪种有损压缩**。
    clear_ctx = SummarizerContext(
        harness=harness, state=state, messages=messages,
        estimated_tokens=estimated_tokens, llm=llm, turn=turn,
    )
    tokens_before_clear = estimate_tokens(messages)
    try:
        messages = await _strategy_clear_tool_results(clear_ctx)
    except Exception as e:
        log.warning("无损清除层失败（不阻断后续压缩）：%s", e)
    freed_by_clear = tokens_before_clear - estimate_tokens(messages)
    tokens_after_clear = estimate_tokens(messages)
    est_after_clear = effective_prompt_tokens(tokens_after_clear, state)
    window = effective_context_window(harness, state)
    threshold = int(window * float(harness.summarizer.trigger_threshold or 0.7))
    # 早退的判据是"清除**确实**解决了问题"：既要清出了东西，又要现在低于阈值。
    # 光看"现在低于阈值"会用我自己重算的值否决调用方的触发决定 —— 清除一个
    # 字节都没清掉时那等于把压缩静默跳过（既有用例 strategy=truncate 当场
    # 暴露：调用方说 8500 该压，这里重算后说不用压，配置的策略再没跑过）。
    if freed_by_clear > 0 and est_after_clear <= threshold:
        # 清够了就不进有损层 —— 省一次 LLM 调用，且零信息损失。
        state.append_transcript(
            "summarizer_clear_sufficed", turn=turn,
            tokens_before=tokens_before_clear,
            tokens_after=tokens_after_clear,
            freed=freed_by_clear,
        )
        if state is not None:
            state.hook_state[_LAST_COMPRESS_EST_KEY] = tokens_after_clear
        return messages

    ctx = SummarizerContext(
        harness=harness, state=state, messages=messages,
        estimated_tokens=estimate_tokens(messages), llm=llm, turn=turn,
    )
    try:
        new_msgs = await fn(ctx)
    except Exception as e:
        log.error("Summarizer 跑挂了：%s。返回原 messages。", e, exc_info=True)
        return messages
    if not isinstance(new_msgs, list) or not new_msgs:
        log.warning("Summarizer 返回了空 / 非法结果，跳过。")
        return messages

    # v3.3：压缩保任务状态。压缩按 token 数裁剪，会把"已查过什么、结论是什么"
    # 一并裁掉 —— E2E 实测该缺口让一个 run 用 78 个 query 重复查了 51,937 次
    # （149 次压缩，每次压完就"忘了"查过，从头再来一轮 checklist）。
    # 工具结论台账活在 hook_state、不随 messages 消失，这里把它注回压缩结果尾部。
    try:
        _digest = _tool_cache.findings_digest(state)
    except Exception as e:      # 台账异常绝不阻断压缩本身
        log.debug("findings_digest failed: %s", e)
        _digest = None
    if _digest:
        new_msgs = [*new_msgs, framework_notice(_digest)]
        state.append_transcript(
            "compression_findings_digest_injected",
            turn=turn, digest_chars=len(_digest),
        )

    # v1.6 A: transcript 记 before/after tokens（之前盲飞）
    tokens_after = estimate_tokens(new_msgs)
    ratio = (round(tokens_after / estimated_tokens, 3)
             if estimated_tokens > 0 else None)
    state.append_transcript(
        "summarizer_compress",
        turn=turn,
        strategy=harness.summarizer.strategy,
        tokens_before=estimated_tokens,
        tokens_after=tokens_after,
        compression_ratio=ratio,
        messages_before=len(messages),
        messages_after=len(new_msgs),
    )

    # v1.6 F: 二次保护 —— 压完仍 > 90% × max_context_tokens：跑 drop_tool_results
    # 再省一次（防长 session 撞 API hard limit）。用校准后的有效 token 判定，
    # 与 should_compress 一致 —— 否则本地低估会让二次兜底也触发过晚。
    limit = harness.max_context_tokens
    _eff_after = effective_prompt_tokens(tokens_after, state)
    if limit > 0 and _eff_after > 0.9 * limit:
        log.warning(
            "压完仍 %d tokens（有效 ~%d）> 90%%×%d。跑 drop_tool_results 二次兜底。",
            tokens_after, _eff_after, limit,
        )
        ctx2 = SummarizerContext(
            harness=harness, state=state, messages=new_msgs,
            estimated_tokens=tokens_after, llm=llm, turn=turn,
        )
        try:
            new_msgs2 = await _strategy_drop_tool_results(ctx2)
            tokens_after2 = estimate_tokens(new_msgs2)
            state.append_transcript(
                "summarizer_compress_secondary",
                turn=turn,
                tokens_before=tokens_after,
                tokens_after=tokens_after2,
            )
            new_msgs = new_msgs2
        except Exception as e:
            log.error("二次兜底 drop_tool_results 跑挂：%s，保留一次压缩结果。", e)

    # ── P4 压后再定向：事实以压缩当刻的重扫为准 ──────────────────────────
    # 账本只管叙事连续性；"项目现在什么样"由机械扫盘回答（同 turn-1 定向层
    # 同一个 builder，单一真相源）。旧定向注入先移除再补新的 —— notice 累积
    # bug（PR #351）的教训：合并了内容就必须删被取代的那条，否则每压一次
    # 多一份。压缩本身已经重写了消息列表，此刻动 head 不额外损失缓存。
    try:
        from core.loop_hooks_builtin import ORIENTATION_PREFIX, build_orientation_snapshot

        fresh = build_orientation_snapshot(state)
        if fresh:
            # 认「正文」而不是「角色 + 整条 startswith」：定向注入已改成
            # framework-notice 信封（user 角色 + 壳），按老判据一条都匹配不上，
            # 去重会静默失效 —— 于是每压一次多留一份旧快照，正是 PR #351 那个
            # 累积 bug 原样复发。framework_notice_body 对非信封消息返回原文，
            # 所以两种形态都能认。
            new_msgs = [m for m in new_msgs
                        if not framework_notice_body(m).startswith(ORIENTATION_PREFIX)]
            new_msgs = [*new_msgs, framework_notice(
                fresh + "\n\n（压缩后重扫 —— 以本快照为准，账本中的状态性描述可能已过期。）"
            )]
            state.append_transcript("orientation_refreshed_after_compress", turn=turn)
    except Exception as e:
        log.debug("压后定向重扫失败（不阻断压缩）：%s", e)

    # ── P6 机械换届信号：压缩已证明无法把上下文压回窗口 ──────────────────
    # 这是最后一道保险丝，不是常态路径 —— P1-P3 之后绝大多数场景都能压回去。
    # 触发即 fail-loud：结构化事件给平台/driver，注入建议给模型（收尾、把
    # 交接状态写进自己的目录、请求换届）。**不自动 publish**：publish 带科学
    # 门语义（审查/完成度），何时换届是平台和人的决定，机制只负责把"压缩救
    # 不了了"这个事实亮出来（v20 实测：这个事实被沉默吞掉，orchestrator 白
    # 磨了 5 小时；手动 publish+新 session 后 v21 一次收尾）。
    _final_est = estimate_tokens(new_msgs)
    _final_eff = effective_prompt_tokens(_final_est, state)
    _window = effective_context_window(harness, state)
    if (_final_eff >= int(_window * 0.9) and state is not None
            and not state.hook_state.get("_session_rotation_advised")):
        state.hook_state["_session_rotation_advised"] = True
        try:
            state.append_transcript(
                "session_rotation_advised", turn=turn,
                est_after_compress=_final_est, effective=_final_eff,
                window=_window,
            )
        except Exception:
            pass
        new_msgs = [*new_msgs, framework_notice((
            f"⛔ **压缩已到极限**：压完仍约 {_final_eff} tokens，超过有效窗口 "
            f"{_window} 的 90%。继续硬撑会退化到空响应（实测过）。\n"
            "现在就收尾：\n"
            "  1. 停止派发新的大任务；\n"
            "  2. 把交接状态（做到哪、下一步、被什么挡着）写进你自己目录的"
            " scratchpad / 工作文件；\n"
            "  3. 告知用户本 session 需要换届：publish 当前成果后开新 session"
            "（项目 Git 会带全部产物过去，新 session 开局定向层会自动看到）。"
        ))]

    if state is not None:
        try:
            state.hook_state[_LAST_COMPRESS_EST_KEY] = estimate_tokens(new_msgs)
        except Exception:
            pass
    return new_msgs


# ── 公共 helper：把 messages 切成 (system, keep_head_turns, middle, last_n_turns) ──

def _is_turn_boundary(msg: LLMMessage) -> bool:
    """assistant 消息是 turn 的开始（每个 turn 由 assistant 调用 + 工具 result 组成）。"""
    return msg.role == "assistant"


def split_for_compression(
    messages: list[LLMMessage], keep_last_n_turns: int,
) -> tuple[list[LLMMessage], list[LLMMessage], list[LLMMessage]]:
    """把 messages 切成三段：(初始 system + user，中间可压区，末尾 N 轮)。

    "一轮" 定义：从一条 assistant 消息开始，到下一条 assistant 消息或结尾。
    role=system 的消息会被吸收到它前一条 user/assistant 所在的"轮"里。

    初始段：直到第一条 assistant 之前的所有消息（一般是 system + 第一条 user）。
    末尾段：最后 keep_last_n_turns 个 assistant 起始的轮（含工具 result）。
    中间段：剩余的（这才是被压缩的部分）。
    """
    # 找出每条 assistant 消息的位置
    assistant_idxs = [i for i, m in enumerate(messages) if m.role == "assistant"]
    if len(assistant_idxs) <= keep_last_n_turns:
        # 不够，整个都保留
        return messages, [], []

    head_end = assistant_idxs[0]              # 第一条 assistant 之前的索引
    tail_start = assistant_idxs[-keep_last_n_turns]
    return (
        messages[:head_end],
        messages[head_end:tail_start],
        messages[tail_start:],
    )


def _is_keep_tool_call(msg: LLMMessage, keep_tool_calls: list[str]) -> bool:
    """assistant 消息：检查其 tool_calls 是否包含 keep 列表中的工具名。
       tool 消息：检查 name 字段。
    """
    if not keep_tool_calls:
        return False
    if msg.role == "tool" and msg.name in keep_tool_calls:
        return True
    if msg.role == "assistant" and msg.tool_calls:
        for tc in msg.tool_calls:
            fn_name = (tc.get("function") or {}).get("name")
            if fn_name in keep_tool_calls:
                return True
    return False


def extract_keep_tool_pairs(
    middle: list[LLMMessage], keep_tool_calls: list[str],
) -> list[LLMMessage]:
    """从 middle 段抽出那些含 keep_tool_calls 工具调用的 (assistant, tool, ...) 序列。

    v1.6 fix：assistant.tool_calls 是原子 list。如果其中**任一** call 在
    keep 名单 → 整个 assistant + 其**所有** tool result 都得保留（OpenAI API
    硬契约：assistant.tool_calls=[A,B] 的 tool result A 跟 B 必须同时在 messages
    里且顺序正确，否则 API 拒）。
    """
    out: list[LLMMessage] = []
    i = 0
    while i < len(middle):
        m = middle[i]
        if m.role == "assistant" and m.tool_calls:
            wanted = [
                (tc.get("function") or {}).get("name") in keep_tool_calls
                for tc in m.tool_calls
            ]
            if any(wanted):
                # 把这条 assistant + 紧随其后的**所有** tool result 都保留
                # （即使 tool result 对应的 tool 不在 keep 名单 —— pair 完整性硬约束）
                out.append(m)
                j = i + 1
                while j < len(middle) and middle[j].role == "tool":
                    out.append(middle[j])
                    j += 1
                i = j
                continue
        i += 1
    return out


# ── 内置策略：truncate ───────────────────────────────────────────────────────

async def _strategy_truncate(ctx: SummarizerContext) -> list[LLMMessage]:
    """最朴素：丢弃中间段，只保留首尾。无 LLM 调用 —— 最便宜，但损失信息最多。"""
    cfg = ctx.harness.summarizer
    head, middle, tail = split_for_compression(ctx.messages, cfg.keep_last_n_turns)

    keep_from_middle = extract_keep_tool_pairs(middle, cfg.keep_tool_calls)

    dropped = len(middle) - len(keep_from_middle)
    notice = build_compression_notice(ctx.turn, "truncate 策略。丢弃了中间 {dropped} 条 messages，保留了 head/tail 和 ")
    return [*head, notice, *keep_from_middle, *tail]


register_strategy("truncate", _strategy_truncate)


# ── 内置策略：drop_tool_results ──────────────────────────────────────────────

async def _strategy_drop_tool_results(ctx: SummarizerContext) -> list[LLMMessage]:
    """中间段所有 tool 消息的 content 截短到 200 字符。保留全部 assistant 推理。

    适合：tool result 巨大（papers search / list_artifacts 输出）但 LLM 自己
    的推理需要保留的节点。
    """
    cfg = ctx.harness.summarizer
    head, middle, tail = split_for_compression(ctx.messages, cfg.keep_last_n_turns)

    new_middle: list[LLMMessage] = []
    truncated_count = 0
    for m in middle:
        if m.role == "tool" and not _is_keep_tool_call(m, cfg.keep_tool_calls):
            if m.content and len(m.content) > 200:
                truncated_count += 1
                new_middle.append(LLMMessage(
                    role="tool",
                    tool_call_id=m.tool_call_id,
                    name=m.name,
                    content=(m.content[:180] + "…[truncated by summarizer]"),
                ))
                continue
        new_middle.append(m)

    notice = build_compression_notice(
        ctx.turn,
        f"drop_tool_results 策略，中间段 {truncated_count} 条 tool result "
        f"被截短到 200 字符。",
    )
    return [*_head_without_superseded_notices(head), notice, *new_middle, *tail]


register_strategy("drop_tool_results", _strategy_drop_tool_results)


# ── 内置策略：llm（默认）─────────────────────────────────────────────────────

#: 账本的固定 section（P3）。自由体摘要经过几十次重写会退化成套话
#: （ACE 论文叫 context collapse；v20 实录第 26 次重写时账本里已混进
#: "好的，这是合并后的压缩摘要。"这类模型口水）。固定结构 + 增量合并 +
#: 机械校验，才守得住几十轮。
_LEDGER_SECTIONS = ("## 目标", "## 已完成", "## 未决与下一步", "## 决策与理由")

_DEFAULT_LLM_INSTRUCTION = (
    "你在维护一份 agent 工作账本（不是写摘要散文）。输出必须是且只是这四个"
    " section，按此顺序：\n"
    "  ## 目标 —— 本 run 要达成什么（1-3 行，通常不变）\n"
    "  ## 已完成 —— 条目列表；**每条末尾必须带证据指针**"
    "（artifact_id / 子 run id / 文件路径），没有指针的成果不要写\n"
    "  ## 未决与下一步 —— 还欠什么、下一步做什么、被什么挡着\n"
    "  ## 决策与理由 —— 已做出的选择和为什么（含放弃的路线）\n"
    "规则：\n"
    "  1. **第一行必须就是 '## 目标'** —— 不要任何开场白、客套、解释。\n"
    "  2. **id 原样抄写不许编造**：任何 `claim_<hex>` / `concept_<hex>` /"
    " `chunk_<hex>` / `experiment_<hex>` / artifact_id / run id 看到啥写啥，"
    "一个字符都不能改（下游 phantom citation check 会把编的 id 标 critical"
    " fail）。拿不准就别提那个 id。\n"
    "  3. 丢弃：tool result 原文、模型自我重复、已解决的死胡同的过程细节"
    "（死胡同的**结论**写进 决策与理由）。\n"
    "  4. 第三人称客观叙述，不用'我'。\n"
)


def _sanitize_ledger(text: str) -> str:
    """机械剥掉账本前的模型口水（"好的，这是…"）。第一处 section 之前的全删。"""
    idx = text.find(_LEDGER_SECTIONS[0])
    if idx < 0:
        idx = text.find("## ")
    return text[idx:].strip() if idx >= 0 else text.strip()


def _ledger_missing_sections(text: str) -> list[str]:
    return [h for h in _LEDGER_SECTIONS if h not in text]


# 上一轮压缩留下的 notice 标记 —— 用于检测 + running summary 合并
_COMPRESSION_NOTICE_MARKER = "📦 历史压缩"
_COMPRESSION_SUMMARY_HEADER = "## 历史压缩摘要"

#: 摘要复活防线。压缩摘要是**对已发生的事的记录**，但它读起来和任务清单
#: 长得一样 —— 里面的"未决事项 / 下一步"很容易被当成现在就该执行的指令，
#: 于是模型回头去做一件早已被推翻或已经做完的事。
#:
#: 三条都必要：说清它是什么（参考不是指令）、说清谁优先（最新的用户消息）、
#: 说清没有新指令时该干嘛（接着当前工作，别从摘要里翻活干）。
_ANTI_REVIVAL_PREAMBLE = (
    "\n⚠️ 下面这段是**历史记录，不是当前指令**：\n"
    "- 它描述已经发生过的事，供你回忆上下文用；里面的「未决事项 / 下一步」是"
    "**当时**的判断，未必仍然成立。\n"
    "- 与它冲突时，**以摘要之后的消息为准** —— 尤其是最新的用户指令，"
    "它的权威性高于这段转述。\n"
    "- 摘要之后如果没有新的指令，就接着你当前正在做的事，"
    "不要从这段历史里翻出旧任务重做。\n"
)



def build_compression_notice(turn: int, detail: str, body: str = "") -> LLMMessage:
    """所有压缩策略共用的 notice 构造器。

    三条策略原本各写各的文案，防复活 preamble 只补在了 llm 那条上 —— 实测
    (E2E 强制压缩) llm 压完后二次兜底又跑 drop_tool_results，最终留在上下文里
    的是**不带 preamble 的那一条**。摘要复活防线只覆盖一条路径 = 没有防线。
    """
    content = f"{_COMPRESSION_NOTICE_MARKER}（turn {turn}）：{detail}"
    content += _ANTI_REVIVAL_PREAMBLE
    if body:
        content += f"\n{body}"
    return framework_notice(content)


def _extract_prior_summary(head: list[LLMMessage]) -> str | None:
    """v1.6 H: 从 head 找上次压缩留下的 notice，抽 markdown 摘要部分。

    若有，下次压缩时 prepend 给 LLM 作"running summary"基底，避免多段摘要拼接碎片。
    """
    for m in reversed(head):
        # 判据走 `_is_compression_notice`（marker + 框架身份），不自己再写一遍：
        # 这两处问的是同一个问题「这条是不是框架写的压缩 notice」。
        if not _is_compression_notice(m):
            continue
        # 抽 "## 历史压缩摘要" 后到末尾的内容
        idx = m.content.find(_COMPRESSION_SUMMARY_HEADER)
        if idx < 0:
            return None
        text = m.content[idx + len(_COMPRESSION_SUMMARY_HEADER):].strip()
        # 去掉尾部的"（X 条 keep_tool_calls...）"括号说明
        cut = text.find("\n（")
        if cut > 0:
            text = text[:cut].strip()
        return text or None
    return None



def _is_compression_notice(msg: LLMMessage) -> bool:
    """这条是不是框架写的压缩 notice。

    判据 = **marker + 框架身份**，两者缺一不可：

      - 不看角色单看 marker：用户完全可以在自己的消息里打出那几个字
        （粘贴日志、讨论压缩机制），那不该被当成框架的 notice 摘掉。
        （这里刻意不复述 marker 字面量 —— `test_every_strategy_notice_carries_the_preamble`
        扫全文件出现次数，多写一处就等于多一个会分叉的副本。）
      - 不看 marker 单看角色：这条 notice 的角色刚从 system 改成 framework-notice
        （user + 归属信封）。按角色过滤会**静默**失效 —— 旧 notice 摘不掉，
        一轮一轮堆在 head 里，而且不报错。

    `role == "system"` 那一支是为**旧 checkpoint** 留的：续跑恢复的历史里
    还有老格式的 notice，它们同样该被识别。
    """
    if not msg.content or _COMPRESSION_NOTICE_MARKER not in msg.content:
        return False
    return is_framework_notice(msg) or msg.role == "system"


def _head_without_superseded_notices(head: list[LLMMessage]) -> list[LLMMessage]:
    """把 head 里**上一轮**的压缩 notice 摘掉。

    压缩装配原本是 `[*head, notice, ...]` —— head 原样带下去，新 notice 追加在
    后面。而 `split_for_compression` 把"第一条 assistant 之前"算作 head，上一条
    notice 恰好落在那里，于是**它从此进了永不压缩的 head**。压 N 次就有 N 条
    摘要永久驻留。

    实测（E2E v20 orchestrator）：head 40 条里 33 条是历次压缩 notice，约
    14,500 tokens；每压一次 prompt 反而涨 ~1,000：
    197,671 → 198,942 → 200,226 → 201,113 → 202,527，直到模型返回空响应。
    **压缩机制自己在制造它要治的症状。**

    `_extract_prior_summary` 早就把旧摘要的**内容**并进了新摘要，所以删掉旧
    notice 不丢信息 —— 缺的一直是"合并之后要把被取代的那条去掉"这一步。
    """
    return [m for m in head if not _is_compression_notice(m)]


#: 单条被保留的 user 消息的字节上限。用户指令通常很短；真超了说明那不是指令
#: 而是粘进来的大块材料（日志 / 数据），那种该走摘要。
_PRESERVE_USER_MAX_CHARS = 4000
#: 最多保留多少条 —— 防止一个几百轮的 run 把历史 user 消息堆成新的膨胀源。
_PRESERVE_USER_MAX_COUNT = 12


def _preserve_user_messages(
    middle: list[LLMMessage],
    already_kept: list[LLMMessage],
) -> list[LLMMessage]:
    """从被压缩的中间段里挑出 user 消息逐字留下。

    只留**真正的用户/上游指令**：
      - 跳过 tool_result（它们 role 是 tool 或带 tool_call_id，属派生物）
      - 跳过已经被 keep_tool_calls 保留的，避免重复
      - 超长的不留（那是粘进来的材料不是指令，该走摘要）
      - 总条数封顶，取**最近的**（越近的约束越可能还有效）
    """
    kept_ids = {id(m) for m in already_kept}
    picked: list[LLMMessage] = []
    for m in middle:
        if id(m) in kept_ids:
            continue
        if getattr(m, "role", None) != "user":
            continue
        if getattr(m, "tool_call_id", None):        # tool_result 走 user role 的情况
            continue
        content = getattr(m, "content", None)
        if not content or not str(content).strip():
            continue
        if len(str(content)) > _PRESERVE_USER_MAX_CHARS:
            continue
        picked.append(m)
    return picked[-_PRESERVE_USER_MAX_COUNT:]


async def _strategy_llm(ctx: SummarizerContext) -> list[LLMMessage]:
    """LLM 主导：把中间段交给 LLM 压缩为一段叙述。

    保留：初始 system + 最近 N 轮原样 + keep_tool_calls 涉及的工具调用。
    替换：中间段 → 单条 system message（包含 LLM 生成的压缩叙述）。

    v1.6 改进：
      - running summary：如果 head 含上次压缩 notice，merge 其摘要 + 新 middle
        → 一段连贯新摘要（避免 N 段叙述碎片）
      - 失败 fallback 改 drop_tool_results（不是 truncate，损失少）
      - 摘要 save_artifact(type='compression_log') 落盘可审计
      - transcript 含 before/after tokens
    """
    cfg = ctx.harness.summarizer
    head, middle, tail = split_for_compression(ctx.messages, cfg.keep_last_n_turns)

    if not middle:
        return ctx.messages

    tokens_before = ctx.estimated_tokens
    keep_from_middle = extract_keep_tool_pairs(middle, cfg.keep_tool_calls)

    raw_text = _render_messages_for_compression(middle)
    instruction = cfg.instruction.strip() or _DEFAULT_LLM_INSTRUCTION
    target_tokens = max(500, cfg.target_tokens)

    # v1.6 H: running summary —— 检测上次压缩留下的摘要，prepend 给 LLM 作 baseline
    prior_summary = _extract_prior_summary(head)
    if prior_summary:
        user_content = (
            f"以下是节点 `{ctx.harness.node_type}` 的现有账本 + 新增中间 messages。\n\n"
            f"## 现有账本（增量更新的基底）\n{prior_summary}\n\n"
            f"## 新增 messages（共 {len(middle)} 条，估算 {estimate_tokens(middle)} tokens）\n"
            f"────────\n{raw_text}\n────────\n\n"
            f"输出**完整的新账本**（同样四个 section，≈ {target_tokens} tokens）。"
            f"增量合并，不是重写：现有账本里未被新信息推翻的条目**原样保留**"
            f"（措辞都不要动 —— 每改写一次就丢一点细节，几十轮后账本会退化成"
            f"套话）；新信息只落到它所属的 section；被推翻的条目改写并注明。"
        )
    else:
        user_content = (
            f"以下是节点 `{ctx.harness.node_type}` 的中间历史 messages "
            f"（共 {len(middle)} 条，估算 {estimate_tokens(middle)} tokens）。"
            f"请按要求压缩。压缩目标 ≈ {target_tokens} tokens。\n\n"
            f"────────\n{raw_text}\n────────"
        )

    compression_messages = [
        opening_system_prompt(instruction),
        LLMMessage(role="user", content=user_content),
    ]

    # 压缩是**内务调用**，不是给用户看的回复：必须用不挂 stream_display 的独立
    # 实例（issue #166）。以前直接用 ctx.llm，在 chat.py 里就是主 orchestrator 那
    # 个挂了显示回调的实例 —— 压缩摘要会顶着 `🔬 Orchestrator` header 流到用户屏
    # 幕上，还把每轮只打一次的 header_shown 消耗掉，导致真正的回复反而没了 header。
    # 假 llm（测试 / 其它调用方）没有 spawn_silent → 原样用 ctx.llm，行为不变。
    compress_llm = ctx.llm
    _spawn = getattr(ctx.llm, "spawn_silent", None)
    if callable(_spawn):
        try:
            compress_llm = _spawn()
        except Exception:
            log.warning("spawn_silent 失败，压缩回退复用原 llm 实例", exc_info=True)

    try:
        resp = await compress_llm.chat(
            compression_messages,
            tools=None,
            max_tokens=min(8192, target_tokens + 1000),
            temperature=0.2,
        )
        summary_text = (resp.content or "").strip()
        if isinstance(resp.usage, dict) and ctx.state is not None:
            ctx.state.tokens_used += int(resp.usage.get("total_tokens") or 0)
    except Exception as e:
        log.error("Summarizer LLM 调用失败：%s。回退到 drop_tool_results。", e)
        return await _strategy_drop_tool_results(ctx)

    if not summary_text:
        log.warning("Summarizer LLM 返回空，回退到 drop_tool_results。")
        return await _strategy_drop_tool_results(ctx)

    # ── 账本 schema 校验（仅默认指令时适用；节点自定义 instruction 自行负责）──
    # 先机械剥口水，再查 section 齐不齐。缺就给**一次**指名纠偏的机会；仍缺则
    # fail-loud 回退 drop —— 结构坏掉的账本比没有账本更糟（它会以权威口吻
    # 继承到之后的每一轮）。
    if instruction is _DEFAULT_LLM_INSTRUCTION:
        summary_text = _sanitize_ledger(summary_text)
        missing = _ledger_missing_sections(summary_text)
        if missing:
            try:
                retry_resp = await compress_llm.chat(
                    [*compression_messages,
                     LLMMessage(role="assistant", content=summary_text),
                     LLMMessage(role="user", content=(
                         f"上一次输出缺 section：{'、'.join(missing)}。"
                         f"重新输出**完整**账本，四个 section 一个不缺，"
                         f"第一行就是 '## 目标'。"))],
                    tools=None,
                    max_tokens=min(8192, target_tokens + 1000),
                    temperature=0.2,
                )
                retry_text = _sanitize_ledger((retry_resp.content or "").strip())
                if isinstance(retry_resp.usage, dict) and ctx.state is not None:
                    ctx.state.tokens_used += int(retry_resp.usage.get("total_tokens") or 0)
                if not _ledger_missing_sections(retry_text):
                    summary_text = retry_text
                    missing = []
                else:
                    missing = _ledger_missing_sections(retry_text)
            except Exception as e:
                log.warning("账本纠偏重试失败：%s", e)
        if missing:
            if ctx.state is not None:
                try:
                    ctx.state.append_transcript(
                        "ledger_schema_invalid", turn=ctx.turn,
                        missing_sections=missing,
                    )
                except Exception:
                    pass
            log.warning("账本缺 section %s（纠偏后仍缺），回退 drop_tool_results。", missing)
            return await _strategy_drop_tool_results(ctx)

    notice = build_compression_notice(
        ctx.turn,
        f"把中间 {len(middle)} 条 messages 压成下面这段。",
        body=(f"{_COMPRESSION_SUMMARY_HEADER}\n{summary_text}\n\n"
              f"（{len(keep_from_middle)} 条 keep_tool_calls 相关 messages "
              f"已原样保留在下方。）"),
    )

    # 压派生物，留真相源：middle 里的 **user 消息逐字保留**，不交给摘要器。
    #
    # head 只护住了第一条用户输入；后续的 steering、resume 回答、人工补充的
    # 约束都落在 middle 里，会被折进叙述。而摘要是模型转述的产物 —— 把
    # 「用现成的 retry helper，别新写一个」paraphrase 一遍，正是它之后自信地
    # 做了被明确禁止的事的那条路径。指令不是可以概括的素材。
    preserved_users = _preserve_user_messages(middle, keep_from_middle)

    new_messages = [
        *_head_without_superseded_notices(head), notice,
        *preserved_users, *keep_from_middle, *tail,
    ]

    # v1.6 I: 摘要落盘（compression_log artifact）便于 user 事后审计
    try:
        tokens_after = estimate_tokens(new_messages)
        ctx.state.save_artifact(
            artifact_type="compression_log",
            name=f"compression_turn_{ctx.turn}",
            content=summary_text,
            metadata={
                "turn": ctx.turn,
                "strategy": "llm",
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "compression_ratio": (
                    round(tokens_after / tokens_before, 3)
                    if tokens_before > 0 else None
                ),
                "messages_compressed": len(middle),
                "messages_kept_by_tool_calls": len(keep_from_middle),
                "running_summary_merged": prior_summary is not None,
            },
        )
    except Exception as e:
        log.debug("compression_log artifact 落盘失败（不致命）：%s", e)

    return new_messages


register_strategy("llm", _strategy_llm)


# ── 可逆清除层（v3.4）——压缩的第一层，先于任何有损策略 ────────────────────
#
# 真实数据（214 个 checkpoint 实测）：中段 92–99% 是 tool result。其中绝大多数
# 要么可以重读（read_artifact / read_file / search_kb …——参数就在前一条
# assistant 消息里），要么落了盘（run_node 的子 run 目录）。对这些内容做 LLM
# 摘要是**花钱把无损信息变有损**；正确动作是清内容、留结构、留恢复指针。
#
# 四条纪律：
#   1. 不写工具名单 —— 扫注册表的 replayable_read / result_compactor 声明；
#      新工具不声明就不清（fail-closed）。
#   2. 保护区按 token 不按轮 —— 实测有 498k 的 checkpoint 只有 ≤3 条 assistant
#      消息，按轮保护会把全部内容罩住，摘要器整个失效。
#   3. 只原位替换 content，不增删消息、不动顺序 —— 第一条被清消息之前的前缀
#      逐字节不变，prompt cache 前缀继续命中。
#   4. error 结果不清 —— 失败是模型自我修正的信号（重复失败熔断依赖它）。

#: 已清除标记 —— **转发工作集的那个**，不另立一个。
#:
#: 曾经这里有一份独立的标记文本，而 `core/context_view` 有另一份。两份标记
#: 意味着"这条已经清过了吗"有两个答案：谁用哪一份判断，取决于它碰巧 import
#: 了谁 —— 幂等检查、跳过逻辑、测试判据会各自认一半，而且都不报错。
from core.context_view import TOMBSTONE_PREFIX as _CLEARED_MARKER
_COMPACTED_MARKER = "[压缩的 run_node 结果"
_CLEAR_TARGET_RATIO = 0.5     # 清到有效窗的这个比例就停（低水位）
_CLEAR_KEEP_RECENT_PER_TOOL = 2   # 每个工具最近 N 条结果不清（近因性）
_CLEAR_PROTECT_TAIL_TOKENS = 12_000  # 最近这么多 tokens 内的结果不清
#: 超过这个大小的结果**永不受保护**。近因保护是给正常大小的工作集的；真实
#: 数据里 466k tokens 的单条 read_artifact 恰好是"该工具最近一条"，按近因
#: 保护它就等于整个策略失效（回放实测四个 checkpoint 一条都清不动）。
#: 巨型结果恰恰是要清的对象 —— 它可恢复（有指针），且新的有界读会防止复发。
_CLEAR_PROTECT_MAX_RESULT_TOKENS = 6_000


def _clearable_tools() -> dict[str, object]:
    """扫工具注册表：name → compactor（None 表示走通用重读占位符）。"""
    from core.tool_registry import _REGISTRY

    out: dict[str, object] = {}
    for name, td in _REGISTRY.tools.items():
        compactor = getattr(td, "result_compactor", None)
        if compactor is not None:
            out[name] = compactor
        elif getattr(td, "replayable_read", False):
            out[name] = None
    return out


def _result_looks_like_error(content: str) -> bool:
    head = (content or "")[:200]
    return '"status": "error"' in head or "'status': 'error'" in head


async def _strategy_clear_tool_results(ctx: SummarizerContext) -> list[LLMMessage]:
    """把工具结果清出上下文，直到低水位 —— **委托给工作集，不再自己实现一遍**。

    ## 为什么这里只剩一个转发

    这个策略和 `core/context_view.ContextView` 是同一个问题的两份答案：
    「工具结果占满了 context，清掉哪些、留下什么痕迹」。两份答案必然分叉，而且
    分叉时两边都不报错 —— 这里曾经有一套独立的保护区 / 紧凑器 / 占位符逻辑，
    与工作集的墓碑各说各话。

    更糟的是它们对**同一条消息**的判断可能相反：这边把某条清成占位符，工作集
    那边仍记着它 live，于是 `is_live` 说"完整答案就摆在眼前"，lookup 回一个
    "往上翻"的指针，而模型翻到的是一个占位符 —— 翻不到就只能再问，再拿到同一个
    指针。框架把它锁死在一个无法满足的指令上。

    工作集本来就在每轮做这件事（`enforce_budget`，LRU + 墓碑 + 可从 run log
    完整取回）。这里要的"降到低水位"只是同一个动作换一个目标值。
    """
    view = _context_view_of(ctx.state)
    window = effective_context_window(ctx.harness, ctx.state)
    # 低水位与 per-turn 驱逐共用同一个预算 owner（issue #710：三个各自为政的
    # 预算互相不一致，三层全绿、网关 400）。取两者中更紧的：清除层的存在意义
    # 就是"压得比日常更狠一点"。
    target_bytes = min(
        int(window * _CLEAR_TARGET_RATIO) * 4,
        derived_tool_budget_bytes(ctx.harness, ctx.state, ctx.messages),
    )
    dropped = view.enforce_budget(ctx.messages, max_bytes=target_bytes,
                                  protect_turn_ge=ctx.turn - 1)
    out = view.apply(ctx.messages) if dropped else ctx.messages

    if dropped and ctx.state is not None:
        try:
            ctx.state.append_transcript(
                "summarizer_cleared_tool_results", turn=ctx.turn,
                cleared=dropped, target_bytes=target_bytes, **view.stats(),
            )
        except Exception:
            pass
    return out


def _context_view_of(state):
    """拿本 run 的工作集；没有 state 就现造一个临时的。

    清除能力**不该依赖 state 存在**。裸调用（CLI、测试、外部构造的 messages）
    照样可能拿着一份 50 万 token 的历史，照样需要清。临时工作集少的只是 run log
    备份 —— 它 adopt 现场的 tool 消息、按预算驱逐、留墓碑，一样不丢结构。
    可重读的工具重调即取回（真跑一次），不可重读的墓碑本来就写着"别重放"。

    **退化成慢，不退化成错。**
    """
    from core import context_view as _cv

    if state is not None and getattr(state, "hook_state", None) is not None:
        try:
            return _cv.get(state)
        except Exception:
            pass
    return _cv.ContextView(state)


async def _strategy_escalate(ctx: SummarizerContext) -> list[LLMMessage]:
    """默认策略：先可逆清除；只有还不够时才付 LLM 摘要的钱（和信息损失）。"""
    cleared = await _strategy_clear_tool_results(ctx)
    window = effective_context_window(ctx.harness, ctx.state)
    est = effective_prompt_tokens(estimate_tokens(cleared), ctx.state)
    threshold = int(window * float(ctx.harness.summarizer.trigger_threshold or 0.7))
    if est <= threshold:
        return cleared
    ctx2 = SummarizerContext(
        harness=ctx.harness, state=ctx.state, messages=cleared,
        estimated_tokens=est, llm=ctx.llm, turn=ctx.turn,
    )
    return await _strategy_llm(ctx2)


register_strategy("clear_tool_results", _strategy_clear_tool_results)
register_strategy("escalate", _strategy_escalate)



# ── helper：messages → 纯文本（给压缩 LLM 看）─────────────────────────────

def _render_messages_for_compression(messages: list[LLMMessage]) -> str:
    """把 messages 列表渲染成可读的纯文本块。"""
    lines: list[str] = []
    for m in messages:
        role = m.role
        if role == "assistant":
            text = (m.content or "").strip()
            if text:
                lines.append(f"[assistant]: {text}")
            if m.tool_calls:
                for tc in m.tool_calls:
                    fn = (tc.get("function") or {})
                    name = fn.get("name", "?")
                    args = (fn.get("arguments") or "")[:300]
                    lines.append(f"[assistant tool_call]: {name}({args})")
        elif role == "tool":
            text = (m.content or "")[:400]
            lines.append(f"[tool {m.name or '?'}]: {text}")
        elif role == "user":
            # 框架注入的提示走的是 role="user" 信封（见 loop_hooks._as_framework_notices）。
            # 在摘要里把它标成 `[user]`，等于让下一轮把框架自己说的话当成用户诉求 ——
            # 摘要是会被反复喂回模型的，这个错标会一路传下去。
            tag = "framework-notice" if is_framework_notice(m) else "user"
            lines.append(f"[{tag}]: {(m.content or '')[:500]}")
        elif role == "system":
            lines.append(f"[system]: {(m.content or '')[:300]}")
    return "\n".join(lines)
