import test from "node:test";
import assert from "node:assert/strict";
import { ApiClient } from "./api.ts";

test("authenticated transport attaches the current bearer token and handles 401", async () => {
  const requests: RequestInit[] = [];
  const client = new ApiClient((async (_input: string | URL | Request, init?: RequestInit) => {
    requests.push(init ?? {});
    return new Response(null, { status: 401 });
  }) as typeof fetch);
  let unauthorized = 0;
  client.setToken("session-token");
  client.onUnauthorized(() => { unauthorized += 1; });

  await client.fetchWithAuth("http://api.test/sessions/one/events", {
    headers: { Accept: "application/json" },
  });

  const headers = new Headers(requests[0].headers);
  assert.equal(headers.get("Authorization"), "Bearer session-token");
  assert.equal(headers.get("Accept"), "application/json");
  assert.equal(unauthorized, 1);
});

test("project run listing uses the canonical projectId query through authenticated transport", async () => {
  const inputs: string[] = [];
  const client = new ApiClient((async (input: string | URL | Request) => {
    inputs.push(String(input));
    return new Response(JSON.stringify({ items: [], nextCursor: null }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch);
  client.setToken("session-token");

  const response = await client.listRuns("project/a", 12);

  assert.deepEqual(response.items, []);
  assert.match(inputs[0], /\/runs\?projectId=project%2Fa&limit=12$/);
});

test("visible run listing omits projectId instead of inventing a global project", async () => {
  const inputs: string[] = [];
  const client = new ApiClient((async (input: string | URL | Request) => {
    inputs.push(String(input));
    return new Response(JSON.stringify({ items: [], nextCursor: null }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch);

  await client.listRuns(undefined, 4);

  assert.match(inputs[0], /\/runs\?limit=4$/);
  assert.equal(inputs[0].includes("projectId"), false);
});

test("session run listing addresses the canonical Session directly", async () => {
  const inputs: string[] = [];
  const client = new ApiClient((async (input: string | URL | Request) => {
    inputs.push(String(input));
    return new Response(JSON.stringify({ items: [], nextCursor: null }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch);

  await client.listRuns(undefined, 10, "session/a");

  assert.match(inputs[0], /\/runs\?sessionId=session%2Fa&limit=10$/);
});

test("Run detail uses an encoded identity and preserves canonical eventCount", async () => {
  const inputs: string[] = [];
  const responseBody = {
    run: { id: "run/a" },
    attempts: [],
    eventCount: 4,
  };
  const client = new ApiClient((async (input: string | URL | Request) => {
    inputs.push(String(input));
    return new Response(JSON.stringify(responseBody), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch);

  const response = await client.getRun("run/a");

  assert.match(inputs[0], /\/runs\/run%2Fa$/);
  assert.equal(response.eventCount, 4);
});

test("Project profile update PATCHes only persisted metadata through authenticated transport", async () => {
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const responseBody = {
    id: "project/a",
    name: "Updated project",
    description: null,
    research_domain: "Computational science",
    status: "active",
    entry_type: null,
    capabilities: ["manage_settings"],
    created_at: "2026-08-04T00:00:00Z",
    updated_at: "2026-08-04T01:00:00Z",
  };
  const client = new ApiClient((async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ input: String(input), init });
    return new Response(JSON.stringify(responseBody), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch);
  client.setToken("session-token");

  const updated = await client.updateProject("project/a", {
    name: "Updated project",
    research_domain: "Computational science",
    description: null,
  });

  assert.match(requests[0].input, /\/projects\/project%2Fa$/);
  assert.equal(requests[0].init?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(requests[0].init?.body)), {
    name: "Updated project",
    research_domain: "Computational science",
    description: null,
  });
  assert.equal(new Headers(requests[0].init?.headers).get("Authorization"), "Bearer session-token");
  assert.equal(updated.capabilities?.includes("manage_settings"), true);
});

test("personal research settings use an authenticated atomic replace contract", async () => {
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const responseBody = {
    response_language: "zh-CN",
    citation_style: "author_year",
    evidence_standard: "strict",
    memory_enabled: false,
    instructions: [],
    effective_layers: [],
    updated_at: "2026-08-04T00:00:00Z",
  };
  const client = new ApiClient((async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ input: String(input), init });
    return new Response(JSON.stringify(responseBody), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch);
  client.setToken("session-token");

  const loaded = await client.getResearchSettings();
  const saved = await client.saveResearchSettings({
    response_language: "zh-CN",
    citation_style: "author_year",
    evidence_standard: "strict",
    memory_enabled: false,
    instructions: [],
  });

  assert.equal(loaded.evidence_standard, "strict");
  assert.equal(saved.response_language, "zh-CN");
  assert.match(requests[0].input, /\/settings\/research$/);
  assert.equal(requests[1].init?.method, "PUT");
  assert.deepEqual(JSON.parse(String(requests[1].init?.body)), {
    response_language: "zh-CN",
    citation_style: "author_year",
    evidence_standard: "strict",
    memory_enabled: false,
    instructions: [],
  });
  assert.equal(new Headers(requests[1].init?.headers).get("Authorization"), "Bearer session-token");
});

test("settings center contracts use authenticated canonical endpoints and methods", async () => {
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const interfaceSettings = {
    theme: "dark",
    density: "compact",
    font_scale: 110,
    reduce_motion: true,
    language: "en",
    default_landing: "projects",
    show_run_usage: false,
    onboarding_done: false,
    project_guide_done: false,
    execution_detail: "standard",
    auto_collapse_completed_tools: true,
    auto_collapse_completed_steps: true,
    follow_active_run: true,
  } as const;
  const client = new ApiClient((async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ input: String(input), init });
    const path = String(input);
    const body = path.endsWith("/settings/usage")
      ? { session_count: 1, run_count: 2, prompt_tokens: 3, completion_tokens: 4, total_tokens: 7, retry_count: 0, known_cost: 0, cost_currency: null, cost_known_runs: 0, cost_unknown_runs: 2, active_days: 1, current_streak: 1, longest_streak: 1, daily: [] }
      : path.endsWith("/settings/notifications")
        ? { decision_required: true, run_failed: true, run_completed: false, budget_warning: true, delivery_capabilities: ["in_app"] }
      : path.endsWith("/settings/governance/members")
        ? { scope: { kind: "individual", id: "u1", name: "Individual" }, editable: false, members: [], effective_permissions: ["sessions.create"], policy_sources: [] }
        : path.endsWith("/auth/me")
          ? { id: "u1", email: "u@example.edu", display_name: "Updated", is_active: true, role: "researcher" }
          : interfaceSettings;
    // A password change answers with a replacement credential, because it has
    // just invalidated the one the caller sent.
    if (path.endsWith("/auth/change-password")) {
      return new Response(
        JSON.stringify({
          access_token: "rotated-token",
          token_type: "bearer",
          changed_at: "2026-08-17T04:00:00Z",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(init?.method === "POST" ? null : JSON.stringify(body), { status: init?.method === "POST" ? 204 : 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch);
  client.setToken("session-token");

  await client.getInterfaceSettings();
  await client.saveInterfaceSettings(interfaceSettings);
  await client.updateCurrentUser({ display_name: "Updated" });
  await client.getUsageSettings();
  await client.getNotificationSettings();
  await client.saveNotificationSettings({ decision_required: false, run_failed: true, run_completed: true, budget_warning: false });

  assert.deepEqual(requests.map((request) => new URL(request.input, "http://platform.test").pathname), [
    "/api/v1/settings/interface",
    "/api/v1/settings/interface",
    "/api/v1/auth/me",
    "/api/v1/settings/usage",
    "/api/v1/settings/notifications",
    "/api/v1/settings/notifications",
  ]);
  assert.deepEqual(requests.map((request) => request.init?.method ?? "GET"), ["GET", "PUT", "PATCH", "GET", "GET", "PUT"]);
  assert.deepEqual(JSON.parse(String(requests[5].init?.body)), {
    decision_required: false,
    run_failed: true,
    run_completed: true,
    budget_warning: false,
  });
  // 改密码（token 轮换）和组织名录是专业版的：src/pro/lib/api-auth.test.ts。
});

test("Project resources use the authenticated registry contract without exposing secret values", async () => {
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const client = new ApiClient((async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ input: String(input), init });
    return new Response(JSON.stringify(init?.method === "POST" ? {
      id: "resource-1",
      project_id: "project/a",
      resource_type: "compute",
      name: "Local worker",
      provider: "local",
      description: null,
      endpoint: null,
      workspace_binding: "project-a",
      config: {},
      is_enabled: true,
      health_status: "unknown",
      has_secret_reference: true,
      created_by_user_id: "user-1",
      updated_by_user_id: "user-1",
      created_at: "2026-08-04T00:00:00Z",
      updated_at: "2026-08-04T00:00:00Z",
      disabled_at: null,
    } : []), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch);
  client.setToken("session-token");

  await client.listProjectResources("project/a", { includeDisabled: true });
  const created = await client.createProjectResource("project/a", {
    resource_type: "compute",
    name: "Local worker",
    provider: "local",
    workspace_binding: "project-a",
    secret_ref: "env://LOCAL_MODEL_KEY",
  });

  assert.match(requests[0].input, /\/projects\/project%2Fa\/resources\?include_disabled=true$/);
  assert.equal(requests[1].init?.method, "POST");
  assert.equal(JSON.parse(String(requests[1].init?.body)).secret_ref, "env://LOCAL_MODEL_KEY");
  assert.equal(created.has_secret_reference, true);
  assert.equal("secret_ref" in created, false);
  assert.equal(new Headers(requests[1].init?.headers).get("Authorization"), "Bearer session-token");
});

test("compute inventory reads the real local inventory endpoint", async () => {
  const inputs: string[] = [];
  const client = new ApiClient((async (input: string | URL | Request) => {
    inputs.push(String(input));
    return new Response(JSON.stringify({
      scope: "local_development",
      observed_at: "2026-08-04T00:00:00Z",
      health: { status: "unknown", checks: [] },
      nodes: [],
      schedulers: [],
      capacity: {
        status: "unknown",
        cpu_logical_cores: null,
        memory_total_bytes: null,
        memory_available_bytes: null,
        storage_total_bytes: null,
        storage_free_bytes: null,
        gpu_count: null,
        gpu_memory_total_bytes: null,
        gpu_memory_available_bytes: null,
      },
      recent_jobs: { supported: false, items: [] },
      limitations: ["GPU probe unavailable"],
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch);

  const inventory = await client.getComputeInventory();

  assert.match(inputs[0], /\/compute\/inventory$/);
  assert.equal(inventory.capacity.gpu_count, null);
  assert.equal(inventory.recent_jobs.supported, false);
});
