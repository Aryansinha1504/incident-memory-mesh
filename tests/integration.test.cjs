/* Real HTTP + DOM integration, with a disposable database and an OS-assigned port. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { mkdtemp, rm } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const path = require("node:path");
const { createInterface } = require("node:readline");
const { once } = require("node:events");
const { JSDOM, VirtualConsole } = require("jsdom");

async function waitFor(predicate) {
  for (let n = 0; n < 300; n++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(
    predicate(),
    "Real API/DOM integration did not reach expected state",
  );
}

test(
  "actual API drives run → approval → recall → rejection → reload",
  { timeout: 15000 },
  async (t) => {
    const directory = await mkdtemp(
      path.join(tmpdir(), "memory-mesh-integration-"),
    );
    const code = [
      "from http.server import ThreadingHTTPServer",
      "from memory_mesh.server import Handler",
      "server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)",
      "print('READY http://127.0.0.1:' + str(server.server_port), flush=True)",
      "server.serve_forever()",
    ].join("\n");
    const child = spawn("python3", ["-u", "-c", code], {
      cwd: path.join(__dirname, ".."),
      env: {
        ...process.env,
        MEMORY_MESH_DB: path.join(directory, "integration.db"),
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (data) => {
      stderr += data;
    });
    t.after(async () => {
      if (child.exitCode === null && child.signalCode === null) {
        const exited = once(child, "exit");
        child.kill("SIGTERM");
        await exited;
      }
      await rm(directory, { recursive: true, force: true });
    });
    const lines = createInterface({ input: child.stdout });
    const base = await new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("Server startup timed out: " + stderr)),
        5000,
      );
      lines.on("line", (line) => {
        if (line.startsWith("READY ")) {
          clearTimeout(timeout);
          resolve(line.slice(6));
        }
      });
      child.once("error", (error) => {
        clearTimeout(timeout);
        reject(error);
      });
      child.once("exit", (status) => {
        clearTimeout(timeout);
        reject(new Error("Server exited " + status + ": " + stderr));
      });
    });
    t.after(() => lines.close());
    const html = await (await fetch(base)).text();
    const js = await (await fetch(base + "/static/app.js")).text();
    const errors = [];
    function page(url) {
      const virtualConsole = new VirtualConsole();
      virtualConsole.on("jsdomError", (error) => errors.push(error.message));
      const dom = new JSDOM(html, {
        url,
        runScripts: "outside-only",
        virtualConsole,
      });
      t.after(() => dom.window.close());
      const w = dom.window,
        d = w.document;
      w.fetch = (url, options) => fetch(new URL(url, base), options);
      w.HTMLDialogElement.prototype.showModal = function () {
        this.open = true;
      };
      w.HTMLDialogElement.prototype.close = function () {
        this.open = false;
      };
      w.eval(js);
      return {
        w,
        d,
        click: (selector) => d.querySelector(selector).click(),
        submit: (selector, submitter) =>
          d.querySelector(selector).dispatchEvent(
            new w.SubmitEvent("submit", {
              bubbles: true,
              cancelable: true,
              submitter: submitter ? d.querySelector(submitter) : null,
            }),
          ),
      };
    }
    const firstPage = page(base + "/?tenant=Integration");
    await waitFor(() =>
      firstPage.d
        .querySelector("#connection")
        .textContent.includes("Connected"),
    );
    firstPage.click("[data-action=new]");
    firstPage.submit("#incident-form");
    await waitFor(
      () =>
        firstPage.d.querySelector("#decision-form") &&
        !firstPage.d.querySelector("#submit-incident").disabled,
    );
    const firstID = new URL(firstPage.w.location.href).searchParams.get(
      "incident",
    );
    assert.ok(firstID.startsWith("inc_"));
    assert.equal(firstPage.d.querySelectorAll(".memory-card").length, 2);
    firstPage.d.querySelector("#decision-note").value =
      "Reviewed for our integration test";
    firstPage.submit("#decision-form", "[value=accepted]");
    await waitFor(
      () =>
        firstPage.d.querySelector(".decision-result") &&
        !firstPage.d.querySelector("#submit-incident").disabled,
    );
    assert.equal(firstPage.d.querySelectorAll(".memory-card").length, 3);
    assert.match(
      firstPage.d.querySelector(".decision-result").textContent,
      /Approved/,
    );
    firstPage.click("[data-action=new]");
    firstPage.submit("#incident-form");
    await waitFor(
      () =>
        firstPage.d.querySelector("#decision-form") &&
        !firstPage.d.querySelector("#submit-incident").disabled,
    );
    const secondID = new URL(firstPage.w.location.href).searchParams.get(
      "incident",
    );
    assert.notEqual(firstID, secondID);
    firstPage.click("[data-memory-filter=recalled]");
    assert.equal(firstPage.d.querySelectorAll(".memory-card").length, 3);
    assert.ok(
      [...firstPage.d.querySelectorAll(".source-link")].every(
        (button) => button.dataset.id === firstID,
      ),
    );
    firstPage.submit("#decision-form", "[value=rejected]");
    await waitFor(
      () =>
        firstPage.d.querySelector(".decision-result") &&
        !firstPage.d.querySelector("#submit-incident").disabled,
    );
    assert.match(
      firstPage.d.querySelector(".decision-result").textContent,
      /Rejected/,
    );
    const reloaded = page(firstPage.w.location.href);
    await waitFor(() => reloaded.d.querySelector(".decision-result"));
    assert.match(
      reloaded.d.querySelector(".decision-result").textContent,
      /Rejected/,
    );
    assert.match(
      reloaded.d.querySelector(".case-id").textContent,
      new RegExp(secondID),
    );
    reloaded.click("[data-view=library]");
    await waitFor(
      () =>
        reloaded.d.querySelectorAll("#library-grid .memory-card").length === 5,
    );
    const dashboard = await (
      await fetch(base + "/api/dashboard?tenant_id=Integration")
    ).json();
    assert.equal(dashboard.metrics.investigations, 2);
    assert.equal(dashboard.metrics.pending_review, 0);
    assert.equal(dashboard.memory_counts.procedural, 1);
    assert.deepEqual(errors, []);
  },
);
