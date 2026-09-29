"use client";
import { create } from "zustand";

/**
 * 右栏是什么、现在显示哪一个。
 *
 * ## 为什么是 store 而不是某个页面的 useState
 *
 * 右栏原来只属于会话页（`SessionWorkspace` 的 `inspectorOpen`）。但"打开一个
 * 文件看看"这件事发起自**三个不同的地方**：Project files 页（另一条路由）、
 * 会话里的产出卡片、以及右栏自己。放在任何一个页面的 state 里，另外两处就
 * 够不着 —— 于是要么各做一个面板（两份会各自演化），要么把状态一层层 prop
 * 穿过去（穿不过路由边界）。
 *
 * 打开的文件按 **project** 记：换项目时上一个项目的文件标签毫无意义，但在同
 * 一个项目里跨页面走动（文件页 ↔ 会话页）时它们应该还在。
 */

export type PanelFileTab = {
  projectId: string;
  /** 从哪个会话的工作区读 —— 树和预览必须同源，否则"树里有、点开 404"。 */
  sessionId: string;
  path: string;
};

/** 「研究进程」那一栏的固定 id。它由会话页提供内容，别的页面没有它。 */
export const RESEARCH_TAB_ID = "research";

export function fileTabId(tab: PanelFileTab): string {
  return `file:${tab.projectId}:${tab.sessionId}:${tab.path}`;
}

type WorkspacePanelStore = {
  open: boolean;
  activeTabId: string;
  files: PanelFileTab[];
  /** 打开一个文件并切过去。已经开着的不重复加，只是激活它。 */
  openFile: (tab: PanelFileTab) => void;
  closeTab: (id: string) => void;
  activate: (id: string) => void;
  /** 打开右栏并停在「研究进程」。 */
  openResearch: () => void;
  toggle: () => void;
  close: () => void;
};

export const useWorkspacePanel = create<WorkspacePanelStore>((set) => ({
  open: false,
  activeTabId: RESEARCH_TAB_ID,
  files: [],

  openFile: (tab) =>
    set((state) => {
      const id = fileTabId(tab);
      const already = state.files.some((file) => fileTabId(file) === id);
      return {
        open: true,
        activeTabId: id,
        files: already ? state.files : [...state.files, tab],
      };
    }),

  closeTab: (id) =>
    set((state) => {
      const index = state.files.findIndex((file) => fileTabId(file) === id);
      if (index < 0) return state;
      const files = state.files.filter((_, at) => at !== index);
      if (state.activeTabId !== id) return { ...state, files };
      // 关掉当前这个之后落到哪：右边的邻居，没有就左边的，都没有就回研究进程。
      // 直接回研究进程的话，连着关几个标签会在中间反复跳回去。
      const next = files[index] ?? files[index - 1];
      return {
        ...state,
        files,
        activeTabId: next ? fileTabId(next) : RESEARCH_TAB_ID,
      };
    }),

  activate: (id) => set({ open: true, activeTabId: id }),
  openResearch: () => set({ open: true, activeTabId: RESEARCH_TAB_ID }),
  toggle: () => set((state) => ({ open: !state.open })),
  close: () => set({ open: false }),
}));

/** 这个项目下开着的文件标签。跨项目的不显示（见文件头注释）。 */
export function fileTabsForProject(
  files: readonly PanelFileTab[],
  projectId: string,
): PanelFileTab[] {
  return files.filter((file) => file.projectId === projectId);
}
