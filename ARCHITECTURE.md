# How the project works

## 1. The idea in plain language

Imagine three specialists working on a suspicious-login report:

- **Triage:** What kind of incident is this, and how urgent is it?
- **Investigator:** What do we already know, and what pattern should we record?
- **Response:** What action should an analyst consider?

They leave records in a shared FalkorDB graph instead of losing context when a page closes. The next investigation searches and traverses those records. Analyst approvals and rejections also persist.

This demonstrates memory and coordination with deterministic workers. It is not a live intrusion detector, autonomous remediation system, or LLM-powered reasoning engine.

## 2. Request flow

```text
Browser: index.html + app.css + app.js
    │
    ├─ GET /api/dashboard             Read counts and recent cases
    ├─ GET /api/incidents/{id}        Inspect one saved case
    ├─ GET /api/memories              Browse tenant memory
    │
    ├─ POST /api/incidents/run
    │       └─ server.py → service.run_workflow()
    │              ├─ create incident + audit event
    │              ├─ Triage → Investigator handoff
    │              ├─ retrieve memory; record findings
    │              ├─ Investigator → Response handoff
    │              └─ save recommendation
    │
    └─ POST /api/incidents/{id}/decision
            ├─ save analyst feedback + update handoff
            └─ if accepted: save procedural memory
                              │
                         FalkorDB
                              │
                   future investigation recalls it
```

The POST completes three sequential worker functions before returning saved state. The UI displays the result, not a live progress stream. Reading, refreshing, searching, switching tabs, and exporting do not create incidents.

## 3. Walk through one investigation

Input: “Repeated login failures for an admin from a suspicious IP and shared device.”

### A. Create the case

`run_workflow()` normalizes tenant, user, and description, then calls `create_incident()`. It writes a case with a generated `inc_…` ID and an `incident_created` audit event.

### B. Triage

`_run_triage()` applies keyword rules. Privileged terms include admin, root, privileged, and token. A privileged report containing token, successful, or impossible travel is marked critical.

It saves classification, records an audit event, creates the Triage → Investigator handoff, and claims that handoff. This is a demo classifier, not trained threat detection.

### C. Recall and record knowledge

`_run_investigator()` searches using the description plus fixed authentication keywords:

1. Search within this tenant and user.
2. If fewer than three matches exist, search across users in the same tenant.
3. Tokenize query/content into lowercase word sets.
4. Rank by word overlap, stored confidence, then update time; return up to eight results.

Retrieved IDs are saved in the handoff. They let the UI label recalled memories and open source incidents.

The worker writes two records:

- **Episodic:** the submitted incident description.
- **Semantic:** a rule-generated pattern and evidence labels.

It completes its handoff, then creates/claims the Investigator → Response handoff.

Current evidence labels include hardcoded authentication indicators; they are not verified observations from logs. Confidence and useful-memory counts are heuristics.

### D. Propose a response

`_run_response()` chooses one of two demo recommendations using keywords, saves it, and completes the second handoff.

The legacy database status becomes `resolved`: this means response generation finished, not that a threat was contained. The UI uses **Needs review**, **Approved**, or **Rejected** to communicate the human decision.

### E. Analyst feedback becomes memory

`record_response_decision()` verifies the selected tenant owns the incident and a recommendation exists.

Acceptance creates a **procedural** memory describing the approved demo response. Rejection records feedback without adding a procedure. Neither invokes a security tool.

The handoff update, decision audit event, and new procedure commit in one transaction. `BEGIN IMMEDIATE` serializes competing decisions across service instances. Repeating the same decision returns the existing result; conflicting decisions are rejected.

Approval is not proof that a response worked in reality.

### F. The next investigation

It can retrieve the first case's episodic/semantic records and approved procedure. Follow recalled source links to verify reuse.

This is the project’s central feature: **knowledge survives and can be reused with provenance**.

## 4. File responsibilities

| File | Responsibility |
| --- | --- |
| `start.py` | Launcher and compatibility imports |
| `memory_mesh/config.py` | Paths, host/port, limits, validation |
| `memory_mesh/service.py` | FalkorDB graph writes/queries, explicit SQLite test backend, workers, memory, decisions, metrics |
| `memory_mesh/server.py` | HTTP routing, parsing, errors, assets |
| `memory_mesh/ui.py` | HTML loader and static-file allowlist |
| `memory_mesh/static/index.html` | Layout, navigation, forms, explanatory guide |
| `memory_mesh/static/app.css` | Design tokens, components, responsive layout |
| `memory_mesh/static/app.js` | Browser state, API requests, safe rendering |
| `tests/` | Service, HTTP, and DOM regression tests |

There is no frontend build step. Python's standard library runs the app. Node/jsdom are optional development-only testing dependencies.

## 5. Data model

| Graph label | Stores | Relationship |
| --- | --- | --- |
| `Incident` | Tenant/user, description, classification, and case identity | Parent case |
| `Memory` | Type, content, author, confidence, timestamps | `(:Memory)-[:DERIVED_FROM]->(:Incident)` |
| `Handoff` | From/to agents, task, findings JSON, lifecycle timestamps | `(:Handoff)-[:FOR_INCIDENT]->(:Incident)` |
| `Audit` | Actor, event, detail JSON, timestamp | `(:Audit)-[:ABOUT]->(:Incident)` |
| `Decision` | One immutable analyst decision per tenant/case | `(:Decision)-[:ABOUT]->(:Incident)` |
| `Agent` | Tenant-scoped worker identity | `(:Agent)-[:ASSIGNED/CREATED]->(:Handoff)` |

Production graph queries always constrain `tenant_id`. FalkorDB is the production source of truth: incident state, handoffs, audits, approvals, dashboard counts, history, investigator retrieval, and the memory library all read from graph nodes. SQLite is initialized only when a test explicitly constructs `MemoryService(path)`. If FalkorDB is unavailable, `MemoryService()` raises during startup rather than silently using SQLite.

The production graph is intentionally tenant-scoped at the node level. Every `Incident`, `Memory`, `Handoff`, `Audit`, and `Agent` carries `tenant_id`; relationship traversals also verify both endpoint tenants. Handoffs use both `FOR_INCIDENT` and `HAS_HANDOFF` edges, while recalled memories gain `RECALLED_IN` edges to the investigator handoff. Retrieval returns an explainable score based on keyword overlap, memory type priority, confidence, and same-user relevance.

Representative required Cypher:

```cypher
MATCH (m:Memory)-[:DERIVED_FROM]->(i:Incident)
WHERE m.tenant_id = $tenant_id AND i.tenant_id = $tenant_id
RETURN m, i
```

```cypher
MATCH (m:Memory)
WHERE m.tenant_id = $tenant_id
  AND toLower(m.content) CONTAINS toLower($query)
RETURN m
ORDER BY m.created_at DESC
SKIP $offset LIMIT $limit
```

Handoff lifecycle: `pending → claimed → completed`. These are persisted coordination records, not network messages to independent machines. Methods also pass return dictionaries directly in one process.

On an empty database, `seed_history()` creates ten Acme Corp and two Globex examples. Their final action is `Historical response recorded`; they have no live worker chain. The inbox labels them **Reference** and excludes them from **Investigations run**.

Existing historical procedures remain intact. They are not retroactive proof of analyst approval; inspect the source/audit trail.

## 6. Memory versus conversation history

| Type | Question | Example |
| --- | --- | --- |
| Episodic | What happened? | Repeated failed logins on an admin account |
| Semantic | What pattern was recorded? | Shared devices may connect credential attacks |
| Procedural | What response was recorded? | An analyst approved credential revocation |

Cases, notes, and audit events persist across sessions and server restarts. There is **no general-purpose conversational message history**. Chat would need a session/message model; an audit log is not a substitute.

“Mesh” means shared knowledge with provenance links. It is not a graph database or a distributed agent network.

## 7. Tenant and user boundaries

Memory/incident service queries include `tenant_id`. Source validation prevents linking new memories to another tenant's case. The library scopes searches to the selected tenant.

User IDs identify authors and initially narrow retrieval. The workflow intentionally falls back to other users' memories in the same tenant: shared team memory, not strict per-user privacy.

The dropdown directly supplies tenant identity. Anyone with API access can choose another tenant. Database filtering exists; authentication and production authorization do not.

## 8. Read the numbers honestly

| Display | Meaning |
| --- | --- |
| Investigations run | Non-reference cases, including incomplete cases |
| Saved memories | Stored memory rows for this tenant |
| Awaiting your review | Non-reference cases with a final action but no decision |
| Completed handoffs | Completed rows / all handoff rows |
| Recalled memories | Existing records linked by investigator memory IDs |
| Confidence | Assigned heuristic, not calibrated accuracy |
| Modeled resolution | Fixed 90 or 30 seconds, not elapsed runtime |

The 30-second rule checks for another case with handoffs for the same tenant/user. It does **not** require successful containment, a completed prior workflow, or verified similarity. Even an unrelated second case can receive 30 seconds.

Legacy JSON fields such as `similar_incidents`, `duplicate_steps`, and `useful_memories` are rule-based proxies, not measured impact. The new dashboard avoids presenting them as performance proof.

## 9. Frontend state and errors

`app.js` keeps tenant, incident, view, filters, and request counters in one state object.

- Tenant and case IDs appear in the URL, allowing reload to restore selection.
- Dashboard load selects a recent investigation when available.
- Tenant changes clear old data immediately. Request counters ignore late responses from earlier selections.
- Writes disable competing writes. POST requests are not auto-retried because case creation is not idempotent.
- Tabs preserve unsent analyst notes.
- Errors appear in the form or workspace banner.
- Server-provided text is HTML-escaped.
- Reference and incomplete cases explain why no approval is available.
- Library reads are paginated. Inbox search covers the latest 40 loaded cases, not all history.
- JSON exports include a simulation notice.

The server only serves allowlisted assets, not the database or arbitrary project files.

## 10. Concurrency and persistence limits

The explicit SQLite backend is used only by isolated unit tests. Production persistence and retrieval use FalkorDB; a process-local lock protects local write sections and the graph client provides the shared graph service.

This suits a local demo, not high-concurrency production. The full workflow spans multiple graph writes, so interruptions can leave partial records. There is no durable job queue, automatic recovery, distributed ownership, or end-to-end exactly-once guarantee.

Tests cover filtering, persistence after service reinitialization, repeat recall, atomic/idempotent approvals, concurrent decisions, API validation, static routes, stale UI requests, and interactions. They do not prove throughput or real-browser visual correctness.

## 11. Future real-application integration

Start with read-only structured events from a test application, preserving event IDs as evidence. Add authenticated tenant identity and retrieval-quality tests. Independent workers need durable jobs and atomic ownership; larger deployments need a suitable database and load testing.

Model/tool adapters can replace deterministic workers while preserving memory and auditable handoffs. Keep mitigation execution separate from recommendations and record actual execution results rather than treating approval as success.

For the hackathon, demonstrate the working memory loop and traceable analyst decision. Present production integrations as future work.
