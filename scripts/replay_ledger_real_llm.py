#!/usr/bin/env python3
"""真模型账本回放：v20 orchestrator checkpoint → clear → LLM 账本 → 机械评卷。

评卷标准（全部机械）：
  1. 四个 section 齐全，第一行就是 ## 目标（无口水）
  2. 探针事实保持：从原文抽 20 个高频 artifact/run id + H1/H2/H3，账本或
     留存消息里必须可见（账本只需覆盖"已完成/决策"类事实）
  3. 无编造 id：账本里出现的所有 id 必须在原文里出现过（phantom check）
  4. 第二轮增量合并：第一轮账本的条目行在第二轮里原样保留的比例
"""
from __future__ import annotations

import asyncio, json, os, re, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# provider env
for line in Path("/tmp/p2-provider.env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from core.bootstrap import bootstrap
from core.harness import SummarizerConfig
from core.llm import LLMClient, LLMMessage
from core import summarizer as sm

CKPT = ("platform/backend/data/project_worktrees/287d42e1-5a69-4972-8bae-b67e64df2fb9/"
        "ede8cb46-25ab-49cd-86fe-e822620ac3f1/.research/cache/runtime/runs/"
        "orchestrator__287d42e1-5a69-4972-8bae-b67e64df2fb9__session__"
        "ede8cb46-25ab-49cd-86fe-e822620ac3f1/messages_checkpoint.json")

ID_PAT = re.compile(r"\b[a-z_]+__[A-Za-z0-9][\w\-]{3,}|\b\d{10}-[0-9a-f]{6}\b")


class _H:
    node_type = "_orchestrator"
    max_context_tokens = 120_000
    summarizer = SummarizerConfig()


class _State:
    def __init__(self):
        self.hook_state = {}
        self.tokens_used = 0
    def append_transcript(self, *a, **k): pass
    def save_artifact(self, **k): return {"id": "x"}


async def main() -> None:
    bootstrap(force=True)
    d = json.load(open(CKPT))
    msgs = [LLMMessage(**{k: v for k, v in m.items() if k in
            ("role", "content", "tool_calls", "tool_call_id", "name",
             "reasoning_content")}) for m in d["messages"]]
    llm = LLMClient()
    state = _State()

    # 先清（escalate 第一层），再**强制**走 LLM 账本层 —— 上一版脚本吃过亏：
    # 清完已低于阈值，escalate 按设计跳过 LLM，抓到的"账本"是 checkpoint 里
    # 遗留的旧版摘要（评卷器立刻报四个 section 全缺）。用 turn 号锁定自己的
    # notice，不认遗留的。
    ctx = sm.SummarizerContext(harness=_H(), state=state, messages=msgs,
                               estimated_tokens=sm.estimate_tokens(msgs),
                               llm=llm, turn=97)
    cleared = await sm._strategy_clear_tool_results(ctx)
    print(f"清除层: {sm.estimate_tokens(msgs)} → {sm.estimate_tokens(cleared)}")
    ctx = sm.SummarizerContext(harness=_H(), state=state, messages=cleared,
                               estimated_tokens=sm.estimate_tokens(cleared),
                               llm=llm, turn=97)
    out = await sm._strategy_llm(ctx)
    after = sm.estimate_tokens(out)
    notice = next((m for m in out if "（turn 97）" in (m.content or "")), None)
    print(f"账本层: → {after}")
    assert notice is not None, "没产出本轮账本 notice"
    ledger = notice.content

    # 1. schema + 口水
    body = ledger[ledger.find("## "):] if "## " in ledger else ledger
    missing = sm._ledger_missing_sections(ledger)
    print("缺 section:", missing or "无")
    print("口水检查:", "干净" if "好的" not in ledger[:200] and "以下是" not in ledger[:100] else "有口水")

    # 2. 探针事实：原文最高频 20 个 id + 假设编号（账本或留存消息里可见）
    orig_text = "\n".join((m.content or "") for m in msgs)
    kept_text = "\n".join((m.content or "") for m in out)
    freq = Counter(ID_PAT.findall(orig_text))
    probes = [x for x, _ in freq.most_common(20)] + ["H1", "H2", "H3"]
    lost = [p for p in probes if p not in kept_text]
    print(f"探针事实: {len(probes)} 个，压缩后不可见: {len(lost)}", lost[:5] if lost else "")

    # 3. phantom check：账本里的 id 必须来自原文
    phantom = [x for x in set(ID_PAT.findall(ledger)) if x not in orig_text]
    print("编造 id:", phantom or "无")

    # 4. 增量合并：追加两轮新工作，再压一次，量第一轮条目的原样保留率
    entries1 = [l for l in ledger.splitlines() if l.strip().startswith("- ")]
    more = out + [
        LLMMessage(role="assistant", content="派 writing 出终稿",
                   tool_calls=[{"id": "cx", "function": {"name": "run_node",
                    "arguments": '{"node_type": "writing"}'}}]),
        LLMMessage(role="tool", name="run_node", tool_call_id="cx",
                   content=json.dumps({"status": "completed",
                    "child_run_id": "1786220130-bbb404",
                    "produced_artifacts": [{"id": "manuscript__final_v2"}],
                    "detail": "x" * 60_000})),
        LLMMessage(role="assistant", content="终稿完成，准备 publish"),
    ]
    ctx2 = sm.SummarizerContext(harness=_H(), state=state, messages=more,
                                estimated_tokens=sm.estimate_tokens(more),
                                llm=llm, turn=98)
    out2 = await sm._strategy_llm(ctx2)
    notice2 = next((m for m in out2 if "（turn 98）" in (m.content or "")), None)
    if notice2:
        ledger2 = notice2.content
        kept = sum(1 for e in entries1 if e.strip() in ledger2)
        print(f"增量合并: 第一轮 {len(entries1)} 条条目，第二轮原样保留 {kept} 条 "
              f"({kept * 100 // max(len(entries1), 1)}%)")
        print("新事实进账:", "manuscript__final_v2" in ledger2)
    else:
        print("增量合并: 第二轮未触发 LLM（清除已够）—— 增长门/低水位生效")
    print()
    print("═" * 60)
    print(ledger[:2400])


if __name__ == "__main__":
    asyncio.run(main())
