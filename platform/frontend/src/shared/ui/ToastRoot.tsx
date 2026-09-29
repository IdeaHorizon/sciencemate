"use client";
import { X } from "lucide-react";
import { useNotifications } from "@/stores/notification";
import { cn } from "./cn";
import { useT } from "@/shared/i18n";

/**
 * Toast root — render once near the app root. Reads from the notification
 * Zustand store and renders stacked toasts in a fixed-position container.
 */
export function ToastRoot() {
  const t = useT();
  const items = useNotifications((s) => s.items);
  const dismiss = useNotifications((s) => s.dismiss);

  if (items.length === 0) return null;

  return (
    <div className="ui-toast-root" role="status" aria-live="polite">
      {items.map((n) => (
        <div key={n.id} className={cn("ui-toast", `ui-toast-${n.kind}`)}>
          <div className="ui-toast-body">
            {n.title && <div className="ui-toast-title">{n.title}</div>}
            <div className="ui-toast-text">{n.text}</div>
            {n.action && (
              <button
                type="button"
                className="ui-toast-action"
                onClick={() => {
                  n.action?.onClick();
                  dismiss(n.id);
                }}
              >
                {n.action.label}
              </button>
            )}
          </div>
          <button
            className="ui-toast-dismiss"
            onClick={() => dismiss(n.id)}
            aria-label={t({ zh: "关掉这条提示", en: "Dismiss notification" })}
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
