"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import { API_BASE_URL, api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { ToolGroup } from "@/features/chat/components/CanonicalRunActivity";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { createExecutionReadClient } from "@/features/execution/lib/execution-read-client";
import type { ExecutionEvent } from "@/features/execution/lib/execution-event";
import {
  mergeCanonicalRunEvents,
  readCanonicalRunEvents,
} from "@/features/execution/lib/run-event-reader";
import {
  projectRunActivity,
  type RunActivityStep,
} from "@/features/execution/lib/run-activity-detail";
import { parseRunLineage, projectResearchMap } from "@/features/execution/lib/research-map";
import { ResearchMap } from "@/features/execution/components/ResearchMap";
import { useT, useLanguage } from "@/shared/i18n";

//: 流程图收起状态记在本地 —— 收起是一个持续的意图，不是一次性动作。
const MAP_COLLAPSED_KEY = "session-inspector-map-collapsed";

/**
 * 右栏检查器：上半是研究地图，下半是每个节点 run 的明细，按发生顺序排列、
 * 默认折叠（wangd 2026-08-17：「这样的话能非常清晰的看到整体的流程是怎么样
 * 的」—— 它取代了地图上的足迹线：足迹一长就没意义，折叠列表不会）。
 *
 * 数据与对话列的 accessory 共用同一批 React Query key（qk.runEvents）——
 * 同一个真相源，两个视口；accessory 的 SSE 更新会直接反映到这里。
 */

export function SessionInspectorPanel({
  sessionId, rootRunIds, interruptedRunIds, focus,
}: {
  sessionId: string;
  /** 会话的 owning run id 列表，按时间序（消息流权威，见 inspectorRunIds）。 */
  rootRunIds: readonly string[];
  /**
   * owning run 的当前状态。用来判"这条子 run 是被打断了还是真在跑"——
   * 进程被杀的 run 没有任何终态事件，只靠事件流永远显示"进行中"
   * （2026-08-18 实测：一条 1 action 的 hypothesis 尸体一直挂着转圈）。
   */
  /** 哪些 owning run 没走到终点就断了 —— 一个集合，不是一份状态名单。 */
  interruptedRunIds?: ReadonlySet<string>;
  /** 左栏状态卡点过来时要定位的节点；nonce 让同一节点连点两次也生效。 */
  focus?: { nodeType: string; stepId?: string; nonce: number } | null;
}) {
  const t = useT();
  const lang = useLanguage();
  const { settings } = useInterfaceSettings();
  const containerRef = useRef<HTMLDivElement>(null);
  const client = useMemo(() => createExecutionReadClient({
    baseUrl: API_BASE_URL,
    fetchImpl: (input, init) => api.fetchWithAuth(input, init),
  }), []);

  const eventQueries = useQueries({
    queries: rootRunIds.map((runId) => ({
      queryKey: qk.runEvents(sessionId, runId),
      queryFn: async () => mergeCanonicalRunEvents(
        [],
        await readCanonicalRunEvents(client, sessionId, runId),
        sessionId,
        runId,
      ),
      staleTime: Number.POSITIVE_INFINITY,
    })),
  });

  const loading = eventQueries.some((query) => query.isLoading);
  const perRunEvents = eventQueries
    .map((query) => query.data)
    .filter((data): data is ExecutionEvent[] => Boolean(data?.length));

  const allEvents = useMemo(
    () => perRunEvents.flat().sort((a, b) => a.sequence - b.sequence),
    [perRunEvents],
  );
  const model = useMemo(() => projectResearchMap(allEvents), [allEvents]);

  // 右栏 = 完整过程，按发生顺序：调度器自己的独白 + 每个子节点 run 的卡片。
  // 左栏（对话）2026-08-18 起只留对话级内容，独白搬到这里 —— 同一份数据两个
  // 视口：左边回答"到哪了"，右边回答"怎么做的"。
  type Entry = {
    kind: "node"; sequence: number; id: string;
    step: RunActivityStep; activity: ReturnType<typeof projectRunActivity>;
    nodeType: string; note?: string;
  };

  const entries = useMemo<Entry[]>(() => perRunEvents.flatMap((events) => {
    const owningRunId = events.find((event) => !event.parentRunId)?.runId;
    const activity = projectRunActivity(
      events,
      owningRunId ? interruptedRunIds?.has(owningRunId) : undefined,
      lang,
    );
    const childRunIds = new Set(
      activity.steps.filter((s) => s.kind === "child" && s.runId).map((s) => s.runId as string),
    );
    const nodes: Entry[] = activity.steps
      .filter((step) => step.kind === "child")
      .map((step) => {
        const nodeType = (step.runId && parseRunLineage(step.runId)?.nodeType) || step.title || "";
        // 卡片的语境行：派它之前对用户说的那句话（时间上最贴近的一条）。
        const note = [...(activity.said ?? [])]
          .filter((said) => said.aboutNodeType === nodeType && said.sequence < step.sequence)
          .at(-1)?.text;
        return {
          kind: "node" as const, sequence: step.sequence, id: step.id,
          step, activity, nodeType, note,
        };
      });
    // 右栏**只有卡片**：调度器说的话是对话，归左栏（wangd 2026-08-18：
    // 「这不都是调度器节点的输出吗？是否应该显示在左边比较好啊？右边就是
    // 各种卡片点开是子节点历史比较好啊？」）。子节点自己的独白在它的卡片
    // 里展开 —— 就像后台任务面板：外面一行状态，点开是那一条的完整过程。
    void childRunIds;
    return nodes;
  }).sort((a, b) => a.sequence - b.sequence), [perRunEvents, interruptedRunIds, lang]);

  useEffect(() => {
    if (!focus) return;
    scrollToRun(focus);
    // entries.length 变化时重试一次：点卡片打开面板的瞬间数据可能还没到。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, entries.length]);

  /** 左栏那个「→ hypothesis」箭头点的是**哪一次**派发，就定位到哪一张卡。
   *
   * wangd 2026-08-21：「点开之后应该直接切入到右边这个具体的本次节点的运行
   * 细节…目前好像只是把右边展开，并没有直接定位到那个当次的运行细节」。
   *
   * 原来 focus 只带 `nodeType`，而定位取的是 `matches.item(length - 1)` ——
   * **最后一张同类卡**。同一个节点被派发三次时，点第一次的箭头也会滚到第三张。
   * 「这一次」和「这一趟」又一次被当成一件事。
   *
   * 现在 focus 带上派发自己的 `stepId`（卡片上有 `data-step-id`）：有它就精确
   * 命中，没有（老链路、地图上的车站点击）才退回按 nodeType 取最后一张。
   * 命中之后展开它并高亮 —— 滚到位了但卡是收起的，等于没定位。
   */
  const scrollToRun = (target: { nodeType: string; stepId?: string }) => {
    const root = containerRef.current;
    if (!root) return;
    const exact = target.stepId
      ? root.querySelector(`[data-step-id="${CSS.escape(target.stepId)}"]`)
      : null;
    const matches = root.querySelectorAll(`[data-map-node="${CSS.escape(target.nodeType)}"]`);
    const card = exact ?? matches.item(matches.length - 1);
    if (!card) return;
    card.querySelector("details")?.setAttribute("open", "");
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    card.classList.add("is-focused");
    window.setTimeout(() => card.classList.remove("is-focused"), 2000);
  };

  const scrollToStation = (nodeType: string) => scrollToRun({ nodeType });

  // ── 当前 / 历史（wangd 2026-08-18）────────────────────────────────────
  //
  // 「当前」= **只有正在跑的节点卡**。不含调度器独白 —— 那是"怎么做的"，
  // 属于历史/过程（wangd 2026-08-18：「这个里面还有很多是调度器的输出啊」）。
  const current = entries.filter((entry) => entry.step.status === "running");
  const history = entries.filter((entry) => !current.includes(entry));
  // 默认永远停在「当前」；没有在跑的就**空着**，不许自作主张跳去历史
  // （wangd：「如果说没有当前节点是在运行的话，你就空着，也不能显示历史」）。
  // 历史是**点了才看**的东西。
  const [tab, setTab] = useState<"current" | "history">("current");
  // 流程图默认展开，但**可以收起** —— 它回答"整体走到哪了"，而在你翻具体
  // 节点细节时那个问题暂时不重要，图却占掉小半个面板
  // （wangd 2026-08-19：「那个图现在占地方太大了…点一下能展开，然后能缩回」）。
  // 记住选择：收起是一个持续的意图，不该每次重开面板都要再点一次。
  const [mapOpen, setMapOpen] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    return window.localStorage.getItem(MAP_COLLAPSED_KEY) !== "1";
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(MAP_COLLAPSED_KEY, mapOpen ? "0" : "1");
  }, [mapOpen]);
  const shown = tab === "current" ? current : history;

  // 这里只是右栏里的**一个 Tab 的内容** —— 外壳（宽度、拖拽、标签条、收起）
  // 归 WorkspacePanelHost。原来它自带 <aside> 和标题栏，因为那时候右栏只装
  // 得下它一样东西；现在同一栏还要装打开的文件，壳就不该由内容之一来定义。
  return (
    <div ref={containerRef} className="session-inspector-content" aria-label={t({ zh: "研究进程", en: "Research progress" })}>
      {/* 流程图常驻：它回答"整体走到哪了"，那个问题在你翻任何细节的时候
          都还在（wangd 2026-08-18：「流程图应该是常驻显示比较合理」）。 */}
      <div className="session-inspector-pinned">
        <button
          type="button"
          className="session-inspector-map-toggle"
          onClick={() => setMapOpen((open) => !open)}
          aria-expanded={mapOpen}
        >
          <ChevronDown size={12} className={mapOpen ? "" : "is-collapsed"} />{t({ zh: "流程图", en: "Flow map" })}</button>
        {mapOpen && (loading && allEvents.length === 0
          ? <p className="session-inspector-empty">{t({ zh: "正在读取运行记录…", en: "Reading the run record…" })}</p>
          : <ResearchMap model={model} onStationClick={scrollToStation} />)}
        <div className="session-inspector-tabs" role="tablist">
          <button
            type="button" role="tab" aria-selected={tab === "current"}
            className={tab === "current" ? "is-active" : ""}
            onClick={() => setTab("current")}
          >
            当前{current.length > 0 ? ` · ${current.length}` : ""}
          </button>
          <button
            type="button" role="tab" aria-selected={tab === "history"}
            className={tab === "history" ? "is-active" : ""}
            onClick={() => setTab("history")}
          >
            历史 · {history.length}
          </button>
        </div>
      </div>
      <div className="session-inspector-runs">
        {shown.map((entry) => (
          <div
            key={entry.id}
            className="session-inspector-card"
            data-map-node={entry.nodeType}
            data-step-id={entry.step.id}
          >
            {entry.note && <p className="session-inspector-card-note">{entry.note}</p>}
            <ToolGroup
              step={entry.step}
              trace={settings.execution_detail === "trace"}
              narration={entry.activity.narration}
              workspaceChanges={entry.activity.workspaceChanges}
            />
          </div>
        ))}
        {!loading && shown.length === 0 && (
          <p className="session-inspector-empty">
            {tab === "current" ? t({ zh: "当前没有节点在跑。", en: "No node is running right now." }) : t({ zh: "还没有运行记录。", en: "No run record yet." })}
          </p>
        )}
      </div>
    </div>
  );
}
