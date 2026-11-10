"""Unit tests for Task A8: Idempotency key store (FD-4).

Verifies the Done Condition: 'Replaying the same event gives one result',
plus conflict detection when the same key is reused with mutated parameters,
failure recovery (failed runs are not cached), and canonical key generation.
"""
import unittest

from foundation.idempotency_store import (
    IdempotencyConflictError,
    IdempotencyKeyStore,
    InMemoryIdempotencyBackend,
    build_idempotency_key,
)


class TestIdempotencyKeyStoreA8(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InMemoryIdempotencyBackend()
        self.store = IdempotencyKeyStore(backend=self.backend)

    def test_replaying_same_event_gives_one_result_and_executes_once(self) -> None:
        """Task A8 Done Condition: Replaying the same event gives one result."""
        side_effect_counter = {"emails_sent": 0}

        def dispatch_supplier_order() -> dict:
            side_effect_counter["emails_sent"] += 1
            return {
                "order_id": 1001,
                "status": "DISPATCHED",
                "dispatch_sequence": side_effect_counter["emails_sent"],
            }

        key = build_idempotency_key("restock_email", 1, "order_1001", "v1")
        params = {"order_id": 1001, "supplier_id": 1, "requested_quantity": 50}

        # First call: executes side effect
        first = self.store.run_once(
            idempotency_key=key,
            workflow_name="restock",
            branch_id=1,
            operation=dispatch_supplier_order,
            request_params=params,
        )
        self.assertFalse(first.was_replayed)
        self.assertEqual(side_effect_counter["emails_sent"], 1)
        self.assertEqual(first.response_payload["order_id"], 1001)
        self.assertEqual(first.response_payload["dispatch_sequence"], 1)

        # Replay 1 & Replay 2: duplicate scheduler or webhook retry
        second = self.store.run_once(
            idempotency_key=key,
            workflow_name="restock",
            branch_id=1,
            operation=dispatch_supplier_order,
            request_params=params,
        )
        third = self.store.run_once(
            idempotency_key=key,
            workflow_name="restock",
            branch_id=1,
            operation=dispatch_supplier_order,
            request_params=params,
        )

        # Side effect MUST still have executed only once, and result is identical
        self.assertTrue(second.was_replayed)
        self.assertTrue(third.was_replayed)
        self.assertEqual(side_effect_counter["emails_sent"], 1)
        self.assertEqual(second.response_payload, first.response_payload)
        self.assertEqual(third.response_payload, first.response_payload)

    def test_replaying_same_key_with_different_payload_raises_conflict(self) -> None:
        """Prevents silent bugs when a key is accidentally reused for different parameters."""
        key = build_idempotency_key("pos_activation", 2, "offer_55", "v1")

        self.store.run_once(
            idempotency_key=key,
            workflow_name="offers",
            branch_id=2,
            operation=lambda: {"activated": True, "discount_pct": 20},
            request_params={"offer_id": 55, "discount_pct": 20},
        )

        # Replaying the exact same key with a different discount_pct or branch_id is blocked
        with self.assertRaises(IdempotencyConflictError):
            self.store.run_once(
                idempotency_key=key,
                workflow_name="offers",
                branch_id=2,
                operation=lambda: {"activated": True, "discount_pct": 35},
                request_params={"offer_id": 55, "discount_pct": 35},
            )

        with self.assertRaises(IdempotencyConflictError):
            self.store.run_once(
                idempotency_key=key,
                workflow_name="offers",
                branch_id=3,  # Different branch!
                operation=lambda: {"activated": True, "discount_pct": 20},
                request_params={"offer_id": 55, "discount_pct": 20},
            )

    def test_failed_execution_is_not_cached_and_can_succeed_on_retry(self) -> None:
        """If the operation raises an exception, no record is stored so a retry can succeed."""
        key = build_idempotency_key("daily_report", 1, "2026-11-05")
        attempts = {"count": 0}

        def flaky_report_run() -> dict:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise ConnectionError("Temporary database glitch")
            return {"report_run_id": "rep-001", "status": "SENT"}

        with self.assertRaises(ConnectionError):
            self.store.run_once(
                idempotency_key=key,
                workflow_name="reporting",
                branch_id=1,
                operation=flaky_report_run,
            )

        # Key must not be locked after a failed attempt
        self.assertIsNone(self.store.get(key))

        # Next attempt executes and caches the successful result
        recovered = self.store.run_once(
            idempotency_key=key,
            workflow_name="reporting",
            branch_id=1,
            operation=flaky_report_run,
        )
        self.assertFalse(recovered.was_replayed)
        self.assertEqual(recovered.response_payload["status"], "SENT")
        self.assertEqual(attempts["count"], 2)

    def test_build_idempotency_key_formatting_and_validation(self) -> None:
        key = build_idempotency_key("Restock_Daily", 3, "2026-11-05", "batch_1")
        self.assertEqual(key, "restock_daily:branch_3:2026-11-05:batch_1")

        with self.assertRaises(ValueError):
            build_idempotency_key("", 1, "2026-11-05")
        with self.assertRaises(ValueError):
            build_idempotency_key("restock", -1, "2026-11-05")
        with self.assertRaises(ValueError):
            build_idempotency_key("restock", 1)


if __name__ == "__main__":
    unittest.main()
