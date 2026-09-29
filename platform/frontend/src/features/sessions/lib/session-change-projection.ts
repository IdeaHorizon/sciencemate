import type { SessionMergeConflict } from "../types";

export function openSessionConflicts(conflicts: readonly SessionMergeConflict[]) {
  return conflicts.filter((conflict) => conflict.status === "open");
}

/** 冲突的路径单独有一块卡片，这里就不再重复列一遍。 */
export function visibleSessionChangePaths(
  paths: readonly string[],
  conflicts: readonly SessionMergeConflict[],
) {
  const conflicted = new Set(openSessionConflicts(conflicts).map((conflict) => conflict.resourceKey));
  return paths.filter((path) => !conflicted.has(path));
}

export function sessionChangeDetailExpanded(requested: boolean, openConflictCount: number) {
  return openConflictCount > 0 || requested;
}

/**
 * 「这个会话有多少改动还没发布」—— **只有一个答案**。
 *
 * 两个数据源各自只说了一半：
 *   · `session.unpublishedChangeCount` 是会话行上的计数（研究产出以节点
 *     checkpoint 直接提交进 session 分支，不经过它，所以常年是 0）
 *   · `changeSet.filesChanged` 数的是工作区相对 base 的真实 Git diff
 *
 * 研究产出是以节点 checkpoint 的形式直接提交进 session 分支的，不产生
 * ChangeItem —— 于是前者是 0、后者是 26（2026-08-17 node20 实测：英国饮食
 * 那个会话 git 侧 34 files/+1122，而 session 字段报 0）。面板取了两者的
 * max（显示 26），composer 的 chip 直接读 session 字段（显示"无未发布改动"
 * 且不挂角标）。同一个问题两个答案，且**说"没有"的那个更容易被相信** ——
 * 用户据此认为没东西可发布，26 个文件的研究产出就可能被当成空会话丢掉。
 *
 * 所以把公式收成一处，两边都问它。
 */
export function sessionUnpublishedCount(
  changeSet: { changedPaths?: readonly unknown[]; filesChanged?: number } | null | undefined,
  fallbackCount: number,
): number {
  return Math.max(changeSet?.changedPaths?.length ?? fallbackCount, changeSet?.filesChanged ?? 0);
}
