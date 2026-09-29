"use client";

/**
 * 模型连接与模型角色 —— 本机（「设置 → 模型与密钥」）和一个组织（「组织 → 设置 → 模型」）
 * 共用这一块。
 *
 * 两处问的是同一件事（这些会话用哪个模型、那些连接好不好），差别只在**问谁**和**谁能改**：
 * 本机问本机后端；组织问那个组织的服务器（每一问点名那条连接，`proApi.organisation(id).models`）。
 * 组织项目跑在服务器上、用服务器上的模型 —— 本机配的模型和它无关。所以这一块不按页面抄
 * 两份：抄两份就是两份各自演化的表单，而分叉不报错（`RFC_ORGANISATION_PAGE_20260923` E 批）。
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, LockKeyhole, Plus, X } from "lucide-react";
import {
  type ModelBackend,
  type ModelRole,
  type SaveModelBackendRequest,
} from "@/lib/api";
import { modelBackendScopeLabel } from "@/features/settings/lib/model-actions";
import { probeFreshness, probeSummary } from "@/features/settings/lib/probe-freshness";
import { deduplicateModelBackends, modelBackendDisplayName, modelBackendModelLabel , modelBackendStatusLabel } from "@/features/settings/lib/model-backend-presentation";
import { credentialIsOptional, providerNeedsBaseUrl, providerOptions } from "@/features/settings/lib/model-providers";
import { backendsForRole, hasEditableBackend, roleAssignmentHint } from "@/features/settings/lib/model-roles";
import { Button, Skeleton, cn } from "@/shared/ui";
import { pushError, pushSuccess } from "@/stores/notification";
import { useT, useLanguage, say, type Language } from "@/shared/i18n";

// 表单里这一项是**字符串**（输入框的原生形态，空串 = 没填）；提交时才转成
// 数字。直接用 number 会逼出一个"0 还是没填"的歧义，而这个数决定何时压缩。
type BackendForm = Omit<SaveModelBackendRequest, "context_window_tokens"> & {
  id?: string;
  context_window_tokens: string;
  roles: string[];
  /** 服务器上**现在**存着凭据吗。只用来决定 key 那一栏该说什么话 —— 编辑一条
      从没存过凭据的连接时，"留空以保留已存凭据"是一句假话。 */
  has_api_key: boolean;
};

const EMPTY_FORM: BackendForm = {
  provider: "",
  display_name: "",
  model: "",
  base_url: "",
  api_key: "",
  context_window_tokens: "",
  // 绝大多数连接就是主模型。默认勾上 reasoning，但**可以取消** —— 一条
  // 专门用来审图的连接不该被迫也当主模型。
  roles: ["reasoning"],
  has_api_key: false,
};

// 纯函数收 t，不自己够 hook —— 见 shared/i18n/useT.ts 的「纯函数不许用它」。
function managementLabel(backend: ModelBackend, lang: Language) {
  if (backend.provided_by) {
    return say({ zh: "来源：{name}", en: "From {name}" }, lang).replace("{name}", backend.provided_by.organisation_name);
  }
  if (backend.provider === "demo" || backend.provider === "local_demo") return say({ zh: "本地演示", en: "Local demonstration" }, lang);
  return modelBackendScopeLabel(backend.scope, lang);
}



function ConnectionDialog({
  form,
  saving,
  scopeLabel,
  roleCatalog,
  onChange,
  onClose,
  onSave,
}: {
  form: BackendForm;
  saving: boolean;
  /** 这条连接会落在哪个 scope —— 后端按角色定，人在存之前有权知道。 */
  scopeLabel: string;
  /** 角色目录（来自 API）。空 = 目录读不到，那时不渲染这一段而不是渲染空框。 */
  roleCatalog: ModelRole[];
  onChange: (form: BackendForm) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  const t = useT();
  const options = providerOptions(form.provider);
  const needsBaseUrl = providerNeedsBaseUrl(form.provider);
  // 「保留已存凭据」只有在**真的存着**的时候才成立。
  const keptCredential = Boolean(form.id && form.has_api_key);
  return (
    <div className="settings-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="connection-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <div><h2 id="connection-dialog-title">{form.id ? t({ zh: "编辑连接", en: "Edit connection" }) : t({ zh: "添加模型连接", en: "Add model connection" })}</h2><p>{form.id ? t({ zh: "密钥只写不读，接口永远不会把它回传。", en: "Credentials are write-only and never returned by the API." }) : t({ zh: `存成 ${scopeLabel}，马上就能用。密钥只写不读，接口永远不会把它回传。`, en: `Saved as ${scopeLabel} and usable right away. Credentials are write-only and never returned by the API.` })}</p></div>
          <button type="button" onClick={onClose} aria-label={t({ zh: "关闭模型连接对话框", en: "Close model connection dialog" })}><X size={16} /></button>
        </header>
        <form onSubmit={(event) => { event.preventDefault(); onSave(); }}>
          <div className="settings-dialog-fields">
            <label><span>{t({ zh: "显示名称", en: "Display name" })}</span><input required value={form.display_name} onChange={(event) => onChange({ ...form, display_name: event.target.value })} /></label>
            {/* 自由输入框的时候，"合法取值"只在存完之后以一个状态词的形式
                出现（unsupported_by_harness）—— 连接建出来了、看着是好的、
                就是永远跑不了。词表进控件。 */}
            <label>
              <span>{t({ zh: "厂商", en: "Provider" })}</span>
              <select required value={form.provider} disabled={Boolean(form.id)} onChange={(event) => onChange({ ...form, provider: event.target.value })}>
                <option value="" disabled>{t({ zh: "选一个厂商", en: "Select a provider" })}</option>
                {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label><span>{t({ zh: "模型", en: "Model" })}</span><input required value={form.model} onChange={(event) => onChange({ ...form, model: event.target.value })} /></label>
            <label>
              <span>{t({ zh: "接口地址", en: "Base URL" })}</span>
              <input
                required={needsBaseUrl}
                value={form.base_url}
                onChange={(event) => onChange({ ...form, base_url: event.target.value })}
                placeholder={needsBaseUrl ? "https://host/v1" : t({ zh: "由厂商托管的连接可以不填", en: "Optional for provider-managed connections" })}
              />
              {needsBaseUrl && <small>{t({ zh: "OpenAI 兼容接口，以 /v1 结尾。", en: "An OpenAI-compatible endpoint, ending in /v1." })}</small>}
            </label>
            {/* 上下文窗口是**模型的属性**。此前只有一个全局 env，且后端建
                worker 环境时根本没传下去 —— 不管选哪个模型，harness 一律吃
                120000 的兜底默认值。摘要器按窗口的 70% 触发压缩，于是百万
                窗口的模型也在 84k 处开始反复压（实测一个会话压了 15 次）。 */}
            <label>
              <span>{t({ zh: "上下文窗口（tokens）", en: "Context window (tokens)" })}</span>
              <input
                type="number" min={8000} max={2000000} step={1000}
                value={form.context_window_tokens}
                onChange={(event) => onChange({ ...form, context_window_tokens: event.target.value })}
                placeholder={t({ zh: "留空 = 用平台默认（256000）", en: "Leave blank to use the platform default (256000)" })}
              />
              <small>{t({ zh: "决定摘要器何时压缩上下文（阈值为窗口的 70%）。按这个模型的真实窗口填 —— 兜底值对 128K 的模型偏大，对 1M 的模型偏小。", en: "Decides when the summarizer compacts context (at 70% of the window). Use this model's real window; the fallback is too large for 128K models and too small for 1M ones." })}</small>
            </label>
            {/* 角色授权。没有这一段，一条连接永远只能当主模型 —— 审图 VLM
                在界面上就是不存在的（2026-08-22 的实际状态）。 */}
            {roleCatalog.length > 0 && (
              <fieldset className="settings-dialog-wide settings-role-fieldset">
                <legend>{t({ zh: "这条连接可以承担哪些角色", en: "Roles this connection may serve" })}</legend>
                {roleCatalog.map((role) => (
                  <label key={role.id} className="settings-role-checkbox">
                    <input
                      type="checkbox"
                      checked={form.roles.includes(role.id)}
                      onChange={(event) =>
                        onChange({
                          ...form,
                          roles: event.target.checked
                            ? [...form.roles, role.id]
                            : form.roles.filter((item) => item !== role.id),
                        })
                      }
                    />
                    <span>
                      {role.title}
                      {role.modality === "vision" && ` · ${t({ zh: "要能收图片", en: "needs image input" })}`}
                    </span>
                  </label>
                ))}
                <small>
                  {t({
                    zh: "勾选只是声明，保存时才真的去探一次。视觉这一项拿一张真的 1×1 图去试 —— 「这个模型不收图片」要在这里暴露，而不是跑到一半才发现。",
                    en: "Authorizing is a declaration; saving probes it. A vision role is checked with a real 1×1 image, so \"this model does not accept images\" shows up here rather than mid-run.",
                  })}
                </small>
              </fieldset>
            )}
            {/* 这一栏曾经只问"是不是在编辑"：编辑一条**从没存过凭据**的连接时，
                placeholder 照样写"留空以保留已存凭据" —— 于是人照着留空，而那条
                连接一直没有凭据（2026-09-15 yuankk 就是这么卡住的）。现在按三个
                真实局面分三句话说，其中一句是"自建端点本来就可以不填"。 */}
            <label className="settings-dialog-wide">
              <span>{keptCredential
                ? t({ zh: "新的 API 密钥", en: "New API key" })
                : t({ zh: "API 密钥", en: "API key" })}</span>
              <input
                type="password"
                autoComplete="new-password"
                value={form.api_key ?? ""}
                onChange={(event) => onChange({ ...form, api_key: event.target.value })}
                placeholder={
                  keptCredential
                    ? t({ zh: "留空就沿用已存的那把", en: "Leave blank to keep the stored credential" })
                    : credentialIsOptional(form.base_url)
                      ? t({ zh: "自建端点不鉴权就留空", en: "Leave blank if your endpoint needs no key" })
                      : t({ zh: "只写密钥", en: "Write-only credential" })
                }
              />
              {!keptCredential && credentialIsOptional(form.base_url) && (
                <small>
                  {t({
                    zh: "自建端点（vLLM / SGLang / Ollama…）多半不鉴权，可以留空 —— 保存时会不带 Authorization 头去探一次，端点收了就是 Ready。要鉴权的话探针会直接说出来。",
                    en: "Self-hosted endpoints (vLLM / SGLang / Ollama…) usually need no key — leave it blank and the probe will try once without an Authorization header. If the endpoint does want a key, the probe says so.",
                  })}
                </small>
              )}
            </label>
          </div>
          <footer><Button type="button" size="sm" variant="ghost" onClick={onClose}>{t({ zh: "取消", en: "Cancel" })}</Button><Button type="submit" size="sm" variant="primary" loading={saving}>{t({ zh: "保存连接", en: "Save connection" })}</Button></footer>
        </form>
      </section>
    </div>
  );
}

/** 这一块要的全部动作 —— 本机和组织各给一份，形状相同。 */
export type ModelsClient = {
  list: () => Promise<ModelBackend[]>;
  save: (data: SaveModelBackendRequest, id?: string) => Promise<unknown>;
  remove: (id: string) => Promise<unknown>;
  probe: (id: string) => Promise<unknown>;
  setEnabled: (id: string, isEnabled: boolean) => Promise<unknown>;
  roles: () => Promise<ModelRole[]>;
  setRoleDefault: (id: string, role: string) => Promise<unknown>;
};

export function ModelConnections({ client, keys, canSelect, canAdd, newConnectionScope, showRoles = true }: {
  client: ModelsClient;
  /** 两份缓存的键。组织那份带着连接 id —— 切组织时不许摆上一个组织的连接。 */
  keys: { backends: readonly unknown[]; roles: readonly unknown[] };
  /** 能不能给自己挑模型（每个角色用哪条）。 */
  canSelect: boolean;
  /** 能不能加连接。本机：谁都能加自己的；组织：只有管理员（成员的密钥不该存在别人管的服务器上）。 */
  canAdd: boolean;
  /** 新建的连接会落在哪个 scope —— 后端按身份定（管理员→组织、其余→个人），人在存之前有权知道。 */
  newConnectionScope: string;
  /**
   * 画不画「模型角色」那一段。组织页上不画：组织**提供**模型，每个人在自己的「设置 → 模型与
   * 密钥」里挑 —— 在组织页上再摆一套角色槽，就把组织做成了又一个要配模型的地方（wangd 2026-09-24）。
   */
  showRoles?: boolean;
}) {
  const t = useT();
  const lang = useLanguage();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<BackendForm | null>(null);

  const backends = useQuery({
    queryKey: keys.backends,
    queryFn: () => client.list(),
  });

  const save = useMutation({
    mutationFn: (form: BackendForm) => {
      const { id, ...data } = form;
      const window = data.context_window_tokens.trim();
      return client.save({
        ...data,
        api_key: data.api_key?.trim() || undefined,
        // 留空 → null：显式告诉后端"用平台默认"，而不是悄悄不带这个字段
        // （不带 = 保持原值，改不回默认）。
        context_window_tokens: window ? Number(window) : null,
      }, id);
    },
    onSuccess: async () => {
      // 角色授权可能变了 —— 两份缓存一起失效。只刷连接列表的话，角色那一段
      // 会继续显示改动之前的绑定，而它看起来完全正常。
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.backends }),
        queryClient.invalidateQueries({ queryKey: keys.roles }),
      ]);
      setEditing(null);
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "模型连接没能保存", en: "Model connection could not be saved" })),
  });

  const remove = useMutation({
    mutationFn: (backend: ModelBackend) => client.remove(backend.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.backends });
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "模型连接没能删除", en: "Model connection could not be removed" })),
  });

  // 手动探活。看得见就能点 —— 研究员改不了机构那条连接，但"它现在还能不能
  // 用"正是他要判断的事。后端有 60 秒冷却，`probed_now=false` 就说明这次拿的
  // 是冷却期内的旧观测，得如实说，不能假装刚测过。
  const probe = useMutation({
    mutationFn: (backend: ModelBackend) => client.probe(backend.id),
    // 冷却期内拿的是旧观测，这里**不弹提示**：屏幕上已经写着了。冷却是 60 秒，
    // 所以这种情况下那一格显示的必然是"刚刚检测"——它说的就是真话，没有需要
    // 补充解释的地方。（仓库规矩：成功提示要能通过"屏幕上看不出来吗？"这一问；
    // 而且它也不是错误，不该走 pushError。）
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.backends });
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "连接测试没跑起来", en: "Connection could not be tested" })),
  });

  // 禁用比删除温和：坏掉的连接先关掉，已建好的会话不受影响（它们记着自己
  // 当时用的模型）。字段和 PUT 后端一直都有，从前只是列表不投影它。
  const toggleEnabled = useMutation({
    mutationFn: (backend: ModelBackend) => client.setEnabled(backend.id, backend.is_enabled === false),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.backends });
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "连接没能切换", en: "Connection could not be switched" })),
  });

  // 角色目录 + 当下绑定。前端不枚举角色，这份返回是唯一来源。
  const roles = useQuery({ queryKey: keys.roles, queryFn: () => client.roles() });

  // 指派一个角色。主模型走的是同一条路（role="reasoning"）—— 从前它有自己
  // 的端点和自己的 mutation，于是"设默认"这件事在代码里有两份实现。
  const setRoleDefault = useMutation({
    mutationFn: ({ backend, role }: { backend: ModelBackend; role: string }) =>
      client.setRoleDefault(backend.id, role),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.backends }),
        queryClient.invalidateQueries({ queryKey: keys.roles }),
      ]);
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "角色分配没能更新", en: "Role assignment could not be updated" })),
  });

  const visibleBackends = useMemo(() => deduplicateModelBackends(backends.data ?? []), [backends.data]);
  const sharedBackendCount = useMemo(() => visibleBackends.filter((backend) => !backend.editable).length, [visibleBackends]);

  const editBackend = (backend: ModelBackend) => setEditing({
    id: backend.id,
    provider: backend.provider,
    display_name: backend.display_name,
    model: backend.model,
    base_url: backend.base_url ?? "",
    context_window_tokens: backend.context_window_tokens != null ? String(backend.context_window_tokens) : "",
    // 编辑时按**服务器上现在的样子**回填。缺字段（老后端）回退成 reasoning，
    // 而不是回退成空 —— 空会在保存时把这条连接的角色全撤掉，而人什么都没点。
    roles: backend.roles ?? ["reasoning"],
    api_key: "",
    has_api_key: backend.has_api_key,
  });

  return (
    <>

      {/* 模型角色 —— 主模型只是其中一个（reasoning）。
          此前这一段只有"Default model"一行，于是节点里的辅助模型（审图 VLM）
          在界面上完全不存在：既看不见它是谁，也没法换。现在按角色目录渲染，
          目录来自 API，前端不枚举。 */}
      {showRoles && <section className="settings-compact-section">
        <div className="settings-compact-heading">
          <div>
            <h2>{t({ zh: "模型角色", en: "Model roles" })}</h2>
            <p>{t({ zh: "每个角色是一个位置，平台拿一条模型连接去填它。改动只对新会话生效；已经在跑的会话保持它建立时冻结的绑定。这一页不会新建会话。", en: "Each role is a capability slot the platform fills with a model connection. Changes affect new Sessions only; existing Sessions keep their frozen bindings. This page does not create Sessions." })}</p>
          </div>
        </div>
        <div className="settings-row-group">
          {roles.isLoading && !roles.data && <div className="settings-row"><Skeleton height={34} /></div>}
          {/* 目录读不到就照实说是部署问题，别渲染成一个空列表 —— 空列表长得
              像"这套部署没有角色"，人会去找一个根本不存在的开关。 */}
          {roles.isError && (
            <div className="settings-row settings-error-row" role="alert">
              {roles.error instanceof Error ? roles.error.message : t({ zh: "读不到模型角色。", en: "Model roles could not be loaded." })}
            </div>
          )}
          {(roles.data ?? []).map((role) => {
            const candidates = backendsForRole(visibleBackends, role.id);
            const bound = candidates.find((backend) => backend.id === role.bound_backend_id);
            // 局面，不是权限：一个研究员**有权**填这个槽（后端建个人连接从不
            // 查管理权限），但他一条连接都改不动 —— 于是"去下面某条上勾"这
            // 句话对他是死路。两件事得分开算，才说得出他走得通的那一步。
            const situation = {
              canSelect,
              candidateCount: candidates.length,
              canAuthorizeExisting: hasEditableBackend(visibleBackends),
              canRegister: canAdd,
              hasAnyConnection: visibleBackends.length > 0,
            };
            return (
              <div className="settings-row" key={role.id}>
                <span>
                  <strong>{role.title}{role.required ? "" : " · optional"}</strong>
                  <small>{role.description}</small>
                </span>
                <span className="settings-control-stack">
                  <select
                    aria-label={`Connection for the ${role.title} role`}
                    value={bound?.id ?? ""}
                    disabled={!canSelect || candidates.length === 0 || setRoleDefault.isPending}
                    onChange={(event) => {
                      const backend = candidates.find((item) => item.id === event.target.value);
                      if (backend) setRoleDefault.mutate({ backend, role: role.id });
                    }}
                  >
                    <option value="">
                      {candidates.length === 0 ? t({ zh: "这个角色还没有可用的连接", en: "No connection authorized for this role" }) : t({ zh: "还没指定", en: "Not assigned" })}
                    </option>
                    {candidates.map((backend) => (
                      <option key={backend.id} value={backend.id}>
                        {modelBackendDisplayName(backend)} · {modelBackendModelLabel(backend)}
                      </option>
                    ))}
                  </select>
                  <small>{roleAssignmentHint(role, bound, situation, lang)}</small>
                  {/* 缺口和补口接在一起。此前"注册一条自己的连接"这条唯一
                      走得通的路，入口只在另一段的标题旁边叫「添加模型」，
                      跟"我缺一个审图模型"在界面上没有任何关联。 */}
                  {canAdd && candidates.length === 0 && (
                    <button
                      type="button"
                      className="settings-secondary-action settings-role-action"
                      onClick={() => setEditing({ ...EMPTY_FORM, roles: [role.id] })}
                    >
                      <Plus size={13} />{t({ zh: "给这个角色配一个模型", en: "Register a model for this role" })}</button>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      </section>}

      <section className="settings-compact-section">
        <div className="settings-compact-heading">
          <div><h2>{t({ zh: "模型连接", en: "Gateway connections" })}</h2><p>{t({ zh: "连接是否健康、属于哪个治理范围、有没有存着一份只写的凭据。", en: "Connection health, governance scope, and whether a write-only credential is stored." })}</p></div>
          {/* 加一条连接此前要 `model_backends.manage` —— 研究员永远没有这个
              权限，于是整段（列表 + Add）对他们不存在，界面上没有任何"加个模型"
              的入口。而后端 POST /model-backends 从来不查这个权限：它把新连接
              存进调用者自己管得着的 scope，研究员就是个人 scope。按钮补回来。 */}
          {canAdd && <button type="button" className="settings-secondary-action" onClick={() => setEditing(EMPTY_FORM)}><Plus size={13} />{t({ zh: "添加模型", en: "Add model" })}</button>}
        </div>
        <div className="settings-row-group model-connection-group">
          {backends.isLoading && !backends.data && <div className="settings-row"><Skeleton height={34} /></div>}
          {backends.isError && <div className="settings-row settings-error-row" role="alert">{t({ zh: "读不到模型连接。", en: "Model connections could not be loaded." })}</div>}
          {visibleBackends.length === 0 && !backends.isLoading && !backends.isError && <div className="settings-row settings-empty-row">{t({ zh: "还没有配过模型连接。", en: "No model connections configured." })}</div>}
          {sharedBackendCount > 0 && (
            <div className="settings-row settings-disabled-row">
              <span><strong><LockKeyhole size={14} /> {t({ zh: "{n} 条由管理员维护", en: "{n} maintained by an administrator" }, { n: sharedBackendCount })}</strong><small>{t({ zh: "组织的连接在这里只读，由组织管理员维护。", en: "Organisation connections are read-only here and maintained by an organisation administrator." })}</small></span>
              <span>{t({ zh: "只读", en: "Read only" })}</span>
            </div>
          )}
          {visibleBackends.map((backend) => {
            // 能不能改由后端答（`editable` = 这条连接的 scope 是不是你管得着的那个）。
            const actions = { canEdit: backend.editable };
            return (
              <div className="settings-row model-connection-row" key={backend.id}>
                <span className={cn("model-status-dot", `status-${backend.status}`)} title={modelBackendStatusLabel(backend.status)} aria-label={modelBackendStatusLabel(backend.status)} />
                <span className="model-connection-identity"><strong>{modelBackendDisplayName(backend)}</strong><small>{modelBackendModelLabel(backend)} · {modelBackendStatusLabel(backend.status)}</small></span>
                <span className="model-connection-fact"><small>{t({ zh: "范围", en: "Scope" })}</small><strong>{managementLabel(backend, lang)}</strong></span>
                <span className="model-connection-fact"><small>{t({ zh: "密钥", en: "Credential" })}</small><strong><KeyRound size={11} /> {backend.has_api_key ? t({ zh: "已存", en: "Stored" }) : t({ zh: "没存", en: "Not stored" })}</strong></span>
                {/* status 是判决，这里是**证据有多旧**。两个都给，人才判断得了
                    那句 Ready 值不值得信 —— 从前只有前者，于是三周前的快照和
                    当前事实长得一模一样。 */}
                <span className={cn("model-connection-fact", probeFreshness(backend).kind === "stale" && "is-stale")}>
                  <small>{t({ zh: "已校验", en: "Checked" })}</small>
                  <strong title={backend.last_probe_detail ?? undefined}>{probeSummary(backend)}</strong>
                </span>
                {/* 能建就得能删 —— 否则一条填错的连接（provider 选错、地址
                    打错）会永远留在列表里。侧栏那边删除藏在右键里，因为误点
                    的代价是别人的研究；这里是管理面，代价是重建一条连接，
                    常驻按钮 + 点名确认更合适。 */}
                <span className="model-connection-control">
                  {/* 不受 canEdit 门控：只让管理员测，等于让其他人继续对着
                      陈旧的 Ready 猜。 */}
                  <button
                    type="button"
                    disabled={probe.isPending}
                    onClick={() => probe.mutate(backend)}
                  >
                    Test
                  </button>
                  {actions.canEdit && (
                    <button
                      type="button"
                      disabled={toggleEnabled.isPending}
                      onClick={() => toggleEnabled.mutate(backend)}
                    >
                      {backend.is_enabled === false ? "Enable" : "Disable"}
                    </button>
                  )}
                  {actions.canEdit && <button type="button" onClick={() => editBackend(backend)}>{t({ zh: "编辑", en: "Edit" })}</button>}
                  {actions.canEdit && (
                    <button
                      type="button"
                      className="is-danger"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (!globalThis.confirm(t({ zh: `确定删除模型连接「${modelBackendDisplayName(backend)}」吗？已经建好的会话不受影响，它们记着自己当时用的模型。`, en: `Delete the model connection "${modelBackendDisplayName(backend)}"? Existing sessions are unaffected; each records the model it actually used.` }))) return;
                        remove.mutate(backend);
                      }}
                    >
                      Delete
                    </button>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      </section>

      {editing && (
        <ConnectionDialog
          form={editing}
          saving={save.isPending}
          scopeLabel={newConnectionScope}
          roleCatalog={roles.data ?? []}
          onChange={setEditing}
          onClose={() => setEditing(null)}
          onSave={() => save.mutate(editing)}
        />
      )}
    </>
  );
}
