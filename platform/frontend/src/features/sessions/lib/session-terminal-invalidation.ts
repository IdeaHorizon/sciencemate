import { qk } from "../../../lib/query/keys.ts";

export type QueryInvalidationTarget = {
  queryKey: readonly unknown[];
  /**
   * `false` = 按前缀失效。只有文件树用它：那棵树一层一个 query（key 末尾带
   * 层路径），逐个精确失效等于要求这里知道用户展开了哪几层。前缀仍然钉死在
   * 本 project + 本 session 上，所以"绝不冲掉别的会话"那条不变。
   */
  exact: boolean;
};

export type QueryInvalidationClient = {
  invalidateQueries: (target: QueryInvalidationTarget) => Promise<unknown> | unknown;
};

/**
 * Canonical reads that can change as a persisted Session turn reaches its SSE
 * terminal event. Keep this list explicit: a broad cache flush would hide
 * missing ownership boundaries and make unrelated project screens refetch.
 */
export function sessionTerminalInvalidationTargets(
  projectId: string,
  sessionId: string,
  runId?: string | null,
): QueryInvalidationTarget[] {
  const targets: QueryInvalidationTarget[] = [
    { queryKey: qk.session(projectId, sessionId, "api"), exact: true },
    { queryKey: qk.sessionMessages(projectId, sessionId, "api"), exact: true },
    { queryKey: qk.sessionChangeSet(projectId, sessionId), exact: true },
    { queryKey: qk.sessionProjectTreeRoot(projectId, sessionId), exact: false },
    { queryKey: qk.sessionConflicts(projectId, sessionId), exact: true },
    { queryKey: qk.sessions(projectId, "api"), exact: true },
    { queryKey: qk.artifacts(projectId), exact: true },
  ];
  if (runId) targets.push({ queryKey: qk.run(runId), exact: true });
  return targets;
}

/**
 * Run **生命周期**事件（起、暂停、续跑）之后要刷新的那一小撮。
 *
 * `executionState` 与 `title` 都挂在 session 对象上，而这个对象原本只在 run
 * 走到**终态**时才被刷新（invalidateSessionAfterTerminal）。run 开始不是终态，
 * 于是出现过这个实测形状（本机 2026-08-19，session 78e7681a）：
 *
 *   16:06:59  第一个 run 因模型 503 failed —— 终态，session 刷新一次，
 *             存下 executionState=failed，以及**标题生成之前**的那份标题
 *   16:09:24  换模型重发，新 run 起 —— 不是终态，session 不刷新
 *   之后      agent 生成了短标题写进库；hypothesis 一路在跑
 *   结果      顶栏一直挂着上一次 503 的「Failed」，标题一直是原始那段长 prompt
 *
 * 病根是两个真相源：runs 缓存跟着事件活着更新，session 缓存只在终态更新，
 * 而顶栏读的偏偏是后者。`executionState` 本来就是 run 状态的投影，两者必须同源。
 *
 * 这里**只刷 session 自己**，不带终态那套（消息/变更集/文件树/冲突/产物）——
 * run.* 事件在一轮里会来很多次，把那些一起刷会让无关面板反复重取。
 */
export function sessionLifecycleInvalidationTargets(
  projectId: string,
  sessionId: string,
): QueryInvalidationTarget[] {
  return [
    { queryKey: qk.session(projectId, sessionId, "api"), exact: true },
    { queryKey: qk.sessions(projectId, "api"), exact: true },
  ];
}

export function invalidateSessionLifecycle(
  client: QueryInvalidationClient,
  projectId: string,
  sessionId: string,
) {
  return Promise.all(
    sessionLifecycleInvalidationTargets(projectId, sessionId)
      .map((target) => client.invalidateQueries(target)),
  );
}

export function invalidateSessionAfterTerminal(
  client: QueryInvalidationClient,
  projectId: string,
  sessionId: string,
  runId?: string | null,
) {
  return Promise.all(
    sessionTerminalInvalidationTargets(projectId, sessionId, runId)
      .map((target) => client.invalidateQueries(target)),
  );
}
