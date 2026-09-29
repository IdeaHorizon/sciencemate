/**
 * 右栏检查器读哪些 owning run。
 *
 * ## 为什么不能只读 runs 列表（2026-08-17，会话 c9deb4f2）
 *
 * runs 列表查询是**进页面时拍的快照**：新会话第一轮跑着的时候，没有任何东西
 * 刷新它 —— 于是 hypothesis 在聊天列里满屏动作，右栏却写着「还没有节点跑过」。
 * header 的「Ready」误报也是同一个病根（standaloneLiveRun 读的同一份陈旧快照）。
 *
 * 「这个会话有哪些 turn」的**现场权威是消息流**：每条消息都带它所属的 owning
 * runId，本轮的 runId 在 chat.terminal 上。runs 列表只用来兜底（旧会话的消息
 * 可能没有 runId）。同时 useRunEvents 现在会在 run 生命周期事件到达时失效
 * runs 列表缓存，让快照自己追上来。
 */

type MessageLike = { runId?: string | null };
type RunLike = { id: string; parentRunId: string | null };

export function inspectorRunIds(
  messages: readonly MessageLike[],
  terminalRunId: string | null | undefined,
  runs: readonly RunLike[] | undefined,
): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  const push = (value?: string | null) => {
    const id = value?.trim();
    if (id && !seen.has(id)) {
      seen.add(id);
      ids.push(id);
    }
  };
  for (const message of messages) push(message.runId);
  push(terminalRunId);
  // 兜底放最后：消息流没认领的 owning run（旧数据、边缘路径）仍然可见。
  for (const run of runs ?? []) if (!run.parentRunId) push(run.id);
  return ids;
}
