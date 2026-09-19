// compare-ui-kit orchestrator: a dashboard shell for tools that manage
// workspace context + long-running retrieve/compare actions + history —
// a different shape than shell.js's Shell.mount() (which assumes a
// searchable list of comparable entities with a detail pane). This module
// is deliberately separate from shell.js and depends on window.Shell being
// loaded first; it reuses Shell's utility primitives (fetchJSON, streamPost,
// confirmDialog, prefs, downloadBlob) rather than reimplementing them.
//
// Vanilla ES2020, one global `Orchestrator`, no build step. Nothing in this
// file names a tool-specific concept ("branch", "org", "packages", ...) —
// those only ever appear as label/id values the consuming tool supplies.
(function (global) {
  "use strict";

  const Shell = global.Shell;
  const { el, escapeHtml, debounce } = Shell._internal;

  // -----------------------------------------------------------------------
  // Field rendering
  // -----------------------------------------------------------------------
  function buildField(field, initialValue) {
    const value = initialValue !== undefined ? initialValue : field.default;

    if (field.type === "checkbox") {
      const wrap = el("label", { class: "kit-check" });
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.fieldId = field.id;
      input.checked = !!value;
      wrap.appendChild(input);
      wrap.appendChild(document.createTextNode(" " + field.label));
      return wrap;
    }

    const wrap = el("div", { class: "kit-field" });
    wrap.appendChild(el("label", { text: field.label }));
    let input;
    if (field.type === "select") {
      input = document.createElement("select");
      (field.options || []).forEach((o) => {
        const opt = document.createElement("option");
        opt.value = o.value;
        opt.textContent = o.label;
        input.appendChild(opt);
      });
    } else {
      input = document.createElement("input");
      input.type = field.type === "number" ? "number" : "text";
      if (field.placeholder) input.placeholder = field.placeholder;
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
    }
    input.dataset.fieldId = field.id;
    if (value !== undefined && value !== null) input.value = value;
    wrap.appendChild(input);
    return wrap;
  }

  function buildFieldGroup(fields, initialValues) {
    const wrap = el("div", { class: "kit-orch-field-group" });
    (fields || []).forEach((f) => wrap.appendChild(buildField(f, (initialValues || {})[f.id])));
    return wrap;
  }

  function collectValues(container) {
    const values = {};
    container.querySelectorAll("[data-field-id]").forEach((input) => {
      const id = input.dataset.fieldId;
      values[id] = input.type === "checkbox" ? input.checked : input.value;
    });
    return values;
  }

  // -----------------------------------------------------------------------
  // Pills (mode switcher)
  // -----------------------------------------------------------------------
  function renderPills(items, activeId, onSelect) {
    const wrap = el("div", { class: "kit-pills" });
    items.forEach((item) => {
      wrap.appendChild(
        el("button", {
          class: "kit-pill" + (item.id === activeId ? " is-active" : ""),
          text: item.label,
          onclick: () => onSelect(item.id),
        })
      );
    });
    return wrap;
  }

  // -----------------------------------------------------------------------
  // Streaming / request actions — stage-line + live-log, generic over
  // Shell.streamPost (kind: "stream") or a plain POST (kind: "request").
  // -----------------------------------------------------------------------
  function addStageLine(box, label) {
    box.classList.remove("hidden");
    const line = el("div", { class: "kit-stage-line running" });
    line.innerHTML = '<span class="icon">⟳</span><span class="label"></span><span class="elapsed"></span>';
    line.querySelector(".label").textContent = label;
    box.appendChild(line);

    const start = Date.now();
    const timer = setInterval(() => {
      line.querySelector(".elapsed").textContent = ((Date.now() - start) / 1000).toFixed(1) + "s";
    }, 250);

    return {
      done() {
        clearInterval(timer);
        line.classList.remove("running");
        line.classList.add("done");
        line.querySelector(".icon").textContent = "✓";
        line.querySelector(".elapsed").textContent = ((Date.now() - start) / 1000).toFixed(1) + "s";
      },
      error(msg) {
        clearInterval(timer);
        line.classList.remove("running");
        line.classList.add("error");
        line.querySelector(".icon").textContent = "✗";
        line.querySelector(".label").textContent += " — " + msg;
        line.querySelector(".elapsed").textContent = ((Date.now() - start) / 1000).toFixed(1) + "s";
      },
    };
  }

  async function runAction(action, values, ws, progress, onDone) {
    const { box, log } = progress;
    box.innerHTML = "";

    if (action.kind === "custom") {
      // Full manual control for actions that don't fit "one stream call" or
      // "one request call" — e.g. two sequential streamed subprocess calls.
      // The adapter drives its own stage-line(s) via addStage().
      try {
        const result = await action.run(values, ws, { addStage: (label) => addStageLine(box, label), log });
        if (onDone) onDone(result, null);
      } catch (err) {
        if (onDone) onDone(null, err);
      }
      return;
    }

    const label = typeof action.label === "function" ? action.label(values) : action.label || action.submitLabel || "Running…";
    const step = addStageLine(box, label);

    if (action.kind === "stream") {
      if (log) {
        log.innerHTML = "";
        log.classList.remove("hidden");
      }
      let exitCode = null;
      let finalStderr = "";
      let streamError = null;
      await Shell.streamPost(action.endpoint, action.buildPayload(values, ws), {
        onLine: (line) => {
          if (!log) return;
          const lineEl = el("span", { class: "log-line", text: line });
          log.appendChild(lineEl);
          log.scrollTop = log.scrollHeight;
        },
        onDone: (result) => {
          exitCode = result ? result.exit_code : null;
          finalStderr = (result && result.stderr) || "";
        },
        onError: (msg) => {
          streamError = msg || "Request failed";
        },
      });
      if (streamError !== null) {
        step.error(streamError);
        if (onDone) onDone(null, new Error(streamError));
        return;
      }
      if (exitCode !== 0) {
        const msg = (finalStderr || "Command failed").split("\n").filter(Boolean).slice(-3).join(" | ");
        step.error(msg || "Command failed");
        if (onDone) onDone(null, new Error(msg || "Command failed"));
        return;
      }
      step.done();
      if (onDone) onDone({ exitCode, stderr: finalStderr }, null);
    } else {
      try {
        const data = await Shell.fetchJSON(action.endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(action.buildPayload(values, ws)),
        });
        step.done();
        if (onDone) onDone(data, null);
      } catch (err) {
        step.error(err.message);
        if (onDone) onDone(null, err);
      }
    }
  }

  // -----------------------------------------------------------------------
  // Generic table (snapshot history, comparison history)
  // -----------------------------------------------------------------------
  function renderTable(columns, rows, opts) {
    opts = opts || {};
    const wrap = el("div", { class: "kit-table-wrap" });
    if (!rows.length) {
      wrap.appendChild(el("div", { class: "kit-empty-state", text: opts.emptyText || "No rows." }));
      return wrap;
    }
    const table = el("table", { class: "kit-table" });
    const thead = document.createElement("thead");
    const trh = document.createElement("tr");
    let selectAllCb = null;
    if (opts.selectable) {
      const th = document.createElement("th");
      selectAllCb = document.createElement("input");
      selectAllCb.type = "checkbox";
      selectAllCb.className = "kit-orch-row-select";
      th.appendChild(selectAllCb);
      trh.appendChild(th);
    }
    columns.forEach((c) => trh.appendChild(el("th", { text: c.label })));
    if (opts.rowAction) trh.appendChild(document.createElement("th"));
    thead.appendChild(trh);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    const rowCheckboxes = [];
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      if (opts.selectable) {
        const td = document.createElement("td");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "kit-orch-row-select";
        cb.addEventListener("click", (e) => e.stopPropagation());
        cb.addEventListener("change", () => opts.onSelectionChange && opts.onSelectionChange(selectedRows()));
        rowCheckboxes.push({ cb, row });
        td.appendChild(cb);
        tr.appendChild(td);
      }
      columns.forEach((c) => {
        const td = document.createElement("td");
        const rendered = c.render ? c.render(row) : row[c.id];
        if (rendered instanceof Node) td.appendChild(rendered);
        else td.textContent = rendered === undefined || rendered === null ? "" : String(rendered);
        tr.appendChild(td);
      });
      if (opts.rowAction) {
        const td = document.createElement("td");
        td.appendChild(
          el("button", {
            class: "kit-btn",
            text: opts.rowAction.label,
            onclick: (e) => {
              e.stopPropagation();
              opts.rowAction.run(row);
            },
          })
        );
        tr.appendChild(td);
      }
      if (opts.onRowClick) {
        tr.style.cursor = "pointer";
        tr.addEventListener("click", () => opts.onRowClick(row));
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    function selectedRows() {
      return rowCheckboxes.filter((r) => r.cb.checked).map((r) => r.row);
    }
    if (selectAllCb) {
      selectAllCb.addEventListener("change", () => {
        rowCheckboxes.forEach((r) => (r.cb.checked = selectAllCb.checked));
        opts.onSelectionChange && opts.onSelectionChange(selectedRows());
      });
    }
    return wrap;
  }

  // -----------------------------------------------------------------------
  // mount()
  // -----------------------------------------------------------------------
  function mount(config) {
    document.body.innerHTML = "";
    const prefs = Shell.prefs.create(config.storageKey);
    let currentWorkspace = null;

    // ---- theme (same logic as Shell.mount's built-in toggle) ------------
    function applyTheme(theme) {
      document.documentElement.setAttribute("data-theme", theme);
    }
    const savedTheme = prefs.get("theme", null);
    if (savedTheme) applyTheme(savedTheme);

    // ---- header -----------------------------------------------------------
    const brand = el("div", { class: "kit-orch-brand" }, [
      el("div", { class: "kit-orch-brand-title", text: config.title || "" }),
      config.subtitle ? el("div", { class: "kit-orch-brand-sub", text: config.subtitle }) : null,
    ]);

    const wsBtn = el("button", { class: "kit-btn", text: "No workspace" });
    const themeBtn = el("button", {
      class: "kit-btn",
      text: "Theme",
      onclick: () => {
        const current =
          document.documentElement.getAttribute("data-theme") ||
          (global.matchMedia && global.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
        const next = current === "light" ? "dark" : "light";
        applyTheme(next);
        prefs.set("theme", next);
      },
    });

    const headerActions = el("div", { class: "kit-orch-header-actions" });
    if (config.workspace) headerActions.appendChild(wsBtn);
    headerActions.appendChild(themeBtn);

    const header = el("header", { class: "kit-orch-header" }, [brand, el("div", { class: "kit-orch-header-spacer" }), headerActions]);

    // ---- status bar (persistent, dismissible — not Shell.toast; long
    // stderr blocks don't fit a corner toast that auto-dismisses) ---------
    const statusText = el("span", {});
    const statusDismiss = el("button", { class: "kit-orch-status-dismiss", text: "✕", title: "Dismiss" });
    const statusBar = el("div", { class: "kit-orch-status hidden" }, [statusText, statusDismiss]);
    let statusTimer = null;
    function showStatus(msg, opts) {
      const isError = !!(opts && opts.error);
      statusText.textContent = msg;
      statusBar.className = "kit-orch-status" + (isError ? " is-error" : "");
      clearTimeout(statusTimer);
      if (!isError) statusTimer = setTimeout(() => statusBar.classList.add("hidden"), 5000);
    }
    statusDismiss.addEventListener("click", () => {
      clearTimeout(statusTimer);
      statusBar.classList.add("hidden");
    });

    // ---- nav + section bodies ---------------------------------------------
    const navEl = el("nav", { class: "kit-orch-nav" });
    const mainEl = el("main", { class: "kit-orch-main" });
    const sectionEls = {};
    const sectionState = {};
    let activeSectionId = prefs.get("activeSection", (config.sections[0] || {}).id);

    function selectSection(id) {
      activeSectionId = id;
      prefs.set("activeSection", id);
      Object.entries(sectionEls).forEach(([sid, secEl]) => secEl.classList.toggle("is-active", sid === id));
      navEl.querySelectorAll(".kit-orch-nav-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.sectionId === id));
      const sec = config.sections.find((s) => s.id === id);
      if (sec) refreshSection(sec, { refreshBody: true });
    }

    config.sections.forEach((sec) => {
      navEl.appendChild(
        el("button", {
          class: "kit-orch-nav-btn",
          text: sec.label,
          "data-section-id": sec.id,
          onclick: () => selectSection(sec.id),
        })
      );
      const secEl = el("div", { class: "kit-orch-section", "data-section-id": sec.id });
      sectionEls[sec.id] = secEl;
      sectionState[sec.id] = {};
      mainEl.appendChild(secEl);
      buildSection(sec, secEl, sectionState[sec.id]);
    });
    mainEl.appendChild(el("div", { class: "kit-footer", text: Shell.VERSION }));

    // ---- section builders --------------------------------------------------
    function buildHistoryTable(historyCfg, rows) {
      let selected = [];
      const deleteBtn = historyCfg.onDelete
        ? el("button", {
            class: "kit-btn kit-btn-danger hidden",
            text: "Delete selected",
            onclick: async () => {
              if (!selected.length) return;
              const ok = await Shell.confirmDialog(`Delete ${selected.length} item(s)?`, { confirmLabel: "Delete" });
              if (!ok) return;
              await historyCfg.onDelete(selected.map((r) => r.id), currentWorkspace);
              activeRefresh();
            },
          })
        : null;
      const wrap = el("div", {});
      if (deleteBtn) wrap.appendChild(deleteBtn);
      wrap.appendChild(
        renderTable(historyCfg.columns, rows, {
          selectable: historyCfg.selectable,
          onSelectionChange: (rows_) => {
            selected = rows_;
            if (deleteBtn) deleteBtn.classList.toggle("hidden", rows_.length === 0);
          },
          rowAction: historyCfg.onBrowse ? { label: "Browse", run: (row) => historyCfg.onBrowse(row, currentWorkspace) } : null,
        })
      );
      return wrap;
    }

    function buildTableSection(sec, host, state) {
      const tableHost = el("div", {});
      host.appendChild(tableHost);
      state.refreshTable = async () => {
        const rows = (await sec.table.source(currentWorkspace)) || [];
        const capped = sec.table.cap ? rows.slice(0, sec.table.cap) : rows;
        tableHost.innerHTML = "";
        tableHost.appendChild(
          renderTable(sec.table.columns, capped, {
            rowAction: sec.table.rowAction ? { label: sec.table.rowAction.label, run: (row) => sec.table.rowAction.run(row, currentWorkspace) } : null,
            onRowClick: sec.table.onRowClick,
          })
        );
        if (sec.table.cap && rows.length > sec.table.cap) {
          tableHost.appendChild(el("p", { class: "kit-orch-table-note", text: `Showing the ${sec.table.cap} most recent.` }));
        }
      };
    }

    // Compare section's "manual" mode needs a picker + extraFields + actions
    // + preview, which doesn't fit the generic mode-form path above (a mode
    // form is single-action; manual compare has a pair-picker plus multiple
    // possible actions). Handle it as a distinct mode shape: a mode with
    // `picker` instead of `fields`+`action`.
    function buildSection(sec, host, state) {
      if (sec.modes) {
        state.activeModeId = state.activeModeId || sec.modes[0].id;
        const pillsHost = el("div", {});
        const bodyHost = el("div", { class: "kit-orch-mode-form" });
        host.appendChild(pillsHost);
        host.appendChild(bodyHost);
        if (sec.history) {
          state.historyHost = el("div", { class: "kit-orch-section-history" });
          host.appendChild(state.historyHost);
        }

        function renderPillsRow() {
          pillsHost.innerHTML = "";
          if (sec.modes.length > 1) {
            pillsHost.appendChild(
              renderPills(sec.modes, state.activeModeId, (id) => {
                state.activeModeId = id;
                state.activeSubModeId = null;
                renderPillsRow();
                renderBody();
              })
            );
          }
        }

        function renderBody() {
          bodyHost.innerHTML = "";
          const mode = sec.modes.find((m) => m.id === state.activeModeId);
          if (!mode) return;
          if (mode.picker) renderPickerMode(mode, bodyHost, state);
          else renderFormMode(sec, mode, bodyHost, state);
        }

        function renderFormMode(sec_, mode, formHost, state_) {
          let fields = mode.fields;
          let action = mode.action;
          if (mode.subModes && mode.subModes.length) {
            state_.activeSubModeId = state_.activeSubModeId || mode.subModes[0].id;
            formHost.appendChild(
              renderPills(mode.subModes, state_.activeSubModeId, (id) => {
                state_.activeSubModeId = id;
                renderBody();
              })
            );
            const subMode = mode.subModes.find((m) => m.id === state_.activeSubModeId);
            fields = subMode.fields;
            action = subMode.action;
          }
          const fieldGroup = buildFieldGroup(fields, currentWorkspace || {});
          formHost.appendChild(fieldGroup);
          const box = el("div", { class: "kit-stage-line-box hidden" });
          const log = el("div", { class: "kit-live-log hidden" });
          formHost.appendChild(
            el("button", {
              class: "kit-btn kit-btn-primary",
              text: action.submitLabel || "Run",
              onclick: () => {
                const values = collectValues(fieldGroup);
                runAction(action, values, currentWorkspace, { box, log }, (result, err) => {
                  if (err) return;
                  refreshSection(sec_);
                  if (action.onSuccess) action.onSuccess(result, currentWorkspace);
                });
              },
            })
          );
          formHost.appendChild(box);
          formHost.appendChild(log);
        }

        async function renderPickerMode(mode, pickerHost, state_) {
          const leftSel = document.createElement("select");
          const rightSel = document.createElement("select");
          const swapBtn = el("button", {
            class: "kit-btn",
            text: "⇄",
            title: "Swap",
            onclick: () => {
              const tmp = leftSel.value;
              leftSel.value = rightSel.value;
              rightSel.value = tmp;
              onPairChange();
            },
          });
          const pickerRow = el("div", { class: "kit-orch-picker-row" }, [
            el("div", { class: "kit-field" }, [el("label", { text: "Left" }), leftSel]),
            swapBtn,
            el("div", { class: "kit-field" }, [el("label", { text: "Right" }), rightSel]),
          ]);
          pickerHost.appendChild(pickerRow);

          const previewHost = el("div", { class: "kit-orch-preview hidden" });
          if (mode.preview) pickerHost.appendChild(previewHost);

          const extraGroup = mode.extraFields ? buildFieldGroup(mode.extraFields, currentWorkspace || {}) : null;
          if (extraGroup) pickerHost.appendChild(extraGroup);

          const box = el("div", { class: "kit-stage-line-box hidden" });
          const actionsRow = el("div", { class: "kit-orch-actions-row" });
          (mode.actions || []).forEach((a) => {
            const btn = el("button", {
              class: "kit-btn" + (a.primary ? " kit-btn-primary" : ""),
              text: a.label,
              onclick: () => {
                const left = leftSel.value;
                const right = rightSel.value;
                if (!left || !right) {
                  showStatus("Select both left and right.", { error: true });
                  return;
                }
                const values = Object.assign({ left, right }, extraGroup ? collectValues(extraGroup) : {});
                if (a.kind === "custom") {
                  a.run(values, currentWorkspace);
                  return;
                }
                runAction(
                  { kind: a.kind || "request", endpoint: a.endpoint, buildPayload: () => (a.buildPayload ? a.buildPayload(values, currentWorkspace) : values), submitLabel: a.label },
                  values,
                  currentWorkspace,
                  { box, log: null },
                  (result, err) => {
                    if (err) return;
                    if (a.onSuccess) a.onSuccess(result, currentWorkspace);
                  }
                );
              },
            });
            a._btn = btn;
            actionsRow.appendChild(btn);
          });
          pickerHost.appendChild(actionsRow);
          pickerHost.appendChild(box);

          let currentRows = [];
          function rowByValue(val) {
            return currentRows.find((r) => (mode.picker.value ? mode.picker.value(r) : r.id) === val);
          }

          async function populate() {
            const rows = (await mode.picker.source(currentWorkspace)) || [];
            currentRows = rows;
            const groups = new Map();
            rows.forEach((r) => {
              const g = mode.picker.groupBy ? mode.picker.groupBy(r) : "";
              if (!groups.has(g)) groups.set(g, []);
              groups.get(g).push(r);
            });
            [leftSel, rightSel].forEach((sel) => {
              sel.innerHTML = "";
              sel.appendChild(el("option", { value: "", text: "— select —" }));
              groups.forEach((items, groupName) => {
                const og = document.createElement("optgroup");
                og.label = groupName || "";
                items.forEach((r) => {
                  const opt = document.createElement("option");
                  opt.value = mode.picker.value ? mode.picker.value(r) : r.id;
                  opt.textContent = mode.picker.optionLabel ? mode.picker.optionLabel(r) : opt.value;
                  og.appendChild(opt);
                });
                sel.appendChild(og);
              });
            });
          }
          await populate();

          function updateActionVisibility() {
            const left = leftSel.value;
            const right = rightSel.value;
            const ctx = { left, right, leftRow: rowByValue(left), rightRow: rowByValue(right) };
            (mode.actions || []).forEach((a) => {
              if (a._btn) a._btn.classList.toggle("hidden", !!a.visible && !a.visible(ctx, currentWorkspace));
            });
          }
          updateActionVisibility();

          function onPairChange() {
            updateActionVisibility();
            if (mode.picker.onPairSelected) mode.picker.onPairSelected(leftSel.value, rightSel.value);
            if (mode.preview) {
              Promise.resolve(mode.preview.source(leftSel.value, rightSel.value, currentWorkspace)).then((match) => {
                if (!match) {
                  previewHost.classList.add("hidden");
                  return;
                }
                const rendered = mode.preview.render(match);
                previewHost.innerHTML = "";
                if (rendered instanceof Node) previewHost.appendChild(rendered);
                else previewHost.innerHTML = rendered;
                previewHost.classList.remove("hidden");
              });
            }
          }
          leftSel.addEventListener("change", onPairChange);
          rightSel.addEventListener("change", onPairChange);

          state_.prefillPicker = (left, right) => {
            leftSel.value = left;
            rightSel.value = right;
            onPairChange();
          };
        }

        renderPillsRow();
        renderBody();
        state.refreshBody = renderBody;

        state.refreshHistory = async () => {
          if (!sec.history || !state.historyHost) return;
          const rows = (await sec.history.list(currentWorkspace)) || [];
          state.historyHost.innerHTML = "";
          if (sec.history.searchable) {
            const search = el("input", { type: "search", class: "kit-orch-search", placeholder: "Filter…" });
            const tableHost = el("div", {});
            state.historyHost.appendChild(search);
            state.historyHost.appendChild(tableHost);
            const renderRows = (filterText) => {
              const filtered = filterText
                ? rows.filter((r) => JSON.stringify(r).toLowerCase().includes(filterText.toLowerCase()))
                : rows;
              tableHost.innerHTML = "";
              tableHost.appendChild(buildHistoryTable(sec.history, filtered));
            };
            search.addEventListener("input", debounce((e) => renderRows(e.target.value), 250));
            renderRows("");
          } else {
            state.historyHost.appendChild(buildHistoryTable(sec.history, rows));
          }
        };
        state.prefillPickerMode = (left, right) => {
          const pickerMode = sec.modes.find((m) => m.picker);
          if (pickerMode && state.activeModeId !== pickerMode.id) {
            state.activeModeId = pickerMode.id;
            renderPillsRow();
            renderBody();
          }
          // renderBody() rebuilt the picker; prefill happens on next tick
          // once renderPickerMode's async populate() has run.
          setTimeout(() => state.prefillPicker && state.prefillPicker(left, right), 0);
        };
      } else if (sec.table) {
        buildTableSection(sec, host, state);
      }
    }

    function activeRefresh(opts) {
      const sec = config.sections.find((s) => s.id === activeSectionId);
      if (sec) refreshSection(sec, opts);
    }

    function refreshSection(sec, opts) {
      const state = sectionState[sec.id];
      if (opts && opts.refreshBody && state.refreshBody) state.refreshBody();
      if (state.refreshHistory) state.refreshHistory();
      if (state.refreshTable) state.refreshTable();
    }

    document.body.append(header, el("div", { class: "kit-orch-layout" }, [navEl, mainEl]), statusBar);
    selectSection(activeSectionId);

    // ---- workspace popover -------------------------------------------------
    function updateWsButton() {
      wsBtn.textContent = currentWorkspace ? currentWorkspace[config.workspace.fields[0].id] : "No workspace";
    }

    async function openWorkspacePopover() {
      const backdrop = el("div", { class: "kit-dialog-backdrop" });
      const dialog = el("div", { class: "kit-dialog is-wide" });
      backdrop.appendChild(dialog);
      backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) backdrop.remove();
      });

      const listHost = el("div", { class: "kit-orch-ws-list" });
      const formHost = el("div", {});
      dialog.appendChild(el("h3", { text: "Workspaces" }));
      dialog.appendChild(listHost);
      dialog.appendChild(el("hr", {}));
      dialog.appendChild(formHost);

      async function refreshList() {
        const items = (await config.workspace.list()) || [];
        listHost.innerHTML = "";
        if (!items.length) {
          listHost.appendChild(el("p", { class: "kit-empty-state", text: "No saved workspaces yet." }));
          return;
        }
        items.forEach((ws) => {
          const nameField = config.workspace.fields[0].id;
          const row = el("div", { class: "kit-orch-ws-item" });
          row.appendChild(el("span", { text: ws[nameField] }));
          const actions = el("span", {});
          actions.appendChild(
            el("button", {
              class: "kit-btn",
              text: "Load",
              onclick: () => {
                setWorkspace(ws);
                backdrop.remove();
              },
            })
          );
          actions.appendChild(
            el("button", {
              class: "kit-btn kit-btn-danger",
              text: "✕",
              onclick: async () => {
                const ok = await Shell.confirmDialog(`Delete workspace "${ws[nameField]}"?`, { confirmLabel: "Delete" });
                if (!ok) return;
                await config.workspace.delete(ws.id);
                refreshList();
              },
            })
          );
          row.appendChild(actions);
          listHost.appendChild(row);
        });
      }

      const fieldGroup = buildFieldGroup(config.workspace.fields, currentWorkspace || {});
      formHost.appendChild(el("h3", { text: "New / edit workspace" }));
      formHost.appendChild(fieldGroup);
      formHost.appendChild(
        el("button", {
          class: "kit-btn kit-btn-primary",
          text: "Save workspace",
          onclick: async () => {
            const values = collectValues(fieldGroup);
            const nameField = config.workspace.fields[0].id;
            if (!values[nameField] || !String(values[nameField]).trim()) {
              showStatus("Name is required.", { error: true });
              return;
            }
            const saved = await config.workspace.save(values);
            setWorkspace(saved.workspace || saved);
            backdrop.remove();
          },
        })
      );

      document.body.appendChild(backdrop);
      await refreshList();
    }

    wsBtn.addEventListener("click", openWorkspacePopover);

    function setWorkspace(ws) {
      currentWorkspace = ws;
      updateWsButton();
      if (config.workspace && config.workspace.onLoad) config.workspace.onLoad(ws);
      activeRefresh({ refreshBody: true });
    }

    updateWsButton();

    return {
      setWorkspace,
      refreshSection: (id) => {
        const sec = config.sections.find((s) => s.id === id);
        if (sec) refreshSection(sec);
      },
      showStatus,
      prefillPicker: (sectionId, left, right) => {
        const state = sectionState[sectionId];
        if (state && state.prefillPickerMode) {
          selectSection(sectionId);
          state.prefillPickerMode(left, right);
        }
      },
      getWorkspace: () => currentWorkspace,
      elements: { header, nav: navEl, main: mainEl, statusBar },
    };
  }

  global.Orchestrator = { mount };
})(window);
