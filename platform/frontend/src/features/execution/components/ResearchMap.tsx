"use client";

import { useId } from "react";
import type { MapSatellite, MapStation, ResearchMapModel } from "../lib/research-map";
import {
  CAPTION_DY,
  LABEL_DY,
  PULSE_R,
  SERVICE_DY,
  WIDTH,
  lift,
  mapBand,
  stationX,
  visibleServices,
} from "../lib/research-map-layout";
import { useT } from "@/shared/i18n";

/**
 * 研究地图渲染。视觉语法（wangd 2026-08-17 定稿）：
 *
 * - 车站是圆环不是方块；只画到访过的，地图随项目生长。
 * - 弧线有方向语义：前进弧走上方，回访弧从下方绕回，各带次数；
 *   正在发生的转移是流动虚线，视线被引到亮着的节点上。
 * - 服务节点是卫星，挂在派它的车站旁，不占主线。
 * - 三种颜色封顶：墨色（事实）、teal（此刻）、紫（服务）。
 *
 * 布局是手工模板不是自动布局 —— 车站集固定（最小节点集），所以位置可以
 * 排一次版，之后只有状态在数据驱动地变。这正是它能不长成蜘蛛网的原因。
 *
 * ## 竖向：按内容现算，且不随面板宽度膨胀（wangd 2026-08-22）
 *
 * 原来是「固定 viewBox 比例 + width:100%」，于是**图的高度是右栏宽度的函数**：
 * 面板拖到 600px，440×150 的图就被等比放大成 205px 高 —— 只画了一个车站也
 * 占掉小半个面板。8-21 那次把 WIDTH/HEIGHT 一起调小只是把系数改了，
 * 「会自己长大」这件事没变，所以拖宽之后又长回去。
 *
 * 所以改两件事：
 * - 竖向尺寸按**这张图实际画了什么**现算（有没有上弧/下弧、跨几站、挂没挂
 *   卫星），没画的东西不预留位置 —— 只有一个车站时就只剩车站那一条带；
 * - 渲染封在 1:1（内联 `max-height` = 现算高度，preserveAspectRatio 默认
 *   meet），面板再宽也只是左右留白。窄面板下 height:auto 仍让它整体缩，
 *   不会横向溢出。
 */

const TEAL = "#1D9E75";
const INK = "#888780";
// 紫色（服务）挪进了 sessions.css 的 .research-map-service-running ——
// 服务不再画成 SVG 里的卫星圆点，只剩服务行的文字色还需要它。

function Station({ station, x, y, onClick }: {
  station: MapStation;
  x: number;
  y: number;
  onClick?: () => void;
}) {
  const t = useT();
  const color = station.running ? TEAL : INK;
  const caption = [
    station.caption,
    station.visits > 1 ? `×${station.visits}` : null,
  ].filter(Boolean).join(" ");
  return (
    <g
      onClick={onClick}
      style={onClick ? { cursor: "pointer" } : undefined}
      role={onClick ? "button" : undefined}
      aria-label={t({ zh: `${station.label}${station.running ? " · 进行中" : ""}`, en: `${station.label}${station.running ? ` · ${t({ zh: "进行中", en: "in progress" })}` : ""}` })}
    >
      {station.running && (
        <circle cx={x} cy={y} r={9} fill={TEAL} opacity={0.5}>
          <animate attributeName="r" values={`9;${PULSE_R}`} dur="1.8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.5;0" dur="1.8s" repeatCount="indefinite" />
        </circle>
      )}
      <circle cx={x} cy={y} r={station.running ? 8 : 7} fill={color} />
      <circle
        cx={x}
        cy={y}
        r={12.5}
        fill="none"
        stroke={color}
        strokeOpacity={0.5}
        strokeDasharray={station.running ? "3 4" : undefined}
      />
      <text x={x} y={y + LABEL_DY} textAnchor="middle" className="research-map-label">
        {station.label}
      </text>
      <text x={x} y={y + CAPTION_DY} textAnchor="middle" className="research-map-caption">
        {station.running ? (
          <>
            {caption ? `${caption} · ` : ""}
            <tspan fill={TEAL}>{t({ zh: "进行中", en: "In progress" })}</tspan>
          </>
        ) : caption || null}
      </text>
    </g>
  );
}

/**
 * 服务用量行：一行小字挂在车站小注下面，不再是挂出去的卫星（拓扑留给轨迹，
 * 属性只做标注 —— 见 research-map-layout.ts 的说明）。
 *
 * - 显示哪几个由 visibleServices 定（架构节点只在跑的时候露面）；
 * - 正在跑的排最前、紫色脉动 —— "此刻有支持性工作在进行"仍然一眼可见；
 * - 车站多时列一个、其余折进 +n（列宽摆不下），完整清单在 hover title 里；
 * - `_reviewer` 这类架构节点去掉前缀下划线再显示。
 */
function serviceChip(satellite: MapSatellite): string {
  const name = satellite.nodeType.replace(/^_/, "");
  return satellite.count > 1 ? `${name} ×${satellite.count}` : name;
}

function ServiceLine({ satellites, x, y, maxChips }: {
  satellites: MapSatellite[];
  x: number;
  y: number;
  maxChips: number;
}) {
  const sorted = visibleServices(satellites);
  if (sorted.length === 0) return null;
  const shown = sorted.slice(0, maxChips);
  const hidden = sorted.length - shown.length;
  return (
    <text x={x} y={y} textAnchor="middle" className="research-map-service">
      <title>{sorted.map(serviceChip).join(" · ")}</title>
      {shown.map((satellite, index) => (
        <tspan
          key={satellite.nodeType}
          className={satellite.running ? "research-map-service-running" : undefined}
        >
          {index > 0 ? " · " : ""}
          {serviceChip(satellite)}
        </tspan>
      ))}
      {hidden > 0 ? ` +${hidden}` : ""}
    </text>
  );
}

export function ResearchMap({ model, onStationClick }: {
  model: ResearchMapModel;
  onStationClick?: (nodeType: string) => void;
}) {
  const t = useT();
  const markerId = useId().replaceAll(":", "");
  const xByType = new Map(
    model.stations.map((station, index) => [station.nodeType, stationX(index, model.stations.length)]),
  );
  const indexByType = new Map(model.stations.map((station, index) => [station.nodeType, index]));
  const satellitesByStation = new Map<string | null, MapSatellite[]>();
  for (const satellite of model.satellites) {
    const key = satellite.station && xByType.has(satellite.station) ? satellite.station : null;
    satellitesByStation.set(key, [...(satellitesByStation.get(key) ?? []), satellite]);
  }
  const floating = satellitesByStation.get(null) ?? [];

  if (model.stations.length === 0 && floating.length === 0) {
    return <p className="research-map-empty">{t({ zh: "还没有节点跑过 —— 派出第一个节点后，地图从这里长出来。", en: "No node has run yet — the map grows from here once the first one is dispatched." })}</p>;
  }

  // 服务原样交给 mapBand —— 画得出来几个（架构节点跑完就不画）由它自己按
  // visibleServices 判，这边不重复一遍那条规则。浮动服务画在同一行上，一起给。
  const { stationY, height } = mapBand(model.edges, indexByType, [
    ...model.stations.map((station) => satellitesByStation.get(station.nodeType) ?? []),
    floating,
  ]);

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${height}`}
      className="research-map"
      // 1:1 封顶 —— 面板拖宽只是左右留白，图不跟着长高（见文件头说明）。
      style={{ maxHeight: `${height}px` }}
      role="img"
      aria-label={t({ zh: "研究进程地图", en: "Research progress map" })}
    >
      <defs>
        <marker id={`${markerId}-ink`} viewBox="0 0 8 8" refX="6.5" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M1.5,1.5 L6.5,4 L1.5,6.5" fill="none" stroke={INK} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
        </marker>
        <marker id={`${markerId}-teal`} viewBox="0 0 8 8" refX="6.5" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M1.5,1.5 L6.5,4 L1.5,6.5" fill="none" stroke={TEAL} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
        </marker>
      </defs>
      {model.edges.map((edge) => {
        const fromX = xByType.get(edge.from);
        const toX = xByType.get(edge.to);
        if (fromX === undefined || toX === undefined) return null;
        // 前进（画布上从左往右）弧走上方，回访走下方；跨得越远弧越高。
        const forward = (indexByType.get(edge.from) ?? 0) < (indexByType.get(edge.to) ?? 0);
        const span = Math.abs((indexByType.get(edge.to) ?? 0) - (indexByType.get(edge.from) ?? 0));
        const rise = lift(span) * (forward ? -1 : 1);
        const gap = forward ? -9 : 9;
        const midX = (fromX + toX) / 2;
        const apexY = stationY + rise / 2;
        const color = edge.active ? TEAL : INK;
        return (
          <g key={`${edge.from}→${edge.to}`}>
            <path
              d={`M${fromX},${stationY + gap} Q${midX},${stationY + rise} ${toX},${stationY + gap}`}
              fill="none"
              stroke={color}
              strokeWidth={edge.active ? 1.8 : 1.5}
              className={edge.active ? "research-map-flow" : undefined}
              markerEnd={`url(#${markerId}-${edge.active ? "teal" : "ink"})`}
            />
            <text
              x={midX}
              y={apexY + (forward ? -7 : 15)}
              textAnchor="middle"
              className="research-map-caption"
              fill={edge.active ? TEAL : undefined}
            >
              ×{edge.count}
            </text>
          </g>
        );
      })}
      {model.stations.map((station) => (
        <g key={station.nodeType}>
          <Station
            station={station}
            x={xByType.get(station.nodeType) ?? 0}
            y={stationY}
            onClick={onStationClick ? () => onStationClick(station.nodeType) : undefined}
          />
          <ServiceLine
            satellites={satellitesByStation.get(station.nodeType) ?? []}
            x={xByType.get(station.nodeType) ?? 0}
            y={stationY + SERVICE_DY}
            maxChips={model.stations.length >= 4 ? 1 : 2}
          />
        </g>
      ))}
      {/* 认不出派发车站的服务：沉到左下角一行，别再假装占个站位。 */}
      <ServiceLine
        satellites={floating}
        x={54}
        y={stationY + SERVICE_DY}
        maxChips={2}
      />
    </svg>
  );
}
