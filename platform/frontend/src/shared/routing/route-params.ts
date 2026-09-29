"use client";

import { usePathname } from "next/navigation";

/**
 * 动态段的取值从**地址栏**解析，不用 `useParams()`。
 *
 * ## 为什么不能用 useParams（2026-09-05 真机点验）
 *
 * 静态导出下每个动态段只有一份外壳（`/projects/_/sessions/_`），`_` 被烤进了
 * 那份 HTML 的 RSC 载荷。浏览器打开真实地址时 React 先按载荷水合 —— 这一帧里
 * `useParams()` 返回的是 `_`。于是首帧就发出了
 *
 *     GET /api/v1/projects/_/sessions/_            → 404
 *     GET /api/v1/projects/_/sessions/_/messages   → 404
 *
 * 四个 404，然后（水合完成后）才用真 id 再问一次。用户看到的是一次闪烁的
 * 「Session unavailable」，日志里是一串指不到病因的 404。
 *
 * `usePathname()` 不一样：它读的是浏览器的地址，第一帧就是真的。所以这里按
 * 路由里的**字面量段**去取它后面那一段 —— 全站只有三个动态段，写清楚比让每个
 * 页面各自猜要好。
 */
function useSegmentAfter(literal: string): string {
  const pathname = usePathname() ?? "";
  const parts = pathname.split("/").filter(Boolean);
  const at = parts.indexOf(literal);
  const value = at >= 0 ? parts[at + 1] : undefined;
  if (!value) return "";
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** `/projects/<id>/…` */
export function useProjectId(): string {
  return useSegmentAfter("projects");
}

/** `/projects/<id>/sessions/<sessionId>` */
export function useSessionId(): string {
  return useSegmentAfter("sessions");
}

/** `/projects/<id>/artifacts/<aId>` */
export function useArtifactId(): string {
  return useSegmentAfter("artifacts");
}
