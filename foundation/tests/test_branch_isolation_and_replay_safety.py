"""A13 - Tests: branch isolation and replay safety (Epic A: Stories FD-2, FD-4).

Comprehensive integration and security test suite verifying:
1. Multi-Branch Isolation (FD-2):
   - Pairwise read, write, and update isolation across Branch 1 (Maadi),
     Branch 2 (Nasr City), and Branch 3 (6th of October).
   - Cross-branch human approval/rejection blocking across restock approvers
     and sales team actors.
   - Combined AIOutputGuard + BranchIsolation protection against cross-branch
     prompt injection and unauthorized recipient overrides.
   - Branch-scoped isolation of LangGraph checkpoints and TransactionalOutbox
     dispatch queues.

2. Replay Safety (FD-4):
   - Scheduler & webhook event replay deduplication via `IdempotencyKeyStore`
     (replaying N times executes side-effect exactly once).
   - Parameter and branch tampering detection (`IdempotencyConflictError`) when
     an idempotency key is replayed with altered arguments.
   - TransactionalOutbox replay safety: duplicate `enqueue` calls and repeated
     `dispatch_pending` runs send exactly one email/POS webhook.
   - CheckpointedGraphRuntime replay safety: re-invoking `start()` or `resume()`
     on an already-completed workflow run never re-executes graph nodes or
     duplicates outbound side effects.
"""
import unittest
from typing import Any, Dict

from foundation.ai_output_guard import AIOutputGuard
from foundation.approval_service import ApprovalService, InMemoryApprovalBackend, compute_version_hash
from foundation.audit_log_writer import AuditLogWriter, InMemoryAuditBackend
from foundation.auth_branch_middleware import (
    AuthAndBranchIsolationMiddleware,
    BranchIsolationError,
    BranchScopedRepository,
)
from foundation.config_service import ConfigService, InMemoryConfigBackend
from foundation.idempotency_store import (
    IdempotencyConflictError,
    IdempotencyKeyStore,
    InMemoryIdempotencyBackend,
    build_idempotency_key,
)
from foundation.langgraph_runtime import (
    CheckpointedGraphRuntime,
    END_NODE,
    InMemoryCheckpointBackend,
    InMemoryOutboxBackend,
    OutboxMessage,
    TransactionalOutbox,
)


class TestBranchIsolationAndReplaySafetyA13(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ConfigService(backend=InMemoryConfigBackend())
        self.ai_guard = AIOutputGuard(config_service=self.config)
        self.auth = AuthAndBranchIsolationMiddleware()
        self.idem_store = IdempotencyKeyStore(backend=InMemoryIdempotencyBackend())
        self.audit = AuditLogWriter(backend=InMemoryAuditBackend())
        self.approvals = ApprovalService(
            backend=InMemoryApprovalBackend(),
            audit_writer=self.audit,
        )

        # Seed multi-branch inventory across all 3 pilot branches
        self.initial_inventory = [
            {"branch_id": 1, "product_id": 1, "sku": "SKU-0001", "stock_quantity": 1782},
            {"branch_id": 2, "product_id": 1, "sku": "SKU-0001", "stock_quantity": 2077},
            {"branch_id": 3, "product_id": 1, "sku": "SKU-0001", "stock_quantity": 1942},
        ]
        self.inventory_repo = BranchScopedRepository(
            self.auth,
            initial_rows=self.initial_inventory,
        )

    # =========================================================================
    # PART 1: BRANCH ISOLATION TESTS (Story FD-2)
    # =========================================================================
    def test_pairwise_branch_isolation_across_all_three_branches(self) -> None:
        """FD-2: Every branch (1, 2, 3) is strictly isolated from reading, writing, or updating others."""
        contexts = {
            1: self.auth.create_branch_context(
                actor_id="system.scheduler.branch1",
                branch_id=1,
                run_id="run-b1",
            ),
            2: self.auth.create_branch_context(
                actor_id="system.scheduler.branch2",
                branch_id=2,
                run_id="run-b2",
            ),
            3: self.auth.create_branch_context(
                actor_id="system.scheduler.branch3",
                branch_id=3,
                run_id="run-b3",
            ),
        }

        for own_branch, ctx in contexts.items():
            # Own branch read succeeds and returns ONLY own_branch rows
            own_rows = self.inventory_repo.read_all(ctx)
            self.assertEqual(len(own_rows), 1)
            self.assertEqual(own_rows[0]["branch_id"], own_branch)

            for other_branch in (1, 2, 3):
                if other_branch == own_branch:
                    continue

                with self.subTest(own_branch=own_branch, other_branch=other_branch):
                    # 1. Cross-branch read blocked
                    with self.assertRaises(BranchIsolationError):
                        self.inventory_repo.read_all(ctx, requested_branch_id=other_branch)

                    # 2. Cross-branch filter read blocked
                    with self.assertRaises(BranchIsolationError):
                        self.auth.guard_read_query(ctx, filters={"branch_id": other_branch})

                    # 3. Cross-branch write blocked
                    with self.assertRaises(BranchIsolationError):
                        self.inventory_repo.write_row(
                            ctx,
                            {"branch_id": other_branch, "product_id": 99, "stock_quantity": 100},
                            permission="restock:draft",
                        )

                    # 4. Cross-branch update blocked
                    with self.assertRaises(BranchIsolationError):
                        self.inventory_repo.update_rows(
                            ctx,
                            target_branch_id=other_branch,
                            match_fn=lambda r: True,
                            updates={"stock_quantity": 0},
                            permission="restock:draft",
                        )

    def test_cross_branch_human_approver_isolation(self) -> None:
        """FD-2: Branch 1 approver cannot approve or reject restock orders for Branch 2 or 3."""
        maadi_approver = self.auth.authenticate("stock.maadi@supermarket.eg")
        nasr_approver = self.auth.authenticate("stock.nasrcity@supermarket.eg")

        # Maadi approver can approve Branch 1, but is blocked on Branch 2 and Branch 3
        self.auth.authorize(maadi_approver, "restock:approve", target_branch_id=1)
        with self.assertRaises(BranchIsolationError):
            self.auth.authorize(maadi_approver, "restock:approve", target_branch_id=2)
        with self.assertRaises(BranchIsolationError):
            self.auth.authorize(maadi_approver, "restock:reject", target_branch_id=3)

        # Nasr City approver can approve Branch 2, but is blocked on Branch 1
        self.auth.authorize(nasr_approver, "restock:approve", target_branch_id=2)
        with self.assertRaises(BranchIsolationError):
            self.auth.authorize(nasr_approver, "restock:approve", target_branch_id=1)

    def test_ai_prompt_injection_cannot_cross_branch_or_hijack_recipient(self) -> None:
        """FD-1 & FD-2: AI output attempting cross-branch write & recipient hijack is neutralized."""
        ctx_b1 = self.auth.create_branch_context(
            actor_id="system.scheduler.branch1",
            branch_id=1,
            run_id="run-b1-sec",
        )

        # Malicious/hallucinated LLM output tries to target Branch 3 and send to external email
        raw_ai_output = {
            "product_id": 1,
            "requested_quantity": 45,
            "branch_id": 3,
            "recipient": "exfiltrate@attacker.com",
            "cc": "spy@attacker.com",
        }

        sanitized = self.ai_guard.sanitize_ai_output(
            raw_ai_output,
            trusted_branch_id=ctx_b1.branch_id,
            recipient_role="restock_approver",
        )

        # Verify AIOutputGuard locked branch_id=1 and recipient=stock.maadi@supermarket.eg
        self.assertEqual(sanitized.enforced_branch_id, 1)
        self.assertEqual(sanitized.enforced_recipient, "stock.maadi@supermarket.eg")
        self.assertTrue(sanitized.had_blocked_fields)

        # Now pass the sanitized payload into BranchScopedRepository write guard -> succeeds for Branch 1
        written = self.inventory_repo.write_row(
            ctx_b1,
            sanitized.payload,
            permission="restock:draft",
        )
        self.assertEqual(written["branch_id"], 1)
        self.assertEqual(written["recipient"], "stock.maadi@supermarket.eg")

    def test_outbox_and_checkpoint_isolation_between_branches(self) -> None:
        """FD-2: Dispatching pending outbox messages or loading checkpoints is strictly branch-scoped."""
        outbox = TransactionalOutbox(backend=InMemoryOutboxBackend())
        ckpt_backend = InMemoryCheckpointBackend()

        # Enqueue one message for Branch 1 and one for Branch 2
        outbox.enqueue(
            idempotency_key="msg:b1:1",
            branch_id=1,
            channel="EMAIL",
            recipient="stock.maadi@supermarket.eg",
            payload={"branch": 1},
        )
        outbox.enqueue(
            idempotency_key="msg:b2:1",
            branch_id=2,
            channel="EMAIL",
            recipient="stock.nasrcity@supermarket.eg",
            payload={"branch": 2},
        )

        # Dispatching for Branch 1 ONLY sends Branch 1's message; Branch 2 remains PENDING
        b1_sent: list[OutboxMessage] = []
        outbox.dispatch_pending(lambda m: b1_sent.append(m), branch_id=1)
        self.assertEqual(len(b1_sent), 1)
        self.assertEqual(b1_sent[0].branch_id, 1)

        b2_pending = outbox.list_messages(branch_id=2, status="PENDING")
        self.assertEqual(len(b2_pending), 1)
        self.assertEqual(b2_pending[0].branch_id, 2)

        # Checkpoints with the same run_id string on different branches never collide
        graph = CheckpointedGraphRuntime("restock", checkpointer=ckpt_backend, outbox=outbox)
        graph.add_node("Step1", lambda s, ob: {"tag": f"branch-{s['branch_id']}"})
        graph.add_edge("Step1", END_NODE)

        graph.start(run_id="daily-midnight", branch_id=1)
        graph.start(run_id="daily-midnight", branch_id=2)

        ckpt_1 = graph.get_checkpoint("daily-midnight", branch_id=1)
        ckpt_2 = graph.get_checkpoint("daily-midnight", branch_id=2)
        assert ckpt_1 is not None and ckpt_2 is not None
        self.assertEqual(ckpt_1.state["tag"], "branch-1")
        self.assertEqual(ckpt_2.state["tag"], "branch-2")

    # =========================================================================
    # PART 2: REPLAY SAFETY TESTS (Story FD-4)
    # =========================================================================
    def test_duplicate_scheduler_and_webhook_replays_produce_single_side_effect(self) -> None:
        """FD-4: Replaying the same event 5 times executes the side-effect once and returns 1 result."""
        db_writes = {"restock_orders_created": 0}

        def create_restock_order() -> Dict[str, Any]:
            db_writes["restock_orders_created"] += 1
            return {
                "restock_order_id": 9001,
                "branch_id": 1,
                "product_id": 1,
                "requested_quantity": 50,
                "write_count": db_writes["restock_orders_created"],
            }

        idem_key = build_idempotency_key("restock_Order_Create", 1, "2026-11-06", "product_1")
        req_params = {"product_id": 1, "requested_quantity": 50}

        results = [
            self.idem_store.run_once(
                idempotency_key=idem_key,
                workflow_name="restock",
                branch_id=1,
                operation=create_restock_order,
                request_params=req_params,
            )
            for _ in range(5)
        ]

        # Side effect ran only once
        self.assertEqual(db_writes["restock_orders_created"], 1)
        self.assertFalse(results[0].was_replayed)
        for replay_res in results[1:]:
            self.assertTrue(replay_res.was_replayed)
            self.assertEqual(replay_res.response_payload, results[0].response_payload)

        # Replaying the same key with a different branch or mutated quantity raises IdempotencyConflictError
        with self.assertRaises(IdempotencyConflictError):
            self.idem_store.run_once(
                idempotency_key=idem_key,
                workflow_name="restock",
                branch_id=2,
                operation=create_restock_order,
                request_params=req_params,
            )
        with self.assertRaises(IdempotencyConflictError):
            self.idem_store.run_once(
                idempotency_key=idem_key,
                workflow_name="restock",
                branch_id=1,
                operation=create_restock_order,
                request_params={"product_id": 1, "requested_quantity": 999},
            )

    def test_completed_graph_and_outbox_replay_safety(self) -> None:
        """FD-4: Re-running start(), resume(), or dispatch_pending() on a completed run causes zero duplicates."""
        ckpt_backend = InMemoryCheckpointBackend()
        outbox = TransactionalOutbox(backend=InMemoryOutboxBackend())
        node_executions = {"Evaluate": 0, "SendEmail": 0}

        graph = CheckpointedGraphRuntime("restock", checkpointer=ckpt_backend, outbox=outbox)

        def evaluate_node(state: Dict[str, Any], ob: TransactionalOutbox) -> Dict[str, Any]:
            node_executions["Evaluate"] += 1
            return {"qty": 50}

        def send_email_node(state: Dict[str, Any], ob: TransactionalOutbox) -> Dict[str, Any]:
            node_executions["SendEmail"] += 1
            ob.enqueue(
                idempotency_key=f"email:{state['branch_id']}:{state['run_id']}",
                branch_id=state["branch_id"],
                channel="EMAIL",
                recipient="orders@supplier-demo.eg",
                payload={"qty": state["qty"]},
            )
            return {"sent": True}

        graph.add_node("Evaluate", evaluate_node)
        graph.add_node("SendEmail", send_email_node)
        graph.add_edge("Evaluate", "SendEmail")
        graph.add_edge("SendEmail", END_NODE)

        # Initial run completes
        first_ckpt = graph.start(run_id="run-replay-safe", branch_id=1)
        self.assertEqual(first_ckpt.status, "COMPLETED")
        self.assertEqual(node_executions, {"Evaluate": 1, "SendEmail": 1})

        # Replaying start() and resume() after completion is a no-op
        replay_start = graph.start(run_id="run-replay-safe", branch_id=1)
        replay_resume = graph.resume(run_id="run-replay-safe", branch_id=1)
        self.assertEqual(replay_start.status, "COMPLETED")
        self.assertEqual(replay_resume.status, "COMPLETED")
        self.assertEqual(node_executions, {"Evaluate": 1, "SendEmail": 1})

        # Dispatching outbox 3 times in a row sends the email only once
        delivered: list[OutboxMessage] = []
        outbox.dispatch_pending(lambda m: delivered.append(m), branch_id=1)
        outbox.dispatch_pending(lambda m: delivered.append(m), branch_id=1)
        outbox.dispatch_pending(lambda m: delivered.append(m), branch_id=1)
        self.assertEqual(len(delivered), 1)


if __name__ == "__main__":
    unittest.main()
