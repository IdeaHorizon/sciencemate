import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { HARNESS_COMPATIBLE_PROVIDERS, providerOptions, providerNeedsBaseUrl, credentialIsOptional } from "./model-providers.ts";

/**
 * 词表在进程边界两侧各写一次，这里钉住两边相等。
 * 前端给的选项如果比后端支持的多，人能建出一条永远 `unsupported_by_harness`
 * 的连接；少了，则有能用的 provider 在 UI 里根本选不出来。
 */
test("provider 选项与后端 HARNESS_COMPATIBLE_PROVIDERS 逐条相等", () => {
  const source = readFileSync(
    new URL("../../../../../backend/app/services/model_backends.py", import.meta.url),
    "utf8",
  );
  const block = source.match(/HARNESS_COMPATIBLE_PROVIDERS\s*=\s*frozenset\(\s*\{([^}]*)\}/);
  assert.ok(block, "后端的 HARNESS_COMPATIBLE_PROVIDERS 没找到——词表挪了地方就得改这条测试");
  const backendProviders = [...block[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual([...HARNESS_COMPATIBLE_PROVIDERS].sort(), backendProviders.sort());
});

test("编辑一条 provider 不在词表里的连接，不会被下拉悄悄改成别的 provider", () => {
  const options = providerOptions("demo");
  assert.ok(options.some((option) => option.value === "demo" && !option.supported));
  assert.equal(providerOptions().some((option) => option.value === "demo"), false);
});

test("自定义端点要地址，托管 provider 不要", () => {
  assert.equal(providerNeedsBaseUrl("openai_compatible"), true);
  assert.equal(providerNeedsBaseUrl("local"), true);
  assert.equal(providerNeedsBaseUrl("deepseek"), false);
});

test("「可以不填 key」两侧问的是同一件事：填没填 base_url", () => {
  // 不是按 provider 名单判：`openai_compatible` 既可能是自建 vLLM 也可能是
  // 某家托管服务 —— 同一个标签两种答案，名单在这件事上是错的量具。
  assert.equal(credentialIsOptional("http://10.0.0.1:8000/v1"), true);
  assert.equal(credentialIsOptional(""), false);
  assert.equal(credentialIsOptional(null), false);

  const source = readFileSync(
    new URL("../../../../../backend/app/services/model_backends.py", import.meta.url),
    "utf8",
  );
  const body = source.match(/def credential_is_optional\(config[^)]*\)[^\n]*\n(?:.|\n)*?\n    return ([^\n]+)/);
  assert.ok(body, "后端 credential_is_optional 没找到——判据挪了地方就得改这条测试");
  assert.match(body[1], /base_url/, "后端也必须按 base_url 判，否则两边会各自演化");
});
