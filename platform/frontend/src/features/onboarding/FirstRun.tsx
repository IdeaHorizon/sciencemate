"use client";

/**
 * 开场 —— **叠在工作区上的一叠卡片**，不是一个把产品挡在门外的页面。
 *
 * ## 它替掉的是什么
 *
 * 在此之前是 `/setup`：一个独占整屏的向导。用户还没看见任何东西，就被要求先做
 * 决定，做完了才放行 —— 而且三个字段填不全「继续」是灰的，跳都跳不过去。
 * 2026-09-17 走查时的原话：「上来就是一个空白页面……这么死板」。
 *
 * 现在：打开直接进真实工作区，开场浮在上面。每一步都能跳过，关掉也行。
 *
 * ## 跳过不等于没事发生
 *
 * 这是整套设计里唯一不让步的一条：**每一个被跳过的步骤都要在界面上留下一个
 * 看得见的缺口**，而缺口由真实状态说话，不由"他跳过了"这个标记说话 ——
 *
 *   - 没有主推理模型 → 工作区顶部常驻一条提示，「新建会话」不可点并说明原因
 *     （见 MissingModelBanner；判据是主模型那个角色有没有人担着，和这里第一步
 *     问的是同一个 `coversTheMainRole`，用户在别处配好了它自己就消失）；
 *   - 没选方向 → 资讯页照旧在页内问。
 *
 * `onboarding_done` 只管一件事：**要不要再教一遍**。它从不代表任何东西配好了。
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Building2, Compass, Cpu, LayoutDashboard, X } from "lucide-react";
import { InterestPicker } from "@/features/feed/components/InterestPicker";
import { useCapabilities, useHasCapability } from "@/features/capabilities";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { LANDING_OPTIONS } from "@/features/settings/lib/landing-options";
import { api, type DefaultLanding, type ModelRole } from "@/lib/api";
import { Button } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { useT, type Phrase } from "@/shared/i18n";
import { useProjectId } from "@/shared/routing/route-params";
import { GuidedTour } from "./GuidedTour";
import { WORKSPACE_STOPS } from "./lib/tour-stops";
import { ModelConnectionFields, draftIsComplete, emptyDraft, type ConnectionDraft } from "./ModelConnectionFields";
import { keyStorageLine, providerForApi } from "./lib/providers";
import { coversTheMainRole, stepsToShow, type OnboardingFacts, type OnboardingStep } from "./lib/steps";

const STEP_TITLE: Record<OnboardingStep, Phrase> = {
  model: { zh: "用哪个模型", en: "Which model" },
  interests: { zh: "你关心哪些方向", en: "Which fields do you follow" },
  landing: { zh: "打开时先看什么", en: "What to open first" },
};

const STEP_ICON: Record<OnboardingStep, typeof Cpu> = {
  model: Cpu, interests: Compass, landing: LayoutDashboard,
};

export function FirstRun() {
  const t = useT();
  const router = useRouter();
  const preference = useInterfaceSettings();
  const { capabilities } = useCapabilities();
  const canConnect = useHasCapability("connections");

  // 开场只浮在工作区上，项目页上不开。它问的、存的全是这台机器的事（主模型、资讯的
  // 方向、打开先看哪页）；而项目页上每一问都带着"说的是这个项目"的兜底头
  // （`api.askingAbout`），组织项目里就被转去组织服务器 —— 2026-09-24 真跑：成员的
  // 主模型是「组里的网关」，一进组织项目，开场照样问「用哪个模型」，因为答话的是服务器
  // 上的他。项目页有项目自己那一圈（ProjectGuide），这里的气泡也只贴得上工作区的侧栏。
  const onAProject = useProjectId() !== "";

  const [facts, setFacts] = useState<OnboardingFacts | null>(null);
  const [index, setIndex] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [tourStarted, setTourStarted] = useState(false);

  // 事实只取一次：开场问的是"这台机器现在什么样"，问完就按那个样子走完。
  // 中途跟着状态变会让步骤在用户眼皮底下增减。
  useEffect(() => {
    if (canConnect === null || onAProject || facts !== null) return;
    let active = true;
    void (async () => {
      const [roles, interests] = await Promise.all([
        api.listModelRoles().catch(() => []),
        api.getFeedInterests().catch(() => null),
      ]);
      if (!active) return;
      setFacts({
        hasMainModel: coversTheMainRole(roles),
        hasInterests: Boolean(interests && (interests.onboarded || interests.domains.length > 0)),
      });
    })();
    return () => { active = false; };
  }, [canConnect, onAProject, facts]);

  const steps = facts ? stepsToShow(facts) : [];
  const running = !dismissed && !onAProject && preference.ready && !preference.settings.onboarding_done && facts !== null;
  // 开场 = 几张卡片 + 最后贴在侧栏上的三个气泡。没有卡片要问（都配好了、只是没
  // 教过）就直接走气泡那一段 —— 那也是升级上来的老安装唯一会看到的东西。
  const open = running && steps.length > 0 && !tourStarted;
  const tour = running && (tourStarted || steps.length === 0);

  // 走完（或关掉）就记一笔"教过了"。这一笔不代表任何东西配好了 —— 它只让下次
  // 打开安静一点，缺口该提示的照样提示。
  const remember = useCallback(() => {
    setDismissed(true);
    if (preference.settings.onboarding_done) return;
    void preference.save({ ...preference.settings, onboarding_done: true });
  }, [preference]);

  useEffect(() => {
    if (!open) return;
    const onEscape = (event: KeyboardEvent) => { if (event.key === "Escape") remember(); };
    window.addEventListener("keydown", onEscape);
    return () => window.removeEventListener("keydown", onEscape);
  }, [open, remember]);

  if (tour) return <GuidedTour stops={WORKSPACE_STOPS} onDone={remember} />;
  if (!open) return null;
  const step = steps[Math.min(index, steps.length - 1)];
  const last = index >= steps.length - 1;
  // 卡片走完不直接收工，接着走那三个气泡。中途按 X / Esc 是「以后再说」，
  // 气泡也一并不看 —— 他说的是"现在别教我"。
  const advance = () => (last ? setTourStarted(true) : setIndex(index + 1));
  const Icon = STEP_ICON[step];

  return (
    <div className="onboarding-scrim" role="presentation">
      <section className="onboarding-card" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
        <header className="onboarding-head">
          <div>
            <span className="onboarding-progress">{t({ zh: `第 ${index + 1} 步 / 共 ${steps.length} 步`, en: `Step ${index + 1} of ${steps.length}` })}</span>
            <h2 id="onboarding-title"><Icon size={17} aria-hidden="true" />{t(STEP_TITLE[step])}</h2>
          </div>
          <button type="button" className="onboarding-close" onClick={remember}
            aria-label={t({ zh: "以后再说", en: "Later" })} title={t({ zh: "以后再说", en: "Later" })}>
            <X size={16} />
          </button>
        </header>

        {step === "model" && (
          <ModelStep
            credentialKeyStorage={capabilities.credentialKeyStorage}
            onSaved={advance}
            onSkip={advance}
            lastStep={last}
          />
        )}
        {step === "interests" && (
          <div className="onboarding-body">
            <p className="onboarding-lead">{t({ zh: "挑几个方向，资讯页就按它们给你挑今天值得看的。以后随时能改。", en: "Pick a few fields and the feed will use them to choose what is worth your time today. You can change them any time." })}</p>
            <div className="onboarding-interests"><InterestPicker showSkip={false} onDone={advance} /></div>
            <footer className="onboarding-actions">
              <button type="button" className="onboarding-skip" onClick={advance}>{t({ zh: "先跳过", en: "Skip for now" })}</button>
            </footer>
          </div>
        )}
        {step === "landing" && <LandingStep onDone={advance} lastStep={last} />}

        <p className="onboarding-foot">{t({ zh: "每一步都能跳过 —— 跳过的东西以后在「设置」里随时能补。", en: "Every step can be skipped; anything you skip is waiting for you in Settings." })}</p>
      </section>
    </div>
  );
}

/**
 * 第 1 步：模型。
 *
 * 主模型之外那几个**可选角色按服务器的目录扫出来**，不写死名单 —— 角色目录的
 * 权威在 harness 的 `shared/model_roles.yaml`，哪天那边加一个，这里自动多一行。
 * 每一行写的是这个槽空着**会少什么**（`absence_impact`，专门写给"唯一能把它
 * 填上的那个人"看的那句），不是一句"未配置"。
 */
function ModelStep({
  credentialKeyStorage, onSaved, onSkip, lastStep,
}: { credentialKeyStorage: string; onSaved: () => void; onSkip: () => void; lastStep: boolean }) {
  const t = useT();
  // 这里**不问权限** —— 问了就会把人指向一堵墙。
  //
  // 第一版我写的是 `canManageModels(user)`：能管共享模型才给表单，否则让他去找
  // 管理员。跑起来当场露馅 —— 个人版那个隐式本机用户是 researcher，手里只有
  // `model_backends.select`，于是**一台自己电脑上的软件**告诉自己的主人"去找
  // 管理员"。
  //
  // 真身是问错了问题：能不能管**机构共享的**那些连接，和能不能给自己加一条，
  // 是两件事。后端 `create_model_backend` 一个权限都不查，`managed_scope()` 对
  // 研究员返回 ("personal", user.id) —— 谁都能注册自己的连接。model-roles.ts
  // 里正记着这个坑的实测：一位研究员因为界面把他指向一堵墙，认定平台不支持配
  // 审图模型。别再撞第二次。
  const sharedWithOthers = useHasCapability("auth");
  const [roles, setRoles] = useState<ModelRole[]>([]);
  const [main, setMain] = useState<ConnectionDraft>(emptyDraft());
  const [extras, setExtras] = useState<Record<string, ConnectionDraft>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void api.listModelRoles().then((list) => { if (active) setRoles(list); }).catch(() => {});
    return () => { active = false; };
  }, []);

  const required = roles.find((role) => role.required);
  const optional = roles.filter((role) => !role.required);

  const save = async () => {
    setError(null);
    if (!draftIsComplete(main)) {
      setError(t({ zh: "地址、模型、API 密钥三样都要填。", en: "Address, model and API key are all required." }));
      return;
    }
    setSaving(true);
    try {
      const saved = await api.saveModelBackend({
        provider: providerForApi(main.provider.id),
        display_name: `${t(main.provider.label)} · ${main.model.trim()}`,
        model: main.model.trim(),
        base_url: main.baseUrl.trim(),
        api_key: main.apiKey.trim(),
        ...(required ? { roles: [required.id] } : {}),
      });
      // 「被授权服务这个角色」和「是这个角色的默认」是两件事，各有各的端点。
      // 只做前一件的话，这条连接会躺在那儿不被选中 —— 用户填完了却还是开不了工。
      if (required && saved?.id) await api.setRoleDefaultBackend(saved.id, required.id).catch(() => undefined);
      // 可选角色各存各的连接。其中一条存不上不该把已经存好的主模型一起退回去 ——
      // 它们是各自独立的东西，失败也各自说。
      for (const [roleId, draft] of Object.entries(extras)) {
        if (!draftIsComplete(draft)) continue;
        const role = optional.find((item) => item.id === roleId);
        try {
          const extra = await api.saveModelBackend({
            provider: providerForApi(draft.provider.id),
            display_name: `${role ? role.title : roleId} · ${draft.model.trim()}`,
            model: draft.model.trim(),
            base_url: draft.baseUrl.trim(),
            api_key: draft.apiKey.trim(),
            roles: [roleId],
          });
          if (extra?.id) await api.setRoleDefaultBackend(extra.id, roleId);
        } catch (cause) {
          pushError(cause instanceof Error ? cause.message
            : t({ zh: "这个可选模型没能保存", en: "That optional model could not be saved" }));
        }
      }
      onSaved();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t({ zh: "没能保存这个模型连接。", en: "This model connection could not be saved." }));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="onboarding-body">
      <p className="onboarding-lead">
        {required?.description || t({ zh: "会话和每个节点用的主模型。", en: "The main model used by sessions and every node." })}
        {" "}{t(keyStorageLine(credentialKeyStorage))}
      </p>
      {sharedWithOthers && (
        <p className="onboarding-lead">{t({ zh: "这条连接归你自己用。机构统一的那些由管理员配，不受这里影响。", en: "This connection is yours alone. Institution-wide ones are configured by an administrator and are unaffected by this." })}</p>
      )}
      <ModelConnectionFields draft={main} onChange={setMain} idPrefix="onboarding-main" />

      {optional.length > 0 && (
        <details className="onboarding-optional">
          <summary>{t({ zh: `还可以配 ${optional.length} 个可选模型（跳过不影响开工）`, en: `${optional.length} optional models (skipping them does not block you)` })}</summary>
          {optional.map((role) => {
            const draft = extras[role.id];
            return (
              <div className="onboarding-optional-role" key={role.id}>
                <div>
                  <strong>{role.title}</strong>
                  <small>{role.absence_impact || role.description}</small>
                </div>
                {draft
                  ? <ModelConnectionFields draft={draft} idPrefix={`onboarding-${role.id}`}
                      onChange={(next) => setExtras({ ...extras, [role.id]: next })} />
                  : <button type="button" className="onboarding-skip"
                      onClick={() => setExtras({ ...extras, [role.id]: emptyDraft() })}>
                      {t({ zh: "配一个", en: "Add one" })}
                    </button>}
              </div>
            );
          })}
        </details>
      )}

      {error && <p className="onboarding-error" role="alert">{error}</p>}
      <footer className="onboarding-actions">
        <button type="button" className="onboarding-skip" onClick={onSkip}>
          {t({ zh: "先跳过 —— 没有模型开不了会话", en: "Skip — without a model you cannot start a session" })}
        </button>
        <Button size="sm" variant="primary" loading={saving} onClick={() => void save()}
          iconRight={<ArrowRight size={14} />}>
          {lastStep ? t({ zh: "存好，开始用", en: "Save and start" }) : t({ zh: "存好，继续", en: "Save and continue" })}
        </Button>
      </footer>
    </div>
  );
}

/** 最后一步：打开时先看什么。纯偏好，没有"没配好"这回事，所以它不单独构成开场。 */
function LandingStep({ onDone, lastStep }: { onDone: () => void; lastStep: boolean }) {
  const t = useT();
  const preference = useInterfaceSettings();
  const [choice, setChoice] = useState<DefaultLanding>(preference.settings.default_landing);
  return (
    <div className="onboarding-body">
      <p className="onboarding-lead">{t({ zh: "每次打开这个应用先落在哪一页。随时能在「设置 → 通用」里改。", en: "Where this app lands each time you open it. Change it any time in Settings → General." })}</p>
      <div className="onboarding-landing">
        {LANDING_OPTIONS.map((option) => (
          <label key={option.value} className={choice === option.value ? "is-chosen" : undefined}>
            <input type="radio" name="onboarding-landing" value={option.value} checked={choice === option.value}
              onChange={() => setChoice(option.value)} />
            <span>{t(option.label)}</span>
          </label>
        ))}
      </div>
      <footer className="onboarding-actions">
        <button type="button" className="onboarding-skip" onClick={onDone}>{t({ zh: "用默认的", en: "Keep the default" })}</button>
        <Button size="sm" variant="primary" onClick={() => {
          if (choice !== preference.settings.default_landing) {
            void preference.save({ ...preference.settings, default_landing: choice });
          }
          onDone();
        }} iconRight={<ArrowRight size={14} />}>{t({ zh: "开始用", en: "Start" })}</Button>
      </footer>
    </div>
  );
}
