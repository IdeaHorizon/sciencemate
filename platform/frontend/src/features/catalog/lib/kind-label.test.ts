import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ARTIFACT_KIND_LABEL, artifactKindLabel } from "./kind-label.ts";

test("类型名表覆盖 artifact_policy 里全部 permanent 类型", () => {
  /**
   * 交付面上的名字来自这张表。少一个类型，那一类交付物就会以 `analysis_report`
   * 这种代号出现在用户面前 —— 而没有任何一层报错。所以对着权威名单扫，不靠记性。
   */
  const policy = readFileSync(
    new URL("../../../../../../shared/lib/artifact_policy.py", import.meta.url),
    "utf8",
  );
  const permanent = [...policy.matchAll(/"([a-z_]+)":\s*\{"retention":\s*"permanent"/g)]
    .map((match) => match[1]);
  assert.ok(permanent.length >= 10, `只扫到 ${permanent.length} 个 permanent 类型，正则大概对不上文件了`);
  const missing = permanent.filter((kind) => !(kind in ARTIFACT_KIND_LABEL));
  assert.deepEqual(missing, []);
});

test("表里没有的照原样显示，不猜", () => {
  assert.equal(artifactKindLabel("pre_registration"), "预注册");
  assert.equal(artifactKindLabel("compression_log"), "compression_log");
  assert.equal(artifactKindLabel(null), "产物");
  assert.equal(artifactKindLabel(""), "产物");
});

test("类型名只有这一张表", () => {
  /** 第二张表出现的那一刻，同一种产物就有了两个名字，且分叉不报错。 */
  const src = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  for (const file of [
    "../components/ProjectOutputsView.tsx",
    "../../sessions/components/SessionDeliverables.tsx",
  ]) {
    assert.doesNotMatch(src(file), /_LABEL\s*:\s*Record<string,\s*string>/, `${file} 自己又写了一张类型名表`);
    assert.match(src(file), /artifactKindLabel\(/, `${file} 没有用共用的那张表`);
  }
});
