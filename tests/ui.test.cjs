const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM, VirtualConsole } = require("jsdom");

const root = path.join(__dirname, "..", "memory_mesh", "static");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "app.js"), "utf8");
const clone = (data) => JSON.parse(JSON.stringify(data));
function memory(id, type, source = "inc_one") {
  return {
    id,
    type,
    source_incident_id: source,
    tenant_id: "Acme Corp",
    user_id: "analyst",
    content: type + " memory of repeated admin login failures",
    confidence: 0.82,
  };
}
function incident(id = "inc_one", extra = {}) {
  return {
    id,
    tenant_id: "Acme Corp",
    user_id: "analyst",
    description: "Repeated admin login failures",
    category: "account_takeover",
    severity: "high",
    status: "resolved",
    resolution_seconds: 90,
    created_at: "2026-10-18T12:00:00+00:00",
    final_action: "Disable the exposed token",
    handoffs: [
      {
        to_agent: "investigator",
        from_agent: "triage",
        status: "completed",
        findings: { pattern: "Credential attack", memory_ids: ["old"] },
      },
      {
        to_agent: "response",
        from_agent: "investigator",
        status: "completed",
        findings: { reason: "Privileged access at risk", accepted: null },
      },
    ],
    audit: [
      {
        event: "triage_decision",
        agent: "triage",
        created_at: "2026-10-18T12:00:00Z",
        detail: { reason: "Admin keywords" },
      },
    ],
    memories: [memory("m1", "episodic", id), memory("m2", "semantic", id)],
    retrieved_memories: [memory("old", "episodic", "inc_source")],
    ...extra,
  };
}
async function until(predicate) {
  for (let n = 0; n < 50; n++) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.ok(predicate(), "UI did not reach expected state");
}
async function mount(
  t,
  { records = [incident()], url = "http://mesh.test/", intercept } = {},
) {
  const calls = [],
    errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error.message));
  const dom = new JSDOM(html, {
    url,
    runScripts: "outside-only",
    virtualConsole,
  });
  t.after(async () => {
    // Let queued fetch/render continuations settle before destroying the DOM.
    for (let n = 0; n < 3; n++)
      await new Promise((resolve) => setImmediate(resolve));
    dom.window.close();
  });
  const w = dom.window,
    d = w.document;
  w.HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  w.HTMLDialogElement.prototype.close = function () {
    this.open = false;
  };
  w.URL.createObjectURL = () => "blob:test-export";
  w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = function () {
    calls.push({ download: this.download });
  };
  const database = new Map(records.map((item) => [item.id, clone(item)]));
  w.fetch = async (requestPath, options = {}) => {
    const request = new URL(requestPath, w.location.origin);
    const body = options.body ? JSON.parse(options.body) : undefined;
    const call = { path: request.pathname, query: request.searchParams, body };
    calls.push(call);
    const response = (data, status = 200) => ({
      ok: status < 400,
      status,
      json: async () => clone(data),
    });
    if (intercept) {
      const result = await intercept(call);
      if (result) return response(result.data, result.status);
    }
    const tenant = request.searchParams.get("tenant_id") || body?.tenant_id;
    const selected = [...database.values()].filter(
      (item) => item.tenant_id === tenant,
    );
    if (request.pathname === "/api/dashboard") {
      return response({
        metrics: {
          incidents: selected.length,
          investigations: selected.filter(
            (i) => i.final_action !== "Historical response recorded",
          ).length,
          reference_incidents: selected.filter(
            (i) => i.final_action === "Historical response recorded",
          ).length,
          pending_review: selected.filter(
            (i) =>
              i.handoffs.length &&
              !i.audit.some((a) => a.event === "response_decision"),
          ).length,
          completed_handoffs: selected.reduce(
            (s, i) => s + i.handoffs.length,
            0,
          ),
          total_handoffs: selected.reduce((s, i) => s + i.handoffs.length, 0),
        },
        memory_counts: { episodic: selected.length, semantic: selected.length },
        recent_incidents: selected
          .map((i) => ({
            ...i,
            kind:
              i.final_action === "Historical response recorded"
                ? "reference"
                : "investigation",
          }))
          .reverse(),
      });
    }
    if (request.pathname === "/api/memories") {
      let memories = selected.flatMap((i) => i.memories);
      const type = request.searchParams.get("type");
      if (type !== "all") memories = memories.filter((m) => m.type === type);
      const query = request.searchParams.get("query").toLowerCase();
      memories = memories.filter((m) =>
        m.content.toLowerCase().includes(query),
      );
      const offset = Number(request.searchParams.get("offset"));
      return response({
        items: memories.slice(offset, offset + 24),
        total: memories.length,
      });
    }
    if (request.pathname === "/api/incidents/run") {
      const item = incident("inc_created", {
        tenant_id: tenant,
        user_id: body.user_id,
        description: body.description,
      });
      database.set(item.id, item);
      return response(item);
    }
    const id = request.pathname.split("/")[3],
      item = database.get(id);
    if (!item || item.tenant_id !== tenant)
      return response({ error: "incident not found" }, 404);
    if (request.pathname.endsWith("/decision")) {
      item.audit.push({
        event: "response_decision",
        detail: {
          decision: body.decision,
          recorded_by: body.user_id,
          note: body.note,
        },
      });
      if (body.decision === "accepted")
        item.memories.push(memory("procedure", "procedural", id));
    }
    return response(item);
  };
  w.eval(script);
  await until(() =>
    d.querySelector("#connection").textContent.includes("Connected"),
  );
  if (
    records.length &&
    records.some(
      (i) =>
        i.tenant_id ===
        (new URL(url).searchParams.get("tenant") || "Acme Corp"),
    )
  ) {
    await until(() => d.querySelector(".case-title"));
  }
  return {
    w,
    d,
    calls,
    errors,
    database,
    click: (selector) => {
      const button = d.querySelector(selector);
      assert.ok(button, selector);
      button.click();
    },
    submit: (selector, button) =>
      d.querySelector(selector).dispatchEvent(
        new w.SubmitEvent("submit", {
          bubbles: true,
          cancelable: true,
          submitter: button ? d.querySelector(button) : null,
        }),
      ),
    text: (selector) => d.querySelector(selector)?.textContent || "",
  };
}

test("initial render selects a saved case; navigation and refresh never create incidents", async (t) => {
  const ui = await mount(t);
  assert.match(ui.text("#incident-detail"), /Needs review/);
  assert.ok(ui.d.querySelector("#decision-form"));
  assert.match(ui.text("#stats"), /Investigations run/);
  ui.click("[data-tab=activity]");
  assert.match(ui.text("#record-body"), /triage decision/);
  ui.click("[data-tab=json]");
  assert.match(ui.text("#record-body"), /investigator/);
  ui.click("[data-view=guide]");
  assert.equal(ui.d.querySelector("#view-guide").hidden, false);
  ui.click("#refresh");
  await until(
    () => ui.calls.filter((c) => c.path === "/api/dashboard").length === 2,
  );
  assert.equal(ui.calls.filter((c) => c.body).length, 0);
  assert.deepEqual(ui.errors, []);
});

test("approval survives rendering, saves one procedure and hides decision buttons", async (t) => {
  const ui = await mount(t);
  ui.d.querySelector("#decision-note").value = "Approved for this demo";
  ui.click("[data-tab=activity]");
  ui.click("[data-tab=memory]");
  assert.equal(
    ui.d.querySelector("#decision-note").value,
    "Approved for this demo",
  );
  ui.submit("#decision-form", "[value=accepted]");
  ui.submit("#decision-form", "[value=accepted]");
  await until(() => ui.d.querySelector(".decision-result"));
  assert.match(ui.text(".decision-result"), /Approved · procedure saved/);
  assert.match(ui.text(".decision-result"), /Approved for this demo/);
  assert.equal(ui.calls.filter((c) => c.path?.endsWith("/decision")).length, 1);
  assert.equal(ui.d.querySelector("#decision-form"), null);
  assert.match(ui.text("#record-body"), /procedural/);
  assert.deepEqual(ui.errors, []);
});

test("rejection records feedback without a procedural memory", async (t) => {
  const ui = await mount(t);
  ui.submit("#decision-form", "[value=rejected]");
  await until(() => ui.d.querySelector(".decision-result"));
  assert.match(ui.text(".decision-result"), /Rejected · no new procedure/);
  assert.equal(ui.database.get("inc_one").memories.length, 2);
  assert.deepEqual(ui.errors, []);
});

test("scenario dialog submits once and selects the new result", async (t) => {
  const ui = await mount(t);
  ui.click("[data-action=new]");
  assert.ok(ui.d.querySelector("#incident-dialog").open);
  ui.click("[data-scenario=token]");
  assert.match(ui.d.querySelector("#description").value, /exposed admin token/);
  ui.submit("#incident-form");
  ui.submit("#incident-form");
  await until(() => ui.text(".case-id").includes("inc_created"));
  assert.equal(
    ui.calls.filter((c) => c.path === "/api/incidents/run").length,
    1,
  );
  assert.equal(ui.d.querySelector("#incident-dialog").open, false);
  assert.match(ui.w.location.search, /incident=inc_created/);
  assert.deepEqual(ui.errors, []);
});

test("failed decisions preserve the note and restore usable buttons", async (t) => {
  const ui = await mount(t, {
    intercept: (c) =>
      c.path.endsWith("/decision")
        ? { status: 400, data: { error: "Decision conflict" } }
        : null,
  });
  ui.d.querySelector("#decision-note").value = "Keep this note";
  ui.submit("#decision-form", "[value=accepted]");
  await until(() => ui.text("#message").includes("Decision conflict"));
  assert.equal(ui.d.querySelector("[value=accepted]").disabled, false);
  assert.equal(ui.d.querySelector("#decision-note").value, "Keep this note");
  assert.deepEqual(ui.errors, []);
});

test("references show an explanation, never approval controls", async (t) => {
  const ui = await mount(t, {
    records: [
      incident("inc_ref", {
        final_action: "Historical response recorded",
        handoffs: [],
        audit: [],
        retrieved_memories: [],
      }),
    ],
  });
  assert.ok(ui.d.querySelector(".reference-note"));
  assert.equal(ui.d.querySelector("#decision-form"), null);
  ui.click("[data-action=use-case]");
  assert.match(ui.d.querySelector("#description").value, /Repeated admin/);
  assert.deepEqual(ui.errors, []);
});

test("empty workspace has a useful empty state; a named workspace can be opened", async (t) => {
  const ui = await mount(t, { records: [] });
  assert.match(ui.text("#incident-detail"), /Your next investigation/);
  const select = ui.d.querySelector("#tenant");
  select.value = "__new__";
  select.dispatchEvent(new ui.w.Event("change"));
  assert.ok(ui.d.querySelector("#tenant-dialog").open);
  ui.d.querySelector("#tenant-name").value = "Hackathon fresh";
  ui.submit("#tenant-form");
  await until(() => ui.w.location.search.includes("Hackathon"));
  assert.equal(ui.d.querySelector("#tenant").value, "Hackathon fresh");
  assert.equal(ui.calls.filter((c) => c.body).length, 0);
});

test("tenant switches discard a late incident response from the old workspace", async (t) => {
  let release;
  const ui = await mount(t, {
    records: [incident(), incident("inc_slow")],
    url: "http://mesh.test/?tenant=Acme+Corp&incident=inc_one",
    intercept: (c) =>
      c.path === "/api/incidents/inc_slow"
        ? new Promise((resolve) => {
            release = resolve;
          })
        : null,
  });
  ui.click("[data-action=open][data-id=inc_slow]");
  await until(() => release);
  const select = ui.d.querySelector("#tenant");
  select.value = "Globex";
  select.dispatchEvent(new ui.w.Event("change"));
  release({ data: incident("inc_slow") });
  await until(() => ui.text("#incident-list").includes("No history"));
  assert.doesNotMatch(ui.text("#incident-detail"), /Repeated admin/);
  assert.equal(ui.d.querySelector("#decision-form"), null);
  assert.deepEqual(ui.errors, []);
});

test("untrusted incident and memory text are rendered as text, not HTML", async (t) => {
  const attack = '<img src=x onerror="window.attacked=true">';
  const item = incident("inc_one", { description: attack });
  item.memories[0].content = attack;
  const ui = await mount(t, { records: [item] });
  assert.match(ui.text(".case-description"), /<img/);
  assert.equal(ui.d.querySelector("#incident-detail img"), null);
  assert.equal(ui.w.attacked, undefined);
  assert.deepEqual(ui.errors, []);
});

test("memory library filters by type, searches and paginates without writes", async (t) => {
  const item = incident();
  item.memories = Array.from({ length: 28 }, (_, n) =>
    memory("m" + n, n === 0 ? "procedural" : "episodic"),
  );
  const ui = await mount(t, { records: [item] });
  ui.click("[data-view=library]");
  await until(
    () => ui.d.querySelectorAll("#library-grid .memory-card").length === 24,
  );
  assert.equal(ui.d.querySelector("#load-more").hidden, false);
  ui.click("#load-more");
  await until(
    () => ui.d.querySelectorAll("#library-grid .memory-card").length === 28,
  );
  assert.equal(ui.d.querySelector("#load-more").hidden, true);
  const select = ui.d.querySelector("#memory-type");
  select.value = "procedural";
  select.dispatchEvent(new ui.w.Event("change"));
  await until(
    () => ui.d.querySelectorAll("#library-grid .memory-card").length === 1,
  );
  assert.match(ui.text("#library-grid"), /procedural/);
  ui.d.querySelector("#memory-search").value = "not present";
  select.dispatchEvent(new ui.w.Event("change"));
  await until(() => ui.text("#library-grid").includes("No memories found"));
  assert.equal(ui.calls.filter((c) => c.body).length, 0);
  assert.deepEqual(ui.errors, []);
});

test("source links open the exact incident and export uses its real ID", async (t) => {
  const ui = await mount(t, {
    records: [incident("inc_source"), incident()],
    url: "http://mesh.test/?incident=inc_one",
  });
  ui.click("[data-action=open-source][data-id=inc_source]");
  await until(() => ui.text(".case-id").includes("inc_source"));
  ui.click("[data-action=export]");
  assert.ok(ui.calls.some((c) => c.download === "inc_source.json"));
  assert.deepEqual(ui.errors, []);
});

test("missing saved incident shows a recoverable error, not a null-element crash", async (t) => {
  const ui = await mount(t, {
    records: [],
    url: "http://mesh.test/?incident=missing",
  });
  await until(() => ui.text("#message").includes("incident not found"));
  assert.equal(ui.d.querySelector("#decision-form"), null);
  assert.match(ui.text("#incident-detail"), /Start investigation/);
  assert.deepEqual(ui.errors, []);
});
