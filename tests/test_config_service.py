"""Unit tests for Task A4: Config Service (FD-1).

Verifies the Done Condition: 'Changing a threshold changes the next run',
plus category/subcategory resolution, low-demand protection, branch recipients,
schedules, timezone, and offer guardrail limits.
"""
import unittest
from decimal import Decimal

from foundation.config_service import (
    ConfigService,
    InMemoryConfigBackend,
)


class TestConfigServiceA4(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InMemoryConfigBackend()
        self.service = ConfigService(backend=self.backend)

    def test_changing_threshold_changes_next_run_immediately(self) -> None:
        """Task A4 Done Condition: Changing a threshold changes the next run."""
        # Run 1: Frozen Chicken has high_demand_qty = 50. Stock of 55 does NOT trigger restock.
        run_1 = self.service.evaluate_restock_need(
            stock_quantity=55,
            days_until_expiry=40,
            demand_level="High",
            category="Frozen Food",
            subcategory="Frozen Chicken",
        )
        self.assertFalse(run_1.should_restock)
        self.assertIsNone(run_1.trigger_reason)

        # Change the threshold for Frozen Chicken high_demand_qty from 50 -> 60
        self.service.update_restock_threshold(
            subcategory="Frozen Chicken",
            high_demand_qty=60,
            updated_by="manager_maadi",
        )

        # Run 2 (Next Run): With the exact same stock (55), it NOW triggers restock!
        run_2 = self.service.evaluate_restock_need(
            stock_quantity=55,
            days_until_expiry=40,
            demand_level="High",
            category="Frozen Food",
            subcategory="Frozen Chicken",
        )
        self.assertTrue(run_2.should_restock)
        self.assertEqual(run_2.trigger_reason, "HIGH_DEMAND_LOW_STOCK")
        self.assertEqual(run_2.applied_threshold.high_demand_qty, 60)

    def test_changing_expiry_threshold_changes_next_run(self) -> None:
        """Changing expiry_days threshold immediately alters near-expiry flagging on next run."""
        # Dairy default expiry_days is 3. A batch with 5 days left is NOT flagged.
        run_1 = self.service.evaluate_restock_need(
            stock_quantity=200,
            days_until_expiry=5,
            demand_level="Medium",
            category="Dairy",
        )
        self.assertFalse(run_1.should_restock)

        # Update Dairy expiry_days to 6
        self.service.update_restock_threshold(category="Dairy", expiry_days=6)

        # Next run flags the 5-day batch as NEAR_EXPIRY
        run_2 = self.service.evaluate_restock_need(
            stock_quantity=200,
            days_until_expiry=5,
            demand_level="Medium",
            category="Dairy",
        )
        self.assertTrue(run_2.should_restock)
        self.assertEqual(run_2.trigger_reason, "NEAR_EXPIRY")

    def test_low_demand_never_triggers_on_quantity_alone(self) -> None:
        """RS-3 / B6 rule: Low demand never triggers on quantity alone, only on expiry."""
        eval_qty = self.service.evaluate_restock_need(
            stock_quantity=1,
            days_until_expiry=30,
            demand_level="Low",
            category="Bakery",
        )
        self.assertFalse(eval_qty.should_restock)

        eval_exp = self.service.evaluate_restock_need(
            stock_quantity=1,
            days_until_expiry=2,
            demand_level="Low",
            category="Bakery",
        )
        self.assertTrue(eval_exp.should_restock)
        self.assertEqual(eval_exp.trigger_reason, "NEAR_EXPIRY")

    def test_schedules_and_timezone(self) -> None:
        sched = self.service.get_schedules()
        self.assertEqual(sched.restock_cron, "0 0 * * *")
        self.assertEqual(sched.daily_report_cron, "0 1 * * *")
        self.assertEqual(self.service.get_timezone(), "Africa/Cairo")

    def test_branch_scoped_recipients(self) -> None:
        r1 = self.service.get_recipients(branch_id=1)
        r2 = self.service.get_recipients(branch_id=2)
        r3 = self.service.get_recipients(branch_id=3)
        self.assertEqual(r1.restock_approver, "stock.maadi@supermarket.eg")
        self.assertEqual(r2.restock_approver, "stock.nasrcity@supermarket.eg")
        self.assertEqual(r3.report_recipient, "manager.october@supermarket.eg")

    def test_offer_guardrails_and_live_update(self) -> None:
        g1 = self.service.get_offer_guardrails()
        self.assertEqual(g1.max_discount_percent, Decimal("40.0"))
        self.assertEqual(g1.min_margin_percent, Decimal("5.0"))

        self.service.update_offer_guardrails(max_discount_pct=Decimal("35.0"), min_margin_pct=Decimal("8.5"))
        g2 = self.service.get_offer_guardrails()
        self.assertEqual(g2.max_discount_percent, Decimal("35.0"))
        self.assertEqual(g2.min_margin_percent, Decimal("8.5"))


if __name__ == "__main__":
    unittest.main()
