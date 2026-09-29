import { RESEARCH_TAB_ID } from "../../../stores/workspace-panel.ts";

/**
 * 当前该显示哪个 Tab。
 *
 * 单拿出来是因为它有几个只在**边界**上才走到的分支：记着的那个标签被关掉了、
 * 这个页面根本没有研究进程栏、一个标签都没有。这些状态在界面上难复现，但
 * 走错的后果是"点了没反应"或者右栏空白。
 */
export function resolveActiveTab(
  activeTabId: string,
  { hasResearch, fileIds }: { hasResearch: boolean; fileIds: readonly string[] },
): string {
  if (activeTabId === RESEARCH_TAB_ID) {
    // 研究进程不在场（文件页）时落到第一个文件，而不是显示一个空面板。
    return hasResearch ? RESEARCH_TAB_ID : (fileIds[0] ?? RESEARCH_TAB_ID);
  }
  if (fileIds.includes(activeTabId)) return activeTabId;
  // 记着的标签属于别的项目、或者已经被关掉了。
  if (hasResearch) return RESEARCH_TAB_ID;
  return fileIds[0] ?? RESEARCH_TAB_ID;
}
