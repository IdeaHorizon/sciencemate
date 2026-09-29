"""工作集：工具结果在 context 里**至多一份活副本**，原文全保真落在磁盘。

## 这是 RFC_CONTEXT_AS_A_RENDERED_VIEW 的承重墙

在此之前，context 是一份 append-only 的对话日志，五套机制在上面同时动刀
（summarizer 压缩 / tool_call_cache 紧凑化 / findings_digest / task-state 保全 /
progress_breaker）。每一套都在补上一套的伤口，而所有伤口来自同一刀：
**把对话日志当工作记忆用，然后在历史上做有损编辑。**

最尖锐的证据是一对正面矛盾，两者还是为同一场事故（51,937 次 search_kb）建的：

  - summarizer 清除旧结果时承诺：「用同样参数重调即可取回」
  - tool_call_cache 把"用同样参数重调"定义成病：紧凑 → 报错 → 停机

模型完全照框架说的做，却被框架惩罚。

## 不变量

    任意一个 (tool, canonical_args)，在 context 里**至多一份活副本**。

三条推论，正是上面那堆机制想要而没做到的：

1. **重读免费且完整。** 副本被驱逐后再调，从 run log 原样取回，不再有"第 3 次
   起给你截断版"。summarizer 的承诺第一次成为真话。
2. **重读不膨胀 context。** 新副本进来时，同 key 的旧副本立刻变墓碑。读一百次
   和读一次占一样的空间 —— 所以根本不需要靠"惩罚重复"来控体积。
3. **循环判据变精确。** 真正的病态是「答案正完整摆在眼前，它还在问」，也就是
   请求一个 **live** 的 key。被驱逐后的重读是框架规定的恢复路径，不是病。
   30 / 8 / 12 这三个拍脑袋的阈值随之失去存在理由。

## 磁盘 run log 是权威，context 是视图

原文一律先落 `runs/<run_id>/tool_results.jsonl`（append-only，**永不编辑**），
context 里放的只是它的一个渲染。框架对 context 的唯一合法动作因此只剩两种：
**逐字呈现**，或**换成墓碑**（指明怎么取回）。有损转述占原件的位这件事，
在结构上不可能发生。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 墓碑前缀 —— 机械可辨，别用来做语义判断以外的事。
TOMBSTONE_PREFIX = "[工具结果已从上下文移出]"


def canonical_key(tool_name: str, args: dict | None) -> str:
    """与 tool_call_cache.cache_key 同一形状：(工具, 规范化参数)。"""
    try:
        blob = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        blob = repr(args)
    return f"{tool_name}|{blob}"


def _compactor_for(key: str):
    """工具自己声明的结果摘要函数（`ToolDefinition.result_compactor`）。

    有它就用它 —— 作者比框架更知道这份结果里什么可以丢。这是本模块允许的
    **唯一**一种有损转述：它是工具契约的一部分，不是框架自作主张。
    """
    tool, _, _ = key.partition("|")
    if not tool:
        return None
    try:
        from core.tool_registry import get_tool
        td = get_tool(tool)
        return getattr(td, "result_compactor", None) if td else None
    except Exception:
        return None


def _render_tombstone(key: str, size: int, turn: int, *, replayable: bool,
                      ok: bool = True, logged: bool = False) -> str:
    """墓碑正文。**按能不能安全重调分岔** —— 这是正确性，不是文案偏好。

    "用同样参数再调一次就能取回"对纯读工具是真话；对 `run_bash` / `save_artifact`
    这类有副作用的工具是**危险的错误建议** —— 照做会真的再执行一次。判据取工具
    自己的 `ToolDefinition.replayable_read` 声明（与 summarizer / tool_call_cache
    同一个真相源，不另写名单）。

    `logged` = 原文在本 run 的 tool_results.jsonl 里 —— 墓碑必须如实说"存在哪、
    怎么拿"，别在原文真的还在时说得像丢了（也别反过来）。
    """
    tool, _, raw = key.partition("|")
    kept = ("原文**完整保存在本 run 的 tool_results.jsonl 里没有丢**"
            if logged else "原文**完整保存在磁盘上没有丢**")
    head = (f"{TOMBSTONE_PREFIX}\n"
            f"这一条是 {tool}({raw[:200]}) 在 turn {turn} 的结果，原文 {size:,} 字符，"
            f"已从上下文移出以腾出空间。{kept}。\n")
    if not ok:
        # 失败结果的墓碑**不能**说"重新取回不算重复"：那句话对成功副本是真的
        # （恢复路径 lookup 会放行且不计数），对失败是假的 —— 失败没有可复用的
        # 副本，重调必然重新执行、必然重新计数，多来几次就熔断。
        # 骗它去撞墙，比不说话糟得多。
        return head + ("⚠️ 这是一条**失败**的结果。重新调用会重新执行，"
                       "大概率再次失败，**而且会被计入重复**（够多次就停机）。"
                       "先换地址、换做法，或者说清楚你要的东西不存在。")
    if replayable:
        return head + ("需要它就用同样的参数再调用一次 —— 会原样完整取回，"
                       "**重新取回不算重复调用，不会被判为循环**。")
    return head + ("⚠️ 这个工具**有副作用，不要为了看结果而重跑它**。"
                   "如果后面的判断依赖这条结果，把你还记得的结论先写进白板/产物；"
                   "确实必须重新拿到原文时，说明你需要它，别自己重放。")


class ContextView:
    """一个 run 的工作集。挂在 state.hook_state 上，随 run 存活。

    只管工具结果。章程（system prompt）、台账（白板 / 义务）、近期窗由既有的
    hook 每轮注入 —— 那两段本来就是渲染语义，不需要在这里重做一遍。
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        #: key -> tool_call_id，当前持有完整副本的那一条
        self.live: dict[str, str] = {}
        #: tool_call_id -> {key, size, turn, evicted}
        self.entries: dict[str, dict] = {}
        self._log_path: Path | None = None
        #: run log 里已有原文的 key 集合；惰性建（adopt 首用），note_result 增量维护。
        self._logged_key_cache: set[str] | None = None

    # ── 磁盘：全保真 run log ────────────────────────────────────────────

    def _log(self) -> Path | None:
        if self._log_path is not None:
            return self._log_path
        # ⚠️ 用 State 真有的属性。第一版写的是 `run_dir` / `base_dir` —— 两个
        # 都不存在于 State 上，于是 run log **一次都没落过盘**，而单元测试全绿：
        # 测试里的替身恰好有这两个属性，把被测实现整个遮住了。
        base = getattr(self.state, "root", None)
        if base is None:
            tp = getattr(self.state, "transcript_path", None)
            base = Path(tp).parent if tp else None
        if base is None:
            return None
        try:
            p = Path(base) / "tool_results.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            self._log_path = p
            return p
        except OSError:
            return None

    def _append_log(self, *, key: str, tool_call_id: str, turn: int, content: str) -> bool:
        """原文落盘。**永不编辑**，只 append —— 它是取回的唯一权威。

        返回是否真的写进去了：这个布尔值就是该条目的**驱逐资格**（见
        `enforce_budget`）—— 落了盘的驱逐是无损的，没落盘的才谈得上"真丢"。
        """
        p = self._log()
        if p is None:
            return False
        try:
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"key": key, "tool_call_id": tool_call_id,
                     "turn": turn, "content": content},
                    ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    def _logged_keys(self) -> set[str]:
        """run log 里已有原文的 key 集合（惰性建一次，之后由 note_result 增量维护）。

        为的是 `adopt`：续跑/respawn 恢复的历史消息不是本进程写的，但同一个
        run 目录的 log 里往往**躺着它们的原文**（session 级 run 目录活过 worker
        重启）。不查一下就当"没备份"，等于把可无损驱逐的东西错判成不可驱逐 ——
        issue #710 的豁免扩大化正是这么来的。
        """
        if getattr(self, "_logged_key_cache", None) is not None:
            return self._logged_key_cache
        keys: set[str] = set()
        p = self._log()
        if p is not None and p.exists():
            try:
                with p.open(encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        k = rec.get("key")
                        if k:
                            keys.add(k)
            except OSError:
                pass
        self._logged_key_cache = keys
        return keys

    def recover(self, tool_name: str, args: dict | None) -> str | None:
        """从 run log 取回某个 key 的最新原文。取不到返回 None。"""
        p = self._log()
        if p is None or not p.exists():
            return None
        key = canonical_key(tool_name, args)
        found = None
        try:
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("key") == key:
                        found = rec.get("content")
        except OSError:
            return None
        return found

    # ── 不变量：至多一份活副本 ──────────────────────────────────────────

    def is_live(self, tool_name: str, args: dict | None) -> bool:
        """这个 key 的**完整**结果是不是正摆在 context 里。

        这是新的循环判据：答案就在眼前还在问 = 真卡住。被驱逐之后再问不算。
        """
        return canonical_key(tool_name, args) in self.live

    def note_result(self, *, tool_name: str, args: dict | None, tool_call_id: str,
                    content: str, turn: int, replayable: bool, ok: bool = True) -> None:
        """登记一份新的完整副本：落盘 + 让同 key 的旧副本立刻变墓碑。

        「立刻」很重要：不是等预算不够才收，而是新副本一进来旧的就走。
        这样"读一百次"和"读一次"占用的 context 一样多 —— 控体积不再需要
        任何"惩罚重复"的机制。
        """
        key = canonical_key(tool_name, args)
        logged = self._append_log(key=key, tool_call_id=tool_call_id,
                                  turn=turn, content=content)
        if logged and getattr(self, "_logged_key_cache", None) is not None:
            self._logged_key_cache.add(key)
        prior = self.live.get(key)
        if prior is not None and prior != tool_call_id:
            self._evict(prior)
        self.live[key] = tool_call_id
        self.entries[tool_call_id] = {
            "key": key, "size": len(content), "turn": turn, "evicted": False,
            "replayable": bool(replayable), "ok": bool(ok), "logged": logged,
            # 只在工具声明了 compactor 时才留原文在内存里 —— 否则墓碑够用，
            # 没必要把已经落盘的东西再拷一份。
            "original": content if _compactor_for(key) is not None else None,
        }

    def _evict(self, tool_call_id: str) -> None:
        e = self.entries.get(tool_call_id)
        if not e or e.get("evicted"):
            return
        e["evicted"] = True
        if self.live.get(e["key"]) == tool_call_id:
            self.live.pop(e["key"], None)

    # ── 渲染：把驱逐过的那些换成墓碑 ────────────────────────────────────

    def apply(self, messages: list) -> list:
        """**渲染**：返回一个新 list，已驱逐的条目换成墓碑。**不改原对象。**

        原地改 `m.content` 看着省事，但它把"渲染视图"退化成"编辑历史"，
        代价有两处，都不报错：

          - 轮事务回滚靠把 messages 截回本轮开始的长度，被原地改过的内容截不
            回去 —— 回滚后拿到的不是"相同的一次尝试"。
          - 调用方拿到的 `out is messages`，任何"改了哪些"的比较恒为空。
            实测把一整片 clear 层的测试骗绿过。

        只动 content，不动条数、不动 tool_call_id —— assistant.tool_calls 与
        tool 消息的配对是 API 硬约束，渲染不许碰它。（这也是为什么驱逐用墓碑
        而不是删消息：删一条就得改上一条 assistant，那才是真的在编辑历史。）
        """
        from core.llm import LLMMessage

        out: list = []
        for m in messages:
            if getattr(m, "role", None) != "tool":
                out.append(m); continue
            tcid = getattr(m, "tool_call_id", None)
            e = self.entries.get(tcid or "")
            if not e or not e.get("evicted"):
                out.append(m); continue
            if (m.content or "").startswith(TOMBSTONE_PREFIX):
                out.append(m); continue
            # 工具自己声明了 `result_compactor` 就用它：**作者比框架更知道
            # 这份结果里什么可以丢**。这是唯一被授权的有损转述形式，且它是
            # 工具契约的一部分，不是框架自作主张。
            rendered = None
            compactor = _compactor_for(e["key"])
            if compactor is not None and e.get("original") is not None:
                try:
                    rendered = compactor(e["original"])
                except Exception:
                    rendered = None
            out.append(LLMMessage(
                role="tool", tool_call_id=m.tool_call_id, name=m.name,
                content=rendered or _render_tombstone(
                    e["key"], e["size"], e["turn"],
                    replayable=e.get("replayable", False), ok=e.get("ok", True),
                    logged=e.get("logged", False)),
            ))
        return out

    def adopt(self, messages: list, *, replayable_of=None) -> int:
        """认领 messages 里在场、但账上没有的 tool 消息。返回认领条数。

        ## 为什么必须有这一步

        工作集只登记**本 run 经 agent_loop 产生**的结果。而 messages 里还可能有
        别的来源：会话续跑恢复的历史、pause/resume 重建的序列、上层直接构造的
        起始消息。它们同样占着 context，同样该受预算约束 —— 不认领的后果是
        续跑一条历史很长的会话时，压缩层**完全失去清除能力**（账上没有条目，
        `enforce_budget` 一条也驱逐不了），而没有任何东西会报错。

        `(tool, args)` 从上一条 assistant 的 `tool_calls` 里配对取得；配不上的
        （历史残缺）用 tool_call_id 兜底成一个自有 key，仍可驱逐。

        认领的条目**先查 run log 再定资格**：session 级 run 目录活过 worker
        重启，恢复的历史消息的原文常常就躺在同一份 log 里 —— 查到了就是可
        无损驱逐的（issue #710）。查不到的按无备份处理，墓碑文案本来就按
        `replayable_read` 分岔：可重调的说"重调取回"，不可重调的说"别重放"。
        """
        pending: dict[str, tuple[str, str]] = {}
        for m in messages:
            for tc in (getattr(m, "tool_calls", None) or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                tcid = tc.get("id")
                if tcid:
                    pending[tcid] = (fn.get("name") or "", fn.get("arguments") or "")
        n = 0
        for m in messages:
            if getattr(m, "role", None) != "tool":
                continue
            tcid = getattr(m, "tool_call_id", None)
            if not tcid or tcid in self.entries:
                continue
            content = getattr(m, "content", "") or ""
            if content.startswith(TOMBSTONE_PREFIX):
                continue
            name, raw_args = pending.get(tcid, (getattr(m, "name", "") or "", ""))
            head = content[:200]
            ok = not ('"status": "error"' in head or "'status': 'error'" in head)
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            key = canonical_key(name, args) if name else f"?|{tcid}"
            replayable = bool(replayable_of(name)) if (replayable_of and name) else False
            self.entries[tcid] = {
                "key": key, "size": len(content), "turn": 0, "evicted": False,
                "replayable": replayable, "ok": ok, "adopted": True,
                # 续跑恢复的历史不是本进程写的，但 session 级 run 目录活过
                # worker 重启 —— log 里常躺着它的原文。查一下再定资格，
                # 别把可无损驱逐的错判成不可驱逐（issue #710）。
                "logged": key in self._logged_keys(),
                "original": content if _compactor_for(key) is not None else None,
            }
            # 认领的不进 `live`：它没有 run log 备份，"答案就在眼前"这个判断
            # 应当只由本 run 亲手登记过的副本来支撑。
            n += 1
        return n

    def sync(self, messages: list) -> int:
        """按**当前 messages 现算**哪些副本还在场。返回新判定为不在场的条数。

        任何人动过 messages 之后都该调一次 —— summarizer 整段替换、轮事务回滚、
        pause/resume 重建，都会让某条 tool 消息消失或变成别的东西。

        ## 为什么是"现算"而不是"通知我"

        工作集的 `live` 一旦与真实 messages 脱节，后果不是少省点空间，是**死循环**：
        `is_live` 说"完整答案正摆在眼前"，于是 lookup 回一个指针让模型"往上翻"，
        而那条消息其实已经被摘要吃掉了 —— 模型翻不到，只能再问，再拿到同一个
        指针。框架把它锁死在一个它无法满足的指令上。

        指望每个改动 messages 的地方都记得通知工作集，就是在给这类脱节留门
        （少通知一处 = 一个死循环，且不报错）。判据从现场算，不可能漏。
        """
        present = {
            getattr(m, "tool_call_id", None)
            for m in messages
            if getattr(m, "role", None) == "tool"
            and not (getattr(m, "content", "") or "").startswith(TOMBSTONE_PREFIX)
        }
        n = 0
        for tcid in list(self.entries):
            e = self.entries[tcid]
            if not e.get("evicted") and tcid not in present:
                self._evict(tcid)
                n += 1
        return n

    def enforce_budget(self, messages: list, *, max_bytes: int,
                       protect_turn_ge: int | None = None) -> int:
        """**只做驱逐决策**，不渲染 —— 渲染交给 `apply()`（决策与渲染分离）。

        活副本总量超预算 → 按 LRU（最久没用的先走）驱逐，直到回到预算内。

        这是**机械**的：不问内容重要不重要，只问谁最久没被用到。判断哪份材料
        还需要，是模型的事（它再调一次就回来了，完整且免费）。

        `protect_turn_ge`：turn ≥ 此值的条目不驱逐。预算改成"窗口减其余一切
        现算"（issue #710）之后预算可以变得很紧，紧到轮到**上一轮刚返回、
        模型还没读过**的结果 —— 驱逐它是活锁（模型看不到任何结果，只能重调，
        重调的又被驱逐）。最近一轮的结果是本轮的输入，不是可回收的历史。
        """
        # 先与现场对齐：认领账上没有的，销掉已不在场的。别拿一份过期的账算预算。
        from core.tool_call_cache import is_cacheable
        self.adopt(messages, replayable_of=is_cacheable)
        self.sync(messages)
        if max_bytes <= 0:
            # 配置写错时的保护，**不是**"全部驱逐"的开关：预算算成 0 或负数
            # 说明上游算错了，此时什么都不驱逐比清空工作集安全得多。
            return 0
        # ── 驱逐资格 = 可取回性（issue #710 重推）────────────────────────────
        #
        # 旧的两条豁免（失败结果不驱逐；非 replayable 无 compactor 不驱逐）是
        # 从 run log 存在**之前**的清除层原样继承的 —— 那个世界里驱逐即真丢，
        # fail-closed 是对的。步 0 落盘之后，判据换了根：**原文在 run log 里的，
        # 驱逐是无损的**（墓碑指针指向原文，宪法条款满足），与它可不可重放、
        # 成没成功无关 —— "不可重放"约束的是能不能建议模型重跑，不是框架能不能
        # 取回。把两者混为一谈，就是 qinp 会话里 13 份 25k 字符的 blocked 报告
        # 无限累积、直到网关 400 杀 run 的那条路。
        #
        # 仍然保护的只剩一类：**哪儿都取不回**的结果 —— 那才是真丢，
        # fail-closed 原样成立。注意失败结果的"可取回"只能来自 run log：
        # 重放会**重新执行**（对失败既危险又不是取回），compactor 摘要也
        # 不该替失败正文说话 —— 所以失败结果的资格只看 `logged`。
        #
        # 驱逐顺序（越靠前越先走）：可重放的 > 落盘的成功结果 > 落盘的失败结果
        # （失败正文是自纠依据，LRU 天然保住最近一条；老失败在 log 里没有丢），
        # 同类内按 turn 旧的先走。
        def _eligible(e: dict) -> bool:
            if protect_turn_ge is not None and e.get("turn", 0) >= protect_turn_ge:
                return False
            if not e.get("ok", True):
                return bool(e.get("logged"))
            return bool(e.get("replayable") or e.get("logged")
                        or _compactor_for(e["key"]) is not None)

        alive = [(0 if e.get("replayable") else 1,
                  0 if e.get("ok", True) else 1,
                  e["turn"], tcid, e)
                 for tcid, e in self.entries.items()
                 if not e.get("evicted") and _eligible(e)]
        # 预算的语义是"**全部**工具活副本占多少"（推导预算按此扣窗口），
        # 不可驱逐/受保护的那部分同样占着窗口 —— 从预算里扣掉，别让它们
        # 在账外白坐（账外坐着 = 预算全绿、窗口照样爆，issue #710 同型）。
        shielded = sum(e["size"] for tcid, e in self.entries.items()
                       if not e.get("evicted") and not _eligible(e))
        effective_budget = max(0, max_bytes - shielded)
        total = sum(e["size"] for *_, e in alive)
        if total <= effective_budget:
            return 0
        alive.sort()                       # 可重调的 > 落盘成功 > 落盘失败；旧的先走
        n = 0
        for _, _, _, tcid, e in alive:
            if total <= effective_budget:
                break
            total -= e["size"]
            self._evict(tcid)
            n += 1
        return n

    def stats(self) -> dict:
        alive = [e for e in self.entries.values() if not e.get("evicted")]
        return {
            "live_entries": len(alive),
            "live_bytes": sum(e["size"] for e in alive),
            "evicted": sum(1 for e in self.entries.values() if e.get("evicted")),
        }


_VIEW_KEY = "_context_view"


def get(state: Any) -> ContextView | None:
    """取本 run 的工作集（没有 hook_state 的裸 state 返回 None）。"""
    hs = getattr(state, "hook_state", None)
    if hs is None:
        return None
    view = hs.get(_VIEW_KEY)
    if view is None:
        view = ContextView(state)
        hs[_VIEW_KEY] = view
    return view
