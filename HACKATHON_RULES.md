# Graph Hacks — project constraints

Source: rules supplied by the participant in this conversation on October 18, 2026. This is a saved copy, not independent verification of current organizer announcements. Check organizer channels for changes before submitting.

Intended track: **Agent Memory and Coordination**.

## Implementation status

The production application requires FalkorDB at startup. Incident state, memories, handoffs, audits, analyst decisions, approvals, dashboards, history, investigator retrieval, and the memory library are graph-backed. SQLite is initialized only by the explicit backend used by isolated unit tests. Participant review and real FalkorDB end-to-end verification are still required before submission.

A proposed model to discuss—not an implemented schema—is:

- Tenant owns incidents, memories, and agent coordination records.
- Incident is linked to relevant entities and the memories it produced.
- Memory links back to its source incident.
- Handoff connects an incident and the responsible agents.
- Analyst decision links the recommendation to any approved procedure.

Graph-backed retrieval should follow these relationships to supply usable prior context. Verify the dependency by removing or disconnecting FalkorDB: essential investigation/recall must fail clearly rather than silently succeeding through SQLite.

## Submission checklist

- [x] FalkorDB is the primary graph database and essential to core behavior.
- [x] Graph schema and tenant-scoping approach are documented.
- [x] Required Cypher queries or graph algorithms are included and exercised.
- [ ] Public source-code repository is available.
- [ ] README includes working graph-backed setup instructions and graph data model.
- [ ] Demo video shows the working graph-backed product.
- [ ] Live deployment or complete reproducible local setup is available.
- [ ] Submission explicitly names Agent Memory and Coordination, and any other entered tracks.
- [ ] AI coding assistance is disclosed accurately.
- [ ] Participant can explain the graph model, queries, architecture, and decisions.
- [ ] Participant contribution and verification are meaningful and accurately described.
- [ ] Dataset permissions and restrictions are checked.
- [ ] Team size, one-team membership, and coding-window eligibility are confirmed.
- [ ] Submission is completed before the supplied deadline: October 18, 2026, 11:59 PM IST.

AI assistance already used: OpenAI Codex assisted with code generation/refactoring, UI and backend changes, automated tests, and documentation. This is not a claim that participant review or contribution requirements are satisfied. The participant must review, understand, verify, and accurately describe their own contribution before submission.

## Full rules supplied by the participant

Registering for Graph Hacks means agreeing to these rules and the [WeMakeDevs Code of Conduct](https://www.wemakedevs.org/coc).

1. You can participate solo or in a team of up to 4 members. Each participant may only be part of one team.
2. Required technology: FalkorDB must be the primary graph database in your project, and it must support a central part of what the product does.
3. The graph has to do real work. A visualisation drawn on top of ordinary storage does not qualify: if the product still works once FalkorDB is taken out, the graph is decoration rather than the thing the product runs on.
4. Run FalkorDB however suits you. Docker, a self-hosted instance, and FalkorDB Cloud are all accepted, and you may use any language, framework, frontend, or deployment platform alongside it.
5. The challenge is open-ended within the three tracks: Agents That Act on Connected Data, Agent Memory and Coordination, and Company Brain. You may enter any or all of them, and you must name every track you are entering when you submit.
6. Your project must use data you are allowed to use: public, synthetic, or your own. Do not load private, login-protected, paywalled, personal, or otherwise restricted information into your graph.
7. The hackathon runs from October 15, 12:01 AM IST to October 18, 11:59 PM IST. Submissions close at the end of that window.
8. The main coding and design work must begin after the hackathon starts. You may discuss ideas, create notes, sketch the graph model, or prepare diagrams beforehand.
9. You may use frameworks, open-source libraries, public APIs, templates, third-party tools, and publicly available datasets and assets. The original work completed during the hackathon will be judged.
10. Every submission must include:

    - A public source-code repository
    - A README with setup instructions and your graph data model
    - The Cypher queries or graph algorithms the product depends on
    - A demo video showing the working project
    - A live deployment, or complete local setup instructions if it cannot be deployed
    - The track you are entering

11. AI coding assistants are allowed, but their use must be disclosed.
12. Participants must understand the submitted code and be able to explain the graph model, the queries, the project architecture, and the technical decisions behind them.
13. Projects that are entirely generated using AI without meaningful participant contribution, verification, or technical understanding may be rejected.
14. The prize pool is worth $20,000. The best project in each track wins a prize: an iPhone 18 Pro for Agents That Act on Connected Data, a PS5 with GTA VI for Agent Memory and Coordination, and an M6 Mac Mini for Company Brain. Every member of a winning team gets the prize.
15. Any intellectual property developed during the hackathon belongs to the participant or team that created it. Teams are encouraged to agree internally on ownership before submitting.
16. Treat participants, organisers, sponsors, speakers, judges, and community members with respect.
17. Harassment, discrimination, plagiarism, or attempts to manipulate the judging process will result in disqualification.
18. Failure to follow these rules or the WeMakeDevs Code of Conduct may result in disqualification.

Note: Graph Hacks is an independent developer hackathon organised by WeMakeDevs in collaboration with FalkorDB. These rules, the schedule, and the prize breakdown may be updated before the hackathon begins, and any change is announced through the WeMakeDevs and FalkorDB community channels.
