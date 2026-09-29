# 压缩命运表

压缩发生后，每一类上下文各自会怎样。**没有这张表时，"压缩会丢什么"是玄学** ——
每次事故都要重新读一遍 summarizer 才能回答。

三种命运：

| 命运 | 含义 |
|---|---|
| **重建** | 压缩后由机械层重新放回，不依赖摘要器是否提到它 |
| **逐字保留** | 原消息原封不动留在上下文里 |
| **摘要承载** | 内容被折进那段叙述；原文不再在场 |
| **清除** | 内容离开上下文；可恢复的留指针，不可恢复的算真丢 |

---

## 表

| 内容 | 命运 | 由谁保证 | 备注 |
|---|---|---|---|
| system prompt（harness prompt / rules / 平台能力 / 工作区） | **重建** | `context_engine._build_system_prompt` 每轮重建 | 不在 messages 里，压缩碰不到 |
| MEMORY.md / directives / PROFILE / PROJECT | **重建** | 同上（MEMORY 在 run 开始冻结） | 同上 |
| skills 索引 | **重建** | 同上 | 正文本就按需读，不常驻 |
| 任务清单 / 承诺账（prereg 兑现） | **重建** | 同上（在稳定前缀的可变段） | 每轮现算 |
| 首条用户输入 | **逐字保留** | `split_for_compression` 归入 head | head 永不进摘要器 |
| **后续用户指令**（steering / resume 回答 / 补充约束） | **逐字保留** | `_preserve_user_messages` | ≤4000 字符、最多 12 条取最近；超长的视为粘贴材料走摘要 |
| 最近 N 轮对话 | **逐字保留** | `split_for_compression` 的 tail | N = `keep_last_n_turns` |
| `keep_tool_calls` 指定的工具调用 | **逐字保留** | `extract_keep_tool_pairs` | 节点在 harness 里点名 |
| 中间段的 assistant 推理 | **摘要承载** | `_strategy_llm` 四段账本 | 目标 / 已完成 / 未决 / 决策与理由 |
| 可重放的工具结果（声明了 `replayable_read`） | **清除**（可恢复） | `_strategy_clear_tool_results` | 留指针，需要时重读 |
| 声明了 `result_compactor` 的工具结果 | **清除**（压缩后保留要点） | 同上 | 由工具自己决定留什么 |
| 巨型工具结果（> 6k tokens） | **清除**（不受近因保护） | 同上 | 近因保护是给正常大小工作集的 |
| 其余工具结果 | **摘要承载 / 清除** | 视策略 | `drop_tool_results` 是兜底 |
| 上一轮的压缩 notice | **清除** | `_head_without_superseded_notices` | 内容已并入新摘要；不删会每压一次涨一段 |

---

## 判据

**契约类内容必须是「重建」，不能是「摘要承载」。**

prereg、expected_inputs、白板、任务清单这些是**约束**，不是叙述素材。让摘要器
转述一遍约束，等于给它一次改写约束的机会 —— 而它改写得很像原文，没人看得出来。
现在它们全部走 `context_engine` 每轮重建，压缩碰不到。

**用户指令必须是「逐字保留」。**
把「用现成的 retry helper，别新写一个」paraphrase 一遍，正是模型之后自信地做了
被明确禁止的事的那条路径。指令不是可以概括的素材。

**「清除」必须留可恢复指针。**
回放工具（`scripts/replay_summarizer_on_checkpoints.py`）量的就是这个：
一条事实（artifact id / run id / 文件路径）在压缩后，要么还在文本里，要么其所在
消息带可恢复指针；两者都不满足才算**真丢**。该数必须为 0。

---

## 怎么验证这张表还是真的

```bash
HARNESS_REPLAY_CHECKPOINTS='<你的 checkpoint glob>' \
  python scripts/replay_summarizer_on_checkpoints.py
```

在真实历史 checkpoint 上回放，末尾会汇总"事实销毁"数，非零则脚本以非零码退出。
扫不到任何 checkpoint 时它会**报错退出**，不会打一张空表让你以为验过了。
