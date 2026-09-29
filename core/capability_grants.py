"""受控能力授权：撞墙时问人，答复在本次 run 内生效（#770）。

## 现状是什么

出网白名单只有五个域（pypi / files.pythonhosted / github / githubusercontent /
zenodo），**全是装软件用的，没有一个科学数据源**。于是每个真实数据任务都会撞墙，
而撞墙之后节点只能打印一句"把这个域加进 HARNESS_SANDBOX_EGRESS_ALLOWLIST 后重试"
—— 需要人去改环境变量、重启服务、重跑课题。节点侧的测试还专门钉死了
"不许悄悄写进 os.environ"，那是对的：一个 agent 能自己改自己的墙就没有墙。

缺的不是"放松一点"，是**一条通道**：让 agent 能在撞墙的那一刻把这件事呈到人面前，
人点一下，本次 run 继续。

## 这里提供什么

* :func:`request_network_grant` —— 走**现有的 HITL pause 通道**（和
  ``request_human_input`` 同一条路，不新造一套），把"要连哪个主机、为什么、
  谁在问"呈上去。
* :func:`grant_from_answer` —— 把人的答复翻译成一条授权记录。
* :func:`is_granted` / :func:`granted_hosts` —— 给调用方查"现在准不准"。

## 三条不打算让步的

**一、授权按 run 计，不落盘、不进环境。** 一次授权只在这一次 run 里算数。
写进 os.environ 等于让 agent 改自己的墙；写进磁盘等于建一份没人复核、只会变长的
名单。要长期放行某个域，那是人去改部署配置的事，是一次有意的决定，不该由
"agent 问了一次、人点了一下"顺带完成。

**二、主机逐个批，不批通配。** 批 ``data.example.org`` 不等于批
``*.example.org``。父域放行会让一次点击的含义变得没边 —— 人以为批了一个数据源，
实际批了别人整个域下的任何主机。

**三、没答就是没批。** 拿不到答复（无人值守、超时、人没看见）一律按未授权，
不许因为"问过了"就放行。问过和批过是两件事。

## 为什么不写成"记住这个域"

那会让白名单悄悄变长、没人复核。真到了一天要点五次的时候再加"记住"，
而且那时它应该是一个显式的、带审计的动作，不是一个顺手的复选框。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: pause 载荷里标记这是一次能力授权请求。UI 可据此画确认卡而不是普通问答框。
GRANT_PAUSE_TYPE = "capability_grant"

#: state 上挂授权记录的属性名。**只在内存里**，不进 state 的持久化字段。
_ATTR = "_capability_grants"

#: 人答什么算"准"。大小写无关，两端空白忽略。
_YES = frozenset({"allow", "yes", "y", "grant", "approve", "ok",
                  "允许", "同意", "批准", "可以", "准"})
_NO = frozenset({"deny", "no", "n", "refuse", "reject",
                 "拒绝", "不允许", "不同意", "不行"})


@dataclass(frozen=True)
class NetworkGrant:
    """一次"准你连这个主机"的记录。"""

    host: str
    reason: str
    asked_by_node_type: str = ""
    asked_by_run_id: str = ""
    answer: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "reason": self.reason,
            "asked_by_node_type": self.asked_by_node_type,
            "asked_by_run_id": self.asked_by_run_id,
            "answer": self.answer,
        }


def normalize_host(raw: str) -> str:
    """取出可比较的主机名：去 scheme、去路径、去端口、小写。

    **用 `urlsplit` 拆，不手拼**（#1068 补充三第 3 条）。手拼那版先按第一个 `/`
    截断、再取最后一个 `@` 之后，不认 `?` 和 `#`：

        https://evil.example?@psl.noaa.gov/data/x.nc
            手拼 → psl.noaa.gov          ← 卡片上问的
            urlsplit().hostname → evil.example   ← curl / git / wget 真正要连的

    确认卡上问的主机和请求真正要连的主机不是同一个，人批的就不是他以为的那件事。
    判据只能是**真正解析这条 URL 的那套语法**，不能是我们自己再写一遍。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    from urllib.parse import urlsplit

    candidate = text if "://" in text else f"//{text}"
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        # 畸形 URL（非法 IPv6 括号之类）—— 认不出来就不认，**不回落到手拼**：
        # 回落等于把刚拆掉的那套规则又请回来，而且只在畸形输入上生效。
        return ""
    return host.strip().lower().rstrip(".")


#: 确认卡的选项。**纯字符串**，不是 `{label, description}` 对象（#1068 补充三第 2 条）。
#:
#: 那两个 dict 一路把这条链堵死：平台 `PendingApprovalOut.options: list[str]` 校验
#: 失败 → 会话详情与会话列表 **500**，人连这条会话都打不开；前端 `map(String)` 之后
#: 点任一选项发出的都是 `"[object Object]"`；CLI 把它原样印成
#: `[1] {'label': '允许', …}`。高危确认卡一直用的就是纯字符串，这条没有理由另立一套。
_ALLOW_OPTION_PREFIX = "允许"
_DENY_OPTION_PREFIX = "拒绝"


def grant_options(host: str) -> tuple[str, str]:
    """确认卡的两个选项（顺序固定：0=允许，1=拒绝）。"""
    return (
        f"{_ALLOW_OPTION_PREFIX} —— 本次 run 内准许连接 {host}",
        f"{_DENY_OPTION_PREFIX} —— 不连它，节点换一条路或如实报卡住",
    )


def read_answer(answer: Any) -> bool | None:
    """把人的答复读成 允许 / 拒绝 / 读不出来。

    认四种写法，顺序是**先结构、后自由文本**：
      1. 结构化答复 `{"choice_id": "allow"|"deny"}`（平台按钮）；
      2. 裸序号 `1` / `2`（CLI 界面印的就是 `[1] …`）；
      3. 选项原文或它的前缀词（`允许 —— 本次 run…` / `允许`）；
      4. 关键词（`allow` / `同意` / `deny` / `不行` …）。

    读不出来返回 `None` —— 它和「拒绝」在记账上是两件事：拒绝是人做了决定，
    读不出来是**这条链某处坏了**，而把后者记成前者会让那处坏掉的地方永远查不到。
    调用方对两者的处理可以一样（都不批），但不能分不清。
    """
    if isinstance(answer, dict):
        choice = str(answer.get("choice_id") or answer.get("choice") or "").strip().lower()
        if choice in {"allow", "grant", "approve"}:
            return True
        if choice in {"deny", "refuse", "reject"}:
            return False
        answer = answer.get("response") or answer.get("note") or ""
    text = str(answer or "").strip()
    if not text:
        return None
    if text in {"1", "[1]", "（1）", "(1)"}:
        return True
    if text in {"2", "[2]", "（2）", "(2)"}:
        return False
    lowered = text.lower()
    if text.startswith(_ALLOW_OPTION_PREFIX):
        return True
    if text.startswith(_DENY_OPTION_PREFIX):
        return False
    first = lowered.split()[0].strip("。．.,:：、") if lowered.split() else ""
    if first in _NO or lowered in _NO:
        return False
    if first in _YES or lowered in _YES:
        return True
    return None


def _in_deployment_allowlist(host: str) -> bool:
    """这个主机在**部署白名单**里吗（不是本 run 的授权）。

    口径与取物端一致：host 等于该域，或是它的子域。延迟 import 免得
    `core.sandbox` 与本模块互相 import。
    """
    if not host:
        return False
    try:
        from core.sandbox import effective_egress_policy

        entries = effective_egress_policy().get("from_environment") or []
    except Exception:
        return False
    for entry in entries:
        domain = str(entry or "").strip().lower().lstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


def _grants(state: Any) -> dict[str, NetworkGrant]:
    existing = getattr(state, _ATTR, None)
    if not isinstance(existing, dict):
        existing = {}
        try:
            setattr(state, _ATTR, existing)
        except Exception:               # state 可能是只读替身
            return {}
    return existing


def granted_hosts(state: Any) -> tuple[str, ...]:
    """本次 run 已经批过的主机，排序后返回（给记账和展示用）。"""
    return tuple(sorted(_grants(state)))


def is_granted(state: Any, host_or_url: str) -> bool:
    """这个主机现在准不准连。

    **逐个主机比，不做父域匹配**：批了 ``data.example.org`` 不等于批了
    ``evil.example.org``。
    """
    host = normalize_host(host_or_url)
    return bool(host) and host in _grants(state)


def request_network_grant(
    state: Any,
    host_or_url: str,
    reason: str,
    *,
    what_for: str = "",
    redirect_of: str = "",
) -> dict[str, Any]:
    """把"要连这个主机"呈到人面前。返回一个 pause 结果。

    走的是和 ``request_human_input`` 同一条 HITL 通道 —— 不新造一套。
    UI 可以按 ``metadata.type == "capability_grant"`` 画确认卡。

    ``reason`` 是给人看的一句话："为什么这一步需要连它"。人就靠这句判断，
    所以调用方别写"需要访问网络"这种没有信息的话。
    """
    host = normalize_host(host_or_url)
    if not host:
        raise ValueError("request_network_grant needs a host or URL")
    question = f"允许这次课题连接 {host} 吗？"
    context_lines = [
        f"主机：{host}",
        f"用途：{reason.strip() or '（调用方没说，这本身是个问题）'}",
    ]
    if what_for:
        context_lines.append(f"要取的东西：{what_for}")
    if redirect_of:
        origin = normalize_host(redirect_of)
        # 来路有**三种**，不是两种（#1068 补充四）。
        #
        # 此前只问一句 `is_granted(state, origin)`，于是「部署白名单早就放行的来路」
        # 和「来路不明」落到同一句话上 —— 两行除主机名外一字不差，都写着"这是在
        # 换源"。而 huggingface.co → us.aws.cdn.hf.co 这种正当伴随域恰恰是前者。
        if origin and is_granted(state, origin):
            context_lines.append(
                f"来路：{origin} 把请求 302 到了这里（{origin} 本次已获授权）。"
                "（实测活例：huggingface.co 的权重文件会重定向到 us.aws.cdn.hf.co ——"
                "声明得再完整也只有跑到那一步才知道。）"
            )
        elif origin and _in_deployment_allowlist(origin):
            context_lines.append(
                f"来路：{origin} 把请求 302 到了这里（{origin} 在**部署白名单**里，"
                "本次 run 没有单独授权过它）。伴随域常见于 CDN 下载。"
            )
        elif origin:
            context_lines.append(
                f"⚠️ 声称是 {origin} 重定向过来的，而 {origin} 既不在部署白名单里、"
                "本次 run 也没授权过 —— 这不像伴随域，更像在换源。"
            )
    context_lines.append(
        "批准只在**本次 run** 内有效：不写进环境变量、不落盘、不含子域。"
        "要长期放行请改部署配置。"
    )
    return {
        "status": "pause",
        "pause_event": {
            "question": question,
            "context": "\n".join(context_lines),
            "header": "出网授权",
            "asking_node_type": getattr(state, "node_type", "") or "",
            "asking_run_id": getattr(state, "run_id", "") or "",
            "options": list(grant_options(host)),
            "recommended_option_index": 1,
            "metadata": {
                "type": GRANT_PAUSE_TYPE,
                "header": "出网授权",
                "capability": "network_egress",
                "host": host,
                "reason": reason,
                "redirect_of": normalize_host(redirect_of) if redirect_of else "",
            },
        },
    }


def grant_from_answer(
    state: Any,
    host_or_url: str,
    answer: str | None,
    *,
    reason: str = "",
) -> bool:
    """把人的答复落成授权。返回是否批准。

    **没答就是没批**：``None`` / 空串 / 认不出来的答复一律不批。无人值守下
    这条尤其重要 —— 没人看见的问题不能因为"问过了"就算批过。
    """
    host = normalize_host(host_or_url)
    if not host:
        return False
    verdict = read_answer(answer)
    if verdict is not True:
        return False
    _grants(state)[host] = NetworkGrant(
        host=host,
        reason=reason,
        asked_by_node_type=getattr(state, "node_type", "") or "",
        asked_by_run_id=getattr(state, "run_id", "") or "",
        answer=str(answer or ""),
    )
    return True


def grants_ledger(state: Any) -> list[dict[str, Any]]:
    """本次 run 批过的全部授权 —— 给记账/收尾清单用。"""
    return [g.as_dict() for _, g in sorted(_grants(state).items())]
