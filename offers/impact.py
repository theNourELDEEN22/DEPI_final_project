"""D12 - Impact calculation vs baseline using M3's KPI functions (Epic D: Offers).

For a campaign that ran (ACTIVE now, or STOPPED / ENDED), compares what really
happened with the baseline, for the offer's products in its branch over the
campaign window [start, end) (half-open, like A9).

Rule from the plan (H4): Offers calls the SAME KPI functions as the reports.
So units, net revenue and gross profit are never recomputed here: they are read
through KpiReader (M3's functions, task C14) and the baseline through
BaselineReader (M5's baseline method, task E5). This module only subtracts and
divides. If any figure is missing the impact is refused, never guessed.

Definitions (to confirm with the team):
- incremental = actual - baseline, for units, net revenue and gross profit.
- discount cost = redemptions x average discount per redeemed unit (list price
  minus offer price, averaged over the offer's price lines; a redeemed unit of a
  bundle is one bundle).
- ROI = incremental gross profit / discount cost (None when the discount cost is 0).
- stock cleared = redemptions - returns (units sold under the offer and kept).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Protocol

from offers.guardrails import PriceReader, _price_units
from offers.monitoring import CampaignStats
from offers.review import _jsonable
from offers.schema import Offer, OfferStatus

TWO_PLACES = Decimal("0.01")


class ImpactUnavailable(ValueError):
    """The impact cannot be calculated (missing KPI data, baseline or prices)."""


@dataclass(frozen=True)
class Metrics:
    units: int
    net_revenue: Decimal
    gross_profit: Decimal


class KpiReader(Protocol):
    """M3's KPI functions (C14 / H4). Return None when the data is not available."""

    def units(self, branch_id: str, product_ids: list[str], start: datetime, end: datetime) -> Optional[int]: ...
    def net_revenue(self, branch_id: str, product_ids: list[str], start: datetime, end: datetime) -> Optional[Decimal]: ...
    def gross_profit(self, branch_id: str, product_ids: list[str], start: datetime, end: datetime) -> Optional[Decimal]: ...


class BaselineReader(Protocol):
    """M5's baseline for the same products, branch and window (E5)."""

    def get_baseline(
        self, offer_id: str, version: int, branch_id: str, product_ids: list[str], start: datetime, end: datetime
    ) -> Optional[Metrics]: ...


@dataclass(frozen=True)
class ImpactReport:
    offer_id: str
    version: int
    is_final: bool
    window_start: datetime
    window_end: datetime
    actual: Metrics
    baseline: Metrics
    incremental: Metrics
    discount_cost: Decimal
    roi: Optional[Decimal]
    redemptions: int
    returns: int
    units_cleared: int
    stock_on_hand: int

    def to_dict(self) -> dict[str, Any]:
        def m(x: Metrics) -> dict[str, Any]:
            return {"units": x.units, "net_revenue": x.net_revenue, "gross_profit": x.gross_profit}

        return _jsonable(
            {
                "offer_id": self.offer_id,
                "version": self.version,
                "is_final": self.is_final,
                "window_start": self.window_start,
                "window_end": self.window_end,
                "actual": m(self.actual),
                "baseline": m(self.baseline),
                "incremental": m(self.incremental),
                "discount_cost": self.discount_cost,
                "roi": self.roi,
                "redemptions": self.redemptions,
                "returns": self.returns,
                "units_cleared": self.units_cleared,
                "stock_on_hand": self.stock_on_hand,
            }
        )


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES)


def average_discount_and_cost(offer: Offer, prices: PriceReader) -> tuple[Decimal, Decimal]:
    """(average discount per redeemed unit, average cost per redeemed unit)."""
    units, missing = _price_units(offer, prices)
    if missing:
        raise ImpactUnavailable("; ".join(missing))
    n = Decimal(len(units))
    discount = sum((list_price - offer_price) for _l, list_price, offer_price, _c, _f in units) / n
    cost = sum(cost for _l, _lp, _op, cost, _f in units) / n
    return discount, cost


def offer_totals(offer: Offer, stats: CampaignStats, prices: PriceReader) -> dict[str, Any]:
    """The Offers-side records the daily report reads (H5): offer id, redemptions, discounts."""
    discount, _ = average_discount_and_cost(offer, prices)
    return _jsonable(
        {
            "offer_id": offer.offer_id,
            "version": offer.version,
            "redemptions": stats.redemptions,
            "returns": stats.returns,
            "discount_amount": _money(discount * stats.redemptions),
        }
    )


def calculate_impact(
    offer: Offer,
    *,
    stats: CampaignStats,
    kpi: KpiReader,
    baseline: BaselineReader,
    prices: PriceReader,
    window_end: datetime | None = None,
    now: datetime | None = None,
) -> ImpactReport:
    now = now or datetime.now(timezone.utc)

    if offer.status not in (OfferStatus.ACTIVE, OfferStatus.STOPPED, OfferStatus.ENDED):
        raise ImpactUnavailable(f"offer never ran, status is {offer.status.value}")
    is_final = offer.status in (OfferStatus.STOPPED, OfferStatus.ENDED)

    if window_end is None:
        if offer.status == OfferStatus.STOPPED:
            raise ImpactUnavailable("window_end (the stop time) is required for a stopped offer")
        window_end = offer.end_date if offer.status == OfferStatus.ENDED else min(now, offer.end_date)
    if window_end <= offer.start_date:
        raise ImpactUnavailable("the campaign window is empty")

    products = sorted(set(offer.product_ids) | {i.product_id for i in (offer.bundle_items or [])})
    args = (offer.branch_id, products, offer.start_date, window_end)

    units = kpi.units(*args)
    revenue = kpi.net_revenue(*args)
    profit = kpi.gross_profit(*args)
    if units is None or revenue is None or profit is None:
        raise ImpactUnavailable("KPI data is not available for the campaign window")

    base = baseline.get_baseline(offer.offer_id, offer.version, *args)
    if base is None:
        raise ImpactUnavailable("no baseline for the campaign window")

    actual = Metrics(int(units), _money(revenue), _money(profit))
    incremental = Metrics(
        actual.units - base.units,
        _money(actual.net_revenue - base.net_revenue),
        _money(actual.gross_profit - base.gross_profit),
    )

    discount, _ = average_discount_and_cost(offer, prices)
    discount_cost = _money(discount * stats.redemptions)
    roi = (
        (incremental.gross_profit / discount_cost).quantize(TWO_PLACES) if discount_cost > 0 else None
    )

    return ImpactReport(
        offer_id=offer.offer_id,
        version=offer.version,
        is_final=is_final,
        window_start=offer.start_date,
        window_end=window_end,
        actual=actual,
        baseline=Metrics(base.units, _money(base.net_revenue), _money(base.gross_profit)),
        incremental=incremental,
        discount_cost=discount_cost,
        roi=roi,
        redemptions=stats.redemptions,
        returns=stats.returns,
        units_cleared=max(0, stats.redemptions - stats.returns),
        stock_on_hand=stats.stock_on_hand,
    )
