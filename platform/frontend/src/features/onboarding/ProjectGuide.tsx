"use client";

/**
 * 第一次进一个项目时那一圈气泡。
 *
 * 和进门那一圈分开记（`project_guide_done`），因为它们教的是两件事：工作区那圈
 * 说"这台软件有哪几块"，这一圈说"一个课题在这里怎么走"。一个人可能在工作区里
 * 转了好几天才第一次建项目，那时再教才有用 —— 合成一个标记的话，这一圈会在他
 * 还没有项目的时候被"教过"掉。
 *
 * 它不挑项目：第一个项目里走一遍就够了，之后每个项目都一样。
 */

import { useCallback } from "react";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { GuidedTour } from "./GuidedTour";
import { PROJECT_STOPS } from "./lib/tour-stops";

export function ProjectGuide() {
  const preference = useInterfaceSettings();
  const done = useCallback(() => {
    if (preference.settings.project_guide_done) return;
    void preference.save({ ...preference.settings, project_guide_done: true });
  }, [preference]);

  if (!preference.ready || preference.settings.project_guide_done) return null;
  // 开场还没走完就不插话：一次只教一件事。
  if (!preference.settings.onboarding_done) return null;
  return <GuidedTour stops={PROJECT_STOPS} onDone={done} />;
}
