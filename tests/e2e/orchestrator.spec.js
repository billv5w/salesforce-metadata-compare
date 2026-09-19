const { test, expect } = require("@playwright/test");
const { apiContext } = require("./helpers.cjs");

// API calls need the per-server session token (X-MCT-Token). apiContext
// bootstraps it from the same-origin HTML exactly like the browser does.
let api;
test.beforeAll(async () => {
  api = await apiContext("http://127.0.0.1:8091");
});
test.afterAll(async () => {
  if (api) await api.dispose();
});

test.describe("Orchestrator UI — page structure", () => {
  test("page loads with title and brand heading", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/metadata compare/i);
    await expect(page.locator(".kit-orch-brand-title")).toContainText("Metadata Compare");
  });

  test("all major sections are present in the DOM", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator('.kit-orch-section[data-section-id="retrieve"]')).toBeAttached();
    await expect(page.locator('.kit-orch-section[data-section-id="compare"]')).toBeAttached();
    await expect(page.locator('.kit-orch-section[data-section-id="history"]')).toBeAttached();
  });

  test("nav switches the active section", async ({ page }) => {
    await page.goto("/");
    // Retrieve is shown first by default (config.sections[0])
    await expect(page.locator('.kit-orch-section[data-section-id="retrieve"]')).toHaveClass(
      /is-active/
    );

    await page.click('.kit-orch-nav-btn[data-section-id="compare"]');
    await expect(page.locator('.kit-orch-section[data-section-id="compare"]')).toHaveClass(
      /is-active/
    );
    await expect(page.locator('.kit-orch-section[data-section-id="retrieve"]')).not.toHaveClass(
      /is-active/
    );

    await page.click('.kit-orch-nav-btn[data-section-id="history"]');
    await expect(page.locator('.kit-orch-section[data-section-id="history"]')).toHaveClass(
      /is-active/
    );
    await expect(page.locator('.kit-orch-section[data-section-id="compare"]')).not.toHaveClass(
      /is-active/
    );
  });

  test("workspace control and status bar are present in the header", async ({ page }) => {
    await page.goto("/");
    await expect(
      page.locator(".kit-orch-header-actions button", { hasText: "No workspace" })
    ).toBeAttached();
    await expect(page.locator(".kit-orch-status")).toBeAttached();
  });
});

test.describe("Orchestrator API — workspaces", () => {
  test("GET /api/workspaces returns ok with array", async () => {
    const res = await api.get("/api/workspaces");
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.ok).toBe(true);
    expect(Array.isArray(body.workspaces)).toBe(true);
  });

  test("POST /api/workspaces requires name", async () => {
    const res = await api.post("/api/workspaces", {
      data: { repo_root: "/tmp" },
    });
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.ok).toBe(false);
    expect(body.error).toMatch(/name/i);
  });

  test("workspace CRUD: create, list, delete", async () => {
    const name = `test-ws-${Date.now()}`;
    const createRes = await api.post("/api/workspaces", {
      data: { name, repo_root: "/tmp/fake-project", branch: "main", org: "test-org" },
    });
    const created = await createRes.json();
    expect(created.ok).toBe(true);
    expect(created.workspace).toBeDefined();
    expect(created.workspace.name).toBe(name);
    const wsId = created.workspace.id;

    const listRes = await api.get("/api/workspaces");
    const listed = await listRes.json();
    expect(listed.workspaces.some((w) => w.id === wsId)).toBe(true);

    const delRes = await api.delete(`/api/workspaces?id=${wsId}`);
    const deleted = await delRes.json();
    expect(deleted.ok).toBe(true);

    const listAfter = await api.get("/api/workspaces");
    const listedAfter = await listAfter.json();
    expect(listedAfter.workspaces.some((w) => w.id === wsId)).toBe(false);
  });

  test("DELETE /api/workspaces with missing id returns 400", async () => {
    const res = await api.delete("/api/workspaces");
    expect(res.status()).toBe(400);
  });

  test("DELETE /api/workspaces with unknown id returns 404", async () => {
    const res = await api.delete("/api/workspaces?id=nonexistent-id-xyz");
    expect(res.status()).toBe(404);
  });

  test("POST /api/workspaces with existing name updates in place", async () => {
    // Create
    const r1 = await api.post("/api/workspaces", {
      data: { name: "upsert-test", repo_root: "/tmp/a", branch: "main", org: "dev" },
    });
    const ws1 = await r1.json();
    expect(ws1.ok).toBe(true);
    const id = ws1.workspace.id;

    // Upsert same name with different repo_root
    const r2 = await api.post("/api/workspaces", {
      data: { name: "upsert-test", repo_root: "/tmp/b", branch: "main", org: "dev" },
    });
    const ws2 = await r2.json();
    expect(ws2.ok).toBe(true);
    expect(ws2.workspace.id).toBe(id); // same id preserved
    expect(ws2.workspace.repo_root).toBe("/tmp/b"); // updated value

    // List: only one entry with that name
    const list = await api.get("/api/workspaces");
    const data = await list.json();
    const matches = data.workspaces.filter((w) => w.name === "upsert-test");
    expect(matches.length).toBe(1);

    // Cleanup
    await api.delete(`/api/workspaces?id=${id}`);
  });
});

test.describe("Orchestrator API — snapshots", () => {
  test("GET /api/snapshots returns ok with data", async () => {
    const res = await api.get("/api/snapshots");
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.ok).toBe(true);
    expect(body.data).toBeDefined();
    expect(Array.isArray(body.data.snapshots)).toBe(true);
  });

  test("GET /api/list returns ok", async () => {
    const res = await api.get("/api/list");
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.ok).toBe(true);
  });
});

test.describe("Orchestrator API — validate-repo", () => {
  test("requires repo_root parameter", async () => {
    const res = await api.get("/api/validate-repo");
    expect(res.status()).toBe(400);
  });

  test("returns flags for a valid directory", async () => {
    const res = await api.get(
      `/api/validate-repo?repo_root=${encodeURIComponent(process.cwd())}`
    );
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.ok).toBe(true);
    expect(typeof body.exists).toBe("boolean");
    expect(typeof body.has_git).toBe("boolean");
    expect(typeof body.has_force_app_default).toBe("boolean");
  });

  test("returns exists=false for nonexistent path", async () => {
    const res = await api.get("/api/validate-repo?repo_root=/nonexistent/path/abc123");
    const body = await res.json();
    expect(body.ok).toBe(true);
    expect(body.exists).toBe(false);
  });
});

test.describe("Orchestrator API — error handling", () => {
  test("GET unknown path returns 404", async () => {
    const res = await api.get("/api/does-not-exist");
    expect(res.status()).toBe(404);
  });

  test("POST unknown path returns 404 JSON", async () => {
    const res = await api.post("/api/does-not-exist", { data: {} });
    expect(res.status()).toBe(404);
    const body = await res.json();
    expect(body.ok).toBe(false);
  });

  test("POST with invalid JSON body returns 400", async () => {
    const res = await api.fetch("/api/snapshot-all", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": "17",
      },
      data: Buffer.from("not valid json{{{"),
    });
    expect(res.status()).toBe(400);
  });

  test("POST /api/delete-snapshots requires repo_root", async () => {
    const res = await api.post("/api/delete-snapshots", { data: {} });
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.ok).toBe(false);
    expect(body.error).toMatch(/repo_root/i);
  });

  test("POST /api/delete-snapshots requires ids or all flag", async () => {
    const res = await api.post("/api/delete-snapshots", {
      data: { repo_root: "/tmp" },
    });
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.ok).toBe(false);
  });

  test("DELETE on non-workspace path returns 404", async () => {
    const res = await api.delete("/api/snapshots");
    expect(res.status()).toBe(404);
  });
});

test.describe("Orchestrator API — compare input validation", () => {
  test("POST /api/compare is deprecated with migration guidance", async () => {
    const res = await api.post("/api/compare", {
      data: { repo_root: "/tmp", left: "nonexistent-snap-abc", right: "nonexistent-snap-xyz" },
    });
    expect(res.status()).toBe(410);
    const body = await res.json();
    expect(body.ok).toBe(false);
    expect(body.error).toMatch(/deprecated/i);
    expect(body.error).toMatch(/launch-diff-ui/i);
  });

  test("POST /api/launch-diff-ui returns JSON with url or error", async () => {
    const res = await api.post("/api/launch-diff-ui", {
      data: { repo_root: "/tmp", left: "nonexistent-left", right: "nonexistent-right", port: 0 },
    });
    const body = await res.json();
    expect(body).toHaveProperty("ok");
  });
});
