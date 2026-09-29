import test from "node:test";
import assert from "node:assert/strict";
import type { ModelBackend, ModelRole } from "@/lib/api";
import { backendsForRole, hasEditableBackend, roleAssignmentHint } from "./model-roles.ts";

const VISION_ROLE = {
  id: "visual_review",
  title: "图像审查模型（VLM）",
  description: "对渲染完成的 figure 做可见缺陷判读。",
  modality: "vision",
  required: false,
  absence_note: "无此角色时审图工具不在你的工具面上 —— 平台配没配审图模型不是你能改的事。",
  absence_impact: "这个槽空着，figure 不做可见缺陷判读：交付按 draft 走。",
  available: false,
  bound_backend_id: null,
  bound_display_name: null,
  bound_model: null,
} satisfies ModelRole;

function backend(overrides: Partial<ModelBackend> = {}): ModelBackend {
  return {
    id: "b1",
    provider: "openai_compatible",
    display_name: "机构网关",
    model: "some-model",
    base_url: "https://gateway/v1",
    scope: { kind: "institution", id: "ieit" },
    status: "ready",
    has_api_key: true,
    is_default: false,
    editable: false,
    roles: ["reasoning"],
    ...overrides,
  } as ModelBackend;
}

const situation = (over: Partial<Parameters<typeof roleAssignmentHint>[2]> = {}) => ({
  canSelect: true,
  candidateCount: 0,
  canAuthorizeExisting: false,
  ...over,
});

test("能填这个角色的连接：被授权 + 现在能用", () => {
  const pool = [
    backend({ id: "text-only" }),
    backend({ id: "authorized", roles: ["visual_review"] }),
    backend({ id: "authorized-but-broken", roles: ["visual_review"], status: "credentials_rejected" }),
  ];
  assert.deepEqual(
    backendsForRole(pool, "visual_review").map((item) => item.id),
    ["authorized"],
  );
});

test("一条连接都改不动的人，不该被指去只读列表里授权", () => {
  // 这是 2026-08-31 那位研究员的真实局面：5 条连接全是机构的、全只读，
  // 而提示写着"去下面的 Gateway connections 里授权" —— 他照做，下面一个
  // 能勾的地方都没有，于是判断平台不支持配审图模型。能力一直在（后端建
  // 个人连接从不查管理权限），界面把他指向了一堵墙。
  const hint = roleAssignmentHint(VISION_ROLE, undefined, situation());
  assert.match(hint, /Register your own connection/);
  assert.equal(
    /in Gateway connections|tick it in Edit/.test(hint),
    false,
    "他一条连接都编辑不了，这句是死路",
  );
});

test("有自己改得动的连接时，先说在它上面勾", () => {
  const hint = roleAssignmentHint(VISION_ROLE, undefined, situation({ canAuthorizeExisting: true }));
  assert.match(hint, /tick it in Edit/);
});

test("有候选只是没选，就别讲注册", () => {
  const hint = roleAssignmentHint(
    VISION_ROLE,
    undefined,
    situation({ candidateCount: 1, canAuthorizeExisting: true }),
  );
  assert.match(hint, /pick one/);
  assert.equal(/Register|tick it in Edit/.test(hint), false);
});

test("空槽的代价引 absence_impact —— absence_note 是写给节点的，不摆在能改的人面前", () => {
  for (const s of [
    situation(),
    situation({ canAuthorizeExisting: true }),
    situation({ candidateCount: 2 }),
    situation({ canSelect: false }),
  ]) {
    const hint = roleAssignmentHint(VISION_ROLE, undefined, s);
    assert.match(hint, /这个槽空着/);
    assert.equal(
      hint.includes("不是你能改的事"),
      false,
      "把 absence_note 摆在「注册模型」按钮旁边，就是告诉唯一能填这个槽的人别管",
    );
  }
});

test("槽已经填上了就别再讲它空着", () => {
  const bound = backend({ id: "bound", roles: ["visual_review"], last_vision_ok: true });
  assert.equal(roleAssignmentHint(VISION_ROLE, bound, situation({ candidateCount: 1 })), "Ready.");
  assert.equal(
    roleAssignmentHint(VISION_ROLE, bound, situation({ canSelect: false, candidateCount: 1 })),
    "Your administrator controls this role.",
  );
});

test("视觉角色：模型不认图跟凭据坏了是两件事", () => {
  const rejects = backend({
    roles: ["visual_review"],
    last_vision_ok: false,
    last_vision_detail: "provider rejected an image message",
  });
  assert.match(roleAssignmentHint(VISION_ROLE, rejects, situation({ candidateCount: 1 })), /image message/);
  const unknown = backend({ roles: ["visual_review"], last_vision_ok: null });
  assert.match(
    roleAssignmentHint(VISION_ROLE, unknown, situation({ candidateCount: 1 })),
    /has not been confirmed yet/,
  );
});

test("能不能在现有连接上勾，读的是后端给的 editable", () => {
  assert.equal(hasEditableBackend([backend(), backend({ id: "b2" })]), false);
  assert.equal(hasEditableBackend([backend(), backend({ id: "mine", editable: true })]), true);
});

test("在一个组织里，成员加不了连接 —— 出路是找管理员，不是「加一条你自己的」", () => {
  // 组织的模型存在组织服务器上；成员的密钥不该存在别人管的服务器上，所以那一份不给他「添加」。
  // 这时再说"Register your own connection"就是把人领到一堵墙前面。
  const hint = roleAssignmentHint(VISION_ROLE, undefined, situation({ canRegister: false }));
  assert.match(hint, /administrator of this organisation has to add one/);
  assert.doesNotMatch(hint, /Register your own/);
});

test("一条连接都没有的时候，不说「下面每一条都是管理员的」", () => {
  // 2026-09-24 真跑：组织管理员打开刚建的组织，每个空槽底下都在说「下面每一条都是管理员的、
  // 你只读」—— 对管理员、而且下面一条都没有。
  const hint = roleAssignmentHint(VISION_ROLE, undefined, situation({ hasAnyConnection: false }), "zh");
  assert.match(hint, /还没有任何模型连接/);
  assert.doesNotMatch(hint, /管理员的、你只读/);
});

test("提示跟着界面语言走", () => {
  // 这一块在中文界面上曾经整段英文。
  assert.match(roleAssignmentHint(VISION_ROLE, undefined, situation(), "zh"), /加一条你自己的连接/);
  assert.match(roleAssignmentHint(VISION_ROLE, undefined, situation(), "en"), /Register your own connection/);
});
