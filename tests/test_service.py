"""Regression tests always use temporary databases, never the demo's saved data."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memory_mesh.service import MemoryService


class MemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.service = MemoryService(self.path)
        self.tenant = "Test workspace"

    def run_case(self, tenant=None):
        return self.service.run_workflow(
            tenant_id=tenant or self.tenant, user_id="analyst",
            description="Repeated login failures for an admin from a suspicious IP and shared device.",
        )

    def decide(self, incident, decision="accepted", service=None):
        return (service or self.service).record_response_decision(
            tenant_id=self.tenant, user_id="analyst", incident_id=incident["id"],
            decision=decision, note="Reviewed in test",
        )

    def test_seeded_history_is_separate_from_live_investigations(self):
        dashboard = self.service.dashboard("Acme Corp")
        self.assertEqual(dashboard["metrics"]["investigations"], 0)
        self.assertEqual(dashboard["metrics"]["reference_incidents"], 10)
        self.assertTrue(all(row["kind"] == "reference" for row in dashboard["recent_incidents"]))
        self.assertNotIn("cross_tenant_leakage", dashboard["metrics"])

    def test_workflow_persists_and_second_run_recalls_first(self):
        first = self.run_case()
        self.assertEqual(first["resolution_seconds"], 90)
        self.assertEqual(len(first["handoffs"]), 2)
        self.assertTrue(all(h["status"] == "completed" for h in first["handoffs"]))
        self.assertEqual({m["type"] for m in first["memories"]}, {"semantic", "episodic"})
        self.assertEqual(first["retrieved_memories"], [])
        self.service = MemoryService(self.path)
        self.assertEqual(self.service.get_incident_state(tenant_id=self.tenant, incident_id=first["id"])["id"], first["id"])
        second = self.run_case()
        self.assertEqual(second["resolution_seconds"], 30)
        self.assertTrue(any(m["source_incident_id"] == first["id"] for m in second["retrieved_memories"]))
        self.assertEqual(self.service.dashboard(self.tenant)["metrics"]["investigations"], 2)

    def test_approval_is_idempotent_and_updates_pending_count(self):
        first = self.run_case()
        self.assertEqual(self.service.dashboard(self.tenant)["metrics"]["pending_review"], 1)
        accepted = self.decide(first)
        again = self.decide(first)
        self.assertEqual(accepted, again)
        self.assertEqual(sum(m["type"] == "procedural" for m in again["memories"]), 1)
        self.assertEqual(sum(a["event"] == "response_decision" for a in again["audit"]), 1)
        self.assertEqual(self.service.dashboard(self.tenant)["metrics"]["pending_review"], 0)
        self.assertEqual(self.service.recent_incidents(self.tenant)[0]["decision"], "accepted")
        with self.assertRaises(ValueError):
            self.decide(first, "rejected")

    def test_rejection_records_feedback_without_procedure(self):
        result = self.decide(self.run_case(), "rejected")
        self.assertFalse(any(m["type"] == "procedural" for m in result["memories"]))
        self.assertEqual(result["handoffs"][-1]["findings"]["accepted"], False)
        self.assertEqual(self.service.dashboard(self.tenant)["metrics"]["pending_review"], 0)

    def test_approval_rolls_back_if_procedure_write_fails(self):
        first = self.run_case()
        with self.service.connect() as db:
            db.execute("""CREATE TRIGGER reject_procedure BEFORE INSERT ON memories
                          WHEN NEW.type = 'procedural'
                          BEGIN SELECT RAISE(ABORT, 'test insert failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.decide(first)
        result = self.service.get_incident_state(tenant_id=self.tenant, incident_id=first["id"])
        self.assertFalse(any(a["event"] == "response_decision" for a in result["audit"]))
        self.assertIsNone(result["handoffs"][-1]["findings"]["accepted"])

    def test_concurrent_approvals_across_service_instances_create_one_procedure(self):
        first = self.run_case()
        other = MemoryService(self.path)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda index: self.decide(first, service=other if index % 2 else self.service), range(8)))
        for result in results:
            self.assertEqual(sum(m["type"] == "procedural" for m in result["memories"]), 1)
            self.assertEqual(sum(a["event"] == "response_decision" for a in result["audit"]), 1)

    def test_tenant_filters_cover_incidents_memories_and_decisions(self):
        first = self.run_case()
        self.assertEqual(self.service.list_memories(tenant_id="Other tenant")["total"], 0)
        self.assertEqual(self.service.search_memory(tenant_id="Other tenant", query="admin"), [])
        self.assertEqual(self.service.recent_incidents("Other tenant"), [])
        with self.assertRaises(KeyError):
            self.service.get_incident_state(tenant_id="Other tenant", incident_id=first["id"])
        with self.assertRaises(KeyError):
            self.service.record_response_decision(tenant_id="Other tenant", incident_id=first["id"], user_id="analyst", decision="accepted")

    def test_memory_library_search_filter_and_pagination(self):
        self.decide(self.run_case())
        page = self.service.list_memories(tenant_id=self.tenant, limit=2)
        next_page = self.service.list_memories(tenant_id=self.tenant, offset=2, limit=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(next_page["items"]), 1)
        self.assertFalse({m["id"] for m in page["items"]} & {m["id"] for m in next_page["items"]})
        procedures = self.service.list_memories(tenant_id=self.tenant, type="procedural", query="APPROVED")
        self.assertEqual(procedures["total"], 1)
        self.assertEqual(self.service.list_memories(tenant_id=self.tenant, query="%")["total"], 0)
        for args in ({"limit": 0}, {"offset": -1}, {"type": "unknown"}):
            with self.assertRaises(ValueError):
                self.service.list_memories(tenant_id=self.tenant, **args)

    def test_references_cannot_be_approved(self):
        reference = self.service.recent_incidents("Acme Corp")[0]
        with self.assertRaises(ValueError):
            self.service.record_response_decision(tenant_id="Acme Corp", incident_id=reference["id"], user_id="analyst", decision="accepted")

    def test_workflow_normalizes_identity_before_all_steps(self):
        result = self.service.run_workflow(tenant_id="  New tenant  ", user_id="  analyst ", description="  Repeated admin login failures. ")
        self.assertEqual(result["tenant_id"], "New tenant")
        self.assertEqual(result["user_id"], "analyst")
        self.assertEqual(len(result["handoffs"]), 2)

    def test_partial_incident_is_not_mislabelled_as_reference(self):
        self.service.create_incident(tenant_id=self.tenant, user_id="analyst", description="Partial workflow")
        dashboard = self.service.dashboard(self.tenant)
        self.assertEqual(dashboard["recent_incidents"][0]["kind"], "investigation")
        self.assertEqual(dashboard["metrics"]["pending_review"], 0)


if __name__ == "__main__":
    unittest.main()
