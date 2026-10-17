import test from "node:test";
import assert from "node:assert/strict";
import { bannerModel, environmentSignature, type SessionEnvironment, type EnvironmentWarning } from "./environment.ts";

function env(warnings: Partial<EnvironmentWarning>[], extra: Partial<SessionEnvironment> = {}): SessionEnvironment {
  return {
    warnings: warnings.map((w) => ({
      code: "state_dir_unavailable", severity: "degraded", path: "/data/afs/runs", error: "PermissionError: [Errno 13]",
      consequences: ["这一轮的运行记录落在临时目录"], ...w,
    })),
    capabilities: null, source: "live", repeated: 0, escalated: false, doctor: null, runId: null, data_root: "/data/afs",
    ...extra,
  };
}

test("nothing to say → no banner; a degraded precondition → a yellow banner with its consequences", () => {
  assert.equal(bannerModel(env([]), "zh", null), null);
  const model = bannerModel(env([{}]), "zh", null);
  assert.ok(model);
  assert.equal(model.tone, "warning");
  assert.match(model.title, /会话运行目录建不出来/);
  assert.deepEqual(model.lines, ["这一轮的运行记录落在临时目录"]);
  assert.equal(model.repair, null, "没有提案就没有修复按钮");
});

test("a fatal event is red and names the code in plain words; a conflict offers no command", () => {
  const model = bannerModel(env([{
    code: "tenant_home_conflict", severity: "fatal", consequences: ["这一轮没有起跑"],
    repair: { kind: "needs_administrator", argv: null, command: null, changes: "要管理员", reversible: null, needs_consent: true, automatic: false },
  }]), "zh", null);
  assert.ok(model);
  assert.equal(model.tone, "error");
  assert.match(model.title, /写着别人的身份/);
  assert.equal(model.repair, null);
});

test("the repair button carries the server's command and vanishes once that repair succeeded", () => {
  const repair = { kind: "reset_data_root_permissions", argv: ["chmod", "-R", "u+rwX", "/data/afs"], command: "chmod -R u+rwX /data/afs", changes: "恢复权限", reversible: false, needs_consent: true, automatic: false };
  const before = bannerModel(env([{ repair }]), "zh", null);
  assert.deepEqual(before?.repair, { code: "state_dir_unavailable", path: "/data/afs/runs", command: "chmod -R u+rwX /data/afs", changes: "恢复权限" });
  const after = bannerModel(env([{ repair, repair_result: { code: "state_dir_unavailable", path: "/data/afs/runs", command: repair.command, returncode: 0, stdout: "", stderr: "" } }]), "zh", null);
  assert.equal(after?.repair, null);
  assert.ok(after?.details.some((line) => line.includes("返回码 0")));
});

test("dismissing hides the same group and only the same group", () => {
  const one = env([{}]);
  const signature = environmentSignature(one);
  assert.equal(bannerModel(one, "zh", signature), null, "收起过同一组就不画");
  const two = env([{}, { code: "lock_unavailable", path: "/data/afs/runs/.chat.lock" }]);
  assert.notEqual(environmentSignature(two), signature);
  assert.ok(bannerModel(two, "zh", signature), "多了一条就再弹");
  const repaired = env([{ severity: "repaired", code: "home_marker_rebuilt", consequences: [] }]);
  assert.equal(environmentSignature(repaired), "", "修好的不进指纹");
  assert.equal(bannerModel(repaired, "zh", null)?.tone, "info");
});

test("an escalated report cannot be dismissed and says which repetition this is", () => {
  const escalated = env([], {
    escalated: true, repeated: 2,
    doctor: {
      paths: [{ path: "/data/afs/x", exists: null, kind: null, owner: null, mode: null, acl: null, reparse_tag: null, error: "PermissionError: 拒绝访问" }],
      data_root: { path: "/data/afs", exists: true, kind: "dir", owner: "lujy (uid 501)", mode: "0o700", acl: null, reparse_tag: null, error: null },
      token: { user: "lujy", uid: 501, integrity: null }, holders: [{ pid: 4242, spawn_token: "ab", alive: true }],
    },
  });
  const model = bannerModel(escalated, "zh", environmentSignature(escalated));
  assert.ok(model, "escalated 不许收起");
  assert.match(model.title, /连续出现 2 次/);
  assert.ok(model.details.some((l) => l.includes("PermissionError: 拒绝访问")));
  assert.ok(model.details.some((l) => l.includes("4242")));
});

test("english copy exists for every branch", () => {
  const model = bannerModel(env([{}]), "en", null);
  assert.match(model?.title ?? "", /precondition/);
  assert.match(bannerModel(env([{ severity: "fatal", code: "tenant_home_conflict", consequences: [] }]), "en", null)?.title ?? "", /did not start/);
});
