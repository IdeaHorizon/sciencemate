"""同 run 内重复只读工具调用的记忆层（v3.3）。

## 为什么需要

E2E 实测事故（2026-07-26）：一个 curator run 发了 51,937 次 `search_kb`，其中
**只有 78 个不同 query**，`ATLAS` 一个词就查了 7,409 次，且**每次都成功返回**
（空结果 0 次）。该 run 打了 14,844 次 LLM 调用、烧 22.7 亿 token，而
`write_scratchpad / save_artifact / create_claim` 全是 **0** —— 读了五万次，
一个字没写下来。

机理（transcript 实证）：agent 手里有个 ~7 项的 checklist，循环 "查 A→查 B→…→
回到 A"，相邻两次查同一项的间隔稳定在 7 个工具调用。中途发生了 149 次上下文压缩；
压缩按 token 数裁剪，把工具结果裁掉了 —— agent 于是"忘了"自己查过，从头再查一轮。

**根因是"任务进度只存在于易失的 conversation context 里"**，本模块只治其中一段：
让重复的只读调用变便宜、可观测、并在病态重复时明确喊停。真正的治本是让压缩保住
任务结论（见 summarizer 的 task-state 保全）。

## 语义

- 仅对**纯读**工具生效。判据是工具自己的 `ToolDefinition.replayable_read` 声明，
  不是这里的一份名单 —— 名单会漏（见下）。
- 任何**不在白名单**的工具执行后 `generation += 1`，使全部缓存失效 —— 这样
  "先 search 拿到空 → create_claim 写入 → 再 search" 不会读到过期结果。
- 第 `SOFT` 次起返回**紧凑**结果（保留标量/计数等"答案"，截断大列表长文本）+
  提示已重复；紧凑化本身减缓 context 膨胀 → 压缩更少 → 重问更少。
- 第 `HARD` 次起返回 `status="error"` 的显式循环告警，要求换策略；同参数同代次
  重复这么多次在任何情况下都是 bug，不是正常轮询（真轮询会有写操作 bump 代次）。
- 第 `BREAK` 次起**停机**：登记 blocker + 请求 loop 终止。见下。

## 为什么 HARD 之后还要有 BREAK（2026-08-21 实测代价）

旧参数是 `SOFT=3 / HARD=30`，且 HARD 只换了一段文案 —— **控制流一点没变**。
实测：第 30 次报了 error 之后，模型又发了 34 次同样的调用，一共 64 次。

    一个可以被无限忽略的熔断器不是熔断器，是一条日志。

两处都改：

  1. **HARD 30 → 8。** 真成本不是工具结果的大小，是**每一次重复都要把整个
     上下文重发一遍**。curator / writing 这类 run 的上下文动辄十万 token，
     30 次 = 30 个满上下文的模型轮次白烧。而在第 8 次时，模型已经收到
     5 次（第 3~7 次）明写"你重复了 N 次、结果不会变"的紧凑结果 —— 五次提示
     改不了的行为，第六次也改不了。8 是"给足自纠机会"与"别烧满三十轮"的下沿。
  2. **加 BREAK：连续 4 次无视 error 之后停机。** 做的是结构性的事，不是又
     换一段话：登记一条真 blocker（与 `report_blocker` 同一份数据形状，
     orchestrator 照常收到）＋ 通过既有的 `_loop_terminal` 通道请求 loop 在
     本 turn 收口。模型没有下一轮可以用来无视它。

**"连续"是由构造保证的，不需要另立计数器**：任何非纯读工具执行后
`generation += 1`，整份缓存失效；于是一条 entry 的 `hits` 还能涨到 8、12，
恰恰说明这期间**一次写操作都没有**。中途真做了事，计数自己就归零了。

## v3.4：失败不是结论（同一条 run 的另外两处取证）

`fed96b41` 把"熔断器能不能停机"修好了。同一条 run（2026-08-21，
`run_c5359f57…::_orchestrator->_curator@d1`）还暴露了两件**性质不同**的事，
它们与阈值无关，调多低都还在：

  1. **平台对模型撒谎。** 第 SOFT～HARD 次之间，本层把 `{"status": "error"}`
     的失败结果塞进自制的 `{"status": "success"}` 信封返回 —— 那条 run 里
     105 次。模型问"成了吗"，平台答"成了"。
  2. **失败冒充结论。** 失败结果和成功结果共用一个格子，于是
     `findings_digest` 把 `找不到文件：…` 以"本 run 已确立的结论"的名义注回
     压缩后的上下文 —— 更要命的是 `_break()` 会把这份 digest 写进 **blocker
     正文**交给 orchestrator，让"读不到"变成派发决策的"事实"。

根因是一个格子装了两样东西。`replayable_read` 的语义是"同参数重调能取回同样
结果"，一句 `找不到文件` 字面满足它，但它不是**结论**，是**这条路走不通**：
结论可以复用、可以进台账、可以进 blocker；"走不通"只能用来劝阻。

所以 v3.4 把**记账**与**复用**拆成两份状态：

    调用台账 ledger  —— 所有调用都记（**含失败**），(工具, 参数, 代次) → 次数
                        HARD / BREAK 的计数从此走它，**失败循环照样能熔断**
    结果缓存 cache   —— **只存成功**。复用时 status 一律继承产生方盖的章，
                        本层只砍体积，不改判

拆开之后失败重复走的是"真跑一次工具"（代价一次 stat），并在结果上**附加**
一条重复提示 —— 不动 status、不动 error 正文。产生失败那一层盖的章，本层无权改。
"""
from __future__ import annotations

import json
from typing import Any

_CACHE_KEY = "_tool_call_cache"          # hook_state: key -> entry（**只存成功结果**）
_LEDGER_KEY = "_tool_call_ledger"        # hook_state: key -> {count, gen, turn, status, brief}
_GEN_KEY = "_tool_call_cache_generation"  # hook_state: int

# 阈值（可 env 覆盖交给调用方；这里给保守默认）
SOFT_REPEAT = 3     # 第 3 次相同调用起返回紧凑结果
HARD_REPEAT = 8     # 第 8 次起判定病态循环，返回 error directive
BREAK_REPEAT = 12   # 第 12 次起（= 无视 error 4 次）停机：登记 blocker + 终止本 run

#: （v3.5 起 lookup 不再返回副本，本常量只留给 findings_digest / _break 的预览用。）
#: 小于这个字符数的结果原样保留 —— 紧凑化的本意是**省 context**，不是惩罚重复。
#:
#: summarizer 清除旧 tool result 时明写"参数见上一条 assistant 消息的 tool_calls，
#: **用同样参数重调即可取回**"（core/summarizer.py）。若本层把"重调"一律降级成
#: 400 字截断版，模型完全照框架说的做，却拿回一份残缺品 —— 两条机制正面矛盾，
#: 而它们本是为同一场事故（51,937 次 search_kb）建的。体积门槛让小结果的承诺
#: 真正兑现；真正撑爆 context 的大结果本来也放不下第二份，紧凑仍然合理。
#:
#: ⚠️ 这只兑现了一半：重调**仍然计入重复**，连续重调够多次照样熔断。彻底的解法
#: 是让 context 从"被编辑的对话日志"变成"每轮渲染的有界视图"，重调只是重新渲染
#: 同一条工作集条目 —— 见 docs/RFC_CONTEXT_AS_A_RENDERED_VIEW.md。
COMPACT_MIN_CHARS = 2000

#: `hook_state` 里的停机标记 —— 幂等用，避免同一条 entry 反复登记 blocker。
_BROKEN_KEY = "_tool_call_cache_broken"


def cacheable_tools() -> frozenset[str]:
    """哪些工具的重复调用可以紧凑化 —— **扫声明，不写名单**。

    判据是工具自己的 `ToolDefinition.replayable_read`：「结果可以用同样参数重调
    取回」。这正是紧凑化成立的前提 —— 把旧结果换成占位符不丢信息，因为参数还在
    上一条 assistant 消息里。压缩器一直用的就是这个声明。

    ## 为什么不再写名单（2026-08-13 实测代价）

    这里原本是一份硬编码的 17 项白名单，和 `replayable_read` 是**同一个问题的
    第二份答案**。两份已经双向分叉，而且两边都不报错：

        名单有、声明没有 : 12 个
        声明有、名单漏掉 :  4 个  ← read_file / list_files / search_files /
                                    read_own_prior_attempt

    名单是 KB / artifact 时代写的；workspace-first 之后 `read_file` 成了主力读
    工具，而它**早就在自己的 ToolDefinition 上声明了 replayable_read=True**，
    只是这份名单没去看。代价：v26 的 hypothesis 连读同一个文件 815 次，本该在
    第 30 次触发的病态循环告警一次都没响，5 小时 40 分 / 117M tokens。

    新东西默认被覆盖：谁声明自己是纯读的，谁就自动进来。

    ## 排除项也归声明管（2026-08-21 订正注释）

    这里曾另有一段注释写着「刻意排除 …、read_file（外部进程可改文件）、…」。
    那段话是名单时代的产物，删名单时忘了删它，于是**注释与机制说了相反的
    话**：read_file 自己声明了 `replayable_read=True`，一直是被缓存的。照那段
    注释理解会得出完全相反的结论（"read_file 不受熔断保护"）。

    现在没有排除名单：`query_budget`（计数器每次都变）、`validate_*`（可能落
    validation artifact）、`search_papers`（外部服务、可能顺带 ingest）之所以
    不被缓存，是因为**它们自己没声明 replayable_read**，不是因为这里列了它们。
    要改某个工具的缓存性，去改那个工具的 `ToolDefinition`，不要回来加名单。
    """
    from core.tool_registry import all_tool_names

    return frozenset(name for name in all_tool_names() if is_cacheable(name))


def is_cacheable(tool_name: str) -> bool:
    """单个工具能不能缓存 —— 直接问注册表，O(1)。

    热路径每次工具调用都要问一次，别在这里重扫全表。`cacheable_tools()` 只是
    给测试和自省用的便利视图。
    """
    from core.tool_registry import get_tool

    definition = get_tool(tool_name)
    return bool(definition is not None and getattr(definition, "replayable_read", False))



def _canonical_args(args: dict | None) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(args)


def cache_key(tool_name: str, args: dict | None) -> str:
    return f"{tool_name}|{_canonical_args(args)}"


def _compact(value: Any, *, str_cap: int = 400, list_cap: int = 3, depth: int = 0) -> Any:
    """把结果压成"保住答案、砍掉体积"的紧凑形态。

    标量原样保留（`total_matched: 0`、`status` 这类**就是答案本身**）；
    长字符串截断；长列表只留头几项 + 省略计数。
    """
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: _compact(v, str_cap=str_cap, list_cap=list_cap, depth=depth + 1)
                for k, v in list(value.items())[:20]}
    if isinstance(value, list | tuple):
        head = [_compact(v, str_cap=str_cap, list_cap=list_cap, depth=depth + 1)
                for v in list(value)[:list_cap]]
        if len(value) > list_cap:
            head.append(f"…（另有 {len(value) - list_cap} 项，已省略）")
        return head
    if isinstance(value, str) and len(value) > str_cap:
        return value[:str_cap] + f"…（截断，原长 {len(value)}）"
    return value


def generation(state) -> int:
    return int(getattr(state, "hook_state", {}).get(_GEN_KEY, 0) or 0)


def note_mutation(state) -> None:
    """非白名单（可能有副作用）工具执行后调用：使既有缓存全部失效。

    台账随代次一起作废 —— 写操作之后重新查询是**合法的**，不该继续算它重复。
    """
    hs = state.hook_state
    hs[_GEN_KEY] = generation(state) + 1


def _is_success(result: Any) -> bool:
    """这个结果算不算"成功" —— 判据是**产生方盖的章**，本层只读不写。

    没有 status 字段的 dict 按成功处理（历史上有工具不盖章）；显式 error /
    pause 以及任何非 dict 都不进结果缓存。
    """
    if not isinstance(result, dict):
        return False
    status = result.get("status")
    return status is None or status == "success"


def _ledger_entry(state, key: str) -> dict | None:
    """当前代次下这个 key 的台账；跨代次的旧账当不存在。"""
    hs = getattr(state, "hook_state", None)
    if hs is None:
        return None
    entry = (hs.get(_LEDGER_KEY) or {}).get(key)
    if not entry or entry.get("gen") != generation(state):
        return None
    return entry


def _bump_ledger(state, key: str, *, turn: Any, status: str, brief: Any = None,
                 reset: bool = False, count_delta: int = 1) -> dict:
    """记一次调用；返回记完之后的条目（`count` 即"这是第几次"）。

    **计数走台账、不走结果缓存**，正是为了让失败循环也熔断得了：失败结果不进
    结果缓存（它不是结论），若沿用旧的 `entry["hits"]` 计数，2026-08-21 那种
    连撞 64 次"找不到文件"的循环就一次都数不到。
    """
    hs = state.hook_state
    ledger = hs.setdefault(_LEDGER_KEY, {})
    gen = generation(state)
    entry = ledger.get(key)
    if not entry or entry.get("gen") != gen or reset:
        # 新建（首次登记，或恢复后重置）：**这一次调用本身就是第 1 次**。
        # 曾经初始化成 0 再加 count_delta，而 store 传的是 0 —— 于是首次调用
        # 没被数进去，整条计数一路少 1，BREAK 永远差一次才触发。
        ledger[key] = entry = {
            "count": 1, "gen": gen, "turn": turn, "status": status, "brief": brief,
        }
        return entry
    entry["count"] = int(entry.get("count", 0)) + count_delta
    entry["status"] = status
    if brief is not None:
        entry["brief"] = brief
    return entry


def _hard_directive(tool_name: str, n: int, led: dict) -> dict:
    """病态重复的 error directive —— **文案按上一次的结果性质分岔**。

    v3.3 只有一句话："把已确认的结论写进产物（write_scratchpad / save_artifact
    / create_claim），再从未完成的下一步继续"。2026-08-21 那条 run 里这句话是
    **错的建议**：那些重复全部是 `找不到文件`，模型手里根本没有"已确认的结论"
    可落。它不照做不是不听话，是它照做不了 —— **给不出可执行出口的护栏等于
    没有护栏**，而模型会把整段话连同后面那句"否则停机"一起当噪声。
    """
    first_turn = led.get("turn")
    tail = (f"\n⚠️ 再重复 {BREAK_REPEAT - n} 次，框架会登记 blocker 并"
            f"**停掉本 run** —— 这不是又一句提醒，是控制流。")
    common = (f"检测到病态重复：本 run 已用完全相同的参数调用 {tool_name} "
              f"{n} 次（首次在 turn {first_turn}），期间没有任何写操作改变状态。")
    if led.get("status") == "error":
        return {
            "status": "error",
            "error": (
                f"{common}\n"
                f"**这 {n} 次全部失败，报错一字不差**：{led.get('brief') or ''}\n"
                f"重试不会让它变成功。这条路走不通，只有三个合法出口：\n"
                f"  1. 换地址 —— 先用 list_files 看清目录里到底有什么，别再猜名字；\n"
                f"  2. 换一件事 —— 这个目标不是必需的就跳过，继续未完成的下一步；\n"
                f"  3. 承认做不到 —— 直接说清楚你要的东西不存在，然后停下。"
                + tail
            ),
            "repeat_count": n,
            "first_called_at_turn": first_turn,
            "escalates_at_repeat": BREAK_REPEAT,
            "repeat_kind": "failing",
        }
    return {
        "status": "error",
        "error": (
            f"{common}结果不可能变化。"
            f"这是循环，不是进展。请立刻改变策略：把已确认的结论写进产物"
            f"（write_scratchpad / save_artifact / create_claim），"
            f"再从未完成的下一步继续；不要再用相同参数重复查询。" + tail
        ),
        "repeat_count": n,
        "first_called_at_turn": first_turn,
        "escalates_at_repeat": BREAK_REPEAT,
        "repeat_kind": "succeeding",
        "cached_result_preview": led.get("brief"),
    }


def lookup(state, tool_name: str, args: dict | None,
           *, is_live: bool = True) -> dict | None:
    """要不要替这次调用作答。返回 None = 照常执行真实工具。

    四条出口：

      - 到 BREAK → 停机（登记 blocker + 请求 loop 收口）
      - 到 HARD  → error directive（文案按成功/失败分岔）
      - 有可复用的**成功**结果 → 返回它（到 SOFT 后紧凑；status 一律继承）
      - 其余（本代次首次 / 上一次是失败）→ None，让工具真跑

    ⚠️ 计数走**台账**不走结果缓存：失败不进结果缓存，若还按 `entry["hits"]`
    计数，失败循环就一次也数不到 —— 而 2026-08-21 那条 64 次的循环恰恰全是失败。
    """
    if not is_cacheable(tool_name):
        return None
    hs = getattr(state, "hook_state", None)
    if hs is None:
        return None
    key = cache_key(tool_name, args)
    led = _ledger_entry(state, key)
    if not led:
        return None                        # 本代次第一次：真跑
    if not is_live:
        # 恢复路径：完整副本已被驱逐，模型是照墓碑上写的重新取回。
        # **不计数、不拦截** —— 框架让它这么做，就不能为此惩罚它。
        #
        # `is_live` 由工作集给出，它把"重复"和"恢复"分开，是整个 v3.5 的枢纽：
        #   is_live=True  答案就在眼前它还在问 → 计数，够多次报错、再多就停机
        #   is_live=False 副本已被驱逐，照墓碑重新取回 → 放行，计数归零
        return None
    n = int(led.get("count", 0)) + 1        # 这次是第 n 次
    first_turn = led.get("turn")
    status = led.get("status", "success")

    if n >= BREAK_REPEAT:
        _bump_ledger(state, key, turn=first_turn, status=status)
        return _break(state, tool_name, args, led, count=n, first_turn=first_turn)
    if n >= HARD_REPEAT:
        _bump_ledger(state, key, turn=first_turn, status=status)
        return _hard_directive(tool_name, n, led)

    entry = (hs.get(_CACHE_KEY) or {}).get(key)
    if not entry or entry.get("gen") != generation(state):
        # 上一次是失败（失败不进结果缓存）→ 别替它作答，让工具真跑：本层无权
        # 替一个失败的调用作答，世界也可能真的变了。
        #
        # ⚠️ **但必须先记账。** 这仍然是"对着同一个参数再问一次"，而这里正是
        # 失败循环唯一能被数到的地方 —— 计数若挂在 store 上，失败会因为在这里
        # 提前 return 而一次都数不到，2026-08-21 那种连撞 64 次的现场就永远不
        # 会熔断。
        _bump_ledger(state, key, turn=first_turn, status=status)
        return None

    _bump_ledger(state, key, turn=first_turn, status="success")

    # v3.5：**给指针，不给副本。**
    #
    # 调用方只在工作集判定"完整答案正摆在 context 里"时才问到这里（见
    # agent_loop 的 `_view.is_live`）。既然原文就在上文逐字摆着，再回一份
    # 紧凑版是纯粹的浪费 —— 而且那份紧凑版正是 v3.3 一切麻烦的来源：它是框架
    # 执笔的有损转述，占着 tool result 的位、盖着自己发明的 status 章。
    #
    # 现在体积由工作集的「至多一份活副本」机械保证（新副本进来旧的立刻变墓碑），
    # 所以不需要靠截断来控体积，也不需要靠惩罚重复来控体积。
    out: dict = {
        "already_in_context": True,
        "note": (
            f"这次调用没有执行 —— **它的完整结果已经在上文里逐字摆着**"
            f"（{tool_name}，首次在 turn {first_turn}，本 run 第 {n} 次请求它）。"
            f"期间没有任何写操作，结果不可能变化。往上翻，别再调一次。\n"
            f"如果你翻不到它，说明它已被移出上下文并留了墓碑 —— 那种情况下"
            f"重新调用会完整取回，也不会被算作重复。"
        ),
        "repeat_count": n,
        "first_called_at_turn": first_turn,
    }
    # status 原样继承产生方盖的章，没有就不凭空盖（v3.3 在这里硬写 "success"，
    # 把失败结果说成了成功 —— 那条 run 里 105 次）。
    src = entry["result"]
    if isinstance(src, dict) and src.get("status") is not None:
        out["status"] = src["status"]
    return out


def _break(state, tool_name: str, args: dict | None, entry: dict, *,
           count: int, first_turn: Any) -> dict:
    """无视 error 继续重复 → 做结构性的事，而不是再换一段文案。

    三件事，缺一不可：

      1. **把本 run 已确立的只读结论落进 blocker 正文**。这些结论只活在
         `hook_state` 的缓存里，run 一结束就没了 —— 停机前不带走，等于这一趟
         读的东西全白读。
      2. **登记一条真 blocker**（与 `report_blocker` 同一份形状）。executor 见
         `hook_state["blockers"]` 非空即把终态判成 `blocked`，orchestrator 的
         派发闸照常收到结构化原因，不需要从散文里再猜一遍。
      3. **请求 loop 收口**（`_loop_terminal`，owner hook 用的同一条通道）。
         agent_loop 会在本 turn 的 tool 消息全部落盘后终止 —— 协议合法、
         on_end / QC 照常跑，只是模型没有下一轮可以用来无视这条熔断。

    幂等：一条 run 只登记一次（第一条撞线的 key 说了算），之后的重复调用继续
    收到同一个 envelope，但不再重复记账。
    """
    already = state.hook_state.get(_BROKEN_KEY)
    key = cache_key(tool_name, args)
    failing = (entry or {}).get("status") == "error"
    diagnosis = (
        f"⛔ 病态重复熔断：本 run 用完全相同的参数调用 {tool_name} 已达 {count} 次"
        f"（首次在 turn {first_turn}），期间**一次写操作都没有** —— 缓存代次没变"
        f"就是这件事的机械证据。第 {HARD_REPEAT} 次起框架已明确报错要求换策略，"
        f"仍在重复，因此停机。\n"
        f"每一次重复的真实代价不是这个工具结果的大小，是把整个上下文重发一遍。\n"
        # 停机理由要说得准：全是失败的循环里没有"已确认的结论"可落盘，
        # 对着它说"本该落盘"会让读这条 blocker 的人（和 orchestrator）
        # 以为模型手里有东西没写下来。
        + (f"这 {count} 次**全部失败**，报错一字不差："
           f"{(entry or {}).get('brief') or ''}\n"
           f"本该做的那一步：先看清目标到底存不存在（list_files），"
           f"或者承认它不存在、换一件事做。"
           if failing else
           f"本该做的那一步：把已确认的结论写进产物"
           f"（write_scratchpad / save_artifact / create_claim），再从未完成的下一项继续。")
    )
    if not already:
        digest = findings_digest(state)
        from core.blockers import record_blocker

        record_blocker(
            state,
            summary=(
                f"{diagnosis}\n\n"
                + (digest or "（本 run 没有可留存的查询结论。）")
            ),
            category="missing_capability",
            requested_action=(
                (f"这一趟卡在一个**一直失败**的调用上（{tool_name}，报错："
                 f"{(entry or {}).get('brief') or ''}）。派发前先确认它要的东西"
                 f"到底存不存在：不存在就改任务或补上这个前置产物，存在就把正确"
                 f"路径/参数给它。原样重派不会有不同结果。"
                 if failing else
                 "这一趟卡在一个读不出新东西的循环上。派发前先解决它读不到的那件事："
                 "换更具体的查询、补上它缺的输入、或者确认它想做的事**根本没有对应的"
                 "工具**（那样重派多少次都一样）。原样重派不会有不同结果。")
            ),
            retryable_after_change=True,
            reported_by="framework:tool_call_cache",
        )
        state.hook_state[_BROKEN_KEY] = {
            "tool": tool_name, "cache_key": key, "repeat_count": count,
        }
        try:
            state.append_transcript(
                "tool_call_repeat_circuit_break",
                name=tool_name, repeat_count=count, first_called_at_turn=first_turn,
            )
        except Exception:
            pass
        state.hook_state["_loop_terminal"] = {
            "final_text": diagnosis,
            "status": "failed",
            "requested_by": "tool_call_cache",
            "reason": f"pathological repeat of {tool_name} ×{count}",
        }
    return {
        "status": "error",
        "error": diagnosis + "\n本 run 已被框架终止，并登记了 blocker 交给调用方。",
        "repeat_count": count,
        "first_called_at_turn": first_turn,
        "run_terminated": True,
    }


def findings_digest(state, *, max_items: int = 40) -> str | None:
    """把本 run 已确立的只读结论汇成一段紧凑台账，给**压缩后**注回上下文用。

    这是本模块的另一半价值：缓存活在 `hook_state`，**不随 messages 压缩消失**，
    所以它天然是"任务进度"的持久载体。压缩把工具结果裁掉后，把这段台账注回去，
    agent 就不会"忘了自己查过" —— 这正是 51,937 次重复查询的直接成因。

    只汇当前代次（写操作后失效的旧结论不再声称"已确立"）。无内容返 None。

    ## 两条措辞纪律（2026-08-21 事故）

    1. **只汇成功。** 失败不进结果缓存（见 `store`），所以这里天然干净。此前
       `找不到文件：…` 会以"已确立的结论"的名义被注回上下文 —— 而 `_break()`
       还会把这段写进 blocker 正文交给 orchestrator，让"读不到"变成派发决策
       依据的"事实"。
    2. **不冠名"结论"。** 这里每一行都是**截断后的摘要**，不是原文，更不是模型
       自己下的结论。把有损转述冠上"已确立"，模型就会拿它当事实用而不再核对原文。
    """
    hs = getattr(state, "hook_state", None)
    if not hs:
        return None
    cache = hs.get(_CACHE_KEY) or {}
    gen = generation(state)
    lines: list[str] = []
    for key, entry in cache.items():
        if entry.get("gen") != gen:
            continue
        tool, _, raw_args = key.partition("|")
        summary = _compact(entry.get("result"), str_cap=160, list_cap=1)
        rendered = json.dumps(summary, ensure_ascii=False)[:240]
        lines.append(f"  • {tool}({raw_args[:120]}) → {rendered}")
        if len(lines) >= max_items:
            lines.append(f"  •（另有 {max(0, len(cache) - max_items)} 条已省略）")
            break
    if not lines:
        return None
    return (
        "📒 **本 run 已经查过的条目（摘要，不是结论；压缩前留存）**\n"
        + "\n".join(lines)
        + "\n每一行都是**截断后的摘要**，不是原文。若某一项已经够用，直接用；"
        "确需更多细节时换更具体的查询。未完成的工作请从 checklist 的下一项继续。"
    )


def store(state, tool_name: str, args: dict | None, result: Any, *, turn: int,
          was_recovery: bool = False) -> Any:
    """执行完真实工具后记账。**返回要交给模型的 result**（可能附加了重复提示）。

    调用方必须用返回值替换原 result —— 附加的那条提示是"事实送达"，不是装饰。

    成功结果进结果缓存（可复用）；失败只进台账（可计数、可熔断），**不进缓存**：
    它不是结论，是"这条路走不通"。两者共用一个格子，就会出现 `找不到文件` 被
    `findings_digest` 冠名成"本 run 已确立的结论"、再被 `_break()` 写进 blocker
    正文交给 orchestrator 的那条链路（2026-08-21 实测）。
    """
    hs = getattr(state, "hook_state", None)
    if hs is None:
        return result
    if not is_cacheable(tool_name):
        note_mutation(state)
        return result
    if not isinstance(result, dict):
        return result

    key = cache_key(tool_name, args)
    ok = _is_success(result)
    brief = None if ok else str(result.get("error") or "")[:300]
    # `reset=True`：**真执行过一次，之前的重复计数就作废。**
    #
    # 计数的语义是"对着一个正摆在眼前的答案，重复问了几次"（见 lookup 的调用
    # 条件）。走到 store 说明这次工具真的跑了 —— 那要么是首次，要么是副本被
    # 驱逐后按墓碑指示重新取回。**恢复不是重复。**
    #
    # 不重置的后果实测过：把工作集预算压到极小、让每轮都驱逐，模型老老实实
    # 按墓碑重新取回，计数照样一路涨到 BREAK_REPEAT 被登记 blocker 停机 ——
    # 框架让它这么做，又为此惩罚它。这正是本次重构要消灭的那类矛盾。
    # 计数归 lookup（见那里的 ⚠️）；这里 count_delta=0，只登记"真跑了、结果是
    # 什么"，不重复计一次。
    #
    # `reset=was_recovery`：副本被驱逐后照墓碑重新取回 → 之前的重复计数作废。
    # **恢复不是重复。** 不重置的后果实测过：把工作集预算压到极小让每轮都驱逐，
    # 模型老老实实按墓碑重新取回，计数照样一路涨到 BREAK_REPEAT 被登记 blocker
    # 停机 —— 框架让它这么做，又为此惩罚它。
    led = _bump_ledger(state, key, turn=turn, reset=was_recovery, count_delta=0,
                       status="success" if ok else "error", brief=brief)
    n = int(led["count"])

    cache = hs.setdefault(_CACHE_KEY, {})
    if ok:
        cache[key] = {
            "result": result,
            "gen": generation(state),
            "turn": led["turn"],
            "hits": 0,
        }
        return result

    # 失败：清掉可能存在的旧成功副本 —— 同参数这次失败了，上次那份已经不能
    # 代表当前世界；留着它下一轮就会被当成"结果未变"复用回去。
    cache.pop(key, None)
    if n >= SOFT_REPEAT:
        result = dict(result)
        result["repeat_note"] = (
            f"⚠️ 相同参数你已经调用 {tool_name} {n} 次，**每次都是同一个失败**"
            f"（首次在 turn {led.get('turn')}）。重试不会改变结果 —— 先用 list_files "
            f"看清目录里实际有什么，或者换一件事做，别再猜同一个地址。"
            f"到第 {BREAK_REPEAT} 次框架会登记 blocker 并停掉本 run。"
        )
    return result
