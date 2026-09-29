"use client";

/**
 * 右键菜单 —— 列表项上那些**不该常驻**的操作（删除、归档…）的去处。
 *
 * wangd 2026-08-19：「现在还是没有删除 Session 或者删除 Project，我觉得这里
 * 可以加一个右键菜单。」
 *
 * ## 两条约束
 *
 * 1. **破坏性操作要确认，且确认要说清删的是哪一个** —— 列表里每行长得差不多，
 *    一句"确定删除吗"回答不了"删的是我选中的那个吗"。
 * 2. **打开菜单不等于选中它**：右键不导航、不切换当前会话，否则"我只是想看看
 *    能做什么"就变成了一次跳转。
 */

import { useEffect, useRef, useState } from "react";

export type ContextMenuItem = {
  label: string;
  onSelect: () => void;
  /** 破坏性：红色显示。配合 confirm 使用。 */
  danger?: boolean;
  /** 确认文案（应包含被操作对象的名字）；省略则不确认。 */
  confirm?: string;
  disabled?: boolean;
  disabledReason?: string;
};

export function useContextMenu() {
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  return {
    at,
    close: () => setAt(null),
    onContextMenu: (event: React.MouseEvent) => {
      event.preventDefault();
      // 外层通常是 <Link>：右键不该顺带导航过去。
      event.stopPropagation();
      setAt({ x: event.clientX, y: event.clientY });
    },
  };
}

export function ContextMenu({ at, onClose, items }: {
  at: { x: number; y: number } | null;
  onClose: () => void;
  items: ContextMenuItem[];
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!at) return;
    const dismiss = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) onClose();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    // capture：菜单之外任何一次按下都关掉它，包括落在别的可点元素上那次。
    document.addEventListener("mousedown", dismiss, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", dismiss, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [at, onClose]);

  if (!at) return null;
  const width = 210;
  const height = 12 + items.length * 30;
  return (
    <div
      ref={ref}
      className="context-menu"
      role="menu"
      // 贴着光标，但不许溢出视口 —— 屏幕边缘右键会让整条菜单看不见。
      style={{
        left: Math.max(4, Math.min(at.x, (globalThis.innerWidth ?? at.x) - width)),
        top: Math.max(4, Math.min(at.y, (globalThis.innerHeight ?? at.y) - height)),
      }}
    >
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          role="menuitem"
          className={item.danger ? "is-danger" : ""}
          disabled={item.disabled}
          title={item.disabled ? item.disabledReason : undefined}
          onClick={() => {
            onClose();
            if (item.confirm && !globalThis.confirm(item.confirm)) return;
            item.onSelect();
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
