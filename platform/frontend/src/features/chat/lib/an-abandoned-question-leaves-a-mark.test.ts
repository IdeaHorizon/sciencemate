import test from "node:test";
import assert from "node:assert/strict";
import { runPauseView } from "./canonical-run-activity.ts";
import type { ResearchRun } from "../../../lib/api.ts";

/**
 * 重启弄丢的那个问题，刷新之后还看得见它已经作废（#781）。
 *
 * ## 现场（课题二 c7168ec6，2026-09-03）
 *
 * 08:28 调度器发了一个四选项的 structured_question；16:23 后端被另一个会话
 * 重启，pause 随进程一起丢了。用户带 choice=option_3 + 正文发答复，SSE 流里
 * 依次来 `pause.abandoned` / `run.started` —— 这句话本身是对的，问题在于
 * **它只活在那一次 SSE 里**：刷新页面后那条问题卡还在、四个选项按钮还在、
 * 没有任何"已作废"的标记。用户看不出自己点的那个选项其实没执行。
 *
 * ## 这条判据守什么
 *
 * 作废是**这条问题的事实**，不是一次推送。事实早就落盘了：
 * `resume_stale_session` 把 `summary.pauseAbandonedAt` + `staleReason`
 * 写在那条 run 上，`run.view.phase` 随之不再是 `alive`。
 *
 * 所以读端只要不把它当成入口就行 —— `runPauseView` 只在 `phase !== "alive"`
 * 时产出**记录**（由 `PausedRecord` 渲染：没有 `onAnswer` 这个 prop，选项是
 * `<li>` 不是按钮，结构上点不动）。
 *
 * 判据落在这个纯函数上而不是 JSX 上：入口与记录曾经由调用点各判一遍，两个
 * 判据分叉时没有任何一层报错（2026-09-01 收成一处）。
 */

function run(overrides: {
  phase: string;
  waitingKind?: string;
  pause?: unknown;
}): ResearchRun {
  return {
    id: "run-1",
    summary: overrides.pause === undefined
      ? {
          pause: {
            kind: "human",
            question: "下一步选哪条路线？",
            options: [
              { id: "option_1", label: "先补数据" },
              { id: "option_3", label: "直接跑" },
            ],
          },
          pauseAbandonedAt: "2026-09-03T08:23:00Z",
          staleReason: "runtime_lost",
        }
      : overrides.pause,
    view: {
      phase: overrides.phase,
      waitingOn: overrides.waitingKind ? { kind: overrides.waitingKind } : undefined,
    },
  } as unknown as ResearchRun;
}

test("进程没了之后，那个问题变成只读记录（刷新后仍在）", () => {
  const view = runPauseView(run({ phase: "interrupted", waitingKind: "human" }));
  assert.ok(view, "作废之后问题整个消失了 —— 用户看不出自己点的选项没执行");
  assert.equal(view.pause.question, "下一步选哪条路线？");
  assert.equal(view.pause.options.length, 2, "选项要留着，只是不能点");
  // 这里**不产出**任何"能答"的信号 —— 入口归会话级的 answer.via 管。
  assert.equal("resumable" in view, false, "记录不许再带一个第二判据");
});

test("运行时还活着时这里让开 —— 能点的那张卡归会话级答复入口", () => {
  assert.equal(runPauseView(run({ phase: "alive", waitingKind: "human" })), null);
});

test("等算力不是等人，不画问题卡", () => {
  assert.equal(runPauseView(run({ phase: "interrupted", waitingKind: "compute" })), null);
});

test("没有问题本体时不画一张空卡", () => {
  assert.equal(
    runPauseView(run({ phase: "interrupted", waitingKind: "human", pause: {} })),
    null,
  );
});
