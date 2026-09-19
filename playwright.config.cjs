const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests/e2e",
  fullyParallel: true,
  // The dev server (ui_server_base.py) is a plain single-threaded
  // http.server — concurrent test workers racing it for connections causes
  // intermittent blank-page/timeout flakes. One worker keeps every request
  // sequential; the full suite still runs in well under a minute.
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: "http://127.0.0.1:8091",
    actionTimeout: 10_000,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "python3 scripts/serve-orchestrator-ui.py --port 8091",
    url: "http://127.0.0.1:8091/",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
