"""记忆系统真回路 e2e（**模型侧**）：真 LLM 会不会用对这套东西。

    python scripts/e2e_memory_live.py          # 需要 .env 里的 LLM provider 可达

## ⚠️ 模型选择：v4-pro 在积算通道上带工具就崩

2026-08-21 受控实验（本脚本的副产物）：

    deepseek-v4-pro   + 1 个工具 schema  → 生成塌成重复字符，finish=length
    deepseek-v4-pro   + 0 个工具         → 正常，finish=stop
    deepseek-v4-flash + 1 个工具         → 正常发 tool_call
    deepseek-v4-flash + 45KB ctx + 工具  → 正常发 tool_call

**触发变量是"带不带工具 schema"，不是上下文长度**（158 字节的 prompt 崩得
和 45KB 一样彻底）。这一条**修正**了之前记在账上的病因（"长 harness ctx ×
并行 tool_calls 的交互"）—— 长 ctx 是无辜的。

所以本脚本默认用 flash 跑。要验 pro 是否恢复，直接换 LLM_MODEL 再跑一遍。

## 这个脚本验的东西，`e2e_memory_loop.py` 验不了

那个跑的是**框架接线**：采集原话 → 核验出处 → 落盘 → 下一轮注入 →
reviewer 展开 → 首用附单。它用脚本化的调用，所以可复现、不花钱、
不看 provider 脸色。

但整套设计里有一半押在**模型的判断力**上，而那是 prompt 引导的事：

  1. 用户从没说"以后都要"、只是跨轮反复纠正 —— 模型会不会**想到**立法？
  2. 它会**抽象**成一条干练可判定的规矩，还是把用户原话抄一遍？
  3. `derived_from` 会不会挑出**真实存在**的原话片段（挑错就被框架拒）？
  4. 写教训时会不会给出**合理的 applies_to**（写不出适用面就入不了册）？

这四条只有真模型能回答。prompt 写得好不好，不跑一次就是猜。

## 判据

每一项都有**机械判据**（不靠人读着觉得像）：立法调用发生没有、正文是不是
逐字复制、出处核验过没过、适用面填了没有。跑完打分并给出模型的实际输出，
差在哪一眼能看见 —— 这个脚本的产出是**改 prompt 的依据**，不是一个通过率。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SC = Path(__file__).resolve().parent
sys.path.insert(0, str(SC.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(SC.parent / ".env")
except Exception:
    pass

WORK = Path(tempfile.mkdtemp(prefix="mem-live-"))
os.environ["HARNESS_FRAMEWORK_HOME"] = str(WORK)

from core import memory as M  # noqa: E402
from core.bootstrap import bootstrap  # noqa: E402
from core.llm import LLMClient  # noqa: E402
from core.loader import load_harness  # noqa: E402
from core.state import State  # noqa: E402

bootstrap()

#: 真实形态：用户**从没说**"以后都要遵守"，只是三次为同一件事纠正你。
#: 设计要求模型自己看出这是一条规矩 —— 别等他明说。
USER_TURNS = [
    "这个 bug 你别绕过去，直接改掉",
    "又绕了啊，我说了遇到改不动的就重构，别在外面包一层",
    "不要图省事，不要糊窗户纸。以后都按这个来",
]


def _worktree() -> Path:
    wt = WORK / "proj"
    wt.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=wt, check=True)
    (wt / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=wt, check=True)
    return wt


def _call_name(c) -> str:
    """tool_call 的名字。**按结构取，不按属性猜** —— 不同 provider 的
    ToolCall 形状不一样（有的是对象属性，有的是 OpenAI 原样 dict），
    猜错的后果是"模型一个工具都没调"这种指向假原因的结论。"""
    fn = getattr(c, "function", None)
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    if isinstance(c, dict):
        return str((c.get("function") or {}).get("name") or c.get("name") or "")
    return str(getattr(c, "name", "") or "")


def _call_args(c) -> dict:
    fn = getattr(c, "function", None)
    raw = None
    if isinstance(fn, dict):
        raw = fn.get("arguments")
    elif isinstance(c, dict):
        raw = (c.get("function") or {}).get("arguments") or c.get("arguments")
    else:
        raw = getattr(c, "arguments", None)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return raw or {}


async def _drive(llm, state, node_type: str, user_text: str, *,
                 want: str = "", max_turns: int = 3):
    """真回路的最小驱动器：真 prompt、真工具、**把工具结果喂回去继续**。

    只采一轮是不够的：节点有自己的开场契约（experiment 第一轮必须
    `classify_experiment_scope`），一轮就下结论会把"节点在走流程"误判成
    "模型没听懂"。实测踩过：模型的 reason 字段明写着"用户要求记录一条
    可复用的教训（memory_note）"，而它那一轮先做了强制开场。
    """
    from core.context_engine import build_messages
    from core.llm import LLMMessage
    from core.tool_registry import (
        execute as run_tool, list_tools_for_node, to_openai_schema,
    )

    harness = load_harness(node_type)
    msgs = build_messages(harness, state, {})
    msgs.append(LLMMessage(role="user", content=user_text))
    tools = [to_openai_schema(td) for td in
             list_tools_for_node(node_type, list(harness.tools or []), state=state)]

    seen: list = []
    for turn in range(1, max_turns + 1):
        resp = await llm.chat(msgs, tools=tools, max_tokens=1500)
        calls = list(getattr(resp, "tool_calls", None) or [])
        seen += calls
        names = [_call_name(c) for c in calls]
        print(f"    轮 {turn}: {names or '（无 tool_call）'}")
        if not calls or (want and want in names):
            return seen, (resp.content or "")
        msgs.append(LLMMessage(role="assistant", content=resp.content,
                               tool_calls=calls,
                               reasoning_content=getattr(resp, "reasoning_content", None)))
        for c in calls:
            cid = (c.get("id") if isinstance(c, dict) else getattr(c, "id", "")) or ""
            name = _call_name(c)
            try:
                out = await run_tool(name, state, **_call_args(c))
            except Exception as exc:
                out = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            msgs.append(LLMMessage(role="tool", tool_call_id=cid, name=name,
                                   content=json.dumps(out, ensure_ascii=False)[:2000]))
    return seen, ""


async def main() -> int:
    print(f"provider: {os.getenv('LLM_MODEL')} @ {os.getenv('LLM_BASE_URL')}")
    llm = LLMClient()
    wt = _worktree()
    st = State.new(node_type="_orchestrator", base_dir=WORK / "runs",
                   project_id="live_mem", project_worktree=wt)
    M.ensure_skeleton(st)

    findings: list[tuple[str, bool, str]] = []

    def note(name: str, ok: bool, detail: str = "") -> None:
        findings.append((name, ok, detail))
        print(f"  {'✓' if ok else '✗'} {name}" + (f"\n      {detail}" if detail else ""))

    # ── 跨三轮喂用户的话，看模型第几轮想到立法 ────────────────────────────
    print("\n── 模型会不会想到立法（用户从没说'这是规矩'）──────────────")
    law_call = None
    for i, turn in enumerate(USER_TURNS, 1):
        st.hook_state["_user_utterances"] = USER_TURNS[:i]
        calls, text = await _drive(llm, st, "_orchestrator", turn,
                                   want="memory_write", max_turns=2)
        print(f"  用户「{turn[:24]}…」")
        for c in calls:
            args = _call_args(c)
            if _call_name(c) == "memory_write" and args.get("section") == "law":
                law_call = args
                print(f"      ↑ 第 {i} 轮立法")
                break
        if law_call:
            break

    note("模型自己想到立法（无人明说'记成规矩'）", law_call is not None,
         "" if law_call else "三轮都没调 memory_write(section='law') —— prompt 引导没生效")

    if law_call:
        text = str(law_call.get("text") or "")
        srcs = [str(s) for s in (law_call.get("derived_from") or [])]
        print(f"\n  模型写的铁律：\n      {text}")
        print(f"  它给的出处：{srcs}")

        note("正文是抽象的，不是原话复制",
             bool(text) and all(text.strip() not in u for u in USER_TURNS),
             "" if text else "正文为空")
        # 「可判定」的判据不能拿问号当代理指标 —— 实测模型写过一条陈述式的
        # 好铁律（"不得报完成/如实说明卡在哪"，完全能判遵守/违反）却被判不合格。
        # 换成看有没有**规范性动词**：它决定 reviewer 能不能答"遵守/违反"。
        # 措辞好不好是语义判断，机械层只挡"纯口号"那一档。
        normative = ("必须", "不许", "不得", "禁止", "应当", "要求", "才能",
                     "否则", "must", "not allowed", "shall")
        slogan_only = len(text) < 20
        note("正文可判定（有规范性动词，不是纯口号）",
             bool(text) and not slogan_only and any(k in text for k in normative),
             f"措辞：{text[:70]}" if text else "")
        note("给了 derived_from", bool(srcs))
        real = [s for s in srcs
                if any("".join(s.split()) in "".join(u.split()) for u in USER_TURNS)]
        note("出处是真实原话片段（框架会拒假的）", bool(srcs) and len(real) == len(srcs),
             f"对不上的：{[s for s in srcs if s not in real]}" if len(real) != len(srcs) else "")

        # 真走一遍工具 —— 框架的核验是最终判据
        from core.tool_registry import execute as run_tool

        st.hook_state["_user_utterances"] = USER_TURNS
        res = await run_tool("memory_write", st, **law_call)
        note("框架接受了模型这次立法", res.get("status") == "success",
             str(res)[:160])

    # ── 教训：模型会不会给合理的适用面 ────────────────────────────────────
    print("\n── 写教训时会不会给出适用面（写不出就入不了册）──────────────")
    exp = State.new(node_type="experiment", base_dir=WORK / "runs2",
                    project_id="live_mem", project_worktree=wt)
    calls, text = await _drive(
        llm, exp, "experiment",
        "刚才冻结 experiment_log 挂了，因为里面引用的 claim 还没写进 KB。"
        "把这个坑记下来，别让下次跑的人再踩。",
        want="memory_note", max_turns=4)
    note_call = next((c for c in calls if _call_name(c) == "memory_note"), None)
    print(f"  tool_calls={[_call_name(c) for c in calls] or '（无）'}")
    note("模型用了 memory_note", note_call is not None)
    if note_call:
        args = _call_args(note_call)
        print(f"  它写的：{str(args.get('text'))[:90]}")
        print(f"  适用面：nodes={args.get('nodes')} tools={args.get('tools')}")
        note("给了适用面（nodes 或 tools）",
             bool(args.get("nodes") or args.get("tools")),
             "两个都空 —— 框架会拒，模型没读懂 applies_to 的要求")

    ok = sum(1 for _, good, _ in findings if good)
    print(f"\n{'=' * 70}\n  模型侧：{ok} / {len(findings)} 项达标\n{'=' * 70}")
    print(f"\n工作目录 {WORK}")
    if ok < len(findings):
        print("\n未达标的项**是改 prompt 的依据** —— 模型没做到的事，多半是"
              "工具描述或节点 prompt 没讲清，不是模型笨。")
    return 0 if ok == len(findings) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception as exc:
        # 归属要分清：provider 不可达 vs 本脚本自己坏了。把后者报成前者，
        # 就是本仓反复付学费的那件事 —— 报错指向假原因比不报错更费时间。
        from core.llm import LLMHTTPError

        network = (isinstance(exc, LLMHTTPError)
                   or isinstance(exc, (ConnectionError, TimeoutError, OSError))
                   or "Connection" in type(exc).__name__)
        if network:
            print(f"\n⚠️ provider 不可达：{type(exc).__name__}: {str(exc)[:160]}")
            print("这不是平台、也不是记忆系统的问题 —— 先确认 .env 里的 LLM "
                  "endpoint 可达（实验室网内地址需要连内网）。\n"
                  "框架侧接线用 scripts/e2e_memory_loop.py 验，它不依赖 provider。")
            raise SystemExit(2)
        print(f"\n💥 **本脚本自己坏了**（不是 provider 的锅）：")
        import traceback

        traceback.print_exc()
        raise SystemExit(3)
