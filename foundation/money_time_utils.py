"""A9 - Decimal money and half-open period utilities (Africa/Cairo to UTC) (Epic A: Story FD-4).

Provides deterministic financial arithmetic and timezone-aware half-open interval
`[start_utc, end_utc)` utilities used across Restocking (Epic B), Branch Reporting
(Epic C), and Offers (Epic D):

1. Decimal Money Utilities:
   - Strictly rejects `float` and `bool` inputs to prevent IEEE-754 floating-point
     representation bugs.
   - Standardizes rounding to 2 decimal places (`0.01` EGP) using `ROUND_HALF_UP`.
   - Provides helpers for line totals, percentage discounts, gross profit, and
     margin percentage calculations.

2. Half-Open Period Utilities (`[start, end)` in `Africa/Cairo` -> `UTC`):
   - Computes daily `[YYYY-MM-DD 00:00:00 Cairo, next_day 00:00:00 Cairo)` and
     monthly `[YYYY-MM-01 00:00:00 Cairo, next_month 01 00:00:00 Cairo)` periods
     and converts both bounds to `UTC` (`timezone.utc`).
   - Correctly handles day edges, month-end edges (28/29/30/31 days, year rollovers),
     and Egypt Daylight Saving Time (`UTC+2` vs `UTC+3`) via `zoneinfo.ZoneInfo`.
   - Provides half-open membership checks (`start_utc <= ts_utc < end_utc`) so a
     transaction at exact midnight belongs to the new period, never double-counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

CAIRO_TZ_NAME = "Africa/Cairo"
CAIRO_TZ = ZoneInfo(CAIRO_TZ_NAME)
UTC_TZ = timezone.utc

MONEY_QUANTIZE = Decimal("0.01")
PCT_QUANTIZE = Decimal("0.01")


class MoneyError(ValueError):
    """Raised when a money or percentage value is invalid, float, or negative where forbidden."""


class PeriodError(ValueError):
    """Raised when a date, timestamp, or period specification is invalid."""


# ---------------------------------------------------------------------------
# 1. Decimal Money Utilities
# ---------------------------------------------------------------------------
def to_decimal(value: Any, *, field_name: str = "amount") -> Decimal:
    """Converts `Decimal`, `int`, or numeric `str` to `Decimal`; rejects `float` and `bool`."""
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise MoneyError(
            f"Field '{field_name}' must be Decimal, int, or numeric string (got {type(value).__name__}: {value!r})."
        )
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MoneyError(f"Field '{field_name}' is not a valid decimal number: {value!r}.") from exc

    if not dec.is_finite():
        raise MoneyError(f"Field '{field_name}' must be a finite decimal, got {value!r}.")
    return dec


def quantize_money(
    value: Any,
    *,
    allow_negative: bool = False,
    field_name: str = "amount",
) -> Decimal:
    """Quantizes a monetary value to `0.01` using `ROUND_HALF_UP`."""
    dec = to_decimal(value, field_name=field_name)
    rounded = dec.quantize(MONEY_QUANTIZE, rounding=ROUND_HALF_UP)
    if not allow_negative and rounded < Decimal("0.00"):
        raise MoneyError(f"Field '{field_name}' cannot be negative ({rounded}).")
    return rounded


def sum_money(values: Iterable[Any], *, allow_negative: bool = True) -> Decimal:
    """Safely sums an iterable of monetary values using exact `Decimal` arithmetic."""
    total = Decimal("0.00")
    for idx, val in enumerate(values):
        total += quantize_money(val, allow_negative=allow_negative, field_name=f"items[{idx}]")
    return quantize_money(total, allow_negative=allow_negative, field_name="sum_total")


def calculate_line_total(
    unit_price: Any,
    quantity: int,
    *,
    discount_amount: Any = Decimal("0.00"),
) -> Decimal:
    """Calculates `(unit_price * quantity) - discount_amount` quantized to `0.01`."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise MoneyError(f"quantity must be a positive integer, got {quantity!r}.")

    price = quantize_money(unit_price, allow_negative=False, field_name="unit_price")
    discount = quantize_money(discount_amount, allow_negative=False, field_name="discount_amount")
    gross = quantize_money(price * Decimal(quantity), allow_negative=False, field_name="gross_amount")

    if discount > gross:
        raise MoneyError(f"discount_amount ({discount}) cannot exceed gross line total ({gross}).")
    return quantize_money(gross - discount, allow_negative=False, field_name="line_total")


def apply_discount_pct(unit_price: Any, discount_pct: Any) -> Decimal:
    """Applies a percentage discount (`0` to `100`) to `unit_price` with `ROUND_HALF_UP`."""
    price = quantize_money(unit_price, allow_negative=False, field_name="unit_price")
    pct = to_decimal(discount_pct, field_name="discount_pct")
    if pct < Decimal("0") or pct > Decimal("100"):
        raise MoneyError(f"discount_pct must be between 0 and 100, got {pct}.")

    multiplier = (Decimal("100") - pct) / Decimal("100")
    return quantize_money(price * multiplier, allow_negative=False, field_name="discounted_price")


def calculate_gross_profit(net_revenue: Any, total_cost: Any) -> Decimal:
    """Calculates `net_revenue - total_cost` (can be negative if sold below cost)."""
    rev = quantize_money(net_revenue, allow_negative=True, field_name="net_revenue")
    cost = quantize_money(total_cost, allow_negative=False, field_name="total_cost")
    return quantize_money(rev - cost, allow_negative=True, field_name="gross_profit")


def calculate_margin_pct(offer_price: Any, cost_price: Any) -> Decimal:
    """Calculates gross margin percentage `((offer_price - cost_price) / offer_price) * 100`."""
    price = quantize_money(offer_price, allow_negative=False, field_name="offer_price")
    cost = quantize_money(cost_price, allow_negative=False, field_name="cost_price")
    if price <= Decimal("0.00"):
        raise MoneyError("offer_price must be strictly positive to compute margin percentage.")
    margin = ((price - cost) / price) * Decimal("100")
    return margin.quantize(PCT_QUANTIZE, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 2. Half-Open Period Utilities (Africa/Cairo -> UTC)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HalfOpenPeriod:
    """Represents a half-open time interval `[start_utc, end_utc)` mapped from local timezone."""
    period_type: str  # "DAILY" or "MONTHLY"
    label: str        # e.g., "2025-01-15" or "2025-02"
    timezone_name: str
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime

    def contains(self, timestamp: datetime) -> bool:
        """Half-open membership check: `True` iff `start_utc <= timestamp_utc < end_utc`.

        Requires a timezone-aware `datetime`. Exact `start_utc` is included;
        exact `end_utc` is excluded (belongs to the next period).
        """
        ts_utc = to_utc(timestamp)
        return self.start_utc <= ts_utc < self.end_utc


def parse_iso_date(target_date: date | str) -> date:
    """Parses a `date` or `YYYY-MM-DD` string into a `date` object (rejecting `datetime`)."""
    if isinstance(target_date, datetime):
        raise PeriodError("Expected a date or 'YYYY-MM-DD' string, got datetime.")
    if isinstance(target_date, date):
        return target_date
    if isinstance(target_date, str):
        try:
            return date.fromisoformat(target_date.strip())
        except ValueError as exc:
            raise PeriodError(f"Invalid ISO date string '{target_date}'. Expected 'YYYY-MM-DD'.") from exc
    raise PeriodError(f"Unsupported date type: {type(target_date).__name__}.")


def to_utc(dt: datetime, *, default_tz: str = CAIRO_TZ_NAME) -> datetime:
    """Converts a timezone-aware datetime to UTC (raises `PeriodError` if naive)."""
    if not isinstance(dt, datetime):
        raise PeriodError(f"Expected datetime, got {type(dt).__name__}.")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise PeriodError("Naive datetime is not allowed; provide a timezone-aware datetime.")
    return dt.astimezone(UTC_TZ)


def localize_cairo_datetime(
    target_date: date | str,
    target_time: time | str = time(0, 0, 0),
    *,
    tz_name: str = CAIRO_TZ_NAME,
) -> tuple[datetime, datetime]:
    """Combines a date and time in `Africa/Cairo` and returns `(local_dt, utc_dt)`."""
    d = parse_iso_date(target_date)
    if isinstance(target_time, str):
        try:
            t = time.fromisoformat(target_time.strip())
        except ValueError as exc:
            raise PeriodError(f"Invalid ISO time string '{target_time}'.") from exc
    elif isinstance(target_time, time):
        t = target_time
    else:
        raise PeriodError(f"Unsupported time type: {type(target_time).__name__}.")

    tz = ZoneInfo(tz_name)
    local_dt = datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, t.microsecond, tzinfo=tz)
    return local_dt, local_dt.astimezone(UTC_TZ)


def resolve_daily_period(
    target_date: date | str,
    *,
    tz_name: str = CAIRO_TZ_NAME,
) -> HalfOpenPeriod:
    """Builds the half-open daily period `[00:00:00 local, next_day 00:00:00 local)` in UTC."""
    day = parse_iso_date(target_date)
    next_day = day + timedelta(days=1)
    tz = ZoneInfo(tz_name)

    start_local = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end_local = datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=tz)

    return HalfOpenPeriod(
        period_type="DAILY",
        label=day.isoformat(),
        timezone_name=tz_name,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_local.astimezone(UTC_TZ),
        end_utc=end_local.astimezone(UTC_TZ),
    )


def resolve_monthly_period(
    year: int,
    month: int,
    *,
    tz_name: str = CAIRO_TZ_NAME,
) -> HalfOpenPeriod:
    """Builds the half-open monthly period `[YYYY-MM-01 00:00 local, next_month-01 00:00 local)` in UTC."""
    if isinstance(year, bool) or not isinstance(year, int) or year < 1970 or year > 9999:
        raise PeriodError(f"Invalid year: {year!r}.")
    if isinstance(month, bool) or not isinstance(month, int) or month < 1 or month > 12:
        raise PeriodError(f"Invalid month: {month!r}. Must be 1..12.")

    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    tz = ZoneInfo(tz_name)
    start_local = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    end_local = datetime(next_year, next_month, 1, 0, 0, 0, tzinfo=tz)

    return HalfOpenPeriod(
        period_type="MONTHLY",
        label=f"{year:04d}-{month:02d}",
        timezone_name=tz_name,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_local.astimezone(UTC_TZ),
        end_utc=end_local.astimezone(UTC_TZ),
    )
