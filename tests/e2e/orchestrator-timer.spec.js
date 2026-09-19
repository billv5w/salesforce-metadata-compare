const { test, expect } = require("@playwright/test");

/**
 * Verifies the elapsed clock on the progress line advances during a long SSE
 * streaming operation. Uses a mocked /api/run-stream that delays 9 s before
 * responding — the progress line is created before the fetch starts, so the
 * timer runs for the full duration of the mock delay.
 */
test.describe("Orchestrator progress timer", () => {
  test("stage line shows changing elapsed time during streaming operation (mocked delay)", async ({
    page,
  }) => {
    await page.route("**/api/workspaces", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          workspaces: [
            {
              id: "ws-test-timer-001",
              name: "qaint",
              repo_root: "/fake/repo",
              branch: "main",
              org: "qaint",
              api_version: "66.0",
            },
          ],
        }),
      });
    });

    await page.route("**/api/snapshots**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, data: { snapshots: [] } }),
      });
    });

    await page.route("**/api/run-stream", async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      await new Promise((r) => setTimeout(r, 9000));
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: 'event: done\ndata: {"exit_code":0,"stderr":""}\n\n',
      });
    });

    await page.goto("/");
    await expect(page.locator(".kit-orch-brand-title")).toBeVisible();

    // Open the workspace popover and load the mocked "qaint" workspace.
    await page.locator(".kit-orch-header-actions button", { hasText: "No workspace" }).click();
    await expect(page.locator(".kit-orch-ws-list")).toContainText("qaint");
    await page.locator('.kit-orch-ws-item:has-text("qaint") button:has-text("Load")').click();

    // Retrieve > Org > "From branch manifest" (default submode) — fields
    // pre-fill from the loaded workspace (branch=main, org=qaint).
    await page.click('.kit-orch-nav-btn[data-section-id="retrieve"]');
    await page.click('.kit-orch-section[data-section-id="retrieve"].is-active .kit-pill:has-text("Org")');
    const activeSection = page.locator('.kit-orch-section[data-section-id="retrieve"].is-active');
    await expect(activeSection.locator('input[data-field-id="branch"]')).not.toHaveValue("");
    await activeSection.locator('button:has-text("Retrieve org")').click();

    // The stage line is added before the fetch starts, so it appears immediately.
    await expect(activeSection.locator(".kit-stage-line.running")).toContainText("Retrieving org", {
      timeout: 5000,
    });

    // Sample the elapsed timer via evaluate() — reads DOM synchronously without
    // Playwright's auto-wait, which is correct for polling a live timer element.
    const samples = [];
    for (let i = 0; i < 12; i++) {
      await page.waitForTimeout(800);
      const elapsed = await page.evaluate(() => {
        const el = document.querySelector(
          '.kit-orch-section[data-section-id="retrieve"].is-active .kit-stage-line.running .elapsed'
        );
        return el ? el.textContent.trim() : "";
      });
      samples.push(elapsed);
    }

    const withDecimal = samples.filter((t) => /\d+\.\d+s/.test(t));
    expect(withDecimal.length).toBeGreaterThanOrEqual(5);

    const nonEmpty = samples.filter(Boolean);
    const unique = new Set(nonEmpty);
    expect(unique.size).toBeGreaterThan(3);
  });
});
