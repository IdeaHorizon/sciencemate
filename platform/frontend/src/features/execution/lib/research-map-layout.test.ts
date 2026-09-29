import test from "node:test";
import assert from "node:assert/strict";
import {
  CAPTION_DY,
  PULSE_R,
  SERVICE_DY,
  lift,
  mapBand,
  stationX,
  visibleServices,
} from "./research-map-layout.ts";

/**
 * 这张图的高度是现算的，所以两个方向都要盯：
 *
 * - **算小了**会裁掉东西（弧上的次数、卫星名），而裁掉之后图还是"画出来了"，
 *   没有任何报错 —— 只有量一遍才看得见；
 * - **算大了**就退回 wangd 报的那个问题（一个车站占掉小半个面板）。
 *
 * 所以断言写的是「上下边界必须罩住图上真画到的最远那个字」+「常见局面的
 * 高度必须留在这个数以内」。数字是手算的，不是从被测函数里再算一遍。
 */

const idx = (types: string[]) => new Map(types.map((t, i) => [t, i]));
const service = (nodeType: string, count: number, running = false) => ({ nodeType, count, running });
/** n 个普通服务（一定画得出来）—— 只关心"有没有服务"的用例用它。 */
const services = (n: number) => Array.from({ length: n }, (_, i) => service(`svc${i}`, 1));

test("只有一个车站时，只占车站那一条带", () => {
  const { stationY, height } = mapBand([], idx(["hypothesis"]), [[]]);
  // 上：呼吸圈 19 + 留白 3；下：小注那一行 38 + 留白 3。
  assert.equal(stationY, 22);
  assert.equal(height, 63);
});

test("上边界罩得住前进弧的次数标注（跨得越远弧越高）", () => {
  const types = idx(["a", "b", "c", "d"]);
  for (const [from, to, span] of [["a", "b", 1], ["a", "c", 2], ["a", "d", 3]] as const) {
    const { stationY } = mapBand([{ from, to }], types, [[], [], [], []]);
    // 弧顶在抬升量的一半处，次数标注写在弧顶上方 7px，字身还要 8px。
    const labelTop = lift(span) / 2 + 7 + 8;
    assert.ok(stationY >= labelTop, `span=${span}: stationY=${stationY} < ${labelTop}`);
  }
});

test("下边界罩得住回访弧的次数标注", () => {
  const types = idx(["a", "b", "c"]);
  const { stationY, height } = mapBand([{ from: "c", to: "a" }], types, [[], [], []]);
  // 回访弧走下方，标注写在弧顶下方 15px，字身 3px。
  assert.ok(height - stationY >= lift(2) / 2 + 15 + 3);
});

test("有服务才给服务行让位，没有就不预留", () => {
  const one = mapBand([], idx(["a"]), [services(1)]);
  const none = mapBand([], idx(["a"]), [[]]);
  // 服务行的字底要落在画布里（基线 SERVICE_DY，字身 3px）。
  assert.ok(one.height - one.stationY >= SERVICE_DY + 3);
  // 服务只占下边界，不动上边界；没有服务就不该为它留位置。
  assert.equal(one.stationY, none.stationY);
  assert.ok(one.height > none.height);
});

test("服务行和车站小注不叠在同一行", () => {
  assert.ok(SERVICE_DY - CAPTION_DY >= 10);
});

test("最热闹的图也留在这个高度以内（这是「不许长回去」的那道线）", () => {
  const types = idx(["hypothesis", "observation", "experiment", "writing"]);
  const { height } = mapBand(
    [
      { from: "hypothesis", to: "writing" },
      { from: "writing", to: "hypothesis" },
      { from: "hypothesis", to: "experiment" },
      { from: "experiment", to: "observation" },
    ],
    types,
    [services(4), [], services(3), []],
  );
  assert.ok(height <= 130, `height=${height}`);
  // 服务不再挂成卫星，上边界只归弧线管 —— 最热闹的图也比卫星时代扁。
  // 改回"固定比例 + 100% 宽"的话，600px 宽的右栏会把它放大到 200px 以上。
  assert.ok(height >= PULSE_R);
});

test("车站按 WIDTH 均分，与高度无关", () => {
  assert.deepEqual([0, 1, 2].map((i) => stationX(i, 3)), [110, 220, 330]);
});

test("架构节点只在正在跑的时候出现在服务行", () => {
  const shown = visibleServices([
    service("_reviewer", 5),
    service("_curator", 3, true),
    service("literature", 4),
  ]).map((s) => s.nodeType);
  // 跑着的 _curator 在；跑完的 _reviewer 不在（它的次数≈run 数，恒为真＝没信息）。
  assert.deepEqual(shown, ["_curator", "literature"]);
});

test("非架构服务不管跑没跑都在，正在跑的排最前", () => {
  const shown = visibleServices([
    service("literature", 4),
    service("postprocess", 1),
    service("data", 2, true),
  ]).map((s) => s.nodeType);
  assert.deepEqual(shown, ["data", "literature", "postprocess"]);
});

test("判据是名字前缀，不是写死的 reviewer/curator 名单", () => {
  // 将来新加的架构节点（`_` 开头）默认跟着同一条规则，不用回来改名单。
  assert.deepEqual(visibleServices([service("_futurearch", 9)]), []);
  assert.equal(visibleServices([service("_futurearch", 9, true)]).length, 1);
});

test("一个车站只挂了跑完的架构节点时，不给服务行留白", () => {
  // 留了高度却一个字都不画，就是图上凭空多出一条空行。mapBand 收的是清单，
  // 所以"画得出来几个"这条规则只有一份 —— 调用方没有机会数错。
  const doneReviewer = mapBand([], idx(["a"]), [[service("_reviewer", 5)]]);
  assert.deepEqual(doneReviewer, mapBand([], idx(["a"]), [[]]));
  // 同一个 reviewer 正在跑：它要画出来，那一行就得有位置。
  const runningReviewer = mapBand([], idx(["a"]), [[service("_reviewer", 5, true)]]);
  assert.ok(runningReviewer.height > doneReviewer.height);
  assert.ok(runningReviewer.height - runningReviewer.stationY >= SERVICE_DY + 3);
});
