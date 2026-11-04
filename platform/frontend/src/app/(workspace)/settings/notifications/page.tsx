"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bell, LockKeyhole } from "lucide-react";
import { useNotificationSettings } from "@/features/settings/useNotificationSettings";
import { api, type NotificationSettingsPayload } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Empty, Skeleton } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { useT } from "@/shared/i18n";

type EditableNotificationKey = "decision_required" | "run_failed";

export default function NotificationSettingsPage() {
  const t = useT();
  const queryClient = useQueryClient();
  const settings = useNotificationSettings();
  const save = useMutation({
    mutationFn: (payload: NotificationSettingsPayload) => api.saveNotificationSettings(payload),
    onSuccess: (saved) => {
      queryClient.setQueryData(qk.notificationSettings(), saved);
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "保存失败", en: "Notification preference could not be saved" })),
  });

  const update = (key: EditableNotificationKey, checked: boolean) => {
    if (!settings.data) return;
    save.mutate({
      decision_required: settings.data.decision_required,
      run_failed: settings.data.run_failed,
      budget_warning: settings.data.budget_warning,
      [key]: checked,
    });
  };
  const hasInApp = settings.data?.delivery_capabilities.includes("in_app") ?? false;

  return (
    <div className="settings-page notification-settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "通知", en: "Notifications" })}</h1><p>{t({ zh: "选择哪些事情需要提醒你。", en: "Choose what you want to be alerted about." })}</p></header>

      {settings.isLoading && !settings.data && <div className="settings-page-loading"><Skeleton height={210} /><Skeleton height={150} /></div>}
      {settings.isError && <Empty title={t({ zh: "无法加载通知设置", en: "Couldn't load notification settings" })} hint={t({ zh: "请稍后重试。", en: "Please try again later." })} />}

      {settings.data && (
        <>
          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "应用内提醒", en: "In-app alerts" })}</h2></div><small>{hasInApp ? t({ zh: "可用", en: "Available" }) : t({ zh: "暂不可用", en: "Unavailable" })}</small></div>
            <div className="settings-row-group">
              <label className="settings-row settings-toggle-row">
                <span><strong><Bell size={14} />{t({ zh: "需要你做决定", en: "Decision required" })}</strong><small>{t({ zh: "会话暂停、等你决定时提醒你。", en: "Alert when a session pauses for your decision." })}</small></span>
                <input type="checkbox" checked={settings.data.decision_required} disabled={!hasInApp || save.isPending} onChange={(event) => update("decision_required", event.target.checked)} />
              </label>
              <label className="settings-row settings-toggle-row">
                <span><strong><Bell size={14} />{t({ zh: "执行失败", en: "Run failed" })}</strong><small>{t({ zh: "执行出错或中断时提醒你。", en: "Alert when a run fails or is interrupted." })}</small></span>
                <input type="checkbox" checked={settings.data.run_failed} disabled={!hasInApp || save.isPending} onChange={(event) => update("run_failed", event.target.checked)} />
              </label>
              <div className="settings-row settings-readonly-row settings-disabled-row">
                <span><strong>{t({ zh: "预算告警", en: "Budget warning" })}</strong></span>
                <span>{t({ zh: "暂不支持", en: "Not supported yet" })}</span>
              </div>
            </div>
            <p className="settings-readonly-note">{t({ zh: "需要你决定的事项和执行失败的记录，始终会显示在对应会话里，不受这些开关影响。", en: "Decisions waiting for you and failed runs always appear in their session, whatever these switches are set to." })}</p>
            {!hasInApp && <p className="settings-readonly-note"><LockKeyhole size={13} />{t({ zh: "当前服务器不支持应用内提醒。", en: "This server does not support in-app alerts." })}</p>}
          </section>
        </>
      )}
    </div>
  );
}
