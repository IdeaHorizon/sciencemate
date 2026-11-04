"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type SSHConnectInput } from "@/lib/api";
import { useT } from "@/shared/i18n";
import { pushError } from "@/stores/notification";

export function SSHConnectForm({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const t = useT();
  const [values, setValues] = useState<SSHConnectInput>({ host: "" });
  const cache = useQueryClient();
  const connect = useMutation({
    mutationFn: () => api.connectSSHResource(projectId, values),
    onError: (error) => pushError(error.message),
    onSuccess: () => {
      setValues({ host: "" });
      void cache.invalidateQueries({ queryKey: ["project-resources", projectId] });
      onClose();
    },
  });
  return <form className="resource-editor" onSubmit={event => { event.preventDefault(); connect.mutate(); }}>
    <header><strong>{t({ zh: "连接 SSH 服务器", en: "Connect SSH server" })}</strong><span>{t({ zh: "连接并为当前项目登记此账号。", en: "Connect and register this account for your Project." })}</span></header>
    <div className="resource-editor-grid">
      <label>{t({ zh: "地址或 SSH 别名", en: "Host or SSH alias" })}<input required value={values.host} onChange={e => setValues({ ...values, host: e.target.value })} placeholder="node15 / 10.130.15.2" /></label>
      <label>{t({ zh: "用户", en: "User" })}<input value={values.user_name ?? ""} onChange={e => setValues({ ...values, user_name: e.target.value || undefined })} autoComplete="username" /></label>
      <label>{t({ zh: "端口", en: "Port" })}<input type="number" min={1} max={65535} value={values.port ?? ""} onChange={e => setValues({ ...values, port: e.target.value ? Number(e.target.value) : undefined })} placeholder={t({ zh: "沿用 SSH 配置，默认 22", en: "From SSH config or 22" })} /></label>
      <label>{t({ zh: "密码", en: "Password" })}<input type="password" value={values.password ?? ""} onChange={e => setValues({ ...values, password: e.target.value || undefined })} autoComplete="new-password" placeholder={t({ zh: "留空使用现有密钥", en: "Leave blank to use existing keys" })} /></label>
      <label>{t({ zh: "跳板", en: "Jump host" })}<input value={values.jump ?? ""} onChange={e => setValues({ ...values, jump: e.target.value || undefined })} placeholder={t({ zh: "可选，沿用现有 SSH 配置", en: "Optional; existing SSH config is used" })} /></label>
      <label>{t({ zh: "名称", en: "Name" })}<input value={values.name ?? ""} onChange={e => setValues({ ...values, name: e.target.value || undefined })} placeholder={t({ zh: "默认使用地址", en: "Defaults to host" })} /></label>
    </div>
    {connect.error && <p role="alert">{connect.error.message}</p>}
    <footer><button type="button" onClick={onClose} disabled={connect.isPending}>{t({ zh: "取消", en: "Cancel" })}</button><button disabled={connect.isPending || !values.host}>{connect.isPending ? t({ zh: "正在连接…", en: "Connecting…" }) : t({ zh: "连接", en: "Connect" })}</button></footer>
  </form>;
}

export function SSHResourceControls({ projectId, resourceId }: { projectId: string; resourceId: string }) {
  const t = useT();
  const cache = useQueryClient();
  const jobs = useQuery({
    queryKey: ["ssh-jobs", projectId, resourceId],
    queryFn: () => api.operateSSHResource(projectId, resourceId, { action: "jobs" }),
    refetchInterval: query => query.state.data?.jobs?.some(job => ["launching", "running"].includes(job.state)) ? 5000 : false,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["ssh-job", projectId, resourceId, selected],
    queryFn: () => api.operateSSHResource(projectId, resourceId, { action: "status", job_id: selected }),
    enabled: !!selected,
    refetchInterval: query => ["launching", "running"].includes(query.state.data?.state ?? "") ? 3000 : false,
  });
  const operation = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.operateSSHResource(projectId, resourceId, data),
    onError: (error) => pushError(error.message),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["project-resources", projectId] });
      void cache.invalidateQueries({ queryKey: ["ssh-jobs", projectId, resourceId] });
      void cache.invalidateQueries({ queryKey: ["ssh-job", projectId, resourceId] });
    },
  });
  return <div className="ssh-resource-controls">
    <div className="resource-actions">
      <button disabled={operation.isPending} onClick={() => operation.mutate({ action: "probe" })}>{t({ zh: "检查连接", en: "Check connection" })}</button>
      <button disabled={jobs.isFetching} onClick={() => { void jobs.refetch(); }}>{t({ zh: "刷新任务", en: "Refresh tasks" })}</button>
    </div>
    {(operation.error || jobs.error || detail.error) && <p role="alert">{(operation.error || jobs.error || detail.error)?.message}</p>}
    {operation.data?.cpu_count != null && <p>{operation.data.cpu_count} CPU · {operation.data.gpu_csv || t({ zh: "未观测到 NVIDIA GPU", en: "No NVIDIA GPU observation" })}</p>}
    {!jobs.isLoading && !jobs.error && !jobs.data?.jobs?.length && <p>{t({ zh: "尚无远程任务，可以让 Agent 在此服务器上执行任务。", en: "No remote tasks yet. Ask the agent to run a task on this server." })}</p>}
    {jobs.data?.jobs?.map(job => <div key={job.job_id} className="resource-actions">
      <button onClick={() => setSelected(job.job_id)}><code>{job.job_id.slice(0, 12)}</code> · {job.state}</button>
      {["launching", "running"].includes(job.state) && <button disabled={operation.isPending} onClick={() => operation.mutate({ action: "cancel", job_id: job.job_id })}>{t({ zh: "取消任务", en: "Cancel task" })}</button>}
    </div>)}
    {detail.data && <div><p>{detail.data.state} · {t({ zh: "退出码", en: "Exit code" })} {detail.data.exit_code ?? "…"}</p><pre style={{ maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap" }}>{detail.data.stdout}{detail.data.stderr}</pre></div>}
  </div>;
}
