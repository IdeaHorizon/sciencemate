"""记忆系统真回路 e2e（框架侧）：一条铁律与一条教训，从产生到被下游收到。

    python scripts/e2e_memory_loop.py

跑真 State / 真 worktree / 真 hook / 真工具派发，在临时目录里建项目，
不碰用户数据。7 个场景、23 条断言。

这里跑的是**真框架**（真 State、真 worktree、真 agent_loop hook、真工具派发），
只把 LLM 换成脚本化的 FakeLLM —— 它按预定顺序发真 tool_call。

为什么这样切：模型的判断力（会不会想到立法、抽象得好不好）需要真 provider；
但**框架侧的接线**（采集用户原话 → 核验出处 → 落盘 → 下一轮注入 → reviewer
展开 → 首用附单）不该依赖模型的心情才验得了。两半分开验，各自可复现。
"""
import os, sys, json, asyncio, subprocess, tempfile
from pathlib import Path

SC = Path(__file__).resolve().parent
WORK = Path(tempfile.mkdtemp(prefix="mem-e2e-"))
os.environ["HARNESS_FRAMEWORK_HOME"] = str(WORK)
sys.path.insert(0, str(SC.parent))

from core.bootstrap import bootstrap
from core.state import State
from core.loader import load_harness
from core.context_engine import build_messages
from core.llm import LLMMessage
from core.tool_registry import execute as run_tool
from core import memory as M
from core.memory_delivery import constitution_block, onboarding_slice, law_checklist
from core.loop_hooks import HookContext
from core.loop_hooks_builtin import (
    _memory_onboarding_on_turn_start, _law_review_gate_on_turn_start)
bootstrap()

OK, BAD = [], []
def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))

def rule(t): print(f"\n{'='*74}\n  {t}\n{'='*74}")

# ── 真 Project worktree ────────────────────────────────────────────────────
WT = WORK / "proj"; WT.mkdir(parents=True)
for c in (["git","init","-q"],["git","config","user.email","t@t"],["git","config","user.name","t"]):
    subprocess.run(c, cwd=WT, check=True)
(WT/".gitkeep").write_text(""); subprocess.run(["git","add","-A"],cwd=WT,check=True)
subprocess.run(["git","commit","-qm","init"],cwd=WT,check=True)

def mk(node):
    st = State.new(node_type=node, base_dir=WORK/"runs"/node,
                   project_id="e2e_mem", project_worktree=WT)
    M.ensure_skeleton(st)
    return st

async def main():
    # ══ 场景 1：用户跨三轮反复强调 → 调度器抽象成铁律 ═══════════════════
    rule("场景 1 · 用户零散强调 → 调度器抽象立法")
    orch = mk("_orchestrator")
    # 真实形态：用户从没说"以后都要"，只是三次纠正同一件事
    orch.hook_state["_user_utterances"] = [
        "这个 bug 你别绕过去，直接改掉",
        "又绕了啊，我说了遇到改不动的就重构",
        "不要图省事，不要糊窗户纸",
    ]
    law = "任何修复必须回答：改的是产生问题的那一层，还是症状层？答不出的不许合。"
    r = await run_tool("memory_write", orch, section="law", text=law,
                       derived_from=["别绕过去", "遇到改不动的就重构", "不要糊窗户纸"])
    check("立法成功（无收件箱、无人审）", r.get("status")=="success", str(r)[:120])
    check("正文是抽象版，不等于任何一句原话",
          law not in " ".join(orch.hook_state["_user_utterances"]))
    check("出处三条都留痕", len(M.laws(orch)[0].derived_from)==3)
    check("返回值要求告知用户", "告诉用户" in str(r.get("next_step","")))

    # 防伪：换一句用户没说过的
    bad = await run_tool("memory_write", orch, section="law",
                         text="所有实验必须跑够十万步",
                         derived_from=["实验要跑十万步"])
    check("凭空立法被拒", bad.get("code")=="derived_from_not_found")
    check("拒了就没落盘", len(M.laws(orch))==1)

    # ══ 场景 2：铁律下一轮到达每个节点 ═════════════════════════════════
    rule("场景 2 · 铁律送达（下游节点、真 build_messages）")
    for node in ("hypothesis","experiment","writing","_curator"):
        st = mk(node)
        blob = "\n".join((m.content or "") for m in build_messages(load_harness(node), st, {}))
        check(f"{node} 的 prompt 里有这条铁律", "还是症状层" in blob)

    # ══ 场景 3：reviewer 把铁律展开成必答检查项 ════════════════════════
    rule("场景 3 · reviewer 红旗（机械展开）")
    rv = mk("_reviewer")
    out = _law_review_gate_on_turn_start(
        HookContext(state=rv, harness=load_harness("_reviewer"), turn=1, messages=[]))
    body = out[0].content if out else ""
    check("reviewer 收到检查项", bool(out))
    check("要求逐条回答", "逐条回答" in body and "遵守 / 违反 / 不适用" in body)
    check("红旗不硬闸（措辞明确）", "不必因此直接 revise" in body)
    check("非 reviewer 节点不收", _law_review_gate_on_turn_start(
        HookContext(state=mk("experiment"), harness=load_harness("experiment"),
                    turn=1, messages=[])) is None)

    # ══ 场景 4：experiment 踩坑 → 写手册 → writing 开工收到 ═════════════
    rule("场景 4 · 教训沉淀与跨节点送达")
    exp = mk("experiment")
    r = await run_tool("memory_note", exp,
                       text="冻结 experiment_log 前必须先把引用的 claim 登记进 KB，否则引用校验会挂",
                       category="pitfall", nodes=["experiment","writing"],
                       tools=["freeze_artifact"])
    check("教训直接入册（无候选队列）", r.get("created") is True)

    wr = mk("writing")
    sl = onboarding_slice(wr, "writing", [t for t in (load_harness("writing").tools or [])])
    check("writing 开工切片收到它", sl is not None and "登记进 KB" in (sl or ""))
    lit = mk("literature")
    sl2 = onboarding_slice(lit, "literature", [])
    check("literature 不该收到（适用面不含它）", sl2 is None or "登记进 KB" not in sl2)

    # ══ 场景 5：首用附单在动手时刻弹出 ═════════════════════════════════
    rule("场景 5 · 首用附单")
    exp2 = mk("experiment")
    await run_tool("memory_note", exp2, text="save_artifact 之前先确认类型已注册 E2ECANARY",
                   tools=["save_artifact"], nodes=["experiment"])
    r1 = await run_tool("save_artifact", exp2, artifact_type="analysis_report",
                        name="a1", content="x")
    r2 = await run_tool("save_artifact", exp2, artifact_type="analysis_report",
                        name="a2", content="y")
    check("首次调用附上教训", "E2ECANARY" in str(r1.get("memory_note_on_this_tool","")))
    check("第二次不再附", "memory_note_on_this_tool" not in r2)

    # ══ 场景 6：用户撤律 ═══════════════════════════════════════════════
    rule("场景 6 · 撤销归用户")
    orch.hook_state["_user_utterances"] = ["这次先跑起来再说"]
    r = await run_tool("memory_write", orch, section="law", retire="任何修复")
    check("模型自作主张撤 → 拒", r.get("code")=="only_user_can_retire_a_law")
    orch.hook_state["_user_utterances"] = ["那条修复的铁律撤了吧"]
    r = await run_tool("memory_write", orch, section="law", retire="任何修复")
    check("用户说撤 → 撤掉", r.get("status")=="success" and M.laws(orch)==[])
    st = mk("experiment")
    blob = "\n".join((m.content or "") for m in build_messages(load_harness("experiment"), st, {}))
    check("撤后不再注入", "还是症状层" not in blob)

    # ══ 场景 7：Git 里是不是人能读的 ═══════════════════════════════════
    rule("场景 7 · MEMORY.md 的可读性")
    doc = M.read_document(orch)
    print(doc)
    check("是一个 Git 文件", (WT/"MEMORY.md").is_file())

    rule(f"结果：{len(OK)} 通过 / {len(BAD)} 失败")
    if BAD: print("  失败：", BAD)
    print(f"\n工作目录 {WORK}")

asyncio.run(main())
