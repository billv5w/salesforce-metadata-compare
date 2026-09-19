const { test, expect } = require("@playwright/test");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// Phase 8: the exported HTML report must render fully offline — no CDN, no
// live server, no session token. This spec exports a real report from the
// diff server, then opens it via file:// with every network request aborted
// and asserts readable diff content renders.

const REPO = path.join(__dirname, "..", "..");

function makeFixtures() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mct-report-e2e-"));
  for (const side of ["left", "right"]) {
    fs.mkdirSync(path.join(tmp, side, "classes"), { recursive: true });
  }
  fs.writeFileSync(
    path.join(tmp, "left/classes/Foo.cls"),
    "public class Foo {\n  void m() { debug('L'); }\n}\n"
  );
  fs.writeFileSync(
    path.join(tmp, "right/classes/Foo.cls"),
    "public class Foo {\n  void m() { debug('R'); }\n}\n"
  );
  fs.writeFileSync(path.join(tmp, "left/classes/LeftOnly.cls"), "public class LeftOnly {}\n");
  return tmp;
}

function startDiffServer(left, right) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      "python3",
      [
        path.join(REPO, "scripts/serve-diff-ui.py"),
        "--left",
        left,
        "--right",
        right,
        "--port",
        "0",
        "--no-open",
      ],
      { cwd: REPO }
    );
    let out = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`diff server did not print URL marker. stdout: ${out}`));
    }, 20000);
    child.stdout.on("data", (d) => {
      out += d;
      const m = out.match(/METADATA_COMPARE_DIFF_UI_URL=(\S+)/);
      if (m) {
        clearTimeout(timer);
        resolve({ child, url: m[1] });
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`diff server exited early (${code}). stdout: ${out}`));
    });
  });
}

test.describe("Offline HTML report", () => {
  let server;
  let tmp;

  test.beforeAll(async () => {
    tmp = makeFixtures();
    server = await startDiffServer(path.join(tmp, "left"), path.join(tmp, "right"));
  });

  test.afterAll(async () => {
    if (server) server.child.kill();
    if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
  });

  test("exported report renders with network fully blocked", async ({ page }) => {
    // Bootstrap the session token the same way the UI does: fetch the page
    // HTML and read window.__MCT_TOKEN__.
    const pageResp = await page.request.get(server.url);
    const pageHtml = await pageResp.text();
    const token = pageHtml.match(/window\.__MCT_TOKEN__="([^"]+)"/)[1];

    const exportResp = await page.request.post(`${server.url}/api/export-html`, {
      headers: { "X-MCT-Token": token },
    });
    expect(exportResp.ok()).toBeTruthy();
    const { html, filename } = await exportResp.json();
    expect(html).toContain("<html");
    expect(filename).toContain(".html");
    expect(html).not.toMatch(/<script[^>]*\ssrc=/);
    expect(html).not.toMatch(/<link[^>]*href=/);

    const reportPath = path.join(tmp, filename);
    fs.writeFileSync(reportPath, html);

    // Fresh context, every network request aborted — the report must be
    // completely self-contained.
    const context = await page.context().browser().newContext();
    const offPage = await context.newPage();
    const blocked = [];
    await offPage.route(/https?:\/\//, (route) => {
      blocked.push(route.request().url());
      route.abort();
    });
    const jsErrors = [];
    offPage.on("pageerror", (e) => jsErrors.push(String(e)));

    await offPage.goto(`file://${reportPath}`);
    await expect(offPage.locator(".d2h-wrapper")).toHaveCount(1);
    await expect(offPage.locator(".d2h-code-line").first()).toBeVisible();
    await expect(offPage.getByText("classes/Foo.cls").first()).toBeVisible();
    await expect(offPage.getByText("LeftOnly.cls").first()).toBeVisible();
    expect(jsErrors).toEqual([]);
    expect(blocked).toEqual([]);

    await context.close();
  });
});
