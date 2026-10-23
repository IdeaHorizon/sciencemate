import assert from "node:assert/strict";
import test from "node:test";
import { FeedTranslationQueue, FEED_TRANSLATION_BATCH_SIZE } from "./feed-translation-queue.ts";
import type { LiteratureTranslatePaperInput, LiteratureTranslateResponse, LiteratureTranslation } from "@/lib/api";

const paper = (key: string): LiteratureTranslatePaperInput => ({
  key, title: `Veterinary medicine ${key}`, abstract: "Animal health study.", authors: [], year: 2026, venue: null,
});
const translated = (papers: LiteratureTranslatePaperInput[]): LiteratureTranslateResponse => ({
  status: "ok", diagnostics: {},
  translations: papers.map(({ key }) => ({ key, title_zh: "兽医学", abstract_zh: "动物健康研究。", ai_summary: null })),
});

test("21-card pages stay bounded and scrolling preserves earlier translations", async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const calls: number[] = [];
  const published: string[] = [];
  let inFlight = 0;
  const queue = new FeedTranslationQueue(async (batch) => {
    assert.equal(++inFlight, 1);
    calls.push(batch.length);
    if (calls.length === 1) await gate;
    inFlight--;
    return translated(batch);
  }, (values) => published.push(...values.map((value) => value.key)), 0);
  const first = Array.from({ length: 21 }, (_, index) => paper(String(index)));
  const running = queue.enqueue(first);
  queue.enqueue([...first, ...Array.from({ length: 21 }, (_, index) => paper(String(index + 21)))]);
  release();
  await running;
  assert.equal(published.length, 42);
  assert.equal(new Set(published).size, 42);
  assert.ok(calls.every((count) => count <= FEED_TRANSLATION_BATCH_SIZE));
  assert.ok(published.includes("0") && published.includes("41"));
});

test("partial title/abstract responses are merged and missing fields retried", async () => {
  let calls = 0;
  const updates: LiteratureTranslation[] = [];
  const queue = new FeedTranslationQueue(async () => ({
    status: "ok", diagnostics: {}, translations: [{
      key: "a", title_zh: ++calls === 1 ? null : "兽医学",
      abstract_zh: calls === 1 ? "动物健康研究。" : null, ai_summary: null,
    }],
  }), (values) => updates.push(...values), 0);
  await queue.enqueue([paper("a")]);
  assert.equal(calls, 2);
  assert.equal(updates.at(-1)?.title_zh, "兽医学");
  assert.equal(updates.at(-1)?.abstract_zh, "动物健康研究。");
});

test("failure retries are bounded and later cards still run", async () => {
  let calls = 0;
  const keys: string[] = [];
  const queue = new FeedTranslationQueue(async (batch) => {
    calls++;
    if (batch[0].key === "0") throw new Error("timeout");
    return translated(batch);
  }, (values) => keys.push(...values.map((value) => value.key)), 0);
  await queue.enqueue(Array.from({ length: 4 }, (_, index) => paper(String(index))));
  assert.equal(calls, 3);
  assert.deepEqual(keys, ["3"]);
});

test("unmount stops queued work and prevents late result publication", async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let calls = 0;
  const queue = new FeedTranslationQueue(async (batch) => {
    calls++; await gate; return translated(batch);
  }, () => assert.fail("disposed worker published results"), 0);
  const running = queue.enqueue(Array.from({ length: 6 }, (_, index) => paper(String(index))));
  queue.dispose(); release(); await running;
  assert.equal(calls, 1);
});
