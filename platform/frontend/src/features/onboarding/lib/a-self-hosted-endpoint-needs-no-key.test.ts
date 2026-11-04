/**
 * 新手引导第 1 步：自建端点不填密钥也能保存 —— 和设置页同一个判据。
 *
 * 11-04 在引导里选「其他（兼容 OpenAI 的服务）」、填本机地址和模型、密钥留空，点保存被拦：
 * 「请填写地址、模型和 API 密钥。」而同一条连接在设置页能存、探针能过、会话能用。
 */
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { PROVIDERS, keyIsOptional } from "./providers.ts";

const preset = (id: string) => PROVIDERS.find((item) => item.id === id)!;

test("自己的服务可以不填密钥，厂商官方地址不行", () => {
  assert.equal(keyIsOptional({ provider: preset("custom"), baseUrl: "http://127.0.0.1:8000/v1" }), true);
  assert.equal(keyIsOptional({ provider: preset("custom"), baseUrl: "  " }), false, "地址还没填");
  assert.equal(keyIsOptional({ provider: preset("deepseek"), baseUrl: preset("deepseek").base_url }), false,
    "厂商官方地址没有密钥连不上");
  assert.equal(keyIsOptional({ provider: preset("openai"), baseUrl: "http://10.0.0.5:8000/v1" }), true,
    "把预设地址换成了自己的服务，就是自建端点");
});

test("保存前的检查和输入框都按这个判据走", () => {
  const fields = readFileSync("src/features/onboarding/ModelConnectionFields.tsx", "utf8");
  assert.match(fields, /draft\.apiKey\.trim\(\) \|\| keyIsOptional\(draft\)/, "保存前的检查还在一律要密钥");
  const firstRun = readFileSync("src/features/onboarding/FirstRun.tsx", "utf8");
  assert.match(firstRun, /keyIsOptional\(main\)/, "缺东西时的提示没按这个判据说");
  // 留空的密钥不能以空串发出去：后端的 api_key 是 min_length=1，空串是一次 422（11-04 点出来的）。
  assert.doesNotMatch(firstRun, /api_key: (main|draft)\.apiKey\.trim\(\),/, "空的密钥框会以空串发给后端");
  assert.match(firstRun, /api_key: main\.apiKey\.trim\(\) \|\| undefined/);
});
