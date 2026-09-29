"use client";

import { Lock } from "lucide-react";

import { useT } from "@/shared/i18n";

/**
 * Inline badge shown on artifacts that have been frozen via freeze_artifact
 * The artifact's extra_data carries `frozen=true`, `frozen_at`,
 * `frozen_by`.
 */
export function FrozenLockBadge({
  extraData,
  size = 11,
}: {
  extraData: Record<string, unknown> | null | undefined;
  size?: number;
}) {
  const t = useT();
  if (!extraData || !extraData.frozen) return null;
  const frozenAt = typeof extraData.frozen_at === "string" ? extraData.frozen_at : "";
  const frozenBy = typeof extraData.frozen_by === "string" ? extraData.frozen_by : "";
  const tooltip = [
    t({ zh: "已冻结 —— 不可更改", en: "Frozen — immutable" }),
    frozenBy && `by: ${frozenBy}`,
    frozenAt && `at: ${frozenAt.slice(0, 19)}`,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span className="frozen-lock-badge" title={tooltip}>
      <Lock size={size} /> frozen
    </span>
  );
}
