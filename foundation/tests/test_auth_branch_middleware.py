"""Unit tests for Task A6: Authentication, authorization and branch-isolation middleware (FD-2).

Verifies the Done Condition: 'Branch A run cannot read or write branch B',
plus authentication checks, role-based authorization (RBAC), and cross-branch
row leak protection.
"""
import unittest

from foundation.auth_branch_middleware import (
    ActorPrincipal,
    AuthAndBranchIsolationMiddleware,
    AuthenticationError,
    AuthorizationError,
    BranchIsolationError,
    BranchScopedRepository,
)


class TestAuthAndBranchIsolationA6(unittest.TestCase):
    def setUp(self) -> None:
        self.middleware = AuthAndBranchIsolationMiddleware()
        self.sample_products = [
            {"branch_id": 1, "product_id": 1, "sku": "SKU-0001", "stock_quantity": 1782},
            {"branch_id": 2, "product_id": 1, "sku": "SKU-0001", "stock_quantity": 2077},
            {"branch_id": 3, "product_id": 1, "sku": "SKU-0001", "stock_quantity": 1942},
        ]
        self.repo = BranchScopedRepository(self.middleware, initial_rows=self.sample_products)

        # Create a trusted execution context for Branch 1 (Maadi Branch)
        self.branch_a_ctx = self.middleware.create_branch_context(
            actor_id="system.scheduler.branch1",
            branch_id=1,
            run_id="run-maadi-001",
        )

    def test_branch_a_run_cannot_read_branch_b(self) -> None:
        """Task A6 Done Condition (Part 1): Branch A run cannot read Branch B."""
        # 1. Direct read request for Branch 2 during a Branch 1 run is blocked
        with self.assertRaises(BranchIsolationError):
            self.repo.read_all(self.branch_a_ctx, requested_branch_id=2)

        # 2. Query filter attempting to read Branch 3 during a Branch 1 run is blocked
        with self.assertRaises(BranchIsolationError):
            self.middleware.guard_read_query(
                self.branch_a_ctx,
                filters={"branch_id": 3, "category": "Dairy"},
            )

        # 3. Valid Branch 1 read ONLY returns Branch 1 rows (never Branch 2 or 3)
        rows = self.repo.read_all(self.branch_a_ctx)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["branch_id"], 1)
        self.assertEqual(rows[0]["stock_quantity"], 1782)

    def test_branch_a_run_cannot_write_or_update_branch_b(self) -> None:
        """Task A6 Done Condition (Part 2): Branch A run cannot write Branch B."""
        # 1. Attempting to insert a restock order into Branch 2 during a Branch 1 run is blocked
        with self.assertRaises(BranchIsolationError):
            self.repo.write_row(
                self.branch_a_ctx,
                {"branch_id": 2, "product_id": 1, "requested_quantity": 50},
                permission="restock:draft",
            )

        # 2. Attempting to update rows in Branch 2 during a Branch 1 run is blocked
        with self.assertRaises(BranchIsolationError):
            self.repo.update_rows(
                self.branch_a_ctx,
                target_branch_id=2,
                match_fn=lambda r: r["product_id"] == 1,
                updates={"stock_quantity": 9999},
                permission="restock:draft",
            )

        # Verify Branch 2's data remained completely untouched
        branch_b_ctx = self.middleware.create_branch_context(
            actor_id="system.scheduler.branch2",
            branch_id=2,
            run_id="run-nasr-001",
        )
        b_rows = self.repo.read_all(branch_b_ctx)
        self.assertEqual(len(b_rows), 1)
        self.assertEqual(b_rows[0]["stock_quantity"], 2077)

    def test_cross_branch_row_leak_is_blocked_on_read(self) -> None:
        """If an underlying DB query ever returns a row from Branch B, guard_read_rows blocks it."""
        leaked_rows = [
            {"branch_id": 1, "product_id": 1},
            {"branch_id": 2, "product_id": 1},  # Leaked row from Branch 2
        ]
        with self.assertRaises(BranchIsolationError):
            self.middleware.guard_read_rows(self.branch_a_ctx, leaked_rows)

    def test_authentication_rejects_unknown_or_bad_token(self) -> None:
        """Unauthenticated actors or invalid tokens are rejected with AuthenticationError."""
        with self.assertRaises(AuthenticationError):
            self.middleware.authenticate("unknown.user@evil.com")

        with self.assertRaises(AuthenticationError):
            self.middleware.authenticate(
                "stock.maadi@supermarket.eg",
                auth_token="wrong-token",
                require_token=True,
            )

        valid = self.middleware.authenticate(
            "stock.maadi@supermarket.eg",
            auth_token="tok-stock-maadi",
            require_token=True,
        )
        self.assertEqual(valid.actor_id, "stock.maadi@supermarket.eg")

    def test_authorization_enforces_roles_and_branch_scope(self) -> None:
        """Actors can only perform actions permitted by their role AND within their branch."""
        maadi_approver = self.middleware.authenticate("stock.maadi@supermarket.eg")

        # Allowed: Maadi restock approver can approve restock in Branch 1
        self.middleware.authorize(maadi_approver, "restock:approve", target_branch_id=1)

        # Blocked (RBAC): Restock approver cannot approve promotional offers (Epic D)
        with self.assertRaises(AuthorizationError):
            self.middleware.authorize(maadi_approver, "offer:approve", target_branch_id=1)

        # Blocked (Branch Isolation): Maadi restock approver cannot approve restock in Branch 2 (Nasr City)
        with self.assertRaises(BranchIsolationError):
            self.middleware.authorize(maadi_approver, "restock:approve", target_branch_id=2)


if __name__ == "__main__":
    unittest.main()
