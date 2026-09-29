/**
 * 右栏「研究进程」的宽度 —— 可拖，记得住。
 *
 * 原来是 `flex: 0 0 min(480px, 44%)`，一个写死的数。读节点明细的时候嫌窄，
 * 只想扫一眼流程图的时候又嫌它占地方，而这两件事在同一次研究里来回切换。
 *
 * 夹取规则单独放在这里而不是写在拖拽回调里：拖拽那段代码要真拖才跑得到，
 * 而这条规则最需要被验的恰恰是**边界**（窗口比两栏加起来还窄的时候会怎样）。
 */

export const INSPECTOR_MIN_WIDTH = 320;
export const INSPECTOR_MAX_WIDTH = 900;
export const INSPECTOR_DEFAULT_WIDTH = 480;
/** 对话列不许被挤到这个宽度以下 —— 右栏再有用，正文才是主体。 */
export const CANVAS_MIN_WIDTH = 420;

const STORAGE_KEY = "atrium.session.inspector_width";

/**
 * 把一个宽度夹进当前窗口下的合法区间。
 *
 * 上界取「窗口宽度减去对话列的最小宽度」，但**下界永远优先**：窗口窄到两者
 * 都放不下时，上界会低于下界，天真的 `Math.min(Math.max(...))` 在这里会算出
 * 一个比最小值还小的宽度（甚至负数），右栏直接塌成一条缝。宁可挤对话列，
 * 也不要给出一个不可用的右栏。
 */
export function clampInspectorWidth(width: number, viewportWidth: number): number {
  if (!Number.isFinite(width)) return INSPECTOR_DEFAULT_WIDTH;
  const upper = Math.max(
    INSPECTOR_MIN_WIDTH,
    Math.min(INSPECTOR_MAX_WIDTH, viewportWidth - CANVAS_MIN_WIDTH),
  );
  return Math.round(Math.min(Math.max(width, INSPECTOR_MIN_WIDTH), upper));
}

export function readInspectorWidth(): number {
  if (typeof window === "undefined") return INSPECTOR_DEFAULT_WIDTH;
  try {
    const stored = Number(window.localStorage.getItem(STORAGE_KEY));
    if (!stored) return INSPECTOR_DEFAULT_WIDTH;
    return clampInspectorWidth(stored, window.innerWidth);
  } catch {
    // 隐私模式下 localStorage 会抛。记不住宽度不该让整个会话页打不开。
    return INSPECTOR_DEFAULT_WIDTH;
  }
}

export function writeInspectorWidth(width: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, String(Math.round(width)));
  } catch {
    // 同上：存不下就算了。
  }
}
