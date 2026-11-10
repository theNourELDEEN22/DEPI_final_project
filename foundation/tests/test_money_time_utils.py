"""Unit tests for Task A9: Decimal money and half-open period utilities (Africa/Cairo to UTC) (FD-4).

Verifies the Done Condition: 'Unit tests pass for day and month edges',
including:
- Half-open daily boundaries (`start` inclusive, `end` exclusive, microsecond before `end` inclusive).
- Half-open monthly boundaries (28-day Feb, 29-day leap Feb, 30-day April, Dec->Jan year rollover).
- Africa/Cairo to UTC offset conversion in both standard time (UTC+2) and Egypt DST (UTC+3).
- Decimal money rounding, float rejection, line totals, discounts, and gross profit/margin math.
"""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from foundation.money_time_utils import (
    CAIRO_TZ,
    MoneyError,
    PeriodError,
    apply_discount_pct,
    calculate_gross_profit,
    calculate_line_total,
    calculate_margin_pct,
    localize_cairo_datetime,
    quantize_money,
    resolve_daily_period,
    resolve_monthly_period,
    sum_money,
)


class TestMoneyAndTimeUtilsA9(unittest.TestCase):
    # -------------------------------------------------------------------------
    # 1. Day Edge Tests (Done Condition Part 1)
    # -------------------------------------------------------------------------
    def test_daily_period_cairo_to_utc_and_half_open_edges(self) -> None:
        """Task A9 Done Condition: Daily half-open edges in Africa/Cairo -> UTC."""
        # 2025-01-15 in Cairo is UTC+2 (winter standard time)
        period = resolve_daily_period("2025-01-15")

        self.assertEqual(period.period_type, "DAILY")
        self.assertEqual(period.label, "2025-01-15")
        self.assertEqual(
            period.start_utc,
            datetime(2025, 1, 14, 22, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            period.end_utc,
            datetime(2025, 1, 15, 22, 0, 0, tzinfo=timezone.utc),
        )

        # Edge 1: 1 microsecond before start (2025-01-14 23:59:59.999999 Cairo) -> Excluded
        before_start = period.start_utc - timedelta(microseconds=1)
        self.assertFalse(period.contains(before_start))

        # Edge 2: Exact start (2025-01-15 00:00:00 Cairo) -> Included
        self.assertTrue(period.contains(period.start_utc))
        self.assertTrue(period.contains(datetime(2025, 1, 15, 0, 0, 0, tzinfo=CAIRO_TZ)))

        # Edge 3: 1 microsecond before end (2025-01-15 23:59:59.999999 Cairo) -> Included
        just_before_end = period.end_utc - timedelta(microseconds=1)
        self.assertTrue(period.contains(just_before_end))

        # Edge 4: Exact end (2025-01-16 00:00:00 Cairo) -> Excluded from Jan 15, Included in Jan 16!
        self.assertFalse(period.contains(period.end_utc))
        next_day_period = resolve_daily_period("2025-01-16")
        self.assertTrue(next_day_period.contains(period.end_utc))
        self.assertEqual(period.end_utc, next_day_period.start_utc)

    def test_daily_period_handles_egypt_summer_dst_utc_plus_3(self) -> None:
        """Verifies Africa/Cairo summer DST offset (UTC+3 in July) converts accurately."""
        summer_period = resolve_daily_period("2025-07-15")
        # In July, Cairo is UTC+3, so 00:00 Cairo is 21:00 UTC the previous day
        self.assertEqual(
            summer_period.start_utc,
            datetime(2025, 7, 14, 21, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            summer_period.end_utc,
            datetime(2025, 7, 15, 21, 0, 0, tzinfo=timezone.utc),
        )

    # -------------------------------------------------------------------------
    # 2. Month Edge Tests (Done Condition Part 2)
    # -------------------------------------------------------------------------
    def test_monthly_period_february_non_leap_and_leap_year_edges(self) -> None:
        """Task A9 Done Condition: Month edges for 28-day Feb (2025) and 29-day leap Feb (2024)."""
        # Non-leap year February 2025 (28 days)
        feb_2025 = resolve_monthly_period(2025, 2)
        self.assertEqual(feb_2025.label, "2025-02")
        self.assertEqual(
            feb_2025.start_utc,
            datetime(2025, 1, 31, 22, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            feb_2025.end_utc,
            datetime(2025, 2, 28, 22, 0, 0, tzinfo=timezone.utc),
        )
        # Feb 28 23:59:59 Cairo is inside Feb; Mar 1 00:00:00 Cairo is outside Feb
        self.assertTrue(feb_2025.contains(datetime(2025, 2, 28, 23, 59, 59, tzinfo=CAIRO_TZ)))
        self.assertFalse(feb_2025.contains(datetime(2025, 3, 1, 0, 0, 0, tzinfo=CAIRO_TZ)))

        # Leap year February 2024 (29 days)
        feb_2024 = resolve_monthly_period(2024, 2)
        self.assertTrue(feb_2024.contains(datetime(2024, 2, 29, 23, 59, 59, tzinfo=CAIRO_TZ)))
        self.assertFalse(feb_2024.contains(datetime(2024, 3, 1, 0, 0, 0, tzinfo=CAIRO_TZ)))

    def test_monthly_period_december_to_january_year_rollover_edge(self) -> None:
        """Task A9 Done Condition: December 2025 -> January 2026 year boundary."""
        dec_2025 = resolve_monthly_period(2025, 12)
        jan_2026 = resolve_monthly_period(2026, 1)

        self.assertEqual(dec_2025.end_utc, jan_2026.start_utc)
        self.assertEqual(
            dec_2025.end_utc,
            datetime(2025, 12, 31, 22, 0, 0, tzinfo=timezone.utc),
        )

        # Last second of 2025 in Cairo -> in Dec 2025, not Jan 2026
        last_sec_2025 = datetime(2025, 12, 31, 23, 59, 59, tzinfo=CAIRO_TZ)
        self.assertTrue(dec_2025.contains(last_sec_2025))
        self.assertFalse(jan_2026.contains(last_sec_2025))

        # First second of 2026 in Cairo -> in Jan 2026, not Dec 2025
        first_sec_2026 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=CAIRO_TZ)
        self.assertFalse(dec_2025.contains(first_sec_2026))
        self.assertTrue(jan_2026.contains(first_sec_2026))

    def test_naive_datetime_and_bad_month_rejected(self) -> None:
        period = resolve_daily_period("2025-01-01")
        with self.assertRaises(PeriodError):
            period.contains(datetime(2025, 1, 1, 12, 0, 0))  # Naive datetime rejected
        with self.assertRaises(PeriodError):
            resolve_monthly_period(2025, 13)  # Month 13 rejected

        local_dt, utc_dt = localize_cairo_datetime("2025-01-01", "10:05:00")
        self.assertEqual(local_dt.hour, 10)
        self.assertEqual(utc_dt.hour, 8)

    # -------------------------------------------------------------------------
    # 3. Decimal Money Utilities Tests
    # -------------------------------------------------------------------------
    def test_decimal_money_rounding_and_float_rejection(self) -> None:
        # Float and bool rejected
        with self.assertRaises(MoneyError):
            quantize_money(19.04)
        with self.assertRaises(MoneyError):
            quantize_money(True)

        # ROUND_HALF_UP verification (e.g., 10.005 -> 10.01, 10.004 -> 10.00)
        self.assertEqual(quantize_money("10.005"), Decimal("10.01"))
        self.assertEqual(quantize_money("10.004"), Decimal("10.00"))

    def test_line_total_discount_profit_and_margin_calculations(self) -> None:
        # 4 units * 63.90 EGP = 255.60 EGP (matches sales_transactions_6th_October row 0)
        self.assertEqual(calculate_line_total("63.90", 4), Decimal("255.60"))
        self.assertEqual(
            calculate_line_total("63.90", 4, discount_amount="15.60"),
            Decimal("240.00"),
        )

        # 20% discount on 29.25 EGP -> 23.40 EGP
        self.assertEqual(apply_discount_pct("29.25", "20"), Decimal("23.40"))

        # Sum money list
        self.assertEqual(sum_money(["255.60", "49.05", "122.40"]), Decimal("427.05"))

        # Gross profit & margin percentage
        self.assertEqual(calculate_gross_profit("100.00", "75.00"), Decimal("25.00"))
        self.assertEqual(calculate_margin_pct("100.00", "75.00"), Decimal("25.00"))


if __name__ == "__main__":
    unittest.main()
