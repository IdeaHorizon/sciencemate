import type { CurrentUser } from "@/lib/api";

/**
 * 开机时问一句「我是谁」。
 *
 * ## 为什么不能"没票就不问"
 *
 * 这里原来是 `if (!token) return;` —— 浏览器里没存 token 就不去问服务器。那在
 * 组织档上说得通：没登录就是没身份。但**个人档从头到尾没有登录这件事**，永远
 * 不会有 token，于是这一问永远不发生，`user` 永远是 `null`。
 *
 * 后果不是"少显示一个名字"：界面上每一处"能不能做这件事"问的都是
 * `hasAnyPermission(user, …)`，而 `user` 是 null 时它一律返回 false。2026-09-06
 * 真机实测，装好的应用里**没有任何地方能开一个研究会话** —— 空状态写着"第一条
 * 消息会成为一个会话"，而那个按钮就在同一段 JSX 里被 `canCreate` 挡掉了。API
 * 本身是通的（`POST …/sessions` 回 201，`GET /auth/me` 不带 token 也回一个带
 * `projects.create` 的本机用户）。也就是说：能力在，只是界面不知道自己有。
 *
 * ## 新规矩：**答这个问题的是服务器，不是浏览器里有没有一张票**
 *
 * 一律问。组织档没票时服务器回 401 —— 那正是"我没有身份"这个答案，跟原来
 * 一模一样。个人档回一个本机用户。前端不需要知道自己跑在哪个档位上。
 */
export type SessionBootstrap = {
  /** 浏览器里存着的票（组织档登录后写的）。个人档永远是 null。 */
  storedToken: string | null;
  /** 把票装到 API 客户端上，之后的请求都带着它。 */
  adoptToken: (token: string) => void;
  /** 问服务器：我是谁。没有身份时它抛错（401）。 */
  askWhoIAm: () => Promise<CurrentUser>;
  /** 票是坏的：清掉它和一切跟着它的东西。 */
  discardTheToken: () => void;
};

export async function bootstrapSession(deps: SessionBootstrap): Promise<CurrentUser | null> {
  if (deps.storedToken) {
    deps.adoptToken(deps.storedToken);
  }
  try {
    return await deps.askWhoIAm();
  } catch {
    // 拿着票被拒 = 票过期或被吊销，得清掉（否则每个请求都带着一张坏票）。
    // 本来就没票被拒 = 这台服务器要登录而我还没登录 —— 没有任何东西需要清，
    // 清了反而会把别处的缓存一并倒掉。两种情况的答案都是"我没有身份"，但
    // 该做的事不同。
    if (deps.storedToken) {
      deps.discardTheToken();
    }
    return null;
  }
}
