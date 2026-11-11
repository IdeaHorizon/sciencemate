"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Laptop, Plus, Server } from "lucide-react";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import type { RemoteServer } from "@/lib/remote-runtime";
import { RemoteServerForm } from "@/features/settings/components/RemoteServerForm";
import { ModelConnections, type ModelsClient } from "@/features/settings/components/ModelConnections";
import { Button } from "@/shared/ui";
import { useT } from "@/shared/i18n";

function RuntimeModels({ server }: { server: RemoteServer }) {
  const t = useT();
  const client = useMemo<ModelsClient>(() => api.remoteModels(server.id), [server.id]);
  return <div className="runtime-models">
    <h3>{t({ zh: "{name} 上的模型与密钥", en: "Models and credentials on {name}" }, { name: server.name })}</h3>
    <p>{t({ zh: "远程 Agent 使用保存在这台服务器上的模型配置。请使用服务器能直接访问的模型地址。", en: "Remote agents use model configuration stored on this server. Use model endpoints reachable directly from the server." })}</p>
    <ModelConnections client={client} keys={{ backends: qk.runtimeModels(server.id), roles: qk.runtimeModelRoles(server.id) }}
      canSelect canAdd newConnectionScope={t({ zh: "这台个人服务器", en: "This personal server" })} />
  </div>;
}

export default function RuntimeSettingsPage() {
  const t = useT();
  const cache = useQueryClient();
  const servers = useQuery({ queryKey: qk.remoteServers(), queryFn: () => api.listRemoteServers() });
  const [editing, setEditing] = useState<RemoteServer | "new" | null>(null);
  const [models, setModels] = useState<string | null>(null);
  const operation = useMutation({
    mutationFn: async ({ server, action }: { server: RemoteServer; action: "check" | "disconnect" | "remove" }) => {
      if (action === "check") await api.checkRemoteServer(server.id);
      if (action === "disconnect") await api.disconnectRemoteServer(server.id);
      if (action === "remove") await api.removeRemoteServer(server.id);
    },
    onSuccess: (_, { server, action }) => {
      if (action === "disconnect" && models === server.id) setModels(null);
      void cache.invalidateQueries({ queryKey: qk.remoteServers() });
    },
  });
  return <div className="settings-page runtime-settings">
    <header className="settings-page-header">
      <h1>{t({ zh: "运行环境", en: "Runtime environments" })}</h1>
      <p>{t({ zh: "管理后端运行的位置。新建项目时选择本机或服务器，再选择对应机器上的根文件夹。", en: "Manage where the backend runs. Choose a computer and its root folder when creating a project." })}</p>
    </header>
    <section className="runtime-card"><h2><Laptop size={17} />{t({ zh: "本机（默认）", en: "This computer (default)" })}</h2>
      <p>{t({ zh: "项目和 Agent 在当前电脑上运行。", en: "Projects and agents run on this computer." })}</p>
    </section>
    {servers.data?.items.map((server) => <section className="runtime-card" key={server.id}>
      <h2><Server size={17} />{server.name}<small>{server.connected ? t({ zh: "已连接", en: "Connected" }) : t({ zh: "未连接", en: "Disconnected" })}</small></h2>
      <p><code>{server.username ? `${server.username}@` : ""}{server.host}{server.port ? `:${server.port}` : ""}</code> · {server.version}</p>
      <p>{t({ zh: "项目和运行记录保存在服务器上。断开连接或关闭桌面不会停止远程任务。", en: "Projects and run records live on the server. Disconnecting or closing the desktop does not stop remote tasks." })}</p>
      <div className="runtime-actions">
        <Button size="sm" disabled={operation.isPending} onClick={() => operation.mutate({ server, action: "check" })}>{t({ zh: "连接检查", en: "Check connection" })}</Button>
        <Button size="sm" variant="ghost" onClick={() => setEditing(server)}>{t({ zh: "重新配置", en: "Reconnect" })}</Button>
        <Button size="sm" variant="ghost" onClick={() => setModels(models === server.id ? null : server.id)}>{t({ zh: "模型与密钥", en: "Models & credentials" })}</Button>
        <Button size="sm" variant="ghost" disabled={operation.isPending} onClick={() => operation.mutate({ server, action: "disconnect" })}>{t({ zh: "断开连接", en: "Disconnect" })}</Button>
        <Button size="sm" variant="ghost" disabled={operation.isPending} onClick={() => operation.mutate({ server, action: "remove" })}>{t({ zh: "移除连接", en: "Remove connection" })}</Button>
      </div>
      {models === server.id && <RuntimeModels server={server} />}
    </section>)}
    {(servers.error || operation.error) && <p role="alert">{servers.error?.message || operation.error?.message}</p>}
    {operation.isSuccess && <p role="status">{t({ zh: "操作完成。", en: "Done." })}</p>}
    {editing ? <RemoteServerForm key={editing === "new" ? "new" : editing.id}
      server={editing === "new" ? undefined : editing} onCancel={() => setEditing(null)}
      onDone={(server) => { setEditing(null); setModels(server.id); }} />
      : <Button size="sm" iconLeft={<Plus size={14} />} onClick={() => setEditing("new")}>{t({ zh: "添加服务器", en: "Add server" })}</Button>}
  </div>;
}
