/**
 * 「这条事件属不属于这一轮」—— 判据只有这一处。
 *
 * ## 为什么要收成一个函数（2026-08-12 的教训）
 *
 * 「一轮 = 这条 run **和它派出去的子节点**」这个定义，前端有**六处**各自写了
 * 一遍：分页解析器、读取循环、合并、SSE 流、注册表的 token/gap 路由。
 *
 * 我改这个定义时，一次只找到两三处，于是同一个下午撞了三轮：
 *
 *   第一轮  首屏带上了子代，实时轮询那条路没带 → 正在跑的那轮看不到子节点
 *   第二轮  读取循环放宽了，合并那处没放宽 → 抛 "merge crossed the identity"
 *   第三轮  两处都放宽了，客户端分页解析器没放宽 → 抛 "outside the requested runId"
 *
 * 每一轮的症状都一样：整页执行记录空白，而报错指向读取代码，离改动十万八千里。
 *
 * 判据散在六处，就不存在"改对"这回事 —— 只存在"这次改到了几处"。
 *
 * ## 边界仍然是边界
 *
 * 放宽 ≠ 取消。别人家的 run 混进来照样要吵 —— 那说明后端过滤错了，是真 bug，
 * 不该被悄悄咽掉。
 */
export function belongsToRun(
  event: { runId?: string | null; parentRunId?: string | null },
  runId: string,
): boolean {
  return event.runId === runId || event.parentRunId === runId;
}
