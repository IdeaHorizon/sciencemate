"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * 取一条资讯的配图，返回一个可以直接放进 `<img src>` 的地址。
 *
 * ## 为什么不能直接 `<img src={代理地址}>`
 *
 * 代理端点是**要鉴权**的（`Depends(get_current_user)`），而浏览器给
 * `<img>` 发的请求**带不了 Authorization 头** —— 这个平台的令牌在内存里，
 * 不是 cookie。所以第一版那样写的结果是：`<img>` 元素渲染出来了、
 * `naturalWidth` 是 0，一张图都不显示，而且控制台里只有一条 401。
 *
 * 只有真把部署起起来、在浏览器里看 DOM 才发现 —— typecheck、单测、build
 * 全绿，后端直接 curl 那个端点也是 200。
 *
 * ## 为什么不把令牌放进 URL
 *
 * `?token=…` 是最省事的改法，但 URL 会进 access log、进 Referer、进浏览器
 * 历史。令牌不进 URL 是这个仓库的既有纪律。
 *
 * ## 为什么 blob 不会拖慢重复访问
 *
 * 代理响应带 `Cache-Control: private, max-age=86400`，所以第二次 `fetch`
 * 命中浏览器缓存，不会再打后端、更不会再打出版商。
 */
export function useFeedImage(itemId: string, enabled = true): string | null {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      // 切到列表（不显示图）时清掉手里的 blob 地址 —— 否则残留的地址已经
      // 在上一次的 cleanup 里被 revoke 掉了，切回图卡的那一瞬间 <img> 会去
      // 加载一个已失效的 URL、触发 onError，把 imageBroken 永久点成 true，
      // 图就再也回不来了。清空让它先不渲染 <img>，等这次 fetch 拿到新 blob。
      setObjectUrl(null);
      return;
    }
    let revoked = false;
    let created: string | null = null;

    void (async () => {
      try {
        const response = await api.fetchWithAuth(api.feedImageUrl(itemId));
        if (!response.ok) return;
        const blob = await response.blob();
        if (revoked) return;
        created = URL.createObjectURL(blob);
        setObjectUrl(created);
      } catch {
        // 取不回来就当没有图。**不显示碎图标** —— 大多数条目本来就没有配图，
        // 一个碎图会让用户以为是平台坏了。
      }
    })();

    return () => {
      revoked = true;
      // 组件卸下时释放，否则翻几页就攒下几十个 blob 不回收。
      if (created) URL.revokeObjectURL(created);
    };
  }, [itemId, enabled]);

  return objectUrl;
}
