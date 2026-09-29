import test from "node:test";
import assert from "node:assert/strict";
import type { RunDetailResponse } from "../../../lib/api.ts";
import { canonicalRunAttention, canonicalRunPause } from "./canonical-run-activity.ts";

const detail = (overrides: Record<string, unknown> = {}): RunDetailResponse => ({
  run: {
    id: "run-a",
    tenantId: "tenant-a",
    workspaceId: "workspace-a",
    projectId: "project-a",
    sessionId: "session-a",
    parentRunId: null,
    nodeType: "chat",
    status: "completed",
    usage: {
      promptTokens: 10,
      completionTokens: 5,
      totalTokens: 15,
      cost: null,
      currency: null,
      coverage: "partial" as const,
    },
    retryCount: 0,
    createdAt: "2026-08-04T00:00:00Z",
    updatedAt: "2026-08-04T00:00:01Z",
    startedAt: "2026-08-04T00:00:00Z",
    endedAt: "2026-08-04T00:00:01Z",
    summary: null,
  view: { phase: "ended" as const, waitingOn: null, outcome: "ok" as const, error: null,
    canStop: false, label: "Completed", runId: "run-a", since: null },
  },
  attempts: [],
  eventCount: 4,
  ...overrides,
} as RunDetailResponse);

/**
 * 状态与 view 是**同一个事实**的两面（后端由同一个 builder 出）。测试里也只
 * 允许一处设置，否则 fixture 自己就能造出一个后端造不出来的自相矛盾局面 ——
 * 那样测出来的绿色不指向任何真实行为。
 */
function setStatus(d: RunDetailResponse, status: string) {
  d.run.status = status;
  const map: Record<string, Partial<RunDetailResponse["run"]["view"]>> = {
    completed:              { phase: "ended", outcome: "ok", waitingOn: null },
    completed_with_warning: { phase: "ended", outcome: "ok_with_warning", waitingOn: null },
    incomplete:             { phase: "ended", outcome: "incomplete", waitingOn: null },
    failed:                 { phase: "ended", outcome: "failed", waitingOn: null },
    cancelled:              { phase: "ended", outcome: "cancelled", waitingOn: null },
    stale_unknown:          { phase: "interrupted", outcome: null, waitingOn: null },
    running:                { phase: "alive", outcome: null, waitingOn: null },
    waiting_human:          { phase: "alive", outcome: null, waitingOn: { kind: "human" } },
    waiting_permission:     { phase: "alive", outcome: null, waitingOn: { kind: "permission" } },
  };
  d.run.view = { ...d.run.view, ...(map[status] ?? {}) } as RunDetailResponse["run"]["view"];
}

test("被打断的一轮是「接着跑」，不是「失败」", () => {
  // wangd 2026-08-18：「失败是啥？为啥会有'失败'这么一个状态？」
  // 逐条过那张故障表：除了输入过大/凭据这类要人动手的，其余用户什么都
  // 不用做 —— 下一条消息就从断点接着跑。把它说成"研究停止了"是在为
  // 平台自己的抖动吓用户。
  const stale = detail();
  setStatus(stale, "stale_unknown");
  // 后端的 view 把两件事分开答：问题还在（waitingOn），但运行时没了（phase）。
  stale.run.view = { ...stale.run.view, waitingOn: { kind: "permission" } };
  const staleAttention = canonicalRunAttention(stale);
  assert.equal(staleAttention?.tone, "continue");
  assert.match(staleAttention?.recovery ?? "", /接着跑/);
  assert.doesNotMatch(staleAttention?.title ?? "", /失败|stopped/i);
  assert.equal(JSON.stringify(staleAttention).includes("stale_unknown"), false);

  const failed = detail();
  setStatus(failed, "failed");
  const failure = canonicalRunAttention(failed, {
    title: "Paper classification was not saved",
    message: "The generated classification could not be read.",
    recovery: "Ask the agent to regenerate and save the classification.",
  });
  assert.equal(failure?.title, "Paper classification was not saved");
  assert.match(failure?.recovery ?? "", /regenerate/);
});

test("a persisted Run failure takes precedence over an earlier tool failure", () => {
  const failed = detail();
  setStatus(failed, "failed");
  failed.run.summary = {
    title: "Execution failed",
    failure: {
      code: "execution_failed",
      message: "Execution failed before an agent response was produced: Harness session operation timed out",
    },
  };
  const attention = canonicalRunAttention(failed, {
    title: "Search literature did not complete",
    message: "Semantic Scholar HTTP 429",
    recovery: "Adjust the source access.",
  });
  // 这条测试的本意（也是它的标题）：**run 级失败盖过工具级失败**，且工具那边的
  // 细节不泄露到这里来。这两点原样保留。
  assert.equal(JSON.stringify(attention).includes("Semantic Scholar"), false);
  assert.ok(attention?.title);

  // 原来这里还断言 `/former platform time limit/` —— 那是前端一条正则
  // if-链在认领故障：`/Harness session operation timed out/i.test(message)`。
  // 那条链已经删掉了（文案表搬到 `app/services/run_failures.py`，那是唯一
  // 握着异常对象的地方），因为它的默认分支是"认不得就把 message 原样显示"，
  // 而 2026-08-11 会话页上整条 SQLAlchemy INSERT 就是这么出来的。
  //
  // 代价是：**这种老格式记录**（只有一个字符串 message、没有结构）拿到的是
  // 通用文案。信息没丢 —— 原文进了 detail，用户展开就能看到。
  assert.match(attention?.detail ?? "", /Harness session operation timed out/);
  assert.equal(
    (attention?.message ?? "").includes("Harness session operation timed out"),
    false,
    "老记录里的原文不该出现在正文",
  );
});

test("还活着、还在等的那一个不由运行记录来画 —— 它归会话级的答复入口", () => {
  // 2026-09-01：可点的那张卡全前端只构造一次（SessionWorkspace），由后端的
  // `answer.via === "pause"` 唯一决定。运行记录这条路**只产出只读记录**，
  // 而且只在运行时已经没了的时候。
  //
  // 两个渲染点就要有一个静音开关（当年叫 hidePausePrompt），而静音开关判错
  // 一次，人就会看见「请回答」却没有可答的东西。
  const paused = detail();
  setStatus(paused, "waiting_human");
  paused.run.summary = {
    pause: {
      question: "Which system should the experiment use?",
      context: "No Gromacs executable is available on the current node.",
      asking_node_type: "experiment",
      options: ["Use the lab cluster", "Install locally"],
    },
    resumable: true,
  };
  assert.equal(canonicalRunPause(paused), null, "还活着的那一个不归这条路画");

  // 运行时没了 → 它变成一条**记录**，这条路才接手（下一个用例是真现场）。
  setStatus(paused, "stale_unknown");
  paused.run.view = { ...paused.run.view, waitingOn: { kind: "human" } };
  const record = canonicalRunPause(paused);
  assert.equal(record?.pause.askingNodeType, "experiment");
  assert.equal(record?.pause.options.length, 2);

  setStatus(paused, "completed");
  assert.equal(canonicalRunPause(paused), null);
});

test("等授权的时候被服务重启掐掉：问题照样呈现，但只读", () => {
  // 取自 node20 真实 payload（run_93373df4f2c54d76a5c4c5035c2d4f10）：
  // 执行进程被 app_server_restart 掐掉，run 转 stale_unknown，而
  // summary.pause 一字没少。旧判据只看 `status`，于是这条"是否批准提交高危
  // 作业"在界面上彻底消失——用户只在记录里看到问句，底下没有任何可点的东西，
  // 也没有一句话说明它已经问不成了。
  const stale = detail();
  setStatus(stale, "stale_unknown");
  // 后端的 view 把两件事分开答：问题还在（waitingOn），但运行时没了（phase）。
  stale.run.view = { ...stale.run.view, waitingOn: { kind: "permission" } };
  stale.run.summary = {
    staleFromStatus: "waiting_permission",
    staleReason: "app_server_restart",
    resumable: false,
    pause: {
      question: "⚠️ 检测到高危操作（真实外部作业提交），是否批准执行？",
      context: "工具：submit_job\n命中类别：真实外部作业提交",
      asking_node_type: "project_chat",
      options: ["批准执行", "拒绝"],
      metadata: { tool: "submit_job", type: "highrisk_confirm" },
    },
  };
  const shown = canonicalRunPause(stale);
  assert.ok(shown, "run 转 stale 之后待答问题不该消失");
  assert.equal(shown?.pause.options.length, 2);
  assert.equal(shown?.pause.kind, "permission");

  // 「进程已经没了，不能让人以为点了会有用」这条约束还在，但它不再靠一个
  // `resumable` 布尔表达 —— 这条路产出的类型里根本没有回答入口，渲染它的
  // `PausedRecord` 连 `onAnswer` 这个 prop 都没有。布尔判错是一次静默死锁，
  // 而"没有那个 prop"判不错。
  assert.equal(Object.keys(shown ?? {}).includes("resumable"), false);

  // 后端偶尔会把 resumable 留在 true（写下那一刻它确实可续），run 事后转
  // stale 时不回头改它。它现在对呈现没有任何影响。
  stale.run.summary = { ...(stale.run.summary as Record<string, unknown>), resumable: true };
  assert.ok(canonicalRunPause(stale), "账面上那个 resumable 不参与判断");

  // 从来没在等过的终态 run 不受影响。
  const done = detail();
  setStatus(done, "stale_unknown");
  done.run.summary = { staleFromStatus: "running", pause: { question: "x" } };
  assert.equal(canonicalRunPause(done), null);
});

test("前端的猜测不许盖住后端记录的原因", () => {
  // 2026-08-12：后端能分辨具体原因，但前端 `stale_unknown` 那段写死的文案
  // 排在读取持久化 failure 之前，把分辨结果整个盖掉了。
  //
  // 2026-08-18 更新：平台重启那一类现在**什么都不显示**（平台自己接着跑，
  // 没有要告诉用户的事），所以这条改用另一个后端记录的原因来守同一个不变量。
  const interrupted = detail();
  setStatus(interrupted, "stale_unknown");
  interrupted.run.summary = {
    failure: {
      code: "harness_process_exited",
      title: "这一轮的执行进程中途退出了",
      body: "已经记录下来的工作都在。",
      recovery: "发下一条消息就会从断点接着跑。",
      reference: "run_x",
      retryable: true,
    },
  };
  const attention = canonicalRunAttention(interrupted);
  assert.match(attention?.title ?? "", /执行进程中途退出/);
  assert.equal((attention?.title ?? "").includes("没有收到完成记录"), false,
    "前端的通用兜底盖住了后端记录的具体原因");
  assert.equal(attention?.tone, "continue");
});

test("a stale run with no recorded reason still says something", () => {
  // 后端没记原因时（老数据、或真的不知道），按状态给的通用文案要留着。
  const unknown = detail();
  setStatus(unknown, "stale_unknown");
  unknown.run.summary = {};
  const attention = canonicalRunAttention(unknown);
  assert.match(attention?.title ?? "", /这一轮没跑完/);
  assert.equal(attention?.tone, "continue");
});

test("重发一定还是同样结果的那类才要人动手（retryable === false）", () => {
  // 判据不是新造的：后端 run_failures 早就有 retryable 三态，只是呈现层
  // 从来没读它。false = 重发无用 → 要动手；其余（含缺失）一律"接着跑"。
  const blocked = detail();
  setStatus(blocked, "failed");
  blocked.run.summary = {
    failure: {
      title: "这次输入超出了模型窗口",
      body: "整段内容一次放不进去。",
      recovery: "把它拆短，或作为附件让 agent 分段读。",
      retryable: false,
    },
  };
  assert.equal(canonicalRunAttention(blocked)?.tone, "action");

  const transient = detail();
  setStatus(transient, "failed");
  transient.run.summary = {
    failure: { title: "平台内部错误", body: "记录这次运行时出错。", retryable: true },
  };
  assert.equal(canonicalRunAttention(transient)?.tone, "continue");

  const unknownRetryable = detail();
  setStatus(unknownRetryable, "failed");
  unknownRetryable.run.summary = {
    failure: { title: "老记录", body: "没写 retryable。" },
  };
  assert.equal(
    canonicalRunAttention(unknownRetryable)?.tone, "continue",
    "不确定时按可续处理 —— 说错的代价是让人多发一条，反过来是把人吓停在能继续的地方",
  );
});

test("平台重启打断、又没自愈：显示'被重启打断/从断点接着跑'，不掉进通用兜底", () => {
  // 历史：这条曾断言"什么都不显示" —— 前提是 startup_resume 让平台自动续跑。
  // 那个自动续跑被删了（伪造用户消息，wangd 反对）。前提没了，断言就反了：
  //
  //   - continuous 档会自愈 → run 转回 running，`status` 不是 stale_unknown，
  //     这个函数提前返回、什么都不显示（那条路径由别的测试覆盖）。
  //   - 没自愈的（assisted / 自愈失败）停在 stale_unknown，就**必须**显示那条
  //     早已写好的文案，而不是"这一轮没跑完 / Status unknown"（wangd 2026-08-24：
  //     「最起码你得显示正确的报错吧」）。
  const restarted = detail();
  setStatus(restarted, "stale_unknown");
  restarted.run.summary = {
    failure: {
      code: "app_server_restarted",
      title: "这一轮被平台重启打断了",
      body: "研究本身没有失败 —— 重启前记录的一切都在。",
      recovery: "发下一条消息就会从断点接着跑：同一个节点接上上次的位置，不会从头再来。",
      retryable: true,
    },
  };
  const attention = canonicalRunAttention(restarted);
  assert.ok(attention, "被重启打断且没自愈的 run 必须有可见提示");
  assert.equal(attention?.title, "这一轮被平台重启打断了");
  assert.equal(attention?.tone, "continue", "重启可续 → 接着跑，不是要用户动手");
  // 绝不能掉进通用兜底那句 —— 那正是用户投诉的误导文案。
  assert.notEqual(attention?.message, "执行进程在写下终态之前退出了 —— 已经做完的部分都还在。");
});
