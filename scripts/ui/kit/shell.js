// compare-ui-kit shell: chrome + interaction patterns shared by every
// consuming tool's diff/compare UI. Vanilla ES2020, no deps, no build.
// The tool page supplies data + callbacks via Shell.mount(); the shell owns
// header/sidebar/tabs/filter/toast/theme/keyboard chrome around a single
// `detail` host element the tool renders into.
(function (global) {
  "use strict";

  const KIT_VERSION = "kit-0.5.0";
  const SIDEBAR_CHUNK_SIZE = 200;
  const SIDEBAR_CHUNK_THRESHOLD = 2000;

  // -----------------------------------------------------------------------
  // Small DOM helpers
  // -----------------------------------------------------------------------
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
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

  function debounce(fn, ms) {
    let t = null;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  // -----------------------------------------------------------------------
  // Prefs: localStorage under a single namespaced JSON blob per storageKey.
  // -----------------------------------------------------------------------
  function makePrefs(storageKey) {
    const load = () => {
      try {
        return JSON.parse(localStorage.getItem(storageKey) || "{}");
      } catch (e) {
        return {};
      }
    };
    const save = (data) => {
      try {
        localStorage.setItem(storageKey, JSON.stringify(data));
      } catch (e) {
        /* storage unavailable/full — prefs just won't persist */
      }
    };
    return {
      get(key, fallback) {
        const data = load();
        return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : fallback;
      },
      set(key, value) {
        const data = load();
        data[key] = value;
        save(data);
      },
    };
  }

  // -----------------------------------------------------------------------
  // Toast
  // -----------------------------------------------------------------------
  let toastHost = null;
  function ensureToastHost() {
    if (!toastHost || !document.body.contains(toastHost)) {
      toastHost = el("div", { class: "kit-toast-host" });
      document.body.appendChild(toastHost);
    }
    return toastHost;
  }

  function toast(msg, opts) {
    const isError = !!(opts && opts.error);
    const host = ensureToastHost();
    const node = el("div", { class: "kit-toast" + (isError ? " is-error" : ""), text: msg });
    host.appendChild(node);
    setTimeout(() => node.remove(), isError ? 6000 : 3000);
  }

  // Per-server session token injected into the bootstrap HTML by the server
  // (window.__MCT_TOKEN__). API routes require it on the X-MCT-Token header;
  // foreign origins never receive it because the bootstrap is same-origin.
  function sessionHeaders(extra) {
    const h = Object.assign({}, extra || {});
    if (window.__MCT_TOKEN__) h["X-MCT-Token"] = window.__MCT_TOKEN__;
    return h;
  }

  // -----------------------------------------------------------------------
  // fetchJSON: no-store baked in, throws Error(data.error || HTTP status)
  // -----------------------------------------------------------------------
  async function fetchJSON(url, opts) {
    const o = Object.assign({ cache: "no-store" }, opts || {});
    o.headers = sessionHeaders(o.headers);
    const res = await fetch(url, o);
    let data;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    // Subprocess-wrapping servers commonly report failure via "stderr" with
    // no dedicated "error" field (e.g. a {"ok":false,"stdout","stderr"} CLI
    // shell-out response) — fall back to it before the generic HTTP status.
    if (!res.ok) throw new Error((data && (data.error || data.stderr)) || `HTTP ${res.status}`);
    return data;
  }

  // -----------------------------------------------------------------------
  // streamPost: POST a JSON body, parse a text/event-stream response.
  // EventSource can't POST a body, so long-running-subprocess-over-SSE
  // endpoints need this instead. Server framing expected:
  //   data: <line>\n\n              -> onLine(line)      (default "message" event)
  //   event: done\ndata: <json>\n\n -> onDone(JSON.parse(json))
  //   event: error\ndata: <msg>\n\n -> onError(msg)
  // -----------------------------------------------------------------------
  async function streamPost(url, body, handlers) {
    const { onLine, onDone, onError } = handlers || {};
    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: sessionHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
      if (onError) onError(err.message);
      return;
    }
    if (!res.ok || !res.body) {
      let msg = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data && data.error) msg = data.error;
      } catch (e) {
        /* not JSON — keep the HTTP status message */
      }
      if (onError) onError(msg);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const frames = buf.split("\n\n");
      buf = frames.pop();
      frames.forEach((frame) => {
        if (!frame.trim()) return;
        let event = "message";
        let data = "";
        frame.split("\n").forEach((line) => {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trim();
        });
        if (event === "done") {
          let payload = null;
          try {
            payload = JSON.parse(data);
          } catch (e) {
            /* malformed done payload — pass null through */
          }
          if (onDone) onDone(payload);
        } else if (event === "error") {
          if (onError) onError(data);
        } else if (onLine) {
          onLine(data);
        }
      });
    }
  }

  // -----------------------------------------------------------------------
  // downloadBlob
  // -----------------------------------------------------------------------
  function downloadBlob(data, filename, mime) {
    const blob =
      data instanceof Blob ? data : new Blob([data], { type: mime || "application/octet-stream" });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: filename });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // -----------------------------------------------------------------------
  // confirmDialog: replaces raw alert()/confirm()
  // -----------------------------------------------------------------------
  function confirmDialog(msg, opts) {
    return new Promise((resolve) => {
      const confirmLabel = (opts && opts.confirmLabel) || "OK";
      const cancelLabel = (opts && opts.cancelLabel) || "Cancel";
      const backdrop = el("div", { class: "kit-dialog-backdrop" });
      const finish = (result) => {
        backdrop.remove();
        resolve(result);
      };
      const dialog = el("div", { class: "kit-dialog" }, [
        el("div", { class: "msg", text: msg }),
        el("div", { class: "actions" }, [
          el("button", { class: "kit-btn", text: cancelLabel, onclick: () => finish(false) }),
          el("button", {
            class: "kit-btn kit-btn-primary",
            text: confirmLabel,
            onclick: () => finish(true),
          }),
        ]),
      ]);
      backdrop.appendChild(dialog);
      backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) finish(false);
      });
      document.body.appendChild(backdrop);
      dialog.querySelector(".kit-btn-primary").focus();
    });
  }

  // -----------------------------------------------------------------------
  // banner: render a list of warning strings into a host element
  // -----------------------------------------------------------------------
  function banner(hostEl, items) {
    hostEl.innerHTML = "";
    (items || []).forEach((w) => {
      hostEl.appendChild(el("div", { class: "kit-warning-banner", text: w }));
    });
  }

  // -----------------------------------------------------------------------
  // acceptControls: wires accept/unaccept buttons to POST endpoints
  // -----------------------------------------------------------------------
  function acceptControls(opts) {
    const { acceptUrl, unacceptUrl, onDone } = opts;
    async function post(url, body, action) {
      try {
        const data = await fetchJSON(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (onDone) onDone(data, null, { action, body });
        return data;
      } catch (err) {
        toast(err.message, { error: true });
        if (onDone) onDone(null, err, { action, body });
        throw err;
      }
    }
    return {
      acceptButton(body, label) {
        return el("button", {
          class: "kit-btn",
          text: label || "Accept",
          onclick: () => post(acceptUrl, body, "accept"),
        });
      },
      unacceptButton(body, label) {
        return el("button", {
          class: "kit-btn",
          text: label || "Undo",
          onclick: () => post(unacceptUrl, body, "unaccept"),
        });
      },
      accept: (body) => post(acceptUrl, body, "accept"),
      unaccept: (body) => post(unacceptUrl, body, "unaccept"),
    };
  }

  // -----------------------------------------------------------------------
  // keys: j/k list navigation + enter, ignored while typing in an input
  // -----------------------------------------------------------------------
  function keys(handlers) {
    function isTyping() {
      const a = document.activeElement;
      return a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable);
    }
    const listener = (e) => {
      if (e.key === "/" && !isTyping()) {
        if (handlers.slash) {
          e.preventDefault();
          handlers.slash();
        }
        return;
      }
      if (e.key === "Escape") {
        if (handlers.esc) handlers.esc();
        return;
      }
      if (isTyping()) return;
      if (e.key === "j" && handlers.j) handlers.j();
      else if (e.key === "k" && handlers.k) handlers.k();
      else if (e.key === "Enter" && handlers.enter) handlers.enter();
    };
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }

  // -----------------------------------------------------------------------
  // Theme: dark/light, persisted, defaults to prefers-color-scheme
  // -----------------------------------------------------------------------
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  // -----------------------------------------------------------------------
  // mount(): builds the chrome, returns a handle for the tool to push data
  // through as it arrives.
  // -----------------------------------------------------------------------
  function mount(config) {
    const cfg = config || {};
    const storageKey = cfg.storageKey || "compare-ui-kit";
    const prefs = makePrefs(storageKey);

    document.body.innerHTML = "";

    const theme = prefs.get("theme", null);
    if (theme) applyTheme(theme);

    // ---- header --------------------------------------------------------
    const brand = el("div", { class: "kit-brand" }, [
      document.createTextNode(cfg.title || "compare"),
    ]);

    const provenanceEl = el("div", { class: "kit-provenance" });
    setProvenance(cfg.provenance);

    const statStrip = el("div", { class: "kit-stat-strip" });
    let activeStatId = null;
    function renderStats(stats) {
      statStrip.innerHTML = "";
      (stats || []).forEach((s) => {
        const card = el(
          "div",
          {
            class:
              "kit-stat-card" +
              (s.tone ? ` tone-${s.tone}` : "") +
              (s.filter ? " is-clickable" : "") +
              (s.filter && s.id === activeStatId ? " is-active" : ""),
            onclick: s.filter
              ? () => {
                  activeStatId = activeStatId === s.id ? null : s.id;
                  renderStats(stats);
                  if (cfg.onStatClick) cfg.onStatClick(activeStatId);
                }
              : null,
          },
          [
            el("span", { class: "n", text: (s.count || 0).toLocaleString() }),
            el("span", { class: "l", text: s.label }),
          ]
        );
        statStrip.appendChild(card);
      });
    }
    renderStats(cfg.stats);

    const headerActions = el("div", { class: "kit-header-actions" });
    (cfg.actions || []).forEach((a) => {
      headerActions.appendChild(
        el("button", { class: "kit-btn", text: a.label, title: a.title || "", onclick: a.onClick })
      );
    });
    (cfg.toggles || []).forEach((t) => {
      const checked = prefs.get(`toggle:${t.id}`, !!t.default);
      const input = el("input", { type: "checkbox" });
      input.checked = checked;
      input.addEventListener("change", () => {
        prefs.set(`toggle:${t.id}`, input.checked);
        if (t.onChange) t.onChange(input.checked);
      });
      headerActions.appendChild(
        el("label", { class: "kit-toggle", title: t.title || "" }, [
          input,
          document.createTextNode(t.label),
        ])
      );
    });

    const themeBtn = el("button", {
      class: "kit-btn",
      text: "Theme",
      title: "Toggle light/dark theme",
      onclick: () => {
        const current =
          document.documentElement.getAttribute("data-theme") ||
          (global.matchMedia && global.matchMedia("(prefers-color-scheme: light)").matches
            ? "light"
            : "dark");
        const next = current === "light" ? "dark" : "light";
        applyTheme(next);
        prefs.set("theme", next);
      },
    });
    headerActions.appendChild(themeBtn);

    const header = el("header", { class: "kit-header" }, [
      brand,
      provenanceEl,
      statStrip,
      headerActions,
    ]);

    // ---- warnings --------------------------------------------------------
    const warningsEl = el("div", { class: "kit-warnings" });
    banner(warningsEl, cfg.warnings);

    // ---- sidebar --------------------------------------------------------
    const sidebarSearch = el("input", {
      type: "search",
      placeholder: "Filter… (press / to focus)",
    });
    const sidebarList = el("div", { class: "kit-sidebar-list" });
    const sidebar = el("aside", { class: "kit-sidebar" }, [
      el("div", { class: "kit-sidebar-search" }, [sidebarSearch]),
      sidebarList,
    ]);

    let sidebarItems = [];
    let sidebarSelected = prefs.get("selected", null);
    const sidebarGroups = !!(cfg.sidebar && cfg.sidebar.groups);
    const collapsedGroups = new Set(prefs.get("collapsedGroups", []));

    function itemKey(item, idx) {
      return item && item.id !== undefined ? String(item.id) : String(idx);
    }

    function renderSidebarFlat(items) {
      sidebarList.innerHTML = "";
      appendChunked(sidebarList, items, (item) => renderItemRow(item));
    }

    function renderSidebarGrouped(items) {
      sidebarList.innerHTML = "";
      const groups = new Map();
      items.forEach((item) => {
        const g = item.group || "";
        if (!groups.has(g)) groups.set(g, []);
        groups.get(g).push(item);
      });
      groups.forEach((groupItems, groupName) => {
        const collapsed = collapsedGroups.has(groupName);
        const body = el("div", { class: "kit-sidebar-group-body" });
        appendChunked(body, groupItems, (item) => renderItemRow(item));
        const titleChildren = [
          el("span", { class: "chevron", text: "▾" }),
          el("span", { text: groupName || "(ungrouped)" }),
          el("span", { class: "count", text: String(groupItems.length) }),
        ];
        if (cfg.sidebar.renderGroupHeader) {
          const extra = cfg.sidebar.renderGroupHeader(groupName, groupItems);
          if (extra) titleChildren.push(extra);
        }
        const groupEl = el(
          "div",
          { class: "kit-sidebar-group" + (collapsed ? " is-collapsed" : "") },
          [
            el(
              "div",
              {
                class: "kit-sidebar-group-title",
                onclick: () => {
                  if (collapsedGroups.has(groupName)) collapsedGroups.delete(groupName);
                  else collapsedGroups.add(groupName);
                  prefs.set("collapsedGroups", Array.from(collapsedGroups));
                  groupEl.classList.toggle("is-collapsed");
                },
              },
              titleChildren
            ),
            body,
          ]
        );
        sidebarList.appendChild(groupEl);
      });
    }

    function appendChunked(host, items, makeRow) {
      if (items.length <= SIDEBAR_CHUNK_THRESHOLD) {
        items.forEach((item) => host.appendChild(makeRow(item)));
        return;
      }
      let i = 0;
      function step() {
        const end = Math.min(i + SIDEBAR_CHUNK_SIZE, items.length);
        for (; i < end; i++) host.appendChild(makeRow(items[i]));
        if (i < items.length) setTimeout(step, 0);
      }
      step();
    }

    function renderItemRow(item) {
      const row =
        cfg.sidebar && cfg.sidebar.renderItem ? cfg.sidebar.renderItem(item) : defaultItemRow(item);
      row.classList.add("kit-item-row");
      if (
        sidebarSelected !== null &&
        itemKey(item, sidebarItems.indexOf(item)) === sidebarSelected
      ) {
        row.classList.add("is-active");
      }
      row.addEventListener("click", () => selectItem(item));
      return row;
    }

    function defaultItemRow(item) {
      return el("div", {}, [
        el("span", { class: "name", text: item.label || item.id || String(item) }),
      ]);
    }

    function selectItem(item) {
      sidebarSelected = itemKey(item, sidebarItems.indexOf(item));
      prefs.set("selected", sidebarSelected);
      sidebarList.querySelectorAll(".kit-item-row").forEach((r) => r.classList.remove("is-active"));
      renderSidebar();
      if (cfg.sidebar && cfg.sidebar.onSelect) cfg.sidebar.onSelect(item);
    }

    function renderSidebar() {
      if (sidebarGroups) renderSidebarGrouped(sidebarItems);
      else renderSidebarFlat(sidebarItems);
    }

    function setSidebarItems(items) {
      sidebarItems = items || [];
      renderSidebar();
    }

    function selectSidebarItem(id) {
      const target = sidebarItems.find((it, idx) => itemKey(it, idx) === String(id));
      if (target) selectItem(target);
    }

    function setGroupsCollapsed(collapsed) {
      if (collapsed) {
        sidebarItems.forEach((it) => collapsedGroups.add(it.group || ""));
      } else {
        collapsedGroups.clear();
      }
      prefs.set("collapsedGroups", Array.from(collapsedGroups));
      renderSidebar();
    }

    sidebarSearch.addEventListener(
      "input",
      debounce((e) => {
        const q = e.target.value.trim().toLowerCase();
        const filtered = q
          ? sidebarItems.filter((it) => JSON.stringify(it).toLowerCase().includes(q))
          : sidebarItems;
        if (sidebarGroups) renderSidebarGrouped(filtered);
        else renderSidebarFlat(filtered);
      }, 250)
    );

    // ---- main: tabs + filter + detail ------------------------------------
    const tabsEl = el("div", { class: "kit-tabs" });
    let activeTab = prefs.get("activeTab", (cfg.tabs && cfg.tabs[0] && cfg.tabs[0].id) || null);
    function renderTabs() {
      tabsEl.innerHTML = "";
      (cfg.tabs || []).forEach((t) => {
        tabsEl.appendChild(
          el("button", {
            class: "kit-tab" + (t.id === activeTab ? " is-active" : ""),
            text: t.label,
            onclick: () => {
              activeTab = t.id;
              prefs.set("activeTab", activeTab);
              renderTabs();
              if (cfg.onTabChange) cfg.onTabChange(activeTab);
            },
          })
        );
      });
    }
    renderTabs();

    const filterInput = el("input", {
      type: "search",
      class: "kit-filter",
      placeholder: (cfg.filter && cfg.filter.placeholder) || "Filter… (press / to focus)",
    });
    if (cfg.filter && cfg.filter.onChange) {
      filterInput.addEventListener(
        "input",
        debounce((e) => cfg.filter.onChange(e.target.value.trim()), 250)
      );
    }

    const showToolbar = !!(cfg.tabs && cfg.tabs.length) || !!cfg.filter;
    const toolbar = el("div", { class: "kit-toolbar" }, [tabsEl, filterInput]);
    const detailHost = el("div", { class: "kit-detail-host" });
    if (cfg.detail) detailHost.appendChild(cfg.detail);

    const footer = el("div", { class: "kit-footer", text: KIT_VERSION });

    const main = el("main", { class: "kit-main" }, [
      ...(showToolbar ? [toolbar] : []),
      detailHost,
      footer,
    ]);
    const layout = el("div", { class: "kit-layout" }, [sidebar, main]);

    document.body.appendChild(header);
    document.body.appendChild(warningsEl);
    document.body.appendChild(layout);

    keys({
      slash: () => (cfg.sidebar ? sidebarSearch : filterInput).focus(),
      esc: () => {
        sidebarSearch.value = "";
        filterInput.value = "";
      },
      j: () => stepSidebar(1),
      k: () => stepSidebar(-1),
    });

    function stepSidebar(dir) {
      const rows = Array.from(sidebarList.querySelectorAll(".kit-item-row"));
      if (!rows.length) return;
      const activeIdx = rows.findIndex((r) => r.classList.contains("is-active"));
      const nextIdx = Math.min(rows.length - 1, Math.max(0, activeIdx + dir));
      rows[nextIdx].click();
      rows[nextIdx].scrollIntoView({ block: "nearest" });
    }

    function setProvenance(p) {
      provenanceEl.innerHTML = "";
      if (!p) return;
      if (p.leftHtml) provenanceEl.insertAdjacentHTML("beforeend", p.leftHtml);
      if (p.leftHtml && p.rightHtml) provenanceEl.insertAdjacentHTML("beforeend", "  ·  ");
      if (p.rightHtml) provenanceEl.insertAdjacentHTML("beforeend", p.rightHtml);
    }

    return {
      version: KIT_VERSION,
      prefs,
      setProvenance,
      setWarnings: (items) => banner(warningsEl, items),
      setStats: (stats) => renderStats(stats),
      setSidebarItems,
      selectSidebarItem,
      setGroupsCollapsed,
      setTabs: (tabs) => {
        cfg.tabs = tabs;
        renderTabs();
      },
      setActiveTab: (id) => {
        activeTab = id;
        prefs.set("activeTab", id);
        renderTabs();
      },
      getActiveTab: () => activeTab,
      setToggle: (id, checked) => prefs.set(`toggle:${id}`, checked),
      getToggle: (id, fallback) => prefs.get(`toggle:${id}`, fallback),
      focusFilter: () => filterInput.focus(),
      elements: {
        header,
        sidebar,
        main,
        detailHost,
        filterInput,
        sidebarSearch,
        warnings: warningsEl,
      },
    };
  }

  const Shell = {
    VERSION: KIT_VERSION,
    mount,
    toast,
    fetchJSON,
    streamPost,
    sessionHeaders,
    downloadBlob,
    confirmDialog,
    banner,
    acceptControls,
    keys,
    prefs: { create: makePrefs },
    _internal: { el, escapeHtml, debounce },
  };

  global.Shell = Shell;
})(window);
