"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, ChevronUp, Wrench, X } from "lucide-react";
import { qk } from "@/lib/query/keys";
import { useLanguage, useT } from "@/shared/i18n";
import { pushError } from "@/stores/notification";
import { getSessionEnvironment, repairSessionEnvironment } from "../api/session-repository";
import { bannerModel, environmentSignature } from "../lib/environment";

/**
 * 会话顶部那条非模态的环境条幅（RFC_WORKER_START_IS_NOT_A_GATE §3.3 / §3.4）。
 *
 * 不是红字、不是弹窗挡路：一句人话 + 「修复」「这轮先这样跑」「详情」。挂在会话上不挂在消息上；
 * 修好之后下一次 ready 全绿就自动收起。「修复」把服务端按表算出的命令原文给人看、人点头才执行，
 * 输出回填到这里。同一个问题连续第二次（escalated）不许收起 —— 那时平台已经自动诊断过，详情里是事实。
 *
 * 判断全在 `../lib/environment.ts`（纯函数，有 node:test）；这里只画。
 */
export function SessionEnvironmentBanner({
  projectId, sessionId, enabled, canDrive,
}: { projectId: string; sessionId: string; enabled: boolean; canDrive: boolean }) {
  const t = useT();
  const lang = useLanguage();
  const queryClient = useQueryClient();
  const storageKey = `session-environment-dismissed:${sessionId}`;
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    try {
      setDismissed(window.localStorage.getItem(storageKey));
    } catch {
      setDismissed(null);
    }
  }, [storageKey]);

  const query = useQuery({
    queryKey: qk.sessionEnvironment(projectId, sessionId),
    queryFn: () => getSessionEnvironment(projectId, sessionId),
    enabled,
    // 有事的时候勤一点（修好之后要自己收起）；没事的时候一分钟一次就够。
    refetchInterval: (q) => ((q.state.data?.warnings?.length ?? 0) > 0 || q.state.data?.escalated ? 15_000 : 60_000),
  });

  const repair = useMutation({
    mutationFn: (input: { code: string; path: string | null }) => repairSessionEnvironment(projectId, sessionId, input),
    // 没执行成（409 没有这条提案 / 没权限 / 网络）要出声；执行了但返回码非零不是这里的失败，
    // 它是正常结果，回填在条幅里。
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "修复没能执行", en: "The repair could not be run" })),
    onSettled: () => queryClient.invalidateQueries({ queryKey: qk.sessionEnvironment(projectId, sessionId) }),
  });

  const model = useMemo(() => bannerModel(query.data, lang, dismissed), [query.data, lang, dismissed]);
  if (!enabled || !model) return null;

  const dismiss = () => {
    const signature = environmentSignature(query.data);
    try {
      window.localStorage.setItem(storageKey, signature);
    } catch {
      /* 存不下也只是下次再弹一次 */
    }
    setDismissed(signature);
  };

  const confirmAndRepair = () => {
    if (!model.repair) return;
    const ok = window.confirm(
      t({
        zh: "平台将以你的身份执行这条命令：\n\n{command}\n\n{changes}\n\n继续吗？",
        en: "The platform will run this command as you:\n\n{command}\n\n{changes}\n\nContinue?",
      }, { command: model.repair.command, changes: model.repair.changes }),
    );
    if (ok) repair.mutate({ code: model.repair.code, path: model.repair.path });
  };

  return (
    <aside className={`session-environment-banner tone-${model.tone}`} role="status" aria-live="polite">
      <div className="session-environment-main">
        <AlertTriangle size={14} />
        <div className="session-environment-text">
          <strong>{model.title}</strong>
          {model.lines.map((line) => <span key={line}>{line}</span>)}
          {repair.data && (
            <span className="session-environment-result">
              {repair.data.returncode === 0
                ? t({ zh: "已执行，返回码 0。下一轮起跑时会重新核一遍。", en: "Done, exit code 0. It will be re-checked when the next turn starts." })
                : t({ zh: "执行了，但返回码是 {rc}：{err}", en: "Ran, but the exit code was {rc}: {err}" }, {
                  rc: String(repair.data.returncode ?? "?"), err: repair.data.stderr || repair.data.stdout || "",
                })}
            </span>
          )}
          {repair.error && (
            <span className="session-environment-result">{repair.error instanceof Error ? repair.error.message : String(repair.error)}</span>
          )}
        </div>
        <div className="session-environment-actions">
          {model.repair && canDrive && (
            <button type="button" className="session-environment-repair" disabled={repair.isPending} onClick={confirmAndRepair}>
              <Wrench size={13} />{repair.isPending ? t({ zh: "正在修复…", en: "Repairing…" }) : t({ zh: "修复", en: "Repair" })}
            </button>
          )}
          <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
            {open ? <ChevronUp size={13} /> : <ChevronDown size={13} />}{t({ zh: "详情", en: "Details" })}
          </button>
          {!model.escalated && (
            <button type="button" onClick={dismiss} aria-label={t({ zh: "这轮先这样跑", en: "Carry on for now" })} title={t({ zh: "这轮先这样跑；问题变了会再提醒", en: "Carry on for now; you'll be told again if the problem changes" })}>
              <X size={13} />
            </button>
          )}
        </div>
      </div>
      {open && (
        <pre className="session-environment-details">
          {model.repair && `${t({ zh: "修复命令", en: "Repair command" })}: ${model.repair.command}\n\n`}
          {model.details.join("\n")}
        </pre>
      )}
    </aside>
  );
}
