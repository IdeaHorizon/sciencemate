import assert from "node:assert/strict";
import test from "node:test";
import { feedTranslationReady } from "./feed-translation-ready.ts";

test("cards wait for both title and existing abstract, including partial responses", () => {
  const item = { title: "Irregular particle settling", summary: "Lattice Boltzmann fluid simulation." };
  assert.equal(feedTranslationReady(item), false);
  assert.equal(feedTranslationReady(item, { titleZh: "不规则颗粒沉降" }), false);
  assert.equal(feedTranslationReady(item, { abstractZh: "流体模拟" }), false);
  assert.equal(feedTranslationReady(item, { titleZh: "不规则颗粒沉降", abstractZh: "流体模拟" }), true);
  assert.equal(feedTranslationReady(item, { titleZh: " ", abstractZh: "流体模拟" }), false);
});

test("Chinese text and missing abstracts do not wait for nonexistent translation", () => {
  assert.equal(feedTranslationReady({ title: "颗粒沉降", summary: "流体模拟" }), true);
  assert.equal(feedTranslationReady({ title: "Particle settling", summary: null }), false);
  assert.equal(feedTranslationReady({ title: "Particle settling", summary: null }, { titleZh: "颗粒沉降" }), true);
  assert.equal(feedTranslationReady({ title: "颗粒沉降", summary: "Fluid simulation" }), false);
});
