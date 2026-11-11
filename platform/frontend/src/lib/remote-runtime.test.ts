import assert from "node:assert/strict";
import test from "node:test";
import { ApiClient } from "./api.ts";
import { availabilityPollInterval, canKeepCachedSession, changeProjectRuntime } from "./remote-runtime.ts";

function recordingClient(payload: unknown = {}) {
  const requests: { url: string; init: RequestInit }[] = [];
  const client = new ApiClient((async (url, init) => {
    requests.push({ url: String(url), init: init ?? {} });
    return new Response(JSON.stringify(payload), { status: 200 });
  }) as typeof fetch);
  client.askingAbout({ project: "previous-project" });
  return { client, requests };
}

test("remote folder browsing names the server and never inherits the current project", async () => {
  const { client, requests } = recordingClient({ available: true });
  await client.listProjectFolders("/srv/研究 data", "remote:lab");
  const headers = new Headers(requests[0].init.headers);
  assert.equal(headers.get("X-Remote-Server"), "remote:lab");
  assert.equal(headers.get("X-Project"), null);
  assert.equal(new URL(requests[0].url, "http://desktop").searchParams.get("path"), "/srv/研究 data");
});

test("changing runtime always resets the previous machine's root folder", () => {
  const before = { name: "Study", home: "", root_folder: "/Users/me/study" };
  assert.deepEqual({ ...before, ...changeProjectRuntime("remote:lab") }, {
    name: "Study", home: "remote:lab", root_folder: null,
  });
  assert.deepEqual(changeProjectRuntime(""), { home: "", root_folder: null });
});

test("server management and operation polling always stay on the desktop", async () => {
  const { client, requests } = recordingClient({ items: [] });
  await client.listRemoteServers();
  await client.remoteConnectionOperation("operation/1");
  await client.disconnectRemoteServer("remote:lab");
  for (const request of requests) {
    const headers = new Headers(request.init.headers);
    assert.equal(headers.get("X-Project"), null);
    assert.equal(headers.get("X-Remote-Server"), null);
  }
  assert.match(requests[2].url, /remote-servers\/remote%3Alab\/disconnect$/);
  assert.equal(requests[2].init.method, "POST");
  assert.equal(requests.some((request) => /cancel|terminate/.test(request.url)), false);
});

test("remote model credentials and role assignments target only their selected server", async () => {
  const { client, requests } = recordingClient({ roles: [] });
  const models = client.remoteModels("remote:lab");
  await models.roles();
  await models.setRoleDefault("model/1", "reasoning");
  await models.setEnabled("model/1", true);
  assert.equal(requests.length, 3);
  for (const request of requests) {
    const headers = new Headers(request.init.headers);
    assert.equal(headers.get("X-Remote-Server"), "remote:lab");
    assert.equal(headers.get("X-Project"), null);
  }
  assert.match(requests[1].url, /model%2F1\/roles\/reasoning\/default$/);
});

test("project creation sends its server and server-side root together", async () => {
  const { client, requests } = recordingClient({ id: "created" });
  await client.createProject({ name: "Long study", home: "remote:lab", root_folder: "/srv/study" });
  assert.deepEqual(JSON.parse(String(requests[0].init.body)), {
    name: "Long study", home: "remote:lab", root_folder: "/srv/study",
  });
});

test("availability checks are bounded read-only requests on the desktop", async () => {
  const { client, requests } = recordingClient({ state: "unavailable" });
  await client.remoteServerAvailability("remote:lab");
  const request = requests[0];
  assert.match(request.url, /remote-servers\/remote%3Alab\/availability$/);
  assert.equal(new Headers(request.init.headers).get("X-Project"), null);
  assert.equal(request.init.method ?? "GET", "GET");
  assert.ok(request.init.signal instanceof AbortSignal);
  assert.equal(requests.some((item) => /stream|cancel|stop/.test(item.url)), false);
});

test("unavailable APIs are checked less often without changing task state", () => {
  assert.equal(availabilityPollInterval("online"), 5000);
  assert.equal(availabilityPollInterval("unavailable"), 15000);
  assert.equal(availabilityPollInterval("disconnected"), 15000);
});

test("cached conversations remain readable when a background refresh fails", () => {
  const session = { id: "session", status: "running" };
  const messages = { items: [{ role: "assistant", content: "Previous progress" }] };
  assert.equal(canKeepCachedSession(session, messages), true);
  assert.equal(canKeepCachedSession(session, { items: [] }), true);
  assert.equal(canKeepCachedSession(undefined, messages), false);
  assert.equal(canKeepCachedSession(session, undefined), false);
  assert.equal(session.status, "running");
});
