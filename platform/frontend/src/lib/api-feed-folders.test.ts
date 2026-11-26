import test from "node:test";
import assert from "node:assert/strict";
import { ApiClient, ApiError } from "./api.ts";

function clientWithRequests(body: unknown = {}) {
  const requests: { url: string; init: RequestInit }[] = [];
  const client = new ApiClient((async (input, init) => {
    requests.push({ url: String(input), init: init ?? {} });
    return new Response(JSON.stringify(body), { status: 200 });
  }) as typeof fetch);
  return { client, requests };
}

test("default saves retain the old payload, while selecting a folder explicitly sends its identity", async () => {
  const { client, requests } = clientWithRequests();
  await client.recordFeedEngagement("article/a", "save");
  await client.recordFeedEngagement("article/a", "save", "folder-one");
  assert.deepEqual(JSON.parse(String(requests[0].init.body)), { action: "save" });
  assert.deepEqual(JSON.parse(String(requests[1].init.body)), { action: "save", folder_id: "folder-one" });
  assert.match(requests[0].url, /\/feed\/items\/article%2Fa\/engagement$/);
});

test("moving a saved article uses one update, including moving back to default", async () => {
  const { client, requests } = clientWithRequests();
  await client.moveSavedFeedItem("article/a", "folder-two");
  await client.moveSavedFeedItem("article/a", null);
  assert.deepEqual(requests.map((request) => request.init.method), ["PUT", "PUT"]);
  assert.ok(requests.every((request) => request.url.endsWith("/feed/items/article%2Fa/save")));
  assert.deepEqual(requests.map((request) => JSON.parse(String(request.init.body))), [
    { folder_id: "folder-two" }, { folder_id: null },
  ]);
});

test("saved list filtering distinguishes all folders, the default folder and a named folder", async () => {
  const { client, requests } = clientWithRequests([]);
  await client.listFeedItems({ saved: true });
  await client.listFeedItems({ saved: true, folder_id: "default" });
  await client.listFeedItems({ saved: true, folder_id: "folder-one" });
  assert.deepEqual(requests.map((request) => new URL(request.url, "http://local").searchParams.get("folder_id")), [null, "default", "folder-one"]);
  assert.ok(requests.every((request) => new URL(request.url, "http://local").searchParams.get("saved") === "true"));
});

test("a missing folder endpoint on an older server leaves default saving usable", async () => {
  const requests: RequestInit[] = [];
  const client = new ApiClient((async (input, init) => {
    requests.push(init ?? {});
    return String(input).endsWith("/feed/folders")
      ? new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 })
      : new Response("{}", { status: 200 });
  }) as typeof fetch);
  assert.deepEqual(await client.getFeedFolders(), { folders: [], default_count: 0, supported: false });
  await client.recordFeedEngagement("article", "save");
  assert.deepEqual(JSON.parse(String(requests[1].body)), { action: "save" });
});

test("folder permissions and network failures are reported instead of being treated as an old server", async () => {
  const client = new ApiClient((async () => new Response(JSON.stringify({ detail: "Forbidden" }), { status: 403 })) as typeof fetch);
  await assert.rejects(client.getFeedFolders(), (error) => error instanceof ApiError && error.status === 403);
});

test("new folder and academic-search saves preserve the chosen location in their requests", async () => {
  const { client, requests } = clientWithRequests({ id: "new-folder", name: "Reading", item_count: 0 });
  await client.createFeedFolder("Reading");
  await client.savePaperToFeed({ title: "Paper", doi: null, url: null, authors: [], venue: null,
    published: null, year: null, abstract: null, folder_id: "new-folder" });
  assert.match(requests[0].url, /\/feed\/folders$/);
  assert.deepEqual(JSON.parse(String(requests[0].init.body)), { name: "Reading" });
  assert.equal(JSON.parse(String(requests[1].init.body)).folder_id, "new-folder");
});

const searchPaper = {
  title: "Paper 101", doi: "10.1234/paper-101", url: "https://doi.org/10.1234/paper-101",
  authors: [], venue: null, published: null, year: null, abstract: null,
};

function savedPaper(number: number) {
  return { id: `paper-${number}`, kind: "paper", title: `Paper ${number}`,
    url: `https://doi.org/10.1234/paper-${number}`, summary: null, authors: [], venue: null,
    published_at: null, domains: [], domain_labels: [], source_name: null,
    author_display_name: null, saved: true, saved_folder_id: "reading", viewed: false,
    extra: { doi: `10.1234/paper-${number}` } };
}

test("the complete save index includes article 101 and later pages, then moves the same save to default", async () => {
  const items = Array.from({ length: 205 }, (_, index) => savedPaper(index + 1));
  const offsets: number[] = [];
  const originalSave = { id: "save-101", created_at: "2026-11-01T12:00:00Z", folder_id: "reading" as string | null };
  const savePayloads: Record<string, unknown>[] = [];
  const client = new ApiClient((async (input, init) => {
    const url = new URL(String(input), "http://local");
    if (url.pathname.endsWith("/feed/items")) {
      const offset = Number(url.searchParams.get("offset"));
      offsets.push(offset);
      return new Response(JSON.stringify(items.slice(offset, offset + 100)), { status: 200 });
    }
    if (url.pathname.endsWith("/feed/folders")) {
      return new Response(JSON.stringify({ folders: [{ id: "reading", name: "Reading", item_count: 205 }], default_count: 0 }), { status: 200 });
    }
    assert.ok(url.pathname.endsWith("/feed/saved-papers"));
    const data = JSON.parse(String(init?.body));
    savePayloads.push(data);
    if (Object.hasOwn(data, "folder_id")) originalSave.folder_id = data.folder_id;
    return new Response(JSON.stringify({ ...items[100], saved_folder_id: originalSave.folder_id }), { status: 200 });
  }) as typeof fetch);
  const allSaved = await client.listSavedFeedItems();
  assert.equal(allSaved.length, 205);
  assert.deepEqual(offsets, [0, 100, 200]);
  const { indexSavedItems, savedItemIdFor } = await import("../features/academic-search/lib/saved-papers.ts");
  assert.equal(savedItemIdFor(indexSavedItems(allSaved), searchPaper), "paper-101");
  const result = await client.savePaperToFeedFolder(searchPaper, null);
  assert.equal(savePayloads.length, 1);
  assert.equal(Object.hasOwn(savePayloads[0], "folder_id"), true);
  assert.equal(savePayloads[0].folder_id, null);
  assert.equal(result.saved_folder_id, null);
  assert.deepEqual(originalSave, { id: "save-101", created_at: "2026-11-01T12:00:00Z", folder_id: null });
});

test("overlapping saved pages are deduplicated and a server ignoring offset terminates", async () => {
  const hundred = Array.from({ length: 100 }, (_, index) => savedPaper(index));
  let calls = 0;
  const oldClient = new ApiClient((async () => {
    calls += 1;
    assert.ok(calls <= 2, "an ignored offset must not request an unbounded sequence");
    return new Response(JSON.stringify(hundred), { status: 200 });
  }) as typeof fetch);
  assert.equal((await oldClient.listSavedFeedItems()).length, 100);
  assert.equal(calls, 2);

  const pages = [hundred, [...hundred.slice(50), ...Array.from({ length: 50 }, (_, i) => savedPaper(i + 100))], []];
  let page = 0;
  const overlapping = new ApiClient((async () => new Response(JSON.stringify(pages[page++]), { status: 200 })) as typeof fetch);
  const complete = await overlapping.listSavedFeedItems();
  assert.equal(complete.length, 150);
  assert.equal(new Set(complete.map((item) => item.id)).size, 150);
  assert.equal(page, 3);
});

test("default academic saves omit the new field only for an old server with no folders endpoint", async () => {
  const bodies: Record<string, unknown>[] = [];
  const client = new ApiClient((async (input, init) => {
    if (String(input).endsWith("/feed/folders")) return new Response("{}", { status: 404 });
    bodies.push(JSON.parse(String(init?.body)));
    const legacy: Partial<ReturnType<typeof savedPaper>> = { ...savedPaper(101) };
    delete legacy.saved_folder_id;
    return new Response(JSON.stringify(legacy), { status: 200 });
  }) as typeof fetch);
  const result = await client.savePaperToFeedFolder({ ...searchPaper, folder_id: "outdated-selection" }, null);
  assert.equal(Object.hasOwn(bodies[0], "folder_id"), false);
  assert.equal(result.saved, true);
  assert.equal(result.saved_folder_id, undefined);
});

test("academic save returns the server's location rather than inventing the requested location", async () => {
  const client = new ApiClient((async () => new Response(JSON.stringify({
    ...savedPaper(101), saved_folder_id: "server-confirmed-location",
  }), { status: 200 })) as typeof fetch);
  const result = await client.savePaperToFeedFolder(searchPaper, "requested-location");
  assert.equal(result.saved_folder_id, "server-confirmed-location");
});

test("feed-card saves also send explicit default on new servers while retaining the legacy body", async () => {
  for (const supportsFolders of [true, false]) {
    let body: Record<string, unknown> | undefined;
    const client = new ApiClient((async (input, init) => {
      if (String(input).endsWith("/feed/folders")) return supportsFolders
        ? new Response(JSON.stringify({ folders: [], default_count: 0 }), { status: 200 })
        : new Response("{}", { status: 404 });
      body = JSON.parse(String(init?.body));
      return new Response(null, { status: 204 });
    }) as typeof fetch);
    await client.saveFeedItemToFolder("article", null);
    assert.deepEqual(body, supportsFolders ? { action: "save", folder_id: null } : { action: "save" });
  }
});
