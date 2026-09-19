const { test, expect } = require("@playwright/test");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// Release remediation: retrieval incompleteness (retrieve warnings, skipped
// org types, API-version mismatch) and an active type scope must render as
// visible warnings in the diff UI and in exported reports — an incomplete
// snapshot must never read as a clean comparison.

const REPO = path.join(__dirname, "..", "..");

function makeFixtures() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mct-warnings-e2e-"));
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
  return tmp;
}

function startDiffServer(left, right, extraArgs) {
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
        ...extraArgs,
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

test.describe("Snapshot completeness warnings", () => {
  let server;
  let tmp;

  const LEFT_INFO = {
    id: "snap-left",
    type: "org_retrieve",
    created_at: "2025-01-01-000000",
    manifest_kind: "union",
    org_alias: "dev",
    api_version: "60.0",
  };
  const RIGHT_INFO = {
    id: "snap-right",
    type: "org_retrieve",
    created_at: "2025-01-02-000000",
    manifest_kind: "union",
    org_alias: "uat",
    api_version: "62.0",
    skipped_org_types: ["Report"],
    retrieve_warnings: ["classes/Foo.cls: entity is deleted"],
  };

  test.beforeAll(async () => {
    tmp = makeFixtures();
    server = await startDiffServer(
      path.join(tmp, "left"),
      path.join(tmp, "right"),
      [
        "--left-info",
        JSON.stringify(LEFT_INFO),
        "--right-info",
        JSON.stringify(RIGHT_INFO),
        "--include-type",
        "classes",
      ]
    );
  });

  test.afterAll(async () => {
    if (server) server.child.kill();
    if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
  });

  test("warnings render in the diff UI", async ({ page }) => {
    await page.goto(server.url);
    // API-version mismatch, skipped types, retrieve warning detail, and the
    // active include-type scope are all visible — not just a count.
    await expect(
      page.getByText(/API version mismatch \(left 60\.0, right 62\.0\)/)
    ).toBeVisible();
    await expect(
      page.getByText(/skipped org-only type\(s\).*Report/)
    ).toBeVisible();
    await expect(
      page.getByText(/Right retrieve warning: classes\/Foo\.cls: entity is deleted/)
    ).toBeVisible();
    await expect(
      page.getByText(/Comparison scoped to metadata type\(s\): classes/)
    ).toBeVisible();
  });

  test("warnings reach the exported offline report", async ({ page }) => {
    const pageResp = await page.request.get(server.url);
    const pageHtml = await pageResp.text();
    const token = pageHtml.match(/window\.__MCT_TOKEN__="([^"]+)"/)[1];

    const exportResp = await page.request.post(`${server.url}/api/export-html`, {
      headers: { "X-MCT-Token": token },
    });
    expect(exportResp.ok()).toBeTruthy();
    const { html } = await exportResp.json();
    expect(html).toContain("Snapshot completeness warnings");
    expect(html).toContain("API version mismatch");
    expect(html).toContain("Report");
    expect(html).toContain("entity is deleted");
    expect(html).toContain("Scoped to metadata type(s):");
    // The filtered summary only lists the in-scope file.
    expect(html).toContain("classes/Foo.cls");
  });
});
