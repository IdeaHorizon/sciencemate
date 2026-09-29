"""REDIRECT 的目标必须是**跑完能把这条 flow 关掉**的节点。

## 病根

一条 `redirect_upstream` 授权的全部意义是"去起 target，起完这条审查义务就关了"。
可"起完能关"在 run_node 里不是自动成立的 —— 绑定、空转计数、闭合三件事全挂在
同一个前提上：

    run_node.py  if node_owes_post_node_flow(node_type):   ← 服务节点为 False
                     ...把 entry 绑成 action_in_progress...

绑定被跳过 ⇒ entry 永不 `action_in_progress` ⇒ `_find_in_progress_entry` 永远
返回 None ⇒ **既关不掉，也不被空转熔断看见**。熔断器恰好在最需要它的那一档缺席。

2026-09-17 yuankk 的会话是这条链的完整实例：reviewer 把根因指向 `postprocess`
（早已改造成 figures 服务，`post_run_flow: none`），授权通过，然后 hook 每轮喊
"去起 postprocess"、模型每轮照做、账本一动不动，转了 40 轮；最后模型申报
`blocked` 又被"pending_post_node_flow 非空"驳回 —— 它被卡住的那件事，正是禁止它
说自己被卡住的那件事。

## 为什么是扫盘不是名单

框架**自己**把不可执行的目标摆进了候选集：`PIPELINE_ORDER` 那张手写名单里还留着
`postprocess`，_reviewer 的 harness 正文里举的例子是 `literature` / `postprocess` /
`analysis` —— 写这些字的时候它们还都是产出节点。删掉这几个名字是打补丁：下一个
改成服务的节点照样漏。所以判据现算，覆盖三处：

  1. 候选集（`upstream_candidates`）不许产出不可执行的目标；
  2. reviewer 的 `set_recommended_action` 当场拒绝并列出合法值；
  3. 授权那一刻（`present_decision_package`）再拒一次 —— 人工/自由填写绕得过前两道；

外加第 4 条：**没有任何 prompt 正文点名一个不可执行的目标**。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.loader import list_harnesses, node_owes_post_node_flow
from core.upstream_routing import PIPELINE_ORDER, upstream_candidates
from shared.tools.library.decision_package import _NORMAL_ACTIONS, record_decision_answer

NODES = Path(__file__).resolve().parents[1] / "nodes"

#: 每个夹具各用各的 scratch 目录 —— 同一条测试里调两个 helper 会撞同名目录。
_seq = iter(range(10_000))


def _cannot_close_a_flow() -> set[str]:
    return {n for n in list_harnesses() if not node_owes_post_node_flow(n)}


def _binding_guard_of_run_node() -> str | None:
    """run_node 里"把 flow entry 绑成本次 run 的前驱"那道 if，测的是哪个函数。

    绑定是整条链的咽喉：绑不上 ⇒ 不进 action_in_progress ⇒ 空转不计数、flow 不
    闭合。所以"授权侧放行的目标集"必须恰好等于"这道 if 放行的目标集"。
    """
    import ast
    import inspect

    import shared.tools.run_node as rn

    tree = ast.parse(inspect.getsource(rn))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        binds = any(
            isinstance(n, ast.Name) and n.id == "authorized_flow_entry"
            and isinstance(n.ctx, ast.Store)
            for n in ast.walk(node)
        )
        if binds and isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Name):
            return node.test.func.id
    return None


def test_the_service_nodes_are_really_service_nodes():
    """这条是下面几条的前提 —— 前提塌了它们会静默变成空断言。"""
    unable = _cannot_close_a_flow()
    assert "postprocess" in unable, "postprocess 不再是服务节点了？那本文件的病例得重写"
    assert {"literature", "data"} <= unable
    assert "writing" not in unable and "experiment" not in unable


def test_the_candidate_set_never_offers_a_target_that_cannot_close_the_flow():
    """框架不许把不可执行的目标递到 reviewer 面前。"""
    unable = _cannot_close_a_flow()
    for node in (*PIPELINE_ORDER, *list_harnesses()):
        offered = set(upstream_candidates(node))
        assert not (offered & unable), (
            f"upstream_candidates({node!r}) 给出了关不掉 flow 的目标："
            f"{sorted(offered & unable)}"
        )


def _recommend(tmp_path, target: str) -> dict:
    """走 reviewer 真正那条路：production 的 compose_review_critique handler。"""
    import asyncio

    from core.state import State
    from nodes._reviewer.tools.critique_builder import _compose_review_critique

    root = tmp_path / f"rev{next(_seq)}"
    root.mkdir(parents=True)
    state = State(run_id="r", node_type="_reviewer", root=root)
    return asyncio.run(_compose_review_critique(
        state, "set_recommended_action",
        recommended_action="redirect_upstream", target_node=target,
        feedback_to_next_run="补齐后重来",
    ))


def test_the_reviewers_own_tool_refuses_such_a_target(tmp_path):
    out = _recommend(tmp_path, "postprocess")
    assert out["status"] == "error", "reviewer 指向服务节点竟然被接受了"
    assert "postprocess" in out["error"]
    # 契约要送到调用方：报错必须列出合法值，否则模型只能瞎猜下一个名字。
    assert "writing" in out["error"] and "experiment" in out["error"]


def test_the_reviewers_own_tool_still_accepts_a_real_upstream(tmp_path):
    """对照：这道闸不能把合法的 redirect 一起挡掉。"""
    out = _recommend(tmp_path, "hypothesis")
    assert out["status"] == "success", out


#: 我们改得了的 prompt 正文。producing 节点（literature/hypothesis/data/experiment/
#: postprocess/writing）归各自 owner，按节点 owner 红线只诊断不代改 —— 所以那边
#: 的存量措辞由下面那条"如实报出来"的测试负责暴露，不由这条判红。
#: 架构组自己维护的节点 —— 这几个的 prompt 判红（其余节点归 owner，红线：
#: 只诊断不代改，由下面那条机械网兜住）。`writing` 2026-09-21 加入：它早已归
#: 架构组（writing 重建），而 #1061 正是它的 review_spec 还在指派 `postprocess`
#: —— 缺陷躲过这道闸整整四天，就因为这张 owner 表没跟上 owner 本身。
_OURS = ("_orchestrator", "_reviewer", "_curator", "writing")

#: 指派关键词。名字出现在它**后面**才算"被指派成目标"。
_DESIGNATES = re.compile(r"target_node|redirect_upstream|redirect")

#: 名字后面紧跟这些，说明它是句子的**主语**而不是被指派的目标
#: （例："`redirect_upstream` 几乎不会用（literature 是上游头）"）。
#: 把解释性句子一起判红，只会逼人去加豁免 —— 豁免一开这道闸就死了。
_IS_THE_SUBJECT = re.compile(r"^\s*[`'\"]?\s*(是|不|没|这|那|侧|节点是)")


def _designated_dead_targets() -> list[tuple[str, str]]:
    """(相对路径:行号 → 目标, 整行) —— prompt 里被**指派**成 redirect 目标、
    而跑完关不掉 flow 的那些节点名。

    判据刻意不绑写法：第一版只认 `target_node=X` 那几种等号形态，于是漏掉了
    真实存在的 `target_node 指向实际负责出图的 analysis 或 postprocess`
    —— 护栏扫的是写法不是那件事，写法一换就穿过去了。现在只问两件事：
    这一行在说 redirect 吗；关不掉 flow 的那个名字出现在指派词后面吗。
    """
    unable = _cannot_close_a_flow()
    found: list[tuple[str, str]] = []
    for path in sorted(NODES.rglob("*")):
        if path.suffix not in (".yaml", ".md") or not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if "redirect" not in line:
                # 只管 **redirect 目标**。同名的 `target_node` 还用在两个别的地方
                # （actionable_next_step 派谁去做、REVISE 退回产出节点自己），那两
                # 处指向服务节点完全合法 —— 一起判红就是在制造豁免需求。
                continue
            keyword = _DESIGNATES.search(line)
            if keyword is None:
                continue
            for node in unable:
                for m in re.finditer(rf"\b{re.escape(node)}\b", line):
                    if m.start() <= keyword.start():
                        continue
                    if _IS_THE_SUBJECT.match(line[m.end():]):
                        continue          # 在解释它，不是在指派它
                    found.append((
                        f"{path.relative_to(NODES.parent)}:{lineno} → {node!r}",
                        line.strip()[:110],
                    ))
                    break
    return found


def test_no_prompt_we_own_designates_a_target_that_cannot_close_the_flow():
    """架构侧的 prompt 点名的目标必须真能执行。

    _reviewer 的正文原先举的三个例子（literature / postprocess / analysis）今天
    全都已经不可执行 —— 写下它们的时候它们还是产出节点。举例是会烂的，所以判据
    现算、扫盘，不写名单。
    """
    offenders = [f"{where}  |  {line}" for where, line in _designated_dead_targets()
                 if any(f"nodes/{ours}/" in where for ours in _OURS)]
    assert not offenders, (
        "我们自己的 prompt 指派了一个跑完关不掉 flow 的 redirect 目标 —— "
        "模型照做就会空转，而空转熔断在这一档结构性缺席：\n  "
        + "\n  ".join(offenders)
    )


def test_the_mechanical_net_catches_what_a_stale_prompt_still_says():
    """owner 节点里的存量措辞**不判红**（红线：只诊断不代改），但必须接得住。

    这条才是真正的防线：不管哪份 spec 还写着"退回 literature"，模型照做时
    `set_recommended_action` 当场拒绝并列出合法值 —— prompt 烂掉的代价从"空转
    40 轮"降到"一次被驳回的工具调用"。

    同时把存量如实列出来（失败信息里带全），供转告各 owner。
    """
    stale = _designated_dead_targets()
    named = {where.rsplit("→ ", 1)[-1].strip("'\"") for where, _ in stale}
    for node in named:
        out = _recommend_with_tmp(node)
        assert out["status"] == "error", (
            f"{node!r} 还写在某份 spec 里，而机械闸放行了它 —— 防线不在场：\n  "
            + "\n  ".join(f"{w}  |  {l}" for w, l in stale)
        )


def _recommend_with_tmp(target: str) -> dict:
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        return _recommend(_P(td), target)


# ── 授权那一刻（最后一道，也是唯一挡得住人工的一道）────────────────────────
#
# reviewer 的工具挡不住两种来路：人在决策卡上手选 REDIRECT 并自己填目标；以及
# 早于本次修复就已经躺在 hook_state 里的存量 entry（yuankk 那两条就是）。
# `record_decision_answer` 是这两条路共同的收口。

def _flow_with_a_redirect_recommendation(tmp_path, target: str):
    from core.state import State

    base = tmp_path / f"run{next(_seq)}"
    base.mkdir()
    state = State.new(node_type="_orchestrator", base_dir=base, project_id="p1")
    entry = {
        "producing_node": "writing",
        "producing_run_id": "run-w-1",
        "artifact_ids": ["research_article__x"],
        "review_state": "done",
        "review_critique_artifact_id": "review_critique__c",
        "curator_state": "done",
        "decision_state": "pending",
        "decision_options": list(_NORMAL_ACTIONS),
        "recommended_target_node": target,
    }
    state.hook_state["pending_post_node_flow"] = [entry]
    return state


def test_authorization_refuses_a_target_that_cannot_close_the_flow(tmp_path):
    """人手选 REDIRECT + 服务节点 → 绝不留下 action_authorized。"""
    state = _flow_with_a_redirect_recommendation(tmp_path, "postprocess")
    entry = record_decision_answer(
        state, {"type": "decision_package", "producing_run_id": "run-w-1"},
        str(_NORMAL_ACTIONS.index("redirect_upstream") + 1))

    assert entry["decision_state"] == "awaiting_human", (
        "一条起谁都关不掉的义务被授权出去了 —— 这正是 yuankk 空转 40 轮的那一步"
    )
    assert entry.get("authorized_target_node") is None
    assert "postprocess" in entry["decision_validation_error"]
    # 决策留在人手上（而不是静默降级成 revise —— 那会抹掉"根因在别处"的诊断）
    assert entry["accepted_action"] is None


def test_authorization_still_accepts_a_target_that_can(tmp_path):
    """对照：合法 redirect 一路通到 action_authorized。"""
    state = _flow_with_a_redirect_recommendation(tmp_path, "experiment")
    entry = record_decision_answer(
        state, {"type": "decision_package", "producing_run_id": "run-w-1"},
        str(_NORMAL_ACTIONS.index("redirect_upstream") + 1))

    assert entry["decision_state"] == "action_authorized", entry
    assert entry["authorized_target_node"] == "experiment"


def test_the_authorized_target_is_exactly_what_run_node_can_bind_and_close(tmp_path):
    """判据对齐：授权放行的目标集 ≡ run_node 绑得上的目标集。

    两边各写一遍判据，就会各自漂移 —— 这次事故正是"候选集用手写名单、执行侧
    用 harness 声明"漂了之后的产物。这条把两边钉死在同一个函数上。
    """
    # run_node 那一侧的判据用 AST 取，不用 substring —— 名字"在文件里出现过"
    # 证明不了它守着**这个** if（同名调用在本文件里有七处）。
    assert _binding_guard_of_run_node() == "node_owes_post_node_flow", (
        "run_node 里绑定 authorized_flow_entry 的那道 if 换判据了 —— 授权侧必须"
        "跟着换，否则两边又分叉，而分叉的后果就是一条关不掉的义务"
    )
    for node in list_harnesses():
        state = _flow_with_a_redirect_recommendation(tmp_path, node)
        entry = record_decision_answer(
            state, {"type": "decision_package", "producing_run_id": "run-w-1"},
            str(_NORMAL_ACTIONS.index("redirect_upstream") + 1))
        authorized = entry["decision_state"] == "action_authorized"
        assert authorized is node_owes_post_node_flow(node), (
            f"{node!r}: 授权侧说 {authorized}，run_node 绑定侧说 "
            f"{node_owes_post_node_flow(node)}"
        )


# ── 呈递那一刻：推荐出去的动作必须执行得了 ──────────────────────────────────
#
# 授权侧会拒绝一个关不掉 flow 的目标，但**推荐照旧** → 自动裁决每轮挑同一个被拒
# 的推荐，拒了再呈递、呈递再拒：换成一个更短的死循环而已。要让它第一轮就终止，
# 推荐本身就不能是那个东西。

def _present(tmp_path, *, target: str, action: str = "redirect_upstream",
             verdict: str = "major_concerns"):
    """走真入口 `present_decision_package`，拿 pause 的 metadata。"""
    import asyncio
    import json as _json

    from core.artifact_provenance import forwarded, produced
    from core.state import State
    from core.tool_registry import execute as execute_tool

    base = tmp_path / f"pre{next(_seq)}"
    base.mkdir()
    state = State.new(node_type="_orchestrator", base_dir=base, project_id="p1")
    art = state.save_artifact(
        "review_critique", "c1",
        _json.dumps({"verdict": verdict, "concerns": [], "strengths": [],
                     "recommended_action": {"action": action,
                                            "target_node": target,
                                            "feedback_to_next_run": "图全缺"}}),
        {"produced_by_node_type": "_reviewer", "source_node_type": "writing"},
        provenance=forwarded(produced("_reviewer", "run-rev"),
                             via_node_type=state.node_type, via_run_id=state.run_id))
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "writing", "producing_run_id": "run-w-1",
        "artifact_ids": ["research_article__x"], "review_state": "done",
        "curator_state": "done", "decision_state": "pending"}]
    res = asyncio.run(execute_tool(
        "present_decision_package", state, source_node_type="writing",
        producing_run_id="run-w-1", review_critique_artifact_id=art["id"]))
    return state, res["pause_event"]


def test_an_unexecutable_recommendation_is_withdrawn_before_it_is_offered(tmp_path):
    state, pause = _present(tmp_path, target="postprocess")
    md = pause["metadata"]
    assert md["recommended_action"] != "redirect_upstream", (
        "框架推荐了一个起了也关不掉 flow 的目标 —— 自动裁决会照单全收，"
        "然后被授权侧拒掉，拒了再呈递，换成一个更短的死循环"
    )
    assert not md.get("recommended_target_node")
    # 诊断一个字不丢：抹掉「根因在上游」比推荐错目标更糟
    assert "postprocess" in pause["context"], "reviewer 点名的目标必须还看得见"
    assert "postprocess" in (md.get("recommended_feedback") or "")
    # REDIRECT 仍在菜单里 —— 人可以自己指一个合法上游
    assert any("REDIRECT" in o for o in pause["offer"]["options"])
    assert pause["offer"]["recommended_choice_id"] == "revise"


def test_an_executable_recommendation_is_offered_unchanged(tmp_path):
    """对照：合法的 REDIRECT 推荐一个字不动。"""
    _state, pause = _present(tmp_path, target="experiment")
    md = pause["metadata"]
    assert md["recommended_action"] == "redirect_upstream"
    assert md["recommended_target_node"] == "experiment"


def test_a_non_redirect_recommendation_is_never_touched(tmp_path):
    """这道网只管 REDIRECT。

    PROCEED / REVISE 根本没有"上游目标"这回事，`recommended_target_node` 天然是
    空 —— 若判据忘了先问"这是不是一条 REDIRECT 推荐"，那个空值会被当成"没指名
    目标"，于是每一次 PROCEED 都被改判成 REVISE，还附一段莫名其妙的说明。
    变异 `if True` 正是这个形状，第一版测试没抓到。
    """
    _state, pause = _present(tmp_path, target="", action="proceed", verdict="approve")
    md = pause["metadata"]
    assert md["recommended_action"] == "proceed", "PROCEED 被这道网顺手改判了"
    assert "框架撤下 REDIRECT 推荐" not in (pause["context"] or "")


# ── 跑不起来的节点关不掉任何 flow（2026-09-17，护栏自己藏着一张烂名单）────────
#
# 这个谓词的第 3 档原来是一张手写回退名单 `PRODUCING_NODE_TYPES`，用于"harness
# 读不出来"时"偏保守地"答 True。名单里躺着 `analysis`（节点早已删除）、`literature`、
# `data`（两个都已改成服务），于是：
#
#     node_owes_post_node_flow("analysis") → True
#
# reviewer 点名一个**不存在的节点**，三道 redirect 闸会一致放行，直到 run_node
# 起的时候才炸。刚修完的那个病的同一个形状 —— 护栏内部藏着一张需要有人记得更新
# 的名单，而 writing 的 review_spec 今天还在教模型 redirect 到 `analysis`。

#: 历史上真的存在过、后来被删掉的节点名（memory / harness 注释里都还留着它们）。
_GHOSTS = ("analysis", "review", "survey", "planning", "exploration")


def test_the_ghosts_really_are_gone():
    """前提：这些名字确实不再是节点。塌了下面两条会静默变成空断言。"""
    alive = set(list_harnesses())
    assert not (set(_GHOSTS) & alive), f"这些又回来了？{sorted(set(_GHOSTS) & alive)}"


def test_a_node_that_cannot_run_cannot_close_a_flow():
    """run_node 起子节点前有一道 harness 存在性预检 —— harness 读不出来就跑不起来。

    跑不起来 ⇒ 既不会产出需要审查的东西，也不可能把任何 flow 关掉。两个问题在这
    一档是同一个 False；"偏保守"当初想防的"把科学节点误当服务放行"要求节点能跑。
    """
    for ghost in _GHOSTS:
        assert node_owes_post_node_flow(ghost) is False, (
            f"{ghost!r} 这个节点根本不存在，却被判成能接 REDIRECT —— "
            "三道闸会一致放行它，直到 run_node 起的时候才炸"
        )
    assert node_owes_post_node_flow("definitely_not_a_node") is False


def test_the_legal_set_has_exactly_one_source():
    """合法目标集必须**完全**由现存节点自己的声明决定，没有第二个来源。

    有第二个来源（一张名单、一份缓存、一个环境变量）就有第二份会各自演化的答案，
    而分叉不会报错 —— 只会让某一天的某个 redirect 悄悄卡死。
    """
    from core.loader import load_harness

    legal = {n for n in list_harnesses() if node_owes_post_node_flow(n)}
    declared = {
        n for n in list_harnesses()
        if not n.startswith("_") and load_harness(n).post_run_flow != "none"
    }
    assert legal == declared, (
        f"合法集与节点声明分叉了：只在合法集里={sorted(legal - declared)}，"
        f"只在声明里={sorted(declared - legal)}"
    )


def test_the_gates_reject_a_deleted_node_end_to_end(tmp_path):
    """真正要紧的那一条：三道 redirect 闸对一个已删节点必须一致说不。

    `analysis` 不是随便挑的 —— `nodes/writing/review_spec.md` 今天还在教 reviewer
    "target_node 指向实际负责出图的 `analysis` 或 `postprocess`"（issue #1061）。
    模型照做是大概率事件。
    """
    out = _recommend(tmp_path, "analysis")
    assert out["status"] == "error", "reviewer 工具放行了一个已删节点"
    assert "analysis" in out["error"]

    state = _flow_with_a_redirect_recommendation(tmp_path, "analysis")
    entry = record_decision_answer(
        state, {"type": "decision_package", "producing_run_id": "run-w-1"},
        str(_NORMAL_ACTIONS.index("redirect_upstream") + 1))
    assert entry["decision_state"] == "awaiting_human"
    assert entry.get("authorized_target_node") is None

    _s, pause = _present(tmp_path, target="analysis")
    assert pause["metadata"]["recommended_action"] != "redirect_upstream"
