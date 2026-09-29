"use client";

import { createContext, useContext, useMemo, type ReactNode } from "react";

/**
 * "在右栏打开这个工作区文件" —— 提供给对话流深处的那些卡片。
 *
 * ## 为什么是 context 而不是把回调穿下去
 *
 * 产出文件名出现在 `WorkspaceChangeRecord` 里，而它挂在
 * `ToolGroup` → 步骤卡 → … 底下，且 `ToolGroup` 有**两个**挂载点（对话流里的
 * accessory、右栏的研究进程）。把回调按 prop 穿过去要改沿途每一层的签名，而
 * 漏传的那一层不会报错 —— 只是那里的文件名点不动，看起来像"有时候能点有时候
 * 不能"。
 *
 * 打开需要 projectId + sessionId，卡片自己两个都没有；会话页两个都有。所以
 * 由会话页在顶上装一次，底下只问"能不能打开、打开哪个路径"。
 *
 * 没有 provider 时返回 null，卡片退回成纯文本 —— 与这个功能出现之前一致。
 */

/**
 * 除了"怎么打开"，还带着"这是哪个工作区" —— 消息正文里内联的图要自己去取
 * 字节，而取字节需要 projectId + sessionId。
 *
 * 两者放同一个 context 不是图省事：它们必须指向**同一个 worktree**。分成两处
 * 各自取的话，某天其中一处改了口径（比如按"最近会话"重算），内联图和点开的
 * 文件就会来自两个不同的会话，而两边都不报错。
 */
export type WorkspaceFilesValue = {
  projectId: string;
  sessionId: string;
  openFile: (path: string) => void;
};

const WorkspaceFilesContext = createContext<WorkspaceFilesValue | null>(null);

export function WorkspaceFileOpenerProvider({
  projectId,
  sessionId,
  enabled = true,
  onOpenFile,
  children,
}: {
  projectId: string;
  sessionId: string;
  /**
   * 右栏在这个模式下存不存在。fixture/demo 模式下右栏是关掉的（那些数据不对应
   * 任何真实 worktree），所以这里必须一起关。
   *
   * 不关会得到最难查的那种状态：文件名照样长得可点，点下去 store 记下了
   * "打开哪个文件"，而承载它的右栏根本没渲染 —— 表现是**点了没反应**，
   * 而且没有任何一层报错。关掉之后一切退回纯文本，与这个功能出现之前一致。
   */
  enabled?: boolean;
  onOpenFile: (path: string) => void;
  children: ReactNode;
}) {
  // 每次渲染换一个新对象会让所有消费者跟着重渲染 —— 对话流里这样的卡片有几百张。
  const value = useMemo(
    () => (enabled ? { projectId, sessionId, openFile: onOpenFile } : null),
    [enabled, projectId, sessionId, onOpenFile],
  );
  return (
    <WorkspaceFilesContext.Provider value={value}>
      {children}
    </WorkspaceFilesContext.Provider>
  );
}

/** 完整语境（内联图要用）。没有 provider 时是 null。 */
export function useWorkspaceFiles(): WorkspaceFilesValue | null {
  return useContext(WorkspaceFilesContext);
}

/** 只要"打开"这个动作的调用方用这个（产出卡片）。 */
export function useWorkspaceFileOpener(): ((path: string) => void) | null {
  return useContext(WorkspaceFilesContext)?.openFile ?? null;
}

/**
 * 已经删掉的文件不给打开 —— 点开必然 404，而那个报错看起来像平台坏了。
 * 判据取事件里的 status，不猜路径。
 */
export function isOpenableChange(status?: string): boolean {
  return status !== "deleted";
}
