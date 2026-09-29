"use client";

/**
 * 「设置 → 模型与密钥」—— 这台电脑上的模型。
 *
 * 表单、角色、连接列表都在 `ModelConnections`（组织那边的「组织 → 设置 → 模型」用的是同一块，
 * 只是问的是那个组织的服务器）。这里只说：问本机后端，谁都能加自己的连接。
 */

import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { canSelectModels, useAuth } from "@/features/auth";
import { managedScopeLabel } from "@/features/settings/lib/model-actions";
import { ModelConnections, type ModelsClient } from "@/features/settings/components/ModelConnections";
import { useT, useLanguage } from "@/shared/i18n";

//: 本机的那一份。角色目录来自 API（`api.listModelRoles()`）—— 前端不枚举角色。
const THIS_MACHINE: ModelsClient = {
  list: () => api.listModelBackends(),
  save: (data, id) => api.saveModelBackend(data, id),
  remove: (id) => api.deleteModelBackend(id),
  probe: (id) => api.probeModelBackend(id),
  setEnabled: (id, isEnabled) => api.setModelBackendEnabled(id, isEnabled),
  roles: () => api.listModelRoles(),
  setRoleDefault: (id, role) => api.setRoleDefaultBackend(id, role),
};

export default function ModelSettingsPage() {
  const t = useT();
  const lang = useLanguage();
  const { user } = useAuth();
  return (
    <div className="settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "模型与密钥", en: "Models & credentials" })}</h1><p>{t({ zh: "选新会话建立时冻结进去的那个模型，并查看你这个角色能看到的网关连接。", en: "Choose the model captured by new Sessions and inspect the gateway connections your role can access." })}</p></header>
      <ModelConnections
        client={THIS_MACHINE}
        keys={{ backends: qk.modelBackends(), roles: qk.modelRoles() }}
        canSelect={canSelectModels(user)}
        // 研究员也能加：后端 POST /model-backends 把新连接存进调用者自己管得着的 scope。
        canAdd
        // 新建的连接落在你自己管得着的那个 scope —— 把后端给过的治理 scope 翻成人话，不另算一遍。
        newConnectionScope={managedScopeLabel(user, lang)}
      />
    </div>
  );
}
