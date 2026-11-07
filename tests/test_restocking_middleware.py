"""Unit tests for Task A14: Add idempotency, audit and approver authorization to Restocking (FD-3, FD-4).

Verifies the Done Condition: 'Restocking uses the shared middleware',
covering:
1. Full end-to-end Restocking run (Evaluate -> RetrieveSuppliers -> Draft ->
   HumanReview interrupt -> Reject v1 -> Re-draft v2 -> Approve v2 -> SendEmail)
   recording RUN, DECISION, and DELIVERY entries in `AuditLogWriter` and exact
   version hashes in `ApprovalService`.
2. Approver Authorization & Branch Isolation:
   - Unauthorized roles (e.g. `sales_team` or `viewer`) cannot approve restock orders.
   - Branch 2 approver (`stock.nasrcity@supermarket.eg`) cannot approve Branch 1's order.
3. AI Output Guard & Input Validation in DraftReportNode:
   - AI-supplied recipient/branch overrides are stripped and logged.
   - Non-positive quantities are blocked before reaching `HumanReviewNode`.
4. Idempotency & Outbox Replay Safety + Max-Retry Operator Escalation:
   - Replaying approval or dispatching outbox multiple times sends only 1 supplier email.
   - Exceeding `max_retries` (3 rejections) halts the loop and queues an escalation email.
"""
import unittest

from foundation.approval_service import VersionMismatchError
from foundation.auth_branch_middleware import AuthorizationError, BranchIsolationError
from foundation.langgraph_runtime import OutboxMessage
from foundation.restocking_middleware import (
    GuardedRestockingWorkflow,
    RestockingDependencies,
)
from foundation.validation_retry_middleware import InputValidationError


class TestRestockingMiddlewareA14(unittest.TestCase):
    def setUp(self) -> None:
        self.deps = RestockingDependencies()
        self.sample_inventory = [
            {
                "branch_id": 1,
                "product_id": 1,
                "sku": "SKU-0001",
                "product_name": "Juhayna Milk Small 001",
                "category": "Dairy",
                "subcategory": "Milk",
                "stock_quantity": 20,  # Below Milk High-demand threshold (60)
                "days_until_expiry": 2,  # Within Dairy 3-day expiry threshold
                "demand_level": "High",
                "recommended_restock_qty": 60,
            },
            {
                "branch_id": 1,
                "product_id": 2,
                "sku": "SKU-0002",
                "product_name": "Cleared Beverage 002",
                "category": "Beverages",
                "subcategory": "Water",
                "stock_quantity": 10,
                "days_until_expiry": 2,
                "demand_level": "High",
                "recommended_restock_qty": 40,
            },
            {
                "branch_id": 2,
                "product_id": 1,
                "sku": "SKU-0001",
                "product_name": "Juhayna Milk Small 001",
                "category": "Dairy",
                "subcategory": "Milk",
                "stock_quantity": 2077,
                "days_until_expiry": 10,
                "demand_level": "High",
            },
        ]
        self.sample_suppliers = [
            {
                "supplier_id": 1,
                "supplier_name": "Nile Valley Dairy Distribution",
                "product_category": "Dairy",
                "shipping_duration_days": 1,
                "minimum_order_quantity": 24,
                "email": "orders@niledairy.eg",
            },
            {
                "supplier_id": 2,
                "supplier_name": "Cairo Beverage Supply",
                "product_category": "Beverages",
                "shipping_duration_days": 2,
                "minimum_order_quantity": 6,
                "email": "orders@cairobeverage.eg",
            },
        ]
        # Product 2 in Branch 1 is being cleared by an active offer (H2 / B14)
        self.workflow = GuardedRestockingWorkflow(
            dependencies=self.deps,
            inventory_rows=self.sample_inventory,
            supplier_rows=self.sample_suppliers,
            active_offer_product_ids={1: [2]},
        )

    def test_full_restocking_flow_uses_shared_middleware_end_to_end(self) -> None:
        """Task A14 Done Condition: Restocking uses the shared middleware (Auth, AI Guard, Approval, Audit, Idempotency, Outbox)."""
        run_id = "run-maadi-2026-11-06"

        # 1. Start daily run for Branch 1 (Maadi) -> pauses at HumanReviewNode
        paused = self.workflow.start_daily_run(
            actor_id="system.scheduler.branch1",
            branch_id=1,
            run_id=run_id,
            initial_state={
                "simulated_ai_injected_fields": {
                    "recipient": "hacker@evil.com",
                    "branch_id": 3,
                }
            },
        )
        self.assertEqual(paused.status, "INTERRUPTED")
        self.assertEqual(paused.next_node, "HumanReviewNode")
        self.assertEqual(paused.state["version"], 1)
        self.assertEqual(paused.state["approver_recipient"], "stock.maadi@supermarket.eg")
        self.assertEqual(paused.state["drafted_report"]["branch_id"], 1)
        self.assertEqual(len(paused.state["skipped_products"]), 1)
        self.assertEqual(paused.state["skipped_products"][0]["product_id"], 2)

        v1_hash = paused.state["version_hash"]

        # 2. Authorized Maadi approver rejects v1 with feedback -> re-drafts v2 and pauses again
        rejected_ckpt = self.workflow.submit_human_review(
            run_id=run_id,
            branch_id=1,
            reviewer_actor_id="stock.maadi@supermarket.eg",
            decision="REJECT",
            reason="Reduce quantity from 60 to 48 units",
            reviewed_version=1,
            reviewed_hash=v1_hash,
            override_requested_quantity=48,
        )
        self.assertEqual(rejected_ckpt.status, "INTERRUPTED")
        self.assertEqual(rejected_ckpt.state["version"], 2)
        self.assertEqual(rejected_ckpt.state["retry_count"], 1)
        self.assertEqual(rejected_ckpt.state["drafted_report"]["requested_quantity"], 48)
        v2_hash = rejected_ckpt.state["version_hash"]
        self.assertNotEqual(v1_hash, v2_hash)

        # 3. Authorized Maadi approver approves v2 -> SendEmailNode stages email in outbox & completes
        completed_ckpt = self.workflow.submit_human_review(
            run_id=run_id,
            branch_id=1,
            reviewer_actor_id="stock.maadi@supermarket.eg",
            decision="APPROVE",
            reason="Approved 48 units",
            reviewed_version=2,
            reviewed_hash=v2_hash,
        )
        self.assertEqual(completed_ckpt.status, "COMPLETED")
        self.assertEqual(completed_ckpt.state["supplier_recipient"], "orders@niledairy.eg")

        # 4. Verify ApprovalService recorded both v1 (REJECT) and v2 (APPROVE) with exact hashes
        assert self.deps.approval_service is not None
        approvals = self.deps.approval_service.list_history(
            entity_type="RESTOCK_ORDER",
            entity_id=1001,
            branch_id=1,
        )
        self.assertEqual(len(approvals), 2)
        self.assertEqual(approvals[0].reviewed_version, 1)
        self.assertEqual(approvals[0].decision, "REJECT")
        self.assertEqual(approvals[1].reviewed_version, 2)
        self.assertEqual(approvals[1].decision, "APPROVE")
        self.assertEqual(approvals[1].reviewed_hash, v2_hash)

        # 5. Verify AuditLogWriter recorded RUN, DECISION, DELIVERY, and SECURITY_AI_OVERRIDE_BLOCKED
        self.assertTrue(self.deps.audit_writer.has_logged_run_decision_and_delivery(run_id))

        # 6. Verify TransactionalOutbox & Idempotency: dispatching multiple times sends exactly 1 email
        sent: list[OutboxMessage] = []
        self.deps.outbox.dispatch_pending(lambda m: sent.append(m), branch_id=1)
        self.deps.outbox.dispatch_pending(lambda m: sent.append(m), branch_id=1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].recipient, "orders@niledairy.eg")
        self.assertEqual(sent[0].payload["requested_quantity"], 48)

    def test_unauthorized_or_cross_branch_approver_is_blocked(self) -> None:
        """Task A14: Only authorized approvers for the same branch can approve or reject."""
        run_id = "run-auth-check"
        self.workflow.start_daily_run(
            actor_id="system.scheduler.branch1",
            branch_id=1,
            run_id=run_id,
        )

        # 1. Sales team actor (wrong role) is blocked by RBAC AuthorizationError
        with self.assertRaises(AuthorizationError):
            self.workflow.submit_human_review(
                run_id=run_id,
                branch_id=1,
                reviewer_actor_id="sales.maadi@supermarket.eg",
                decision="APPROVE",
            )

        # 2. Branch 2 restock approver (wrong branch) is blocked by BranchIsolationError
        with self.assertRaises(BranchIsolationError):
            self.workflow.submit_human_review(
                run_id=run_id,
                branch_id=1,
                reviewer_actor_id="stock.nasrcity@supermarket.eg",
                decision="APPROVE",
            )

        # 3. Stale version / wrong hash is blocked by VersionMismatchError
        with self.assertRaises(VersionMismatchError):
            self.workflow.submit_human_review(
                run_id=run_id,
                branch_id=1,
                reviewer_actor_id="stock.maadi@supermarket.eg",
                decision="APPROVE",
                reviewed_version=1,
                reviewed_hash="tampered-hash-000",
            )

    def test_invalid_restock_quantity_and_max_retry_escalation(self) -> None:
        """Task A14: Rejects non-positive quantities and escalates to operator after max_retries (3)."""
        run_id = "run-escalation-test"
        self.workflow.start_daily_run(
            actor_id="system.scheduler.branch1",
            branch_id=1,
            run_id=run_id,
        )

        # Attempting to override quantity to 0 or negative in rejection loop raises InputValidationError
        with self.assertRaises(InputValidationError):
            self.workflow.submit_human_review(
                run_id=run_id,
                branch_id=1,
                reviewer_actor_id="stock.maadi@supermarket.eg",
                decision="REJECT",
                reason="Set to zero",
                override_requested_quantity=0,
            )

        # Reject 3 times (reaching max_retries=3), then 4th rejection triggers operator escalation
        for i in range(1, 4):
            ckpt = self.workflow.submit_human_review(
                run_id=run_id,
                branch_id=1,
                reviewer_actor_id="stock.maadi@supermarket.eg",
                decision="REJECT",
                reason=f"Rejection #{i}",
                override_requested_quantity=50 - i,
            )
            self.assertEqual(ckpt.status, "INTERRUPTED")
            self.assertEqual(ckpt.state["retry_count"], i)

        # 4th rejection exceeds max_retries (3) -> run stops (COMPLETED at END_NODE) with ESCALATED status
        escalated_ckpt = self.workflow.submit_human_review(
            run_id=run_id,
            branch_id=1,
            reviewer_actor_id="stock.maadi@supermarket.eg",
            decision="REJECT",
            reason="Rejection #4 exceeds limit",
        )
        self.assertEqual(escalated_ckpt.status, "COMPLETED")
        self.assertEqual(escalated_ckpt.state["review_outcome"], "ESCALATED")
        self.assertEqual(self.workflow.saved_orders[1001]["status"], "ESCALATED")

        # Escalation email queued for Maadi escalation operator (`ops.maadi@supermarket.eg`)
        outbox_msgs = self.deps.outbox.list_messages(branch_id=1, status="PENDING")
        self.assertEqual(len(outbox_msgs), 1)
        self.assertEqual(outbox_msgs[0].recipient, "ops.maadi@supermarket.eg")


if __name__ == "__main__":
    unittest.main()
