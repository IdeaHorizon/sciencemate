"""每次 LLM 调用记一行：token 用量、缓存命中、金额。

**为什么需要它**：改造前 `state.tokens_used` 只有一个 total 计数，全仓
`cost_usd|price|per_1k` grep 零命中，`cache_read|cache_creation` 同样零命中。
于是两个问题回答不了：

  1. 这个 run 花了多少钱？7 个 provider 计价方式不同（团队 MaaS / 自建
     GPUStack / 外部 API），只数 token 无法比较，也无法把"某个 judge 烧了
     143M token"归因到具体节点和模型。
  2. prompt 前缀稳不稳定？缓存命中率是判据 —— 没有它，前缀稳定性改造做完
     无法证明有效，退化时也无法定位。

**设计约束**

- *不报的字段记 unknown，不记 0*。provider 不返回缓存字段时写 0，会让"没有
  缓存机制"和"缓存一次没命中"变成同一个数字 —— 静默降级不可接受。
- *价格表可覆盖且允许配 0*。自建模型没有账单，硬塞一个单价只会让总额变成
  假数字；配 0 表示"不计费"，与"未配价格"（unknown）是两件事。
- *记账失败绝不打断主流程*。这是旁路观测，任何异常都吞掉并降级。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

LEDGER_FILENAME = "llm_cost.jsonl"


# ── 价格表 ─────────────────────────────────────────────────────────────────
# 单位：美元 / 每 1M token。None = 未配价格（unknown），0.0 = 明确不计费。
# 覆盖方式：环境变量 HARNESS_LLM_PRICES 传 JSON，键是 "<provider>:<model>"
# 或 "<model>"（前者优先）。例：
#   HARNESS_LLM_PRICES='{"gpustack:deepseek-v4-pro":{"in":0,"out":0}}'

@dataclass(frozen=True)
class Price:
    input_per_m: float | None = None
    output_per_m: float | None = None
    cache_read_per_m: float | None = None      # 缺省时按 input 的 10% 估
    currency: str = "USD"

    def is_known(self) -> bool:
        return self.input_per_m is not None or self.output_per_m is not None


_DEFAULT_PRICES: dict[str, Price] = {
    # 自建 / 团队内推理：不产生外部账单，明确记 0 而不是 unknown
    "gpustack": Price(0.0, 0.0, 0.0),
    "maas": Price(0.0, 0.0, 0.0),
    # 外部 API（公开报价，按 1M token）
    "deepseek-chat": Price(0.27, 1.10, 0.027),
    "deepseek-v4-flash": Price(0.14, 0.28, 0.014),
    "deepseek-v4-pro": Price(1.74, 3.48, 0.174),
    "glm-5.1": Price(0.60, 2.20, 0.06),
}


def _price_overrides() -> dict[str, Price]:
    raw = os.environ.get("HARNESS_LLM_PRICES")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    out: dict[str, Price] = {}
    for key, v in (data or {}).items():
        if not isinstance(v, dict):
            continue
        out[str(key)] = Price(
            input_per_m=_f(v.get("in", v.get("input_per_m"))),
            output_per_m=_f(v.get("out", v.get("output_per_m"))),
            cache_read_per_m=_f(v.get("cache_read", v.get("cache_read_per_m"))),
        )
    return out


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def provider_label(base_url: str | None) -> str | None:
    """从 base_url 取一个稳定的 provider 标识（host[:port]）。

    `LLMClient` 上没有 provider 名字，只有 base_url 和 model。host 是事实、
    可归因（区分得开自建 GPUStack、团队 MaaS 和外部 API），适合做记账维度。
    **价格主要按 model 匹配** —— 决定单价的是模型不是机器。
    """
    if not base_url:
        return None
    s = str(base_url).strip()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return (s.split("/", 1)[0] or None) if s else None


_SELF_HOSTED = Price(0.0, 0.0, 0.0)


def is_self_hosted(provider: str | None) -> bool:
    """私有网段 / 内网主机 = 自建推理，不产生外部账单。

    没有这条判断，跑在自建 GPUStack 上的开放权重模型（deepseek-v4-pro 之类）
    会按同名商业 API 的公开报价计费 —— 那个金额是**编出来的**，比 unknown 更糟。
    """
    if not provider:
        return False
    host = str(provider).split(":", 1)[0].strip().lower()
    if host in ("localhost",) or host.endswith((".local", ".internal")):
        return True
    if host.endswith(".ts.net"):          # tailnet
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or a == 127:
            return True
        if a == 192 and b == 168:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
    return False


#: 会真的给你开账单的商业 API 域名。**只有这些主机才按模型名套用公开报价。**
#:
#: 光排除私有网段不够：实测 `zju.hpc.pub:17882` 是自建 HPC 端点，跑着开放权重的
#: deepseek-v4-pro，既不是私有 IP 也不是内网域名 —— 于是按 DeepSeek 官方报价算出
#: 了 $0.61 的账单，而真实账单是 0。模型名说明"跑的是什么权重"，不说明"谁在收钱"。
_VENDOR_HOSTS = (
    "api.deepseek.com",
    "api.openai.com",
    "api.anthropic.com",
    "open.bigmodel.cn",
    "dashscope.aliyuncs.com",
    "ark.cn-beijing.volces.com",
    "api.moonshot.cn",
    "generativelanguage.googleapis.com",
)


def is_vendor_host(provider: str | None) -> bool:
    if not provider:
        return False
    host = str(provider).split(":", 1)[0].strip().lower()
    return any(host == v or host.endswith("." + v) for v in _VENDOR_HOSTS)


def lookup_price(provider: str | None, model: str | None) -> Price:
    """覆盖表 > 自建判定 > 商业 vendor 的模型报价 > 未知。

    **部署位置决定「有没有账单」，模型名只决定「如果有账单，单价多少」。**
    所以模型名的默认报价只在确认是商业 vendor 主机时才套用；主机不认识时返回
    unknown，而不是拿开放权重模型的公开报价编一个金额出来 —— 编出来的金额比
    unknown 更糟，因为它看起来像真的。运维要给自建端点记内部成本，配 override。
    """
    ov = _price_overrides()
    for key in (f"{provider}:{model}", str(model or ""), str(provider or "")):
        if key and key in ov:
            return ov[key]
    # provider 自身被显式定过价（gpustack / maas 这类裸标签）→ 直接用
    if provider and str(provider) in _DEFAULT_PRICES:
        return _DEFAULT_PRICES[str(provider)]
    if is_self_hosted(provider):
        return _SELF_HOSTED
    # 认得出是商业 vendor 才按模型名套公开报价；否则宁可 unknown 也不猜。
    if provider and not is_vendor_host(provider):
        return Price()
    for key in (f"{provider}:{model}", str(model or ""), str(provider or "")):
        if key and key in _DEFAULT_PRICES:
            return _DEFAULT_PRICES[key]
    return Price()


# ── usage 归一 ─────────────────────────────────────────────────────────────
# 各家字段名不同，且"没有这个字段"必须与"这个字段是 0"区分开。

@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read: int | None = None        # None = provider 没报（unknown）
    cache_write: int | None = None

    def cache_hit_ratio(self) -> float | None:
        """缓存读 / 输入 token。provider 不报缓存字段时返回 None。"""
        if self.cache_read is None or self.prompt_tokens <= 0:
            return None
        return min(1.0, self.cache_read / self.prompt_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


def normalize_usage(usage: dict | None) -> Usage:
    """把各家 usage 方言归一。

    覆盖的方言：
      - OpenAI 兼容：`prompt_tokens_details.cached_tokens`
      - Anthropic：`cache_read_input_tokens` / `cache_creation_input_tokens`
      - DeepSeek：`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`

    缓存字段一个都没出现 → cache_read 保持 None（unknown），**不写 0**。
    """
    u = usage or {}
    pt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    ct = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    tt = int(u.get("total_tokens") or 0) or (pt + ct)

    cache_read: int | None = None
    cache_write: int | None = None

    details = u.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        cache_read = int(details.get("cached_tokens") or 0)

    if u.get("cache_read_input_tokens") is not None:
        cache_read = int(u.get("cache_read_input_tokens") or 0)
    if u.get("cache_creation_input_tokens") is not None:
        cache_write = int(u.get("cache_creation_input_tokens") or 0)

    if u.get("prompt_cache_hit_tokens") is not None:
        cache_read = int(u.get("prompt_cache_hit_tokens") or 0)

    return Usage(pt, ct, tt, cache_read, cache_write)


def compute_cost(usage: Usage, price: Price) -> float | None:
    """未配价格 → None（unknown）。配了 0 → 0.0。"""
    if not price.is_known():
        return None
    inp = price.input_per_m or 0.0
    out = price.output_per_m or 0.0
    cache_rate = price.cache_read_per_m
    if cache_rate is None:
        cache_rate = inp * 0.1          # 常见定价：缓存读约为输入价的一成

    cached = usage.cache_read or 0
    fresh = max(0, usage.prompt_tokens - cached)
    total = (fresh * inp + cached * cache_rate + usage.completion_tokens * out) / 1_000_000
    return round(total, 6)


# ── 落盘 ───────────────────────────────────────────────────────────────────

def ledger_path(project_root: str | Path | None) -> Path | None:
    if not project_root:
        return None
    try:
        p = Path(project_root) / ".harness" / LEDGER_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def record(
    *,
    project_root: str | Path | None,
    run_id: str | None,
    node_type: str | None,
    provider: str | None,
    model: str | None,
    usage: dict | None,
    turn: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """记一行。返回写入的 row（测试用）；任何异常都吞掉 —— 观测不得打断主流程。"""
    try:
        u = normalize_usage(usage)
        price = lookup_price(provider, model)
        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "node": node_type,
            "provider": provider,
            "model": model,
            "turn": turn,
            **u.to_dict(),
            "cache_hit_ratio": u.cache_hit_ratio(),
            "cost_usd": compute_cost(u, price),
            "price_known": price.is_known(),
        }
        if extra:
            row.update(extra)
        path = ledger_path(project_root)
        if path is not None:
            with _LOCK:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
    except Exception:
        return None


# ── 汇总 ───────────────────────────────────────────────────────────────────

@dataclass
class Rollup:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read: int = 0
    cost_usd: float = 0.0
    cost_known_calls: int = 0            # 有价格的调用数
    cache_reported_calls: int = 0        # provider 报了缓存字段的调用数
    by_node: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, float] = field(default_factory=dict)

    @property
    def cache_hit_ratio(self) -> float | None:
        """整体缓存读占比。没有任何一次调用报过缓存字段 → None。"""
        if self.cache_reported_calls == 0 or self.prompt_tokens <= 0:
            return None
        return min(1.0, self.cache_read / self.prompt_tokens)

    @property
    def cost_is_partial(self) -> bool:
        """有调用缺价格 —— 总额是下界，不是真值。"""
        return self.cost_known_calls < self.calls


def read_rollup(project_root: str | Path | None, *,
                run_id: str | None = None) -> Rollup:
    r = Rollup()
    path = ledger_path(project_root)
    if path is None or not path.is_file():
        return r
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if run_id and row.get("run_id") != run_id:
                continue
            r.calls += 1
            r.prompt_tokens += int(row.get("prompt_tokens") or 0)
            r.completion_tokens += int(row.get("completion_tokens") or 0)
            if row.get("cache_read") is not None:
                r.cache_read += int(row.get("cache_read") or 0)
                r.cache_reported_calls += 1
            c = row.get("cost_usd")
            if c is not None:
                r.cost_usd += float(c)
                r.cost_known_calls += 1
                node = str(row.get("node") or "?")
                model = str(row.get("model") or "?")
                r.by_node[node] = round(r.by_node.get(node, 0.0) + float(c), 6)
                r.by_model[model] = round(r.by_model.get(model, 0.0) + float(c), 6)
    except Exception:
        pass
    r.cost_usd = round(r.cost_usd, 6)
    return r


def format_rollup(r: Rollup) -> str:
    """给 `hf cost` 用的一段人话。unknown 一律显式说出来。"""
    lines: list[str] = []
    lines.append(f"LLM 调用 {r.calls} 次｜输入 {r.prompt_tokens:,} / 输出 {r.completion_tokens:,} tokens")

    hit = r.cache_hit_ratio
    if hit is None:
        lines.append("缓存读占比：unknown（provider 未报缓存字段）")
    else:
        lines.append(f"缓存读占比：{hit:.1%}（{r.cache_read:,} tokens 命中）")

    if r.cost_known_calls == 0:
        lines.append("金额：unknown（相关 provider 未配价格）")
    else:
        suffix = ""
        if r.cost_is_partial:
            missing = r.calls - r.cost_known_calls
            suffix = f"（下界；{missing} 次调用未配价格）"
        lines.append(f"金额：${r.cost_usd:.4f}{suffix}")
        if r.by_node:
            top = sorted(r.by_node.items(), key=lambda kv: -kv[1])[:5]
            lines.append("  按节点：" + "  ".join(f"{k} ${v:.4f}" for k, v in top))
        if r.by_model:
            top = sorted(r.by_model.items(), key=lambda kv: -kv[1])[:5]
            lines.append("  按模型：" + "  ".join(f"{k} ${v:.4f}" for k, v in top))
    return "\n".join(lines)
