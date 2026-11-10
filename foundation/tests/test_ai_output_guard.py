"""Unit tests for Task A5: Block AI output from setting recipients or branch (FD-1).

Verifies the Done Condition: 'Test: AI-supplied recipient is ignored',
as well as blocking AI-supplied branch overrides, nested dictionary/list
injection attempts, and role-based recipient resolution.
"""
import unittest

from foundation.ai_output_guard import AIOutputGuard
from foundation.config_service import ConfigService, InMemoryConfigBackend


class TestAIOutputGuardA5(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ConfigService(backend=InMemoryConfigBackend())
        self.guard = AIOutputGuard(config_service=self.config)

    def test_ai_supplied_recipient_is_ignored(self) -> None:
        """Task A5 Done Condition: AI-supplied recipient is ignored and replaced by config."""
        ai_payload = {
            "subject": "Urgent Dairy Restock Draft",
            "body": "Please approve 50 units of Juhayna Milk.",
            "recipient": "hacker@malicious-domain.com",
            "to": "OverrideAdmin@evil.org",
            "cc": ["leak1@evil.org", "leak2@evil.org"],
            "bcc": "hidden@evil.org",
            "restock_approver": "fake.approver@supermarket.eg",
        }

        result = self.guard.sanitize_ai_output(
            ai_payload,
            trusted_branch_id=1,  # Maadi Branch
            recipient_role="restock_approver",
        )

        # Authoritative recipient from ConfigService for Branch 1 (Maadi) must win
        self.assertEqual(result.enforced_recipient, "stock.maadi@supermarket.eg")
        self.assertEqual(result.payload["recipient"], "stock.maadi@supermarket.eg")

        # None of the AI's extra email fields remain in the payload
        self.assertNotIn("to", result.payload)
        self.assertNotIn("cc", result.payload)
        self.assertNotIn("bcc", result.payload)
        self.assertNotIn("restock_approver", result.payload)

        # Safe content is preserved intact
        self.assertEqual(result.payload["subject"], "Urgent Dairy Restock Draft")
        self.assertTrue(result.had_blocked_fields)
        self.assertEqual(result.ignored_ai_fields["recipient"], "hacker@malicious-domain.com")

    def test_ai_supplied_branch_is_ignored(self) -> None:
        """Task A5: AI output cannot switch branch_id or branch_name."""
        ai_payload = {
            "product_id": 1,
            "requested_quantity": 40,
            "branch_id": 3,  # AI tries to target Branch 3 (6th of October) during a Branch 2 run
            "branch_name": "6th of October Branch",
            "target_branch": 3,
        }

        result = self.guard.sanitize_ai_output(
            ai_payload,
            trusted_branch_id=2,  # Nasr City Branch run
            recipient_role="restock_approver",
        )

        self.assertEqual(result.enforced_branch_id, 2)
        self.assertEqual(result.payload["branch_id"], 2)
        self.assertEqual(result.payload["recipient"], "stock.nasrcity@supermarket.eg")
        self.assertNotIn("branch_name", result.payload)
        self.assertNotIn("target_branch", result.payload)
        self.assertEqual(result.ignored_ai_fields["branch_id"], 3)

    def test_nested_ai_recipient_and_branch_keys_are_stripped(self) -> None:
        """Nested tool arguments or line items cannot smuggle recipient or branch_id."""
        ai_payload = {
            "draft_report": {
                "title": "Daily Summary",
                "email": "attacker@evil.com",
                "branch_id": 99,
            },
            "orders": [
                {"product_id": 10, "qty": 20, "supplier_email": "spoofed@vendor.com", "branch": 3}
            ],
        }

        result = self.guard.sanitize_ai_output(
            ai_payload,
            trusted_branch_id=3,
            recipient_role="report_recipient",
        )

        self.assertEqual(result.payload["branch_id"], 3)
        self.assertEqual(result.payload["recipient"], "manager.october@supermarket.eg")
        self.assertNotIn("email", result.payload["draft_report"])
        self.assertNotIn("branch_id", result.payload["draft_report"])
        self.assertNotIn("supplier_email", result.payload["orders"][0])
        self.assertNotIn("branch", result.payload["orders"][0])
        self.assertEqual(result.payload["orders"][0]["qty"], 20)
        self.assertIn("draft_report.email", result.ignored_ai_fields)
        self.assertIn("orders[0].supplier_email", result.ignored_ai_fields)

    def test_supplier_role_uses_verified_db_email_not_ai_email(self) -> None:
        """When dispatching an approved order to a supplier, only DB-verified email is used."""
        ai_payload = {
            "order_id": 501,
            "supplier_email": "fake-nile-dairy@phishing.com",
            "recipient": "fake-nile-dairy@phishing.com",
        }

        result = self.guard.sanitize_ai_output(
            ai_payload,
            trusted_branch_id=1,
            recipient_role="supplier",
            verified_supplier_email="orders@supplier-demo.eg",
        )

        self.assertEqual(result.payload["recipient"], "orders@supplier-demo.eg")
        self.assertEqual(result.ignored_ai_fields["supplier_email"], "fake-nile-dairy@phishing.com")

    def test_langgraph_state_wrapper_attaches_audit_metadata(self) -> None:
        """sanitize_langgraph_state returns a clean state dict with _ignored_ai_fields."""
        raw_state_update = {
            "drafted_report": "Order 24 units of Dairy",
            "recipient": "wrong@email.com",
            "branch_id": 2,
        }
        safe_state = self.guard.sanitize_langgraph_state(
            raw_state_update,
            trusted_branch_id=1,
            recipient_role="restock_approver",
        )
        self.assertEqual(safe_state["branch_id"], 1)
        self.assertEqual(safe_state["recipient"], "stock.maadi@supermarket.eg")
        self.assertEqual(safe_state["_ignored_ai_fields"]["recipient"], "wrong@email.com")
        self.assertEqual(safe_state["_ignored_ai_fields"]["branch_id"], 2)


if __name__ == "__main__":
    unittest.main()
