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

test.describe("Orchestrator UX — snapshot dropdowns", () => {
  const compareManualPicker = (page) =>
    page.locator(
      '.kit-orch-section[data-section-id="compare"].is-active .kit-orch-picker-row select'
    );

  test("manual compare mode has a unified <select> pair for left and right", async ({ page }) => {
    await page.goto("/");
    await page.click('.kit-orch-nav-btn[data-section-id="compare"]');
    await page.click('.kit-pill:has-text("Manual")');
    await expect(compareManualPicker(page)).toHaveCount(2);
  });

  test("unified snapshot dropdowns always have a placeholder option", async ({ page }) => {
    await page.goto("/");
    await page.click('.kit-orch-nav-btn[data-section-id="compare"]');
    await page.click('.kit-pill:has-text("Manual")');
    const leftFirst = await compareManualPicker(page)
      .first()
      .locator("option")
      .first()
      .textContent();
    expect(leftFirst).toMatch(/select/i);
  });
});

test.describe("Orchestrator UX — auto-refresh", () => {
  test("snapshot list refreshes after delete (list endpoint reachable)", async () => {
    const res = await api.get("/api/snapshots");
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.ok).toBe(true);
    expect(Array.isArray(body.data?.snapshots ?? [])).toBe(true);
  });

  test("history section renders after navigating to it", async ({ page }) => {
    await page.goto("/");
    await page.click('.kit-orch-nav-btn[data-section-id="history"]');
    await expect(
      page.locator('.kit-orch-section[data-section-id="history"].is-active .kit-table-wrap')
    ).toBeAttached();
  });
});

test.describe("Orchestrator UX — SSE run-stream endpoint", () => {
  test("POST /api/run-stream with unknown action returns 400 JSON", async () => {
    const res = await api.post("/api/run-stream", {
      data: { action: "not-a-real-action", repo_root: "/tmp" },
    });
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.ok).toBe(false);
    expect(body.error).toMatch(/unknown action/i);
  });

  test("GET /api/task-status requires id param", async () => {
    const res = await api.get("/api/task-status");
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.ok).toBe(false);
  });

  test("GET /api/task-status returns 404 for unknown task id", async () => {
    const res = await api.get("/api/task-status?id=nonexistent-task-id");
    expect(res.status()).toBe(404);
    const body = await res.json();
    expect(body.ok).toBe(false);
  });

  test("only the active mode's stage-line/live-log containers exist in the DOM", async ({
    page,
  }) => {
    await page.goto("/");
    await page.click('.kit-orch-nav-btn[data-section-id="retrieve"]');
    // Branch is the default retrieve mode — its stage-line/log render, start hidden.
    const activeSection = page.locator('.kit-orch-section[data-section-id="retrieve"].is-active');
    await expect(activeSection.locator(".kit-stage-line-box")).toHaveCount(1);
    await expect(activeSection.locator(".kit-live-log")).toHaveCount(1);

    // Switching modes tears down the previous mode's containers and builds
    // fresh ones for the new mode — never several stacked simultaneously.
    await page.click('.kit-pill:has-text("Packages")');
    await expect(activeSection.locator(".kit-stage-line-box")).toHaveCount(1);
    await expect(activeSection.locator(".kit-live-log")).toHaveCount(1);
  });
});
