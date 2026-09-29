"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { FlaskConical } from "lucide-react";
import { authSlots, useAuth } from "@/features/auth";
import { useHasCapability } from "@/features/capabilities";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { resolveDefaultLanding } from "@/features/settings/lib/default-landing";
import { api } from "@/lib/api";
import { useT } from "@/shared/i18n";

export function PreferenceLanding() {
  const t = useT();
  const router = useRouter();
  const auth = useAuth();
  const preference = useInterfaceSettings();
  const needsAuthentication = useHasCapability("auth");

  useEffect(() => {
    // 「没登录就去登录页」只在有登录这回事的服务器上成立。个人档里永远没有
    // token，这一句会把每一次打开都送到一个不该存在的页面 —— 2026-09-05 真机
    // 点验就是这么撞上的：零配置起服务，打开根路径，看到的是 Sign in。
    if (needsAuthentication === null) return;
    if (needsAuthentication && !auth.ready) return;
    if (needsAuthentication && !auth.authenticated) {
      // 登录页在哪由发行登记；核心不写死那一页的地址。
      if (authSlots.signInPath) router.replace(authSlots.signInPath);
      return;
    }
    if (!preference.ready) return;
    let active = true;
    // 没有模型**不再**改变去哪：开场从一个独占整屏的向导（/setup）改成了叠在
    // 工作区上的一层（features/onboarding）。理由是 2026-09-17 走查的原话 ——
    // 「上来就是一个空白页面」：用户还没看见产品，就被要求先做决定。
    //
    // 「没有模型」这件事一点没被藏起来，只是换了个说法：工作区顶上那条缺口提示
    // 和开场第一步都问同一个真实状态（有没有 ready 的连接）。
    void resolveDefaultLanding(
      preference.settings.default_landing,
      async () => (await api.listRuns(undefined, 1)).items[0] ?? null,
    ).then((destination) => { if (active) router.replace(destination); });
    return () => { active = false; };
  }, [needsAuthentication, auth.authenticated, auth.ready, preference.ready, preference.settings.default_landing, router]);

  return <main className="auth-loading" aria-label={t({ zh: "正在打开你的工作区", en: "Opening your workspace" })}><FlaskConical size={19} /><span>{t({ zh: "正在打开工作区…", en: "Opening workspace…" })}</span></main>;
}
