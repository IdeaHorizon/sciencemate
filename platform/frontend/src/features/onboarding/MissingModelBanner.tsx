"use client";

/**
 * 「还没有主推理模型」这个缺口，常驻在工作区顶上。
 *
 * ## 为什么必须有这一条
 *
 * 开场那几步全都能跳过 —— 那是对的，不该把人堵在门口。但「跳过模型」和「跳过
 * 选方向」的代价差着数量级：没有模型，任何会话都开不了工。允许跳过而不把代价
 * 说出口，就是让用户在一个跑不动的工作区里点来点去，然后以为是软件坏了。
 *
 * ## 判据是真实状态，不是"他跳过了"
 *
 * 问的是「主模型那个角色现在有没有人担着」（`coversTheMainRole`，和开场第一步同一
 * 个判据）。用户在设置页配好了、或者组织提供了一条而他挑中了它，这一条自己就消失；
 * 反过来把模型删了，它自己就回来。一个记着"他跳过了"的标记做不到这两件事里的任何一件。
 */

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useT } from "@/shared/i18n";
import { coversTheMainRole } from "./lib/steps";

/** 主模型那个角色有没有人担着。别处要画"能不能开工"也问这个。 */
export function useHasMainModel(): boolean | null {
  const roles = useQuery({ queryKey: qk.modelRoles(), queryFn: () => api.listModelRoles() });
  if (!roles.isSuccess) return null; // 还不知道 —— 别抢答，也别吓唬人
  return coversTheMainRole(roles.data);
}

export function MissingModelBanner() {
  const t = useT();
  const hasModel = useHasMainModel();
  if (hasModel !== false) return null;
  return (
    <div className="missing-model-banner" role="status">
      <TriangleAlert size={15} aria-hidden="true" />
      <span>{t({ zh: "还没有可用的模型 —— 会话开不了工。", en: "No model is available yet — sessions cannot run." })}</span>
      <Link href="/settings/models">{t({ zh: "去配置", en: "Configure" })}</Link>
    </div>
  );
}
