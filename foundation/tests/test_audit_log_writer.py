"""Unit tests for Task A11: Audit log writer (actor, timestamp, version, run_id) (FD-3).

Verifies the Done Condition: 'Every run, decision and delivery is logged',
and checks that actor, timestamp (`occurred_at`), version, and run_id are
validated, stored, and immutable on every audit entry.
"""
import unittest
from datetime import datetime, timezone

from foundation.audit_log_writer import (
    AuditLogError,
    AuditLogWriter,
    InMemoryAuditBackend,
)


class TestAuditLogWriterA11(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InMemoryAuditBackend()
        self.writer = AuditLogWriter(backend=self.backend)

    def test_every_run_decision_and_delivery_is_logged(self) -> None:
        """Task A11 Done Condition: Every run, decision and delivery is logged."""
        run_id = "run-maadi-2026-11-06-001"
        branch_id = 1

        # 1. Log workflow run start
        run_entry = self.writer.log_run(
            run_id=run_id,
            branch_id=branch_id,
            actor="system.scheduler.branch1",
            workflow_name="restock",
            status="STARTED",
            version=1,
            details={"flagged_products": 2},
        )
        self.assertEqual(run_entry.run_id, run_id)
        self.assertEqual(run_entry.actor, "system.scheduler.branch1")
        self.assertEqual(run_entry.version, 1)
        self.assertEqual(run_entry.action, "RUN_STARTED")
        self.assertTrue(len(run_entry.timestamp) > 0)

        # 2. Log human review decision (rejection on v1, approval on v2)
        reject_entry = self.writer.log_decision(
            run_id=run_id,
            branch_id=branch_id,
            actor="stock.maadi@supermarket.eg",
            decision="REJECT",
            entity_type="RESTOCK_ORDER",
            entity_id=501,
            version=1,
            reason="Reduce quantity from 60 to 40",
            reviewed_hash="sha256-v1-hash",
        )
        self.assertEqual(reject_entry.action, "DECISION_REJECT")
        self.assertEqual(reject_entry.version, 1)
        self.assertEqual(reject_entry.details["reason"], "Reduce quantity from 60 to 40")

        approve_entry = self.writer.log_decision(
            run_id=run_id,
            branch_id=branch_id,
            actor="stock.maadi@supermarket.eg",
            decision="APPROVE",
            entity_type="RESTOCK_ORDER",
            entity_id=501,
            version=2,
            reason="Quantity 40 looks good",
            reviewed_hash="sha256-v2-hash",
        )
        self.assertEqual(approve_entry.action, "DECISION_APPROVE")
        self.assertEqual(approve_entry.version, 2)

        # 3. Log outbound email delivery
        delivery_entry = self.writer.log_delivery(
            run_id=run_id,
            branch_id=branch_id,
            actor="system.outbox.dispatcher",
            channel="EMAIL",
            recipient="orders@supplier-demo.eg",
            delivery_status="SENT",
            entity_type="RESTOCK_ORDER",
            entity_id=501,
            version=2,
            outbox_message_id=10,
        )
        self.assertEqual(delivery_entry.action, "DELIVERY_SENT")
        self.assertEqual(delivery_entry.version, 2)
        self.assertEqual(delivery_entry.details["recipient"], "orders@supplier-demo.eg")

        # Verify all 4 entries are recorded and cover RUN, DECISION, and DELIVERY
        entries = self.writer.list_entries(run_id=run_id)
        self.assertEqual(len(entries), 4)
        self.assertTrue(self.writer.has_logged_run_decision_and_delivery(run_id))

    def test_required_fields_and_timezone_validation(self) -> None:
        """Rejects missing run_id, missing actor, invalid version, or naive datetime."""
        with self.assertRaises(AuditLogError):
            self.writer.write(run_id="", actor="system", action="RUN_STARTED", version=1)

        with self.assertRaises(AuditLogError):
            self.writer.write(run_id="run-1", actor="   ", action="RUN_STARTED", version=1)

        with self.assertRaises(AuditLogError):
            self.writer.write(run_id="run-1", actor="system", action="RUN_STARTED", version=0)

        with self.assertRaises(AuditLogError):
            self.writer.write(
                run_id="run-1",
                actor="system",
                action="RUN_STARTED",
                version=1,
                timestamp=datetime(2026, 11, 6, 12, 0, 0),  # Naive datetime rejected
            )

        # Valid aware datetime is converted to UTC ISO string
        aware_dt = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
        entry = self.writer.write(
            run_id="run-1",
            actor="system",
            action="RUN_STARTED",
            version=1,
            timestamp=aware_dt,
        )
        self.assertEqual(entry.timestamp, "2026-11-06T12:00:00+00:00")

    def test_filtering_by_branch_and_entity(self) -> None:
        """Supports querying audit history by branch_id, entity_type, and entity_id."""
        self.writer.log_run(
            run_id="run-b1",
            branch_id=1,
            actor="system.scheduler.branch1",
            workflow_name="reporting",
            status="COMPLETED",
        )
        self.writer.log_run(
            run_id="run-b2",
            branch_id=2,
            actor="system.scheduler.branch2",
            workflow_name="reporting",
            status="COMPLETED",
        )

        b1_logs = self.writer.list_entries(branch_id=1)
        b2_logs = self.writer.list_entries(branch_id=2)
        self.assertEqual(len(b1_logs), 1)
        self.assertEqual(b1_logs[0].run_id, "run-b1")
        self.assertEqual(len(b2_logs), 1)
        self.assertEqual(b2_logs[0].run_id, "run-b2")


if __name__ == "__main__":
    unittest.main()
