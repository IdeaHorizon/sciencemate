/**
 * 模型角色在设置页上的呈现规则。
 *
 * 这里**不枚举角色** —— 目录来自 `GET /settings/model-roles`（权威在 harness
 * 的 shared/model_roles.yaml）。provider 词表当年在 Python 和 TypeScript 各活
 * 一份、靠一条测试钉着相等，那条路不走第二遍。
 */
import type { ModelBackend, ModelRole } from "@/lib/api";
import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

/** 能填这个角色的连接：被授权 + 现在能用。 */
export function backendsForRole(backends: ModelBackend[], role: string): ModelBackend[] {
  return backends.filter(
    (backend) => (backend.roles ?? []).includes(role) && backend.status === "ready",
  );
}

/**
 * 有没有一条**自己改得动**的连接。
 *
 * `editable` 是后端 `can_edit_backend()` 的投影（连接的 scope == 你管得着的
 * scope），不在前端另算一遍。
 */
export function hasEditableBackend(backends: ModelBackend[]): boolean {
  return backends.some((backend) => backend.editable);
}

/**
 * 一个角色行**当下的局面** —— 说哪句话由它决定，不由权限单独决定。
 *
 * `canAuthorizeExisting` 是关键的一维：没有它，"去下面某条连接上勾这个角色"
 * 这句话对研究员是条死路 —— 他下面每一行都是机构管理员的、只读。
 */
export type RoleSituation = {
  /** 能不能指派（`model_backends.select` / `.manage`）。 */
  canSelect: boolean;
  /** 已经被授权给这个角色、且现在能用的连接有几条。 */
  candidateCount: number;
  /** 有没有一条连接是自己能编辑的（能在它上面勾这个角色）。 */
  canAuthorizeExisting: boolean;
  /**
   * 能不能自己加一条连接。本机上谁都能（缺省 true）；在一个组织里只有管理员能 ——
   * 成员的密钥不该存在别人管的服务器上。说「去加一条你自己的」给一个加不了的人，是死路。
   */
  canRegister?: boolean;
  /**
   * 下面到底有没有连接。一条都没有的时候，"下面每一条都是管理员的、你只读"是句假话 ——
   * 2026-09-24 真跑：组织管理员打开一个刚建的组织，每个空槽底下都这么对他说。
   */
  hasAnyConnection?: boolean;
};

/**
 * 选择框下面那句话。
 *
 * 每一种局面各说各的话，别混成一句"未配置"，更别指一条这个人走不通的路：
 *
 *  - 没有选择权 → 说是谁在管。
 *  - 已绑定但视觉探针明确说不认图 → 单独说这件事。它跟"凭据坏了"是两件
 *    事：凭据完全正常，去换 key 是白费功夫。
 *  - 有候选没选 → 就差点一下，别讲注册。
 *  - 零候选、但有自己改得动的连接 → 指"在你自己那条上勾"。
 *  - 零候选、且一条都改不动（研究员的常态）→ 指**注册你自己的**。此前这里
 *    统一说"去下面的 Gateway connections 里授权"，而他下面每一行都是只读的
 *    —— 能力一直在（后端建个人连接从不查管理权限），只是界面把他指向了一堵
 *    墙。2026-08-31 实测：一位研究员因此认为平台不支持配审图模型。
 *
 * 缺槽的代价一律引 `absence_impact`（写给人的那句），**不引 `absence_note`**
 * —— 后者是注入给消费方节点的（"你什么都不用做，也不要为这件事重跑"），摆在
 * 一个「注册模型」按钮旁边就是在跟唯一能填这个槽的人说"这不是你能改的事"。
 */
export function roleAssignmentHint(
  role: ModelRole,
  bound: ModelBackend | undefined,
  situation: RoleSituation,
  lang: Language = "en",
): string {
  const { canSelect, candidateCount, canAuthorizeExisting } = situation;
  const say_ = (phrase: Phrase) => say(phrase, lang);
  if (!canSelect) {
    // 槽已经填上了就别再讲"空着会少什么" —— 那句话此刻是假的。
    const who = say_({ zh: "这个角色由管理员决定。", en: "Your administrator controls this role." });
    return bound ? who : withImpact(who, role);
  }
  if (!bound) {
    if (candidateCount > 0) {
      return withImpact(say_({ zh: "已经有连接能担这个角色 —— 选一条就行。",
                               en: "A connection is authorized for this role — pick one to fill it." }), role);
    }
    const how = canAuthorizeExisting
      ? say_({ zh: "给这个角色授权一条连接 —— 在你自己的某条连接「编辑」里勾上它，或者新加一条。",
               en: "Authorize a connection for this role — tick it in Edit on one of your own connections, or register a new one." })
      : situation.canRegister === false
        ? say_({ zh: "还没有连接能担这个角色 —— 要这个组织的管理员来加。",
                 en: "No connection serves this role yet — an administrator of this organisation has to add one." })
        : situation.hasAnyConnection === false
          ? say_({ zh: "还没有任何模型连接 —— 加一条来填这个位置。",
                   en: "There are no model connections yet — register one to fill this slot." })
          : say_({ zh: "下面每一条都是管理员的、你只读。加一条你自己的连接（自己的地址和密钥）来填这个位置。",
                   en: "Every connection below belongs to an administrator and is read-only for you. Register your own connection — your own endpoint and API key — to fill this slot yourself." });
    return withImpact(how, role);
  }
  if (role.modality === "vision" && bound.last_vision_ok === false) {
    return (
      bound.last_vision_detail ??
      say_({ zh: "这条连接拒收了一条带图的消息 —— 密钥没问题，是模型不认图。",
             en: "This connection rejected an image message — the credential is fine, the model is not." })
    );
  }
  if (role.modality === "vision" && bound.last_vision_ok == null) {
    return say_({ zh: "还没确认它认不认图 —— 点「测试」查一次。",
                  en: "Image support has not been confirmed yet — use Test connection to check." });
  }
  return say_({ zh: "就绪。", en: "Ready." });
}

function withImpact(how: string, role: ModelRole): string {
  const impact = (role.absence_impact ?? "").trim();
  return impact ? `${how} ${impact}` : how;
}
