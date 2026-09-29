import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/**
 * 对比度是算得出来的，所以它该由机器守着，而不是靠改的人记得去查。
 *
 * 修之前实测：暗色（默认主题）下用户气泡白字压实色 accent 只有 2.53:1，
 * `--color-fg-subtle` 4.12:1，`--color-border` 1.55:1 —— 三条都不达标，
 * 而用户气泡是全站出现频率最高的文字块。
 */
const CSS = readFileSync(new URL("./tokens.css", import.meta.url), "utf8");

function token(name: string): string {
  const m = CSS.match(new RegExp(`^\\s*--${name}:\\s*(#[0-9a-fA-F]{3,8})\\s*;`, "m"));
  assert.ok(m, `调色板里找不到 --${name}（或它不是字面 hex）`);
  return m![1];
}

function relativeLuminance(hex: string): number {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const channels = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255);
  const linear = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrast(a: string, b: string): number {
  const [la, lb] = [relativeLuminance(a), relativeLuminance(b)];
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** 正文 4.5:1。--color-fg-subtle 用在 11–12px 上，所以两个底面都得过。 */
const TEXT_PAIRS = (t: (n: string) => string) => [
  ["正文", t("fg"), t("bg"), 4.5],
  ["次要文字", t("fg-muted"), t("bg"), 4.5],
  ["弱化文字 / 底色", t("fg-subtle"), t("bg"), 4.5],
  ["弱化文字 / 抬升面", t("fg-subtle"), t("bg-elevated"), 4.5],
  ["用户气泡", t("accent-fill-fg"), t("accent-fill"), 4.5],
  ["accent 文字", t("accent"), t("bg"), 4.5],
  ["success", t("success"), t("bg"), 4.5],
  ["warning", t("warning"), t("bg"), 4.5],
  ["danger", t("danger"), t("bg"), 4.5],
  // 非文字：有意义的控件边界 3:1。装饰性分隔线（--color-border）不在此列，
  // 但也不该低到看不见 —— 原来 1.55:1 就是那个状态。
  ["控件边界", t("border-strong"), t("bg"), 3.0],
  ["装饰分隔线", t("border"), t("bg"), 2.0],
] as const;

for (const theme of ["dark", "light"] as const) {
  test(`${theme} 主题：所有文字/边界组合达标`, () => {
    const t = (n: string) => token(`${theme}-${n}`);
    const failures: string[] = [];
    for (const [label, fg, bg, need] of TEXT_PAIRS(t)) {
      const ratio = contrast(fg, bg);
      if (ratio < need) failures.push(`${label}: ${ratio.toFixed(2)}:1 < ${need}:1 (${fg} on ${bg})`);
    }
    assert.deepEqual(failures, [], `\n  ${failures.join("\n  ")}\n`);
  });
}

test("别再把实色 accent 当文字底色用", () => {
  // 这是 2.53:1 那条的来源。要「填充底 + 可读文字」就用 accent-fill 这一对。
  const bad = [...CSS.matchAll(/background:\s*var\(--color-accent\)\s*;/g)];
  assert.deepEqual(
    bad.map((m) => m[0]),
    [],
    "tokens.css 内不该出现；组件里若要实色填充请改用 --color-accent-fill / --color-accent-fill-fg",
  );
});
