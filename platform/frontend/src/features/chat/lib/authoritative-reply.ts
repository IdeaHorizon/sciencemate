/**
 * 一轮结束时，这条 assistant 消息该显示什么。
 *
 * ## 为什么需要收敛（2026-08-17）
 *
 * 流式期间 `appendToken` 逐个追加，累积出来的是这一轮**所有中间轮次的散文
 * 首尾相接** —— 调度器起 literature 前说的话、拿到结果后说的话、决定下一步
 * 说的话，全都黏在一起。那是过程，不是回复。
 *
 * 它就这么留在对话里，于是同一段话在页面上出现两次：这坨拼接产物在上面，
 * 下面"第 N 轮"叙述事件把同一段又画一遍（实测截图）。
 *
 * 中间散文本身有价值 —— 但它的归宿是时间线上那条叙述，紧挨着它解释的那次
 * 工具调用。对话里该留的是回复契约认可的那一段（后端的 `final_text`）。
 *
 * ## 为什么终稿为空时保留累积文本
 *
 * 收敛的目的是"换成更权威的那份"，不是"清空"。后端没给终稿（老版本、异常
 * 路径、pause 逃逸）时，累积文本是用户手上仅有的东西 —— 拿掉它就是把一轮
 * 的可见产出直接抹掉，比重复难受得多。
 */
export function authoritativeReplyText(
  accumulated: string,
  meta: Record<string, unknown>,
): string {
  const reply = typeof meta.reply === "string" ? meta.reply.trim() : "";
  return reply || accumulated;
}
