/**
 * 分区的两种排布。
 *
 * ## 为什么这是一个选择，而不是我们替用户定死
 *
 * 两种排布回答的是不同的问题：
 *
 *   - `list`（一行一条）：一屏能扫过更多标题，适合"今天有什么"这种快速筛选。
 *     但它放不下配图 —— 一条 60px 高的行里塞图，图小到没有信息量。
 *   - `grid`（一行三块）：能显示配图和更长的摘要，适合"慢慢看"。代价是
 *     一屏只剩三到六条。
 *
 * 哪种更好取决于此刻在做什么，不取决于我们的审美。所以给开关，并且记住选择。
 */
export type FeedViewMode = "list" | "grid";

export const FEED_VIEW_MODES: readonly FeedViewMode[] = ["list", "grid"] as const;

export function isFeedViewMode(value: unknown): value is FeedViewMode {
  return value === "list" || value === "grid";
}

/**
 * 从本地存储读上次的选择。
 *
 * 存本地而不是发回服务端：这是一个**呈现偏好**，换台机器换个屏幕本来就该
 * 可以不一样，而且它不值得为它多一次写请求。
 *
 * SSR 时 `window` 不存在 —— 返回默认值，客户端挂载后再纠正。
 */
export const VIEW_MODE_STORAGE_KEY = "atrium.feed_view_mode";

/**
 * 默认排布。
 *
 * **默认带图**（`grid`）：图片是这个功能明确要有的东西，而藏在一个开关后面
 * 等于没做 —— 用户不会为了看图去找开关，他只会觉得这里没有图。
 *
 * 每一条都有图：feed 自带的图 → 论文 PDF 里裁出来的插图 → 生成的排版封面
 * （后端三级供给，见 `feed/thumbnail.py`）。所以格子视图不会出现成片的
 * 灰底占位，界面也不再需要为"这批没图"解释什么。
 */
export const DEFAULT_VIEW_MODE: FeedViewMode = "grid";

export function readStoredViewMode(storage: Pick<Storage, "getItem"> | null): FeedViewMode {
  if (!storage) return DEFAULT_VIEW_MODE;
  try {
    const raw = storage.getItem(VIEW_MODE_STORAGE_KEY);
    return isFeedViewMode(raw) ? raw : DEFAULT_VIEW_MODE;
  } catch {
    // 隐私模式下 localStorage 可能直接抛。呈现偏好读不到不该让页面打不开。
    return DEFAULT_VIEW_MODE;
  }
}
