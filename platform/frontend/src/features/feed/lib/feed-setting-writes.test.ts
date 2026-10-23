import assert from "node:assert/strict";
import test from "node:test";
import { waitForFeedSettingWrites } from "./feed-setting-writes.ts";

function fixture(keys: string[][]) {
  const mutations = keys.map((mutationKey) => ({ options: { mutationKey }, state: { status: "pending" } }));
  const listeners = new Set<() => void>();
  const client = { getMutationCache: () => ({ getAll: () => mutations, subscribe: (listener: () => void) => { listeners.add(listener); return () => listeners.delete(listener); } }) } as unknown as Parameters<typeof waitForFeedSettingWrites>[0];
  return { client, listeners, finish(index: number, status = "success") { mutations[index].state.status = status; for (const listener of listeners) listener(); } };
}

test("unrelated pending work does not block returning to recommendations", async () => {
  const state = fixture([["chat", "send"]]);
  await waitForFeedSettingWrites(state.client);
  assert.equal(state.listeners.size, 0);
});

test("recommendations wait for all queued subscription writes", async () => {
  const state = fixture([["feed", "save-subscriptions"], ["feed", "save-subscriptions"]]);
  let refreshed = false;
  const done = waitForFeedSettingWrites(state.client).then(() => { refreshed = true; });
  state.finish(0);
  await Promise.resolve();
  assert.equal(refreshed, false);
  state.finish(1);
  await done;
  assert.equal(refreshed, true);
  assert.equal(state.listeners.size, 0);
});

test("domain and curation writes both settle before refresh, including failed writes", async () => {
  const state = fixture([["feed", "save-interests"], ["feed", "save-curation"]]);
  let refreshed = false;
  const done = waitForFeedSettingWrites(state.client).then(() => { refreshed = true; });
  state.finish(1);
  await Promise.resolve();
  assert.equal(refreshed, false);
  state.finish(0, "error");
  await done;
  assert.equal(refreshed, true);
});
