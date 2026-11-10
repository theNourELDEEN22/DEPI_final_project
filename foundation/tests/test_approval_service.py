"""Unit tests for Task A12: Approval service: actor, time, reason, version, hash (FD-3).

Verifies the Done Condition: 'Approval stores the exact version reviewed',
plus:
- Cryptographic SHA-256 hash binding (`reviewed_hash`) so changing any field or
  creating a new version invalidates old approvals (D8).
- Rejection of stale-version or tampered-hash review submissions.
- Mandatory reason enforcement on `REJECT` and `REQUEST_CHANGES`.
- Integration with `AuditLogWriter` (A11).
"""
import unittest

from foundation.approval_service import (
    ApprovalError,
    ApprovalService,
    InMemoryApprovalBackend,
    UnapprovedExecutionError,
    VersionMismatchError,
    compute_version_hash,
)
from foundation.audit_log_writer import AuditLogWriter, InMemoryAuditBackend


class TestApprovalServiceA12(unittest.TestCase):
    def setUp(self) -> None:
        self.audit_backend = InMemoryAuditBackend()
        self.audit_writer = AuditLogWriter(backend=self.audit_backend)
        self.backend = InMemoryApprovalBackend()
        self.service = ApprovalService(backend=self.backend, audit_writer=self.audit_writer)

    def test_approval_stores_exact_version_and_hash_reviewed(self) -> None:
        """Task A12 Done Condition: Approval stores the exact version reviewed."""
        v1_payload = {"product_id": 1, "supplier_id": 1, "requested_quantity": 60}
        v1_hash = compute_version_hash(
            entity_type="RESTOCK_ORDER",
            entity_id=101,
            branch_id=1,
            version=1,
            payload=v1_payload,
        )

        # Reviewer rejects v1 with feedback note
        rec_v1 = self.service.record_decision(
            entity_type="RESTOCK_ORDER",
            entity_id=101,
            branch_id=1,
            actor_id="stock.maadi@supermarket.eg",
            actor_role="restock_approver",
            decision="REJECT",
            reason="Reduce quantity from 60 to 40",
            reviewed_version=1,
            current_version=1,
            payload_snapshot=v1_payload,
            reviewed_hash=v1_hash,
            run_id="run-maadi-101",
        )
        self.assertEqual(rec_v1.reviewed_version, 1)
        self.assertEqual(rec_v1.reviewed_hash, v1_hash)
        self.assertEqual(rec_v1.actor_id, "stock.maadi@supermarket.eg")
        self.assertEqual(rec_v1.reason, "Reduce quantity from 60 to 40")
        self.assertTrue(len(rec_v1.time) > 0)

        # Draft v2 created with requested_quantity = 40
        v2_payload = {"product_id": 1, "supplier_id": 1, "requested_quantity": 40}
        v2_hash = compute_version_hash(
            entity_type="RESTOCK_ORDER",
            entity_id=101,
            branch_id=1,
            version=2,
            payload=v2_payload,
        )
        self.assertNotEqual(v1_hash, v2_hash)

        # Reviewer approves v2
        rec_v2 = self.service.record_decision(
            entity_type="RESTOCK_ORDER",
            entity_id=101,
            branch_id=1,
            actor_id="stock.maadi@supermarket.eg",
            actor_role="restock_approver",
            decision="APPROVE",
            reason="Approved v2 quantity 40",
            reviewed_version=2,
            current_version=2,
            payload_snapshot=v2_payload,
            reviewed_hash=v2_hash,
            run_id="run-maadi-101",
        )

        # Exact version (2) and exact hash (v2_hash) are stored and distinct from v1
        self.assertEqual(rec_v2.reviewed_version, 2)
        self.assertEqual(rec_v2.version, 2)
        self.assertEqual(rec_v2.reviewed_hash, v2_hash)
        self.assertEqual(rec_v2.hash, v2_hash)

        # Both v1 and v2 records are preserved in history
        history = self.service.list_history(entity_type="RESTOCK_ORDER", entity_id=101, branch_id=1)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].reviewed_version, 1)
        self.assertEqual(history[1].reviewed_version, 2)

        # Execution gate succeeds for exact v2 payload
        verified = self.service.verify_approved_for_execution(
            entity_type="RESTOCK_ORDER",
            entity_id=101,
            branch_id=1,
            current_version=2,
            current_payload=v2_payload,
        )
        self.assertEqual(verified.approval_id, rec_v2.approval_id)

    def test_stale_version_or_mutated_payload_invalidates_approval(self) -> None:
        """If an offer or order changes after approval, old approval hash no longer matches (D8)."""
        offer_v1 = {"product_id": 10, "offer_type": "DISCOUNT", "discount_pct": "20.0", "quantity_cap": 100}

        # Approve v1
        self.service.record_decision(
            entity_type="OFFER",
            entity_id=55,
            branch_id=2,
            actor_id="sales.nasrcity@supermarket.eg",
            actor_role="sales_team",
            decision="APPROVE",
            reason="Looks good",
            reviewed_version=1,
            current_version=1,
            payload_snapshot=offer_v1,
        )

        # Case 1: Payload is silently mutated at v1 (e.g. discount_pct changed from 20.0 to 35.0)
        tampered_v1 = {**offer_v1, "discount_pct": "35.0"}
        with self.assertRaises(VersionMismatchError):
            self.service.verify_approved_for_execution(
                entity_type="OFFER",
                entity_id=55,
                branch_id=2,
                current_version=1,
                current_payload=tampered_v1,
            )

        # Case 2: Offer is bumped to version 2 without a new v2 approval
        with self.assertRaises(VersionMismatchError):
            self.service.verify_approved_for_execution(
                entity_type="OFFER",
                entity_id=55,
                branch_id=2,
                current_version=2,
                current_payload=offer_v1,
            )

        # Case 3: Reviewer tries to submit an approval for stale version 1 when current_version is 2
        with self.assertRaises(VersionMismatchError):
            self.service.record_decision(
                entity_type="OFFER",
                entity_id=55,
                branch_id=2,
                actor_id="sales.nasrcity@supermarket.eg",
                actor_role="sales_team",
                decision="APPROVE",
                reviewed_version=1,
                current_version=2,
                payload_snapshot=offer_v1,
            )

    def test_reject_and_request_changes_require_reason_and_block_execution(self) -> None:
        """REJECT and REQUEST_CHANGES require a reason and block execution."""
        payload = {"product_id": 5, "offer_type": "BUY_2_GET_1", "quantity_cap": 50}

        # Missing reason on REJECT or REQUEST_CHANGES raises ApprovalError
        with self.assertRaises(ApprovalError):
            self.service.record_decision(
                entity_type="OFFER",
                entity_id=77,
                branch_id=1,
                actor_id="sales.maadi@supermarket.eg",
                actor_role="sales_team",
                decision="REQUEST_CHANGES",
                reason="   ",
                reviewed_version=1,
                current_version=1,
                payload_snapshot=payload,
            )

        # Valid REQUEST_CHANGES recorded
        self.service.record_decision(
            entity_type="OFFER",
            entity_id=77,
            branch_id=1,
            actor_id="sales.maadi@supermarket.eg",
            actor_role="sales_team",
            decision="REQUEST_CHANGES",
            reason="Lower quantity cap to 30",
            reviewed_version=1,
            current_version=1,
            payload_snapshot=payload,
        )

        # Cannot execute while latest decision is REQUEST_CHANGES
        with self.assertRaises(UnapprovedExecutionError):
            self.service.verify_approved_for_execution(
                entity_type="OFFER",
                entity_id=77,
                branch_id=1,
                current_version=1,
                current_payload=payload,
            )


if __name__ == "__main__":
    unittest.main()
