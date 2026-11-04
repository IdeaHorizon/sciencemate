"use client";

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { authSlots, useAuth } from "@/features/auth";
import { api } from "@/lib/api";
import { Button } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { useT } from "@/shared/i18n";

export default function ProfileSettingsPage() {
  const t = useT();
  const { user, updateUser } = useAuth();
  const ChangePassword = authSlots.changePassword;
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");

  useEffect(() => setDisplayName(user?.display_name ?? ""), [user?.display_name]);


  const profile = useMutation({
    mutationFn: () => api.updateCurrentUser({ display_name: displayName.trim() }),
    onSuccess: (saved) => {
      updateUser(saved);
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "保存失败", en: "Profile could not be updated" })),
  });


  const profileDirty = Boolean(user && displayName.trim() && displayName.trim() !== user.display_name);

  return (
    <div className="settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "账户与安全", en: "Account & security" })}</h1></header>

      <section className="settings-compact-section">
        <div className="settings-compact-heading"><div><h2>{t({ zh: "个人资料", en: "Profile" })}</h2></div><Button size="sm" variant="primary" disabled={!profileDirty} loading={profile.isPending} onClick={() => profile.mutate()}>{t({ zh: "保存", en: "Save" })}</Button></div>
        <div className="settings-row-group">
          <label className="settings-row"><span><strong>{t({ zh: "显示名称", en: "Display name" })}</strong><small>{t({ zh: "其他成员看到的你的名字。", en: "The name other members see." })}</small></span><input value={displayName} maxLength={100} disabled={profile.isPending} onChange={(event) => setDisplayName(event.target.value)} /></label>
          <div className="settings-row settings-readonly-row"><span><strong>{t({ zh: "邮箱", en: "Email" })}</strong><small>{t({ zh: "用于登录，不能在这里修改。", en: "Used to sign in. It can't be changed here." })}</small></span><span>{user?.email ?? "—"}</span></div>
        </div>
      </section>

      {ChangePassword && <ChangePassword />}
    </div>
  );
}
