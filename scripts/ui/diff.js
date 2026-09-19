// mct diff UI client. Talks to the local server started by `env-compare.py ui`.
// Adapter on top of compare-ui-kit's Shell: header/sidebar-tree/toasts/theme/
// keyboard nav come from Shell; this file owns metadata-type badges, the
// folder-tree manifest-selection checkboxes, diff2html rendering, all five
// export types, and provenance/warning composition — mct's own business.
(function () {
  "use strict";

  const CACHE_MAX = 20;
  const PROFILE_TYPES = new Set([
    "profiles",
    "permissionsets",
    "permissionsetgroups",
    "mutingpermissionsets",
  ]);
  const SELECTABLE_STATUSES = new Set(["different", "only_left", "only_right"]);

  const state = {
    summary: null,
    allFiles: [],
    currentPath: null,
    activeStatFilter: null,
    activeTypeFilters: new Set(),
    viewMode: "side-by-side",
    selectedForManifest: new Set(),
    ignoreWs: true,
    ignoreCase: false,
    stripNoGrant: false,
    hideProfileTypes: false,
  };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null) node.setAttribute(k, v);
    });
    (children || []).forEach((c) => c && node.appendChild(c));
    return node;
  }

  function escapeHtml(s) {
    return String(s).replace(
      /[&<>"]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]
    );
  }

  const fmt = (n) => (n || 0).toLocaleString();

  // ---------------------------------------------------------------------
  // Diff cache: LRU, max 20 entries. Keyed on path + normalization flags
  // since a toggle change changes the server's response for the same path.
  // ---------------------------------------------------------------------
  let diffCache = {};
  const diffCacheKeys = [];

  function cacheKeyFor(path, ws, ci) {
    return `${path}\x1f${ws ? 1 : 0}\x1f${ci ? 1 : 0}`;
  }

  function cacheGet(key) {
    return diffCache[key];
  }

  function cacheSet(key, value) {
    if (!(key in diffCache)) {
      diffCacheKeys.push(key);
      if (diffCacheKeys.length > CACHE_MAX) {
        const evict = diffCacheKeys.shift();
        delete diffCache[evict];
      }
    }
    diffCache[key] = value;
  }

  // ---------------------------------------------------------------------
  // Provenance / staleness / scope warnings
  // ---------------------------------------------------------------------
  function parseSnapshotStamp(s) {
    const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(s || "");
    if (!m) return null;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  }

  function describeSide(info, rel) {
    if (!info) return rel;
    let base;
    if (info.type === "branch") base = `branch ${info.branch || rel}`;
    else if (info.type === "org_retrieve") {
      base = `org ${info.org_alias || rel}`;
      if (info.manifest_kind) base += ` (${info.manifest_kind})`;
    } else {
      base = info.type || "snapshot";
    }
    if (info.created_at) base += ` — created ${String(info.created_at).slice(0, 15)}`;
    return base;
  }

  function hasProfileDrift() {
    return state.allFiles.some(
      (f) => PROFILE_TYPES.has(f.type) && f.status !== "identical_normalized"
    );
  }

  function buildWarnings() {
    const s = state.summary;
    const warns = [];
    const li = s.left_info,
      ri = s.right_info;

    if (li && li.type === "org_retrieve" && li.manifest_kind === "repo-manifest") {
      warns.push(
        "Left is an org retrieve scoped to the repo's manifest — components that exist only in the org are invisible to this comparison. Consider a bidirectional (union) snapshot mode."
      );
    }
    if (ri && ri.type === "org_retrieve" && ri.manifest_kind === "repo-manifest") {
      warns.push(
        "Right is an org retrieve scoped to the repo's manifest — components that exist only in the org are invisible to this comparison. Consider a bidirectional (union) snapshot mode."
      );
    }
    if (li && li.type === "org_retrieve" && li.manifest_kind === "org-manifest") {
      warns.push(
        'Left was retrieved from the org\'s own manifest — "only in right" entries may just be untracked types, not real drift.'
      );
    }
    if (ri && ri.type === "org_retrieve" && ri.manifest_kind === "org-manifest") {
      warns.push(
        'Right was retrieved from the org\'s own manifest — "only in left" entries may just be untracked types, not real drift.'
      );
    }

    const lt = parseSnapshotStamp(li && li.created_at);
    const rt = parseSnapshotStamp(ri && ri.created_at);
    if (lt !== null && rt !== null && Math.abs(rt - lt) > 7 * 24 * 3600 * 1000) {
      warns.push(
        "Left and right snapshots are more than 7 days apart — some drift may simply reflect elapsed time."
      );
    }

    const scopeMismatch =
      li && ri && (li.type !== ri.type || li.manifest_kind !== ri.manifest_kind);
    if (scopeMismatch && hasProfileDrift()) {
      warns.push(
        "Profile / permission-set diffs may be scope artifacts of differing retrieve requests (not real drift) — left and right were retrieved with different scope."
      );
    }

    // Server-computed retrieval-completeness warnings: skipped org types,
    // retrieve warnings, API-version mismatch — an incomplete snapshot must
    // never read as a trustworthy clean comparison.
    if (Array.isArray(s.provenance_warnings)) {
      for (const w of s.provenance_warnings) warns.push(w);
    }
    // The recorded per-component retrieve failures themselves — a count
    // alone doesn't tell the reviewer which components may be missing.
    for (const [label, info] of [
      ["Left", li],
      ["Right", ri],
    ]) {
      const rw = (info && info.retrieve_warnings) || [];
      const shown = rw.slice(0, 10);
      for (const w of shown) warns.push(`${label} retrieve warning: ${w}`);
      if (rw.length > shown.length) {
        warns.push(
          `${label} snapshot: …and ${rw.length - shown.length} more retrieve warning(s) (see the exported report).`
        );
      }
    }

    // Effective type scope — a filtered comparison is not a complete one.
    const sc = s.scope || {};
    if (sc.include_types && sc.include_types.length) {
      warns.push(
        `Comparison scoped to metadata type(s): ${sc.include_types.join(", ")} — other types are hidden from this view.`
      );
    }
    if (sc.exclude_types && sc.exclude_types.length) {
      warns.push(`Metadata type(s) excluded from this view: ${sc.exclude_types.join(", ")}.`);
    }
    return warns;
  }

  let profileToggleBtn = null;
  function refreshProfileToggle() {
    if (!hasProfileDrift()) {
      if (profileToggleBtn) {
        profileToggleBtn.remove();
        profileToggleBtn = null;
      }
      return;
    }
    if (!profileToggleBtn) {
      profileToggleBtn = el("button", { class: "kit-btn", style: "margin-bottom: 8px;" });
      shell.elements.warnings.appendChild(profileToggleBtn);
    }
    profileToggleBtn.textContent = state.hideProfileTypes
      ? "Show profile / permission-set rows"
      : "Hide profile / permission-set rows";
    profileToggleBtn.onclick = () => {
      state.hideProfileTypes = !state.hideProfileTypes;
      refreshProfileToggle();
      refreshSidebar();
    };
  }

  // ---------------------------------------------------------------------
  // Detail pane: diff2html target + placeholder/loading/error states.
  // ---------------------------------------------------------------------
  const placeholderEl = el("div", {
    class: "kit-empty-state",
    text: "Select a file from the sidebar to view its diff.",
  });
  const loadingEl = el("div", { class: "kit-loading", text: "Loading diff…" });
  loadingEl.style.display = "none";
  const contentEl = el("div", {});
  contentEl.style.display = "none";
  const errorEl = el("div", { class: "kit-empty-state" });
  errorEl.style.display = "none";
  const detailHost = el("div", { style: "padding: 8px 16px; height: 100%; overflow: auto;" }, [
    placeholderEl,
    loadingEl,
    contentEl,
    errorEl,
  ]);

  function showDiffState(which, message) {
    placeholderEl.style.display = which === "placeholder" ? "block" : "none";
    loadingEl.style.display = which === "loading" ? "block" : "none";
    contentEl.style.display = which === "content" ? "block" : "none";
    errorEl.style.display = which === "error" ? "block" : "none";
    if (which === "placeholder" && message) placeholderEl.textContent = message;
    if (which === "error") errorEl.textContent = message || "Error";
  }

  function renderDiff(diffText) {
    contentEl.innerHTML = "";
    new window.Diff2HtmlUI(contentEl, diffText, {
      drawFileList: false,
      matching: "lines",
      outputFormat: state.viewMode,
      highlight: true,
      renderNothingWhenEmpty: false,
      rawTemplates: {},
    }).draw();
  }

  async function loadDiff(path) {
    state.currentPath = path;
    showDiffState("loading");
    const key = cacheKeyFor(path, state.ignoreWs, state.ignoreCase);
    let data = cacheGet(key);
    if (!data) {
      try {
        const params = new URLSearchParams({
          path,
          ignore_ws: state.ignoreWs ? "1" : "0",
          ignore_case: state.ignoreCase ? "1" : "0",
          _: String(Date.now()),
        });
        data = await Shell.fetchJSON(`/api/diff?${params.toString()}`);
        cacheSet(key, data);
      } catch (err) {
        showDiffState("error", err.message);
        return;
      }
    }
    if (data.error) {
      showDiffState("error", data.error);
      return;
    }
    if (!data.diff) {
      showDiffState("placeholder", "No textual diff to show (identical after normalization).");
      return;
    }
    renderDiff(data.diff);
    showDiffState("content");
  }

  function setViewMode(mode) {
    state.viewMode = mode;
    updateViewToggle();
    if (state.currentPath && contentEl.style.display === "block") {
      const cached = cacheGet(cacheKeyFor(state.currentPath, state.ignoreWs, state.ignoreCase));
      if (cached && cached.diff) renderDiff(cached.diff);
    }
  }

  let viewToggleEls = null;
  function buildViewToggle() {
    const unifiedBtn = el("button", {
      id: "btn-unified",
      class: "kit-btn",
      text: "Unified",
      onclick: () => setViewMode("line-by-line"),
    });
    const splitBtn = el("button", {
      id: "btn-split",
      class: "kit-btn",
      text: "Split",
      onclick: () => setViewMode("side-by-side"),
    });
    viewToggleEls = { unifiedBtn, splitBtn };
    updateViewToggle();
    return el("div", { class: "view-toggle" }, [unifiedBtn, splitBtn]);
  }
  function updateViewToggle() {
    if (!viewToggleEls) return;
    viewToggleEls.unifiedBtn.classList.toggle("kit-btn-primary", state.viewMode === "line-by-line");
    viewToggleEls.splitBtn.classList.toggle("kit-btn-primary", state.viewMode === "side-by-side");
  }

  // ---------------------------------------------------------------------
  // Sidebar: folder-tree with per-file + per-folder manifest checkboxes.
  // ---------------------------------------------------------------------
  function statusTone(status) {
    return (
      {
        different: "yellow",
        only_left: "red",
        only_right: "red",
        identical_normalized: "blue",
        accepted: "purple",
        ignored: "dim",
      }[status] || "dim"
    );
  }
  function statusLabel(status) {
    return (
      {
        different: "changed",
        only_left: "only left",
        only_right: "only right",
        identical_normalized: "identical (normalized)",
        accepted: "accepted",
        ignored: "ignored",
      }[status] || status
    );
  }

  function shortPath(item) {
    const prefix = `${item.group}/`;
    return item.path.startsWith(prefix) ? item.path.slice(prefix.length) : item.path;
  }

  function renderItem(item) {
    const row = el("div", {});
    const left = el("span", { class: "name" });

    if (SELECTABLE_STATUSES.has(item.status)) {
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "file-select-chk";
      cb.checked = state.selectedForManifest.has(item.path);
      cb.style.marginRight = "6px";
      cb.addEventListener("click", (e) => e.stopPropagation());
      cb.addEventListener("change", () => toggleFileSelection(item.path, cb.checked));
      left.appendChild(cb);
    }

    const dot = el("span", {
      class: `kit-dot tone-${statusTone(item.status)}`,
      title: statusLabel(item.status),
    });
    left.appendChild(dot);
    left.appendChild(document.createTextNode(" " + shortPath(item)));
    if (item.normalization_skipped)
      left.appendChild(el("span", { class: "kit-badge tone-dim", text: "raw" }));
    if (item.accepted_stale)
      left.appendChild(el("span", { class: "kit-badge tone-red", text: "changed since accepted" }));
    if (item.status === "ignored" && item.rule)
      left.appendChild(el("span", { class: "kit-badge tone-dim", text: item.rule }));
    row.appendChild(left);

    if (state.summary.baseline_enabled) {
      const badges = el("span", { class: "badges" });
      if (SELECTABLE_STATUSES.has(item.status) || item.accepted_stale) {
        badges.appendChild(controls.acceptButton({ path: item.path }, "Accept"));
      } else if (item.status === "accepted") {
        badges.appendChild(controls.unacceptButton({ path: item.path }, "Undo"));
      }
      row.appendChild(badges);
    }
    return row;
  }

  function renderGroupHeader(groupName, items) {
    const selectable = items.filter((it) => SELECTABLE_STATUSES.has(it.status));
    if (!selectable.length) return undefined;
    const checked = selectable.every((it) => state.selectedForManifest.has(it.path));
    const some = !checked && selectable.some((it) => state.selectedForManifest.has(it.path));
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "folder-select-chk";
    cb.checked = checked;
    cb.indeterminate = some;
    cb.style.marginLeft = "8px";
    cb.addEventListener("click", (e) => e.stopPropagation());
    cb.addEventListener("change", () => toggleFolderSelection(groupName, selectable, !checked));
    return cb;
  }

  function toggleFolderSelection(groupName, selectable, selectAll) {
    selectable.forEach((it) =>
      selectAll ? state.selectedForManifest.add(it.path) : state.selectedForManifest.delete(it.path)
    );
    updateFolderCheckboxes();
  }

  function toggleFileSelection(path, checked) {
    if (checked) state.selectedForManifest.add(path);
    else state.selectedForManifest.delete(path);
    updateFolderCheckboxes();
  }

  function bulkSelectAllFiltered(select) {
    visibleFiles()
      .filter((it) => SELECTABLE_STATUSES.has(it.status))
      .forEach((it) =>
        select ? state.selectedForManifest.add(it.path) : state.selectedForManifest.delete(it.path)
      );
    updateFolderCheckboxes();
  }

  function updateFolderCheckboxes() {
    updateSelectedCountLabel();
    refreshSidebar();
  }

  function updateSelectedCountLabel() {
    if (!selectedManifestBtn) return;
    const n = state.selectedForManifest.size;
    selectedManifestBtn.textContent = `Selected manifest (${n})`;
    selectedManifestBtn.disabled = n === 0;
  }

  // ---------------------------------------------------------------------
  // Filtering: stat card (exclusive) + type pills (multi) + profile hide.
  // ---------------------------------------------------------------------
  function visibleFiles() {
    return state.allFiles
      .filter((it) => {
        if (state.activeStatFilter && it.status !== state.activeStatFilter) return false;
        if (state.activeTypeFilters.size && !state.activeTypeFilters.has(it.type)) return false;
        if (state.hideProfileTypes && PROFILE_TYPES.has(it.type)) return false;
        return true;
      })
      .sort((a, b) => a.group.localeCompare(b.group) || a.path.localeCompare(b.path));
  }

  function refreshSidebar() {
    shell.setSidebarItems(visibleFiles());
  }

  let typePillsEl = null;
  function refreshTypePills() {
    if (!typePillsEl) return;
    typePillsEl.innerHTML = "";
    const counts = {};
    state.allFiles.forEach((f) => (counts[f.type] = (counts[f.type] || 0) + 1));
    Object.keys(counts)
      .sort()
      .forEach((t) => {
        const pill = el("button", {
          class: "kit-btn type-pill" + (state.activeTypeFilters.has(t) ? " kit-btn-primary" : ""),
          text: `${t} (${counts[t]})`,
          "data-type": t,
        });
        pill.addEventListener("click", () => {
          if (state.activeTypeFilters.has(t)) state.activeTypeFilters.delete(t);
          else state.activeTypeFilters.add(t);
          refreshTypePills();
          refreshSidebar();
        });
        typePillsEl.appendChild(pill);
      });
  }

  let fileCountEl = null;
  function refreshFileCount() {
    if (fileCountEl) fileCountEl.textContent = `${fmt(visibleFiles().length)} files`;
  }

  // ---------------------------------------------------------------------
  // Exports
  // ---------------------------------------------------------------------
  function withBusy(btn, busyLabel, fn) {
    return async () => {
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = busyLabel;
      try {
        await fn();
      } catch (err) {
        Shell.toast(err.message, { error: true });
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    };
  }

  function downloadManifestFiles(data, suffix) {
    if (data.warning) Shell.toast(data.warning, { error: true });
    if (data.notes) Shell.toast(data.notes);
    Shell.downloadBlob(data.package_xml || "", `package${suffix}.xml`, "application/xml");
    setTimeout(() => {
      Shell.downloadBlob(
        data.destructive_changes_xml || "",
        `destructiveChanges${suffix}.xml`,
        "application/xml"
      );
    }, 300);
  }

  async function downloadManifests() {
    const data = await Shell.fetchJSON("/api/export-manifest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    downloadManifestFiles(data, "");
  }

  async function downloadSelectedManifest() {
    const data = await Shell.fetchJSON("/api/export-manifest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_paths: Array.from(state.selectedForManifest) }),
    });
    downloadManifestFiles(data, "-selected");
  }

  async function downloadBundle() {
    const res = await fetch("/api/export-bundle", {
      method: "POST",
      headers: Shell.sessionHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
      body: JSON.stringify({ strip_no_grant: state.stripNoGrant }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    Shell.downloadBlob(blob, "metadata-delta-bundle.zip", "application/zip");
  }

  async function exportHtml() {
    const data = await Shell.fetchJSON("/api/export-html", { method: "POST" });
    Shell.downloadBlob(data.html, data.filename, "text/html");
  }

  function buildMarkdownReport() {
    const s = state.summary;
    const total = s.different_count + s.only_left_count + s.only_right_count;
    const driftRatio = s.total_left ? ((total / s.total_left) * 100).toFixed(1) : "0.0";
    const lines = [];
    lines.push("# 📊 Salesforce Metadata Compare Report", "");
    lines.push(`**Left:** \`${s.left}\`  `, `**Right:** \`${s.right}\``, "");
    lines.push("## Summary", "");
    lines.push("| Metric | Count |", "| --- | ---: |");
    lines.push(`| Identical | ${fmt(s.identical_count)} |`);
    lines.push(`| Identical (normalized) | ${fmt(s.identical_normalized_count)} |`);
    lines.push(`| Changed | ${fmt(s.different_count)} |`);
    lines.push(`| Only left | ${fmt(s.only_left_count)} |`);
    lines.push(`| Only right | ${fmt(s.only_right_count)} |`);
    lines.push(`| Accepted | ${fmt(s.accepted_count)} |`);
    lines.push(`| Ignored | ${fmt(s.ignored_count)} |`);
    lines.push(
      "",
      `**Drift Ratio:** ${driftRatio}% of left-side components differ or are missing on the right.`,
      ""
    );

    const sc = s.scope || {};
    if (sc.include_types && sc.include_types.length) {
      lines.push(
        `> [!NOTE]`,
        `> Comparison scoped to metadata type(s): **${sc.include_types.join(", ")}** — other types are not shown in this report.`,
        ""
      );
    }
    if (sc.exclude_types && sc.exclude_types.length) {
      lines.push(
        `> [!NOTE]`,
        `> Metadata type(s) excluded from this comparison: **${sc.exclude_types.join(", ")}**.`,
        ""
      );
    }

    const mdWarnings = buildWarnings();
    if (mdWarnings.length) {
      lines.push("## Snapshot completeness warnings", "");
      mdWarnings.forEach((w) => lines.push(`- ⚠️ ${w}`));
      lines.push("");
    }

    if (s.different_count === 0 && s.only_left_count === 0 && s.only_right_count === 0) {
      lines.push("> [!NOTE]", "> No drift detected between left and right.", "");
    } else {
      lines.push(
        "> [!WARNING]",
        "> Drift detected — review the sections below before deploying.",
        ""
      );
    }

    if (Object.keys(s.type_counts).length) {
      lines.push("## Type breakdown", "");
      lines.push("| Type | Only left | Only right | Changed |", "| --- | ---: | ---: | ---: |");
      Object.entries(s.type_counts).forEach(([t, c]) => {
        lines.push(`| ${t} | ${fmt(c.only_left)} | ${fmt(c.only_right)} | ${fmt(c.different)} |`);
      });
      lines.push("");
    }

    const section = (title, items) => {
      if (!items.length) return;
      lines.push(`<details><summary>${title} (${items.length})</summary>`, "");
      lines.push("| Path |", "| --- |");
      items.forEach((i) =>
        lines.push(`| \`${i.path}\`${i.binary ? " — binary content changed" : ""} |`)
      );
      lines.push("", "</details>", "");
    };
    section("Changed", s.differ);
    section("Only left", s.only_left);
    section("Only right", s.only_right);
    section("Accepted", s.accepted);
    section("Ignored", s.ignored);

    return lines.join("\n");
  }

  function downloadReport() {
    const md = buildMarkdownReport();
    Shell.downloadBlob(
      md,
      `metadata-compare-${state.summary.left}-vs-${state.summary.right}.md`,
      "text/markdown"
    );
  }

  // ---------------------------------------------------------------------
  // Accept / unaccept
  // ---------------------------------------------------------------------
  const controls = Shell.acceptControls({
    acceptUrl: "/api/accept-diff",
    unacceptUrl: "/api/unaccept-diff",
    onDone: (data, err, meta) => {
      if (err) return;
      if (!data.ok) {
        Shell.toast(data.error || "Failed", { error: true });
        return;
      }
      Shell.toast(
        meta.action === "accept" ? `Accepted ${meta.body.path}` : `Un-accepted ${meta.body.path}`
      );
      init();
    },
  });

  // ---------------------------------------------------------------------
  // Init / mount
  // ---------------------------------------------------------------------
  let shell = null;
  let selectedManifestBtn = null;

  async function init() {
    try {
      state.summary = await Shell.fetchJSON(`/api/summary?_=${Date.now()}`);
    } catch (err) {
      const loading = document.getElementById("loading");
      if (loading) loading.textContent = `Failed to load comparison: ${err.message}`;
      return;
    }

    state.allFiles = [
      ...state.summary.only_left,
      ...state.summary.only_right,
      ...state.summary.differ,
      ...state.summary.identical_normalized,
      ...state.summary.accepted,
      ...state.summary.ignored,
    ].map((f) => Object.assign(f, { id: f.path, group: f.type }));

    const li = state.summary.left_info,
      ri = state.summary.right_info;

    if (!shell) {
      const exportManifestsBtn = el("button", {
        class: "kit-btn",
        text: "Manifests",
        title: "package.xml + destructiveChanges.xml for all changed files",
      });
      const exportBundleBtn = el("button", {
        class: "kit-btn",
        text: "Delta bundle",
        title: "Zip of manifests + deployable source files",
      });
      selectedManifestBtn = el("button", {
        id: "btn_selected_manifest",
        class: "kit-btn",
        text: "Selected manifest (0)",
        disabled: "",
      });
      const exportHtmlBtn = el("button", {
        class: "kit-btn",
        text: "Export HTML",
        title: "Standalone offline diff report",
      });
      const reportBtn = el("button", {
        id: "btn_generate_report",
        class: "kit-btn",
        text: "Report",
        title: "Markdown drift report",
      });

      exportManifestsBtn.addEventListener(
        "click",
        withBusy(exportManifestsBtn, "Generating...", downloadManifests)
      );
      exportBundleBtn.addEventListener(
        "click",
        withBusy(exportBundleBtn, "Generating...", downloadBundle)
      );
      selectedManifestBtn.addEventListener(
        "click",
        withBusy(selectedManifestBtn, "Generating...", async () => {
          if (!state.selectedForManifest.size) {
            Shell.toast("Select at least one file first.", { error: true });
            return;
          }
          await downloadSelectedManifest();
        })
      );
      exportHtmlBtn.addEventListener("click", withBusy(exportHtmlBtn, "Generating...", exportHtml));
      reportBtn.addEventListener("click", () => downloadReport());

      shell = Shell.mount({
        storageKey: "mct-diff",
        title: "mct — metadata diff",
        toggles: [
          {
            id: "ignore_ws",
            label: "Ignore whitespace",
            default: true,
            onChange: (v) => {
              state.ignoreWs = v;
              if (state.currentPath) loadDiff(state.currentPath);
            },
          },
          {
            id: "ignore_case",
            label: "Ignore case",
            default: false,
            onChange: (v) => {
              state.ignoreCase = v;
              if (state.currentPath) loadDiff(state.currentPath);
            },
          },
          {
            id: "strip_nogrant",
            label: "Strip no-grant perms",
            default: false,
            onChange: (v) => {
              state.stripNoGrant = v;
            },
          },
        ],
        provenance: {
          leftHtml: `Left: ${escapeHtml(describeSide(li, state.summary.left))}`,
          rightHtml: `Right: ${escapeHtml(describeSide(ri, state.summary.right))}`,
        },
        warnings: buildWarnings(),
        stats: [
          {
            id: "different",
            label: "Changed",
            count: state.summary.different_count,
            tone: "yellow",
            filter: true,
          },
          {
            id: "only_left",
            label: "Only left",
            count: state.summary.only_left_count,
            tone: "red",
            filter: true,
          },
          {
            id: "only_right",
            label: "Only right",
            count: state.summary.only_right_count,
            tone: "red",
            filter: true,
          },
          {
            id: "identical",
            label: "Identical",
            count: state.summary.identical_count,
            tone: "green",
            filter: true,
          },
          {
            id: "identical_normalized",
            label: "Identical (normalized)",
            count: state.summary.identical_normalized_count,
            tone: "blue",
            filter: true,
          },
          {
            id: "accepted",
            label: "Accepted",
            count: state.summary.accepted_count,
            tone: "purple",
            filter: true,
          },
          {
            id: "ignored",
            label: "Ignored",
            count: state.summary.ignored_count,
            tone: "dim",
            filter: true,
          },
        ],
        onStatClick: (id) => {
          if (id === "identical") {
            state.activeStatFilter = null;
            refreshSidebar();
            showDiffState(
              "placeholder",
              "Identical files aren't listed individually — see the count above."
            );
            return;
          }
          state.activeStatFilter = id;
          refreshSidebar();
        },
        sidebar: {
          groups: true,
          renderItem,
          renderGroupHeader,
          onSelect: (item) => loadDiff(item.path),
        },
        detail: detailHost,
      });

      // Real header action buttons (Shell's `actions` config doesn't support
      // the "Selected manifest (N)" dynamic-label / disabled-until-selection
      // pattern, so these are appended directly rather than routed through it).
      const headerActions = shell.elements.header.querySelector(".kit-header-actions");
      headerActions.insertBefore(buildViewToggle(), headerActions.firstChild);
      [exportManifestsBtn, exportBundleBtn, selectedManifestBtn, exportHtmlBtn, reportBtn].forEach(
        (b) => headerActions.appendChild(b)
      );

      // Sidebar extras: type-pill filter row, folder bulk actions, file
      // count, keyboard hints — inserted around Shell's search+list.
      typePillsEl = el("div", { id: "type-filters" });
      const folderActions = el("div", { class: "folder-actions" }, [
        el("button", {
          class: "kit-btn",
          text: "Expand All",
          onclick: () => shell.setGroupsCollapsed(false),
        }),
        el("button", {
          class: "kit-btn",
          text: "Collapse All",
          onclick: () => shell.setGroupsCollapsed(true),
        }),
        el("button", {
          class: "kit-btn",
          text: "Select All",
          onclick: () => bulkSelectAllFiltered(true),
        }),
        el("button", {
          class: "kit-btn",
          text: "Clear All",
          onclick: () => bulkSelectAllFiltered(false),
        }),
      ]);
      const listEl = shell.elements.sidebar.querySelector(".kit-sidebar-list");
      shell.elements.sidebar.insertBefore(typePillsEl, listEl);
      shell.elements.sidebar.insertBefore(folderActions, listEl);
      fileCountEl = el("div", { class: "kit-sidebar-group-title" });
      shell.elements.sidebar.appendChild(fileCountEl);
      shell.elements.sidebar.insertAdjacentHTML(
        "beforeend",
        '<div class="keyboard-hints" style="padding:6px 8px;font-size:11px;color:var(--text-faint);"><kbd>j</kbd><kbd>k</kbd> navigate · <kbd>/</kbd> search · <kbd>Esc</kbd> clear</div>'
      );
    } else {
      shell.setProvenance({
        leftHtml: `Left: ${escapeHtml(describeSide(li, state.summary.left))}`,
        rightHtml: `Right: ${escapeHtml(describeSide(ri, state.summary.right))}`,
      });
      shell.setWarnings(buildWarnings());
      shell.setStats([
        {
          id: "different",
          label: "Changed",
          count: state.summary.different_count,
          tone: "yellow",
          filter: true,
        },
        {
          id: "only_left",
          label: "Only left",
          count: state.summary.only_left_count,
          tone: "red",
          filter: true,
        },
        {
          id: "only_right",
          label: "Only right",
          count: state.summary.only_right_count,
          tone: "red",
          filter: true,
        },
        {
          id: "identical",
          label: "Identical",
          count: state.summary.identical_count,
          tone: "green",
          filter: true,
        },
        {
          id: "identical_normalized",
          label: "Identical (normalized)",
          count: state.summary.identical_normalized_count,
          tone: "blue",
          filter: true,
        },
        {
          id: "accepted",
          label: "Accepted",
          count: state.summary.accepted_count,
          tone: "purple",
          filter: true,
        },
        {
          id: "ignored",
          label: "Ignored",
          count: state.summary.ignored_count,
          tone: "dim",
          filter: true,
        },
      ]);
    }

    // Prune manifest selections for paths that no longer qualify (e.g. accepted).
    const selectableNow = new Set(
      state.allFiles.filter((f) => SELECTABLE_STATUSES.has(f.status)).map((f) => f.path)
    );
    Array.from(state.selectedForManifest).forEach((p) => {
      if (!selectableNow.has(p)) state.selectedForManifest.delete(p);
    });

    refreshTypePills();
    refreshProfileToggle();
    refreshSidebar();
    refreshFileCount();
    updateSelectedCountLabel();

    const stillThere =
      state.currentPath && state.allFiles.some((f) => f.path === state.currentPath);
    if (stillThere) {
      shell.selectSidebarItem(state.currentPath);
    } else {
      const firstDrifted = state.allFiles.find((f) => SELECTABLE_STATUSES.has(f.status));
      if (firstDrifted) shell.selectSidebarItem(firstDrifted.path);
      else showDiffState("placeholder");
    }
  }

  document.addEventListener("DOMContentLoaded", init);
  window.addEventListener("pageshow", (e) => {
    if (e.persisted) init();
  });
})();
