import type { ExecutionEvent } from "./execution-event";

/**
 * 研究地图：把一个会话的执行事件投影成「走过的流程图」。
 *
 * ## 形状（wangd 2026-08-17 拍板）
 *
 * 流程**不是流水线**：核心是 Analysis ⇄ experiment 迭代回路，收敛后 writing
 * 收尾，其余节点都是服务。所以地图只有三种元素：
 *
 * - **车站**：治理型产出节点 —— hypothesis（v0.5 起承载 Analysis 角色，显示名
 *   用 analysis）/ observation / experiment / writing。只画到访过的 —— 地图是记录不是规划，writing 没开始
 *   就不存在，到了那天从图上长出来。
 * - **弧线**：车站之间按时间序的真实转移，带次数。去程和回程分开计数 ——
 *   「派出 ×3 / 交回 ×2」对不齐的那个 1 就是正在跑的这一次。
 * - **卫星**：literature / data / postprocess / _reviewer / _curator 挂在
 *   派它的那个车站旁边，带调用次数。它们不是车站，不占主线。
 *
 * ## 数据来源
 *
 * 全部来自 execution 事件流，与时间线同一个真相源（[[一个问题一个真相源]]）：
 * - 子 run 的身份：`run.started`（payload.nodeType + runId）。**不读 runs 表的
 *   status** —— 子 run 那一行的状态永远停在 queued，没人维护（见
 *   run_liveness / execution_view 的说明）。
 * - 谁派的：runId 末段 `A->B@dN` 是 ingest 有意写进身份里的血缘
 *   （"父子关系顺带写在 id 里，看一眼就知道谁派的"）。平台侧 parentRunId
 *   被拍平成根 run，回答不了这个问题。
 * - 活着/结束：该 runId 有没有终态事件。
 */

export type MapStation = {
  nodeType: string;
  /** 显示名 —— hypothesis 的角色是 Analysis，图上叫 analysis。 */
  label: string;
  /** 物理 node_type 与显示名不同时的小注（"hypothesis"）。 */
  caption?: string;
  visits: number;
  running: boolean;
};

export type MapEdge = {
  from: string;
  to: string;
  count: number;
  /** 这条转移的终点正在跑 —— 渲染成流动虚线。 */
  active: boolean;
};

export type MapSatellite = {
  nodeType: string;
  /** 挂在哪个车站；派发者不是车站时按时间就近归位，都没有则为 null（浮动）。 */
  station: string | null;
  count: number;
  running: boolean;
};

export type ResearchMapModel = {
  stations: MapStation[];
  edges: MapEdge[];
  satellites: MapSatellite[];
};

/** 主线车站。其余一切 node_type（包括未来新增的）都按卫星渲染 —— 宁可挂错位置也不消失。 */
//: 主线车站 = **治理型产出节点**（跑完走 review → decision 的那些）。
//:
//: 判据不是这里定的，是节点自己在 `harness.yaml` 里声明的 `post_run_flow`：
//: `none` = 服务型（literature / data / postprocess —— 被消费方随时调用，
//: 挂在派它的车站旁边），其余 = 治理型产出节点（见 `core/harness.py`
//: 的 `is_service`）。`_` 开头的是架构节点，永远是卫星。
//:
//: ⚠️ 前端读不到 yaml，所以这里是一份**抄件**。抄件会分叉，而且分叉不报错 ——
//: observation 2026 年加进来时就没人回来改这张表，于是一个治理型产出节点在
//: 图上被画成了小卫星（wangd 2026-08-21）。
//:
//: 所以配了扫盘闸：`tests/test_research_map_stations_match_node_roles.py` 直接
//: 读 `nodes/*/harness.yaml`，对不上就 CI 红。**名单可以存在，分叉不可以静默。**
const STATION_ORDER = [
  "hypothesis", "observation", "experiment", "derivation", "writing",
] as const;
const STATION_TYPES = new Set<string>(STATION_ORDER);

const STATION_LABELS: Record<string, { label: string; caption?: string }> = {
  hypothesis: { label: "analysis", caption: "hypothesis" },
  experiment: { label: "experiment" },
  derivation: { label: "derivation", caption: "演绎" },
  writing: { label: "writing" },
};

const TERMINAL_KINDS = new Set([
  "run.completed",
  "run.incomplete",
  "run.failed",
  "run.cancelled",
]);

type ChildRun = {
  runId: string;
  nodeType: string;
  dispatcher: string | null;
  startSequence: number;
  running: boolean;
};

/** runId 末段 `…::A->B@dN` → { dispatcher: A, nodeType: B }。 */
export function parseRunLineage(runId: string): { dispatcher: string; nodeType: string } | null {
  const match = /::([^:>]+)->([^@:>]+)@d\d+$/.exec(runId);
  if (!match) return null;
  return { dispatcher: match[1], nodeType: match[2] };
}

/**
 * 一次 `run.started` = **一次到访**。不是"一个 run id = 一次到访"。
 *
 * 调度器复用确定性 run id（`…::_orchestrator->literature@d1`），同一个节点派
 * 三次都是这个 id。旧写法的 `!runs.has(event.runId)` 守卫把第二次、第三次的
 * `run.started` 整个丢掉 —— 后果有两个，而且都是静默的：
 *
 * - 一个节点**第二次开跑永远不会在图上亮**（第一次的终态已经把 running 置
 *   false，之后没有任何东西能再把它翻回来）；
 * - `visits` 永远是 1。2026-08-22 现场真实事件流：literature 派了 3 次、
 *   data 3 次、hypothesis 2 次，图上一律显示 ×1。
 *
 * 「同一条 run 被打断又续跑」是另一件事，它发的是 `run.resumed`，不是
 * `run.started` —— 两者机械可分，所以这里不会把续跑误记成新到访。
 */
function collectChildRuns(events: readonly ExecutionEvent[]): ChildRun[] {
  const ordered = [...events].sort((a, b) => a.sequence - b.sequence);
  const visits: ChildRun[] = [];
  //: 每条 run id 当下**还开着的**那一次到访 —— 终态只关掉它，关不掉更早的。
  const open = new Map<string, ChildRun>();
  for (const event of ordered) {
    if (!event.runId || !event.parentRunId) continue;
    if (event.kind === "run.started") {
      const payload = event.payload as Record<string, unknown> | undefined;
      const lineage = parseRunLineage(event.runId);
      const nodeType = typeof payload?.nodeType === "string" && payload.nodeType
        ? payload.nodeType
        : lineage?.nodeType ?? "research";
      const visit: ChildRun = {
        runId: event.runId,
        nodeType,
        dispatcher: lineage?.dispatcher ?? null,
        startSequence: event.sequence,
        running: true,
      };
      visits.push(visit);
      open.set(event.runId, visit);
      continue;
    }
    if (TERMINAL_KINDS.has(event.kind)) {
      const visit = open.get(event.runId);
      if (visit) {
        visit.running = false;
        // 关掉就摘下来：同一次到访连发 cancelled + incomplete（现场就有）
        // 不该被读成两次结束，更不该让下一个终态回头去关别人。
        open.delete(event.runId);
      }
    }
  }
  return visits;
}

export function projectResearchMap(events: readonly ExecutionEvent[]): ResearchMapModel {
  const childRuns = collectChildRuns(events);
  const stationVisits = childRuns.filter((run) => STATION_TYPES.has(run.nodeType));
  const serviceRuns = childRuns.filter((run) => !STATION_TYPES.has(run.nodeType));

  // 到访过的车站，按 STATION_ORDER 排；**不在名单里的排在后面**，按首次到访
  // 的先后 —— 名单只排序，不准入（见 isStation）。一个没被排过序的新
  // producing 节点会出现在图上最右边，而不是消失。
  const stations: MapStation[] = STATION_ORDER
    .filter((nodeType) => stationVisits.some((visit) => visit.nodeType === nodeType))
    .map((nodeType) => ({
      nodeType,
      label: STATION_LABELS[nodeType]?.label ?? nodeType,
      caption: STATION_LABELS[nodeType]?.caption,
      visits: stationVisits.filter((visit) => visit.nodeType === nodeType).length,
      running: stationVisits.some((visit) => visit.nodeType === nodeType && visit.running),
    }));

  const edges = new Map<string, MapEdge>();
  for (let i = 1; i < stationVisits.length; i += 1) {
    const from = stationVisits[i - 1].nodeType;
    const to = stationVisits[i].nodeType;
    // 同型接续（重试/续跑）计入 visits，不是转移。
    if (from === to) continue;
    const key = `${from}→${to}`;
    const edge = edges.get(key) ?? { from, to, count: 0, active: false };
    edge.count += 1;
    if (stationVisits[i].running) edge.active = true;
    edges.set(key, edge);
  }

  const satellites = new Map<string, MapSatellite>();
  for (const run of serviceRuns) {
    // 派它的是车站就挂那；派发者是 _orchestrator/未知时，挂在它开跑时
    // 最近开始的那个车站（时间就近），一个车站都没有就浮动。
    const station = run.dispatcher && STATION_TYPES.has(run.dispatcher)
      ? run.dispatcher
      : [...stationVisits]
          .filter((visit) => visit.startSequence < run.startSequence)
          .at(-1)?.nodeType ?? null;
    const key = `${station ?? "·"}::${run.nodeType}`;
    const satellite = satellites.get(key)
      ?? { nodeType: run.nodeType, station, count: 0, running: false };
    satellite.count += 1;
    satellite.running = satellite.running || run.running;
    satellites.set(key, satellite);
  }

  return {
    stations,
    edges: [...edges.values()],
    satellites: [...satellites.values()],
  };
}
