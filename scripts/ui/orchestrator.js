// mct orchestrator adapter. Talks to the local server started by `mct start`
// (scripts/serve-orchestrator-ui.py — unchanged by this rewrite). Builds the
// full Orchestrator.mount() config: field schemas, endpoints, and payload
// builders are mct's business; the dashboard chrome (header, nav, mode
// pills, streaming progress, tables, dialogs) comes from the shared kit.
(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // Pure helpers (ported from the pre-kit orchestrator.js unchanged)
  // ---------------------------------------------------------------------
  function relativeTime(rawTs) {
    if (!rawTs) return "";
    const m = rawTs.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
    if (!m) return rawTs;
    const dt = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
    const diffMs = Date.now() - dt.getTime();
    if (isNaN(diffMs)) return rawTs;
    const diffS = Math.floor(diffMs / 1000);
    if (diffS < 60) return "just now";
    const diffM = Math.floor(diffS / 60);
    if (diffM < 60) return `${diffM}m ago`;
    const diffH = Math.floor(diffM / 60);
    if (diffH < 24) return `${diffH}h ago`;
    const diffD = Math.floor(diffH / 24);
    if (diffD === 1) return "yesterday";
    if (diffD < 30) return `${diffD}d ago`;
    const diffW = Math.floor(diffD / 7);
    if (diffW < 8) return `${diffW}w ago`;
    return dt.toLocaleDateString();
  }

  let snapshotsCache = [];
  function friendlySnapLabel(pathOrId) {
    if (!pathOrId) return "";
    const snap = snapshotsCache.find((s) => s.path === pathOrId || s.id === pathOrId);
    if (!snap) return pathOrId.split("/").filter(Boolean).pop() || pathOrId;
    const tag = snapKindTag(snap.type);
    const ref = snap.branch || snap.org_alias || "";
    const ago = relativeTime(snap.created_at);
    return ref ? `${tag} ${ref} · ${ago}` : `${tag} ${ago}`;
  }

  function snapKindTag(type) {
    return type === "branch" ? "[branch]" : type === "org_retrieve" ? "[org]" : type === "installed_packages" ? "[pkg]" : `[${type || "?"}]`;
  }
  function snapKindLabel(type) {
    return type === "branch" ? "branch" : type === "org_retrieve" ? "org_retrieve" : type === "installed_packages" ? "installed_packages" : type || "—";
  }
  function snapKindTone(type) {
    return type === "branch" ? "blue" : type === "org_retrieve" ? "green" : "yellow";
  }
  function snapGroupLabel(type) {
    return type === "branch" ? "Branch Snapshots" : type === "org_retrieve" ? "Org Retrieves" : type === "installed_packages" ? "Installed Packages" : "Package Snapshots";
  }

  function parseTypeList(str) {
    return (str || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function repoRoot(ws) {
    return (ws && ws.repo_root) || "";
  }

  async function loadSnapshots(ws) {
    const rr = repoRoot(ws);
    const data = await Shell.fetchJSON(`/api/snapshots${rr ? `?repo_root=${encodeURIComponent(rr)}` : ""}`);
    snapshotsCache = (data.data && data.data.snapshots) || [];
    return snapshotsCache;
  }

  async function loadHistory(ws) {
    const rr = repoRoot(ws);
    const data = await Shell.fetchJSON(`/api/history${rr ? `?repo_root=${encodeURIComponent(rr)}` : ""}`);
    return (data.data && data.data.comparisons) || [];
  }

  async function launchDiff(left, right, ws, extra) {
    try {
      const data = await Shell.fetchJSON("/api/launch-diff-ui", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ repo_root: repoRoot(ws), left: left.path || left.id || left, right: right.path || right.id || right }, extra || {})),
      });
      window.open(data.url, "mct-diff");
      Shell.toast("Diff viewer opened in new tab.");
    } catch (e) {
      Shell.toast(e.message, { error: true });
    }
  }

  function streamOne(payload, step) {
    let exitCode = null;
    let finalStderr = "";
    let streamErr = null;
    return Shell.streamPost("/api/run-stream", payload, {
      onLine: () => {},
      onDone: (result) => {
        exitCode = result ? result.exit_code : null;
        finalStderr = (result && result.stderr) || "";
      },
      onError: (msg) => {
        streamErr = msg || "Request failed";
      },
    }).then(() => {
      if (streamErr !== null) {
        step.error(streamErr);
        throw new Error(streamErr);
      }
      if (exitCode !== 0) {
        const msg = (finalStderr || "Command failed").split("\n").filter(Boolean).slice(-3).join(" | ");
        step.error(msg || "Command failed");
        throw new Error(msg || "Command failed");
      }
      step.done();
    });
  }

  // ---------------------------------------------------------------------
  // Small custom dialogs the kit doesn't own (file browser, package-diff
  // stdout) — styled with the kit's dialog classes for visual consistency.
  // ---------------------------------------------------------------------
  function openDialog(titleText, bodyEl) {
    const backdrop = document.createElement("div");
    backdrop.className = "kit-dialog-backdrop";
    const dialog = document.createElement("div");
    dialog.className = "kit-dialog is-wide";
    const header = document.createElement("div");
    header.style.cssText = "display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;";
    const h3 = document.createElement("h3");
    h3.style.cssText = "font-size:15px;font-weight:600;";
    h3.textContent = titleText;
    const closeBtn = document.createElement("button");
    closeBtn.className = "kit-btn";
    closeBtn.textContent = "✕";
    closeBtn.onclick = () => backdrop.remove();
    header.append(h3, closeBtn);
    dialog.append(header, bodyEl);
    backdrop.appendChild(dialog);
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) backdrop.remove();
    });
    document.body.appendChild(backdrop);
  }

  async function browseSnapshot(row, ws) {
    const body = document.createElement("div");
    body.style.cssText = "max-height:60vh;overflow-y:auto;font-family:ui-monospace,monospace;font-size:11px;color:var(--text-dim);";
    body.textContent = "Loading…";
    openDialog(`Snapshot Files — ${row.branch || row.org_alias || row.id}`, body);
    try {
      const rr = repoRoot(ws);
      const data = await Shell.fetchJSON(`/api/snapshot-files?repo_root=${encodeURIComponent(rr)}&snapshot_path=${encodeURIComponent(row.path || row.id)}`);
      const files = data.files || [];
      body.innerHTML = "";
      if (!files.length) {
        body.textContent = "No files found.";
        return;
      }
      files.forEach((f) => {
        const line = document.createElement("div");
        line.style.cssText = "padding:3px 0;border-bottom:1px solid var(--border-subtle);";
        line.textContent = f;
        body.appendChild(line);
      });
    } catch (e) {
      body.textContent = e.message;
    }
  }

  function showPackageDiffResult(stdout) {
    const pre = document.createElement("pre");
    pre.style.cssText = "font-size:11px;color:var(--text);background:var(--bg);padding:10px;border-radius:6px;overflow:auto;max-height:400px;white-space:pre-wrap;";
    pre.textContent = stdout || "No output.";
    openDialog("Package comparison", pre);
  }

  // ---------------------------------------------------------------------
  // Mount
  // ---------------------------------------------------------------------
  let orch;
  orch = Orchestrator.mount({
    storageKey: "mct-orchestrator",
    title: "Metadata Compare",
    subtitle: "Metadata Compare",

    workspace: {
      fields: [
        { id: "name", label: "Name", required: true },
        { id: "repo_root", label: "Repo root", required: true },
        { id: "branch", label: "Branch" },
        { id: "org", label: "Org alias" },
        { id: "api_version", label: "API version", default: "66.0" },
      ],
      list: () => Shell.fetchJSON("/api/workspaces").then((d) => d.workspaces),
      save: (payload) =>
        Shell.fetchJSON("/api/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
      delete: (id) => Shell.fetchJSON(`/api/workspaces?id=${encodeURIComponent(id)}`, { method: "DELETE" }),
      onLoad: (ws) => {
        Shell.toast(`Loaded workspace: ${ws.name}`);
        updateGettingStarted();
      },
    },

    sections: [
      {
        id: "retrieve",
        label: "Retrieve",
        modes: [
          {
            id: "branch",
            label: "Branch",
            fields: [
              { id: "branch", label: "Branch", type: "text" },
              { id: "fetch", label: "fetch first", type: "checkbox" },
            ],
            action: {
              kind: "stream",
              endpoint: "/api/run-stream",
              buildPayload: (v, ws) => ({ action: "snapshot-branch", repo_root: repoRoot(ws), branch: v.branch, fetch: v.fetch }),
              label: (v) => `Archiving branch: ${v.branch || "(default)"}`,
              submitLabel: "Snapshot",
              onSuccess: () => Shell.toast("Branch snapshot complete."),
            },
          },
          {
            id: "org",
            label: "Org",
            subModes: [
              {
                id: "from_source",
                label: "From branch manifest",
                fields: [
                  { id: "branch", label: "Branch", type: "text" },
                  { id: "org", label: "Org alias", type: "text" },
                  { id: "api_version", label: "API version", type: "text", default: "66.0" },
                  { id: "wait", label: "Timeout (s)", type: "number", default: 120, min: 1, max: 3600 },
                  { id: "fetch", label: "fetch branch first", type: "checkbox" },
                ],
                action: {
                  kind: "stream",
                  endpoint: "/api/run-stream",
                  buildPayload: (v, ws) => ({
                    action: "snapshot-org-from-source",
                    repo_root: repoRoot(ws),
                    branch: v.branch,
                    org: v.org,
                    api_version: v.api_version,
                    wait_seconds: Math.min(parseInt(v.wait, 10) || 120, 3600),
                    fetch: v.fetch,
                  }),
                  label: (v) => `Retrieving org (${v.org}) from branch (${v.branch})`,
                  submitLabel: "Retrieve org",
                  onSuccess: () => Shell.toast("Org retrieval complete."),
                },
              },
              {
                id: "direct",
                label: "From org directly",
                fields: [
                  { id: "org", label: "Org alias", type: "text" },
                  { id: "wait", label: "Timeout (s)", type: "number", default: 120, min: 1, max: 3600 },
                ],
                action: {
                  kind: "stream",
                  endpoint: "/api/run-stream",
                  buildPayload: (v, ws) => ({
                    action: "snapshot-org-from-org",
                    repo_root: repoRoot(ws),
                    org: v.org,
                    wait_seconds: Math.min(parseInt(v.wait, 10) || 120, 3600),
                  }),
                  label: (v) => `Retrieving from org: ${v.org}`,
                  submitLabel: "Retrieve org",
                  onSuccess: () => Shell.toast("Org snapshot complete."),
                },
              },
              {
                id: "bidirectional",
                label: "Bidirectional (union)",
                fields: [
                  { id: "branch", label: "Branch", type: "text" },
                  { id: "org", label: "Org alias", type: "text" },
                  { id: "api_version", label: "API version", type: "text", default: "66.0" },
                  { id: "wait", label: "Timeout (s)", type: "number", default: 120, min: 1, max: 3600 },
                  { id: "fetch", label: "fetch branch first", type: "checkbox" },
                  { id: "bidi_types", label: "Extra org types (optional, comma-separated)", type: "text", placeholder: "e.g. Report, Dashboard" },
                ],
                action: {
                  kind: "stream",
                  endpoint: "/api/run-stream",
                  buildPayload: (v, ws) => ({
                    action: "snapshot-org-bidirectional",
                    repo_root: repoRoot(ws),
                    branch: v.branch,
                    org: v.org,
                    api_version: v.api_version,
                    wait_seconds: Math.min(parseInt(v.wait, 10) || 120, 3600),
                    fetch: v.fetch,
                    include_org_types: parseTypeList(v.bidi_types),
                  }),
                  label: (v) => `Bidirectional retrieve: ${v.branch} ∪ ${v.org}`,
                  submitLabel: "Retrieve org",
                  onSuccess: () => Shell.toast("Org retrieval complete."),
                },
              },
            ],
          },
          {
            id: "packages",
            label: "Packages",
            fields: [{ id: "org", label: "Org alias", type: "text" }],
            action: {
              kind: "request",
              endpoint: "/api/snapshot-packages",
              buildPayload: (v, ws) => ({ repo_root: repoRoot(ws), org: v.org }),
              submitLabel: "Snapshot",
              onSuccess: () => Shell.toast("Package snapshot complete."),
            },
          },
        ],
        history: {
          list: loadSnapshots,
          columns: [
            { id: "id", label: "Snapshot ID" },
            {
              id: "kind",
              label: "Type",
              render: (row) => {
                const badge = document.createElement("span");
                badge.className = `kit-badge tone-${snapKindTone(row.type)}`;
                badge.textContent = snapKindLabel(row.type);
                return badge;
              },
            },
            { id: "ref", label: "Branch / Org", render: (row) => row.branch || row.org_alias || "—" },
            { id: "created_at", label: "Created", render: (row) => relativeTime(row.created_at) || row.created_at || "—" },
            { id: "size", label: "Size", render: (row) => row.size || "—" },
          ],
          selectable: true,
          onDelete: (ids, ws) =>
            Shell.fetchJSON("/api/delete-snapshots", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ repo_root: repoRoot(ws), ids }),
            }),
          onBrowse: browseSnapshot,
          searchable: true,
        },
      },
      {
        id: "compare",
        label: "Compare",
        modes: [
          {
            id: "quick",
            label: "Quick",
            subModes: [
              {
                id: "branch_org",
                label: "Branch vs Org",
                fields: [
                  { id: "branch", label: "Branch", type: "text" },
                  { id: "org", label: "Org alias", type: "text" },
                ],
                action: {
                  kind: "stream",
                  endpoint: "/api/run-stream",
                  buildPayload: (v, ws) => ({ action: "snapshot-all", repo_root: repoRoot(ws), branch: v.branch, org: v.org }),
                  label: (v) => `Snapshot all — branch: ${v.branch}, org: ${v.org}`,
                  submitLabel: "Quick compare",
                  onSuccess: async (result, ws) => {
                    await loadSnapshots(ws);
                    const snaps = [...snapshotsCache].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
                    const branchSnap = snaps.find((s) => s.type === "branch");
                    const orgSnap = snaps.find((s) => s.type === "org_retrieve");
                    if (!branchSnap || !orgSnap) {
                      orch.showStatus("Snapshots created — select them in Compare to open diff viewer.");
                      return;
                    }
                    await launchDiff(branchSnap, orgSnap, ws);
                  },
                },
              },
              {
                id: "branch_branch",
                label: "Branch vs Branch",
                fields: [
                  { id: "branch", label: "Branch", type: "text" },
                  { id: "branch2", label: "Right branch", type: "text", placeholder: "release/1.2" },
                ],
                action: {
                  kind: "custom",
                  run: async (v, ws, ctx) => {
                    const step1 = ctx.addStage(`Archiving branch: ${v.branch}`);
                    await streamOne({ action: "snapshot-branch", repo_root: repoRoot(ws), branch: v.branch }, step1);
                    const step2 = ctx.addStage(`Archiving branch: ${v.branch2}`);
                    await streamOne({ action: "snapshot-branch", repo_root: repoRoot(ws), branch: v.branch2 }, step2);
                    await loadSnapshots(ws);
                    const snaps = [...snapshotsCache].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
                    const leftSnap = snaps.find((s) => s.type === "branch" && (s.branch || "") === v.branch);
                    const rightSnap = snaps.find((s) => s.type === "branch" && (s.branch || "") === v.branch2);
                    if (!leftSnap || !rightSnap) {
                      orch.showStatus("Snapshots created — select them in Compare to open diff viewer.");
                      return;
                    }
                    await launchDiff(leftSnap, rightSnap, ws);
                  },
                },
              },
            ],
          },
          {
            id: "manual",
            label: "Manual",
            picker: {
              source: loadSnapshots,
              groupBy: (row) => snapGroupLabel(row.type),
              optionLabel: (row) => {
                const ref = row.branch || row.org_alias || "";
                const ago = relativeTime(row.created_at);
                return ref ? `${snapKindTag(row.type)} ${ref} · ${ago}` : `${snapKindTag(row.type)} ${ago}`;
              },
              value: (row) => row.path || row.id,
            },
            extraFields: [
              { id: "include_types", label: "Include types", type: "text", placeholder: "All types" },
              { id: "exclude_types", label: "Exclude types", type: "text", placeholder: "None" },
            ],
            actions: [
              {
                id: "launch",
                label: "Open diff viewer ↗",
                primary: true,
                kind: "request",
                endpoint: "/api/launch-diff-ui",
                visible: (ctx) => !isPackageRow(ctx.leftRow) && !isPackageRow(ctx.rightRow),
                buildPayload: (v, ws) => ({
                  repo_root: repoRoot(ws),
                  left: v.left,
                  right: v.right,
                  api_version: ws && ws.api_version,
                  include_types: parseTypeList(v.include_types),
                  exclude_types: parseTypeList(v.exclude_types),
                }),
                onSuccess: (data) => {
                  window.open(data.url, "mct-diff");
                  Shell.toast("Diff viewer opened in new tab.");
                },
              },
              {
                id: "compare_packages",
                label: "Compare packages",
                kind: "request",
                endpoint: "/api/compare-packages",
                visible: (ctx) => isPackageRow(ctx.leftRow) && isPackageRow(ctx.rightRow),
                buildPayload: (v, ws) => ({ repo_root: repoRoot(ws), left: v.left, right: v.right }),
                onSuccess: (data) => {
                  showPackageDiffResult(data.stdout);
                  Shell.toast("Package comparison complete.");
                },
              },
              {
                id: "copy",
                label: "Copy CLI Command",
                kind: "custom",
                visible: (ctx) => !isPackageRow(ctx.leftRow) && !isPackageRow(ctx.rightRow),
                run: (v, ws) => {
                  let cmd = `mct diff --left ${v.left} --right ${v.right} --fail-on-diff`;
                  parseTypeList(v.include_types).forEach((t) => (cmd += ` --include-type ${t}`));
                  parseTypeList(v.exclude_types).forEach((t) => (cmd += ` --exclude-type ${t}`));
                  if (repoRoot(ws)) cmd += ` --repo-root ${repoRoot(ws)}`;
                  navigator.clipboard
                    .writeText(cmd)
                    .then(() => Shell.toast("CLI command copied to clipboard!"))
                    .catch(() => Shell.toast("Failed to copy command.", { error: true }));
                },
              },
            ],
            preview: {
              source: async (left, right, ws) => {
                if (!left || !right) return null;
                const comparisons = await loadHistory(ws);
                return (
                  comparisons
                    .filter((c) => (c.left_path === left && c.right_path === right) || (c.left_path === right && c.right_path === left))
                    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))[0] || null
                );
              },
              render: (match) =>
                `Last comparison: ${match.different_count || 0} different · ${match.only_left_count || 0} left-only · ` +
                `${match.only_right_count || 0} right-only · ${match.identical_count || 0} identical (${relativeTime(match.created_at)})`,
            },
          },
        ],
      },
      {
        id: "history",
        label: "History",
        table: {
          source: loadHistory,
          columns: [
            { id: "created_at", label: "Created", render: (row) => relativeTime(row.created_at) || row.created_at || "" },
            { id: "left", label: "Left", render: (row) => friendlySnapLabel(row.left_path) },
            { id: "right", label: "Right", render: (row) => friendlySnapLabel(row.right_path) },
            { id: "different_count", label: "Diff", render: (row) => row.different_count || 0 },
            { id: "only_left_count", label: "L-only", render: (row) => row.only_left_count || 0 },
            { id: "only_right_count", label: "R-only", render: (row) => row.only_right_count || 0 },
            { id: "identical_count", label: "Same", render: (row) => row.identical_count || 0 },
          ],
          rowAction: { label: "View diff ↗", run: (row, ws) => launchDiff(row.left_path, row.right_path, ws) },
          onRowClick: (row) => orch.prefillPicker("compare", row.left_path, row.right_path),
          cap: 10,
        },
      },
    ],
  });

  function isPackageRow(row) {
    return !!(row && row.type === "installed_packages");
  }

  // ---------------------------------------------------------------------
  // First-run guidance: shown when no workspace is active, same content as
  // the pre-kit "Getting Started" card, now a plain insert at the top of
  // main rather than a dedicated nav panel.
  // ---------------------------------------------------------------------
  const gettingStarted = document.createElement("div");
  gettingStarted.className = "kit-card";
  gettingStarted.style.margin = "16px 28px 0";
  gettingStarted.innerHTML =
    "<h3>Getting Started</h3>" +
    '<ol style="padding-left:18px;font-size:12px;color:var(--text-dim);display:flex;flex-direction:column;gap:5px;margin:0;">' +
    "<li><strong>Set up a workspace</strong> — click the workspace button in the header, enter a name/repo root/branch/org, then save.</li>" +
    "<li><strong>Take snapshots</strong> — use Retrieve to archive a branch or retrieve an org's metadata.</li>" +
    "<li><strong>Compare snapshots</strong> — use Compare to diff two snapshots and open the diff viewer.</li>" +
    "</ol>";

  function updateGettingStarted() {
    const ws = orch.getWorkspace();
    if (!ws) {
      if (!gettingStarted.parentNode) orch.elements.main.insertBefore(gettingStarted, orch.elements.main.firstChild);
    } else if (gettingStarted.parentNode) {
      gettingStarted.remove();
    }
  }
  updateGettingStarted();
})();
