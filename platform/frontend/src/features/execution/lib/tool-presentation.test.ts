import test from "node:test";
import assert from "node:assert/strict";
import {
  isVisibleResearchTool,
  researchActivityAcceptance as acceptanceIn,
  researchActivityKindForTool,
  researchToolFailurePresentation as failureIn,
  researchToolInputPreview as inputPreviewIn,
  researchToolRunningStatus as runningStatusIn,
  researchToolTitle as titleIn,
} from "./tool-presentation.ts";

// 下面的判据断言的是**英文**那一份文案，所以这里显式说英文 —— 界面默认语言
// 是中文。不写就等于把「这些句子长什么样」交给默认值，判据会跟着默认值漂。
// 中文那一份由本文件末尾「两种语言各说一遍」那条判据守。
const researchActivityAcceptance = (kind: Parameters<typeof acceptanceIn>[0]) => acceptanceIn(kind, "en");
const researchToolFailurePresentation = (
  name: Parameters<typeof failureIn>[0],
  error?: Parameters<typeof failureIn>[1],
) => failureIn(name, error, "en");
const researchToolInputPreview = (args?: Parameters<typeof inputPreviewIn>[0]) => inputPreviewIn(args, "en");
const researchToolRunningStatus = (
  name: Parameters<typeof runningStatusIn>[0],
  args?: Parameters<typeof runningStatusIn>[1],
  detail?: string,
) => runningStatusIn(name, args, detail, "en");
const researchToolTitle = (
  name: Parameters<typeof titleIn>[0],
  args?: Parameters<typeof titleIn>[1],
  detail?: string,
) => titleIn(name, args, detail, "en");

test("internal orchestration and Decision tools never become ordinary tool rows", () => {
  for (const name of [
    "write_scratchpad",
    "resolve_research_context",
    "run_node",
    "request_human_input",
    "present_decision_package",
  ]) {
    assert.equal(isVisibleResearchTool(name), false, name);
  }
  assert.equal(isVisibleResearchTool("semantic_scholar_search"), true);
  assert.equal(isVisibleResearchTool("platform_internal_transport"), false);
});

test("visible tools use a human action and attributable object", () => {
  assert.equal(
    researchToolTitle("semantic_scholar_search", { query: "agentic materials discovery" }),
    "Search literature — agentic materials discovery",
  );
  assert.equal(
    researchToolRunningStatus("read_artifact", { name: "survey-report" }),
    "Reading source material — survey-report",
  );
});

test("question, research, figure, PDF, experiment, compute, and deployment have acceptance copy", () => {
  assert.equal(researchActivityAcceptance("question").running, "Formulating an answer");
  const cases = [
    ["search_papers", "research", "Research report or source set"],
    ["render_figure", "figure", "Figure or chart"],
    ["compile_latex", "pdf", "PDF document"],
    ["run_experiment", "experiment", "Experiment results and log"],
    ["execute_python", "compute", "Computed result or analysis"],
    ["deploy_site", "deployment", "Deployment record or application link"],
  ] as const;
  for (const [tool, kind, completed] of cases) {
    assert.equal(researchActivityKindForTool(tool), kind);
    assert.equal(researchActivityAcceptance(kind).completed, completed);
    assert.match(researchActivityAcceptance(kind).recovery, /then|before/);
  }
});

test("standard tool preview excludes bulk JSON and secrets", () => {
  assert.equal(researchToolInputPreview({
    classification_json: "{\"large\":\"payload\"}",
    papers_json: "[{\"title\":\"many papers\"}]",
    token: "secret",
    name: "literature_index",
  }), "Name: literature_index");
  assert.equal(researchToolInputPreview({ classification_json: "{}", token: "secret" }), undefined);
  assert.equal(researchToolInputPreview({
    name: "ablation",
    temperature: 0.2,
    seed: 7,
    code: "bulk code hidden",
  }), "Name: ablation · Temperature: 0.2 · Seed: 7");
});

test("错误正文照原样给人看，不再按长相判成「技术细节」", () => {
  // 回归：这几句真实报错以前全被换成
  // "The tool stopped before producing a usable result."
  // —— 分别栽在 snake_case 标识符、方括号、长度三条上。
  const real = [
    "artifact 'experiment_log__Cooling_Rate_Tg' 未 freeze。先 freeze_artifact。",
    "quality_mode must be one of ['auto', 'publication', 'quick', 'standard'], got 'draft'",
    "x".repeat(400),
  ];
  for (const message of real) {
    assert.equal(researchToolFailurePresentation("save_artifact", { message }).message, message);
  }
});

test("驳回和真失败分开：判据是 harness 盖的章，不是字符串长相", () => {
  const declined = researchToolFailurePresentation("create_claim", {
    code: "rejected",
    message: "claim_type='methodological' 必须 independent_source_count ≥ 2（当前 1）。",
  });
  assert.equal(declined.loopLevel, true);
  assert.match(declined.title, /declined by the framework/);

  const crashed = researchToolFailurePresentation("run_node", {
    code: "tool_exception",
    message: "KeyError: 'turns'",
  });
  assert.equal(crashed.loopLevel, false);
  assert.equal(crashed.message, "KeyError: 'turns'");
  assert.match(crashed.title, /did not complete/);
});

test("没有正文时才兜底，而且说清楚是「没给原因」", () => {
  const blank = researchToolFailurePresentation("execute_python", { message: "" });
  assert.equal(blank.message, "The tool reported no reason for this failure.");
});

test("分类失败的专用标题只认分类工具，不按消息认领别人的失败", () => {
  assert.equal(
    researchToolFailurePresentation("classify_papers", {
      message: "papers_json 解析失败: Extra data",
    }).title,
    "Paper classification was not saved",
  );
  // 同样含 "extra data" 的 save_artifact 失败，以前会被冠上分类的标题。
  assert.notEqual(
    researchToolFailurePresentation("save_artifact", {
      message: "参数 JSON 解析失败: Extra data",
    }).title,
    "Paper classification was not saved",
  );
});

test("上游写了下一步就用它的原话，别因为提到工具名就换成通用兜底", () => {
  assert.equal(
    researchToolFailurePresentation("archive_papers", {
      message: "papers_json 解析失败",
      recovery: "retry archive_papers with fixed papers_json",
    }).recovery,
    "retry archive_papers with fixed papers_json",
  );
});

test("异常原文有界：8-11 那次 SQL 转储不能再进会话正文", () => {
  // backend/tests/test_failures_reach_the_user_as_product_copy.py 守的是
  // run 级横幅；这里守的是工具级 —— 同一条教训的另一半。
  const dump = [
    "IntegrityError: duplicate key value violates unique constraint \"uq_events_sequence\"",
    "[SQL: INSERT INTO execution_events (id, tenant_id, workspace_id, session_id) VALUES ($1, $2, $3, $4)]",
    "[parameters: ('b145e65e', 'tenant-a', 'ws-1', 'sess-9')]",
  ].join("\n");
  const shown = researchToolFailurePresentation("save_artifact", {
    code: "tool_exception",
    message: dump,
  }).message;
  assert.equal(shown.includes("INSERT INTO"), false);
  assert.equal(shown.includes("parameters:"), false);
  assert.ok(shown.startsWith("IntegrityError: duplicate key"));

  // 但短的一行异常仍然一字不差 —— 那是修 bug 的线索，正是面板存在的理由。
  assert.equal(
    researchToolFailurePresentation("run_node", {
      code: "tool_exception",
      message: "KeyError: 'turns'",
    }).message,
    "KeyError: 'turns'",
  );

  // 界只对异常设。harness 写给模型的指导一个字都不能少，多长都给。
  const guidance = "⛔ " + "先 freeze_artifact，或改用 amend_artifact。".repeat(20);
  assert.equal(
    researchToolFailurePresentation("save_artifact", { code: "rejected", message: guidance }).message,
    guidance,
  );
});

test("模型服务的故障不算我们的 bug，也不算研究出了问题", () => {
  const p = researchToolFailurePresentation("run_node", {
    code: "provider_error",
    message: "LLMHTTPError(HTTP 400) maximum context length is 262144 tokens",
  });
  assert.match(p.title, /model service/);
  // 仍然要人看（不是循环自纠），但标题不能读成"你的研究挂了"或"平台崩了"
  assert.equal(p.loopLevel, false);
  assert.equal(p.message.includes("262144"), true);
});

test("参数形状不对 = 模型自己调错了，归循环", () => {
  const p = researchToolFailurePresentation("score_hypothesis_innovation", {
    code: "rejected",
    message: "调用 'score_hypothesis_innovation' 的参数形状不对：assessments 声明是 object 的数组，但第 0 个元素是 string。",
  });
  assert.equal(p.loopLevel, true);
  assert.match(p.title, /declined by the framework/);
  assert.ok(p.message.includes("第 0 个元素是 string"));
});

test("缺工具链：标题说是这台机器缺东西，不是叫人去查稿子", () => {
  // 2026-09-09 现场：三轮 `bwrap: execvp latexmk: No such file or directory`
  // 全显示成 "Build PDF did not complete / Review the document source"，
  // 而稿子一个字都没错。标题必须先把归属说对，人才知道该去装 TeX。
  const p = researchToolFailurePresentation("compile_latex", {
    code: "toolchain_missing",
    message: "这台机器上没有 LaTeX 工具链：latexmk、tectonic 都不在 PATH 上。",
    recovery: "Linux `apt install texlive-full latexmk`，或把 tectonic 放进 PATH。",
  });
  assert.match(p.title, /missing a tool/);
  assert.doesNotMatch(p.title, /did not complete|declined/);
  // 工具自己写的下一步必须原样用上 —— 兜底那句正是把人带偏的那句。
  assert.match(p.recovery, /texlive/);
  assert.doesNotMatch(p.recovery, /Review the document source/);
  assert.equal(p.loopLevel, false, "模型重试绕不过去，不能当循环自纠藏起来");
});

test("工具没写 recovery 时才用兜底", () => {
  const p = researchToolFailurePresentation("compile_latex", {
    code: "toolchain_missing",
    message: "没有 LaTeX 工具链",
  });
  assert.match(p.recovery, /Install the missing program/);
});

test("编译器真跑了、拒绝了源码：这一类照旧让模型去改稿", () => {
  const p = researchToolFailurePresentation("compile_latex", {
    code: "rejected",
    message: "LaTeX 编译失败",
    recovery: "按 stderr_tail / log_path 里的 TeX 报错改源码，然后重新编译。",
  });
  assert.equal(p.loopLevel, true);
  assert.match(p.recovery, /改源码/);
});

test("命令失败 / 返回值不合规范各有自己的说法，不落兜底", () => {
  const cmd = researchToolFailurePresentation("compile_latex", {
    code: "command_failed",
    message: "编译器 'latexmk' 没能启动（隔离后端/沙箱问题，不是稿件问题）",
  });
  assert.match(cmd.title, /ran a command that failed/);

  const bad = researchToolFailurePresentation("some_tool", {
    code: "non_dict_result",
    message: "tool 'x' returned non-dict (str)",
  });
  assert.match(bad.title, /malformed result/);
  assert.match(bad.recovery, /defect in the tool/);
});

test("两种语言各说一遍：工具动作、验收文案、失败标题都不许只有英文", () => {
  // 这条判据守的是**中文那一份存在且真的不一样**。只断言英文的话，把中文
  // 整片删掉判据仍然全绿 —— 那正是这次返工前的状态：一个声称能切语言的开关，
  // 切过去半屏还是英文。
  assert.equal(titleIn("semantic_scholar_search", { query: "固态电解质" }, undefined, "zh"),
    "查文献 — 固态电解质");
  assert.equal(runningStatusIn("read_artifact", { name: "survey-report" }, undefined, "zh"),
    "正在读原始材料 — survey-report");
  assert.equal(acceptanceIn("pdf", "zh").completed, "PDF 文档");

  const zh = failureIn("compile_latex", { code: "toolchain_missing", message: "缺 latexmk" }, "zh");
  assert.match(zh.title, /这台机器缺了/);
  assert.match(zh.recovery, /装上缺的程序/);

  // 每一个动作在两种语言里都得有话说，不能有哪一条落回另一种语言。
  for (const tool of [
    "deploy_site", "render_figure", "compile_latex", "run_experiment",
    "semantic_scholar_search", "classify_papers", "kb_search", "read_artifact",
    "save_artifact", "run_node", "execute_python", "evidence_chain",
    "query_project_status", "list_artifacts", "decision_package",
  ]) {
    const chinese = titleIn(tool, undefined, undefined, "zh");
    const english = titleIn(tool, undefined, undefined, "en");
    assert.notEqual(chinese, english, tool);
    assert.match(chinese, /[一-鿿]/, tool);
  }
});
