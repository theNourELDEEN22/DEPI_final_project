"""E5 - Numeric demand forecast and baseline method (Epic E).

Method (simple, explainable, and the same for forecast and baseline):

1. Look back `lookback_days` (default 28) before the window starts, per branch and
   product, by local Cairo date.
2. Drop every day on which the product sold under an offer: promotions would
   inflate "normal" demand. Days with no sales count as 0 units.
3. Need at least `min_history_days` (default 7) clean days, otherwise there is NO
   forecast (fail closed: Offers refuses a candidate without one).
4. base rate = clean units / clean days.
   weekday index = (average units on that weekday) / base rate, shrunk toward 1:
       index = (n x raw + k x 1) / (n + k),  n = clean days of that weekday, k = weekday_shrinkage
   With 28 days there are only ~4 samples per weekday, so the raw ratio is mostly
   noise (a random 0.5 would halve a forecast). Shrinkage keeps real weekly
   patterns but damps noise; with no samples the index is exactly 1.
5. Forecast for a window [start, end) = sum over each local day the window
   touches of base rate x weekday index x (share of that day inside the window).
   Rounded half-up to 0.01 units.

Baseline (offers.impact.BaselineReader): what the window would have sold WITHOUT
the offer = the forecast units, priced at the average non-promo net unit price and
costed at the current unit cost, summed over the offer's products.

ForecastStore implements offers.review.ForecastReader, so Offers "can read a
forecast number" (E5 done-when) by reference.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from offers.guardrails import PriceReader
from offers.impact import Metrics
from offers.review import Forecast

CAIRO = ZoneInfo("Africa/Cairo")
TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class ForecastConfig:
    # Placeholders until the Week 1 decisions meeting (E1, decision D-09).
    lookback_days: int = 28
    min_history_days: int = 7
    horizon_days: int = 7  # the "expected demand" an offer candidate is reviewed against
    weekday_shrinkage: int = 4  # pseudo-days pulling each weekday index toward 1

    def __post_init__(self) -> None:
        if not 1 <= self.min_history_days <= self.lookback_days:
            raise ValueError("need 1 <= min_history_days <= lookback_days")
        if self.weekday_shrinkage < 0:
            raise ValueError("weekday_shrinkage cannot be negative")
        if self.horizon_days < 1:
            raise ValueError("horizon_days must be at least 1")


@dataclass(frozen=True)
class DemandProfile:
    base_rate: Decimal           # units per day
    weekday_index: dict[int, Decimal]
    clean_days: int
    avg_net_price: Optional[Decimal]


def _key(branch_id: Any, product_id: Any) -> tuple[str, str]:
    return str(branch_id), str(product_id)


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


class DemandForecaster:
    def __init__(self, sales_lines: Iterable[dict[str, Any]], config: ForecastConfig = ForecastConfig()) -> None:
        self.config = config
        self._units: dict[tuple[str, str], dict[date, int]] = defaultdict(lambda: defaultdict(int))
        self._revenue: dict[tuple[str, str], dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        self._promo_days: dict[tuple[str, str], set[date]] = defaultdict(set)
        for line in sales_lines:
            key = _key(line["branch_id"], line["product_id"])
            day = line["date"] if isinstance(line["date"], date) else date.fromisoformat(str(line["date"]))
            if line.get("offer_id"):
                self._promo_days[key].add(day)
            self._units[key][day] += int(line["quantity"])
            self._revenue[key][day] += Decimal(str(line["total_amount"]))

    def profile(self, branch_id: Any, product_id: Any, before: date) -> Optional[DemandProfile]:
        key = _key(branch_id, product_id)
        promo = self._promo_days.get(key, set())
        days = [before - timedelta(days=n) for n in range(1, self.config.lookback_days + 1)]
        clean = [d for d in days if d not in promo]
        if len(clean) < self.config.min_history_days:
            return None
        units = self._units.get(key, {})
        revenue = self._revenue.get(key, {})
        total_units = sum(units.get(d, 0) for d in clean)
        base = Decimal(total_units) / Decimal(len(clean))
        index: dict[int, Decimal] = {}
        k = Decimal(self.config.weekday_shrinkage)
        for weekday in range(7):
            same = [d for d in clean if d.weekday() == weekday]
            n = Decimal(len(same))
            if not same or base == 0 or n + k == 0:
                index[weekday] = Decimal(1)
                continue
            raw = Decimal(sum(units.get(d, 0) for d in same)) / n / base
            index[weekday] = (n * raw + k) / (n + k)
        total_revenue = sum((revenue.get(d, Decimal(0)) for d in clean), Decimal(0))
        price = total_revenue / Decimal(total_units) if total_units else None
        return DemandProfile(base, index, len(clean), price)

    def forecast_units(self, branch_id: Any, product_id: Any, start: datetime, end: datetime) -> Optional[Decimal]:
        if end <= start:
            raise ValueError("the forecast window is empty")
        profile = self.profile(branch_id, product_id, start.astimezone(CAIRO).date())
        if profile is None:
            return None
        total = Decimal(0)
        for day, share in _day_shares(start, end):
            total += profile.base_rate * profile.weekday_index[day.weekday()] * share
        return _q(total)


def _day_shares(start: datetime, end: datetime) -> list[tuple[date, Decimal]]:
    """Each local Cairo day touched by [start, end) and the share of it inside the window
    (days are not always 24 h: Egypt has daylight saving time)."""
    shares = []
    day = start.astimezone(CAIRO).date()
    while True:
        day_start = datetime.combine(day, time.min, CAIRO)
        day_end = datetime.combine(day + timedelta(days=1), time.min, CAIRO)
        if day_start >= end:
            break
        overlap = min(end, day_end) - max(start, day_start)
        if overlap > timedelta(0):
            shares.append((day, Decimal(overlap.total_seconds()) / Decimal((day_end - day_start).total_seconds())))
        day += timedelta(days=1)
    return shares


class ForecastStore:
    """offers.review.ForecastReader backed by DemandForecaster. Refs are deterministic."""

    def __init__(self, forecaster: DemandForecaster, clock: Callable[[], datetime]) -> None:
        self._forecaster = forecaster
        self._clock = clock
        self._data: dict[str, Forecast] = {}

    @staticmethod
    def ref_for(branch_id: Any, product_id: Any, start: datetime, end: datetime) -> str:
        fmt = "%Y%m%dT%H%MZ"
        utc_start = start.astimezone(ZoneInfo("UTC")).strftime(fmt)
        utc_end = end.astimezone(ZoneInfo("UTC")).strftime(fmt)
        return f"fc:{branch_id}:{product_id}:{utc_start}:{utc_end}"

    def create(self, branch_id: Any, product_id: Any, start: datetime, end: datetime) -> Optional[Forecast]:
        ref = self.ref_for(branch_id, product_id, start, end)
        if ref in self._data:
            return self._data[ref]
        units = self._forecaster.forecast_units(branch_id, product_id, start, end)
        if units is None:
            return None
        forecast = Forecast(ref=ref, expected_units=units, generated_at=self._clock())
        self._data[ref] = forecast
        return forecast

    def create_horizon(self, branch_id: Any, product_id: Any, now: datetime) -> Optional[Forecast]:
        """Expected demand over the next horizon_days: what a candidate is reviewed against."""
        horizon = timedelta(days=self._forecaster.config.horizon_days)
        return self.create(branch_id, product_id, now, now + horizon)

    def get_forecast(self, ref: str) -> Optional[Forecast]:
        return self._data.get(ref)


class BaselineService:
    """offers.impact.BaselineReader: forecast units x normal net price, minus unit cost."""

    def __init__(self, forecaster: DemandForecaster, prices: PriceReader) -> None:
        self._forecaster = forecaster
        self._prices = prices

    def get_baseline(
        self, offer_id: str, version: int, branch_id: str, product_ids: list[str], start: datetime, end: datetime
    ) -> Optional[Metrics]:
        units_total, revenue, cost = Decimal(0), Decimal(0), Decimal(0)
        before = start.astimezone(CAIRO).date()
        for product_id in product_ids:
            profile = self._forecaster.profile(branch_id, product_id, before)
            units = self._forecaster.forecast_units(branch_id, product_id, start, end)
            info = self._prices.get_price_info(str(branch_id), str(product_id))
            if profile is None or units is None or info is None or profile.avg_net_price is None:
                return None  # never guess a baseline
            units_total += units
            revenue += units * profile.avg_net_price
            cost += units * info.cost
        return Metrics(
            units=int(units_total.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            net_revenue=_q(revenue),
            gross_profit=_q(revenue - cost),
        )
