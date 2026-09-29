"""e2e5b blocked 时点名的四个问题里，属于我们层的三个（E2E-5b 16 次重试实测）。

它的原话："每次死在一个不同的框架/集群问题上（judge 故障、40KB 截断、
provenance 陷阱、repeated_failure 门禁）……需要人/framework-owner 介入。"
它说得对 —— 四个全查实了：

  ① judge 故障：judge_invalid_json ×3，fail-closed 把 check 名写进
     failed_quality_checks，**跟真失败长得一模一样** → 污染卡死统计
  ② 40KB 截断：save_artifact 的 content 是工具参数 → 产物大小被 token 上限
     物理封顶（#184 同类；reviewer 有 critique_builder，writing 没有对应物）
  ③ provenance 陷阱：被拦的 fig_cost_success.tex 是**本项目更早 run 自己产的**，
     框架明知它是谁产的却要 agent 手工声明，忘了就 run 报废
  ④ repeated_failure 门禁：①的污染凑够阈值 → 拦了 writing 7 次派发

④由①的修复自动解决（门禁读的就是 failure_signals）。
"""
from __future__ import annotations

import asyncio
import json

from core import data_provenance as dp
from core import run_history
from core.bootstrap import bootstrap
from core.state import State

bootstrap()


# ①+④（judge 故障的豁免逻辑）已随 QC 层删除（2026-08-22）：
# 判官不存在了，「judge 自身故障被算成节点连败」这一冤案类别按构造灭绝。


# ② dispatch_pollution 回放已随 QC 层删除（2026-08-22）：judge 信号不再进
# 卡死统计（信号面只剩 missing:*），该污染路径按构造消失。真实重复失败的
# 识别由 test_e2e2_fixes 的熔断器套件（missing:* 信号）覆盖。


# ── ③ 同项目前序 run 的产物自动放行 ─────────────────────────────────────────

def _touch_old(path, content="x"):
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    old = 1785604768.0                      # 比任何新 run 都老
    os.utime(path, (old, old))
    return path


def test_same_project_prior_run_file_is_auto_provenanced(tmp_path):
    """E2E-5b 原形回放：本项目更早 run 产的 fig 文件不再要求手工声明。"""
    base = tmp_path / "runs"
    prior = base / "1785604768-d342eb" / "sci_manuscript" / "figure_src"
    fig = _touch_old(prior / "fig_cost_success.tex", "\\begin{tikzpicture}")

    st = State.new(node_type="writing", base_dir=base, project_id="p_e2e5b")
    dp.mark_run_start(st)          # 生产路径由 executor 打，测试自己打
    dp.record_tool_paths(st, "read_file", {"path": str(fig)})
    assert dp.stale_external_inputs(st) == [], \
        "框架明知这文件是本项目 run 1785604768 产的，不该要 agent 声明"
    auto = dp.auto_provenance(st)
    assert auto and auto[0]["source_run_id"] == "1785604768-d342eb", \
        "但 provenance 要自动出具 —— 审计时'这图哪来的'仍答得上"


def test_cross_project_file_still_requires_declaration(tmp_path):
    """别的项目 / 平台数据照旧要声明 —— E2E-3 静默复用那次是真该拦的。"""
    foreign = _touch_old(tmp_path / "other_project" / "episodes.json", "{}")
    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_strict")
    dp.mark_run_start(st)
    dp.record_tool_paths(st, "read_file", {"path": str(foreign)})
    stale = dp.stale_external_inputs(st)
    assert len(stale) == 1 and stale[0]["path"] == str(foreign), \
        "跨项目文件框架无从知道来历，必须声明"


def test_own_run_files_were_never_external(tmp_path):
    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_own")
    own = _touch_old(st.root / "workspace" / "draft.md")
    dp.mark_run_start(st)
    dp.record_tool_paths(st, "read_file", {"path": str(own)})
    assert dp.stale_external_inputs(st) == []
    assert dp.auto_provenance(st) == []      # 自己 run 的文件连自动记录都不需要


# ── ② save_artifact 内容按引用 ──────────────────────────────────────────────

def _save(st, **kw):
    from core.tool_registry import execute
    return asyncio.run(execute("save_artifact", st, **kw))


def test_large_artifact_by_reference(tmp_path):
    """40KB 手稿走文件引用 —— 内容一个字节不经过模型输出通道。"""
    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_ref")
    big = "# Manuscript\n" + ("x" * 40_000)
    f = st.root / "workspace" / "manuscript.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(big, encoding="utf-8")

    r = _save(st, artifact_type="manuscript", name="paper",
              content_from_file=str(f))
    assert r.get("status") == "success", r
    arts = st.list_artifacts("manuscript")
    rec = st.read_artifact(arts[-1]["id"])
    assert rec["content"] == big, "内容必须与磁盘文件逐字节一致"


def test_relative_path_resolves_inside_workspace(tmp_path):
    """相对路径落在 `working_directory()` 里 —— 那也是产物漏斗的锚点。

    这里不再写死 `workspace/`：锚点由 core.paths 决定，测试写死就会在锚点变了
    之后跟真实行为脱钩（而且"全绿"）。
    """
    from core.project_workspace import working_directory

    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_rel")
    f = working_directory(st) / "out.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("body", encoding="utf-8")
    r = _save(st, artifact_type="scratchpad", name="n", content_from_file="out.md")
    assert r.get("status") == "success", r


def test_by_reference_cannot_escape_the_project(tmp_path):
    """不许变成任意文件读取。"""
    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_jail")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    r = _save(st, artifact_type="scratchpad", name="n",
              content_from_file=str(outside))
    assert r.get("status") == "error" and "越界" in r.get("error", "")


def test_exclusivity_and_empty_guards(tmp_path):
    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_guard")
    f = st.root / "workspace" / "a.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x", encoding="utf-8")
    r = _save(st, artifact_type="t", name="n", content="inline",
              content_from_file=str(f))
    assert r.get("status") == "error" and "只能给一个" in r.get("error", "")
    r = _save(st, artifact_type="t", name="n")
    assert r.get("status") == "error" and "必须给一个" in r.get("error", "")
    # 空文件是合法内容（判决拆除第三波：builtin:145 删）——「上一步没写成」是判断，
    # 归模型；拼装清单如实记下来源。墙加回去这条转红。
    empty = st.root / "workspace" / "empty.md"
    empty.write_text("  \n", encoding="utf-8")
    r = _save(st, artifact_type="t", name="n", content_from_file=str(empty))
    assert r.get("status") == "success", r
    rec = st.read_artifact(r["id"])
    assert rec["metadata"]["content_assembled_from"] == [str(empty.resolve())]


def test_schema_advertises_the_new_path():
    """参数必须进 schema —— 模型传不进没广告的参数（本周已栽过一次）。"""
    from core.tool_registry import _REGISTRY
    spec = _REGISTRY.tools["save_artifact"]
    props = spec.parameters_schema["properties"]
    assert "content_from_file" in props
    assert "content" not in spec.parameters_schema["required"], \
        "content 必须降为可选，否则按引用的调用会被 schema 拒掉"
    assert "write_file" in props["content_from_file"]["description"], \
        "要讲清 write_file 是覆盖语义、大内容分成多个文件再列进来"
    # 2026-08-22：列表形态也必须广告出去 —— schema 只写 string 的话，模型
    # 传数组会被 provider 侧 schema 校验拒掉，能力等于不存在。
    assert props["content_from_file"]["type"] == ["string", "array"]


# ── ②b save_artifact 多文件装配（2026-08-22）────────────────────────────────
#
# writing 实测：manuscript 全源快照大于单轮输出预算（12k tokens），而
# write_file 是整文件覆盖语义 —— 模型在一个不存在的 append 原语上探测了十几
# 分钟。类级出口 = content_from_file 收**有序列表**，框架机械拼接，模型不
# 搬运字节。


def test_assembly_from_ordered_file_list(tmp_path):
    """拼接顺序 = 列表序（不是字典序），拼装清单进 metadata（框架观测）。"""
    from pathlib import Path as _P

    from core.project_workspace import working_directory

    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_asm")
    wd = working_directory(st)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "p1.tex").write_text("PART_A\n", encoding="utf-8")
    (wd / "p2.tex").write_text("PART_B\n", encoding="utf-8")
    (wd / "p3.tex").write_text("PART_C\n", encoding="utf-8")
    r = _save(st, artifact_type="manuscript", name="paper",
              content_from_file=["p2.tex", "p1.tex", "p3.tex"])
    assert r.get("status") == "success", r
    rec = st.read_artifact(st.list_artifacts("manuscript")[-1]["id"])
    assert rec["content"] == "PART_B\nPART_A\nPART_C\n"
    manifest = (rec.get("metadata") or {}).get("content_assembled_from")
    assert manifest and [_P(x).name for x in manifest] == \
        ["p2.tex", "p1.tex", "p3.tex"], manifest


def test_assembly_rejects_if_any_element_escapes(tmp_path):
    """列表不能变成批量任意文件读取：一个元素越界 = 整个调用失败，零落盘。"""
    from core.project_workspace import working_directory

    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_asm_jail")
    wd = working_directory(st)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "ok.md").write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    r = _save(st, artifact_type="scratchpad", name="n",
              content_from_file=["ok.md", str(outside)])
    assert r.get("status") == "error" and "越界" in r.get("error", "")
    assert not st.list_artifacts("scratchpad")


def test_assembly_keeps_the_empty_part_and_names_it_in_the_manifest(tmp_path):
    """空文件是合法内容（判决拆除第三波：builtin:145 删）——「上一步没写成」是判断，
    归模型；拼装清单如实列出每一段来源，读者看得见哪一段是空的。墙加回去这条转红。"""
    from core.project_workspace import working_directory

    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_asm_hole")
    wd = working_directory(st)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "p1.md").write_text("body\n", encoding="utf-8")
    (wd / "hole.md").write_text("  \n", encoding="utf-8")
    r = _save(st, artifact_type="scratchpad", name="n",
              content_from_file=["p1.md", "hole.md"])
    assert r.get("status") == "success", r
    rec = st.read_artifact(r["id"])
    assert rec["content"] == "body\n  \n"
    from pathlib import Path as _P
    assert [_P(x).name for x in rec["metadata"]["content_assembled_from"]] == ["p1.md", "hole.md"]


def test_citation_gate_covers_by_reference_content(tmp_path):
    """引用闸查的必须是**真正落盘的内容**。

    此前引用诚信检查跑在 content_from_file 读盘之前 —— 走文件引用的内容
    （恰恰是最长、最需要查的 manuscript）从来没被闸看过一眼。机制存在但
    没接到路径。
    """
    from core.project_workspace import working_directory

    st = State.new(node_type="writing", base_dir=tmp_path / "runs",
                   project_id="p_asm_cite")
    wd = working_directory(st)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "m.tex").write_text(
        "结论依据 claim_deadbeef01 成立。\n", encoding="utf-8")
    r = _save(st, artifact_type="manuscript", name="paper",
              content_from_file=["m.tex"])
    # 判决拆除第三波（builtin:237 降格）：检测照跑在真正落盘的内容上，但不拒写 ——
    # 幽灵清单作为框架观测进 metadata。检测没接到文件引用这条路时，这里转红。
    assert r.get("status") == "success", r
    rec = st.read_artifact(r["id"])
    assert rec["metadata"]["phantom_claim_ids"] == ["claim_deadbeef01"]
