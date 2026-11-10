"""Unit tests for Task A16: Run duplicate-scheduler and stale-data failure tests (FD-4).

Verifies the Done Condition: 'No duplicate emails, reports or offers',
covering:
1. Duplicate-scheduler bursts (firing 5x in a row) across Restocking, Branch
   Reporting, and Promotional Offers -> produces exactly 1 restock email,
   1 branch report email, and 1 POS offer activation.
2. Stale inventory snapshot blocking in Restocking (prevents unverified orders
   and recovers cleanly without duplicates once inventory syncs).
3. Stale upstream POS transaction blocking in Branch Reporting (prevents
   premature/duplicate reports when POS sync lags behind `period.end_utc`, then
   sends exactly 1 report once sync catches up).
4. Stale cost/expiry and version-mismatch blocking before POS Offer Activation
   (blocks activation when live cost erodes margin or batch expires after approval).
"""
import unittest
from datetime import datetime, timezone

from foundation.approval_service import VersionMismatchError
from foundation.failure_test_harness import (
    DuplicateSchedulerAndStaleDataHarness,
    StaleDataError,
)


class TestDuplicateSchedulerAndStaleDataA16(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = DuplicateSchedulerAndStaleDataHarness()

    def test_duplicate_scheduler_bursts_produce_no_duplicate_emails_reports_or_offers(self) -> None:
        """Task A16 Done Condition: No duplicate emails, reports or offers under 5x scheduler bursts."""
        eval_time = datetime(2026, 11, 7, 0, 5, 0, tzinfo=timezone.utc)
        fresh_sync = datetime(2026, 11, 7, 0, 0, 0, tzinfo=timezone.utc)

        # 1. Fire Restocking scheduler 5 times for Branch 1 on 2026-11-07
        restock_results = [
            self.harness.trigger_scheduled_restock_run(
                branch_id=1,
                schedule_date="2026-11-07",
                snapshot_synced_at=fresh_sync,
                evaluation_time=eval_time,
                auto_approve_by="stock.maadi@supermarket.eg",
            )
            for _ in range(5)
        ]

        # 2. Fire Daily Branch Report scheduler 5 times for Branch 1 covering 2026-11-06
        # Note: 2026-11-06 daily period in Cairo ends at 2026-11-06 22:00:00 UTC
        pos_synced_after_midnight = datetime(2026, 11, 6, 23, 0, 0, tzinfo=timezone.utc)
        report_results = [
            self.harness.trigger_scheduled_daily_report(
                branch_id=1,
                report_date="2026-11-06",
                latest_pos_sync_utc=pos_synced_after_midnight,
            )
            for _ in range(5)
        ]

        # 3. Fire Promotional Offer POS activation scheduler 5 times for Branch 1 Offer #501
        offer_results = [
            self.harness.trigger_scheduled_offer_activation(
                branch_id=1,
                offer_id=501,
                product_id=1,
                campaign_date="2026-11-07",
                approved_version=1,
                current_version=1,
                approved_offer_price="25.00",
                approved_cost_price="20.23",
                live_cost_price="20.23",
                days_until_expiry=5,
            )
            for _ in range(5)
        ]

        # Verify: All 5 restock calls returned the exact same order, and ONLY 1 supplier email was sent
        self.assertEqual(len({r["restock_order_id"] for r in restock_results}), 1)
        supplier_emails = [
            m for m in self.harness.dispatched_emails if m.recipient == "orders@niledairy.eg"
        ]
        self.assertEqual(len(supplier_emails), 1)

        # Verify: Only 1 branch report was generated and ONLY 1 report email was sent
        self.assertEqual(len(self.harness.report_runs), 1)
        report_emails = [
            m for m in self.harness.dispatched_emails if m.recipient == "manager.maadi@supermarket.eg"
        ]
        self.assertEqual(len(report_emails), 1)
        self.assertEqual(len({r["report_id"] for r in report_results}), 1)

        # Verify: Only 1 offer was activated and ONLY 1 POS webhook was dispatched
        self.assertEqual(len(self.harness.active_offers), 1)
        self.assertEqual(len(self.harness.dispatched_webhooks), 1)
        self.assertEqual(len({o["offer_id"] for o in offer_results}), 1)

    def test_stale_inventory_blocks_restocking_until_fresh_sync_arrives(self) -> None:
        """Stale inventory (>30m old) is blocked; subsequent fresh retry sends exactly 1 email."""
        eval_time = datetime(2026, 11, 7, 0, 0, 0, tzinfo=timezone.utc)
        stale_sync = datetime(2026, 11, 6, 20, 0, 0, tzinfo=timezone.utc)  # 4 hours old

        # Stale run is blocked with StaleDataError -> 0 emails dispatched
        with self.assertRaises(StaleDataError):
            self.harness.trigger_scheduled_restock_run(
                branch_id=1,
                schedule_date="2026-11-07",
                snapshot_synced_at=stale_sync,
                evaluation_time=eval_time,
                auto_approve_by="stock.maadi@supermarket.eg",
            )
        self.assertEqual(len(self.harness.dispatched_emails), 0)
        self.assertEqual(self.harness.stale_blocks_count, 1)

        # Inventory sync catches up -> scheduler retries twice -> sends exactly 1 email
        fresh_sync = datetime(2026, 11, 6, 23, 55, 0, tzinfo=timezone.utc)
        for _ in range(2):
            self.harness.trigger_scheduled_restock_run(
                branch_id=1,
                schedule_date="2026-11-07",
                snapshot_synced_at=fresh_sync,
                evaluation_time=eval_time,
                auto_approve_by="stock.maadi@supermarket.eg",
            )
        self.assertEqual(len(self.harness.dispatched_emails), 1)

    def test_stale_pos_sync_blocks_daily_report_until_period_complete(self) -> None:
        """Stale POS data (before 22:00 UTC Cairo midnight) blocks report; fresh retry sends 1 report."""
        # For report_date 2026-11-06 in Africa/Cairo (UTC+2), period ends at 2026-11-06 22:00:00 UTC.
        lagging_pos_sync = datetime(2026, 11, 6, 21, 30, 0, tzinfo=timezone.utc)

        with self.assertRaises(StaleDataError):
            self.harness.trigger_scheduled_daily_report(
                branch_id=1,
                report_date="2026-11-06",
                latest_pos_sync_utc=lagging_pos_sync,
            )
        self.assertEqual(len(self.harness.report_runs), 0)
        self.assertEqual(len(self.harness.dispatched_emails), 0)

        # Once POS sync passes 22:00:00 UTC, even if scheduler fires 3 times, only 1 report is sent
        complete_pos_sync = datetime(2026, 11, 6, 22, 15, 0, tzinfo=timezone.utc)
        for _ in range(3):
            self.harness.trigger_scheduled_daily_report(
                branch_id=1,
                report_date="2026-11-06",
                latest_pos_sync_utc=complete_pos_sync,
            )
        self.assertEqual(len(self.harness.report_runs), 1)
        self.assertEqual(len(self.harness.dispatched_emails), 1)

    def test_stale_offer_cost_expiry_or_version_mismatch_blocks_pos_activation(self) -> None:
        """Pre-activation recheck blocks offers when cost spikes, batch expires, or version changes."""
        # Case 1: Live cost spiked from 20.23 to 24.50 after approval -> invalidates hash & margin
        with self.assertRaises(VersionMismatchError):
            self.harness.trigger_scheduled_offer_activation(
                branch_id=1,
                offer_id=601,
                product_id=1,
                campaign_date="2026-11-07",
                approved_version=1,
                current_version=1,
                approved_offer_price="25.00",
                approved_cost_price="20.23",
                live_cost_price="24.50",
                days_until_expiry=5,
            )
        self.assertEqual(len(self.harness.active_offers), 0)
        self.assertEqual(len(self.harness.dispatched_webhooks), 0)

        # Case 2: Batch expired (days_until_expiry=0) between approval and POS activation
        with self.assertRaises(StaleDataError):
            self.harness.trigger_scheduled_offer_activation(
                branch_id=1,
                offer_id=602,
                product_id=1,
                campaign_date="2026-11-07",
                approved_version=1,
                current_version=1,
                approved_offer_price="25.00",
                approved_cost_price="20.23",
                live_cost_price="20.23",
                days_until_expiry=0,
            )
        self.assertEqual(len(self.harness.active_offers), 0)
        self.assertEqual(len(self.harness.dispatched_webhooks), 0)


if __name__ == "__main__":
    unittest.main()
