"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useT } from "@/shared/i18n";
import { pushError } from "@/stores/notification";
import { SSHConnectForm, SSHResourceControls } from "./SSHResourceControls";

export function ProjectSSHResources({ projectId, canManage }: { projectId: string; canManage: boolean }) {
  const t = useT();
  const [connecting, setConnecting] = useState(false);
  const cache = useQueryClient();
  const resources = useQuery({
    queryKey: qk.projectResources(projectId, true),
    queryFn: () => api.listProjectResources(projectId, { resourceType: "compute", includeDisabled: true }),
  });
  const change = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.updateProjectResource(projectId, id, { is_enabled: enabled }),
    onError: (error) => pushError(error.message),
    onSuccess: () => { void cache.invalidateQueries({ queryKey: ["project-resources", projectId] }); },
  });
  const ssh = resources.data?.filter(resource => resource.provider === "ssh") ?? [];
  return <section className="settings-compact-section" id="project-ssh-resources">
    <div className="settings-compact-heading">
      <div><h2>{t({ zh: "SSH 计算资源", en: "SSH compute resources" })}</h2>
        <p>{t({ zh: "也可以直接把连接信息告诉 Agent，它会自动连接并登记。", en: "You can also give the agent your connection details to connect and register automatically." })}</p></div>
      {canManage && !connecting && <button className="settings-primary-action" onClick={() => setConnecting(true)}>{t({ zh: "连接 SSH", en: "Connect SSH" })}</button>}
    </div>
    {connecting && <SSHConnectForm projectId={projectId} onClose={() => setConnecting(false)} />}
    {(resources.error || change.error) && <p role="alert">{(resources.error || change.error)?.message}</p>}
    {!resources.isLoading && !resources.error && ssh.length === 0 && <p>{t({ zh: "尚未连接 SSH 资源。", en: "No SSH resources connected." })}</p>}
    {ssh.map(resource => <details className="resource-row" key={resource.id}>
      <summary><strong>{resource.name}</strong><span>{resource.endpoint}</span><span>{resource.is_enabled ? resource.health_status : t({ zh: "已停用", en: "Disabled" })}</span></summary>
      <div className="resource-detail">
        <p>{t({ zh: "以连接账号的远程权限执行。", en: "Commands use the remote account's permissions." })}</p>
        {canManage && <><SSHResourceControls projectId={projectId} resourceId={resource.id} />
          <button disabled={change.isPending} onClick={() => change.mutate({ id: resource.id, enabled: !resource.is_enabled })}>{resource.is_enabled ? t({ zh: "停用", en: "Disable" }) : t({ zh: "启用", en: "Enable" })}</button></>}
      </div>
    </details>)}
  </section>;
}
