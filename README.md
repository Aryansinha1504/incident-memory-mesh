# Mesh — Incident Response Memory Mesh

Hackathon track: **Agent Memory and Coordination** (intended entry).

The production application requires FalkorDB for its essential memory and coordination workflow. SQLite is retained only for tests that explicitly construct `MemoryService(path)`.

A local demonstration of a security team that remembers. Three specialized Python workers investigate a reported authentication incident, share structured findings, and leave persistent knowledge for the next investigation.

The core idea is **shared operational memory**, not simply generating another incident. Trace a recalled memory to its source, inspect agent handoffs, and approve a response to save a procedure.

## Start the application

```bash
docker compose up -d
python3 start.py
```

Open <http://127.0.0.1:8000>. No API key, Python dependencies, frontend build, or npm install is needed to run the application.

If an older server is running, stop it with **Ctrl+C**, restart it with the command above, then refresh the page. Responses are served with caching disabled.

FalkorDB stores the production graph in `memory_mesh_v2` by default. Restarting does not reset it. Only an initially empty graph receives Acme Corp and Globex sample history.

## What the interface does

| Area | Purpose |
| --- | --- |
| Investigations | Create a case, search the recent inbox, inspect outputs, approve/reject a recommendation, export JSON |
| Provenance graph | Trace the current case through persisted handoffs, recalled/created memories, source incidents, and analyst decisions |
| Memory trail | Separate recalled context from newly saved knowledge; open source incidents |
| Activity log | Inspect timestamped events and analyst notes |
| Handoff JSON | Inspect structured records passed between workers |
| Memory library | Search/filter the tenant's full memory collection, with pagination |
| How it works | Understand the problem, implementation, and demo boundaries |
| Workspace switcher | Choose a tenant filter or open a fresh named demo workspace |

Selecting an incident, changing tabs, refreshing, and searching only read data. **Run investigation** creates a case. **Approve & save procedure** adds a procedural memory to the selected case; it does not create another incident or execute a mitigation.

## A three-minute hackathon demo

1. Choose **+ New demo workspace**. Use a unique name such as “Judges demo 1” so it starts with no history.
2. Click **New investigation**, choose **Admin account**, keep the analyst ID, and run it.
3. Show the three agent outputs and recommendation. In **Memory trail**, this first case has no recalled memories and two saved records: episodic and semantic.
4. Open **Provenance graph** to show the case → handoffs → memory path, then open **Memory trail** for the full records.
5. Add a note and click **Approve & save procedure**. A third, procedural memory appears. No token is actually disabled.
6. Run the same scenario again with the same analyst ID. Open **Provenance graph** and follow a source link back to the first case. This makes persistent context reuse visible.
7. Refresh to show persistence. Open **Memory library**, then switch to another fresh workspace to demonstrate tenant-scoped reads.
7. Optionally reject another case. Its feedback is recorded without saving a new procedure.

Suggested pitch:

> “Security teams repeatedly investigate similar signals. Mesh gives specialists a shared, persistent case memory: what happened, what pattern was found, and which response an analyst approved. Every handoff and recalled source is inspectable.”

## Real versus simulated

**Implemented:** FalkorDB persistence; Cypher-backed memory retrieval and library queries; three memory types; two durable handoffs per completed workflow; analyst notes; tenant-scoped queries; source links; history and export.

**Simulated:** workers are deterministic Python functions, not LLM calls or independent services. Input is a description, not live telemetry. Findings/confidence use demo rules. Security actions are never executed.

The `90s` / `30s` value is **modeled, not measured**. The code assigns 30 seconds when an earlier workflow with handoffs exists for the same tenant and user. It does not verify similarity for that rule. Do not present it as a benchmark or proven threefold improvement.

Main counters now show database counts instead of an unmeasured “zero leakage” claim. Tenant filtering is tested, but caller-selected tenant IDs are not authentication. Same-tenant memory can be shared across users. General chat history and production-scale concurrency are not implemented.

## Project structure

```text
start.py                         Launcher
memory_mesh/
  config.py                      Settings and input validation
  service.py                     Workflow, FalkorDB graph, test backend, decisions
  server.py                      HTTP routing and responses
  ui.py                          HTML loader / asset allowlist
  static/
    index.html                   Dashboard, dialogs, explanation
    app.css                      Responsive visual design
    app.js                       API calls, state, interactions
    favicon.svg                  Mesh mark
tests/
  test_service.py                Workflow/database regressions
  test_http.py                   HTTP tests on an ephemeral port
  ui.test.cjs                    DOM interaction regressions
  integration.test.cjs           Real API + DOM workflow check
ARCHITECTURE.md                   Detailed walkthrough
```

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the full explanation. Edit `app.css` for appearance, `app.js` for interactions, and `service.py` for worker behavior.

## Tests

Backend tests need only Python and use temporary databases:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Optional DOM tests need Node.js compatible with jsdom 27 (tested here with Node 26):

```bash
npm ci
npm test
```

DOM tests check interactions, not visual layout or a real browser. Tests never operate on your saved database.

## API examples

```bash
curl -X POST http://127.0.0.1:8000/api/incidents/run \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"Acme Corp","user_id":"admin-ops","description":"Repeated admin login failures from a suspicious IP and shared device."}'
```

Copy the actual `id` returned. Replace `ACTUAL_ID` below; the literal placeholder is not a case.

```bash
curl 'http://127.0.0.1:8000/api/incidents/ACTUAL_ID?tenant_id=Acme%20Corp'

curl -X POST http://127.0.0.1:8000/api/incidents/ACTUAL_ID/decision \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"Acme Corp","user_id":"admin-ops","decision":"accepted","note":"Reviewed for the demo."}'

curl 'http://127.0.0.1:8000/api/memories?tenant_id=Acme%20Corp&type=procedural&limit=24&offset=0'
```

Repeating the same decision is idempotent. Reversing a recorded decision is rejected; create a new case to reconsider the response.

## Configuration

```bash
MEMORY_MESH_DB=/absolute/path/to/demo.db MEMORY_MESH_PORT=8001 python3 start.py
```

Defaults: `MEMORY_MESH_HOST=127.0.0.1`, `MEMORY_MESH_PORT=8000`, `FALKORDB_HOST=127.0.0.1`, `FALKORDB_PORT=6380`, and graph `memory_mesh_v2`. Startup fails clearly if FalkorDB is unavailable; there is no silent SQLite fallback.
