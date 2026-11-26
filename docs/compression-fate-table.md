# 压缩命运表

压缩发生后，每一类上下文各自会怎样。**没有这张表时，"压缩会丢什么"是玄学** ——
每次事故都要重新读一遍 summarizer 才能回答。

原始历史先持久归档，以下命运描述的是模型工作集。生命周期 owner 是
`core/context_lifecycle.py`；策略只提供候选，最终投影还要验证来源、工具协议和预算。

四种命运：

| 命运 | 含义 |
|---|---|
| **重建** | 压缩后由机械层重新放回，不依赖摘要器是否提到它 |
| **逐字保留** | 原消息原封不动留在上下文里 |
| **摘要承载** | 内容被折进叙述；原文在持久历史中，可按引用恢复 |
| **归档 / 清除** | 内容离开工作集；留下只读恢复引用，不靠重跑动作取回 |

---

## 表

| 内容 | 命运 | 由谁保证 | 备注 |
|---|---|---|---|
| system prompt（harness prompt / rules / 平台能力 / 工作区） | **重建** | `context_engine._build_system_prompt` 每轮重建 | 不在 messages 里，压缩碰不到 |
| MEMORY.md / directives / PROFILE / PROJECT | **重建** | 同上（MEMORY 在 run 开始冻结） | 同上 |
| skills 索引 | **重建** | 同上 | 正文本就按需读，不常驻 |
| 任务清单 / 承诺账（prereg 兑现） | **重建** | 同上（在稳定前缀的可变段） | 每轮现算 |
| 首条用户输入 | **逐字保留** | `split_for_compression` 归入 head | head 永不进摘要器 |
| **后续用户指令**（steering / 补充约束） | **逐字保留** | `_preserve_user_messages` + owner 验收 | 不按长度、数量猜测权威；来源不明的旧输入保守保留 |
| 人类 resume 回答（包括 tool 角色回执） | **逐字保留** | 经控制面记录验证的 `origin` + owner 验收 | 保留整组工具协议；不能只凭 payload 自称人类 |
| 最近 N 轮对话 | 优先**逐字保留**，必要时**归档** | `split_for_compression` + `_archive_projection` | N = `keep_last_n_turns`，是容量内的偏好 |
| `keep_tool_calls` 指定的已完成工具调用 | 优先**逐字保留**，必要时**归档** | `extract_keep_tool_pairs` + owner 验收 | 不构成无限工作集豁免；原始参数与结果仍可恢复 |
| 未确认工具动作 | **逐字保留** | owner 的 pending 检查 | 不因窗口不足重发或删除 |
| 中间段的 assistant 推理 | **摘要承载 / 归档** | `_strategy_llm` 四段账本 + owner 验收 | 摘要失败时仍可归档已完成消息组 |
| 当前具名状态快照 | **逐字保留 / 重建** | owner 的 context slots | 最新值替换旧值，不被摘要转述 |
| 当前代次的工具查询台账 | **重建** | `_refresh_derived_state` + `findings_digest` | 替换 `tool_findings` slot；有界摘要先计入最终预算，不冠名科学结论 |
| 可重放的工具结果（声明了 `replayable_read`） | **清除**（可恢复） | `_strategy_clear_tool_results` | 留指针，需要时重读 |
| 声明了 `result_compactor` 的工具结果 | **清除**（压缩后保留要点） | 同上 | 由工具自己决定留什么 |
| 巨型工具结果（> 6k tokens） | **清除**（不受近因保护） | 同上 | 近因保护是给正常大小工作集的 |
| 其余已完成工具结果 | **摘要承载 / 归档** | 策略候选 + owner 验收 | 摘要失败不再盲目重复截短 |
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
所有受保护输入仍装不下时，报告 `irreducible`，不静默丢弃较早或较长的约束。

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
