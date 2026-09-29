"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bell, LockKeyhole } from "lucide-react";
import { roleLabel, useAuth } from "@/features/auth";
import { useNotificationSettings } from "@/features/settings/useNotificationSettings";
import { api, type NotificationSettingsPayload } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { Empty, Skeleton } from "@/shared/ui";
import { pushError, pushSuccess } from "@/stores/notification";
import { useT, useLanguage } from "@/shared/i18n";

type EditableNotificationKey = "decision_required" | "run_failed" | "run_completed";

export default function NotificationSettingsPage() {
  const t = useT();
  const lang = useLanguage();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const settings = useNotificationSettings();
  const save = useMutation({
    mutationFn: (payload: NotificationSettingsPayload) => api.saveNotificationSettings(payload),
    onSuccess: (saved) => {
      queryClient.setQueryData(qk.notificationSettings(), saved);
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "通知偏好没能保存", en: "Notification preference could not be saved" })),
  });

  const update = (key: EditableNotificationKey, checked: boolean) => {
    if (!settings.data) return;
    save.mutate({
      decision_required: settings.data.decision_required,
      run_failed: settings.data.run_failed,
      run_completed: settings.data.run_completed,
      budget_warning: settings.data.budget_warning,
      [key]: checked,
    });
  };
  const hasInApp = settings.data?.delivery_capabilities.includes("in_app") ?? false;

  return (
    <div className="settings-page notification-settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "通知", en: "Notifications" })}</h1><p>{t({ zh: "选哪些研究事件除了记录之外还立刻在应用内提醒你。会话和决策的记录不受这里影响，一直都看得到。", en: "Choose which canonical research events also produce an immediate in-app alert. Session and Decision records remain visible regardless." })}</p></header>

      {settings.isLoading && !settings.data && <div className="settings-page-loading"><Skeleton height={210} /><Skeleton height={150} /></div>}
      {settings.isError && <Empty title={t({ zh: "读不到通知设置", en: "Notification settings unavailable" })} hint={t({ zh: "服务端没有返回你存过的通知偏好。", en: "The server did not return your saved notification preferences." })} />}

      {settings.data && (
        <>
          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "应用内即时提醒", en: "Immediate in-app alerts" })}</h2><p>{t({ zh: "这几个开关只管会话事件之后的即时提醒，不会藏起任何正式记录。", en: "These switches control transient alerts after live Session events. They do not hide authoritative records." })}</p></div><small>{hasInApp ? t({ zh: "可用", en: "Available" }) : t({ zh: "暂不可用", en: "Unavailable" })}</small></div>
            <div className="settings-row-group">
              <label className="settings-row settings-toggle-row">
                <span><strong><Bell size={14} />{t({ zh: "需要你拍板", en: "Decision required" })}</strong><small>{t({ zh: "一轮跑到需要你做研究决策而停下来时提醒你。", en: "Alert when a live Run pauses for an authoritative research Decision." })}</small></span>
                <input type="checkbox" checked={settings.data.decision_required} disabled={!hasInApp || save.isPending} onChange={(event) => update("decision_required", event.target.checked)} />
              </label>
              <label className="settings-row settings-toggle-row">
                <span><strong><Bell size={14} />{t({ zh: "执行失败", en: "Run failed" })}</strong><small>{t({ zh: "这一轮以失败、报错或失联收场时提醒你。", en: "Alert when the Run terminal state is failed, errored, or stale-unknown." })}</small></span>
                <input type="checkbox" checked={settings.data.run_failed} disabled={!hasInApp || save.isPending} onChange={(event) => update("run_failed", event.target.checked)} />
              </label>
              <label className="settings-row settings-toggle-row">
                <span><strong><Bell size={14} />{t({ zh: "执行完成", en: "Run completed" })}</strong><small>{t({ zh: "这一轮正常跑完时提醒你。", en: "Alert when the live Run records a completed terminal state." })}</small></span>
                <input type="checkbox" checked={settings.data.run_completed} disabled={!hasInApp || save.isPending} onChange={(event) => update("run_completed", event.target.checked)} />
              </label>
              <div className="settings-row settings-readonly-row settings-disabled-row">
                <span><strong>{t({ zh: "预算告警", en: "Budget warning" })}</strong><small>{t({ zh: "这个偏好服务端记下了，但当前的事件流里还没有预算告警这类事件可供订阅。", en: "The preference is recorded by the service, but the current event stream exposes no canonical budget-warning event to consume." })}</small></span>
                <span><strong>只读 · {settings.data.budget_warning ? t({ zh: "开", en: "on" }) : t({ zh: "关", en: "off" })}</strong><small>{t({ zh: "没有可用的送达方式", en: "No safe delivery source" })}</small></span>
              </div>
            </div>
            {!hasInApp && <p className="settings-readonly-note"><LockKeyhole size={13} />{t({ zh: "服务端没有报出这项能力，所以应用内提醒是关着的。", en: "In-app delivery is disabled because the service did not advertise the capability." })}</p>}
          </section>

          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "一定会显示的那几类", en: "Authoritative attention" })}</h2><p>{t({ zh: "关乎安全的记录不受通知偏好影响，永远显示。", en: "Safety-critical records are never filtered by notification preferences." })}</p></div></div>
            <div className="settings-row-group">
              <div className="settings-row settings-readonly-row"><span><strong>{t({ zh: "决策、授权与审批", en: "Decisions, permissions, and approvals" })}</strong><small>{t({ zh: "只要你的实际权限够，它们始终在所属会话里看得到。", en: "Remain visible inside their owning Session wherever your effective permissions allow access." })}</small></span><span><strong>{t({ zh: "永远可见", en: "Always visible" })}</strong><small>{t({ zh: "来源 · 落库的决策状态", en: "Source · persisted Decision state" })}</small></span></div>
              <div className="settings-row settings-readonly-row"><span><strong>{t({ zh: "失败、重试与警告", en: "Failures, retries, and warnings" })}</strong><small>{t({ zh: "始终挂在对应的执行记录上，便于排查与恢复。", en: "Remain attached to their canonical Run history for inspection and recovery." })}</small></span><span><strong>{t({ zh: "永远可见", en: "Always visible" })}</strong><small>{t({ zh: "来源 · 落库的运行状态", en: "Source · persisted Run state" })}</small></span></div>
            </div>
          </section>

          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "外部送达", en: "External delivery" })}</h2><p>{t({ zh: "当前服务只报出了应用内送达。", en: "The current service advertises in-app delivery only." })}</p></div></div>
            <div className="settings-row-group notification-unavailable-group" aria-disabled="true">
              <div className="settings-row settings-disabled-row"><span><strong>{t({ zh: "邮件通知", en: "Email notifications" })}</strong><small>{t({ zh: "需要一个已验证的投递服务和收件约定。", en: "Requires a verified delivery service and destination contract." })}</small></span><span>{t({ zh: "暂不可用", en: "Unavailable" })}</span></div>
              <div className="settings-row settings-disabled-row"><span><strong>{t({ zh: "桌面通知", en: "Desktop notifications" })}</strong><small>{t({ zh: "需要应用授权和已注册的设备投递约定。", en: "Requires an app permission and registered device delivery contract." })}</small></span><span>{t({ zh: "暂不可用", en: "Unavailable" })}</span></div>
              <div className="settings-row settings-disabled-row"><span><strong>{t({ zh: "定时摘要", en: "Scheduled digest" })}</strong><small>{t({ zh: "需要时区、时间表和收件地址。", en: "Requires a timezone, schedule, and delivery destination." })}</small></span><span>{t({ zh: "暂不可用", en: "Unavailable" })}</span></div>
            </div>
            <p className="settings-readonly-note"><LockKeyhole size={13} /> Read-only for {roleLabel(user?.role, lang)} because no external delivery capability is exposed.</p>
          </section>
        </>
      )}
    </div>
  );
}
