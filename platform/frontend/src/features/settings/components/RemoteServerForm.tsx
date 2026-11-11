"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import type { RemoteServer, RemoteServerInput } from "@/lib/remote-runtime";
import { Button } from "@/shared/ui";
import { useT } from "@/shared/i18n";
import { pushError } from "@/stores/notification";

const EMPTY: RemoteServerInput = {
  name: "", host: "", username: "", port: null, auth_method: "config", identity_file: "",
  password: "", passphrase: "", remember_secret: false,
};

export function RemoteServerForm({ server, onDone, onCancel }: {
  server?: RemoteServer;
  onDone: (server: RemoteServer) => void;
  onCancel: () => void;
}) {
  const t = useT();
  const cache = useQueryClient();
  const [form, setForm] = useState<RemoteServerInput>(() => server
    ? { ...EMPTY, ...server, server_id: server.id } : EMPTY);
  const [operationId, setOperationId] = useState<string | null>(null);
  const options = useQuery({ queryKey: qk.remoteSSHOptions(), queryFn: () => api.remoteSSHOptions() });
  const start = useMutation({
    mutationFn: (input: RemoteServerInput) => api.connectRemoteServer(input),
    onSuccess: ({ operation_id }) => setOperationId(operation_id),
    onError: (error) => pushError(error.message),
  });
  const operation = useQuery({
    queryKey: qk.remoteOperation(operationId),
    queryFn: () => api.remoteConnectionOperation(operationId!),
    enabled: !!operationId,
    refetchInterval: (query) => query.state.data?.done ? false : 1000,
    retry: false,
  });
  const outcome = operation.data;
  const busy = start.isPending || (!!operationId && !outcome?.done && !operation.isError);

  useEffect(() => {
    if (outcome?.phase === "ready" && outcome.server) {
      void cache.invalidateQueries({ queryKey: qk.remoteServers() });
      void cache.invalidateQueries({ queryKey: qk.projects() });
      onDone(outcome.server);
    }
  }, [outcome, cache, onDone]);

  const connect = (fingerprint?: string) => {
    setOperationId(null);
    start.mutate({ ...form, ...(fingerprint ? { fingerprint } : {}) });
  };

  return <form className="runtime-connection-form" onSubmit={(event) => { event.preventDefault(); connect(); }}>
    <h2>{server ? t({ zh: "重新连接服务器", en: "Reconnect server" }) : t({ zh: "添加服务器", en: "Add server" })}</h2>
    <p>{t({ zh: "通过 SSH 自动安装 Linux 后端。服务器需要联网并支持 systemd 用户服务；配置完成后，关闭桌面不影响远程任务。", en: "Install the Linux backend over SSH. The server needs network access and systemd user services. Remote tasks continue after you close the desktop." })}</p>
    <fieldset disabled={busy}>
      <label>{t({ zh: "名称", en: "Name" })}<input required maxLength={120} value={form.name}
        onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder={t({ zh: "例如：实验室服务器", en: "e.g. Lab server" })} /></label>
      {!server && !!options.data?.aliases.length && <label>{t({ zh: "从本机 SSH 配置导入", en: "Import local SSH configuration" })}
        <select value="" onChange={(event) => setForm({ ...form, host: event.target.value, username: "", port: null,
          auth_method: "config", identity_file: "", name: form.name || event.target.value })}>
          <option value="">{t({ zh: "选择已配置的服务器…", en: "Choose a configured server…" })}</option>
          {options.data.aliases.map((alias) => <option key={alias} value={alias}>{alias}</option>)}
        </select>
      </label>}
      <label>{t({ zh: "服务器地址或 SSH 别名", en: "Server address or SSH alias" })}<input required readOnly={!!server}
        value={form.host} onChange={(event) => setForm({ ...form, host: event.target.value })} placeholder="192.168.1.10" /></label>
      <label>{t({ zh: "用户名", en: "Username" })}<input readOnly={!!server} value={form.username}
        onChange={(event) => setForm({ ...form, username: event.target.value })}
        placeholder={t({ zh: "留空使用 SSH 配置或当前用户名", en: "Leave blank to use SSH configuration or current username" })} /></label>
      <label>{t({ zh: "认证方式", en: "Authentication" })}<select value={form.auth_method}
        onChange={(event) => setForm({ ...form, auth_method: event.target.value as RemoteServerInput["auth_method"] })}>
        <option value="config">{t({ zh: "本机 SSH 配置", en: "Local SSH configuration" })}</option>
        <option value="key">{t({ zh: "SSH 密钥", en: "SSH key" })}</option>
        <option value="password">{t({ zh: "密码", en: "Password" })}</option>
      </select></label>
      {form.auth_method === "key" && <>
        <label>{t({ zh: "本机私钥文件", en: "Local private key file" })}
          <input required list="runtime-private-keys" value={form.identity_file} onChange={(event) => setForm({ ...form, identity_file: event.target.value })}
            placeholder="~/.ssh/id_ed25519" />
          <datalist id="runtime-private-keys">{options.data?.keys.map((key) => <option key={key} value={key} />)}</datalist>
        </label>
        <label>{t({ zh: "密钥口令（如有）", en: "Key passphrase (if any)" })}<input type="password" autoComplete="off"
          value={form.passphrase} onChange={(event) => setForm({ ...form, passphrase: event.target.value })} /></label>
      </>}
      {form.auth_method === "password" && <label>{t({ zh: "SSH 密码", en: "SSH password" })}
        <input type="password" required autoComplete="off" value={form.password}
          onChange={(event) => setForm({ ...form, password: event.target.value })} /></label>}
      {form.auth_method !== "config" && <label className="runtime-check">
        <input type="checkbox" checked={form.remember_secret} onChange={(event) => setForm({ ...form, remember_secret: event.target.checked })} />
        {t({ zh: "在本机加密记住密码或密钥口令，便于下次自动连接", en: "Remember the password or passphrase encrypted on this computer for automatic reconnection" })}
      </label>}
      <details><summary>{t({ zh: "高级设置", en: "Advanced" })}</summary>
        <label>{t({ zh: "SSH 端口", en: "SSH port" })}<input type="number" min={1} max={65535} readOnly={!!server}
          value={form.port ?? ""} placeholder="22" onChange={(event) => setForm({ ...form, port: event.target.value ? Number(event.target.value) : null })} /></label>
        <p>{t({ zh: "跳板机和其他连接选项沿用本机 SSH 配置。", en: "Jump hosts and other connection options follow your local SSH configuration." })}</p>
      </details>
    </fieldset>
    {busy && <p role="status">{outcome?.phase === "checking"
      ? t({ zh: "正在检查远程运行时…", en: "Checking the remote runtime…" })
      : t({ zh: "正在连接并配置服务器，首次安装可能需要几分钟…", en: "Connecting and configuring the server. First installation may take a few minutes…" })}</p>}
    {(outcome?.error || start.error || operation.error) && <p role="alert">{outcome?.error || start.error?.message || operation.error?.message}</p>}
    {outcome?.phase === "host_key" && outcome.fingerprint && <div className="runtime-host-key">
      <p>{t({ zh: "首次连接：请与服务器管理员提供的指纹核对。", en: "First connection: compare this fingerprint with the one provided by the server administrator." })}</p>
      <code>{outcome.fingerprint}</code>
      <Button type="button" size="sm" onClick={() => connect(outcome.fingerprint)}>{t({ zh: "指纹一致，信任并继续", en: "Fingerprint matches — trust and continue" })}</Button>
    </div>}
    <div className="settings-form-actions">
      <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>{t({ zh: "取消", en: "Cancel" })}</Button>
      <Button type="submit" size="sm" loading={busy} disabled={!form.name.trim() || !form.host.trim()}>{t({ zh: "连接并配置", en: "Connect and configure" })}</Button>
    </div>
  </form>;
}
