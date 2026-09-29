"use client";
import { create } from "zustand";
import { nanoid } from "nanoid";

export type NotificationKind = "error" | "success" | "info" | "warning";

export type Notification = {
  id: string;
  kind: NotificationKind;
  title?: string;
  text: string;
  /** Auto-dismiss after this many ms. 0 = sticky. */
  ttl?: number;
  /** 可选动作按钮（如失败 toast 上的「重试」）。点击后 toast 自动关闭。 */
  action?: { label: string; onClick: () => void };
};

type NotificationStore = {
  items: Notification[];
  push: (n: Omit<Notification, "id">) => string;
  dismiss: (id: string) => void;
  clear: () => void;
};

export const useNotifications = create<NotificationStore>((set) => ({
  items: [],
  push: (n) => {
    const id = nanoid();
    set((s) => ({ items: [...s.items, { ...n, id }] }));
    if (n.ttl && n.ttl > 0) {
      setTimeout(() => {
        set((s) => ({ items: s.items.filter((i) => i.id !== id) }));
      }, n.ttl);
    }
    return id;
  },
  dismiss: (id) => set((s) => ({ items: s.items.filter((i) => i.id !== id) })),
  clear: () => set({ items: [] }),
}));

/** Convenience helpers for the most common cases. */
export function pushError(text: string, title?: string): string {
  return useNotifications.getState().push({ kind: "error", text, title, ttl: 6000 });
}

/**
 * ⚠️ 弹一条"成功"之前，先回答一个问题：**屏幕上看不出来吗？**
 *
 * wangd 2026-08-20：「这种弹窗一点点用都没有…我记得还有个什么 you are now
 * session owner，这简直太蠢了…啥是 session owner 啊？」当时全站 21 条成功
 * 提示里，绝大多数在复述同一屏上已经发生的事：连接存好了（那一行就地变了）、
 * 项目建好了（已经跳进去了）、会话改名了（侧栏立刻变了）、拿到驾驶权了
 * （那是打字时自动发生的副作用，而且 "session driver" 是内部词）。
 *
 * 判据（和 `presentChatFailure` 那条同源）：
 *
 *   - 结果在同一屏上立刻可见 → **不要弹**。toast 复述屏幕 = 纯噪音。
 *   - 有一个屏幕上看不见的副作用（别处的登录被登出了 / 这一轮仍用旧模型 /
 *     执行进程不在线所以排队了）→ 值得弹，而且要把那个副作用说出来。
 *   - 用户自己在通知设置里订阅的（run 完成 / 需要决策）→ 那是他要的，照发。
 *
 * 失败不适用这条：`pushError` 该弹就弹 —— 失败本来就没有"屏幕上已经写着"。
 */
export function pushSuccess(text: string, title?: string): string {
  return useNotifications.getState().push({ kind: "success", text, title, ttl: 3000 });
}

export function pushInfo(text: string, title?: string): string {
  return useNotifications.getState().push({ kind: "info", text, title, ttl: 4000 });
}

/**
 * 一条**说清了原因**的失败 toast，可带动作按钮（如「重试」）。
 * ttl 比普通报错长：用户要读完原因、可能还要点按钮。
 */
export function pushFailure(
  text: string,
  title: string,
  action?: { label: string; onClick: () => void },
): string {
  return useNotifications.getState().push({ kind: "error", text, title, action, ttl: 12000 });
}
