"use client";

import { Loader2 } from "lucide-react";

import { sayWhatIsHappening } from "@/features/update/say-what-is-happening";
import type { UpdatePhase } from "@/features/update/useUpdate";
import { useT } from "@/shared/i18n";

/**
 * 更新进行中的那一行：转圈 + 正在做什么 + 过了几秒。横幅和关于页都用它 —— 各画一份，
 * 两处会各自演化（关于页曾经只把按钮变灰，横幅换一行字，两边都「对」）。
 */
export function UpdateProgress({ phase, version, elapsedSeconds }: {
  phase: UpdatePhase; version: string | null | undefined; elapsedSeconds: number;
}) {
  const t = useT();
  const said = sayWhatIsHappening(phase, version, elapsedSeconds, t);
  if (!said) return null;
  return (
    <span className="update-progress" role="status" aria-live="polite">
      <Loader2 size={14} aria-hidden="true" className="is-spinning" />
      <span>{said}</span>
    </span>
  );
}
