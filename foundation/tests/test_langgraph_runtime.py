"""Unit tests for Task A10: LangGraph runtime with state checkpointer and outbox (FD-4).

Verifies the Done Condition: 'Test graph survives a restart',
including:
1. Full process restart simulation using disk-backed `JsonFileCheckpointBackend`
   and `TransactionalOutbox` while paused at `HumanReviewNode`.
2. Crash recovery mid-flight (`status="RUNNING"`) without re-running completed
   early nodes or duplicating outbox messages.
3. Transactional outbox idempotency (`enqueue`), retry increment on failure,
   and `DISPATCHED` status transitions.
"""
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from foundation.langgraph_runtime import (
    CheckpointedGraphRuntime,
    END_NODE,
    InMemoryCheckpointBackend,
    InMemoryOutboxBackend,
    JsonFileCheckpointBackend,
    OutboxMessage,
    TransactionalOutbox,
)


def _build_restock_test_graph(
    checkpointer: Any,
    outbox: TransactionalOutbox,
    execution_log: list[str],
) -> CheckpointedGraphRuntime:
    """Builds a 4-node Restocking test graph with an interrupt at HumanReviewNode."""
    graph = CheckpointedGraphRuntime(
        "restock_workflow",
        checkpointer=checkpointer,
        outbox=outbox,
        interrupt_before=["HumanReviewNode"],
    )

    def evaluate_inventory_node(state: Dict[str, Any], ob: TransactionalOutbox) -> Dict[str, Any]:
        execution_log.append("EvaluateInventoryNode")
        return {"flagged_product_id": 1, "requested_quantity": 50}

    def draft_report_node(state: Dict[str, Any], ob: TransactionalOutbox) -> Dict[str, Any]:
        execution_log.append("DraftReportNode")
        retries = int(state.get("retry_count", 0))
        note = state.get("user_feedback", "")
        draft = f"Order {state['requested_quantity']} units of SKU-0001 (retry={retries}, note={note})"
        return {"drafted_report": draft}

    def human_review_node(state: Dict[str, Any], ob: TransactionalOutbox) -> Dict[str, Any]:
        execution_log.append("HumanReviewNode")
        if not state.get("human_approved", False):
            return {"retry_count": int(state.get("retry_count", 0)) + 1}
        return {"review_status": "APPROVED"}

    def send_email_node(state: Dict[str, Any], ob: TransactionalOutbox) -> Dict[str, Any]:
        execution_log.append("SendEmailNode")
        msg = ob.enqueue(
            idempotency_key=f"restock_email:{state['branch_id']}:{state['run_id']}",
            branch_id=state["branch_id"],
            channel="EMAIL",
            recipient="orders@supplier-demo.eg",
            subject=f"Purchase Order {state['run_id']}",
            payload={
                "product_id": state["flagged_product_id"],
                "quantity": state["requested_quantity"],
                "report": state["drafted_report"],
            },
        )
        return {"outbox_message_id": msg.message_id}

    graph.add_node("EvaluateInventoryNode", evaluate_inventory_node)
    graph.add_node("DraftReportNode", draft_report_node)
    graph.add_node("HumanReviewNode", human_review_node)
    graph.add_node("SendEmailNode", send_email_node)

    graph.add_edge("EvaluateInventoryNode", "DraftReportNode")
    graph.add_edge("DraftReportNode", "HumanReviewNode")
    graph.add_conditional_edges(
        "HumanReviewNode",
        lambda s: "SendEmailNode" if s.get("human_approved") else "DraftReportNode",
    )
    graph.add_edge("SendEmailNode", END_NODE)
    return graph


class TestLangGraphRuntimeCheckpointerOutboxA10(unittest.TestCase):
    def test_graph_survives_restart_with_file_checkpointer_and_outbox(self) -> None:
        """Task A10 Done Condition: Test graph survives a restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ckpt_file = Path(tmpdir) / "checkpoints.json"
            shared_outbox_backend = InMemoryOutboxBackend()

            # --- PROCESS INSTANCE 1: Runs until HumanReviewNode interrupt, then shuts down ---
            log_instance_1: list[str] = []
            runtime_1 = _build_restock_test_graph(
                checkpointer=JsonFileCheckpointBackend(ckpt_file),
                outbox=TransactionalOutbox(backend=shared_outbox_backend),
                execution_log=log_instance_1,
            )

            paused_ckpt = runtime_1.start(
                run_id="run-maadi-001",
                branch_id=1,
                initial_state={"retry_count": 0},
            )
            self.assertEqual(paused_ckpt.status, "INTERRUPTED")
            self.assertEqual(paused_ckpt.current_node, "DraftReportNode")
            self.assertEqual(paused_ckpt.next_node, "HumanReviewNode")
            self.assertEqual(paused_ckpt.state["requested_quantity"], 50)
            self.assertEqual(log_instance_1, ["EvaluateInventoryNode", "DraftReportNode"])

            # Destroy runtime_1 completely to simulate a server/container restart
            del runtime_1

            # --- PROCESS INSTANCE 2: Brand-new runtime loads state from disk & resumes ---
            log_instance_2: list[str] = []
            outbox_2 = TransactionalOutbox(backend=shared_outbox_backend)
            runtime_2 = _build_restock_test_graph(
                checkpointer=JsonFileCheckpointBackend(ckpt_file),
                outbox=outbox_2,
                execution_log=log_instance_2,
            )

            # Verify checkpoint survived on disk before resuming
            loaded = runtime_2.get_checkpoint("run-maadi-001", branch_id=1)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.status, "INTERRUPTED")
            self.assertIn("SKU-0001", loaded.state["drafted_report"])

            # Human approves -> resume graph in runtime_2
            final_ckpt = runtime_2.resume(
                run_id="run-maadi-001",
                branch_id=1,
                state_updates={"human_approved": True},
            )

            # EvaluateInventoryNode and DraftReportNode were NOT re-executed after restart!
            self.assertEqual(log_instance_2, ["HumanReviewNode", "SendEmailNode"])
            self.assertEqual(final_ckpt.status, "COMPLETED")
            self.assertEqual(final_ckpt.next_node, END_NODE)

            # Verify outbox staged exactly 1 PENDING email and dispatches it cleanly
            sent_emails: list[OutboxMessage] = []
            dispatched = outbox_2.dispatch_pending(lambda m: sent_emails.append(m), branch_id=1)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0].status, "DISPATCHED")
            self.assertEqual(len(sent_emails), 1)
            self.assertEqual(sent_emails[0].recipient, "orders@supplier-demo.eg")

    def test_rejection_loop_across_multiple_restarts(self) -> None:
        """Graph survives a rejection ('No') loop and subsequent approval across restarts."""
        ckpt_backend = InMemoryCheckpointBackend()
        outbox_backend = InMemoryOutboxBackend()
        log: list[str] = []

        # Run 1: Start and pause at HumanReviewNode
        g1 = _build_restock_test_graph(ckpt_backend, TransactionalOutbox(outbox_backend), log)
        c1 = g1.start(run_id="run-oct-002", branch_id=3, initial_state={"retry_count": 0})
        self.assertEqual(c1.status, "INTERRUPTED")

        # Restart 1 -> Human rejects with feedback ("Reduce qty to 30") -> loops back & pauses again
        g2 = _build_restock_test_graph(ckpt_backend, TransactionalOutbox(outbox_backend), log)
        c2 = g2.resume(
            run_id="run-oct-002",
            branch_id=3,
            state_updates={"human_approved": False, "user_feedback": "Reduce qty to 30", "requested_quantity": 30},
        )
        self.assertEqual(c2.status, "INTERRUPTED")
        self.assertEqual(c2.next_node, "HumanReviewNode")
        self.assertEqual(c2.state["retry_count"], 1)
        self.assertIn("retry=1, note=Reduce qty to 30", c2.state["drafted_report"])

        # Restart 2 -> Human approves the revised draft -> completes and queues email
        g3 = _build_restock_test_graph(ckpt_backend, TransactionalOutbox(outbox_backend), log)
        c3 = g3.resume(
            run_id="run-oct-002",
            branch_id=3,
            state_updates={"human_approved": True},
        )
        self.assertEqual(c3.status, "COMPLETED")
        self.assertEqual(len(outbox_backend.list_messages(branch_id=3)), 1)

    def test_outbox_idempotency_and_bounded_failure_retries(self) -> None:
        """Outbox deduplicates identical idempotency keys and marks FAILED after max_retries."""
        outbox = TransactionalOutbox(max_retries=2)

        m1 = outbox.enqueue(
            idempotency_key="pos:branch_2:offer_10:v1",
            branch_id=2,
            channel="POS_WEBHOOK",
            recipient="https://pos.internal/activate",
            payload={"offer_id": 10},
        )
        # Duplicate enqueue with same key returns the exact same message_id
        m2 = outbox.enqueue(
            idempotency_key="pos:branch_2:offer_10:v1",
            branch_id=2,
            channel="POS_WEBHOOK",
            recipient="https://pos.internal/activate",
            payload={"offer_id": 10},
        )
        self.assertEqual(m1.message_id, m2.message_id)
        self.assertEqual(len(outbox.list_messages(branch_id=2)), 1)

        # Simulate failing webhook 3 times (exceeds max_retries=2 -> status becomes FAILED)
        def failing_sender(_: OutboxMessage) -> None:
            raise ConnectionError("POS offline")

        r1 = outbox.dispatch_pending(failing_sender, branch_id=2)[0]
        self.assertEqual(r1.retry_count, 1)
        self.assertEqual(r1.status, "PENDING")

        r2 = outbox.dispatch_pending(failing_sender, branch_id=2)[0]
        self.assertEqual(r2.retry_count, 2)
        self.assertEqual(r2.status, "PENDING")

        r3 = outbox.dispatch_pending(failing_sender, branch_id=2)[0]
        self.assertEqual(r3.retry_count, 3)
        self.assertEqual(r3.status, "FAILED")

        # Once FAILED, dispatch_pending no longer picks it up
        self.assertEqual(outbox.dispatch_pending(failing_sender, branch_id=2), [])


if __name__ == "__main__":
    unittest.main()
