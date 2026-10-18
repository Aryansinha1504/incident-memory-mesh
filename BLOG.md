# The Second Investigation Should Start Smarter: Building Agent Memory with FalkorDB

*A technical walkthrough of Mesh, a project for the Agent Memory and Coordination track*

An agent can give a convincing answer and still be a poor teammate: next session, it may have forgotten the case, the reasoning, and the procedure a human approved. That makes every investigation feel like the first one.

Mesh explores a more practical question: **what would it take for a team of agents to remember together—and let a human trace what they remember?**

Mesh is a local demonstration built for the **Agent Memory and Coordination** track. It models a small incident-response team with three specialist workers. The workers are deterministic Python functions, and their shared operational memory lives in FalkorDB.

## A memory loop, not a one-shot answer

The demonstration starts with a synthetic report:

> Repeated login failures for an admin from a suspicious IP and shared device.

The triage worker uses keyword rules to classify the report, records its decision, and creates a handoff for an investigator. The investigator searches memories scoped to the selected tenant and first tries the same analyst identity. If that search returns too few matches, it broadens to other users in that tenant so the team can share operational memory.

The investigator then records two different kinds of knowledge: an episodic memory describing the report and a semantic memory describing the pattern inferred by the demo rules. It completes its handoff and creates one for the response worker. The response worker suggests an action for an analyst to review.

If the analyst approves the recommendation, Mesh stores a procedural memory linked to its source incident. When a similar report arrives later, the investigator can retrieve that procedure and the earlier case that produced it. The important demonstration is the loop: experience becomes memory, a person reviews a recommendation, and a later task can reuse the recorded knowledge.

```text
Incident → Triage → Investigator handoff → Memory recall
    → Response recommendation → Analyst decision
    → Approved procedure → Recall in a later incident
```

## The graph is the evidence

Mesh makes that loop inspectable in the investigation workspace. The
**Provenance graph** follows one focused path instead of showing an unreadable
database dump:

```text
Current incident
      ↓
Persisted agent handoffs
      ↓
Recalled or newly created memory
      ↓
Source incident + analyst decision
```

Each memory can be followed back to the incident that produced it. Each
handoff shows which specialist owned the work. The graph answers not only
“what did the system remember?” but also “why is this memory connected to this
investigation?”

## Why use a graph for this?

A collection of notes can tell us what was remembered. A graph can also show how each record relates to the work that produced or reused it.

Mesh models that history with tenant-scoped nodes:

| Node | What it records |
| --- | --- |
| `Incident` | The report, classification, status, and response summary |
| `Memory` | Episodic, semantic, or procedural knowledge, with confidence and timestamps |
| `Agent` | A specialist worker participating in a handoff |
| `Handoff` | The assigned task, findings, status, and lifecycle timestamps |
| `Audit` | An event and its actor, detail, and time |
| `Decision` | The analyst’s accepted or rejected response decision |

Edges make the provenance inspectable:

```text
(Memory)-[:DERIVED_FROM]->(Incident)
(Incident)-[:HAS_HANDOFF]->(Handoff)-[:FOR_INCIDENT]->(Incident)
(Agent)-[:CREATED]->(Handoff)
(Agent)-[:ASSIGNED]->(Handoff)
(Memory)-[:RECALLED_IN]->(Handoff)
(Audit)-[:ABOUT]->(Incident)
(Decision)-[:ABOUT]->(Incident)
```

For example, recall can be followed from the current incident to its investigator handoff, from that handoff to a recalled memory, and from the memory back to its source incident. That relationship is the difference between “the system returned a note” and “the system can show which prior case produced knowledge used here.”

## FalkorDB is part of the workflow

FalkorDB is the production source of truth in Mesh. The application reads and writes incidents, memories, handoffs, audits, decisions, dashboard data, and history through graph queries. Startup requires FalkorDB; an unavailable graph produces a clear startup error. SQLite is only used by tests that explicitly construct the test backend.

The memory library applies tenant filtering, text filtering, and pagination in Cypher. Investigator retrieval also queries FalkorDB for tenant-matched memories and their `DERIVED_FROM` source. The service then ranks those returned records in Python. A representative provenance query for inspecting a completed recall is:

```cypher
MATCH (current:Incident {id: $incident_id, tenant_id: $tenant_id})
      -[:HAS_HANDOFF]->(h:Handoff {to_agent: 'investigator'})
      <-[:RECALLED_IN]-(m:Memory)-[:DERIVED_FROM]->(source:Incident)
WHERE h.tenant_id = $tenant_id
  AND m.tenant_id = $tenant_id
  AND source.tenant_id = $tenant_id
RETURN m, h, source
```

The investigator currently uses keyword overlap rather than embeddings. It ranks matching records using shared words, a small priority for procedural and semantic memories, stored confidence, and a small same-user relevance boost. The result includes a score and human-readable reasons, such as shared keywords, memory type, and same-user relevance. The graph finds and connects the candidate memories; the score is a demo heuristic, not a calibrated measure of correctness.

## Coordination records that survive a refresh

The workers run sequentially in one process. Their handoffs are still persisted as graph records with a simple lifecycle:

```text
pending → claimed → completed
```

That lets someone inspect which worker received a task, what findings were passed, and when the handoff changed state. The `RECALLED_IN` edge records which memories the investigator used. Audits record important events, and the analyst decision is represented as its own graph node linked to the incident. Together, those links describe a small knowledge trail across sessions rather than a transcript of private chat messages.

This is durable coordination history, not a distributed agent platform. There is no background job queue, independent worker deployment, or automatic recovery if the process stops halfway through a workflow. Those are meaningful next steps for a production system, not capabilities this demo claims today.

## Keeping the demo honest

Mesh is a prototype for exploring agent memory, not an intrusion detection product. Its workers use deterministic rules. They do not call an LLM, inspect live security telemetry, or execute mitigations. The sample incidents are synthetic. An approval means an analyst accepted a simulated recommendation; it does not prove the response worked in a real environment.

Tenant filters are applied to graph queries, and the live workflow was exercised against FalkorDB, including a second investigation that recalled memories and an isolation check using another tenant. That check demonstrates query scoping in this local app; it is not a security audit. The caller-selected tenant value is not authentication, so this demonstration does not provide production access control. The project also stores investigation history, not general-purpose conversational transcripts.

These boundaries are part of the design story. Persistent memory should be inspectable, scoped, and attributable. A system should distinguish a stored recommendation from an action that was actually executed.

## Try the memory loop

Start the local graph and application:

```bash
docker compose up -d
python3 start.py
```

Then open `http://127.0.0.1:8000` and:

1. Create a fresh workspace so the demonstration starts without prior cases.
2. Run the admin-login scenario and inspect the agent handoffs and saved memories.
3. Approve the simulated recommendation to save a procedural memory.
4. Run the scenario again in the same workspace and inspect the recalled memories and their source links.
5. Refresh the page, then switch to another workspace to see persistence and tenant-scoped results.

The second run is the heart of the demo. It makes the project’s central question observable: can an agent team carry forward useful experience, and can a person trace that experience back to its source?

## Engineering tradeoffs

The current design favors a traceable end-to-end memory loop over distributed execution. One Python process runs each worker in sequence, while FalkorDB persists the graph state. That makes the handoff and recall behavior easy to inspect in a local demo. It does not provide a durable queue, atomic multi-step workflow, concurrent claim protection across workers, or automatic recovery after a crash. The production code also deliberately refuses to start without FalkorDB; SQLite is only constructed explicitly by isolated service tests.

This creates a useful next step: use durable jobs and atomic handoff claiming, then measure retrieval against a labeled synthetic evaluation set. At present, the score is transparent but heuristic, and no throughput or recall-accuracy benchmark is claimed.

## What comes next

The next engineering steps are to evaluate recall against a labeled set of synthetic cases, make handoff recovery robust to interrupted workflows, and add real authentication before connecting any multi-user environment. A future worker could use an LLM, but its memories and decisions should still carry provenance and remain reviewable.

Mesh takes a deliberately small step toward agents that remember: persist the work as a graph, make handoffs visible, and let the next investigation benefit from what the previous one learned.

## AI assistance disclosure

OpenAI Codex assisted with code generation and refactoring, interface and backend changes, automated tests, and documentation. This disclosure describes the assistance used; it does not assert that participant review, contribution, or hackathon eligibility requirements have been satisfied.
