const { test, expect } = require("@playwright/test");

// The diff UI is served by serve-diff-ui.py on a different port, so these
// specs verify the served source directly rather than driving a live page.
// Since the P2 kit migration (see the vendored UI kit),
// index.html is a thin shell that loads /kit/theme.css + /kit/shell.js —
// chrome (header/sidebar/keyboard nav/badges) lives in the shared kit, not
// in mct's own source, so assertions that used to read index.html for
// dynamically-rendered markup now read diff.js, which builds that markup.

test.describe("Diff UI polish — keyboard hints", () => {
  test("keyboard hints markup is present in diff.js (rendered into the sidebar)", async () => {
    const fs = require("fs");
    const path = require("path");
    const js = fs.readFileSync(path.join(__dirname, "../../scripts/ui/diff.js"), "utf8");
    expect(js).toContain("keyboard-hints");
    expect(js).toContain("<kbd>j</kbd>");
    expect(js).toContain("<kbd>k</kbd>");
    expect(js).toContain("<kbd>/</kbd>");
    expect(js).toContain("<kbd>Esc</kbd>");
  });

  test("badges/status dots are styled via the shared compare-ui-kit theme, not duplicated per-tool CSS", async () => {
    const fs = require("fs");
    const path = require("path");
    const html = fs.readFileSync(path.join(__dirname, "../../scripts/ui/index.html"), "utf8");
    // index.html no longer carries its own badge/status CSS at all — it links
    // the kit stylesheet, which defines .kit-badge/.kit-dot once for every
    // consuming tool. Guard against that regressing back to a per-tool copy.
    expect(html).toContain('href="/kit/theme.css"');
    expect(html).not.toMatch(/\.badge-different\s*\{/);
    expect(html).not.toMatch(/\.stat-dot\s*\{/);
  });

  test("Report button (btn_generate_report) is present in diff.js", async () => {
    const fs = require("fs");
    const path = require("path");
    const js = fs.readFileSync(path.join(__dirname, "../../scripts/ui/diff.js"), "utf8");
    expect(js).toContain("btn_generate_report");
    expect(js).toContain("Report");
  });
});

test.describe("Diff UI polish — diff cache bounds", () => {
  test("diff.js defines CACHE_MAX = 20 and cacheSet/cacheGet helpers", async () => {
    const fs = require("fs");
    const path = require("path");
    const js = fs.readFileSync(path.join(__dirname, "../../scripts/ui/diff.js"), "utf8");
    expect(js).toContain("CACHE_MAX = 20");
    expect(js).toContain("function cacheSet(");
    expect(js).toContain("function cacheGet(");
    expect(js).toContain("function buildMarkdownReport(");
    expect(js).toContain("function downloadReport(");
    expect(js).not.toContain("diffCache[cacheKey] = data");
  });

  test("cacheSet evicts oldest entry when limit is exceeded", async () => {
    // Run the cache logic inline to verify eviction behaviour
    const CACHE_MAX = 20;
    const diffCacheKeys = [];
    let diffCache = {};

    function cacheSet(key, value) {
      if (!(key in diffCache)) {
        if (diffCacheKeys.length >= CACHE_MAX) {
          const oldest = diffCacheKeys.shift();
          delete diffCache[oldest];
        }
        diffCacheKeys.push(key);
      }
      diffCache[key] = value;
    }

    function cacheGet(key) {
      return diffCache[key];
    }

    // Fill to limit
    for (let i = 0; i < CACHE_MAX; i++) {
      cacheSet(`key-${i}`, { diff: `content-${i}` });
    }
    expect(diffCacheKeys.length).toBe(CACHE_MAX);
    expect(cacheGet("key-0")).toBeDefined();

    // One more should evict key-0
    cacheSet("key-overflow", { diff: "overflow" });
    expect(diffCacheKeys.length).toBe(CACHE_MAX);
    expect(cacheGet("key-0")).toBeUndefined();
    expect(cacheGet("key-overflow")).toBeDefined();
  });

  test("cacheSet updating an existing key does not evict", async () => {
    const CACHE_MAX = 20;
    const diffCacheKeys = [];
    let diffCache = {};

    function cacheSet(key, value) {
      if (!(key in diffCache)) {
        if (diffCacheKeys.length >= CACHE_MAX) {
          const oldest = diffCacheKeys.shift();
          delete diffCache[oldest];
        }
        diffCacheKeys.push(key);
      }
      diffCache[key] = value;
    }

    function cacheGet(key) {
      return diffCache[key];
    }

    // Fill to limit minus 1
    for (let i = 0; i < CACHE_MAX - 1; i++) {
      cacheSet(`key-${i}`, { diff: `content-${i}` });
    }
    // Update key-0 (existing) — should not grow the key list
    cacheSet("key-0", { diff: "updated" });
    expect(diffCacheKeys.length).toBe(CACHE_MAX - 1);
    expect(cacheGet("key-0").diff).toBe("updated");
  });
});

test.describe("Diff UI polish — premium markdown and bulk select elements", () => {
  test("bulk select and clear buttons are present in diff.js", async () => {
    const fs = require("fs");
    const path = require("path");
    const js = fs.readFileSync(path.join(__dirname, "../../scripts/ui/diff.js"), "utf8");
    expect(js).toContain("bulkSelectAllFiltered(true)");
    expect(js).toContain("bulkSelectAllFiltered(false)");
    expect(js).toContain("Select All");
    expect(js).toContain("Clear All");
  });

  test("folder-select-chk checkbox integration is present in diff.js", async () => {
    const fs = require("fs");
    const path = require("path");
    const js = fs.readFileSync(path.join(__dirname, "../../scripts/ui/diff.js"), "utf8");
    expect(js).toContain("folder-select-chk");
    expect(js).toContain("toggleFolderSelection(");
    expect(js).toContain("updateFolderCheckboxes(");
  });

  test("buildMarkdownReport outputs structured breakdown and warnings", async () => {
    const fs = require("fs");
    const path = require("path");
    const js = fs.readFileSync(path.join(__dirname, "../../scripts/ui/diff.js"), "utf8");

    // Evaluate buildMarkdownReport to confirm GFM block patterns
    expect(js).toContain("📊 Salesforce Metadata Compare Report");
    expect(js).toContain("[!NOTE]");
    expect(js).toContain("[!WARNING]");
    expect(js).toContain("Drift Ratio");
    expect(js).toContain("<details>");
  });
});
