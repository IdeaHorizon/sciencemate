"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { FileText, Route, X } from "lucide-react";

import {
  clampInspectorWidth,
  INSPECTOR_DEFAULT_WIDTH,
  INSPECTOR_MAX_WIDTH,
  INSPECTOR_MIN_WIDTH,
  readInspectorWidth,
  writeInspectorWidth,
} from "@/features/sessions/lib/inspector-width";
import {
  fileTabId,
  fileTabsForProject,
  RESEARCH_TAB_ID,
  useWorkspacePanel,
} from "@/stores/workspace-panel";

import { resolveActiveTab } from "../lib/panel-tabs";
import { FilePreview } from "./FilePreview";
import { useT } from "@/shared/i18n";

/**
 * 右栏 —— 主内容旁边那一栏，可拖宽、记得住宽度、内容分 Tab。
 *
 * ## 为什么它从会话页搬到了这里
 *
 * 这栏原来整个长在 `SessionWorkspace` 里，只装一样东西（研究进程），因此
 * 拖拽/夹取/持久化那一百来行也长在那里。现在打开一个文件也用这栏，而发起
 * 「打开」的地方不止会话页 —— Project files 是另一条路由。
 *
 * 两个选择：在文件页再实现一份分栏，或者把这一份提出来两边都用。前者意味着
 * 拖拽的边界条件、SSR 首帧、ResizeObserver 重夹取全部各有一份副本，而它们
 * 分叉时**两边都不会报错**（只是其中一边的右栏在窄屏上会塌）。所以提出来。
 *
 * 研究进程仍由会话页提供内容（它要会话的事件流），这里只负责"它是其中一个
 * Tab"。文件页不传 `researchTab`，那一栏就不存在 —— 不是禁用，是不在。
 */

export type WorkspacePanelHostProps = {
  projectId: string;
  /** 会话页提供；文件页没有这一栏。 */
  researchTab?: { render: () => ReactNode };
  /**
   * 关掉整栏（demo/fixture 模式）。给 false 时连分栏容器都不建，
   * 行为与这栏出现之前完全一致。
   */
  enabled?: boolean;
  /** 主内容那一列额外的 class（会话页要 `session-document-canvas` 的排版）。 */
  mainClassName?: string;
  children: ReactNode;
};

export function WorkspacePanelHost({
  projectId,
  researchTab,
  enabled = true,
  mainClassName = "",
  children,
}: WorkspacePanelHostProps) {
  const t = useT();
  const open = useWorkspacePanel((state) => state.open);
  const activeTabId = useWorkspacePanel((state) => state.activeTabId);
  const files = useWorkspacePanel((state) => state.files);
  const activate = useWorkspacePanel((state) => state.activate);
  const closeTab = useWorkspacePanel((state) => state.closeTab);
  const closePanel = useWorkspacePanel((state) => state.close);

  const projectFiles = fileTabsForProject(files, projectId);

  // 宽度。初值刻意**不**读 localStorage：服务端渲染时没有 window，读了两边
  // 首帧就不一致（React 会整棵重挂）。存下来的值在下面的 effect 里补。
  const [width, setWidth] = useState(INSPECTOR_DEFAULT_WIDTH);
  const [resizing, setResizing] = useState(false);
  // callback ref 而不是 useRef：这个容器在数据到达之前可能**不在树里**（调用
  // 方有 loading 骨架的提前 return）。useRef + 挂载 effect 读到的永远是 null，
  // 退化成按窗口宽度夹取 —— 那会把主列挤到低于说好的下限。
  const [splitEl, setSplitEl] = useState<HTMLDivElement | null>(null);
  const availableWidth = useCallback(
    () => splitEl?.clientWidth || window.innerWidth,
    [splitEl],
  );
  // useCallback 不是为了省渲染：拖拽的 effect 依赖它，每次渲染换一个新函数就会
  // 把 pointermove 监听拆了重挂 —— 拖到一半掉事件。
  const applyWidth = useCallback((next: number) => {
    const clamped = clampInspectorWidth(next, availableWidth());
    setWidth(clamped);
    writeInspectorWidth(clamped);
  }, [availableWidth]);

  useEffect(
    () => setWidth(clampInspectorWidth(readInspectorWidth(), availableWidth())),
    [availableWidth],
  );

  // 拖拽期间监听挂在 window 上，不挂在把手上：指针一旦离开把手（拖得快、或者
  // 已经拖到夹取边界不动了），挂在元素上的 move 就收不到了。
  useEffect(() => {
    if (!resizing) return;
    const onMove = (event: PointerEvent) => {
      // 右栏贴窗口右缘：这一趟想要的宽度 = 右缘减去指针位置。
      applyWidth(window.innerWidth - event.clientX);
    };
    const stop = () => setResizing(false);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
    // pointercancel 也要收：系统手势 / 触控笔抬起只发 cancel 不发 up，
    // 漏了它就会一直停在"正在拖"的状态。
    window.addEventListener("pointercancel", stop);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
    };
  }, [resizing, applyWidth]);

  // 容器一有宽度就把当前值夹回合法区间 —— 挂载、开合、窗口缩放、侧边栏折叠，
  // 全是同一件事（"能分的地方变了"），所以只用一个机制。
  //
  // 刻意只 setState 不落盘：容器临时变窄而被夹小的宽度不是用户的选择，写进
  // localStorage 会把他调好的宽度永久改掉。
  useEffect(() => {
    if (!splitEl || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setWidth(
      (current) => clampInspectorWidth(current, splitEl.clientWidth || window.innerWidth),
    ));
    observer.observe(splitEl);
    return () => observer.disconnect();
  }, [splitEl]);

  if (!enabled) {
    return <div className={mainClassName}>{children}</div>;
  }

  // 「研究进程」不在场时（文件页），落到第一个文件标签；一个都没有就空着。
  const resolvedTabId = resolveActiveTab(activeTabId, {
    hasResearch: Boolean(researchTab),
    fileIds: projectFiles.map(fileTabId),
  });
  const activeFile = projectFiles.find((file) => fileTabId(file) === resolvedTabId);
  const showPanel = open && (Boolean(researchTab) || projectFiles.length > 0);

  return (
    <div
      ref={setSplitEl}
      className={`session-canvas-split${showPanel ? " has-inspector" : ""}`}
    >
      <div className={`workspace-split-main ${mainClassName}`.trim()}>{children}</div>
      {showPanel && (
        <>
          {/* 拖拽把手。`separator` 而不是 button：它没有"按下去会发生什么"，
              它是两栏之间的那条边。键盘也能调 —— 拖拽是唯一入口的话，用不了
              鼠标的人就只能接受那个写死的宽度。 */}
          <div
            className={`session-inspector-resizer${resizing ? " is-dragging" : ""}`}
            role="separator"
            aria-orientation="vertical"
            aria-label={t({ zh: "调整右栏宽度", en: "Resize the side panel" })}
            aria-valuenow={width}
            aria-valuemin={INSPECTOR_MIN_WIDTH}
            aria-valuemax={INSPECTOR_MAX_WIDTH}
            tabIndex={0}
            onPointerDown={(event) => {
              event.preventDefault();
              // 指针捕获：拖快了指针会跑到 iframe / 别的元素上去，没有捕获就会
              // 在半路丢掉 move 事件，表现为"拖着拖着栏就不跟手了"。
              event.currentTarget.setPointerCapture(event.pointerId);
              setResizing(true);
            }}
            onKeyDown={(event) => {
              const step = event.shiftKey ? 64 : 16;
              // 右栏在右边：往左拖是变宽，所以左箭头加宽度。
              const delta = event.key === "ArrowLeft" ? step
                : event.key === "ArrowRight" ? -step
                : 0;
              if (!delta) return;
              event.preventDefault();
              applyWidth(width + delta);
            }}
          />
          <aside
            className="session-inspector"
            style={{ flexBasis: `${width}px` }}
            aria-label={t({ zh: "右栏", en: "Side panel" })}
          >
            <header className="workspace-panel-tabbar">
              <div className="workspace-panel-tabs" role="tablist">
                {researchTab && (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={resolvedTabId === RESEARCH_TAB_ID}
                    className={resolvedTabId === RESEARCH_TAB_ID ? "is-active" : ""}
                    onClick={() => activate(RESEARCH_TAB_ID)}
                  >
                    <Route size={12} />{t({ zh: "研究进程", en: "Research progress" })}</button>
                )}
                {projectFiles.map((file) => {
                  const id = fileTabId(file);
                  const name = file.path.split("/").pop() || file.path;
                  return (
                    <span
                      key={id}
                      className={`workspace-panel-tab${resolvedTabId === id ? " is-active" : ""}`}
                    >
                      <button
                        type="button"
                        role="tab"
                        aria-selected={resolvedTabId === id}
                        onClick={() => activate(id)}
                        title={file.path}
                      >
                        <FileText size={12} /> {name}
                      </button>
                      {/* 关掉一个标签的按钮独立于切换它的按钮：套在一起的话
                          点关闭会先切过去再关，中间闪一下别的内容。 */}
                      <button
                        type="button"
                        className="workspace-panel-tab-close"
                        onClick={() => closeTab(id)}
                        aria-label={t({ zh: `关闭 ${name}`, en: `Close ${name}` })}
                      >
                        <X size={11} />
                      </button>
                    </span>
                  );
                })}
              </div>
              <button
                type="button"
                className="workspace-panel-collapse"
                onClick={closePanel}
                aria-label={t({ zh: "收起右栏", en: "Collapse the side panel" })}
              >
                <X size={14} />
              </button>
            </header>
            <div className="workspace-panel-body">
              {activeFile
                ? (
                  <FilePreview
                    // key 让换文件时组件重建：不重建的话上一份 blob 的 object
                    // URL 还挂在 img 上，切过去会先闪一眼旧图。
                    key={fileTabId(activeFile)}
                    projectId={activeFile.projectId}
                    sessionId={activeFile.sessionId}
                    path={activeFile.path}
                  />
                )
                : researchTab?.render()}
            </div>
          </aside>
        </>
      )}
    </div>
  );
}
