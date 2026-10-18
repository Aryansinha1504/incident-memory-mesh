/* Dependency-free UI. Every render is based on persisted API state. */
(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const icon = (name) =>
    '<svg class="icon" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  const pretty = (value) =>
    String(value || "Not recorded").replaceAll("_", " ");
  const stamp = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Date unavailable"
      : date.toLocaleString([], {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        });
  };
  const params = new URLSearchParams(location.search);
  const state = {
    tenant: params.get("tenant") || "Acme Corp",
    view: "investigations",
    dashboard: null,
    incident: null,
    selectedId: params.get("incident"),
    queue: "all",
    tab: "memory",
    memoryFilter: "all",
    busy: false,
    dashboardRequest: 0,
    incidentRequest: 0,
    libraryRequest: 0,
    library: [],
    libraryTotal: 0,
  };
  function applyTheme(theme) {
    const dark = theme === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    const toggle = $("#theme-toggle");
    if (toggle) {
      toggle.setAttribute("aria-label", dark ? "Use light theme" : "Use dark theme");
      toggle.title = dark ? "Use light theme" : "Use dark theme";
      toggle.textContent = dark ? "Light mode" : "Dark mode";
    }
  }
  function initializeTheme() {
    let saved = null;
    try { saved = localStorage.getItem("memory-mesh-theme"); } catch {}
    // Mesh's primary workspace is dark; retain an explicit user preference if
    // one has already been chosen.
    const preferred = saved || "dark";
    applyTheme(preferred);
    $("#theme-toggle")?.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      try { localStorage.setItem("memory-mesh-theme", next); } catch {}
    });
  }
  const scenarios = {
    admin: () =>
      "Repeated login failures for " +
      state.tenant +
      "'s admin account from a suspicious IP; a shared device is linked to another account.",
    device: () =>
      "Multiple employee accounts at " +
      state.tenant +
      " show repeated login failures from the same device and suspicious IP addresses.",
    token: () =>
      "An exposed admin token at " +
      state.tenant +
      " was reused after repeated login failures, followed by successful privileged access.",
  };

  async function api(path, body) {
    const response = await fetch(
      path,
      body === undefined
        ? { cache: "no-store" }
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
    );
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error(
        "The server returned an unreadable response. Check that the updated server is running.",
      );
    }
    if (!response.ok)
      throw new Error(
        data.error || "Request failed (" + response.status + ").",
      );
    return data;
  }
  function tenantQuery(extra = {}) {
    return new URLSearchParams({
      tenant_id: state.tenant,
      ...extra,
    }).toString();
  }
  function notify(message, type = "info") {
    $("#message").hidden = false;
    $("#message").className = "message " + type;
    $("#message").innerHTML =
      "<span>" +
      esc(message) +
      '</span><button class="icon-button" data-action="dismiss" aria-label="Dismiss message">' +
      icon("close") +
      "</button>";
  }
  function updateURL() {
    const query = new URLSearchParams({
      tenant: state.tenant,
      view: state.view,
    });
    if (state.selectedId) query.set("incident", state.selectedId);
    history.replaceState(null, "", "?" + query);
  }
  function setBusy(busy) {
    state.busy = busy;
    $("#tenant").disabled = busy;
    $("#refresh").disabled = busy;
    $$(
      "#incident-form input, #incident-form textarea, #incident-form button, .decision-form button, .decision-form textarea, [data-action=new], [data-action=use-case]",
    ).forEach((el) => {
      el.disabled = busy;
    });
    $("#submit-incident").innerHTML = busy
      ? '<span class="busy-indicator"></span> Saving investigation…'
      : "Run investigation " + icon("arrow");
  }
  function syncTenant() {
    const select = $("#tenant");
    if (![...select.options].some((o) => o.value === state.tenant)) {
      select.add(
        new Option(state.tenant, state.tenant),
        select.options.length - 1,
      );
    }
    select.value = state.tenant;
    $$(".tenant-name").forEach((el) => {
      el.textContent = state.tenant;
    });
  }
  function showView(view) {
    state.view = ["investigations", "library", "guide"].includes(view)
      ? view
      : "investigations";
    $$(".view").forEach((el) => {
      el.hidden = el.id !== "view-" + state.view;
    });
    $$(".nav-item").forEach((el) => {
      const active = el.dataset.view === state.view;
      el.classList.toggle("active", active);
      if (active) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    });
    $("#page-name").textContent = {
      investigations: "Investigations",
      library: "Memory library",
      guide: "How it works",
    }[state.view];
    updateURL();
    if (state.view === "library") loadLibrary();
  }
  function decisionOf(incident) {
    return (
      [...(incident.audit || [])]
        .reverse()
        .find((a) => a.event === "response_decision")?.detail || null
    );
  }
  function isReference(incident) {
    return incident.kind
      ? incident.kind === "reference"
      : incident.final_action === "Historical response recorded";
  }
  function statusOf(incident) {
    if (isReference(incident)) return ["reference", "Reference"];
    const decision = incident.decision || decisionOf(incident)?.decision;
    if (decision === "accepted") return ["accepted", "Approved"];
    if (decision === "rejected") return ["rejected", "Rejected"];
    return incident.final_action
      ? ["review", "Needs review"]
      : ["complete", "Incomplete"];
  }
  const badge = (incident) => {
    const [kind, label] = statusOf(incident);
    return '<span class="badge ' + kind + '">' + label + "</span>";
  };
  function renderStats() {
    const data = state.dashboard;
    $("#nav-count").textContent = data ? data.metrics.pending_review : "—";
    if (!data) {
      $("#stats").innerHTML = "";
      return;
    }
    const m = data.metrics;
    const totalMemory = Object.values(data.memory_counts).reduce(
      (sum, n) => sum + n,
      0,
    );
    const cards = [
      [
        "Investigations run",
        m.investigations,
        m.reference_incidents + " reference examples kept separately",
        "shield",
      ],
      [
        "Saved memories",
        totalMemory,
        "Events, patterns & response procedures",
        "layers",
      ],
      [
        "Awaiting your review",
        m.pending_review,
        "Recommendations that need a decision",
        "clock",
      ],
      [
        "Completed handoffs",
        m.completed_handoffs + " / " + m.total_handoffs,
        "Persisted agent-to-agent records",
        "activity",
      ],
    ];
    $("#stats").innerHTML = cards
      .map(
        ([label, value, hint, symbol]) =>
          '<div class="stat"><div class="stat-top"><span>' +
          label +
          "</span>" +
          icon(symbol) +
          '</div><div class="stat-value">' +
          esc(value) +
          '</div><div class="stat-foot">' +
          hint +
          "</div></div>",
      )
      .join("");
  }
  function renderQueue() {
    const all = state.dashboard?.recent_incidents || [];
    const search = $("#incident-search").value.trim().toLowerCase();
    const rows = all.filter(
      (item) =>
        (state.queue === "all" ||
          (state.queue === "reference"
            ? isReference(item)
            : statusOf(item)[0] === "review")) &&
        (item.description + " " + item.id).toLowerCase().includes(search),
    );
    $("#queue-count").textContent = rows.length;
    $("#queue-scope").textContent =
      "Latest " +
      all.length +
      " of " +
      (state.dashboard?.metrics.incidents || 0) +
      " saved records.";
    $$("[data-queue]").forEach((el) => {
      el.classList.toggle("selected", el.dataset.queue === state.queue);
      el.setAttribute("aria-pressed", String(el.dataset.queue === state.queue));
    });
    $("#incident-list").innerHTML = rows.length
      ? rows
          .map(
            (item) =>
              '<button class="incident-item' +
              (state.selectedId === item.id ? " selected" : "") +
              '" data-action="open" data-id="' +
              esc(item.id) +
              '" aria-pressed="' +
              (state.selectedId === item.id) +
              '">' +
              '<div class="item-meta"><span>' +
              esc(item.id.replace("inc_", "").toUpperCase()) +
              "</span>" +
              badge(item) +
              "</div><h3>" +
              esc(item.description) +
              '</h3><div class="item-footer"><span>' +
              (isReference(item)
                ? "Reference history"
                : esc(pretty(item.category))) +
              "</span><span>" +
              esc(stamp(item.created_at)) +
              "</span></div></button>",
          )
          .join("")
      : '<p class="empty-text">' +
        (all.length
          ? "No incidents match this filter."
          : "No history here yet. Start a new investigation to build this workspace’s memory.") +
        "</p>";
  }
  async function loadDashboard(autoSelect = false) {
    const request = ++state.dashboardRequest;
    const tenant = state.tenant;
    try {
      const data = await api("/api/dashboard?" + tenantQuery());
      if (request !== state.dashboardRequest || tenant !== state.tenant) return;
      state.dashboard = data;
      $("#connection").innerHTML =
        '<span class="tiny-dot"></span> Connected · FalkorDB';
      renderStats();
      renderQueue();
      if (autoSelect) {
        const id =
          state.selectedId ||
          data.recent_incidents.find((i) => i.kind === "investigation")?.id ||
          data.recent_incidents[0]?.id;
        if (id) await loadIncident(id);
        else renderIncident();
      }
    } catch (error) {
      if (request !== state.dashboardRequest || tenant !== state.tenant) return;
      $("#connection").textContent = "Connection unavailable";
      $("#incident-list").innerHTML =
        '<p class="empty-text">Could not load this workspace. Use Refresh to retry.</p>';
      notify(error.message, "error");
    }
  }
  async function loadIncident(id) {
    if (state.busy) return;
    const request = ++state.incidentRequest;
    const tenant = state.tenant;
    state.selectedId = id;
    state.incident = null;
    updateURL();
    renderQueue();
    $("#incident-detail").innerHTML =
      '<section class="panel empty-state"><span class="busy-indicator"></span><p>Opening saved investigation…</p></section>';
    try {
      const data = await api(
        "/api/incidents/" + encodeURIComponent(id) + "?" + tenantQuery(),
      );
      if (request !== state.incidentRequest || tenant !== state.tenant) return;
      state.incident = data;
      state.tab = "memory";
      state.memoryFilter = "all";
      renderIncident();
    } catch (error) {
      if (request !== state.incidentRequest || tenant !== state.tenant) return;
      state.selectedId = null;
      updateURL();
      renderQueue();
      renderIncident();
      notify("Could not open that incident: " + error.message, "error");
    }
  }
  function renderIncident() {
    const item = state.incident;
    if (!item) {
      $("#incident-detail").innerHTML =
        '<section class="panel empty-state"><div class="empty-icon">' +
        icon("shield") +
        "</div><h2>Your next investigation starts here</h2><p>Select a saved incident, or give the team a new case. Refreshing this page never creates an incident.</p>" +
        '<button class="button primary" data-action="new">Start investigation ' +
        icon("arrow") +
        "</button></section>";
      return;
    }
    const reference = isReference(item);
    const triage = (item.audit || []).find(
      (a) => a.event === "triage_decision",
    )?.detail;
    const investigation = item.handoffs.find(
      (h) => h.to_agent === "investigator",
    );
    const response = item.handoffs.find((h) => h.to_agent === "response");
    const steps = [
      [
        "01",
        "Triage",
        triage?.reason || "No triage output recorded.",
        !!triage,
        "Classification & priority",
      ],
      [
        "02",
        "Investigator",
        investigation?.findings.pattern || "No investigation output recorded.",
        investigation?.status === "completed",
        (item.retrieved_memories || []).length + " saved memories recalled",
      ],
      [
        "03",
        "Response",
        response?.findings.reason || "No response output recorded.",
        response?.status === "completed",
        "Recommendation, not execution",
      ],
    ];
    const title = reference
      ? "Reference from your workspace"
      : "Investigation findings";
    $("#incident-detail").innerHTML =
      '<section class="case-panel panel"><div class="case-topline"><span class="case-id">' +
      icon("shield") +
      esc(item.id) +
      '</span><div class="case-toolbar">' +
      badge(item) +
      '<button class="icon-button" data-action="export" title="Export incident JSON" aria-label="Export incident JSON">' +
      icon("download") +
      '</button></div></div><h2 class="case-title">' +
      title +
      '</h2><p class="case-description">' +
      esc(item.description) +
      '</p><div class="case-facts"><span>' +
      icon("clock") +
      esc(stamp(item.created_at)) +
      "</span><span>" +
      esc(item.user_id) +
      "</span>" +
      (item.severity
        ? '<span class="badge ' +
          (item.severity === "critical" ? "critical" : "high") +
          '">' +
          esc(item.severity) +
          " priority</span>"
        : "") +
      (item.category ? "<span>" + esc(pretty(item.category)) + "</span>" : "") +
      '</div><div class="separator"></div>' +
      (reference
        ? '<div class="reference-note"><strong>Background context, not a new investigation.</strong><p>This record has no agent handoffs or reviewable recommendation. Seeded examples make the first memory search useful; they do not count as investigations you ran.</p><button class="button secondary" data-action="use-case">Investigate a similar case ' +
          icon("arrow") +
          "</button></div>"
        : '<div class="workflow-heading"><h3>The agent trail</h3><span>Saved outputs · not a live stream</span></div><div class="agents">' +
          steps
            .map(
              ([number, name, text, done, hint]) =>
                '<article class="agent-card"><div class="agent-header"><span class="agent-icon">' +
                number +
                '</span><span class="agent-done">' +
                (done ? icon("check") + " Complete" : "Not completed") +
                "</span></div><h4>" +
                name +
                "</h4><p>" +
                esc(text) +
                "</p><span>" +
                esc(hint) +
                "</span></article>",
            )
            .join("") +
          "</div>" +
          renderRecommendation(item, response) +
          '<p class="model-note">Modeled resolution: ' +
          esc(item.resolution_seconds ?? "—") +
          's. Assigned by the demo’s prior-workflow rule, not elapsed time. <button class="text-button" data-view="guide">Understand the model ↗</button></p>') +
      '<div class="record-tabs" role="group" aria-label="Investigation details">' +
      [
        ["memory", "Memory trail", "layers"],
        ["activity", "Activity log", "activity"],
        ["json", "Handoff JSON", "code"],
      ]
        .map(
          ([tab, label, symbol]) =>
            '<button data-tab="' +
            tab +
            '" class="' +
            (state.tab === tab ? "active" : "") +
            '" aria-pressed="' +
            (state.tab === tab) +
            '">' +
            icon(symbol) +
            label +
            "</button>",
        )
        .join("") +
      '</div><div class="tab-body" id="record-body">' +
      renderRecord(item) +
      "</div></section>";
  }
  function renderRecommendation(item, response) {
    if (!response || !item.final_action)
      return '<div class="reference-note"><strong>No recommendation recorded yet.</strong><p>This workflow is incomplete. Refresh to check its saved state; do not treat it as resolved.</p></div>';
    const decision = decisionOf(item);
    return (
      '<section class="recommendation"><div class="recommendation-header"><span class="eyebrow">YOUR NEXT DECISION</span><span class="subtle-pill">Simulation only</span></div><h3>' +
      esc(item.final_action) +
      "</h3><p>" +
      esc(response.findings.reason) +
      '</p><div class="simulation-note">' +
      icon("shield") +
      "No account, token, or IP will be changed. Approval records a procedure for future recall.</div>" +
      (decision
        ? '<div class="decision-result ' +
          (decision.decision === "accepted" ? "accepted" : "rejected") +
          '">' +
          icon(decision.decision === "accepted" ? "check" : "close") +
          "<div><strong>" +
          (decision.decision === "accepted"
            ? "Approved · procedure saved"
            : "Rejected · no new procedure") +
          "</strong><p>Recorded by " +
          esc(decision.recorded_by) +
          (decision.note ? " · " + esc(decision.note) : "") +
          "</p></div></div>"
        : '<form class="decision-form" id="decision-form"><label for="decision-note">Analyst note <span>(optional)</span></label><textarea id="decision-note" rows="2" maxlength="4000" placeholder="Why is this response appropriate—or not?"></textarea><div class="decision-buttons"><button type="submit" name="decision" value="accepted" class="button primary">' +
          icon("check") +
          'Approve & save procedure</button><button type="submit" name="decision" value="rejected" class="button secondary">' +
          icon("close") +
          "Reject recommendation</button></div></form>") +
      "</section>"
    );
  }
  function memoryCard(memory, origin = "Saved memory") {
    const type = ["episodic", "semantic", "procedural"].includes(memory.type)
      ? memory.type
      : "episodic";
    return (
      '<article class="memory-card"><div class="memory-card-top"><span class="type-tag ' +
      type +
      '">' +
      esc(memory.type) +
      '</span><span class="memory-origin">' +
      origin +
      "</span></div><p>" +
      esc(memory.content) +
      '</p><div class="memory-card-footer">' +
      (memory.source_incident_id
        ? '<button class="source-link" data-action="open-source" data-id="' +
          esc(memory.source_incident_id) +
          '" title="Open the source incident">Source ' +
          esc(memory.source_incident_id.replace("inc_", "")) +
          " ↗</button>"
        : "<span>No linked incident</span>") +
      '<span title="Assigned by demo rules; not a calibrated probability">' +
      Math.round(Number(memory.confidence || 0) * 100) +
      "% heuristic</span></div></article>"
    );
  }
  function renderRecord(item) {
    if (state.tab === "json")
      return (
        '<p class="tab-intro">Structured handoffs stored for this incident. Fields describe the demo workers’ outputs.</p><pre class="code-block">' +
        esc(JSON.stringify(item.handoffs, null, 2)) +
        "</pre>"
      );
    if (state.tab === "activity")
      return (
        '<p class="tab-intro">Persisted events, oldest first. Expand an entry to inspect its data.</p><ol class="timeline">' +
        (item.audit || [])
          .map(
            (entry) =>
              '<li><div class="event-top"><strong>' +
              esc(pretty(entry.event)) +
              '</strong><time datetime="' +
              esc(entry.created_at) +
              '">' +
              esc(stamp(entry.created_at)) +
              '</time></div><div class="event-meta">' +
              esc(entry.agent) +
              '</div><details><summary>Event details</summary><pre class="code-block">' +
              esc(JSON.stringify(entry.detail, null, 2)) +
              "</pre></details></li>",
          )
          .join("") +
        "</ol>"
      );
    const recalled = item.retrieved_memories || [],
      created = item.memories || [];
    const cards = [
      ...(state.memoryFilter !== "created"
        ? recalled.map((m) => memoryCard(m, "Recalled from history"))
        : []),
      ...(state.memoryFilter !== "recalled"
        ? created.map((m) =>
            memoryCard(
              m,
              isReference(item) ? "Reference memory" : "Saved by this case",
            ),
          )
        : []),
    ];
    return (
      '<div class="memory-toolbar"><div class="segmented" role="group" aria-label="Filter memory origin">' +
      [
        ["all", "All"],
        ["recalled", "Recalled · " + recalled.length],
        ["created", "Saved here · " + created.length],
      ]
        .map(
          ([filter, label]) =>
            '<button data-memory-filter="' +
            filter +
            '" class="' +
            (state.memoryFilter === filter ? "active" : "") +
            '" aria-pressed="' +
            (state.memoryFilter === filter) +
            '">' +
            label +
            "</button>",
        )
        .join("") +
      '</div></div><p class="tab-intro">Recalled = existing context read by the investigator. Saved here = records linked to this case. Confidence is heuristic, not measured accuracy.</p>' +
      (cards.join("") ||
        '<p class="empty-text">No memories in this view. A fresh workspace has nothing to recall until its first investigation saves knowledge.</p>')
    );
  }
  async function loadLibrary(append = false) {
    const request = ++state.libraryRequest,
      tenant = state.tenant;
    const offset = append ? state.library.length : 0;
    $("#load-more").disabled = true;
    if (!append) {
      state.library = [];
      $("#library-grid").innerHTML =
        '<p class="empty-text">Reading saved memories…</p>';
    }
    try {
      const data = await api(
        "/api/memories?" +
          tenantQuery({
            query: $("#memory-search").value.trim(),
            type: $("#memory-type").value,
            offset,
            limit: 24,
          }),
      );
      if (request !== state.libraryRequest || tenant !== state.tenant) return;
      state.library = append ? [...state.library, ...data.items] : data.items;
      state.libraryTotal = data.total;
      $("#library-count").textContent = data.total + " matching memories";
      $("#library-grid").innerHTML = state.library.length
        ? state.library.map((m) => memoryCard(m)).join("")
        : '<section class="panel empty-state"><div class="empty-icon">' +
          icon("layers") +
          "</div><h2>No memories found</h2><p>Try another search, or run an investigation to start saving knowledge in this workspace.</p></section>";
      $("#load-more").hidden = state.library.length >= data.total;
    } catch (error) {
      if (request !== state.libraryRequest || tenant !== state.tenant) return;
      $("#library-grid").innerHTML =
        '<p class="empty-text">Could not read memories. Use Refresh to retry.</p>';
      $("#library-count").textContent = "";
      $("#load-more").hidden = true;
      notify(error.message, "error");
    } finally {
      if (request === state.libraryRequest) $("#load-more").disabled = false;
    }
  }
  async function switchTenant(tenant) {
    if (state.busy) return;
    state.tenant = tenant;
    state.selectedId = null;
    state.incident = null;
    state.dashboard = null;
    state.library = [];
    state.queue = "all";
    ++state.incidentRequest;
    ++state.libraryRequest;
    $("#incident-search").value = "";
    $("#memory-search").value = "";
    $("#memory-type").value = "all";
    $("#message").hidden = true;
    $("#library-grid").innerHTML = "";
    $("#library-count").textContent = "";
    $("#load-more").hidden = true;
    syncTenant();
    updateURL();
    renderStats();
    renderQueue();
    renderIncident();
    await loadDashboard(true);
    if (state.view === "library") await loadLibrary();
  }
  function chooseScenario(name) {
    if (!scenarios[name]) return;
    $("#description").value = scenarios[name]();
    $$("[data-scenario]").forEach((el) => {
      el.classList.toggle("selected", el.dataset.scenario === name);
      el.setAttribute("aria-pressed", String(el.dataset.scenario === name));
    });
  }
  function openInvestigation(description) {
    if (state.busy) return;
    chooseScenario("admin");
    if (description) {
      $("#description").value = description;
      $$("[data-scenario]").forEach((el) => {
        el.classList.remove("selected");
        el.setAttribute("aria-pressed", "false");
      });
    }
    $("#form-error").hidden = true;
    $("#incident-dialog").showModal();
  }
  async function submitIncident(event) {
    event.preventDefault();
    if (state.busy) return;
    const description = $("#description").value.trim(),
      user = $("#user-id").value.trim();
    if (description.length < 8 || !user) {
      $("#form-error").textContent =
        "Add a description of at least 8 characters and an analyst ID.";
      $("#form-error").hidden = false;
      return;
    }
    $("#form-error").hidden = true;
    // Invalidate any in-flight reads before the mutation; never retry a POST automatically.
    ++state.incidentRequest;
    ++state.dashboardRequest;
    setBusy(true);
    try {
      const result = await api("/api/incidents/run", {
        tenant_id: state.tenant,
        user_id: user,
        description,
      });
      state.incident = result;
      state.selectedId = result.id;
      state.tab = "memory";
      state.memoryFilter = "all";
      $("#incident-dialog").close();
      state.queue = "all";
      $("#incident-search").value = "";
      showView("investigations");
      renderIncident();
      notify(
        "Investigation saved. Read the recommendation, then approve or reject it below.",
      );
      await loadDashboard();
    } catch (error) {
      $("#form-error").textContent =
        error.message +
        " Check the inbox before retrying: the server may have saved the investigation.";
      $("#form-error").hidden = false;
    } finally {
      setBusy(false);
    }
  }
  async function submitDecision(event) {
    event.preventDefault();
    if (state.busy || !state.incident) return;
    const decision = event.submitter?.value;
    if (!["accepted", "rejected"].includes(decision)) return;
    const id = state.incident.id;
    const note = $("#decision-note").value.trim();
    ++state.incidentRequest;
    ++state.dashboardRequest;
    setBusy(true);
    try {
      state.incident = await api(
        "/api/incidents/" + encodeURIComponent(id) + "/decision",
        {
          tenant_id: state.tenant,
          user_id: $("#user-id").value.trim() || state.incident.user_id,
          decision,
          note,
        },
      );
      renderIncident();
      notify(
        decision === "accepted"
          ? "Approval recorded. A procedural memory was saved; no real security action was executed."
          : "Rejection recorded. No procedural memory was created.",
      );
      await loadDashboard();
    } catch (error) {
      notify(
        error.message + " Refresh the incident to check its saved decision.",
        "error",
      );
    } finally {
      setBusy(false);
    }
  }
  function exportIncident() {
    if (!state.incident) return;
    const data = {
      demo_notice:
        "Deterministic workers. Timings and confidence are modeled. No real security action executed.",
      ...state.incident,
    };
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = state.incident.id + ".json";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    if (button.dataset.view) {
      showView(button.dataset.view);
      return;
    }
    if (button.dataset.close) {
      if (!state.busy) $("#" + button.dataset.close).close();
      return;
    }
    if (button.dataset.scenario) {
      chooseScenario(button.dataset.scenario);
      return;
    }
    if (button.dataset.queue) {
      state.queue = button.dataset.queue;
      renderQueue();
      return;
    }
    if (button.dataset.tab) {
      state.tab = button.dataset.tab;
      $$("[data-tab]").forEach((el) => {
        el.classList.toggle("active", el.dataset.tab === state.tab);
        el.setAttribute("aria-pressed", String(el.dataset.tab === state.tab));
      });
      $("#record-body").innerHTML = renderRecord(state.incident);
      return;
    }
    if (button.dataset.memoryFilter) {
      state.memoryFilter = button.dataset.memoryFilter;
      $("#record-body").innerHTML = renderRecord(state.incident);
      return;
    }
    switch (button.dataset.action) {
      case "new":
        openInvestigation();
        break;
      case "use-case":
        openInvestigation(state.incident?.description);
        break;
      case "open":
        loadIncident(button.dataset.id);
        break;
      case "open-source":
        if (!state.busy) {
          showView("investigations");
          loadIncident(button.dataset.id);
        }
        break;
      case "dismiss":
        $("#message").hidden = true;
        break;
      case "export":
        exportIncident();
        break;
    }
  });
  document.addEventListener("submit", (event) => {
    if (event.target.id === "decision-form") submitDecision(event);
  });
  $("#incident-form").addEventListener("submit", submitIncident);
  $("#tenant").addEventListener("change", (event) => {
    if (event.target.value === "__new__") {
      event.target.value = state.tenant;
      $("#tenant-dialog").showModal();
    } else switchTenant(event.target.value);
  });
  $("#tenant-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const tenant = $("#tenant-name").value.trim();
    if (!tenant || tenant === "__new__") {
      $("#tenant-name").setCustomValidity("Enter a workspace name.");
      $("#tenant-name").reportValidity();
      return;
    }
    $("#tenant-dialog").close();
    switchTenant(tenant);
  });
  $("#tenant-name").addEventListener("input", () =>
    $("#tenant-name").setCustomValidity(""),
  );
  $$("dialog").forEach((dialog) =>
    dialog.addEventListener("cancel", (event) => {
      if (state.busy) event.preventDefault();
    }),
  );
  $("#incident-search").addEventListener("input", renderQueue);
  let searchTimer;
  $("#memory-search").addEventListener("input", () => {
    ++state.libraryRequest;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadLibrary(), 220);
  });
  $("#memory-type").addEventListener("change", () => {
    clearTimeout(searchTimer);
    loadLibrary();
  });
  $("#load-more").addEventListener("click", () => loadLibrary(true));
  $("#refresh").addEventListener("click", async () => {
    if (state.busy) return;
    $("#message").hidden = true;
    await loadDashboard(true);
    if (state.view === "library") await loadLibrary();
  });
  initializeTheme();
  syncTenant();
  showView(params.get("view") || "investigations");
  loadDashboard(true);
})();
