/**
 * 研究地图的排版几何 —— 车站排在哪、弧抬多高、这张图**竖向占多少**。
 *
 * 单独一层是为了能测：竖向尺寸是现算的（见 mapBand），算小了会裁掉次数标注
 * 或卫星名，算大了就退回"占地方"。渲染在 ResearchMap.tsx，读的是同一套常量。
 */

/** 车站间距由 stationX 按 WIDTH 均分。 */
export const WIDTH = 440;

// —— 以下都是「相对车站圆心」的排版量，单位就是 px（图按 1:1 封顶渲染）。——
/** 进行中的呼吸圈最大半径：没有任何弧线、也没挂卫星时，它就是上边界。 */
export const PULSE_R = 19;
/** 车站名 / 小注的基线，以及小注这一行的视觉底边。 */
export const LABEL_DY = 22;
export const CAPTION_DY = 34;
const CAPTION_BOTTOM = 38;
/** 弧线抬升：跨得越远弧越高。apex 落在抬升量的一半处（二次贝塞尔）。 */
const LIFT_BASE = 44;
const LIFT_STEP = 22;
/** 上/下弧的次数标注各自越过 apex 多少（含字身）。 */
const FORWARD_LABEL_REACH = 15;
const BACKWARD_LABEL_REACH = 18;
/**
 * 服务用量行：车站小注下面的一行小字（literature ×4 · data ×2）。
 *
 * 服务节点**不再画成挂出去的卫星** —— 卫星是给"拓扑"用的视觉语法，而
 * literature/data/reviewer 的调用次数是车站的**属性**（这一阶段消耗了多少
 * 支持性工作），不是研究走到过的地方。属性一旦给了坐标和连线，图就长成
 * 蜘蛛网（wangd 2026-08-22：「每个节点都支出去几个叉，弄得很乱」）。
 */
export const SERVICE_DY = 47;
const SERVICE_BOTTOM = 50;

/**
 * 服务行上真正要显示的那几个，按「正在跑的排最前，其余按次数」排好。
 *
 * 架构节点（`_reviewer` / `_curator` 这些下划线开头的）**只在正在跑的时候出现**
 * （wangd 2026-08-22 拍板）。理由是它们的历史次数 ≈ producing run 数 —— 每个
 * 产出节点跑完都要过审查，所以"审查过 5 次"这条信息在图上恒为真、区分不了任何
 * 两张图，常驻列出来只是噪声，还挤掉了 literature/data 这些**真的有多有少**的
 * 用量。但"此刻正在审"是会变的，那一刻它必须看得见。
 *
 * 判据取节点名前缀而不是写死 reviewer/curator 的名单：新加的架构节点默认跟着
 * 这条规则走，不会因为没人回来改名单而漏在图上（[[护栏要扫盘，不要写名单]]）。
 *
 * 这个函数是**唯一**的可见性真相源：ServiceLine 画哪几个、mapBand 要不要为服务
 * 行留出那一行高度，都读它。否则会出现"高度留了、字没画"的空行。
 */
export function visibleServices<T extends { nodeType: string; count: number; running: boolean }>(
  satellites: readonly T[],
): T[] {
  return satellites
    .filter((satellite) => satellite.running || !satellite.nodeType.startsWith("_"))
    .sort((a, b) => Number(b.running) - Number(a.running) || b.count - a.count);
}
/** 上下各留一点，别让字贴着边。 */
const PAD = 3;

export function lift(span: number): number {
  return LIFT_BASE + (span - 1) * LIFT_STEP;
}

export function stationX(index: number, total: number): number {
  return Math.round(((index + 1) * WIDTH) / (total + 1));
}

/**
 * 竖向占位：图上**真出现的东西**撑出来的上下边界。
 *
 * 之所以现算而不是写死一个 viewBox 高度：写死的那个数是按"最热闹的图"排的，
 * 于是只画了一个车站时也照样占那么高 —— 而右栏里一个车站才是最常见的局面。
 * 上边界现在只由弧线/呼吸圈决定；服务行只占下边界的一行。
 */
export function mapBand(
  edges: readonly { from: string; to: string }[],
  indexByType: ReadonlyMap<string, number>,
  /**
   * 各车站（外加浮动那一组）挂着的服务，**原样传进来**。
   *
   * 收的是清单而不是"数量"：要不要给服务行留那一行高度，取决于**画得出来**
   * 几个，而"画得出来"这条规则住在 visibleServices 里。让调用方先数好再传，
   * 就等于把同一条规则复制到了调用方 —— 两份会各自演化，症状是"高度留了、
   * 字没画"的一条空行（[[一个问题一个真相源]]）。
   */
  satelliteGroups: readonly (readonly { nodeType: string; count: number; running: boolean }[])[],
): { stationY: number; height: number } {
  let top = PULSE_R;
  let bottom = CAPTION_BOTTOM;
  for (const edge of edges) {
    const from = indexByType.get(edge.from);
    const to = indexByType.get(edge.to);
    if (from === undefined || to === undefined) continue;
    const apex = lift(Math.abs(to - from)) / 2;
    if (from < to) top = Math.max(top, apex + FORWARD_LABEL_REACH);
    else bottom = Math.max(bottom, apex + BACKWARD_LABEL_REACH);
  }
  if (satelliteGroups.some((group) => visibleServices(group).length > 0)) {
    bottom = Math.max(bottom, SERVICE_BOTTOM);
  }
  const stationY = Math.round(top + PAD);
  return { stationY, height: stationY + Math.round(bottom + PAD) };
}
