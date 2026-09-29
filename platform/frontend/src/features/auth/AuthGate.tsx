"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { FlaskConical } from "lucide-react";
import { useHasCapability } from "@/features/capabilities";
import { authSlots } from "./slots";
import { useAuth } from "./AuthProvider";
import { useT } from "@/shared/i18n";

/**
 * 登录这道门只在**有身份这回事**的服务器上存在。
 *
 * 个人档装在自己的电脑上：没有账号、没有登录、也没有"会话过期"。这道门在
 * 那里出现，就是在教用户学一个他不需要的概念 —— 而这正是这一版要消灭的东西。
 *
 * 判据来自服务器的能力集合（`/capabilities`），不是前端自己猜的档位：一台真
 * 有别人的服务器必须照旧要身份。能力还没问到时两边都不做，显示"正在打开"。
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const t = useT();
  const { ready, authenticated, user } = useAuth();
  const needsAuthentication = useHasCapability("auth");
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => {
    if (needsAuthentication !== true) return;
    if (!ready || authenticated) return;
    // 登录页在哪由发行登记（核心不写死那一页的地址：公开树里没有它）。
    const signInPath = authSlots.signInPath;
    if (!signInPath) return;
    const next = pathname && pathname !== "/" ? `?next=${encodeURIComponent(pathname)}` : "";
    router.replace(`${signInPath}${next}`);
  }, [needsAuthentication, ready, authenticated, pathname, router]);

  if (needsAuthentication === false) return children;

  // 拿着一次性口令的人先改密码，再进工作区。盖住整个工作区而不是提醒一句：
  // 只要那个口令还能用，这个账号做的事就说不清是谁做的（见 MustChangePassword）。
  const MustChangePassword = authSlots.mustChangePassword;
  if (ready && authenticated && user?.must_change_password && MustChangePassword) return <MustChangePassword />;

  if (needsAuthentication === null || !ready || !authenticated) {
    return (
      <main className="auth-loading" aria-label={t({ zh: "正在恢复你的会话", en: "Restoring your session" })}>
        <FlaskConical size={19} />
        <span>{ready
          ? t({ zh: "正在打开登录…", en: "Opening sign in…" })
          : t({ zh: "正在恢复工作区…", en: "Restoring workspace…" })}</span>
      </main>
    );
  }

  return children;
}

