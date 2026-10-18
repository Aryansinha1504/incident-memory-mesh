"""Persistent memory, coordination records, and the three-agent workflow."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import (
    DB_PATH,
    FALKORDB_HOST,
    FALKORDB_PORT,
    GRAPH_NAME,
    MAX_DESCRIPTION_LENGTH,
    MAX_ID_LENGTH,
    MAX_MEMORY_LENGTH,
    clean_text,
)

try:
    from falkordb import FalkorDB
except ImportError:  # pragma: no cover - exercised by startup configuration
    FalkorDB = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def words(value: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_/-]+", value.lower()) if len(w) > 2}


class MemoryService:
    """Persistent shared memory and coordination records with hard tenant scope."""

    def __init__(self, path: Path | None = None, *, backend: str | None = None):
        # A path is an explicit local SQLite test/projection backend. The normal
        # application constructor has no path and therefore requires FalkorDB.
        self.backend = backend or ("sqlite" if path is not None else "falkordb")
        if self.backend not in {"sqlite", "falkordb"}:
            raise ValueError("backend must be sqlite or falkordb")
        self.path = str(path or DB_PATH)
        self.lock = threading.RLock()
        self.graph = None
        if self.backend == "falkordb":
            if FalkorDB is None:
                raise RuntimeError("FalkorDB client is required; install requirements.txt")
            try:
                self.graph = FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT).select_graph(GRAPH_NAME)
                self._graph_query("RETURN 1 AS ready")
            except Exception as exc:
                raise RuntimeError(
                    f"FalkorDB is required at {FALKORDB_HOST}:{FALKORDB_PORT}; "
                    "start it with `docker compose up -d`"
                ) from exc
        if self.backend == "sqlite" and path is None:
            self.path = str(DB_PATH)
        # SQLite is deliberately initialized only for the explicit test backend.
        # A production startup must not create or consult a second source of truth.
        if self.backend == "sqlite":
            self._init_db()
        self.seed_history()

    def _graph_query(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self.graph is None:
            return []
        result = self.graph.query(query, params or {})
        # FalkorDB exposes headers as [type, name] pairs (for example
        # [1, "total"]); use the column name for stable dict results.
        headers = [item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else str(item)
                   for item in result.header]
        return [dict(zip(headers, row)) for row in result.result_set]

    def _graph_write_incident(self, incident: dict[str, Any]) -> None:
        self._graph_query(
            "MERGE (i:Incident {id: $id}) SET i.tenant_id=$tenant_id, i.user_id=$user_id, "
            "i.description=$description, i.severity=$severity, i.category=$category, "
            "i.status=$status, i.started_at=$started_at, i.resolved_at=$resolved_at, "
            "i.resolution_seconds=$resolution_seconds, i.duplicate_steps=$duplicate_steps, "
            "i.memories_retrieved=$memories_retrieved, i.useful_memories=$useful_memories, "
            "i.final_action=$final_action, i.created_at=$created_at",
            incident,
        )

    @staticmethod
    def _decode_json(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _rank_memory_rows(rows: list[dict[str, Any]], query: str, user_id: str | None) -> list[dict[str, Any]]:
        query_words = words(query)
        type_weight = {"procedural": 1.15, "semantic": 1.05, "episodic": 1.0}
        scored = []
        for row in rows:
            overlap = len(query_words & words(row.get("content", "")))
            lexical = overlap / max(1, len(query_words))
            user_boost = 0.08 if user_id and row.get("user_id") == user_id else 0.0
            row["relevance"] = round(lexical, 2)
            row["score"] = round(lexical * type_weight.get(row.get("type"), 1.0) + float(row.get("confidence") or 0) * 0.2 + user_boost, 3)
            row["recall_reason"] = [f"{overlap} shared keywords", f"{row.get('type')} memory priority"]
            if user_boost:
                row["recall_reason"].append("same analyst workspace")
            if overlap:
                scored.append(row)
        return sorted(scored, key=lambda item: (item["score"], item.get("updated_at", "")), reverse=True)

    def _graph_incident(self, tenant_id: str, incident_id: str) -> dict[str, Any]:
        rows = self._graph_query(
            "MATCH (i:Incident {id:$incident_id, tenant_id:$tenant_id}) "
            "RETURN i.id AS id, i.tenant_id AS tenant_id, i.user_id AS user_id, "
            "i.description AS description, i.severity AS severity, i.category AS category, "
            "i.status AS status, i.started_at AS started_at, i.resolved_at AS resolved_at, "
            "i.resolution_seconds AS resolution_seconds, i.duplicate_steps AS duplicate_steps, "
            "i.memories_retrieved AS memories_retrieved, i.useful_memories AS useful_memories, "
            "i.final_action AS final_action, i.created_at AS created_at",
            {"tenant_id": tenant_id, "incident_id": incident_id},
        )
        if not rows:
            raise KeyError("incident not found")
        return rows[0]

    def _graph_handoffs(self, tenant_id: str, incident_id: str) -> list[dict[str, Any]]:
        rows = self._graph_query(
            "MATCH (h:Handoff)-[:FOR_INCIDENT]->(i:Incident) "
            "WHERE h.tenant_id=$tenant_id AND i.tenant_id=$tenant_id AND i.id=$incident_id "
            "RETURN h.id AS id, h.tenant_id AS tenant_id, h.incident_id AS incident_id, "
            "h.from_agent AS from_agent, h.to_agent AS to_agent, h.task AS task, "
            "h.findings AS findings, h.status AS status, h.created_at AS created_at, "
            "h.claimed_at AS claimed_at, h.completed_at AS completed_at ORDER BY h.created_at",
            {"tenant_id": tenant_id, "incident_id": incident_id},
        )
        for row in rows:
            row["findings"] = self._decode_json(row.get("findings"))
        return rows

    def _graph_audit(self, tenant_id: str, incident_id: str) -> list[dict[str, Any]]:
        rows = self._graph_query(
            "MATCH (a:Audit)-[:ABOUT]->(i:Incident) "
            "WHERE a.tenant_id=$tenant_id AND i.tenant_id=$tenant_id AND i.id=$incident_id "
            "RETURN a.id AS id, a.tenant_id AS tenant_id, a.incident_id AS incident_id, "
            "a.agent AS agent, a.event AS event, a.detail AS detail, a.created_at AS created_at "
            "ORDER BY a.created_at",
            {"tenant_id": tenant_id, "incident_id": incident_id},
        )
        for row in rows:
            row["detail"] = self._decode_json(row.get("detail"))
        return rows

    def _graph_write_memory(self, memory: dict[str, Any]) -> None:
        self._graph_query(
            "MERGE (m:Memory {id: $id}) SET m.tenant_id=$tenant_id, m.user_id=$user_id, "
            "m.type=$type, m.content=$content, m.confidence=$confidence, "
            "m.created_at=$created_at, m.updated_at=$updated_at "
            "WITH m MATCH (i:Incident {id: $source_incident_id, tenant_id: $tenant_id}) "
            "MERGE (m)-[:DERIVED_FROM]->(i)",
            {**memory, "source_incident_id": memory.get("source_incident_id", "")},
        )

    def _graph_write_handoff(self, handoff: dict[str, Any]) -> None:
        self._graph_query(
            "MERGE (h:Handoff {id: $id}) SET h.tenant_id=$tenant_id, h.incident_id=$incident_id, "
            "h.from_agent=$from_agent, h.to_agent=$to_agent, h.task=$task, h.findings=$findings, "
            "h.status=$status, h.created_at=$created_at "
            "WITH h MATCH (i:Incident {id: $incident_id, tenant_id: $tenant_id}) "
            "MERGE (h)-[:FOR_INCIDENT]->(i) MERGE (i)-[:HAS_HANDOFF]->(h) "
            "MERGE (from:Agent {tenant_id:$tenant_id, name:$from_agent}) "
            "MERGE (to:Agent {tenant_id:$tenant_id, name:$to_agent}) "
            "MERGE (from)-[:CREATED]->(h) MERGE (to)-[:ASSIGNED]->(h)",
            handoff,
        )

    def _graph_link_recalled_memories(self, tenant_id: str, handoff_id: str, memory_ids: list[str]) -> None:
        for memory_id in dict.fromkeys(memory_ids):
            self._graph_query(
                "MATCH (m:Memory {id:$memory_id, tenant_id:$tenant_id}), "
                "(h:Handoff {id:$handoff_id, tenant_id:$tenant_id}) "
                "MERGE (m)-[:RECALLED_IN]->(h)",
                {"tenant_id": tenant_id, "handoff_id": handoff_id, "memory_id": memory_id},
            )

    def _graph_write_audit(self, detail: dict[str, Any], *, audit_id: str, tenant_id: str,
                           incident_id: str | None, agent: str, event: str, created_at: str) -> None:
        self._graph_query(
            "MERGE (a:Audit {id: $audit_id}) SET a.tenant_id=$tenant_id, a.incident_id=$incident_id, "
            "a.agent=$agent, a.event=$event, a.detail=$detail, a.created_at=$created_at "
            "WITH a MATCH (i:Incident {id: $incident_id, tenant_id: $tenant_id}) "
            "MERGE (a)-[:ABOUT]->(i)",
            {"audit_id": audit_id, "tenant_id": tenant_id, "incident_id": incident_id or "", "agent": agent,
             "event": event, "detail": json.dumps(detail), "created_at": created_at},
        )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    description TEXT NOT NULL, severity TEXT, category TEXT,
                    status TEXT NOT NULL DEFAULT 'open', started_at TEXT NOT NULL,
                    resolved_at TEXT, resolution_seconds INTEGER,
                    duplicate_steps INTEGER NOT NULL DEFAULT 0,
                    memories_retrieved INTEGER NOT NULL DEFAULT 0,
                    useful_memories INTEGER NOT NULL DEFAULT 0, final_action TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('episodic','semantic','procedural')),
                    content TEXT NOT NULL, source_incident_id TEXT, confidence REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_incident_id) REFERENCES incidents(id)
                );
                CREATE TABLE IF NOT EXISTS handoffs (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL,
                    from_agent TEXT NOT NULL, to_agent TEXT NOT NULL, task TEXT NOT NULL,
                    findings TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT,
                    FOREIGN KEY(incident_id) REFERENCES incidents(id)
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, incident_id TEXT,
                    agent TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memories_tenant_idx ON memories(tenant_id, type);
                CREATE INDEX IF NOT EXISTS handoffs_incident_idx ON handoffs(tenant_id, incident_id);
                CREATE INDEX IF NOT EXISTS audit_incident_idx ON audit_logs(tenant_id, incident_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def _audit(self, db: sqlite3.Connection, tenant_id: str, incident_id: str | None,
               agent: str, event: str, detail: dict[str, Any]) -> None:
        audit_id, timestamp = uid("audit"), now()
        db.execute("INSERT INTO audit_logs VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (audit_id, tenant_id, incident_id, agent, event, json.dumps(detail), timestamp))
        if self.backend == "falkordb":
            self._graph_write_audit(detail, audit_id=audit_id, tenant_id=tenant_id,
                                    incident_id=incident_id, agent=agent, event=event,
                                    created_at=timestamp)

    # Memory service API
    def store_memory(self, *, tenant_id: str, user_id: str, type: str, content: str,
                     source_incident_id: str | None = None, confidence: float = 0.75) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        user_id = clean_text("user_id", user_id)
        type = clean_text("type", type)
        content = clean_text("content", content, max_length=MAX_MEMORY_LENGTH)
        if type not in {"episodic", "semantic", "procedural"}:
            raise ValueError("type must be episodic, semantic, or procedural")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be a number") from exc
        if not math.isfinite(confidence):
            raise ValueError("confidence must be finite")
        if source_incident_id is not None:
            source_incident_id = clean_text("source_incident_id", source_incident_id)
        memory = (uid("mem"), tenant_id, user_id, type, content, source_incident_id,
                  max(0.0, min(1.0, confidence)), now(), now())
        if self.backend == "falkordb":
            if source_incident_id:
                self._graph_incident(tenant_id, source_incident_id)
            payload = dict(zip(("id", "tenant_id", "user_id", "type", "content", "source_incident_id", "confidence", "created_at", "updated_at"), memory))
            self._graph_write_memory(payload)
            rows = self._graph_query(
                "MATCH (m:Memory {id:$id, tenant_id:$tenant_id}) "
                "RETURN m.id AS id, m.tenant_id AS tenant_id, m.user_id AS user_id, m.type AS type, "
                "m.content AS content, m.source_incident_id AS source_incident_id, m.confidence AS confidence, "
                "m.created_at AS created_at, m.updated_at AS updated_at",
                {"id": memory[0], "tenant_id": tenant_id},
            )
            return rows[0] if rows else payload
        with self.lock, self.connect() as db:
            if source_incident_id and not db.execute(
                "SELECT 1 FROM incidents WHERE id = ? AND tenant_id = ?", (source_incident_id, tenant_id)
            ).fetchone():
                raise ValueError("source incident does not belong to tenant")
            db.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", memory)
            row = db.execute("SELECT * FROM memories WHERE id = ? AND tenant_id = ?", (memory[0], tenant_id)).fetchone()
            if self.backend == "falkordb":
                self._graph_write_memory(dict(row))
            return self._row(row) or {}

    def search_memory(self, *, tenant_id: str, query: str, user_id: str | None = None,
                      limit: int = 8) -> list[dict[str, Any]]:
        """Keyword retrieval. Every predicate includes tenant_id by design."""
        tenant_id = clean_text("tenant_id", tenant_id)
        query = clean_text("query", query, max_length=MAX_MEMORY_LENGTH)
        if user_id is not None:
            user_id = clean_text("user_id", user_id)
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if limit < 1:
            return []
        query_words = words(query)
        if not query_words:
            return []
        if self.backend == "falkordb":
            rows = self._graph_query(
                "MATCH (m:Memory) WHERE m.tenant_id=$tenant_id AND "
                "($user_id IS NULL OR m.user_id=$user_id) "
                "OPTIONAL MATCH (m)-[:DERIVED_FROM]->(i) "
                "RETURN m.id AS id, m.tenant_id AS tenant_id, m.user_id AS user_id, m.type AS type, "
                "m.content AS content, m.confidence AS confidence, m.created_at AS created_at, "
                "m.updated_at AS updated_at, coalesce(i.id, '') AS source_incident_id "
                "ORDER BY m.updated_at DESC",
                {"tenant_id": tenant_id, "user_id": user_id},
            )
        else:
            with self.connect() as db:
                rows = self._rows(db.execute(
                    "SELECT * FROM memories WHERE tenant_id = ? AND (? IS NULL OR user_id = ?)",
                    (tenant_id, user_id, user_id),
                ).fetchall())
        return self._rank_memory_rows(rows, query, user_id)[: max(1, min(limit, 50))]

    def update_memory(self, *, tenant_id: str, memory_id: str, content: str | None = None,
                      confidence: float | None = None) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        memory_id = clean_text("memory_id", memory_id)
        if content is not None:
            content = clean_text("content", content, max_length=MAX_MEMORY_LENGTH)
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise ValueError("confidence must be a number") from exc
            if not math.isfinite(confidence):
                raise ValueError("confidence must be finite")
            confidence = max(0.0, min(1.0, confidence))
        if self.backend == "falkordb":
            timestamp = now()
            rows = self._graph_query(
                "MATCH (m:Memory {id:$memory_id, tenant_id:$tenant_id}) "
                "SET m.content=coalesce($content,m.content), m.confidence=coalesce($confidence,m.confidence), "
                "m.updated_at=$updated_at RETURN m.id AS id, m.tenant_id AS tenant_id, m.user_id AS user_id, "
                "m.type AS type, m.content AS content, m.confidence AS confidence, m.created_at AS created_at, "
                "m.updated_at AS updated_at",
                {"tenant_id": tenant_id, "memory_id": memory_id, "content": content,
                 "confidence": confidence, "updated_at": timestamp},
            )
            if not rows:
                raise KeyError("memory not found")
            return rows[0]
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id = ? AND tenant_id = ?", (memory_id, tenant_id)).fetchone()
            if not row:
                raise KeyError("memory not found")
            db.execute("UPDATE memories SET content = COALESCE(?, content), confidence = COALESCE(?, confidence), updated_at = ? WHERE id = ? AND tenant_id = ?",
                       (content, confidence, now(), memory_id, tenant_id))
            return self._row(db.execute("SELECT * FROM memories WHERE id = ? AND tenant_id = ?", (memory_id, tenant_id)).fetchone()) or {}

    def list_memories_for_incident(self, *, tenant_id: str, incident_id: str) -> list[dict[str, Any]]:
        tenant_id = clean_text("tenant_id", tenant_id)
        incident_id = clean_text("incident_id", incident_id)
        if self.backend == "falkordb":
            return self._graph_query(
                "MATCH (m:Memory)-[:DERIVED_FROM]->(i:Incident) "
                "WHERE m.tenant_id=$tenant_id AND i.tenant_id=$tenant_id AND i.id=$incident_id "
                "RETURN m.id AS id, m.tenant_id AS tenant_id, m.user_id AS user_id, "
                "m.type AS type, m.content AS content, m.confidence AS confidence, "
                "m.created_at AS created_at, m.updated_at AS updated_at, i.id AS source_incident_id "
                "ORDER BY m.created_at", {"tenant_id": tenant_id, "incident_id": incident_id}
            )
        with self.connect() as db:
            return self._rows(db.execute("SELECT * FROM memories WHERE tenant_id = ? AND source_incident_id = ? ORDER BY created_at", (tenant_id, incident_id)).fetchall())

    def delete_memory(self, *, tenant_id: str, memory_id: str) -> bool:
        tenant_id = clean_text("tenant_id", tenant_id)
        memory_id = clean_text("memory_id", memory_id)
        if self.backend == "falkordb":
            rows = self._graph_query(
                "MATCH (m:Memory {id:$memory_id, tenant_id:$tenant_id}) DELETE m RETURN count(m) AS deleted",
                {"tenant_id": tenant_id, "memory_id": memory_id},
            )
            return bool(rows and rows[0].get("deleted"))
        with self.lock, self.connect() as db:
            result = db.execute("DELETE FROM memories WHERE id = ? AND tenant_id = ?", (memory_id, tenant_id))
            return result.rowcount == 1

    # Coordination service API
    def create_incident(self, *, tenant_id: str, user_id: str, description: str) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        user_id = clean_text("user_id", user_id)
        description = clean_text("description", description, max_length=MAX_DESCRIPTION_LENGTH)
        incident = (uid("inc"), tenant_id, user_id, description, None, None, "open", now(), None, None, 0, 0, 0, None, now())
        if self.backend == "falkordb":
            payload = dict(zip(("id", "tenant_id", "user_id", "description", "severity", "category", "status", "started_at", "resolved_at", "resolution_seconds", "duplicate_steps", "memories_retrieved", "useful_memories", "final_action", "created_at"), incident))
            self._graph_write_incident(payload)
            self._graph_write_audit({"description": description}, audit_id=uid("audit"), tenant_id=tenant_id, incident_id=incident[0], agent="system", event="incident_created", created_at=incident[-1])
            return self.get_incident_state(tenant_id=tenant_id, incident_id=incident[0])
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", incident)
            self._audit(db, tenant_id, incident[0], "system", "incident_created", {"description": description})
            if self.backend == "falkordb":
                self._graph_write_incident(dict(zip(("id", "tenant_id", "user_id", "description", "severity", "category", "status", "started_at", "resolved_at", "resolution_seconds", "duplicate_steps", "memories_retrieved", "useful_memories", "final_action", "created_at"), incident)))
        return self.get_incident_state(tenant_id=tenant_id, incident_id=incident[0])

    def create_handoff(self, *, tenant_id: str, incident_id: str, from_agent: str, to_agent: str,
                       task: str, findings: dict[str, Any] | None = None) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        incident_id = clean_text("incident_id", incident_id)
        from_agent = clean_text("from_agent", from_agent)
        to_agent = clean_text("to_agent", to_agent)
        task = clean_text("task", task, max_length=MAX_DESCRIPTION_LENGTH)
        if findings is not None and not isinstance(findings, dict):
            raise ValueError("findings must be an object")
        if self.backend == "falkordb":
            self._graph_incident(tenant_id, incident_id)
            handoff = {"id": uid("handoff"), "tenant_id": tenant_id, "incident_id": incident_id,
                       "from_agent": from_agent, "to_agent": to_agent, "task": task,
                       "findings": json.dumps(findings or {}), "status": "pending", "created_at": now(),
                       "claimed_at": None, "completed_at": None}
            self._graph_write_handoff(handoff)
            self._graph_write_audit({"to_agent": to_agent, "task": task}, audit_id=uid("audit"), tenant_id=tenant_id,
                                    incident_id=incident_id, agent=from_agent, event="handoff_created", created_at=handoff["created_at"])
            handoff["findings"] = findings or {}
            return handoff
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM incidents WHERE id = ? AND tenant_id = ?", (incident_id, tenant_id)).fetchone():
                raise KeyError("incident not found")
            handoff = (uid("handoff"), tenant_id, incident_id, from_agent, to_agent, task, json.dumps(findings or {}), "pending", now(), None, None)
            db.execute("INSERT INTO handoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", handoff)
            self._audit(db, tenant_id, incident_id, from_agent, "handoff_created", {"to_agent": to_agent, "task": task})
            if self.backend == "falkordb":
                self._graph_write_handoff(dict(zip(("id", "tenant_id", "incident_id", "from_agent", "to_agent", "task", "findings", "status", "created_at", "claimed_at", "completed_at"), handoff)))
            return self._row(db.execute("SELECT * FROM handoffs WHERE id = ? AND tenant_id = ?", (handoff[0], tenant_id)).fetchone()) or {}

    def claim_handoff(self, *, tenant_id: str, handoff_id: str, agent: str) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        handoff_id = clean_text("handoff_id", handoff_id)
        agent = clean_text("agent", agent)
        if self.backend == "falkordb":
            claimed_at = now()
            rows = self._graph_query("MATCH (h:Handoff {id:$id, tenant_id:$tenant_id}) WHERE h.to_agent=$agent RETURN h.incident_id AS incident_id", {"id": handoff_id, "tenant_id": tenant_id, "agent": agent})
            if not rows:
                raise KeyError("handoff not found or not assigned to agent")
            self._graph_query("MATCH (h:Handoff {id:$id, tenant_id:$tenant_id}) SET h.status='claimed', h.claimed_at=$claimed_at", {"id": handoff_id, "tenant_id": tenant_id, "claimed_at": claimed_at})
            self._graph_write_audit({}, audit_id=uid("audit"), tenant_id=tenant_id, incident_id=rows[0]["incident_id"], agent=agent, event="handoff_claimed", created_at=claimed_at)
            return next(h for h in self._graph_handoffs(tenant_id, rows[0]["incident_id"]) if h["id"] == handoff_id)
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM handoffs WHERE id = ? AND tenant_id = ? AND to_agent = ?", (handoff_id, tenant_id, agent)).fetchone()
            if not row:
                raise KeyError("handoff not found or not assigned to agent")
            db.execute("UPDATE handoffs SET status = 'claimed', claimed_at = ? WHERE id = ? AND tenant_id = ?", (now(), handoff_id, tenant_id))
            if self.backend == "falkordb":
                self._graph_query("MATCH (h:Handoff {id:$id, tenant_id:$tenant_id}) SET h.status='claimed', h.claimed_at=$claimed_at", {"id": handoff_id, "tenant_id": tenant_id, "claimed_at": now()})
            self._audit(db, tenant_id, row["incident_id"], agent, "handoff_claimed", {"handoff_id": handoff_id})
            return self._row(db.execute("SELECT * FROM handoffs WHERE id = ? AND tenant_id = ?", (handoff_id, tenant_id)).fetchone()) or {}

    def complete_handoff(self, *, tenant_id: str, handoff_id: str, agent: str, findings: dict[str, Any]) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        handoff_id = clean_text("handoff_id", handoff_id)
        agent = clean_text("agent", agent)
        if not isinstance(findings, dict):
            raise ValueError("findings must be an object")
        if self.backend == "falkordb":
            completed_at = now()
            rows = self._graph_query("MATCH (h:Handoff {id:$id, tenant_id:$tenant_id}) WHERE h.to_agent=$agent RETURN h.incident_id AS incident_id", {"id": handoff_id, "tenant_id": tenant_id, "agent": agent})
            if not rows:
                raise KeyError("handoff not found or not assigned to agent")
            self._graph_query("MATCH (h:Handoff {id:$id, tenant_id:$tenant_id}) SET h.status='completed', h.findings=$findings, h.completed_at=$completed_at", {"id": handoff_id, "tenant_id": tenant_id, "findings": json.dumps(findings), "completed_at": completed_at})
            self._graph_link_recalled_memories(tenant_id, handoff_id, findings.get("memory_ids", []))
            self._graph_write_audit(findings, audit_id=uid("audit"), tenant_id=tenant_id, incident_id=rows[0]["incident_id"], agent=agent, event="handoff_completed", created_at=completed_at)
            return next(h for h in self._graph_handoffs(tenant_id, rows[0]["incident_id"]) if h["id"] == handoff_id)
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM handoffs WHERE id = ? AND tenant_id = ? AND to_agent = ?", (handoff_id, tenant_id, agent)).fetchone()
            if not row:
                raise KeyError("handoff not found or not assigned to agent")
            db.execute("UPDATE handoffs SET status = 'completed', findings = ?, completed_at = ? WHERE id = ? AND tenant_id = ?",
                       (json.dumps(findings), now(), handoff_id, tenant_id))
            if self.backend == "falkordb":
                self._graph_query("MATCH (h:Handoff {id:$id, tenant_id:$tenant_id}) SET h.status='completed', h.findings=$findings, h.completed_at=$completed_at", {"id": handoff_id, "tenant_id": tenant_id, "findings": json.dumps(findings), "completed_at": now()})
            self._audit(db, tenant_id, row["incident_id"], agent, "handoff_completed", {"handoff_id": handoff_id, **findings})
            return self._row(db.execute("SELECT * FROM handoffs WHERE id = ? AND tenant_id = ?", (handoff_id, tenant_id)).fetchone()) or {}

    def get_incident_state(self, *, tenant_id: str, incident_id: str) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        incident_id = clean_text("incident_id", incident_id)
        if self.backend == "falkordb":
            state = self._graph_incident(tenant_id, incident_id)
            state["handoffs"] = self._graph_handoffs(tenant_id, incident_id)
            state["audit"] = self._graph_audit(tenant_id, incident_id)
            state["memories"] = self.list_memories_for_incident(tenant_id=tenant_id, incident_id=incident_id)
            memory_ids = []
            for handoff in state["handoffs"]:
                if handoff["to_agent"] == "investigator":
                    memory_ids.extend(handoff["findings"].get("memory_ids", []))
            if memory_ids:
                state["retrieved_memories"] = self._graph_query(
                    "MATCH (m:Memory) WHERE m.tenant_id=$tenant_id AND m.id IN $memory_ids "
                    "RETURN m.id AS id, m.tenant_id AS tenant_id, m.user_id AS user_id, m.type AS type, "
                    "m.content AS content, m.confidence AS confidence, m.created_at AS created_at, "
                    "m.updated_at AS updated_at ORDER BY m.updated_at DESC",
                    {"tenant_id": tenant_id, "memory_ids": list(dict.fromkeys(memory_ids))},
                )
                state["retrieved_memories"] = self._rank_memory_rows(
                    state["retrieved_memories"],
                    state["description"] + " account takeover suspicious IP shared device procedure",
                    state["user_id"],
                )
            else:
                state["retrieved_memories"] = []
            return state
        with self.connect() as db:
            incident = db.execute("SELECT * FROM incidents WHERE id = ? AND tenant_id = ?", (incident_id, tenant_id)).fetchone()
            if not incident:
                raise KeyError("incident not found")
            handoffs = self._rows(db.execute("SELECT * FROM handoffs WHERE incident_id = ? AND tenant_id = ? ORDER BY created_at", (incident_id, tenant_id)).fetchall())
            for handoff in handoffs:
                handoff["findings"] = json.loads(handoff["findings"] or "{}")
            audit = self._rows(db.execute("SELECT * FROM audit_logs WHERE incident_id = ? AND tenant_id = ? ORDER BY created_at", (incident_id, tenant_id)).fetchall())
            for entry in audit:
                entry["detail"] = json.loads(entry["detail"] or "{}")
            memories = self.list_memories_for_incident(tenant_id=tenant_id, incident_id=incident_id)
            memory_ids: list[str] = []
            for handoff in handoffs:
                if handoff["to_agent"] == "investigator":
                    memory_ids.extend(handoff["findings"].get("memory_ids", []))
            retrieved_memories: list[dict[str, Any]] = []
            memory_ids = list(dict.fromkeys(memory_ids))
            if memory_ids:
                placeholders = ",".join("?" for _ in memory_ids)
                retrieved_memories = self._rows(db.execute(
                    f"SELECT * FROM memories WHERE tenant_id = ? AND id IN ({placeholders}) ORDER BY updated_at DESC",
                    [tenant_id, *memory_ids],
                ).fetchall())
            state = dict(incident)
            state.update({"handoffs": handoffs, "audit": audit, "memories": memories, "retrieved_memories": retrieved_memories})
            return state

    # Agents: narrow deterministic workers with structured outputs.
    def run_workflow(self, *, tenant_id: str, user_id: str, description: str) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        user_id = clean_text("user_id", user_id)
        description = clean_text("description", description, max_length=MAX_DESCRIPTION_LENGTH)
        incident = self.create_incident(tenant_id=tenant_id, user_id=user_id, description=description)
        incident_id = incident["id"]
        triage = self._run_triage(tenant_id, incident_id, description)
        investigator = self._run_investigator(tenant_id, user_id, incident_id, description, triage)
        self._run_response(tenant_id, user_id, incident_id, description, triage, investigator)
        return self.get_incident_state(tenant_id=tenant_id, incident_id=incident_id)

    def _run_triage(self, tenant_id: str, incident_id: str, description: str) -> dict[str, Any]:
        text = description.lower()
        privileged = any(term in text for term in ("admin", "root", "privileged", "token"))
        category = "account_takeover" if privileged or "suspicious ip" in text else "credential_attack"
        severity = "critical" if privileged and any(term in text for term in ("token", "successful", "impossible travel")) else "high"
        result = {"severity": severity, "category": category, "next_agent": "investigator",
                  "reason": "Privileged account indicators match prior takeover patterns" if privileged else "Repeated authentication failures require investigation"}
        if self.backend == "falkordb":
            self._graph_query("MATCH (i:Incident {id:$incident_id, tenant_id:$tenant_id}) SET i.severity=$severity, i.category=$category", {"tenant_id": tenant_id, "incident_id": incident_id, "severity": severity, "category": category})
            self._graph_write_audit(result, audit_id=uid("audit"), tenant_id=tenant_id, incident_id=incident_id, agent="triage", event="triage_decision", created_at=now())
            handoff = self.create_handoff(tenant_id=tenant_id, incident_id=incident_id, from_agent="triage", to_agent="investigator", task="Find similar incidents, evidence patterns, and applicable procedures.", findings=result)
            self.claim_handoff(tenant_id=tenant_id, handoff_id=handoff["id"], agent="investigator")
            return result
        with self.lock, self.connect() as db:
            db.execute("UPDATE incidents SET severity = ?, category = ? WHERE id = ? AND tenant_id = ?", (severity, category, incident_id, tenant_id))
            self._audit(db, tenant_id, incident_id, "triage", "triage_decision", result)
        handoff = self.create_handoff(tenant_id=tenant_id, incident_id=incident_id, from_agent="triage", to_agent="investigator",
                                      task="Find similar incidents, evidence patterns, and applicable procedures.", findings=result)
        self.claim_handoff(tenant_id=tenant_id, handoff_id=handoff["id"], agent="investigator")
        return result

    def _run_investigator(self, tenant_id: str, user_id: str, incident_id: str, description: str, triage: dict[str, Any]) -> dict[str, Any]:
        recalled = self.search_memory(tenant_id=tenant_id, user_id=user_id, query=f"{description} account takeover suspicious IP shared device procedure", limit=8)
        if len(recalled) < 3:
            recalled = self.search_memory(tenant_id=tenant_id, query=f"{description} account takeover suspicious IP shared device procedure", limit=8)
        # Demo timing rule only: count prior workflows for this tenant/user.
        # This does not measure speed, similarity, or successful containment.
        if self.backend == "falkordb":
            prior_workflows = self._graph_query("MATCH (i:Incident)-[:HAS_HANDOFF]->(h:Handoff) WHERE i.tenant_id=$tenant_id AND i.user_id=$user_id AND i.id<>$incident_id RETURN count(DISTINCT i) AS total", {"tenant_id": tenant_id, "user_id": user_id, "incident_id": incident_id})[0]["total"]
        else:
            with self.connect() as db:
                prior_workflows = db.execute(
                """SELECT COUNT(*) FROM incidents AS i
                   WHERE i.tenant_id = ? AND i.user_id = ? AND i.id != ?
                     AND EXISTS (SELECT 1 FROM handoffs h
                                WHERE h.tenant_id = i.tenant_id AND h.incident_id = i.id)""",
                (tenant_id, user_id, incident_id),
                ).fetchone()[0]
        historical = [m for m in recalled if m["source_incident_id"] and m["source_incident_id"] != incident_id]
        learned = prior_workflows > 0
        useful = min(len(recalled), 4 if learned else 1)
        prior_procedure = next((m for m in recalled if m["type"] == "procedural"), None)
        shared_device = "shared device" in description.lower() or "same device" in description.lower()
        findings = {
            "evidence": ["repeated login failures", "suspicious IP addresses"] + (["shared device across accounts"] if shared_device else []),
            "similar_incidents": prior_workflows,
            "memories_retrieved": len(recalled), "memory_ids": [m["id"] for m in recalled], "useful_memories": useful,
            "pattern": "credential stuffing progressing toward account takeover" if learned or historical else "early-stage credential attack",
            "procedure": prior_procedure["content"] if prior_procedure else "Review account takeover containment procedure",
            "confidence": 0.94 if learned else (0.82 if historical else 0.71),
        }
        if self.backend == "falkordb":
            self._graph_query("MATCH (i:Incident {id:$incident_id, tenant_id:$tenant_id}) SET i.memories_retrieved=$retrieved, i.useful_memories=$useful, i.duplicate_steps=$duplicates", {"tenant_id": tenant_id, "incident_id": incident_id, "retrieved": len(recalled), "useful": useful, "duplicates": 1 if historical else 3})
        else:
            with self.lock, self.connect() as db:
                db.execute("UPDATE incidents SET memories_retrieved = ?, useful_memories = ?, duplicate_steps = ? WHERE id = ? AND tenant_id = ?", (len(recalled), useful, 1 if historical else 3, incident_id, tenant_id))
        self.store_memory(tenant_id=tenant_id, user_id=user_id, type="semantic",
                          content=f"Finding for {triage['category']}: {findings['pattern']}; {', '.join(findings['evidence'])}.",
                          source_incident_id=incident_id, confidence=findings["confidence"])
        self.store_memory(tenant_id=tenant_id, user_id=user_id, type="episodic",
                          content=f"Incident evidence: {description}", source_incident_id=incident_id, confidence=0.82)
        handoff = self._find_open_handoff(tenant_id, incident_id, "investigator")
        self.complete_handoff(tenant_id=tenant_id, handoff_id=handoff["id"], agent="investigator", findings=findings)
        next_handoff = self.create_handoff(tenant_id=tenant_id, incident_id=incident_id, from_agent="investigator", to_agent="response",
                                           task="Recommend a reversible containment action and explain the evidence.", findings=findings)
        self.claim_handoff(tenant_id=tenant_id, handoff_id=next_handoff["id"], agent="response")
        return findings

    def _run_response(self, tenant_id: str, user_id: str, incident_id: str, description: str,
                      triage: dict[str, Any], investigation: dict[str, Any]) -> dict[str, Any]:
        action = "Disable the exposed admin token and force a password reset" if "admin" in description.lower() or "token" in description.lower() else "Rate-limit the source IPs and require MFA re-authentication"
        response = {"recommended_action": action, "reason": f"{investigation['pattern']}; severity is {triage['severity']}.", "simulation": "Awaiting analyst confirmation", "accepted": None}
        handoff = self._find_open_handoff(tenant_id, incident_id, "response")
        self.complete_handoff(tenant_id=tenant_id, handoff_id=handoff["id"], agent="response", findings=response)
        if self.backend == "falkordb":
            timestamp = now()
            self._graph_query("MATCH (i:Incident {id:$incident_id, tenant_id:$tenant_id}) SET i.status='resolved', i.final_action=$action, i.resolved_at=$resolved_at, i.resolution_seconds=$seconds", {"tenant_id": tenant_id, "incident_id": incident_id, "action": action, "resolved_at": timestamp, "seconds": 30 if investigation["similar_incidents"] else 90})
            self._graph_write_audit(response, audit_id=uid("audit"), tenant_id=tenant_id, incident_id=incident_id, agent="response", event="recommendation", created_at=timestamp)
        else:
            with self.lock, self.connect() as db:
                db.execute("UPDATE incidents SET status = 'resolved', final_action = ?, resolved_at = ?, resolution_seconds = ? WHERE id = ? AND tenant_id = ?", (action, now(), 30 if investigation["similar_incidents"] else 90, incident_id, tenant_id))
                self._audit(db, tenant_id, incident_id, "response", "recommendation", response)
        return response

    def record_response_decision(self, *, tenant_id: str, incident_id: str, user_id: str,
                                 decision: str, note: str = "") -> dict[str, Any]:
        """Record analyst confirmation and learn only from accepted actions."""
        tenant_id = clean_text("tenant_id", tenant_id)
        incident_id = clean_text("incident_id", incident_id)
        user_id = clean_text("user_id", user_id)
        note = clean_text("note", note, max_length=MAX_DESCRIPTION_LENGTH) if note else ""
        decision = clean_text("decision", decision)
        if decision not in {"accepted", "rejected"}:
            raise ValueError("decision must be accepted or rejected")
        if self.backend == "falkordb":
            self._graph_incident(tenant_id, incident_id)
            audits = self._graph_audit(tenant_id, incident_id)
            recommendation = next((a for a in reversed(audits) if a["event"] == "recommendation"), None)
            if not recommendation:
                raise ValueError("no response recommendation exists for this incident")
            decision_rows = self._graph_query("MATCH (d:Decision {tenant_id:$tenant_id, incident_id:$incident_id}) RETURN d.decision AS decision", {"tenant_id": tenant_id, "incident_id": incident_id})
            previous_decision = decision_rows[0]["decision"] if decision_rows else None
            if previous_decision:
                if previous_decision == decision:
                    return self.get_incident_state(tenant_id=tenant_id, incident_id=incident_id)
                raise ValueError(f"incident already has a {previous_decision} response decision; create a new incident to revise it")
            accepted = decision == "accepted"
            detail = {"decision": decision, "accepted": accepted, "recommended_action": recommendation["detail"].get("recommended_action"), "note": note, "recorded_by": user_id}
            handoffs = self._graph_handoffs(tenant_id, incident_id)
            response_handoff = next((h for h in reversed(handoffs) if h["to_agent"] == "response"), None)
            if response_handoff:
                findings = response_handoff["findings"]
                findings.update({"accepted": accepted, "decision": decision, "simulation": "Approved for simulation" if accepted else "Rejected by analyst"})
                self._graph_query("MATCH (h:Handoff {id:$id, tenant_id:$tenant_id}) SET h.findings=$findings", {"id": response_handoff["id"], "tenant_id": tenant_id, "findings": json.dumps(findings)})
            self._graph_write_audit(detail, audit_id=uid("audit"), tenant_id=tenant_id, incident_id=incident_id, agent="analyst", event="response_decision", created_at=now())
            self._graph_query("MERGE (d:Decision {tenant_id:$tenant_id, incident_id:$incident_id}) SET d.decision=$decision, d.user_id=$user_id, d.created_at=$created_at WITH d MATCH (i:Incident {id:$incident_id, tenant_id:$tenant_id}) MERGE (d)-[:ABOUT]->(i)", {"tenant_id": tenant_id, "incident_id": incident_id, "decision": decision, "user_id": user_id, "created_at": now()})
            if accepted:
                action = detail["recommended_action"] or "the recommended containment action"
                procedure = {"id": uid("mem"), "tenant_id": tenant_id, "user_id": user_id, "type": "procedural", "content": f"Analyst-approved demo procedure: {action}. Approval recorded; no real mitigation was executed.", "source_incident_id": incident_id, "confidence": 0.98, "created_at": now(), "updated_at": now()}
                self._graph_write_memory(procedure)
            return self.get_incident_state(tenant_id=tenant_id, incident_id=incident_id)
        with self.lock, self.connect() as db:
            # Serialize the read/decision/write sequence across service instances.
            db.execute("BEGIN IMMEDIATE")
            incident = db.execute("SELECT * FROM incidents WHERE id = ? AND tenant_id = ?", (incident_id, tenant_id)).fetchone()
            if not incident:
                raise KeyError("incident not found")
            recommendation = db.execute(
                "SELECT detail FROM audit_logs WHERE incident_id = ? AND tenant_id = ? AND event = 'recommendation' ORDER BY created_at DESC LIMIT 1",
                (incident_id, tenant_id),
            ).fetchone()
            if not recommendation:
                raise ValueError("no response recommendation exists for this incident")
            recommendation_detail = json.loads(recommendation["detail"] or "{}")
            previous = db.execute(
                "SELECT detail FROM audit_logs WHERE incident_id = ? AND tenant_id = ? AND event = 'response_decision' ORDER BY created_at DESC LIMIT 1",
                (incident_id, tenant_id),
            ).fetchone()
            previous_detail = json.loads(previous["detail"] or "{}") if previous else None
            if previous_detail:
                if previous_detail.get("decision") == decision:
                    return self.get_incident_state(tenant_id=tenant_id, incident_id=incident_id)
                raise ValueError(
                    f"incident already has a {previous_detail.get('decision')} response decision; create a new incident to revise it"
                )
            accepted = decision == "accepted"
            detail = {
                "decision": decision,
                "accepted": accepted,
                "recommended_action": recommendation_detail.get("recommended_action"),
                "note": note,
                "recorded_by": user_id,
            }
            handoff = db.execute(
                "SELECT id, findings FROM handoffs WHERE incident_id = ? AND tenant_id = ? AND to_agent = 'response' ORDER BY created_at DESC LIMIT 1",
                (incident_id, tenant_id),
            ).fetchone()
            if handoff:
                findings = json.loads(handoff["findings"] or "{}")
                findings.update({"accepted": accepted, "decision": decision,
                                 "simulation": "Approved for simulation" if accepted else "Rejected by analyst"})
                db.execute("UPDATE handoffs SET findings = ? WHERE id = ? AND tenant_id = ?", (json.dumps(findings), handoff["id"], tenant_id))
            self._audit(db, tenant_id, incident_id, "analyst", "response_decision", detail)
            if accepted:
                action = recommendation_detail.get("recommended_action", "the recommended containment action")
                procedure_id = uid("mem")
                procedure = {
                    "id": procedure_id, "tenant_id": tenant_id, "user_id": user_id,
                    "type": "procedural",
                    "content": f"Analyst-approved demo procedure: {action}. Approval recorded; no real mitigation was executed.",
                    "source_incident_id": incident_id, "confidence": 0.98,
                    "created_at": now(), "updated_at": now(),
                }
                db.execute(
                    "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(procedure.values()),
                )
                if self.backend == "falkordb":
                    self._graph_write_memory(procedure)
        return self.get_incident_state(tenant_id=tenant_id, incident_id=incident_id)

    def _find_open_handoff(self, tenant_id: str, incident_id: str, to_agent: str) -> dict[str, Any]:
        if self.backend == "falkordb":
            rows = [h for h in self._graph_handoffs(tenant_id, incident_id) if h["to_agent"] == to_agent and h["status"] == "claimed"]
            if not rows:
                raise KeyError("agent handoff not found")
            return rows[-1]
        with self.connect() as db:
            row = db.execute("SELECT * FROM handoffs WHERE tenant_id = ? AND incident_id = ? AND to_agent = ? AND status = 'claimed' ORDER BY created_at DESC LIMIT 1", (tenant_id, incident_id, to_agent)).fetchone()
            if not row:
                raise KeyError("agent handoff not found")
            return dict(row)

    # Seed meaningful history for the first page load and cross-tenant proof.
    def seed_history(self) -> None:
        if self.backend == "falkordb":
            if self._graph_query("MATCH (i:Incident) RETURN count(i) AS total")[0]["total"]:
                return
        else:
            with self.connect() as db:
                if db.execute("SELECT 1 FROM incidents LIMIT 1").fetchone():
                    return
        examples = [
            ("Acme Corp", "admin-ops", "Repeated login failures for Acme admin from 185.220.101.4; shared device detected."),
            ("Acme Corp", "admin-ops", "Suspicious IP attempted 47 logins against the root account from a shared device."),
            ("Acme Corp", "security-team", "Previous account takeover: admin token was reused after credential stuffing."),
            ("Acme Corp", "security-team", "Multiple employee accounts saw login failures from the same device fingerprint."),
            ("Acme Corp", "admin-ops", "Admin login failures from a Tor exit node followed by token activity."),
            ("Acme Corp", "security-team", "Account takeover incident contained by disabling exposed token and forcing reset."),
            ("Acme Corp", "admin-ops", "Repeated failed logins and impossible travel on a privileged account."),
            ("Acme Corp", "security-team", "Shared device linked three credential attacks; MFA challenge was bypassed."),
            ("Acme Corp", "admin-ops", "Brute force pattern against an administrator from rotating suspicious IP addresses."),
            ("Acme Corp", "security-team", "Response procedure used: revoke token, reset password, preserve IP evidence, open ticket."),
            ("Globex", "ops", "Repeated login failures for a finance user from a suspicious IP."),
            ("Globex", "ops", "Shared device seen across two standard user accounts."),
        ]
        for tenant, user, description in examples:
            created = self.create_incident(tenant_id=tenant, user_id=user, description=description)
            incident_id = created["id"]
            kind = "procedural" if "procedure" in description.lower() or "contained" in description.lower() else "episodic"
            self.store_memory(tenant_id=tenant, user_id=user, type=kind, content=description, source_incident_id=incident_id, confidence=0.84)
            if "shared device" in description.lower():
                self.store_memory(tenant_id=tenant, user_id=user, type="semantic", content="Shared device across accounts is a strong indicator of coordinated credential attack.", source_incident_id=incident_id, confidence=0.88)
            if "token" in description.lower() or "contained" in description.lower():
                self.store_memory(tenant_id=tenant, user_id=user, type="procedural", content="Revoke or disable exposed token, force password reset, preserve IP evidence, and open a ticket.", source_incident_id=incident_id, confidence=0.95)
            if self.backend == "falkordb":
                self._graph_query("MATCH (i:Incident {id:$id, tenant_id:$tenant_id}) SET i.status='resolved', i.resolved_at=$resolved_at, i.resolution_seconds=90, i.final_action='Historical response recorded'", {"id": incident_id, "tenant_id": tenant, "resolved_at": now()})
            else:
                with self.connect() as db:
                    db.execute("UPDATE incidents SET status = 'resolved', resolved_at = ?, resolution_seconds = 90, final_action = 'Historical response recorded' WHERE id = ?", (now(), incident_id))

    def list_memories(self, *, tenant_id: str, query: str = "", type: str = "all",
                      offset: int = 0, limit: int = 24) -> dict[str, Any]:
        """Read a page of the selected tenant's memory library."""
        tenant_id = clean_text("tenant_id", tenant_id)
        if not isinstance(query, str) or len(query) > MAX_MEMORY_LENGTH:
            raise ValueError("query must be a string of at most 8000 characters")
        if type not in ("all", "episodic", "semantic", "procedural"):
            raise ValueError("invalid memory type")
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        if self.backend == "falkordb":
            rows = self._graph_query(
                "MATCH (m:Memory) WHERE m.tenant_id=$tenant_id AND "
                "($type='all' OR m.type=$type) AND toLower(m.content) CONTAINS toLower($query) "
                "OPTIONAL MATCH (m)-[:DERIVED_FROM]->(i:Incident) "
                "RETURN m.id AS id, m.tenant_id AS tenant_id, m.user_id AS user_id, m.type AS type, "
                "m.content AS content, m.confidence AS confidence, m.created_at AS created_at, "
                "m.updated_at AS updated_at, coalesce(i.id, '') AS source_incident_id "
                "ORDER BY m.created_at DESC SKIP $offset LIMIT $limit",
                {"tenant_id": tenant_id, "type": type, "query": query.strip(), "offset": offset, "limit": limit},
            )
            total = self._graph_query(
                "MATCH (m:Memory) WHERE m.tenant_id=$tenant_id AND "
                "($type='all' OR m.type=$type) AND toLower(m.content) CONTAINS toLower($query) "
                "RETURN count(m) AS total",
                {"tenant_id": tenant_id, "type": type, "query": query.strip()},
            )[0]["total"]
            return {"items": rows, "total": int(total), "offset": offset, "limit": limit}
        where = "tenant_id = ? AND (? = 'all' OR type = ?) AND instr(lower(content), lower(?)) > 0"
        params = (tenant_id, type, type, query.strip())
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params).fetchone()[0]
            items = self._rows(db.execute(
                f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall())
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def recent_incidents(self, tenant_id: str, limit: int = 40) -> list[dict[str, Any]]:
        tenant_id = clean_text("tenant_id", tenant_id)
        if self.backend == "falkordb":
            rows = self._graph_query("MATCH (i:Incident) WHERE i.tenant_id=$tenant_id RETURN i.id AS id, i.tenant_id AS tenant_id, i.user_id AS user_id, i.description AS description, i.severity AS severity, i.category AS category, i.status AS status, i.started_at AS started_at, i.resolved_at AS resolved_at, i.resolution_seconds AS resolution_seconds, i.duplicate_steps AS duplicate_steps, i.memories_retrieved AS memories_retrieved, i.useful_memories AS useful_memories, i.final_action AS final_action, i.created_at AS created_at, CASE WHEN i.final_action='Historical response recorded' THEN 'reference' ELSE 'investigation' END AS kind ORDER BY i.created_at DESC LIMIT $limit", {"tenant_id": tenant_id, "limit": limit})
            for row in rows:
                audits = self._graph_audit(tenant_id, row["id"])
                decision = next((a["detail"] for a in reversed(audits) if a["event"] == "response_decision"), {})
                row["decision"] = decision.get("decision")
            return rows
        with self.connect() as db:
            return self._rows(db.execute("""
                SELECT i.*,
                    CASE WHEN i.final_action = 'Historical response recorded'
                         THEN 'reference' ELSE 'investigation' END AS kind,
                    (SELECT json_extract(a.detail, '$.decision') FROM audit_logs a
                     WHERE a.tenant_id = i.tenant_id AND a.incident_id = i.id AND a.event = 'response_decision'
                     ORDER BY a.rowid DESC LIMIT 1) AS decision
                FROM incidents i WHERE i.tenant_id = ? ORDER BY i.created_at DESC, i.rowid DESC LIMIT ?
            """, (tenant_id, limit)).fetchall())

    def dashboard(self, tenant_id: str) -> dict[str, Any]:
        tenant_id = clean_text("tenant_id", tenant_id)
        if self.backend == "falkordb":
            incidents = self._graph_query("MATCH (i:Incident) WHERE i.tenant_id=$tenant_id RETURN count(i) AS incidents, coalesce(avg(i.resolution_seconds),0) AS avg_resolution, coalesce(sum(i.duplicate_steps),0) AS duplicates, sum(CASE WHEN i.final_action <> 'Historical response recorded' THEN 1 ELSE 0 END) AS live", {"tenant_id": tenant_id})[0]
            pending_rows = self._graph_query("MATCH (i:Incident) WHERE i.tenant_id=$tenant_id AND i.final_action IS NOT NULL AND i.final_action <> 'Historical response recorded' RETURN i.id AS id", {"tenant_id": tenant_id})
            incidents["pending"] = sum(1 for row in pending_rows if not any(a["event"] == "response_decision" for a in self._graph_audit(tenant_id, row["id"])))
            memories = self._graph_query("MATCH (m:Memory) WHERE m.tenant_id=$tenant_id RETURN m.type AS type, count(m) AS count", {"tenant_id": tenant_id})
            handoffs = self._graph_query("MATCH (h:Handoff) WHERE h.tenant_id=$tenant_id RETURN count(h) AS total, sum(CASE WHEN h.status='completed' THEN 1 ELSE 0 END) AS completed", {"tenant_id": tenant_id})[0]
            total = handoffs.get("total") or 0
            completed = handoffs.get("completed") or 0
            return {"metrics": {"incidents": incidents.get("incidents", 0), "investigations": incidents.get("live", 0) or 0, "reference_incidents": (incidents.get("incidents", 0) or 0) - (incidents.get("live", 0) or 0), "pending_review": incidents.get("pending", 0) or 0, "avg_resolution_seconds": round(incidents.get("avg_resolution", 0) or 0, 1), "duplicate_steps": incidents.get("duplicates", 0) or 0, "handoff_completion_rate": round(completed / max(1, total) * 100, 1), "completed_handoffs": completed, "total_handoffs": total}, "memory_counts": {row["type"]: row["count"] for row in memories}, "recent_incidents": self.recent_incidents(tenant_id)}
        with self.connect() as db:
            counts = db.execute("SELECT COUNT(*) AS incidents, COALESCE(AVG(resolution_seconds), 0) AS avg_resolution, COALESCE(SUM(duplicate_steps), 0) AS duplicates FROM incidents WHERE tenant_id = ?", (tenant_id,)).fetchone()
            memories = db.execute("SELECT type, COUNT(*) AS count FROM memories WHERE tenant_id = ? GROUP BY type", (tenant_id,)).fetchall()
            handoffs = db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed FROM handoffs WHERE tenant_id = ?", (tenant_id,)).fetchone()
            live = db.execute("""
                SELECT COUNT(*) AS count, SUM(CASE WHEN NOT EXISTS (
                    SELECT 1 FROM audit_logs a WHERE a.tenant_id = i.tenant_id AND a.incident_id = i.id
                    AND a.event = 'response_decision') AND i.final_action IS NOT NULL THEN 1 ELSE 0 END) AS pending
                FROM incidents i WHERE i.tenant_id = ?
                    AND COALESCE(i.final_action, '') != 'Historical response recorded'
            """, (tenant_id,)).fetchone()
        return {"metrics": {"incidents": counts["incidents"], "investigations": live["count"],
                            "reference_incidents": counts["incidents"] - live["count"], "pending_review": live["pending"] or 0,
                            "avg_resolution_seconds": round(counts["avg_resolution"], 1), "duplicate_steps": counts["duplicates"],
                            "handoff_completion_rate": round((handoffs["completed"] or 0) / max(1, handoffs["total"] or 0) * 100, 1),
                            "completed_handoffs": handoffs["completed"] or 0, "total_handoffs": handoffs["total"]},
                "memory_counts": {row["type"]: row["count"] for row in memories}, "recent_incidents": self.recent_incidents(tenant_id)}
